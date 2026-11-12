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
        self.assertIn("authentication failed", result["error"])

    def test_smtp_connection_failure_is_recorded(self):
        with patch.dict(os.environ, SMTP_ENV, clear=False), patch(
            "email_service.smtplib.SMTP", side_effect=OSError("connection refused")
        ):
            result = email_service.send_email(
                "ap@vendor.example", "Invoice details needed", "Please correct invoice."
            )

        self.assertEqual(result["status"], "FAILED")
        self.assertIn("connection refused", result["error"])

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
        self.assertIn("refused recipient", result["error"])

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
