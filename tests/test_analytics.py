import asyncio
import base64
import calendar
import hashlib
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
os.environ["PYTHON_DOTENV_DISABLED"] = "1"

import db
import main
from fastapi.testclient import TestClient
from samples import inv


def epoch(day):
    return calendar.timegm(time.strptime(f"2026-11-{day:02d}", "%Y-%m-%d"))


class AnalyticsApiTests(unittest.TestCase):
    def setUp(self):
        self.admin_patcher = patch.dict(
            os.environ, {"ADMIN_TOKEN": "fixture-admin-token"}, clear=False,
        )
        self.admin_patcher.start()
        db.init()
        main.UPLOAD_RATE_LIMITS.clear()
        self.connection = db.conn()
        self.connection.execute("delete from email_notifications")
        self.connection.execute("delete from vendor_email_addresses")
        self.connection.execute("delete from vendor_emails")
        self.connection.execute("delete from runs")
        self.connection.execute("delete from trust")
        self.connection.execute("delete from tol_override")
        self.connection.execute("update vendors set email=''")
        self.connection.commit()
        self.client = TestClient(
            main.app, headers={"X-Admin-Token": "fixture-admin-token"},
        )
        self.local_client = TestClient(
            main.app, client=("127.0.0.1", 50000),
        )

    def tearDown(self):
        self.client.close()
        self.local_client.close()
        self.connection.close()
        self.admin_patcher.stop()

    def test_healthz_confirms_database_readiness(self):
        response = self.client.get("/healthz")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})

    def test_loopback_dashboard_apis_work_without_authentication(self):
        responses = [
            self.local_client.get("/api/runs"),
            self.local_client.get("/api/vendors"),
            self.local_client.get("/api/analytics"),
            self.local_client.get("/api/email-history"),
            self.local_client.get("/api/live?after=0"),
            self.local_client.get("/api/samples"),
            self.local_client.post("/api/inbound", json={"Attachments": []}),
        ]

        self.assertTrue(all(response.status_code == 200 for response in responses))
        self.assertEqual(responses[1].json()[0]["id"], "V001")
        self.assertIn("kpis", responses[2].json())
        self.assertEqual(responses[3].json(), [])
        self.assertEqual(responses[-1].json(), {"queued": 0})

    def test_loopback_invoice_upload_and_review_work_without_authentication(self):
        self.add_run("local-review", 8, "V001", "REVIEW", "pending", 100)

        async def fake_pipeline(*_args, **_kwargs):
            yield json.dumps({"type": "start"}) + "\n"

        with patch("main.pipeline", new=fake_pipeline):
            upload = self.local_client.post(
                "/api/run",
                files={"file": ("local.pdf", b"%PDF-1.4 fixture", "application/pdf")},
            )
        review = self.local_client.post(
            "/api/runs/local-review/review", json={"verdict": "approve"},
        )

        self.assertEqual(upload.status_code, 200, upload.text)
        self.assertIn('"type": "start"', upload.text)
        self.assertEqual(review.status_code, 200, review.text)
        self.assertEqual(review.json()["status"], "approved")

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

    def process_missing_invoice(
        self, invoice_email=None, vendor_name="Meridian Print & Packaging",
        file_identity=None, filename="missing-contact.pdf",
        vendor_name_confidence=0.99, email_confidence=0.99,
    ):
        invoice = inv(
            vendor_name, None, "2026-11-04", "PO-1004",
            [("Retail boxes", 5, 10)], 0, "1204", total=None,
        )
        invoice["vendor_email"] = invoice_email
        invoice["vendor_email_invalid"] = bool(
            invoice_email and not main.email_service.valid_email(invoice_email)
        )
        invoice["confidence"] = {
            "vendor_name": vendor_name_confidence,
            "vendor_email": email_confidence if invoice_email else None,
        }
        if file_identity is not None:
            invoice["_test_file_identity"] = file_identity

        async def process_once():
            return [
                json.loads(line)
                async for line in main.pipeline(
                    filename, None, None, "sample", invoice,
                )
            ]

        with patch("main.asyncio.sleep", new=AsyncMock()):
            return asyncio.run(process_once())

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
        self.connection.execute(
            "update vendors set email='ap@meridian.example' where vendor_id='V004'"
        )
        self.connection.commit()
        self.add_run("retry", 8, "V004", "HOLD_FOR_INFO", "pending", 99, "FAILED")
        original = self.connection.execute(
            "select id from email_notifications where run_id='retry'"
        ).fetchone()["id"]
        with patch.dict(os.environ, {
            "EMAIL_MODE": "smtp",
            "EMAIL_ALLOWLIST": "ap@meridian.example",
            "SMTP_HOST": "smtp.example.test",
            "SMTP_FROM_EMAIL": "accounts@example.test",
        }), patch("main.email_service.send_email", return_value={
            "recipient": "ap@vendor.example", "status": "SENT", "error": None
        }):
            response = self.local_client.post(f"/api/runs/retry/emails/{original}/resend")

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
        again = self.local_client.post(f"/api/runs/retry/emails/{original}/resend")
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

    def test_invoice_contact_is_sent_when_allowlisted_and_master_is_empty(self):
        environment = {
            "EMAIL_MODE": "smtp",
            "EMAIL_ALLOWLIST": "billing@outside.example",
            "EMAIL_MAX_PER_HOUR": "5",
            "SMTP_HOST": "smtp.example.test",
            "SMTP_FROM_EMAIL": "accounts@example.test",
        }
        with patch.dict(os.environ, environment), patch(
            "main.email_service.send_email",
            return_value={"recipient": "billing@outside.example", "status": "SENT", "error": None},
        ) as send:
            events = self.process_missing_invoice("Billing@Outside.Example")

        decision = next(event["decision"] for event in events if event["type"] == "decision")
        self.assertEqual(decision["action"], "HOLD_FOR_INFO")
        self.assertEqual(decision["vendor_email"]["recipient"], "billing@outside.example")
        self.assertEqual(decision["vendor_email"]["recipient_source"], "from invoice, allowlisted")
        send.assert_called_once_with(
            "billing@outside.example",
            decision["vendor_email"]["subject"],
            decision["vendor_email"]["body"],
        )
        history = self.client.get(
            f"/api/runs/{next(event['run_id'] for event in events if event['type'] == 'decision')}"
        ).json()["email_history"][0]
        self.assertEqual(history["recipient_source"], "from invoice, allowlisted")
        self.assertEqual(
            self.client.get("/api/email-history").json()[0]["recipient_source"],
            "from invoice, allowlisted",
        )

    def test_invoice_contact_on_configured_vendor_domain_is_sent_without_allowlist(self):
        environment = {
            "EMAIL_MODE": "smtp",
            "EMAIL_ALLOWLIST": "",
            "EMAIL_MAX_PER_HOUR": "5",
            "SMTP_HOST": "smtp.example.test",
            "SMTP_FROM_EMAIL": "accounts@example.test",
        }
        with patch.dict(os.environ, environment), patch(
            "main.email_service.send_email",
            return_value={
                "recipient": "accounts@meridian.example.com",
                "status": "SENT",
                "error": None,
            },
        ) as send:
            events = self.process_missing_invoice("Accounts@Meridian.Example.Com")

        decision = next(event["decision"] for event in events if event["type"] == "decision")
        self.assertEqual(
            decision["vendor_email"]["recipient_source"],
            "automatically authorized extracted invoice email",
        )
        address = self.connection.execute(
            "select * from vendor_email_addresses where email='accounts@meridian.example.com'"
        ).fetchone()
        self.assertEqual(address["authorization_status"], "AUTHORIZED")
        self.assertIn("exact configured vendor domain", address["authorization_reason"])
        self.assertGreaterEqual(address["extraction_confidence"], 0.90)
        send.assert_called_once()

    def test_authorized_extracted_email_is_reused_when_later_invoice_omits_it(self):
        environment = {
            "EMAIL_MODE": "smtp",
            "EMAIL_ALLOWLIST": "",
            "EMAIL_MAX_PER_HOUR": "5",
            "SMTP_HOST": "smtp.example.test",
            "SMTP_FROM_EMAIL": "accounts@example.test",
        }

        def accepted(recipient, *_):
            return {"recipient": recipient, "status": "SENT", "error": None}

        with patch.dict(os.environ, environment), patch(
            "main.email_service.send_email", side_effect=accepted,
        ) as send:
            self.process_missing_invoice("accounts@meridian.example.com")
            later = self.process_missing_invoice(
                None, file_identity="invoice-without-contact", filename="later.pdf",
            )

        decision = next(event["decision"] for event in later if event["type"] == "decision")
        self.assertEqual(decision["vendor_email"]["recipient"], "accounts@meridian.example.com")
        self.assertEqual(
            decision["vendor_email"]["recipient_source"],
            "automatically authorized extracted invoice email",
        )
        self.assertEqual(send.call_count, 2)

    def test_vendor_can_have_multiple_extracted_addresses(self):
        self.process_missing_invoice(
            "accounts@meridian.example.com", file_identity="invoice-one",
        )
        self.process_missing_invoice(
            "billing@meridian.example.com", file_identity="invoice-two",
        )
        rows = self.connection.execute(
            "select email,authorization_status from vendor_email_addresses "
            "where vendor_id='V004' order by email"
        ).fetchall()
        self.assertEqual(
            [(row["email"], row["authorization_status"]) for row in rows],
            [
                ("accounts@meridian.example.com", "AUTHORIZED"),
                ("billing@meridian.example.com", "AUTHORIZED"),
            ],
        )

    def test_untrusted_current_email_does_not_fall_back_to_an_old_authorized_address(self):
        self.process_missing_invoice("accounts@meridian.example.com")
        with patch.dict(os.environ, {
            "EMAIL_MODE": "smtp", "EMAIL_ALLOWLIST": "",
            "SMTP_HOST": "smtp.example.test", "SMTP_FROM_EMAIL": "accounts@example.test",
        }), patch("main.email_service.send_email") as send:
            events = self.process_missing_invoice(
                "billing@outside.example", file_identity="untrusted-current-email",
            )

        decision = next(event["decision"] for event in events if event["type"] == "decision")
        self.assertEqual(decision["vendor_email"]["status"], "OUTBOX")
        self.assertIsNone(decision["vendor_email"]["recipient"])
        self.assertIn("not authorized", decision["vendor_email"]["error"])
        send.assert_not_called()

    def test_invalid_extracted_email_is_stored_pending_and_not_sent(self):
        with patch.dict(os.environ, {
            "EMAIL_MODE": "smtp", "EMAIL_ALLOWLIST": "",
            "SMTP_HOST": "smtp.example.test", "SMTP_FROM_EMAIL": "accounts@example.test",
        }), patch("main.email_service.send_email") as send:
            events = self.process_missing_invoice("invalid-address")

        decision = next(event["decision"] for event in events if event["type"] == "decision")
        email = decision["vendor_email"]
        row = self.connection.execute(
            "select * from vendor_email_addresses where email='invalid-address'"
        ).fetchone()
        self.assertEqual(row["authorization_status"], "PENDING_REVIEW")
        self.assertIn("not a valid email", row["authorization_reason"])
        self.assertEqual(email["status"], "OUTBOX")
        self.assertIn("invalid", email["error"])
        send.assert_not_called()

    def test_low_confidence_vendor_match_does_not_authorize_email(self):
        with patch.dict(os.environ, {
            "EMAIL_MODE": "smtp", "EMAIL_ALLOWLIST": "",
            "SMTP_HOST": "smtp.example.test", "SMTP_FROM_EMAIL": "accounts@example.test",
        }), patch("main.rules.match_vendor", return_value=("V004", 0.84)), \
             patch("main.email_service.send_email") as send:
            events = self.process_missing_invoice("accounts@meridian.example.com")

        decision = next(event["decision"] for event in events if event["type"] == "decision")
        row = self.connection.execute(
            "select authorization_status,authorization_reason "
            "from vendor_email_addresses where email='accounts@meridian.example.com'"
        ).fetchone()
        self.assertEqual(row["authorization_status"], "PENDING_REVIEW")
        self.assertIn("below the 90%", row["authorization_reason"])
        self.assertEqual(decision["vendor_email"]["status"], "OUTBOX")
        send.assert_not_called()

    def test_low_email_extraction_confidence_does_not_authorize_email(self):
        with patch.dict(os.environ, {
            "EMAIL_MODE": "smtp", "EMAIL_ALLOWLIST": "",
            "SMTP_HOST": "smtp.example.test", "SMTP_FROM_EMAIL": "accounts@example.test",
        }), patch("main.email_service.send_email") as send:
            events = self.process_missing_invoice(
                "accounts@meridian.example.com", email_confidence=0.89,
            )

        decision = next(event["decision"] for event in events if event["type"] == "decision")
        row = self.connection.execute(
            "select authorization_status,authorization_reason "
            "from vendor_email_addresses where email='accounts@meridian.example.com'"
        ).fetchone()
        self.assertEqual(row["authorization_status"], "PENDING_REVIEW")
        self.assertIn("Email extraction confidence", row["authorization_reason"])
        self.assertEqual(decision["vendor_email"]["status"], "OUTBOX")
        send.assert_not_called()

    def test_conflicting_saved_vendor_contact_blocks_auto_authorization(self):
        self.connection.execute(
            "update vendors set email='saved@meridian.example.com' where vendor_id='V004'"
        )
        self.connection.commit()
        with patch.dict(os.environ, {
            "EMAIL_MODE": "smtp", "EMAIL_ALLOWLIST": "",
            "SMTP_HOST": "smtp.example.test", "SMTP_FROM_EMAIL": "accounts@example.test",
        }), patch("main.email_service.send_email") as send:
            events = self.process_missing_invoice("accounts@meridian.example.com")

        decision = next(event["decision"] for event in events if event["type"] == "decision")
        row = self.connection.execute(
            "select authorization_status,authorization_reason "
            "from vendor_email_addresses where email='accounts@meridian.example.com'"
        ).fetchone()
        self.assertEqual(row["authorization_status"], "PENDING_REVIEW")
        self.assertIn("conflicts with the vendor's saved contact", row["authorization_reason"])
        self.assertEqual(decision["vendor_email"]["recipient"], "saved@meridian.example.com")
        self.assertEqual(decision["vendor_email"]["status"], "OUTBOX")
        send.assert_not_called()

    def test_unrelated_vendor_domain_is_not_auto_authorized(self):
        with patch.dict(os.environ, {
            "EMAIL_MODE": "smtp", "EMAIL_ALLOWLIST": "",
            "SMTP_HOST": "smtp.example.test", "SMTP_FROM_EMAIL": "accounts@example.test",
        }), patch("main.email_service.send_email") as send:
            events = self.process_missing_invoice("billing@unrelated.example")

        decision = next(event["decision"] for event in events if event["type"] == "decision")
        row = self.connection.execute(
            "select authorization_status,authorization_reason "
            "from vendor_email_addresses where email='billing@unrelated.example'"
        ).fetchone()
        self.assertEqual(row["authorization_status"], "PENDING_REVIEW")
        self.assertIn("does not match a configured domain", row["authorization_reason"])
        self.assertEqual(decision["vendor_email"]["status"], "OUTBOX")
        self.assertIn("not authorized", decision["vendor_email"]["error"])
        send.assert_not_called()

    def test_vendor_email_registry_is_visible_in_vendor_apis(self):
        self.process_missing_invoice("accounts@meridian.example.com")

        vendor = next(
            item for item in self.client.get("/api/vendors").json()
            if item["id"] == "V004"
        )
        analytics_vendor = next(
            item for item in self.client.get("/api/analytics").json()["vendor_performance"]
            if item["vendor_id"] == "V004"
        )
        self.assertEqual(vendor["email_addresses"][0]["email"], "accounts@meridian.example.com")
        self.assertEqual(vendor["email_addresses"][0]["authorization_status"], "AUTHORIZED")
        self.assertEqual(
            analytics_vendor["email_addresses"][0]["source_invoice_hash"],
            vendor["email_addresses"][0]["source_invoice_hash"],
        )

    def test_saved_master_contact_wins_and_notes_different_invoice_email(self):
        self.connection.execute(
            "update vendors set email='billing@meridian.example' where vendor_id='V004'"
        )
        self.connection.commit()
        environment = {
            "EMAIL_MODE": "smtp",
            "EMAIL_ALLOWLIST": "billing@meridian.example",
            "EMAIL_MAX_PER_HOUR": "5",
            "SMTP_HOST": "smtp.example.test",
            "SMTP_FROM_EMAIL": "accounts@example.test",
        }
        with patch.dict(os.environ, environment), patch(
            "main.email_service.send_email",
            return_value={
                "recipient": "billing@meridian.example", "status": "SENT", "error": None,
            },
        ) as send:
            events = self.process_missing_invoice("invoice@outside.example")

        decision = next(event["decision"] for event in events if event["type"] == "decision")
        email = decision["vendor_email"]
        self.assertEqual(email["recipient"], "billing@meridian.example")
        self.assertEqual(email["recipient_source"], "vendor master")
        self.assertIn("invoice@outside.example", email["recipient_note"])
        send.assert_called_once_with(
            "billing@meridian.example", email["subject"], email["body"],
        )

    def test_unverified_invoice_email_is_outbox_without_sending(self):
        environment = {
            "EMAIL_MODE": "smtp",
            "EMAIL_ALLOWLIST": "",
            "EMAIL_MAX_PER_HOUR": "5",
            "SMTP_HOST": "smtp.example.test",
            "SMTP_FROM_EMAIL": "accounts@example.test",
        }
        with patch.dict(os.environ, environment), patch(
            "main.email_service.send_email"
        ) as send:
            events = self.process_missing_invoice("billing@outside.example")

        decision = next(event["decision"] for event in events if event["type"] == "decision")
        email = decision["vendor_email"]
        self.assertEqual(email["status"], "OUTBOX")
        self.assertIsNone(email["recipient"])
        self.assertEqual(email["recipient_source"], "no verified recipient")
        self.assertIn("no verified recipient", email["error"])
        self.assertIn("not authorized", email["error"])
        send.assert_not_called()

    def test_different_files_and_recipients_create_separate_notifications(self):
        environment = {
            "EMAIL_MODE": "smtp",
            "EMAIL_ALLOWLIST": "first@outside.example,second@outside.example",
            "EMAIL_MAX_PER_HOUR": "5",
            "SMTP_HOST": "smtp.example.test",
            "SMTP_FROM_EMAIL": "accounts@example.test",
        }

        def accepted(recipient, *_):
            return {"recipient": recipient, "status": "SENT", "error": None}

        with patch.dict(os.environ, environment), patch(
            "main.email_service.send_email", side_effect=accepted,
        ) as send:
            first = self.process_missing_invoice(
                "first@outside.example", file_identity="invoice-file-one",
                filename="first.pdf",
            )
            second = self.process_missing_invoice(
                "second@outside.example", file_identity="invoice-file-two",
                filename="second.pdf",
            )

        decisions = [
            next(event["decision"] for event in events if event["type"] == "decision")
            for events in (first, second)
        ]
        self.assertEqual([decision["action"] for decision in decisions],
                         ["HOLD_FOR_INFO", "HOLD_FOR_INFO"])
        self.assertEqual(
            [decision["vendor_email"]["recipient"] for decision in decisions],
            ["first@outside.example", "second@outside.example"],
        )
        notifications = self.connection.execute(
            "select recipient,status from email_notifications order by id"
        ).fetchall()
        self.assertEqual(
            [(row["recipient"], row["status"]) for row in notifications],
            [("first@outside.example", "SENT"), ("second@outside.example", "SENT")],
        )
        self.assertEqual(send.call_count, 2)

    def test_same_file_and_recipient_after_sent_is_deduplicated(self):
        environment = {
            "EMAIL_MODE": "smtp",
            "EMAIL_ALLOWLIST": "same@outside.example",
            "EMAIL_MAX_PER_HOUR": "5",
            "SMTP_HOST": "smtp.example.test",
            "SMTP_FROM_EMAIL": "accounts@example.test",
        }

        def accepted(recipient, *_):
            return {"recipient": recipient, "status": "SENT", "error": None}

        with patch.dict(os.environ, environment), patch(
            "main.rules.dup_check",
            return_value=main.rules.ck("R5", "pass", "No duplicate"),
        ), patch("main.email_service.send_email", side_effect=accepted) as send:
            first = self.process_missing_invoice(
                "same@outside.example",
            )
            second = self.process_missing_invoice(
                "same@outside.example",
            )

        second_decision = next(
            event["decision"] for event in second if event["type"] == "decision"
        )
        self.assertTrue(second_decision["vendor_email"]["duplicate_prevented"])
        self.assertEqual(second_decision["vendor_email"]["status"], "SENT")
        self.assertEqual(
            self.connection.execute("select count(*) from email_notifications").fetchone()[0],
            1,
        )
        send.assert_called_once()

    def test_same_file_and_recipient_retries_after_failed_attempt(self):
        environment = {
            "EMAIL_MODE": "smtp",
            "EMAIL_ALLOWLIST": "retry@outside.example",
            "EMAIL_MAX_PER_HOUR": "5",
            "SMTP_HOST": "smtp.example.test",
            "SMTP_FROM_EMAIL": "accounts@example.test",
        }
        outcomes = [
            {"recipient": "retry@outside.example", "status": "FAILED",
             "error": "SMTP login failed (SMTPServerDisconnected): Connection unexpectedly closed"},
            {"recipient": "retry@outside.example", "status": "SENT", "error": None},
        ]

        with patch.dict(os.environ, environment), patch(
            "main.rules.dup_check",
            return_value=main.rules.ck("R5", "pass", "No duplicate"),
        ), patch(
            "main.email_service.send_email", side_effect=outcomes,
        ) as send:
            self.process_missing_invoice(
                "retry@outside.example",
            )
            second = self.process_missing_invoice(
                "retry@outside.example",
            )

        second_decision = next(
            event["decision"] for event in second if event["type"] == "decision"
        )
        self.assertEqual(second_decision["vendor_email"]["status"], "SENT")
        notifications = self.connection.execute(
            "select status,attempt_number,error from email_notifications order by id"
        ).fetchall()
        self.assertEqual(
            [(row["status"], row["attempt_number"]) for row in notifications],
            [("FAILED", 1), ("SENT", 2)],
        )
        self.assertIn("SMTP login failed", notifications[0]["error"])
        self.assertEqual(
            self.client.get("/api/email-history").json()[-1]["error"],
            notifications[0]["error"],
        )
        self.assertEqual(send.call_count, 2)

    def test_unverified_recipient_creates_outbox_despite_matching_failed_notification(self):
        invoice_email = "billing@outside.example"
        invoice = inv(
            "Meridian Print & Packaging", None, "2026-11-04", "PO-1004",
            [("Retail boxes", 5, 10)], 0, "1204", total=None,
        )
        invoice["vendor_email"] = invoice_email
        invoice["vendor_email_invalid"] = False
        invoice["confidence"] = {"vendor_name": 0.99, "vendor_email": 0.99}
        file_hash = hashlib.sha256(json.dumps(invoice, sort_keys=True).encode()).hexdigest()
        issues = main.email_service.critical_field_issues(invoice)
        notification_key = main.email_service.notification_key(
            "V004", invoice, file_hash,
            issues, invoice_email,
        )
        self.connection.execute(
            "insert into email_notifications"
            "(run_id,vendor_id,dedupe_key,notification_key,attempt_number,recipient,"
            "subject,body,status,error,created_at) values(?,?,?,?,?,?,?,?,?,?,?)",
            ("previous", "V004", f"{notification_key}:1", notification_key, 1,
             invoice_email, "Subject", "Body", "FAILED", "Prior SMTP failure", time.time()),
        )
        self.connection.commit()
        environment = {
            "EMAIL_MODE": "smtp",
            "EMAIL_ALLOWLIST": "",
            "EMAIL_MAX_PER_HOUR": "5",
            "SMTP_HOST": "smtp.example.test",
            "SMTP_FROM_EMAIL": "accounts@example.test",
        }

        with patch.dict(os.environ, environment), patch(
            "main.email_service.send_email"
        ) as send:
            events = self.process_missing_invoice(
                invoice_email,
            )

        decision = next(event["decision"] for event in events if event["type"] == "decision")
        self.assertEqual(decision["vendor_email"]["status"], "OUTBOX")
        self.assertIn("no verified recipient", decision["vendor_email"]["error"])
        self.assertIn("not authorized", decision["vendor_email"]["error"])
        self.assertEqual(
            self.connection.execute(
                "select count(*) from email_notifications where notification_key=?",
                (notification_key,),
            ).fetchone()[0],
            2,
        )
        send.assert_not_called()

    def test_failed_smtp_attempt_counts_toward_hourly_email_cap(self):
        now = time.time()
        self.connection.execute(
            "insert into email_notifications"
            "(run_id,dedupe_key,notification_key,subject,body,status,error,created_at,attempted_at) "
            "values(?,?,?,?,?,?,?,?,?)",
            ("prior-failed-send", "prior-failed-send", "prior-failed-send",
             "Subject", "Body", "FAILED", "SMTP send failed (OSError): reset",
             now, now),
        )
        self.connection.commit()
        environment = {
            "EMAIL_MODE": "smtp",
            "EMAIL_ALLOWLIST": "hourly-cap@outside.example",
            "EMAIL_MAX_PER_HOUR": "1",
            "SMTP_HOST": "smtp.example.test",
            "SMTP_FROM_EMAIL": "accounts@example.test",
        }
        with patch.dict(os.environ, environment), patch(
            "main.email_service.send_email"
        ) as send:
            events = self.process_missing_invoice("hourly-cap@outside.example")

        decision = next(event["decision"] for event in events if event["type"] == "decision")
        email = decision["vendor_email"]
        self.assertEqual(email["status"], "OUTBOX")
        self.assertIn("Hourly email limit reached", email["error"])
        send.assert_not_called()

    def test_unknown_vendor_with_missing_fields_never_sends_email(self):
        invoice = inv(
            "Unknown Supplier", None, "2026-11-04", None,
            [("Supplies", 1, 10)], 0, "9999", total=None,
        )
        invoice["vendor_email"] = "accounts@outside.example"
        with patch.dict(os.environ, {
            "EMAIL_MODE": "smtp",
            "EMAIL_ALLOWLIST": "accounts@outside.example",
        }), patch("main.asyncio.sleep", new=AsyncMock()), patch(
            "main.email_service.send_email"
        ) as send:
            async def process_once():
                return [json.loads(line) async for line in main.pipeline(
                    "unknown-missing.pdf", None, None, "sample", invoice,
                )]
            events = asyncio.run(process_once())

        decision = next(event["decision"] for event in events if event["type"] == "decision")
        self.assertEqual(decision["action"], "ESCALATE")
        self.assertIsNone(decision["vendor_email"])
        self.assertEqual(
            self.connection.execute("select count(*) from email_notifications").fetchone()[0],
            0,
        )
        send.assert_not_called()

    def test_action_and_hard_stop_guards_block_any_draft_send(self):
        original_decide = main.rules.decide
        cases = [
            ("ESCALATE", False),
            ("AUTO_REJECT", False),
            ("HOLD_FOR_INFO", True),
        ]
        with patch.dict(os.environ, {
            "EMAIL_MODE": "smtp",
            "EMAIL_ALLOWLIST": "guarded@meridian.example.com",
            "EMAIL_MAX_PER_HOUR": "5",
        }), patch("main.asyncio.sleep", new=AsyncMock()), patch(
            "main.email_service.send_email"
        ) as send:
            for index, (action, has_hard_stop) in enumerate(cases):
                def guarded_decision(*args, **kwargs):
                    decision = original_decide(*args, **kwargs)
                    decision["action"] = action
                    decision["hard_stops"] = ["fixture hard stop"] if has_hard_stop else []
                    decision["vendor_email"] = {"subject": "Draft", "body": "Request"}
                    return decision

                invoice = inv(
                    "Meridian Print & Packaging", None, "2026-11-04", "PO-1004",
                    [("Retail boxes", 5, 10)], 0, "1204", total=None,
                )
                invoice["vendor_email"] = f"guarded-{index}@meridian.example.com"
                with patch("main.rules.decide", side_effect=guarded_decision):
                    async def process_once():
                        return [
                            json.loads(line)
                            async for line in main.pipeline(
                                f"guarded-{index}.pdf", None, None, "sample", invoice,
                            )
                        ]
                    events = asyncio.run(process_once())

                decision_event = next(
                    event for event in events if event["type"] == "decision"
                )
                self.assertIsNone(decision_event["decision"]["vendor_email"])

        self.assertEqual(
            self.connection.execute("select count(*) from email_notifications").fetchone()[0],
            0,
        )
        send.assert_not_called()

    def test_admin_token_protects_mutating_admin_routes_when_configured(self):
        with patch.dict(os.environ, {"ADMIN_TOKEN": "fixture-admin-token"}):
            unauthenticated = TestClient(main.app)
            try:
                self.assertEqual(unauthenticated.post("/api/reset").status_code, 401)
                self.assertEqual(
                    unauthenticated.put(
                        "/api/vendors/V001/email", json={"email": ""},
                    ).status_code,
                    401,
                )
            finally:
                unauthenticated.close()
            accepted = self.client.post(
                "/api/proposals/accept",
                headers={"X-Admin-Token": "fixture-admin-token"},
                json={"vendor_id": "V001", "tol": 0.02},
            )
            reset = self.client.post(
                "/api/reset", headers={"X-Admin-Token": "fixture-admin-token"}
            )
        self.assertEqual(accepted.status_code, 200)
        self.assertEqual(reset.status_code, 200)

    def test_missing_admin_token_fails_closed_for_privileged_operation(self):
        with patch.dict(os.environ, {"ADMIN_TOKEN": ""}):
            response = self.client.post("/api/reset")
            insecure_default = self.client.get(
                "/api/vendors", headers={"X-Admin-Token": "dev-token"},
            )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["detail"], "ADMIN_TOKEN is not configured")
        self.assertEqual(insecure_default.status_code, 503)

    def test_admin_authentication_rejects_missing_and_invalid_credentials(self):
        unauthenticated = TestClient(main.app)
        try:
            missing = unauthenticated.get("/api/vendors")
        finally:
            unauthenticated.close()
        invalid = self.client.get(
            "/api/vendors", headers={"X-Admin-Token": "invalid-credential"},
        )

        self.assertEqual(missing.status_code, 401)
        self.assertIn("WWW-Authenticate", missing.headers)
        self.assertEqual(invalid.status_code, 403)
        self.assertEqual(self.client.get("/api/vendors").status_code, 200)

    def test_private_api_endpoints_require_admin_authentication(self):
        unauthenticated = TestClient(main.app)
        try:
            paths = (
                "/api/runs", "/api/runs/missing", "/api/vendors",
                "/api/email-history", "/api/analytics", "/api/stats",
                "/api/proposals", "/api/live",
            )
            responses = [unauthenticated.get(path) for path in paths]
            responses.extend((
                unauthenticated.post(
                    "/api/runs/missing/review", json={"verdict": "approve"},
                ),
                unauthenticated.post(
                    "/api/runs/missing/emails/1/resend",
                ),
                unauthenticated.post("/api/run"),
            ))
            health = unauthenticated.get("/healthz")
            home = unauthenticated.get("/")
        finally:
            unauthenticated.close()

        self.assertTrue(all(response.status_code == 401 for response in responses))
        self.assertEqual(health.status_code, 200)
        self.assertEqual(home.status_code, 200)

    def test_basic_authentication_supports_browser_dashboard_access(self):
        authorization = "Basic " + base64.b64encode(
            b"admin:fixture-admin-token"
        ).decode("ascii")
        with TestClient(main.app, headers={"Authorization": authorization}) as client:
            response = client.get("/api/vendors")

        self.assertEqual(response.status_code, 200)

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
            headers = {"X-Inbound-Token": "fixture-inbound-token"}

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
                response = await main._process_inbound(InboundRequest())
                await asyncio.gather(*tasks)
                return response

        response = asyncio.run(submit())

        self.assertEqual(response, {"queued": 1})
        self.assertEqual(pipeline_calls[0][0][3], "email")
        self.assertEqual(pipeline_calls[0][1], sender)

    def test_inbound_webhook_requires_configured_header_token(self):
        with patch.dict(os.environ, {"INBOUND_TOKEN": "real-fixture-token"}):
            missing = self.client.post("/api/inbound", json={"Attachments": []})
            invalid = self.client.post(
                "/api/inbound", headers={"X-Inbound-Token": "wrong-token"},
                json={"Attachments": []},
            )
            accepted = self.client.post(
                "/api/inbound", headers={"X-Inbound-Token": "real-fixture-token"},
                json={"Attachments": []},
            )

        self.assertEqual(missing.status_code, 401)
        self.assertEqual(invalid.status_code, 403)
        self.assertEqual(accepted.status_code, 200)

    def test_inbound_webhook_has_no_insecure_dev_token_fallback(self):
        with patch.dict(os.environ, {"INBOUND_TOKEN": ""}):
            response = self.client.post(
                "/api/inbound", headers={"X-Inbound-Token": "dev-token"},
                json={"Attachments": []},
            )

        self.assertEqual(response.status_code, 503)

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
            "notification_key", "attempt_number", "resend_claimed_at",
            "recipient_source", "recipient_note",
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
