import asyncio
import base64
import calendar
import json
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch


_test_directory = tempfile.TemporaryDirectory(prefix="forward-to-decide-tests-")
os.environ["DB_PATH"] = str(Path(_test_directory.name) / "analytics.sqlite")

import db
import main
from fastapi.testclient import TestClient
from samples import inv


def epoch(day):
    return calendar.timegm(time.strptime(f"2026-11-{day:02d}", "%Y-%m-%d"))


class AnalyticsApiTests(unittest.TestCase):
    def setUp(self):
        db.init()
        main.UPLOAD_RATE_LIMITS.clear()
        self.connection = db.conn()
        self.connection.execute("delete from email_notifications")
        self.connection.execute("delete from vendor_emails")
        self.connection.execute("delete from runs")
        self.connection.execute("delete from trust")
        self.connection.execute("delete from tol_override")
        self.connection.execute("update vendors set email=''")
        self.connection.commit()
        self.client = TestClient(main.app)

    def tearDown(self):
        self.client.close()
        self.connection.close()

    def test_healthz_confirms_database_readiness(self):
        response = self.client.get("/healthz")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})

    def add_run(self, run_id, day, vendor_id, action, status, total, email_status=None):
        self.connection.execute(
            "insert into runs"
            "(id,created_at,filename,source,file_hash,vendor_id,po_id,invoice_number,"
            "invoice_date,total,action,recommendation,status,decision) "
            "values(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, epoch(day), f"{run_id}.pdf", "upload", f"hash-{run_id}",
             vendor_id, "PO-1001", f"INV-{run_id}", f"2026-11-{day:02d}", total,
             action, "approve", status, json.dumps({"checks": []})),
        )
        if email_status:
            self.connection.execute(
                "insert into email_notifications"
                "(run_id,vendor_id,dedupe_key,notification_key,attempt_number,recipient,subject,body,status,created_at,attempted_at,completed_at,error) "
                "values(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (run_id, vendor_id, f"key-{run_id}:1", f"key-{run_id}", 1,
                 "ap@vendor.example", "Invoice information needed", "Please correct the invoice.",
                 email_status, epoch(day), epoch(day), epoch(day),
                 "SMTP unavailable" if email_status == "FAILED" else None),
            )
        self.connection.commit()

    def populate(self):
        self.add_run("r1", 1, "V001", "AUTO_APPROVE", "auto_approved", 100)
        self.add_run("r2", 2, "V001", "AUTO_REJECT", "auto_rejected", 50)
        self.add_run("r3", 3, "V002", "REVIEW", "pending", 200)
        self.add_run("r4", 4, "V002", "HOLD_FOR_INFO", "pending", 25, "FAILED")
        self.add_run("r5", 5, "V003", "ESCALATE", "pending", 300, "SENT")
        self.add_run("r6", 6, "V003", "REVIEW", "approved", 75)
        self.connection.execute("insert into trust(vendor_id,points) values('V001',5)")
        self.connection.commit()

    def test_kpis_and_decision_distribution_are_from_real_invoice_rows(self):
        self.populate()

        result = self.client.get("/api/analytics").json()

        self.assertEqual(result["kpis"], {
            "total_processed": 6,
            "auto_approved": 1,
            "auto_rejected": 1,
            "pending_review": 1,
            "on_hold": 1,
            "escalated": 1,
            "manual_approved": 1,
            "manual_rejected": 0,
            "total_value": 750.0,
            "automation_rate": 33.3,
        })
        self.assertEqual(
            {item["action"]: item["count"] for item in result["decision_distribution"]},
            {"AUTO_APPROVE": 1, "AUTO_REJECT": 1, "REVIEW": 2,
             "HOLD_FOR_INFO": 1, "ESCALATE": 1},
        )

    def test_vendor_statistics_and_automation_eligibility(self):
        self.populate()

        result = self.client.get("/api/analytics").json()
        by_vendor = {item["vendor_id"]: item for item in result["vendor_performance"]}

        self.assertEqual(by_vendor["V001"]["total"], 2)
        self.assertEqual(by_vendor["V001"]["approved"], 1)
        self.assertEqual(by_vendor["V001"]["rejected"], 1)
        self.assertEqual(by_vendor["V001"]["total_value"], 150.0)
        self.assertTrue(by_vendor["V001"]["automation_eligible"])
        self.assertEqual(by_vendor["V002"]["escalated"], 0)
        self.assertEqual(by_vendor["V002"]["total"], 2)

    def test_date_vendor_and_email_filters(self):
        self.populate()

        date_result = self.client.get(
            "/api/analytics?date_from=2026-11-02&date_to=2026-11-04"
        ).json()
        vendor_result = self.client.get("/api/analytics?vendor_id=V002").json()
        email_result = self.client.get("/api/analytics?email_status=FAILED").json()

        self.assertEqual(date_result["kpis"]["total_processed"], 3)
        self.assertEqual(vendor_result["kpis"]["total_processed"], 2)
        self.assertEqual(vendor_result["vendor_performance"][0]["vendor_id"], "V002")
        self.assertEqual(email_result["kpis"]["total_processed"], 1)
        self.assertEqual(email_result["kpis"]["on_hold"], 1)

    def test_email_delivery_distribution_and_history(self):
        self.populate()

        result = self.client.get("/api/analytics").json()
        counts = {item["status"]: item["count"] for item in result["email_delivery"]}
        history = self.client.get("/api/email-history?email_status=FAILED").json()

        self.assertEqual(counts["FAILED"], 1)
        self.assertEqual(counts["SENT"], 1)
        self.assertEqual(counts["NOT_REQUIRED"], 4)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["run_id"], "r4")
        self.assertEqual(history[0]["error"], "SMTP unavailable")

    def test_empty_database_returns_zero_metrics_and_empty_series(self):
        result = self.client.get("/api/analytics").json()

        self.assertEqual(result["kpis"]["total_processed"], 0)
        self.assertEqual(result["kpis"]["automation_rate"], 0)
        self.assertEqual(result["kpis"]["total_value"], 0)
        self.assertEqual(result["trends"], [])
        self.assertTrue(all(item["count"] == 0 for item in result["decision_distribution"]))
        self.assertTrue(all(item["count"] == 0 for item in result["email_delivery"]))

    def test_registered_vendor_email_can_be_updated(self):
        response = self.client.put(
            "/api/vendors/V004/email",
            json={"email": "ap@meridian.example"},
        )

        self.assertEqual(response.status_code, 200)
        vendor = next(item for item in self.client.get("/api/vendors").json()
                      if item["id"] == "V004")
        self.assertEqual(vendor["email"], "ap@meridian.example")
        invalid = self.client.put("/api/vendors/V004/email", json={"email": "not-email"})
        self.assertEqual(invalid.status_code, 400)

    def test_failed_email_resend_creates_a_history_attempt(self):
        self.add_run("retry", 8, "V004", "HOLD_FOR_INFO", "pending", 99, "FAILED")
        original = self.connection.execute(
            "select id from email_notifications where run_id='retry'"
        ).fetchone()["id"]
        with patch("main.email_service.send_email", return_value={
            "recipient": "ap@vendor.example", "status": "SENT", "error": None
        }):
            response = self.client.post(f"/api/runs/retry/emails/{original}/resend")

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "SENT")
        detail = self.client.get("/api/runs/retry").json()
        self.assertEqual(len(detail["email_history"]), 2)
        run = self.client.get("/api/runs").json()[0]
        self.assertEqual(run["email_status"], "SENT")
        notifications = self.connection.execute(
            "select status,attempt_number from email_notifications where run_id='retry' order by id"
        ).fetchall()
        self.assertEqual([(row["status"], row["attempt_number"]) for row in notifications],
                         [("FAILED", 1), ("SENT", 2)])
        again = self.client.post(f"/api/runs/retry/emails/{original}/resend")
        self.assertEqual(again.status_code, 409)

    def test_reprocessing_same_hold_invoice_keeps_one_demo_outbox_draft(self):
        self.connection.execute(
            "update vendors set email='ap@meridian.example' where vendor_id='V004'"
        )
        self.connection.commit()
        with patch.dict(os.environ, {"EMAIL_MODE": "demo"}), patch(
            "main.asyncio.sleep", new=AsyncMock()
        ), patch(
            "main.email_service.send_email",
            return_value={"recipient": "ap@meridian.example", "status": "SENT", "error": None},
        ) as send:
            async def process_twice():
                output = []
                for _ in range(2):
                    lines = [json.loads(line) async for line in main.pipeline(
                        "missing.json", None, None, "sample", main.SAMPLES["missing"]["data"]
                    )]
                    output.append(next(event["decision"] for event in lines if event["type"] == "decision"))
                return output
            decisions = asyncio.run(process_twice())

        self.assertEqual(decisions[0]["action"], "HOLD_FOR_INFO")
        self.assertEqual(decisions[1]["action"], "AUTO_REJECT")
        send.assert_not_called()
        count = self.connection.execute("select count(*) from email_notifications").fetchone()[0]
        self.assertEqual(count, 1)
        notification = self.connection.execute(
            "select status,error from email_notifications"
        ).fetchone()
        self.assertEqual(notification["status"], "OUTBOX")
        self.assertEqual(notification["error"], "Drafted, demo mode, not sent")

    def test_missing_vendor_contact_is_a_demo_draft_not_a_failure(self):
        with patch.dict(os.environ, {"EMAIL_MODE": "demo"}), patch(
            "main.asyncio.sleep", new=AsyncMock()
        ), patch("main.email_service.send_email") as send:
            async def process_once():
                return [json.loads(line) async for line in main.pipeline(
                    "missing-contact.json", None, None, "sample",
                    main.SAMPLES["missing"]["data"],
                )]
            events = asyncio.run(process_once())
        decision_event = next(event for event in events if event["type"] == "decision")
        original_email = decision_event["decision"]["vendor_email"]
        self.assertEqual(original_email["status"], "OUTBOX")
        self.assertEqual(original_email["error"], "Drafted, demo mode, not sent")
        send.assert_not_called()

        history = self.client.get(
            f"/api/runs/{decision_event['run_id']}"
        ).json()["email_history"]
        self.assertEqual([item["status"] for item in history], ["OUTBOX"])
        self.assertIn("Invoice total", history[0]["body"])
        self.assertEqual(
            self.client.get("/api/email-history?email_status=OUTBOX").json()[0]["run_id"],
            decision_event["run_id"],
        )
        counts = {
            item["status"]: item["count"]
            for item in self.client.get("/api/analytics").json()["email_delivery"]
        }
        self.assertEqual(counts["OUTBOX"], 1)

    def test_smtp_mode_only_sends_to_configured_allowlisted_recipients(self):
        self.connection.execute(
            "update vendors set email='ap@meridian.example' where vendor_id='V004'"
        )
        self.connection.commit()
        environment = {
            "EMAIL_MODE": "smtp",
            "EMAIL_ALLOWLIST": "",
            "SMTP_HOST": "smtp.example.test",
            "SMTP_FROM_EMAIL": "accounts@example.test",
        }
        with patch.dict(os.environ, environment), patch(
            "main.asyncio.sleep", new=AsyncMock()
        ), patch("main.email_service.send_email") as send:
            async def process_once():
                return [json.loads(line) async for line in main.pipeline(
                    "missing-allowlist.json", None, None, "sample",
                    main.SAMPLES["missing"]["data"],
                )]
            events = asyncio.run(process_once())
        decision = next(event["decision"] for event in events if event["type"] == "decision")
        send.assert_not_called()
        self.assertEqual(decision["vendor_email"]["status"], "OUTBOX")
        self.assertIn("EMAIL_ALLOWLIST", decision["vendor_email"]["error"])

    def test_smtp_mode_sends_when_configured_recipient_is_allowlisted(self):
        self.connection.execute(
            "update vendors set email='ap@meridian.example' where vendor_id='V004'"
        )
        self.connection.commit()
        environment = {
            "EMAIL_MODE": "smtp",
            "EMAIL_ALLOWLIST": "ap@meridian.example",
            "SMTP_HOST": "smtp.example.test",
            "SMTP_PORT": "587",
            "SMTP_USER": "",
            "SMTP_PASSWORD": "",
            "SMTP_FROM_EMAIL": "accounts@example.test",
            "SMTP_USERNAME": "",
            "SMTP_FROM": "",
        }
        with patch.dict(os.environ, environment), patch(
            "main.asyncio.sleep", new=AsyncMock()
        ), patch("main.email_service.send_email", return_value={
            "recipient": "ap@meridian.example", "status": "SENT", "error": None
        }) as send:
            async def process_once():
                return [json.loads(line) async for line in main.pipeline(
                    "missing-allowlisted.json", None, None, "sample",
                    main.SAMPLES["missing"]["data"],
                )]
            events = asyncio.run(process_once())
        decision = next(event["decision"] for event in events if event["type"] == "decision")
        self.assertEqual(decision["vendor_email"]["status"], "SENT")
        send.assert_called_once()

    def test_admin_token_protects_mutating_admin_routes_when_configured(self):
        with patch.dict(os.environ, {"ADMIN_TOKEN": "fixture-admin-token"}):
            self.assertEqual(self.client.post("/api/reset").status_code, 403)
            self.assertEqual(
                self.client.put("/api/vendors/V001/email", json={"email": ""}).status_code,
                403,
            )
            accepted = self.client.post(
                "/api/proposals/accept?admin_token=fixture-admin-token",
                json={"vendor_id": "V001", "tol": 0.02},
            )
            reset = self.client.post(
                "/api/reset", headers={"X-Admin-Token": "fixture-admin-token"}
            )
        self.assertEqual(accepted.status_code, 200)
        self.assertEqual(reset.status_code, 200)

    def test_tolerance_proposal_includes_replayed_backtest_counts(self):
        main.VENDORS["V001"]["tol"] = main.DEFAULT_TOL["V001"]
        for index in range(3):
            run_id = f"proposal-{index}"
            invoice_number = f"BT-{(index + 1) * 11111}"
            invoice = inv(
                "Northwind Office Supplies", invoice_number, f"2026-11-0{index + 1}",
                "PO-1001", [("Paper", 1, 4944)], 0, "7710",
            )
            decision = {
                "action": "REVIEW",
                "invoice": invoice,
                "vendor": {"id": "V001", "match": 0.98, "trust_points": 0},
                "po": {
                    "id": "PO-1001", "vendor": "V001", "amount": 4800,
                    "remaining": 4800, "method": "explicit", "match_conf": 0.95,
                },
                "checks": [],
            }
            self.connection.execute(
                "insert into runs"
                "(id,created_at,filename,source,file_hash,vendor_id,po_id,invoice_number,"
                "invoice_date,total,action,recommendation,status,decision) "
                "values(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (run_id, epoch(index + 1), f"{run_id}.pdf", "upload",
                 f"backtest-{run_id}", "V001", "PO-1001", invoice_number,
                 invoice["invoice_date"], invoice["total"], "REVIEW", "approve",
                 "approved", json.dumps(decision)),
            )
        self.connection.commit()

        proposals = self.client.get("/api/proposals").json()

        self.assertEqual(len(proposals), 1)
        proposal = proposals[0]
        self.assertEqual(proposal["proposed"], 3.0)
        self.assertEqual(proposal["backtest"], {
            "decisions_would_change": 0,
            "extra_auto_approval_eligible": 0,
            "later_human_rejections": 0,
        })

    def test_upload_rejects_unsupported_and_oversized_files(self):
        unsupported = self.client.post(
            "/api/run", files={"file": ("invoice.gif", b"gif", "image/gif")}
        )
        oversized = self.client.post(
            "/api/run",
            files={"file": ("invoice.pdf", b"x" * (5 * 1024 * 1024 + 1), "application/pdf")},
        )
        self.assertEqual(unsupported.status_code, 400)
        self.assertEqual(oversized.status_code, 413)
        self.assertIn("5 MB", oversized.json()["detail"])

    def test_uploads_are_rate_limited_per_client_ip(self):
        async def fake_pipeline(*_args, **_kwargs):
            yield '{"type":"decision"}\n'

        with patch("main.pipeline", side_effect=fake_pipeline):
            responses = [
                self.client.post(
                    "/api/run",
                    files={"file": (f"invoice-{index}.pdf", b"pdf", "application/pdf")},
                )
                for index in range(main.UPLOADS_PER_HOUR)
            ]
            limited = self.client.post(
                "/api/run", files={"file": ("invoice-extra.pdf", b"pdf", "application/pdf")}
            )
        self.assertTrue(all(response.status_code == 200 for response in responses))
        self.assertEqual(limited.status_code, 429)
        self.assertIn("Upload limit", limited.json()["detail"])

    def test_inbound_sender_is_passed_through_pipeline_and_stored(self):
        sender = "Northwind AP <billing@northwind.example.com>"
        with patch.dict(os.environ, {"INBOUND_TOKEN": "fixture-inbound-token"}), patch(
            "main.asyncio.sleep", new=AsyncMock()
        ):
            async def process_once():
                return [json.loads(line) async for line in main.pipeline(
                    "incoming.pdf", None, None, "email",
                    main.SAMPLES["clean"]["data"], sender=sender,
                )]
            events = asyncio.run(process_once())

        decision = next(event["decision"] for event in events if event["type"] == "decision")
        self.assertEqual(decision["action"], "REVIEW")
        sender_check = next(item for item in decision["checks"] if item["rule"] == "R9")
        self.assertEqual(sender_check["status"], "pass")
        run_id = next(event["run_id"] for event in events if event["type"] == "decision")
        detail = self.client.get(f"/api/runs/{run_id}").json()
        self.assertEqual(detail["sender"], sender)

    def test_resubmission_links_corrected_run_and_excludes_superseded_duplicate(self):
        original_invoice = inv(
            "Meridian Print & Packaging", "MP-RESUB-1", "2026-11-02",
            "PO-1004", [("Boxes", 10, 10)], 0, "1204", total=None,
        )
        original_decision = {
            "action": "HOLD_FOR_INFO",
            "invoice": original_invoice,
            "vendor": {"id": "V004", "match": 0.98, "trust_points": 0},
            "po": {"id": "PO-1004", "remaining": 18500, "method": "explicit"},
        }
        self.connection.execute(
            "insert into runs"
            "(id,created_at,filename,source,file_hash,vendor_id,po_id,invoice_number,"
            "invoice_date,total,action,recommendation,status,decision) "
            "values(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("held-run", epoch(2), "held.pdf", "upload", "held-file-hash",
             "V004", "PO-1004", "MP-RESUB-1", "2026-11-02", None,
             "HOLD_FOR_INFO", "request_info", "pending",
             json.dumps(original_decision)),
        )
        self.connection.commit()
        corrected_invoice = inv(
            "Meridian Print & Packaging", "MP-RESUB-1", "2026-11-02",
            "PO-1004", [("Boxes", 10, 10)], 0, "1204",
        )

        with patch("main.asyncio.sleep", new=AsyncMock()), patch(
            "main.extract.extract", new=AsyncMock(return_value=(corrected_invoice, "test"))
        ):
            response = self.client.post(
                "/api/runs/held-run/resubmit",
                files={"file": ("corrected.pdf", b"pdf fixture", "application/pdf")},
            )

        self.assertEqual(response.status_code, 200, response.text)
        events = [json.loads(line) for line in response.text.splitlines() if line.strip()]
        decision_event = next(event for event in events if event["type"] == "decision")
        r5 = next(
            check for check in decision_event["decision"]["checks"]
            if check["rule"] == "R5"
        )
        self.assertEqual(r5["status"], "pass")
        original = self.client.get("/api/runs/held-run").json()
        corrected = self.client.get(
            f"/api/runs/{decision_event['run_id']}"
        ).json()
        self.assertEqual(original["status"], "superseded")
        self.assertEqual(original["superseded_by"], decision_event["run_id"])
        self.assertEqual(corrected["original_run_id"], "held-run")

    def test_inbound_webhook_passes_the_sender_header_into_live_pipeline(self):
        sender = "Northwind AP <billing@northwind.example.com>"
        pipeline_calls = []
        tasks = []

        class InboundRequest:
            async def json(self):
                return {
                    "From": sender,
                    "Attachments": [{
                        "Name": "incoming.pdf",
                        "ContentType": "application/pdf",
                        "Content": base64.b64encode(b"fixture").decode("ascii"),
                    }],
                }

        async def fake_pipeline(*args, sender=None, **_kwargs):
            pipeline_calls.append((args, sender))
            yield json.dumps({"type": "stage"})

        async def submit():
            with patch.dict(os.environ, {"INBOUND_TOKEN": "fixture-inbound-token"}), patch(
                "main.pipeline", new=fake_pipeline
            ), patch(
                "main.asyncio.create_task",
                side_effect=lambda coroutine: tasks.append(asyncio.ensure_future(coroutine)),
            ):
                response = await main.inbound("fixture-inbound-token", InboundRequest())
                await asyncio.gather(*tasks)
                return response

        response = asyncio.run(submit())

        self.assertEqual(response, {"queued": 1})
        self.assertEqual(pipeline_calls[0][0][3], "email")
        self.assertEqual(pipeline_calls[0][1], sender)

    def test_non_email_run_has_neutral_sender_domain_check(self):
        with patch("main.asyncio.sleep", new=AsyncMock()):
            async def process_once():
                return [json.loads(line) async for line in main.pipeline(
                    "upload.pdf", None, None, "upload", main.SAMPLES["clean"]["data"],
                )]
            events = asyncio.run(process_once())
        decision = next(event["decision"] for event in events if event["type"] == "decision")
        sender_check = next(item for item in decision["checks"] if item["rule"] == "R9")
        self.assertEqual(sender_check["status"], "not_applicable")
        self.assertIn("Not applicable", sender_check["detail"])

    def test_legacy_email_and_trust_data_survive_schema_migration(self):
        self.add_run("legacy", 9, "V001", "HOLD_FOR_INFO", "pending", 42)
        self.connection.execute(
            "insert into trust(vendor_id,points,confirms,overrides) values('V001',4,3,1)"
        )
        self.connection.execute(
            "insert into vendor_emails values(?,?,?,?,?,?,?,?,?)",
            ("legacy", "V001", "ap@vendor.example", "Old subject", "Old body",
             "sent", None, epoch(9), epoch(9)),
        )
        self.connection.commit()

        db.init()

        migrated = self.connection.execute(
            "select status,subject from email_notifications where run_id='legacy'"
        ).fetchone()
        trust = self.connection.execute(
            "select points,confirms,overrides from trust where vendor_id='V001'"
        ).fetchone()
        self.assertEqual((migrated["status"], migrated["subject"]), ("SENT", "Old subject"))
        self.assertEqual(tuple(trust), (4, 3, 1))

    def test_existing_notification_table_is_upgraded_before_indexes_are_created(self):
        legacy_path = str(Path(_test_directory.name) / "legacy-email.sqlite")
        legacy = sqlite3.connect(legacy_path)
        legacy.execute("""
            create table email_notifications(
                id integer primary key autoincrement, run_id text not null, vendor_id text,
                dedupe_key text not null unique, recipient text, subject text not null,
                body text not null,
                status text not null check(status in ('SENT','FAILED','PENDING','NOT_REQUIRED')),
                error text, created_at real not null,
                attempted_at real, completed_at real)
        """)
        legacy.execute(
            "insert into email_notifications"
            "(run_id,vendor_id,dedupe_key,recipient,subject,body,status,error,created_at) "
            "values(?,?,?,?,?,?,?,?,?)",
            ("old-run", "V001", "old-key", "ap@vendor.example", "Old subject",
             "Old body", "FAILED", "Previous SMTP error", 123.0),
        )
        legacy.commit()
        legacy.close()

        original_db = db.DB
        db.DB = legacy_path
        try:
            db.init()
            migrated = db.conn()
            try:
                columns = {
                    row["name"] for row in
                    migrated.execute("pragma table_info(email_notifications)")
                }
                record = migrated.execute(
                    "select notification_key,attempt_number,resend_claimed_at,status,error "
                    "from email_notifications where run_id='old-run'"
                ).fetchone()
                indexes = {
                    row["name"] for row in
                    migrated.execute("pragma index_list(email_notifications)")
                }
                migrated.execute(
                    "insert into email_notifications"
                    "(run_id,dedupe_key,notification_key,subject,body,status,created_at) "
                    "values('outbox-run','outbox-key','outbox-key','Draft','Body','OUTBOX',123)"
                )
                migrated.commit()
            finally:
                migrated.close()
        finally:
            db.DB = original_db

        self.assertTrue({
            "notification_key", "attempt_number", "resend_claimed_at"
        }.issubset(columns))
        self.assertEqual(tuple(record), (
            "old-key", 1, None, "FAILED", "Previous SMTP error"
        ))
        self.assertIn("email_notifications_key_idx", indexes)


def tearDownModule():
    try:
        main._c.close()
    except sqlite3.Error:
        pass
    _test_directory.cleanup()


if __name__ == "__main__":
    unittest.main()
