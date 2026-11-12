import asyncio, base64, calendar, hashlib, hmac, json, math, os, random, sqlite3, time, uuid
from dotenv import load_dotenv
load_dotenv()
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Request
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import db, rules, extract, gauntlet, email_service
from data import VENDORS, TRUST_THRESHOLD
from samples import SAMPLES

app = FastAPI()
db.init()
MIMES = {"application/pdf", "image/png", "image/jpeg", "image/webp"}
EXT = {".pdf": "application/pdf", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}
MAX_UPLOAD_BYTES = 5 * 1024 * 1024
UPLOADS_PER_HOUR = 10
UPLOAD_RATE_WINDOW = 60 * 60
UPLOAD_RATE_LIMITS = {}


@app.get("/healthz")
def healthz():
    c = db.conn()
    try:
        c.execute("select 1")
    finally:
        c.close()
    return {"status": "ok"}


def _check_upload_rate(request):
    ip = request.client.host if request.client else "unknown"
    now = time.monotonic()
    recent = [
        timestamp for timestamp in UPLOAD_RATE_LIMITS.get(ip, [])
        if now - timestamp < UPLOAD_RATE_WINDOW
    ]
    if len(recent) >= UPLOADS_PER_HOUR:
        UPLOAD_RATE_LIMITS[ip] = recent
        raise HTTPException(429, "Upload limit reached; try again in an hour")
    recent.append(now)
    UPLOAD_RATE_LIMITS[ip] = recent


def _require_admin(request):
    expected = os.getenv("ADMIN_TOKEN", "")
    if not expected:
        return
    supplied = request.headers.get("X-Admin-Token") or request.query_params.get("admin_token", "")
    if not hmac.compare_digest(supplied, expected):
        raise HTTPException(403, "A valid ADMIN_TOKEN is required")


async def _validated_upload(request, file):
    _check_upload_rate(request)
    extension_mime = EXT.get(os.path.splitext(file.filename or "")[1].lower())
    content_type = (file.content_type or "").lower()
    if content_type in MIMES:
        mime = content_type
        if extension_mime and extension_mime != mime:
            raise HTTPException(400, "File extension does not match the uploaded type")
    elif content_type in ("", "application/octet-stream") and extension_mime:
        mime = extension_mime
    else:
        raise HTTPException(400, "Upload a PDF, PNG, JPG or WebP")
    data = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "Maximum upload size is 5 MB")
    return data, mime

def ev(**k): return json.dumps(k) + "\n"
def S(name, status, summary="", ms=None): return ev(type="stage", name=name, status=status, summary=summary, ms=ms)


def _email_history(c, run_id):
    rows = c.execute(
        "select id,recipient,subject,body,status,error,created_at,attempted_at,completed_at "
        "from email_notifications where run_id=? order by id",
        (run_id,),
    ).fetchall()
    return [dict(row) for row in rows]


async def _send_pending_email(notification_id):
    c = db.conn()
    attempted_at = time.time()
    with c:
        claimed = c.execute(
            "update email_notifications set attempted_at=?,error=null "
            "where id=? and status='PENDING' and attempted_at is null",
            (attempted_at, notification_id),
        ).rowcount
        notification = c.execute(
            "select * from email_notifications where id=?", (notification_id,)
        ).fetchone()
    if not notification:
        c.close()
        raise HTTPException(404, "Email notification not found")
    if not claimed:
        c.close()
        return dict(notification)

    delivery = await asyncio.to_thread(
        email_service.send_email,
        notification["recipient"],
        notification["subject"],
        notification["body"],
    )
    completed_at = time.time()
    with c:
        c.execute(
            "update email_notifications set recipient=?,status=?,error=?,completed_at=? where id=?",
            (delivery["recipient"], delivery["status"], delivery["error"],
             completed_at, notification_id),
        )
        c.execute(
            "insert into vendor_emails(run_id,vendor_id,recipient,subject,body,status,error,attempted_at,completed_at) "
            "values(?,?,?,?,?,?,?,?,?) on conflict(run_id) do update set "
            "recipient=excluded.recipient,subject=excluded.subject,body=excluded.body,"
            "status=excluded.status,error=excluded.error,attempted_at=excluded.attempted_at,"
            "completed_at=excluded.completed_at",
            (notification["run_id"], notification["vendor_id"], delivery["recipient"],
             notification["subject"], notification["body"], delivery["status"],
             delivery["error"], attempted_at, completed_at),
        )
        run = c.execute("select decision from runs where id=?",
                        (notification["run_id"],)).fetchone()
        if run:
            decision = json.loads(run["decision"])
            decision["vendor_email"] = {
                "notification_id": notification_id,
                "recipient": delivery["recipient"],
                "subject": notification["subject"],
                "body": notification["body"],
                "status": delivery["status"],
                "error": delivery["error"],
                "attempted_at": attempted_at,
                "completed_at": completed_at,
            }
            c.execute("update runs set decision=? where id=?",
                      (json.dumps(decision), notification["run_id"]))
    result = c.execute(
        "select id,recipient,subject,status,error,created_at,attempted_at,completed_at "
        "from email_notifications where id=?", (notification_id,)
    ).fetchone()
    c.close()
    return dict(result)


async def pipeline(
    filename, data, mime, source, sample=None, sender=None, original_run_id=None
):
    raw = data if data is not None else json.dumps(sample, sort_keys=True).encode()
    file_hash = hashlib.sha256(raw).hexdigest()
    run_id = uuid.uuid4().hex[:8]
    c = db.conn()
    try:
        with c:
            claimed = c.execute(
                "insert or ignore into processing_claims(file_hash,run_id,created_at) values(?,?,?)",
                (file_hash, run_id, time.time()),
            ).rowcount
        if not claimed:
            yield ev(type="start", run_id=run_id)
            yield S("Ingest", "error", "This invoice file is already being processed")
            yield ev(type="error", message="This invoice file is already being processed")
            return
        async for event in _pipeline(
            filename, data, mime, source, sample, run_id, file_hash, sender,
            original_run_id,
        ):
            yield event
    finally:
        with c:
            c.execute("delete from processing_claims where file_hash=? and run_id=?",
                      (file_hash, run_id))
        c.close()


async def _pipeline(
    filename, data, mime, source, sample, run_id, fh, sender, original_run_id
):
    c = db.conn()
    try:
        async for event in _pipeline_run(
            filename, data, mime, source, sample, run_id, fh, sender,
            original_run_id, c,
        ):
            yield event
    finally:
        c.close()


async def _pipeline_run(
    filename, data, mime, source, sample, run_id, fh, sender, original_run_id, c
):
    raw = data if data is not None else json.dumps(sample, sort_keys=True).encode()
    yield ev(type="start", run_id=run_id)
    yield S("Ingest", "done", f"{filename}, {max(1, len(raw) // 1024)} KB, sha256 {fh[:8]}, via {source}")

    yield S("Extract", "running", f"Reading the invoice ({extract.backend()} extractor)" if data is not None else "Loading sample fixture (no LLM call)")
    t = time.time()
    try:
        if data is not None:
            ex, extractor_name = await extract.extract(data, mime)
        else:
            ex, extractor_name = sample, "sample fixture"
    except Exception as e:
        yield S("Extract", "error", str(e)); yield ev(type="error", message=str(e)); return
    await asyncio.sleep(0.3)
    yield S("Extract", "done", f"Extractor: {extractor_name} | {ex.get('vendor_name')}, invoice {ex.get('invoice_number')}, total {ex.get('total')}", int((time.time() - t) * 1000))

    if original_run_id:
        superseded = c.execute(
            "update runs set status='superseded' "
            "where id=? and action='HOLD_FOR_INFO' and status='pending'",
            (original_run_id,),
        ).rowcount
        if not superseded:
            message = "The held invoice was already superseded or is no longer eligible"
            yield S("Ingest", "error", message)
            yield ev(type="error", message=message)
            return

    yield S("Match vendor", "running"); await asyncio.sleep(0.3)
    vid, vscore = rules.match_vendor(ex.get("vendor_name"))
    vcheck = rules.vendor_check(vid, vscore, ex.get("vendor_name"))
    row = c.execute("select points from trust where vendor_id=?", (vid,)).fetchone() if vid else None
    pts = row["points"] if row else 0
    yield S("Match vendor", "done", vcheck["detail"] + (f" | trust {pts}/{TRUST_THRESHOLD}" if vid else ""))

    verified_sender = sender if source == "email" else None
    yield S("Check sender domain", "running")
    sender_check = rules.sender_domain_check(verified_sender, vid)
    sender_stage_status = "error" if sender_check["status"] == "fail" else "done"
    yield S("Check sender domain", sender_stage_status, sender_check["detail"])

    yield S("Match PO", "running"); await asyncio.sleep(0.3)
    po, method, pconf = rules.resolve_po(ex, vid, c)
    pochecks = rules.po_checks(ex, vid, po, method)
    yield S("Match PO", "done", pochecks[0]["detail"])

    yield S("Run rules", "running"); await asyncio.sleep(0.3)
    checks = [rules.r1(ex), rules.confidence_check(ex), vcheck, sender_check] + pochecks + [
        rules.dup_check(ex, vid, fh, c), rules.bank_check(ex, vid), rules.arithmetic(ex)
    ]
    n_bad = sum(1 for x in checks if x["status"] not in ("pass", "not_applicable"))
    yield S("Run rules", "done", f"{len(checks) - n_bad} passed, {n_bad} flagged")

    yield S("Decide", "running"); await asyncio.sleep(0.3)
    d = rules.decide(ex, vid, vscore, po, method, pconf, checks, pts)
    every = int(os.getenv("AUDIT_EVERY", "10"))
    if d["action"] == "AUTO_APPROVE" and every and int(fh[:8], 16) % every == 0:
        d["action"] = "REVIEW"
        d["reasons"].append(f"Audit sample: 1 in {every} auto-approvals is checked by a human")
    status = {"AUTO_APPROVE": "auto_approved", "AUTO_REJECT": "auto_rejected"}.get(d["action"], "pending")
    email = d.get("vendor_email")
    notification_id = None
    send_email = False
    if email:
        issues = email_service.critical_field_issues(ex)
        content = email_service.build_information_request(
            ex.get("vendor_name"), ex, issues
        )
        recipient_row = c.execute(
            "select email from vendors where vendor_id=?", (vid,)
        ).fetchone() if vid else None
        recipient = recipient_row["email"] if recipient_row else None
        key = email_service.notification_key(vid, ex, fh, issues)
        existing = c.execute(
            "select id,recipient,subject,body,status,error,created_at,attempted_at,completed_at "
            "from email_notifications where notification_key=? order by id desc limit 1", (key,)
        ).fetchone()
        email.update(content)
        if existing:
            notification_id = existing["id"]
            email.update(
                notification_id=notification_id,
                recipient=existing["recipient"],
                status=existing["status"],
                error=existing["error"],
                duplicate_prevented=True,
                attempted_at=existing["attempted_at"],
                completed_at=existing["completed_at"],
            )
        else:
            can_send, draft_reason = email_service.delivery_eligibility(recipient)
            email.update(
                recipient=recipient or None,
                status="PENDING" if can_send else "OUTBOX",
                error=None if can_send else draft_reason,
                attempted_at=None,
                completed_at=None,
            )
            send_email = can_send
    c.execute(
        "insert into runs"
        "(id,created_at,filename,source,file_hash,vendor_id,po_id,invoice_number,invoice_date,total,"
        "action,recommendation,status,decision,sender,original_run_id) "
        "values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (run_id, time.time(), filename, source, fh, vid, po["id"] if po else None,
         ex.get("invoice_number"), ex.get("invoice_date"), ex.get("total"),
         d["action"], d["recommendation"], status, json.dumps(d), verified_sender,
         original_run_id),
    )
    if email and (send_email or email.get("status") == "OUTBOX"):
        key = email_service.notification_key(vid, ex, fh, issues)
        inserted = c.execute(
            "insert or ignore into email_notifications"
            "(run_id,vendor_id,dedupe_key,notification_key,attempt_number,recipient,subject,body,status,error,created_at) "
            "values(?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, vid, f"{key}:1", key, 1, email["recipient"], email["subject"], email["body"],
             email["status"], email["error"], time.time()),
        )
        if inserted.rowcount:
            notification_id = inserted.lastrowid
            email["notification_id"] = notification_id
        else:
            existing = c.execute(
                "select id,recipient,status,error,attempted_at,completed_at "
                "from email_notifications where notification_key=? order by id desc limit 1", (key,)
            ).fetchone()
            notification_id = existing["id"]
            email.update(
                notification_id=notification_id,
                recipient=existing["recipient"],
                status=existing["status"],
                error=existing["error"],
                duplicate_prevented=True,
                attempted_at=existing["attempted_at"],
                completed_at=existing["completed_at"],
            )
            send_email = False
            c.execute("update runs set decision=? where id=?", (json.dumps(d), run_id))
    c.commit()
    yield S("Decide", "done", f"{d['action']} (confidence {d['confidence']})")
    if email and send_email:
        yield S("Email vendor", "running", "Sending the missing-information request")
        delivery = await _send_pending_email(notification_id)
        email.update(delivery)
        c.execute("update runs set decision=? where id=?", (json.dumps(d), run_id))
        c.commit()
        if delivery["status"] == "SENT":
            yield S("Email vendor", "done", f"SMTP server accepted the email to {delivery['recipient']}")
        else:
            yield S("Email vendor", "error", f"Email failed: {delivery['error']}")
    elif email:
        if email.get("duplicate_prevented"):
            summary = "A matching email notification already exists; duplicate send prevented"
        elif email.get("status") == "OUTBOX":
            summary = email.get("error") or "Drafted; email was not sent"
        else:
            summary = "Email notification is recorded but was not sent"
        yield S("Email vendor", "done", summary)
    yield ev(type="decision", run_id=run_id, status=status, decision=d)

def stream(gen): return StreamingResponse(gen, media_type="application/x-ndjson")

@app.post("/api/run")
async def run(request: Request, file: UploadFile = File(...), source: str = Form("upload")):
    data, mime = await _validated_upload(request, file)
    return stream(pipeline(file.filename, data, mime, source))


@app.post("/api/runs/{rid}/resubmit")
async def resubmit(rid: str, request: Request, file: UploadFile = File(...)):
    c = db.conn()
    try:
        original = c.execute(
            "select action,status from runs where id=?", (rid,)
        ).fetchone()
    finally:
        c.close()
    if not original:
        raise HTTPException(404, "Invoice run not found")
    if original["action"] != "HOLD_FOR_INFO" or original["status"] != "pending":
        raise HTTPException(409, "Only a held invoice awaiting information can be resubmitted")
    data, mime = await _validated_upload(request, file)
    return stream(
        pipeline(
            file.filename, data, mime, "resubmission",
            original_run_id=rid,
        )
    )
@app.post("/api/sample/{key}")
async def sample(key: str):
    if key not in SAMPLES: raise HTTPException(404)
    return stream(pipeline(f"sample-{key}.json", None, None, "sample", SAMPLES[key]["data"]))

@app.get("/api/samples")
def samples(): return [dict(key=k, label=v["label"]) for k, v in SAMPLES.items()]

class Review(BaseModel): verdict: str

@app.post("/api/runs/{rid}/review")
def review(rid: str, body: Review):
    c = db.conn()
    r = c.execute("select * from runs where id=?", (rid,)).fetchone()
    if not r: raise HTTPException(404)
    if r["status"] != "pending": raise HTTPException(409, "Already reviewed")
    v = body.verdict
    if v not in ("approve", "reject"): raise HTTPException(400)
    agrees = (v == "approve") == (r["recommendation"] == "approve")
    delta, col = (1, "confirms") if agrees and v == "approve" else (0, "confirms") if agrees else (-3, "overrides")
    if r["vendor_id"]:
        c.execute("insert or ignore into trust(vendor_id) values(?)", (r["vendor_id"],))
        c.execute(f"update trust set points=max(0,points+?), {col}={col}+1 where vendor_id=?", (delta, r["vendor_id"]))
    c.execute("update runs set status=? where id=?", ("approved" if v == "approve" else "rejected", rid))
    c.commit()
    return dict(status="approved" if v == "approve" else "rejected", trust_delta=delta)


DECISION_ACTIONS = {"AUTO_APPROVE", "AUTO_REJECT", "REVIEW", "HOLD_FOR_INFO", "ESCALATE"}
RUN_STATUSES = {"pending", "approved", "rejected", "auto_approved", "auto_rejected", "superseded"}
EMAIL_STATUSES = {"SENT", "FAILED", "PENDING", "OUTBOX", "NOT_REQUIRED"}


def _filters(date_from, date_to, vendor_id, action, status, email_status):
    where, params = ["r.source!='seeded'"], []
    try:
        if date_from:
            start = calendar.timegm(time.strptime(date_from, "%Y-%m-%d"))
            where.append("r.created_at>=?")
            params.append(start)
        if date_to:
            end = calendar.timegm(time.strptime(date_to, "%Y-%m-%d")) + 86400
            where.append("r.created_at<?")
            params.append(end)
    except ValueError as exc:
        raise HTTPException(400, "Dates must use YYYY-MM-DD format") from exc
    if date_from and date_to and date_from > date_to:
        raise HTTPException(400, "date_from must not be after date_to")
    if vendor_id:
        if vendor_id not in VENDORS:
            raise HTTPException(400, "Unknown vendor")
        where.append("r.vendor_id=?")
        params.append(vendor_id)
    if action:
        action = action.upper()
        if action not in DECISION_ACTIONS:
            raise HTTPException(400, "Unknown decision type")
        where.append("r.action=?")
        params.append(action)
    if status:
        if status not in RUN_STATUSES:
            raise HTTPException(400, "Unknown invoice status")
        where.append("r.status=?")
        params.append(status)
    if email_status:
        email_status = email_status.upper()
        if email_status not in EMAIL_STATUSES:
            raise HTTPException(400, "Unknown email delivery status")
        where.append(
            "coalesce((select e.status from email_notifications e where e.run_id=r.id "
            "order by e.id desc limit 1),'NOT_REQUIRED')=?"
        )
        params.append(email_status)
    return where, params


def _invoice_rows(c, where, params):
    return [
        dict(row) for row in c.execute(
            "select r.id,r.created_at,r.filename,r.source,r.vendor_id,r.po_id,"
            "r.invoice_number,r.invoice_date,r.total,r.action,r.recommendation,r.status,"
            "coalesce(v.name,'Unknown') vendor,"
            "coalesce((select e.status from email_notifications e where e.run_id=r.id "
            "order by e.id desc limit 1),'NOT_REQUIRED') email_status,"
            "(select e.recipient from email_notifications e where e.run_id=r.id "
            "order by e.id desc limit 1) email_recipient,"
            "(select e.subject from email_notifications e where e.run_id=r.id "
            "order by e.id desc limit 1) email_subject,"
            "(select e.completed_at from email_notifications e where e.run_id=r.id "
            "order by e.id desc limit 1) email_timestamp,"
            "(select e.error from email_notifications e where e.run_id=r.id "
            "order by e.id desc limit 1) email_error "
            "from runs r left join vendors v on v.vendor_id=r.vendor_id where "
            + " and ".join(where) + " order by r.created_at desc",
            params,
        ).fetchall()
    ]


@app.get("/api/runs")
def runs(date_from: str = None, date_to: str = None, vendor_id: str = None,
         action: str = None, status: str = None, email_status: str = None,
         limit: int = 500):
    if not 1 <= limit <= 1000:
        raise HTTPException(400, "limit must be between 1 and 1000")
    where, params = _filters(date_from, date_to, vendor_id, action, status, email_status)
    c = db.conn()
    try:
        return _invoice_rows(c, where, params)[:limit]
    finally:
        c.close()

@app.get("/api/runs/{rid}")
def run_detail(rid: str):
    c = db.conn()
    try:
        r = c.execute(
            "select decision,status,sender,original_run_id from runs where id=?", (rid,)
        ).fetchone()
        if not r: raise HTTPException(404)
        superseded = c.execute(
            "select id from runs where original_run_id=? order by created_at desc limit 1",
            (rid,),
        ).fetchone()
        return dict(
            status=r["status"],
            sender=r["sender"],
            original_run_id=r["original_run_id"],
            superseded_by=superseded["id"] if superseded else None,
            decision=json.loads(r["decision"]),
            email_history=_email_history(c, rid),
        )
    finally:
        c.close()


@app.post("/api/runs/{rid}/emails/{email_id}/resend")
async def resend_email(rid: str, email_id: int):
    c = db.conn()
    try:
        with c:
            notification = c.execute(
                "select * from email_notifications where id=? and run_id=?",
                (email_id, rid),
            ).fetchone()
            if not notification:
                raise HTTPException(404, "Email notification not found")
            if notification["status"] != "FAILED":
                raise HTTPException(409, "Only failed email notifications can be resent")
            claimed = c.execute(
                "update email_notifications set resend_claimed_at=? "
                "where id=? and status='FAILED' and resend_claimed_at is null",
                (time.time(), email_id),
            ).rowcount
            if not claimed:
                raise HTTPException(409, "This failed notification has already been retried")
            attempt = c.execute(
                "select coalesce(max(attempt_number),0)+1 from email_notifications where notification_key=?",
                (notification["notification_key"],),
            ).fetchone()[0]
            vendor_contact = c.execute(
                "select email from vendors where vendor_id=?",
                (notification["vendor_id"],),
            ).fetchone()
            recipient = vendor_contact["email"] if vendor_contact else notification["recipient"]
            retry = c.execute(
                "insert into email_notifications"
                "(run_id,vendor_id,dedupe_key,notification_key,attempt_number,recipient,subject,body,status,created_at) "
                "values(?,?,?,?,?,?,?,?,?,?)",
                (rid, notification["vendor_id"],
                 f"{notification['notification_key']}:{attempt}",
                 notification["notification_key"], attempt, recipient,
                 notification["subject"], notification["body"], "PENDING", time.time()),
            )
            retry_id = retry.lastrowid
    finally:
        c.close()
    result = await _send_pending_email(retry_id)
    return result


@app.get("/api/vendors")
def vendors():
    c = db.conn()
    try:
        t = {r["vendor_id"]: r for r in c.execute("select * from trust").fetchall()}
        emails = {r["vendor_id"]: r["email"] for r in c.execute("select vendor_id,email from vendors")}
        return [dict(id=k, name=v["name"], email=emails.get(k, ""), points=t[k]["points"] if k in t else 0,
                     confirms=t[k]["confirms"] if k in t else 0,
                     overrides=t[k]["overrides"] if k in t else 0, threshold=TRUST_THRESHOLD) for k, v in VENDORS.items()]
    finally:
        c.close()


class VendorEmail(BaseModel):
    email: str


@app.put("/api/vendors/{vendor_id}/email")
def update_vendor_email(vendor_id: str, body: VendorEmail, request: Request):
    _require_admin(request)
    if vendor_id not in VENDORS:
        raise HTTPException(404, "Vendor not found")
    address = body.email.strip()
    if address and not email_service.valid_email(address):
        raise HTTPException(400, "Enter a valid email address or an empty string")
    c = db.conn()
    try:
        c.execute("update vendors set email=? where vendor_id=?", (address, vendor_id))
        c.commit()
        return dict(vendor_id=vendor_id, email=address)
    finally:
        c.close()

@app.get("/api/stats")
def stats():
    rows = db.conn().execute(
        "select status,source,created_at,action from runs where source!='seeded' order by created_at"
    ).fetchall()
    auto = sum(r["action"] in ("AUTO_APPROVE", "AUTO_REJECT") for r in rows)
    days = {}
    for r in rows:
        d = time.strftime("%Y-%m-%d", time.gmtime(r["created_at"]))
        b = days.setdefault(d, dict(day=d, total=0, auto=0, seeded=False))
        b["total"] += 1
        b["auto"] += r["action"] in ("AUTO_APPROVE", "AUTO_REJECT")
    total = len(rows)
    return dict(total=total, auto=auto,
                autonomy=round(100 * auto / total) if total else 0,
                series=list(days.values()))


@app.get("/api/analytics")
def analytics(date_from: str = None, date_to: str = None, vendor_id: str = None,
              action: str = None, status: str = None, email_status: str = None):
    where, params = _filters(date_from, date_to, vendor_id, action, status, email_status)
    c = db.conn()
    try:
        rows = _invoice_rows(c, where, params)
    finally:
        c.close()
    total = len(rows)
    auto_approved = sum(r["action"] == "AUTO_APPROVE" for r in rows)
    auto_rejected = sum(r["action"] == "AUTO_REJECT" for r in rows)
    manual_approved = sum(r["status"] == "approved" for r in rows)
    manual_rejected = sum(r["status"] == "rejected" for r in rows)
    pending_review = sum(r["action"] == "REVIEW" and r["status"] == "pending" for r in rows)
    holds = sum(r["action"] == "HOLD_FOR_INFO" for r in rows)
    escalated = sum(r["action"] == "ESCALATE" for r in rows)
    total_value = round(sum(float(r["total"] or 0) for r in rows), 2)
    automated = auto_approved + auto_rejected
    counts = {action: 0 for action in DECISION_ACTIONS}
    daily = {}
    monthly = {}
    by_vendor = {}
    email_counts = {status: 0 for status in EMAIL_STATUSES}
    for row in rows:
        counts[row["action"]] = counts.get(row["action"], 0) + 1
        email_counts[row["email_status"]] = email_counts.get(row["email_status"], 0) + 1
        day = time.strftime("%Y-%m-%d", time.gmtime(row["created_at"]))
        bucket = daily.setdefault(day, {"day": day, "total": 0, "automated": 0, "human": 0})
        bucket["total"] += 1
        bucket["automated"] += row["action"] in ("AUTO_APPROVE", "AUTO_REJECT")
        bucket["human"] += row["action"] not in ("AUTO_APPROVE", "AUTO_REJECT")
        month = day[:7]
        month_bucket = monthly.setdefault(month, {"month": month, "count": 0, "value": 0.0})
        month_bucket["count"] += 1
        month_bucket["value"] += float(row["total"] or 0)
        vendor_bucket = by_vendor.setdefault(row["vendor_id"], {
            "vendor_id": row["vendor_id"], "vendor": row["vendor"],
            "total": 0, "approved": 0, "rejected": 0, "escalated": 0,
            "auto_approved": 0, "auto_rejected": 0, "total_value": 0.0,
        })
        vendor_bucket["total"] += 1
        vendor_bucket["approved"] += row["status"] in ("approved", "auto_approved")
        vendor_bucket["rejected"] += row["status"] in ("rejected", "auto_rejected")
        vendor_bucket["escalated"] += row["action"] == "ESCALATE"
        vendor_bucket["auto_approved"] += row["action"] == "AUTO_APPROVE"
        vendor_bucket["auto_rejected"] += row["action"] == "AUTO_REJECT"
        vendor_bucket["total_value"] += float(row["total"] or 0)

    conn = db.conn()
    try:
        trust = {row["vendor_id"]: row["points"] for row in
                 conn.execute("select vendor_id,points from trust").fetchall()}
        vendor_emails = {row["vendor_id"]: row["email"] for row in
                         conn.execute("select vendor_id,email from vendors").fetchall()}
    finally:
        conn.close()
    vendor_filter = [vendor_id] if vendor_id else list(VENDORS)
    vendor_performance = []
    for vid in vendor_filter:
        vendor = VENDORS[vid]
        stats = by_vendor.get(vid, {
            "vendor_id": vid, "vendor": vendor["name"], "total": 0,
            "approved": 0, "rejected": 0, "escalated": 0,
            "auto_approved": 0, "auto_rejected": 0, "total_value": 0.0,
        })
        approved_rate = round(100 * stats["approved"] / stats["total"], 1) if stats["total"] else 0
        rejected_rate = round(100 * stats["rejected"] / stats["total"], 1) if stats["total"] else 0
        vendor_performance.append({
            **stats,
            "approved_rate": approved_rate,
            "rejected_rate": rejected_rate,
            "trust_score": trust.get(vid, 0),
            "trust_threshold": TRUST_THRESHOLD,
            "automation_eligible": vendor["active"] and trust.get(vid, 0) >= TRUST_THRESHOLD,
            "email": vendor_emails.get(vid, ""),
        })
    return {
        "filters": {
            "date_from": date_from, "date_to": date_to, "vendor_id": vendor_id,
            "action": action.upper() if action else None, "status": status,
            "email_status": email_status.upper() if email_status else None,
        },
        "kpis": {
            "total_processed": total,
            "auto_approved": auto_approved,
            "auto_rejected": auto_rejected,
            "pending_review": pending_review,
            "on_hold": holds,
            "escalated": escalated,
            "manual_approved": manual_approved,
            "manual_rejected": manual_rejected,
            "total_value": total_value,
            "automation_rate": round(100 * automated / total, 1) if total else 0,
        },
        "decision_distribution": [
            {"action": item, "count": counts.get(item, 0)}
            for item in ("AUTO_APPROVE", "AUTO_REJECT", "REVIEW", "HOLD_FOR_INFO", "ESCALATE")
        ],
        "trends": sorted(daily.values(), key=lambda item: item["day"]),
        "vendor_volume": sorted(
            ({"vendor_id": item["vendor_id"], "vendor": item["vendor"], "count": item["total"]}
             for item in by_vendor.values()),
            key=lambda item: item["count"], reverse=True,
        ),
        "vendor_performance": vendor_performance,
        "monthly_value": [
            {**item, "value": round(item["value"], 2)}
            for item in sorted(monthly.values(), key=lambda item: item["month"])
        ],
        "processing_mode": [
            {"mode": "Automated", "count": automated},
            {"mode": "Human", "count": total - automated},
        ],
        "email_delivery": [
            {"status": item, "count": email_counts.get(item, 0)}
            for item in ("SENT", "FAILED", "PENDING", "OUTBOX", "NOT_REQUIRED")
        ],
        "recent_activity": rows[:12],
    }


@app.get("/api/email-history")
def email_history(date_from: str = None, date_to: str = None, vendor_id: str = None,
                  action: str = None, status: str = None, email_status: str = None,
                  limit: int = 500):
    if not 1 <= limit <= 1000:
        raise HTTPException(400, "limit must be between 1 and 1000")
    where, params = _filters(date_from, date_to, vendor_id, action, status, None)
    if email_status:
        email_status = email_status.upper()
        if email_status not in EMAIL_STATUSES:
            raise HTTPException(400, "Unknown email delivery status")
        where.append("e.status=?")
        params.append(email_status)
    c = db.conn()
    try:
        rows = c.execute(
            "select e.id,e.run_id,e.vendor_id,e.recipient,e.subject,e.body,e.status,e.error,"
            "e.created_at,e.attempted_at,e.completed_at,r.invoice_number,r.po_id,r.total,"
            "coalesce(v.name,'Unknown') vendor,r.action,r.status invoice_status "
            "from email_notifications e join runs r on r.id=e.run_id "
            "left join vendors v on v.vendor_id=e.vendor_id where "
            + " and ".join(where) + " order by e.id desc limit ?",
            [*params, limit],
        ).fetchall()
    finally:
        c.close()
    return [dict(row) for row in rows]


@app.post("/api/reset")
def reset(request: Request):
    _require_admin(request)
    c = db.conn()
    c.execute("delete from vendor_emails where run_id in (select id from runs where source!='seeded')")
    c.execute("delete from email_notifications where run_id in (select id from runs where source!='seeded')")
    c.execute("delete from runs where source!='seeded'")
    c.execute("delete from trust")
    c.execute("delete from tol_override")
    c.commit()
    for k, v in DEFAULT_TOL.items(): VENDORS[k]["tol"] = v
    return dict(ok=True)

def seed():
    """30 days of simulated history so the autonomy curve is never empty. Marked source='seeded'."""
    c = db.conn()
    try:
        c.execute("delete from runs where source='seeded'")
        rnd, now, vids = random.Random(7), time.time(), list(VENDORS)
        for d in range(30):
            for k in range(rnd.choice([1, 2, 2, 3])):
                auto = rnd.random() < min(0.75, 0.05 + 0.025 * d)
                vid = rnd.choice(vids)
                dec = dict(action="AUTO_APPROVE" if auto else "REVIEW", recommendation="approve", confidence=0.9,
                           vendor=dict(id=vid, name=VENDORS[vid]["name"]), po={}, invoice={}, checks=[],
                           reasons=["Simulated history, seeded for the demo"], hard_stops=[], what_would_change_it=[], vendor_email=None)
                c.execute("insert into runs"
                          "(id,created_at,filename,source,file_hash,vendor_id,po_id,invoice_number,"
                          "invoice_date,total,action,recommendation,status,decision) "
                          "values(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                          (uuid.uuid4().hex[:8], now - (30 - d) * 86400 + k * 3600, "seeded.json", "seeded", uuid.uuid4().hex,
                           vid, None, f"SEED-{d}-{k}", None, round(rnd.uniform(300, 6000), 2), dec["action"], "approve",
                           "auto_approved" if auto else "approved", json.dumps(dec)))
        c.commit()
    finally:
        c.close()

_seed_check = db.conn()
try:
    has_seeded_runs = _seed_check.execute(
        "select 1 from runs where source='seeded' limit 1"
    ).fetchone()
finally:
    _seed_check.close()
if not has_seeded_runs:
    seed()


# ---- tolerance overrides and rule proposals ----
DEFAULT_TOL = {k: v["tol"] for k, v in VENDORS.items()}
_c = db.conn()
try:
    _c.execute("create table if not exists tol_override(vendor_id text primary key, tol real)")
    for _r in _c.execute("select * from tol_override"):
        VENDORS[_r["vendor_id"]]["tol"] = _r["tol"]
    _c.commit()
finally:
    _c.close()

@app.get("/api/proposals")
def proposals():
    over = {}
    c = db.conn()
    try:
        rows = c.execute(
            "select vendor_id,total,decision from runs "
            "where status='approved' and source!='seeded' and vendor_id is not null"
        ).fetchall()
    finally:
        c.close()
    for r in rows:
        rem = (json.loads(r["decision"]).get("po") or {}).get("remaining")
        if rem and r["total"] and r["total"] > rem:
            over.setdefault(r["vendor_id"], []).append((r["total"] - rem) / rem)
    out = []
    for vid, ov in over.items():
        prop = math.ceil(max(ov) * 200) / 2   # percent, rounded up to the nearest 0.5
        if len(ov) >= 3 and prop / 100 > VENDORS[vid]["tol"]:
            out.append(dict(
                vendor_id=vid,
                vendor=VENDORS[vid]["name"],
                count=len(ov),
                max_over=round(max(ov) * 100, 1),
                proposed=prop,
                backtest=_backtest_tolerance(vid, prop / 100),
            ))
    return out


def _backtest_tolerance(vendor_id, tolerance):
    source = db.conn()
    try:
        rows = source.execute(
            "select * from runs where vendor_id=? and source!='seeded' "
            "order by created_at,id",
            (vendor_id,),
        ).fetchall()
    finally:
        source.close()

    history = sqlite3.connect(":memory:")
    history.row_factory = sqlite3.Row
    history.execute("""
        create table runs(
            id text primary key, created_at real, filename text, source text, file_hash text,
            vendor_id text, po_id text, invoice_number text, invoice_date text, total real,
            action text, recommendation text, status text, decision text)
    """)
    changed = extra_auto_approvals = later_rejected = 0
    try:
        for row in rows:
            decision = json.loads(row["decision"])
            invoice = decision.get("invoice")
            vendor = decision.get("vendor") or {}
            if not isinstance(invoice, dict) or not vendor:
                raise HTTPException(
                    409,
                    f"Cannot backtest run {row['id']}: stored invoice decision is incomplete",
                )
            match_score = float(vendor.get("match", 0))
            trust_points = int(vendor.get("trust_points", 0))
            po, method, po_confidence = rules.resolve_po(
                invoice, vendor_id, history, tolerance=tolerance
            )
            checks = [
                rules.r1(invoice),
                rules.confidence_check(invoice),
                rules.vendor_check(vendor_id, match_score, invoice.get("vendor_name")),
                rules.sender_domain_check(
                    row["sender"] if row["source"] == "email" else None,
                    vendor_id,
                ),
                *rules.po_checks(invoice, vendor_id, po, method, tolerance=tolerance),
                rules.dup_check(invoice, vendor_id, row["file_hash"], history),
                rules.bank_check(invoice, vendor_id),
                rules.arithmetic(invoice),
            ]
            replay = rules.decide(
                invoice, vendor_id, match_score, po, method, po_confidence,
                checks, trust_points,
            )
            audit_every = int(os.getenv("AUDIT_EVERY", "10"))
            if (
                replay["action"] == "AUTO_APPROVE"
                and audit_every
                and int(row["file_hash"][:8], 16) % audit_every == 0
            ):
                replay["action"] = "REVIEW"
            action = replay["action"]
            if action != row["action"]:
                changed += 1
            if action == "AUTO_APPROVE" and row["action"] != "AUTO_APPROVE":
                extra_auto_approvals += 1
                later_rejected += row["status"] == "rejected"

            history.execute(
                "insert into runs values(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                tuple(row[column] for column in (
                    "id", "created_at", "filename", "source", "file_hash",
                    "vendor_id", "po_id", "invoice_number", "invoice_date", "total",
                    "action", "recommendation", "status", "decision",
                )),
            )
        return {
            "decisions_would_change": changed,
            "extra_auto_approval_eligible": extra_auto_approvals,
            "later_human_rejections": later_rejected,
        }
    finally:
        history.close()

class Tol(BaseModel):
    vendor_id: str
    tol: float

@app.post("/api/proposals/accept")
def accept(b: Tol, request: Request):
    _require_admin(request)
    c = db.conn(); c.execute("insert or replace into tol_override values(?,?)", (b.vendor_id, b.tol)); c.commit()
    VENDORS[b.vendor_id]["tol"] = b.tol
    return dict(ok=True)

# ---- the Gauntlet ----
@app.post("/api/gauntlet")
def run_gauntlet(): return gauntlet.run()

# ---- email ingestion (Postmark-style inbound JSON) ----
LIVE = []
async def live_run(name, data, mime, source, sender=None):
    async for l in pipeline(name, data, mime, source, sender=sender): LIVE.append(json.loads(l))

@app.post("/api/inbound/{token}")
async def inbound(token: str, req: Request):
    if token != os.getenv("INBOUND_TOKEN", "dev-token"): raise HTTPException(403)
    p = await req.json()
    ok = [a for a in p.get("Attachments", []) if a.get("ContentType") in MIMES][:3]
    for a in ok:
        encoded = a.get("Content", "")
        if not isinstance(encoded, str) or len(encoded) > ((MAX_UPLOAD_BYTES + 2) // 3) * 4:
            raise HTTPException(413, "Maximum attachment size is 5 MB")
        try:
            attachment = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError) as exc:
            raise HTTPException(400, "Inbound attachment must be valid base64") from exc
        if len(attachment) > MAX_UPLOAD_BYTES:
            raise HTTPException(413, "Maximum attachment size is 5 MB")
        asyncio.create_task(
            live_run(
                a.get("Name", "attachment"), attachment, a["ContentType"], "email",
                sender=str(p.get("From", "")),
            )
        )
    return dict(queued=len(ok))

@app.get("/api/live")
def live(after: int = 0): return dict(events=LIVE[after:], cursor=len(LIVE))

app.mount("/", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "static"), html=True), name="static")
