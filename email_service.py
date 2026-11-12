import hashlib
import os
import re
import smtplib
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
EMAIL_PATTERN = re.compile(
    r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@"
    r"(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+"
    r"[A-Za-z]{2,63}"
)


def valid_email(value):
    if not isinstance(value, str) or "\r" in value or "\n" in value:
        return False
    address = parseaddr(value.strip())[1]
    return bool(address == value.strip() and EMAIL_PATTERN.fullmatch(address))


def normalize_email(value):
    if not valid_email(value):
        return None
    return value.strip().lower()


def select_recipient(
    vendor_contact, invoice_email, vendor_domains, *, dynamically_authorized=False
):
    master = normalize_email(vendor_contact)
    invoice = normalize_email(invoice_email)
    allowlist = {
        normalized
        for normalized in (
            normalize_email(item)
            for item in os.getenv("EMAIL_ALLOWLIST", "").split(",")
        )
        if normalized
    }
    domains = {
        str(domain).strip().lower().lstrip("@")
        for domain in vendor_domains or []
        if isinstance(domain, str) and domain.strip()
    }
    invoice_domain_verified = bool(
        invoice and invoice.rsplit("@", 1)[1] in domains
    )
    if master:
        note = (
            f"Invoice shows a different address: {invoice}"
            if invoice and invoice != master else None
        )
        return {
            "recipient": master,
            "recipient_source": "vendor master",
            "recipient_note": note,
            "allowlisted": master in allowlist,
            "invoice_domain_verified": False,
        }
    if invoice and invoice in allowlist:
        return {
            "recipient": invoice,
            "recipient_source": "from invoice, allowlisted",
            "recipient_note": None,
            "allowlisted": True,
            "invoice_domain_verified": False,
        }
    if invoice and dynamically_authorized:
        return {
            "recipient": invoice,
            "recipient_source": "automatically authorized extracted invoice email",
            "recipient_note": None,
            "allowlisted": False,
            "invoice_domain_verified": True,
        }
    if invoice and invoice_domain_verified:
        return {
            "recipient": invoice,
            "recipient_source": "from invoice, verified vendor domain",
            "recipient_note": None,
            "allowlisted": False,
            "invoice_domain_verified": True,
        }
    return {
        "recipient": None,
        "recipient_source": "no verified recipient",
        "recipient_note": None,
        "allowlisted": False,
        "invoice_domain_verified": False,
    }


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


def notification_key(vendor_id, invoice, file_hash, issues, recipient=None):
    identity = "\0".join((
        str(vendor_id or ""),
        str(file_hash or ""),
        normalize_email(recipient) or "",
        ",".join(sorted({field for field, _ in issues})),
    ))
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _failure(recipient, reason):
    return {"recipient": recipient, "status": "FAILED", "error": reason}


def _smtp_failure(recipient, stage, exc, username, password):
    message = str(exc) or ("timed out" if isinstance(exc, TimeoutError) else "")
    for credential in (username, password):
        if credential:
            message = message.replace(credential, "[redacted]")
    message = re.sub(r"[\r\n\t]+", " ", message).strip() or "No error message"
    error = f"SMTP {stage} failed ({type(exc).__name__}): {message[:200]}"
    return _failure(recipient, error)


def delivery_eligibility(
    recipient, *, allowlisted=False, invoice_domain_verified=False, is_resend=False
):
    mode = os.getenv("EMAIL_MODE", "demo").strip().lower() or "demo"
    if mode not in {"demo", "smtp", "resend"}:
        raise ValueError("EMAIL_MODE must be demo, smtp, or resend")
    if mode == "demo":
        return False, "Drafted, demo mode, not sent"
    if mode == "resend" and not is_resend:
        return False, "Drafted, EMAIL_MODE=resend: initial emails are not sent"
    if not recipient:
        return False, "no verified recipient"
    if not allowlisted and not invoice_domain_verified:
        return False, "recipient is not in EMAIL_ALLOWLIST and has no verified vendor domain"
    try:
        maximum = int(os.getenv("EMAIL_MAX_PER_HOUR", "5"))
    except ValueError:
        return False, "EMAIL_MAX_PER_HOUR must be a positive integer"
    if maximum < 1:
        return False, "EMAIL_MAX_PER_HOUR must be a positive integer"
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

    stage = "connect"
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
            stage = "starttls"
            server.ehlo()
            server.starttls(context=ssl.create_default_context())
            server.ehlo()
            stage = "login"
            if username:
                server.login(username, password)
            stage = "send"
            refused = server.send_message(message)
        if refused:
            return _failure(
                recipient,
                f"SMTP send failed (SMTPRecipientsRefused): {str(refused)[:200]}",
            )
        return {"recipient": recipient, "status": "SENT", "error": None}
    except (OSError, smtplib.SMTPException, ValueError) as exc:
        return _smtp_failure(recipient, stage, exc, username, password)
