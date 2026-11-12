import json
import os
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import parseaddr


def send_vendor_email(vendor_id, content):
    recipient = None
    try:
        addresses = json.loads(os.getenv("VENDOR_EMAILS") or "{}")
        if not isinstance(addresses, dict):
            raise ValueError("VENDOR_EMAILS must be a JSON object keyed by vendor ID")
        recipient = addresses.get(vendor_id)
        if not isinstance(recipient, str) or not recipient.strip():
            raise ValueError(f"No email address configured for vendor {vendor_id}")
        recipient = recipient.strip()
        if "\r" in recipient or "\n" in recipient or parseaddr(recipient)[1] != recipient:
            raise ValueError(f"Invalid email address configured for vendor {vendor_id}")

        host = os.getenv("SMTP_HOST", "").strip()
        sender = os.getenv("SMTP_FROM", "").strip()
        if not host or not sender:
            raise ValueError("SMTP_HOST and SMTP_FROM must be configured")
        if "\r" in sender or "\n" in sender or parseaddr(sender)[1] != sender:
            raise ValueError("SMTP_FROM is not a valid email address")
        port = int(os.getenv("SMTP_PORT", "587"))
        username = os.getenv("SMTP_USERNAME", "")
        password = os.getenv("SMTP_PASSWORD", "")
        if bool(username) != bool(password):
            raise ValueError("Configure both SMTP_USERNAME and SMTP_PASSWORD, or neither")

        message = EmailMessage()
        message["From"] = sender
        message["To"] = recipient
        message["Subject"] = content["subject"]
        message.set_content(content["body"])
        with smtplib.SMTP(host, port, timeout=20) as server:
            server.ehlo()
            server.starttls(context=ssl.create_default_context())
            server.ehlo()
            if username:
                server.login(username, password)
            refused = server.send_message(message)
        if refused:
            raise smtplib.SMTPRecipientsRefused(refused)
        return dict(recipient=recipient, status="sent", error=None)
    except (ValueError, OSError, smtplib.SMTPException) as exc:
        return dict(recipient=recipient, status="failed", error=str(exc))
