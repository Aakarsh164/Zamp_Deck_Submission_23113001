import re, time, difflib, datetime as dt
from email.utils import parseaddr
from data import *

CRIT = ["vendor_name", "invoice_number", "invoice_date", "total"]

def ck(rule, status, detail, hard=False, fix=None):
    return dict(rule=rule, status=status, detail=detail, hard=hard, fix=fix)

def norm(s):
    return re.sub(r"[^A-Z0-9]", "", (s or "").upper())

def lev(a, b):
    prev = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        cur = [i]
        for j, y in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[-1] + 1, prev[j - 1] + (x != y)))
        prev = cur
    return prev[-1]

def missing(ex):
    return [k for k in CRIT if ex.get(k) in (None, "")]

def r1(ex):
    m = missing(ex)
    if m:
        return ck("R1", "fail", "Missing critical field(s): " + ", ".join(m),
                  fix="Provide the missing field(s): " + ", ".join(m))
    return ck("R1", "pass", "All critical fields present")

def field_confidences(ex):
    reported = ex.get("confidence")
    valid_mapping = isinstance(reported, dict)
    if not valid_mapping:
        reported = {}
    values = []
    missing_fields = missing(ex)
    for field in CRIT:
        if field in missing_fields:
            continue
        value = reported.get(field, 0.9 if valid_mapping else 0)
        try:
            value = float(value)
        except (TypeError, ValueError):
            value = 0
        if not 0 <= value <= 1:
            value = 0
        values.append(value)
    return values

def confidence_check(ex):
    values = field_confidences(ex)
    if not values:
        return ck("R8", "pass", "No present critical fields require confidence verification")

    confidence = min(values)
    if confidence < HUMAN_REVIEW_CONFIDENCE:
        return ck(
            "R8", "warn",
            f"Low extraction confidence ({confidence:.0%}); a human must verify the extracted fields",
            fix="Compare low-confidence fields with the original invoice"
        )
    if confidence < AUTO_APPROVE_CONFIDENCE:
        return ck(
            "R8", "warn",
            f"Extraction confidence is {confidence:.0%}; additional human verification is required",
            fix="Verify the extracted fields against the original invoice"
        )
    return ck("R8", "pass", f"Critical-field extraction confidence is {confidence:.0%}")

def arithmetic(ex):
    tot = ex.get("total")
    if tot is None:
        return ck("R7", "warn", "Total missing, arithmetic not checked")

    items = ex.get("line_items") or []
    ssum = round(sum(i.get("amount") or 0 for i in items), 2)
    sub = ex["subtotal"] if ex.get("subtotal") is not None else ssum
    tax = 0 if ex.get("tax_included") else (ex.get("tax") or 0)

    if items and abs(ssum - sub) > 1:
        return ck("R7", "fail",
                  f"Line items sum to {ssum:,.2f} but subtotal is {sub:,.2f}",
                  fix="Get a corrected invoice")

    if abs(sub + tax - tot) > 1:
        return ck("R7", "fail",
                  f"Subtotal {sub:,.2f} + tax {tax:,.2f} = {sub + tax:,.2f}, but total is {tot:,.2f}",
                  fix="Get a corrected invoice")

    return ck("R7", "pass", "Line items, tax and total reconcile")

def match_vendor(name):
    best = (None, 0.0)
    for vid, v in VENDORS.items():
        r = difflib.SequenceMatcher(
            None, (name or "").lower(), v["name"].lower()
        ).ratio()
        if r > best[1]:
            best = (vid, r)
    return best if best[1] >= 0.75 else (None, best[1])

def vendor_check(vid, score, name):
    if not vid:
        return ck("R2", "fail", f"'{name}' is not an approved vendor", hard=True,
                  fix="Add the vendor to the approved master after procurement review")

    v = VENDORS[vid]
    if not v["active"]:
        return ck("R2", "fail", f"{v['name']} is inactive", hard=True,
                  fix="Reactivate the vendor first")

    return ck("R2", "pass", f"Approved vendor: {v['name']} (name match {score:.0%})")

def po_state(po_id, c):
    po = POS[po_id]
    s = c.execute(
        "select coalesce(sum(total),0) s from runs "
        "where po_id=? and status in ('approved','auto_approved')",
        (po_id,)
    ).fetchone()["s"]

    inv = po["invoiced"] + s
    return dict(
        id=po_id,
        vendor=po["vendor"],
        amount=po["amount"],
        invoiced_before=round(inv, 2),
        remaining=round(po["amount"] - inv, 2)
    )

def resolve_po(ex, vid, c, tolerance=None):
    ref = norm(ex.get("po_reference"))

    if ref:
        for pid in POS:
            if norm(pid) == ref:
                return po_state(pid, c), "explicit", 0.95
        return None, "explicit-not-found", 0.0

    if vid:
        tol = VENDORS[vid]["tol"] if tolerance is None else tolerance
        tot = ex.get("total") or 0
        cands = [po_state(p, c) for p in POS if POS[p]["vendor"] == vid]
        fit = [
            p for p in cands
            if p["remaining"] > 0 and p["remaining"] * (1 + tol) >= tot
        ]
        if len(fit) == 1:
            return fit[0], "implied", 0.6
        return None, ("ambiguous" if fit else "none"), 0.0

    return None, "none", 0.0

def po_checks(ex, vid, po, method, tolerance=None):
    if po is None:
        msg = {
            "explicit-not-found": f"PO '{ex.get('po_reference')}' does not exist",
            "ambiguous": "No PO reference, and several open POs could fit",
            "none": "No PO reference, and no open PO fits"
        }[method]

        check = ck("R3", "fail", msg, fix="Provide a valid, open PO reference")
        if method == "ambiguous":
            check["code"] = "po_ambiguous"
        return [check]

    if vid and po["vendor"] != vid:
        return [
            ck("R3", "fail", f"{po['id']} belongs to a different vendor",
               hard=True, fix="Confirm the correct PO")
        ]

    if po["remaining"] <= 0:
        return [
            ck("R3", "fail", f"{po['id']} is fully invoiced",
               fix="Raise a new PO or amend the existing one")
        ]

    out = [
        ck("R3", "pass",
           f"{po['id']} matched ({method}); ${po['remaining']:,.2f} "
           f"of ${po['amount']:,.2f} remaining")
    ]

    tot = ex.get("total")
    if tot is not None and vid:
        tol = VENDORS[vid]["tol"] if tolerance is None else tolerance
        rem = po["remaining"]
        limit = round(rem * (1 + tol), 2)
        over = (tot - rem) / rem

        if tot <= rem:
            out.append(
                ck("R4", "pass",
                   f"${tot:,.2f} is within the PO's remaining ${rem:,.2f}")
            )
        elif tot <= limit:
            out.append(
                ck("R4", "warn",
                   f"${tot:,.2f} exceeds remaining ${rem:,.2f} by {over:.1%}, "
                   f"inside the {tol:.0%} tolerance",
                   fix=f"Would pass cleanly if total were at most ${rem:,.2f}")
            )
        else:
            out.append(
                ck("R4", "fail",
                   f"${tot:,.2f} exceeds remaining ${rem:,.2f} by {over:.1%}; "
                   f"tolerance is {tol:.0%} (limit ${limit:,.2f})",
                   fix=f"Would pass if total were at most ${limit:,.2f} "
                       f"(remaining + {tol:.0%} tolerance)")
            )

    return out

def dup_check(ex, vid, fh, c):
    rows = c.execute(
        "select * from runs where vendor_id=? and status not in ('rejected','superseded') "
        "and created_at>?",
        (vid, time.time() - DUP_WINDOW_DAYS * 86400)
    ).fetchall() if vid else []

    n, tot = norm(ex.get("invoice_number")), ex.get("total")

    # A matching hash is conclusive. A matching invoice number also needs the
    # same amount; conflicting or unavailable amounts require investigation.
    for r in rows:
        if r["file_hash"] == fh:
            return {
                **ck("R5", "fail",
                     f"Exact duplicate of earlier invoice {r['invoice_number']} "
                     f"(run {r['id']})",
                     hard=True,
                     fix="No action needed; this invoice has already been processed"),
                "code": "duplicate_exact"
            }
        same_number = n and norm(r["invoice_number"]) == n
        if same_number:
            if ex.get("total") is not None and r["total"] is not None:
                same_amount = abs(ex["total"] - r["total"]) <= 0.01
            else:
                same_amount = False
            if same_amount:
                return {
                    **ck("R5", "fail",
                         f"Confirmed duplicate: invoice {r['invoice_number']} from "
                         f"this vendor has already been processed for the same amount",
                         hard=True,
                         fix="No action needed; this invoice has already been processed"),
                    "code": "duplicate_exact"
                }
            return {
                **ck("R5", "fail",
                     f"Invoice number matches {r['invoice_number']} (run {r['id']}), "
                     "but the amount differs or is unavailable",
                     hard=True,
                     fix="Verify with the vendor whether this is a corrected invoice"),
                "code": "duplicate_suspected"
            }

    # Near-duplicate checks.
    for r in rows:
        try:
            days = abs((
                dt.date.fromisoformat(ex["invoice_date"]) -
                dt.date.fromisoformat(r["invoice_date"])
            ).days)
        except Exception:
            days = NEAR_DUP_DAYS + 1

        distance = lev(n, norm(r["invoice_number"])) if n else NEAR_DUP_DAYS + 1
        same_period_and_number = days <= NEAR_DUP_DAYS and distance <= 2

        same_amount = (
            tot is not None and r["total"] is not None and r["total"] != 0
            and abs(tot - r["total"]) / abs(r["total"]) <= 0.005
        )

        # Very similar invoice number + date + same amount:
        # keep as a hard stop because it is a strong duplicate signal.
        if same_period_and_number and same_amount:
            return {
                **ck("R5", "fail",
                     f"Near-duplicate of {r['invoice_number']} (run {r['id']}): "
                     f"same vendor, same amount, invoice number differs by "
                     f"{distance} character(s)",
                     hard=True,
                     fix="Verify with the vendor whether this is a separate invoice"),
                "code": "duplicate_near"
            }

        # A likely corrected resubmission is not conclusive enough to reject,
        # but is suspicious enough to require investigation.
        same_po = (
            norm(ex.get("po_reference"))
            and norm(ex.get("po_reference")) == norm(r["po_id"])
        )
        changed_amount_close = (
            tot is not None and r["total"] is not None and r["total"] != 0
            and abs(tot - r["total"]) / abs(r["total"]) <= 0.20
        )

        if same_period_and_number and changed_amount_close and same_po:
            return {
                **ck("R5", "fail",
                     f"Possible corrected or resubmitted invoice: same vendor, PO "
                     f"and similar invoice number as {r['invoice_number']} "
                     f"(run {r['id']}), but the total changed",
                     hard=True,
                     fix="Verify with the vendor whether this is a corrected invoice "
                         "or a separate charge"),
                "code": "duplicate_suspected"
            }

    return ck("R5", "pass", f"No duplicate in the last {DUP_WINDOW_DAYS} days")

def bank_check(ex, vid):
    if not vid:
        return ck("R6", "pass", "Skipped (unknown vendor)")

    got, want = ex.get("bank_last4"), VENDORS[vid]["bank"]

    if not got:
        return ck("R6", "warn", "No bank details on invoice")

    if got != want:
        return ck("R6", "fail",
                  f"Bank account ends {got}, vendor master has {want}. "
                  "Possible payment fraud.",
                  hard=True,
                  fix="Approve only after verifying the new bank details by callback "
                      "to the number on file")

    return ck("R6", "pass", "Bank details match the vendor master")


def sender_domain_check(sender, vid):
    if sender is None:
        return ck(
            "R9", "not_applicable",
            "Not applicable: uploaded invoice has no email sender",
        )
    if not vid:
        return ck(
            "R9", "fail",
            "Sender domain cannot be verified because the vendor is not approved",
            hard=True,
            fix="Verify the sender and vendor identity through a trusted channel",
        )

    address = parseaddr(str(sender))[1]
    if not address or address.count("@") != 1:
        return ck(
            "R9", "fail", "Email sender address is missing or invalid",
            hard=True,
            fix="Resend from a verified vendor email address",
        )
    domain = address.rsplit("@", 1)[1].strip().lower().rstrip(".")
    known_domains = {
        item.strip().lower().rstrip(".")
        for item in VENDORS[vid].get("domains", [])
        if isinstance(item, str) and item.strip()
    }
    if domain in known_domains:
        return ck("R9", "pass", f"Sender domain {domain} matches the vendor master")
    if not known_domains:
        return ck(
            "R9", "fail",
            f"No verified sender domains are configured for {VENDORS[vid]['name']}",
            hard=True,
            fix="Verify the vendor's domain independently and add it to the vendor master",
        )

    labels = domain.split(".")
    vendor_first_name = norm(VENDORS[vid]["name"].split()[0]).lower()
    brand_like = bool(labels) and norm(labels[0]).lower().startswith(vendor_first_name)
    lookalike = brand_like
    for known in known_domains:
        known_labels = known.split(".")
        same_base_different_tld = (
            len(labels) > 1 and len(known_labels) > 1
            and ".".join(labels[:-1]) == ".".join(known_labels[:-1])
            and labels[-1] != known_labels[-1]
        )
        close_domain = lev(domain, known) <= 2
        close_brand = bool(labels and known_labels) and lev(labels[0], known_labels[0]) <= 2
        lookalike = lookalike or same_base_different_tld or close_domain or close_brand

    detail = (
        f"Sender domain {domain} is a lookalike vendor-domain impersonation"
        if lookalike else
        f"Sender domain {domain} does not match the vendor's approved domain(s): "
        + ", ".join(sorted(known_domains))
    )
    return ck(
        "R9", "fail", detail, hard=True,
        fix=f"Verify the invoice with {VENDORS[vid]['name']} using a known contact, "
            "then add the independently verified sender domain to the vendor master",
    )


def decide(ex, vid, vscore, po, method, pconf, checks, pts):
    miss = missing(ex)
    hard = [x for x in checks if x["hard"] and x["status"] == "fail"]
    fails = [x for x in checks if x["status"] == "fail" and not x["hard"]]
    warns = [x for x in checks if x["status"] == "warn"]

    confs = field_confidences(ex)
    conf = round(min(confs + ([pconf] if po else [])), 2) if confs else 0.0
    tot = ex.get("total") or 0
    email, extra = None, []

    confirmed_duplicates = [
        x for x in hard if x.get("code") == "duplicate_exact"
    ]
    other_hard_stops = [
        x for x in hard if x.get("code") != "duplicate_exact"
    ]
    ambiguous = any(x.get("code") == "po_ambiguous" for x in checks)

    if other_hard_stops or ambiguous:
        action, rec = "ESCALATE", "investigate"

    elif confirmed_duplicates:
        action, rec = "AUTO_REJECT", "reject"

    elif miss:
        action, rec = "HOLD_FOR_INFO", "request_info"
        name = ex.get("vendor_name") or "there"
        labels = {
            "vendor_name": "vendor name",
            "invoice_number": "invoice number",
            "invoice_date": "invoice date",
            "total": "invoice total"
        }
        missing_labels = [
            labels.get(field, field.replace("_", " ")) for field in miss
        ]
        email = dict(
            subject="Invoice received - information needed",
            body=f"Hi {name},\n\nWe received your invoice but cannot process it yet "
                 f"because it is missing: {', '.join(missing_labels)}.\n"
                 "Please send a corrected copy and we will process it right away.\n\n"
                 "Thanks,\nAccounts Payable"
        )

    elif fails:
        action, rec = "REVIEW", "reject"

    else:
        rec = "approve"
        if not warns and pts >= TRUST_THRESHOLD and tot <= AUTO_CAP and conf >= AUTO_APPROVE_CONFIDENCE:
            action = "AUTO_APPROVE"
        else:
            action = "REVIEW"
            if not warns and pts < TRUST_THRESHOLD:
                extra.append(
                    f"Would auto-approve after {TRUST_THRESHOLD - pts} more "
                    f"confirmed clean invoice(s) from this vendor "
                    f"(trust {pts}/{TRUST_THRESHOLD})"
                )
            if tot > AUTO_CAP:
                extra.append(f"Auto-approval is capped at ${AUTO_CAP:,}")
            if conf < AUTO_APPROVE_CONFIDENCE:
                extra.append(
                    f"Auto-approval requires extraction and match confidence of at least "
                    f"{AUTO_APPROVE_CONFIDENCE:.0%}; current confidence is {conf:.0%}"
                )

    bad = sorted(
        [x for x in checks if x["status"] not in ("pass", "not_applicable")],
        key=lambda x: (x["status"] != "fail", not x["hard"])
    )
    reasons = [x["detail"] for x in bad] or ["All checks passed"]

    if action == "AUTO_APPROVE":
        reasons.append(
            f"Vendor trust {pts}/{TRUST_THRESHOLD}: clean invoices from this "
            "vendor are auto-approved"
        )
    elif action == "AUTO_REJECT":
        reasons.append(
            "Automatically rejected because the vendor and invoice identity "
            "match an already-processed invoice"
        )

    if action == "REVIEW" and rec == "approve" and not warns:
        reasons.append(
            f"Vendor is on probation (trust {pts}/{TRUST_THRESHOLD}), "
            "so a human confirms"
        )

    explanations = {
        "AUTO_APPROVE": "All required checks passed, confidence and vendor trust meet the automatic-approval thresholds.",
        "AUTO_REJECT": "A confirmed duplicate was found: the same invoice file or invoice number and amount was already processed.",
        "ESCALATE": "A fraud indicator, conclusive hard stop, or ambiguous match requires investigation before a decision.",
        "HOLD_FOR_INFO": "Required invoice information is missing; request the listed fields before processing.",
        "REVIEW": "The invoice is not conclusive enough for automatic action and needs human review.",
    }

    return dict(
        action=action,
        recommendation=rec,
        decision_explanation=explanations[action],
        confidence=conf,
        vendor=dict(
            id=vid,
            name=VENDORS[vid]["name"] if vid else ex.get("vendor_name"),
            match=round(vscore, 2),
            trust_points=pts,
            tier=1 if pts >= TRUST_THRESHOLD else 0
        ),
        po=dict(po or {}, method=method, match_conf=pconf) if po
           else dict(method=method),
        invoice=ex,
        checks=checks,
        reasons=reasons,
        hard_stops=[x["detail"] for x in hard],
        what_would_change_it=[
            x["fix"] for x in checks
            if x["status"] not in ("pass", "not_applicable") and x["fix"]
        ] + extra,
        vendor_email=email
    )