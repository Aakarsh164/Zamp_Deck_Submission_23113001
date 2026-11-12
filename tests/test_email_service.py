import os
import smtplib
import unittest
from unittest.mock import patch

import email_service


SMTP_ENV = {
    "SMTP_HOST": "smtp.example.test",
    "SMTP_PORT": "587",
    "SMTP_USER": "",
    "SMTP_PASSWORD": "",
    "SMTP_FROM_EMAIL": "accounts@example.test",
    "SMTP_USERNAME": "",
    "SMTP_FROM": "",
}


class EmailServiceTests(unittest.TestCase):
    def test_saved_vendor_contact_takes_priority_and_notes_invoice_difference(self):
        with patch.dict(os.environ, {"EMAIL_ALLOWLIST": "master@meridian.example"}):
            selection = email_service.select_recipient(
                "Master@Meridian.Example", "invoice@outside.example",
                ["meridian.example.com"],
            )

        self.assertEqual(selection["recipient"], "master@meridian.example")
        self.assertEqual(selection["recipient_source"], "vendor master")
        self.assertIn("invoice@outside.example", selection["recipient_note"])
        self.assertTrue(selection["allowlisted"])
        self.assertFalse(selection["invoice_domain_verified"])

    def test_invoice_contact_can_be_selected_by_allowlist(self):
        with patch.dict(os.environ, {"EMAIL_ALLOWLIST": "ap@other.example"}):
            selection = email_service.select_recipient(
                "", "AP@Other.Example", ["meridian.example.com"],
            )

        self.assertEqual(selection["recipient"], "ap@other.example")
        self.assertEqual(selection["recipient_source"], "from invoice, allowlisted")
        self.assertTrue(selection["allowlisted"])

    def test_invoice_contact_can_be_selected_by_exact_vendor_domain(self):
        with patch.dict(os.environ, {"EMAIL_ALLOWLIST": ""}):
            selection = email_service.select_recipient(
                "", "Accounts@Meridian.Example.Com", ["meridian.example.com"],
            )

        self.assertEqual(selection["recipient"], "accounts@meridian.example.com")
        self.assertEqual(selection["recipient_source"], "from invoice, verified vendor domain")
        self.assertTrue(selection["invoice_domain_verified"])

    def test_unverified_invoice_contact_is_not_selected(self):
        with patch.dict(os.environ, {"EMAIL_ALLOWLIST": ""}):
            selection = email_service.select_recipient(
                "", "accounts@outside.example", ["meridian.example.com"],
            )

        self.assertIsNone(selection["recipient"])
        self.assertEqual(selection["recipient_source"], "no verified recipient")

    def test_vendor_master_address_must_be_allowlisted_to_send(self):
        environment = dict(
            SMTP_ENV, EMAIL_MODE="smtp", EMAIL_ALLOWLIST="", EMAIL_MAX_PER_HOUR="5",
        )
        with patch.dict(os.environ, environment, clear=False):
            can_send, reason = email_service.delivery_eligibility(
                "master@meridian.example", allowlisted=False,
            )

        self.assertFalse(can_send)
        self.assertIn("EMAIL_ALLOWLIST", reason)

    def test_resend_mode_only_allows_an_explicit_resend(self):
        environment = dict(
            SMTP_ENV, EMAIL_MODE="resend", EMAIL_ALLOWLIST="ap@vendor.example",
            EMAIL_MAX_PER_HOUR="5",
        )
        with patch.dict(os.environ, environment, clear=False):
            initial_send = email_service.delivery_eligibility(
                "ap@vendor.example", allowlisted=True,
            )
            retry_send = email_service.delivery_eligibility(
                "ap@vendor.example", allowlisted=True, is_resend=True,
            )

        self.assertFalse(initial_send[0])
        self.assertIn("initial emails are not sent", initial_send[1])
        self.assertTrue(retry_send[0])

    def test_sends_email_only_after_smtp_accepts_it(self):
        with patch.dict(os.environ, SMTP_ENV, clear=False), patch(
            "email_service.smtplib.SMTP"
        ) as smtp:
            server = smtp.return_value.__enter__.return_value
            server.send_message.return_value = {}

            result = email_service.send_email(
                "ap@vendor.example", "Invoice details needed", "Please correct invoice."
            )

        self.assertEqual(result["status"], "SENT")
        smtp.assert_called_once_with("smtp.example.test", 587, timeout=20)
        server.starttls.assert_called_once()
        server.send_message.assert_called_once()

    def test_missing_vendor_email_is_recorded_as_failed_without_smtp(self):
        with patch.dict(os.environ, SMTP_ENV, clear=False), patch(
            "email_service.smtplib.SMTP"
        ) as smtp:
            result = email_service.send_email(
                "", "Invoice details needed", "Please correct invoice."
            )

        self.assertEqual(result["status"], "FAILED")
        self.assertIn("No email address", result["error"])
        smtp.assert_not_called()

    def test_invalid_vendor_email_is_recorded_as_failed(self):
        with patch.dict(os.environ, SMTP_ENV, clear=False), patch(
            "email_service.smtplib.SMTP"
        ) as smtp:
            result = email_service.send_email(
                "not-an-email", "Invoice details needed", "Please correct invoice."
            )

        self.assertEqual(result["status"], "FAILED")
        self.assertIn("invalid", result["error"])
        smtp.assert_not_called()

    def test_smtp_authentication_failure_is_recorded(self):
        env = dict(SMTP_ENV, SMTP_USER="smtp-user", SMTP_PASSWORD="wrong-password")
        with patch.dict(os.environ, env, clear=False), patch(
            "email_service.smtplib.SMTP"
        ) as smtp:
            server = smtp.return_value.__enter__.return_value
            server.login.side_effect = smtplib.SMTPAuthenticationError(535, b"denied")

            result = email_service.send_email(
                "ap@vendor.example", "Invoice details needed", "Please correct invoice."
            )

        self.assertEqual(result["status"], "FAILED")
        self.assertIn("SMTP login failed (SMTPAuthenticationError)", result["error"])
        self.assertNotIn("wrong-password", result["error"])

    def test_smtp_connection_failure_is_recorded(self):
        with patch.dict(os.environ, SMTP_ENV, clear=False), patch(
            "email_service.smtplib.SMTP", side_effect=OSError("connection refused")
        ):
            result = email_service.send_email(
                "ap@vendor.example", "Invoice details needed", "Please correct invoice."
            )

        self.assertEqual(result["status"], "FAILED")
        self.assertIn("SMTP connect failed (OSError): connection refused", result["error"])

    def test_smtp_starttls_failure_reports_stage_and_exception(self):
        with patch.dict(os.environ, SMTP_ENV, clear=False), patch(
            "email_service.smtplib.SMTP"
        ) as smtp:
            smtp.return_value.__enter__.return_value.starttls.side_effect = (
                smtplib.SMTPException("TLS handshake failed")
            )

            result = email_service.send_email(
                "ap@vendor.example", "Invoice details needed", "Please correct invoice."
            )

        self.assertEqual(result["status"], "FAILED")
        self.assertIn("SMTP starttls failed (SMTPException): TLS handshake failed", result["error"])

    def test_smtp_login_disconnect_reports_stage_and_exception(self):
        with patch.dict(os.environ, dict(SMTP_ENV, SMTP_USER="smtp-user",
                                         SMTP_PASSWORD="pwd123"),
                         clear=False), patch(
            "email_service.smtplib.SMTP"
        ) as smtp:
            smtp.return_value.__enter__.return_value.login.side_effect = (
                smtplib.SMTPServerDisconnected("Connection unexpectedly closed")
            )

            result = email_service.send_email(
                "ap@vendor.example", "Invoice details needed", "Please correct invoice."
            )

        self.assertEqual(result["status"], "FAILED")
        self.assertIn("SMTP login failed (SMTPServerDisconnected)", result["error"])
        self.assertIn("Connection unexpectedly closed", result["error"])
        self.assertNotIn("pwd123", result["error"])

    def test_smtp_send_failure_reports_stage_and_exception(self):
        with patch.dict(os.environ, SMTP_ENV, clear=False), patch(
            "email_service.smtplib.SMTP"
        ) as smtp:
            smtp.return_value.__enter__.return_value.send_message.side_effect = (
                OSError("connection reset during send")
            )

            result = email_service.send_email(
                "ap@vendor.example", "Invoice details needed", "Please correct invoice."
            )

        self.assertEqual(result["status"], "FAILED")
        self.assertIn("SMTP send failed (OSError): connection reset during send", result["error"])

    def test_smtp_timeout_is_recorded(self):
        with patch.dict(os.environ, SMTP_ENV, clear=False), patch(
            "email_service.smtplib.SMTP", side_effect=TimeoutError()
        ):
            result = email_service.send_email(
                "ap@vendor.example", "Invoice details needed", "Please correct invoice."
            )

        self.assertEqual(result["status"], "FAILED")
        self.assertIn("timed out", result["error"])

    def test_recipient_rejection_is_not_reported_as_sent(self):
        with patch.dict(os.environ, SMTP_ENV, clear=False), patch(
            "email_service.smtplib.SMTP"
        ) as smtp:
            smtp.return_value.__enter__.return_value.send_message.return_value = {
                "ap@vendor.example": (550, b"mailbox unavailable")
            }

            result = email_service.send_email(
                "ap@vendor.example", "Invoice details needed", "Please correct invoice."
            )

        self.assertEqual(result["status"], "FAILED")
        self.assertIn("SMTP send failed (SMTPRecipientsRefused)", result["error"])
        self.assertIn("mailbox unavailable", result["error"])

    def test_request_identifies_all_missing_fields_and_invoice_references(self):
        invoice = {
            "invoice_number": None,
            "invoice_date": "2026-11-10",
            "total": None,
            "po_reference": "PO-4108",
        }
        issues = email_service.critical_field_issues(invoice)

        email = email_service.build_information_request(
            "Meridian Print & Packaging", invoice, issues
        )

        self.assertIn("Invoice number", email["body"])
        self.assertIn("Invoice total", email["body"])
        self.assertIn("PO number: PO-4108", email["body"])
        self.assertIn("Dear Meridian Print & Packaging", email["body"])
        self.assertIn("corrected invoice", email["body"])

    def test_invalid_critical_fields_are_explained(self):
        issues = email_service.critical_field_issues({
            "vendor_name": "Meridian Print & Packaging",
            "invoice_number": "INV-1",
            "invoice_date": "not-a-date",
            "total": -25,
        })

        self.assertIn(("invoice_date", "invalid date"), issues)
        self.assertIn(("total", "invalid amount"), issues)


if __name__ == "__main__":
    unittest.main()
