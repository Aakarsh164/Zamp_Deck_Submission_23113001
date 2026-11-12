import hashlib
import os
import re
import smtplib
import socket
import ssl
from datetime import date
from decimal import Decimal, InvalidOperation
from email.message import EmailMessage
from email.utils import parseaddr


FIELD_LABELS = {
    "vendor_name": "Vendor name",
    "invoice_number": "Invoice number",
    "invoice_date": "Invoice date",
    "total": "Invoice total",
}


def valid_email(value):
    if not isinstance(value, str) or "\r" in value or "\n" in value:
        return False
    address = parseaddr(value.strip())[1]
    return bool(address == value.strip() and re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", address))


def critical_field_issues(invoice):
    issues = []
    vendor_name = invoice.get("vendor_name")
    if not isinstance(vendor_name, str) or not vendor_name.strip():
        issues.append(("vendor_name", "missing"))

    invoice_number = invoice.get("invoice_number")
    if not isinstance(invoice_number, str) or not invoice_number.strip():
        issues.append(("invoice_number", "missing"))

    invoice_date = invoice.get("invoice_date")
    if not isinstance(invoice_date, str) or not invoice_date.strip():
        issues.append(("invoice_date", "missing"))
    else:
        try:
            date.fromisoformat(invoice_date)
        except ValueError:
            issues.append(("invoice_date", "invalid date"))

    total = invoice.get("total")
    if total is None or total == "":
        issues.append(("total", "missing"))
    else:
        try:
            amount = Decimal(str(total))
            if not amount.is_finite() or amount < 0:
                issues.append(("total", "invalid amount"))
        except (InvalidOperation, ValueError):
            issues.append(("total", "invalid amount"))

    return issues


def build_information_request(vendor_name, invoice, issues):
    name = vendor_name.strip() if isinstance(vendor_name, str) and vendor_name.strip() else "Vendor"
    invoice_number = invoice.get("invoice_number")
    po_number = invoice.get("po_reference")
    references = []
    if isinstance(invoice_number, str) and invoice_number.strip():
        references.append(f"Invoice number: {invoice_number.strip()}")
    if isinstance(po_number, str) and po_number.strip():
        references.append(f"PO number: {po_number.strip()}")

    lines = [
        f"Dear {name},",
        "",
        "We received your invoice but cannot process it because the following "
        "information is missing or invalid:",
        "",
    ]
    lines.extend(
        f"- {FIELD_LABELS.get(field, field.replace('_', ' ').title())}"
        + (f" ({reason})" if reason != "missing" else "")
        for field, reason in issues
    )
    lines.extend([
        "",
        "Please send us a corrected invoice containing these details so that "
        "we can continue processing it.",
        "",
        "Regards,",
        "Accounts Payable Team",
    ])
    if references:
        lines[2:2] = references + [""]
    return {
        "subject": "Invoice received - Additional information required",
        "body": "\n".join(lines),
    }


def notification_key(vendor_id, invoice, file_hash, issues):
    invoice_number = invoice.get("invoice_number")
    issue_fields = ",".join(sorted({field for field, _ in issues}))
    if isinstance(invoice_number, str) and invoice_number.strip():
        identity = "|".join((
            str(vendor_id or ""),
            invoice_number.strip().casefold(),
            str(invoice.get("invoice_date") or ""),
            str(invoice.get("po_reference") or ""),
            issue_fields,
        ))
    else:
        invoice_date = invoice.get("invoice_date")
        po_number = invoice.get("po_reference")
        total = invoice.get("total")
        available_identity = [value for value in (invoice_date, po_number, total) if value not in (None, "")]
        identity_parts = [str(vendor_id or ""), issue_fields]
        if available_identity:
            identity_parts.extend(str(value) for value in (invoice_date, po_number, total))
        else:
            identity_parts.append(file_hash)
        identity = "|".join(identity_parts)
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _failure(recipient, reason):
    return {"recipient": recipient, "status": "FAILED", "error": reason}


def delivery_eligibility(recipient):
    mode = os.getenv("EMAIL_MODE", "demo").strip().lower() or "demo"
    if mode not in {"demo", "smtp"}:
        raise ValueError("EMAIL_MODE must be either demo or smtp")
    if mode == "demo":
        return False, "Drafted, demo mode, not sent"
    if not recipient:
        return False, "Drafted, SMTP mode: no vendor recipient is registered"
    allowlist = {
        address.strip().casefold()
        for address in os.getenv("EMAIL_ALLOWLIST", "").split(",")
        if address.strip()
    }
    if recipient.strip().casefold() not in allowlist:
        return False, "Drafted, SMTP mode: recipient is not in EMAIL_ALLOWLIST"
    host = os.getenv("SMTP_HOST", "").strip()
    sender = (os.getenv("SMTP_FROM_EMAIL") or os.getenv("SMTP_FROM") or "").strip()
    if not host or not sender:
        return False, "Drafted, SMTP mode: SMTP_HOST and SMTP_FROM_EMAIL must be configured"
    if not valid_email(sender):
        return False, "Drafted, SMTP mode: SMTP_FROM_EMAIL is invalid"
    username = os.getenv("SMTP_USER") or os.getenv("SMTP_USERNAME") or ""
    password = os.getenv("SMTP_PASSWORD") or ""
    if bool(username) != bool(password):
        return False, "Drafted, SMTP mode: configure both SMTP_USER and SMTP_PASSWORD"
    try:
        port = int(os.getenv("SMTP_PORT", "587"))
    except ValueError:
        return False, "Drafted, SMTP mode: SMTP_PORT must be a number"
    if not 1 <= port <= 65535:
        return False, "Drafted, SMTP mode: SMTP_PORT must be between 1 and 65535"
    return True, None


def send_email(recipient, subject, body):
    if not recipient:
        return _failure(None, "No email address is registered for this vendor")
    recipient = recipient.strip() if isinstance(recipient, str) else recipient
    if not valid_email(recipient):
        return _failure(recipient, "Vendor email address is invalid")

    host = os.getenv("SMTP_HOST", "").strip()
    sender = (os.getenv("SMTP_FROM_EMAIL") or os.getenv("SMTP_FROM") or "").strip()
    username = os.getenv("SMTP_USER") or os.getenv("SMTP_USERNAME") or ""
    password = os.getenv("SMTP_PASSWORD") or ""
    if not host or not sender:
        return _failure(recipient, "SMTP_HOST and SMTP_FROM_EMAIL must be configured")
    if not valid_email(sender):
        return _failure(recipient, "SMTP_FROM_EMAIL is invalid")
    if bool(username) != bool(password):
        return _failure(recipient, "Configure both SMTP_USER and SMTP_PASSWORD, or neither")

    try:
        port = int(os.getenv("SMTP_PORT", "587"))
        if not 1 <= port <= 65535:
            return _failure(recipient, "SMTP_PORT must be between 1 and 65535")
        message = EmailMessage()
        message["From"] = sender
        message["To"] = recipient
        message["Subject"] = subject
        message.set_content(body)

        with smtplib.SMTP(host, port, timeout=20) as server:
            server.ehlo()
            server.starttls(context=ssl.create_default_context())
            server.ehlo()
            if username:
                server.login(username, password)
            refused = server.send_message(message)
        if refused:
            return _failure(recipient, f"SMTP server refused recipient: {refused}")
        return {"recipient": recipient, "status": "SENT", "error": None}
    except smtplib.SMTPAuthenticationError:
        return _failure(recipient, "SMTP authentication failed; check SMTP_USER and SMTP_PASSWORD")
    except (TimeoutError, socket.timeout):
        return _failure(recipient, "SMTP connection timed out")
    except (OSError, smtplib.SMTPException, ValueError) as exc:
        return _failure(recipient, f"SMTP delivery failed: {exc}")
