"""Pluggable invoice reader. EXTRACTOR=gemini (free key) | anthropic (paid) | local (no key, text PDFs only).
If EXTRACTOR is unset: GEMINI_API_KEY -> gemini, else ANTHROPIC_API_KEY -> anthropic, else local."""
import asyncio, json, os, base64, math, re, io
from decimal import Decimal, InvalidOperation
from datetime import datetime
import httpx
from anthropic import AsyncAnthropic
from groq import AsyncGroq
from pypdf import PdfReader
import pymupdf
from PIL import Image


EMAIL_PATTERN = re.compile(
    r"[A-Z0-9.!#$%&'*+/=?^_`{|}~-]+@"
    r"(?:[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?\.)+"
    r"[A-Z]{2,63}",
    re.I,
)

PROMPT = """You extract data from vendor invoices. Return ONLY one JSON object, no prose, with keys:
vendor_name, vendor_email (the vendor's billing/accounts contact printed on the invoice, or null),
invoice_number, invoice_date (YYYY-MM-DD), po_reference, currency,
line_items [{description, quantity, unit_price, amount}], subtotal, tax,
tax_included (true only if prices already include tax), total,
bank_last4 (last 4 digits of any bank account or IBAN shown),
confidence {vendor_name, vendor_email, invoice_number, invoice_date, total} (0 to 1 each),
evidence {field: short verbatim snippet from the document}.
Rules: use null for anything not clearly present. Never guess or compute a missing value.
Numbers are plain numbers with no currency symbols."""


class InvalidExtractorJSON(RuntimeError):
    pass


class ExtractionError(RuntimeError):
    pass


def backend():
    b = os.getenv("EXTRACTOR", "").lower()

    if b in ("anthropic", "gemini", "groq", "local"):
        return b

    if os.getenv("GROQ_API_KEY"):
        return "groq"

    if os.getenv("GEMINI_API_KEY"):
        return "gemini"

    if os.getenv("ANTHROPIC_API_KEY"):
        return "anthropic"

    return "local"

def _json(text):
    if not isinstance(text, str):
        raise InvalidExtractorJSON("Extractor response was not text")
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise InvalidExtractorJSON("Extractor response did not contain a JSON object")
    try:
        result = json.loads(m.group(0))
    except json.JSONDecodeError as exc:
        raise InvalidExtractorJSON(f"Extractor returned invalid JSON: {exc.msg}") from exc
    if not isinstance(result, dict):
        raise InvalidExtractorJSON("Extractor JSON must be an object")
    return result

async def _claude(data, mime):
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key: raise RuntimeError("ANTHROPIC_API_KEY is not set")
    block = {"type": "document" if mime == "application/pdf" else "image",
             "source": {"type": "base64", "media_type": mime, "data": base64.b64encode(data).decode()}}
    r = await AsyncAnthropic(api_key=key).messages.create(
        model=os.getenv("MODEL", "claude-sonnet-5-5"), max_tokens=2000, system=PROMPT,
        messages=[{"role": "user", "content": [block, {"type": "text", "text": "Extract this invoice."}]}])
    return _json("".join(b.text for b in r.content if b.type == "text"))

async def _gemini(data, mime):
    key, model = os.getenv("GEMINI_API_KEY"), os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
    if not key: raise RuntimeError("GEMINI_API_KEY is not set")
    body = {"system_instruction": {"parts": [{"text": PROMPT}]},
            "contents": [{"parts": [{"inline_data": {"mime_type": mime, "data": base64.b64encode(data).decode()}},
                                    {"text": "Extract this invoice."}]}],
            "generationConfig": {"responseMimeType": "application/json"}}
    async with httpx.AsyncClient(timeout=90) as c:
        r = await c.post(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                         headers={"x-goog-api-key": key}, json=body)
    if r.status_code != 200: raise RuntimeError(f"Gemini error {r.status_code}: {r.text[:250]}")
    return _json(r.json()["candidates"][0]["content"]["parts"][0]["text"])

async def _groq(data, mime):
    key = os.getenv("GROQ_API_KEY")
    model = os.getenv("GROQ_MODEL", "qwen/qwen3.8-27b")

    if not key:
        raise RuntimeError("GROQ_API_KEY is not set")

    content = [
        {"type": "text", "text": "Extract this invoice. Return valid JSON only."}
    ]

    # PDF: first try extracting its text layer
    if mime == "application/pdf":
        text = "\n".join(
            p.extract_text() or ""
            for p in PdfReader(io.BytesIO(data)).pages
        )

        if len(text.strip()) >= 50:
            content[0]["text"] += "\n\nInvoice text:\n" + text

        else:
            # Scanned PDF: render pages into images
            doc = pymupdf.open(stream=data, filetype="pdf")

            if len(doc) > 3:
                raise RuntimeError(
                    "Scanned PDFs with more than 3 pages need to be split "
                    "into smaller files for Groq vision extraction."
                )

            for page in doc:
                pix = page.get_pixmap(
                    matrix=pymupdf.Matrix(1.5, 1.5),
                    alpha=False
                )

                img = Image.open(
                    io.BytesIO(pix.tobytes("png"))
                ).convert("RGB")

                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=90)

                encoded = base64.b64encode(
                    buf.getvalue()
                ).decode()

                content.append({
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:image/jpeg;base64,{encoded}"
                    }
                })

    else:
        # Normal image upload
        img = Image.open(io.BytesIO(data)).convert("RGB")

        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=90)

        encoded = base64.b64encode(
            buf.getvalue()
        ).decode()

        content.append({
            "type": "image_url",
            "image_url": {
                "url": f"data:image/jpeg;base64,{encoded}"
            }
        })

    client = AsyncGroq(api_key=key)

    response = await client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": PROMPT},
            {"role": "user", "content": content}
        ],
        response_format={"type": "json_object"},
        temperature=0,
        max_completion_tokens=2000
    )

    result = response.choices[0].message.content

    if not result:
        raise RuntimeError("Groq returned an empty response")

    return _json(result)

def _date(text):
    for pat, fmts in ((r"\d{4}-\d{2}-\d{2}", ["%Y-%m-%d"]), (r"\d{1,2}\s+[A-Za-z]{3,9}\.?,?\s+\d{4}", ["%d %b %Y", "%d %B %Y"]),
                      (r"\d{1,2}/\d{1,2}/\d{4}", ["%d/%m/%Y"])):
        m = re.search(pat, text)
        if m:
            s = re.sub(r"[.,]", "", m.group(0))
            for f in fmts:
                try: return datetime.strptime(s, f).date().isoformat()
                except ValueError: pass
    return None

async def _local(data, mime):
    """No-key fallback: reads the PDF text layer and uses pattern matching. Clean text PDFs only."""
    if mime != "application/pdf": raise RuntimeError("Local mode reads text PDFs only. Add a free GEMINI_API_KEY to read photos and scans.")
    text = "\n".join(p.extract_text() or "" for p in PdfReader(io.BytesIO(data)).pages)
    if len(text.strip()) < 20: raise RuntimeError("This PDF has no text layer (it is a scan). Add a free GEMINI_API_KEY to read scans.")
    lines, ev = [l.strip() for l in text.splitlines() if l.strip()], {}
    def g(k, p, f=re.I | re.M):
        m = re.search(p, text, f)
        if m: ev[k] = m.group(0).strip()[:80]
        return m
    num = lambda s: float(s.replace(",", ""))
    m = g("invoice_number", r"invoice\s*(?:no\.?|number|#)?[:\s]*([A-Z0-9][\w-]*\d[\w-]*)"); inv = m.group(1) if m else None
    email_match = EMAIL_PATTERN.search(text)
    vendor_email = _normalize_email(email_match.group(0)) if email_match else None
    m = re.search(r"date[:\s]*(.+)", text, re.I); d = (_date(m.group(1)) if m else None) or _date(text)
    m = g("po_reference", r"\bPO[-\s]?(\d{3,})", 0); po = "PO-" + m.group(1) if m else None
    tots = list(re.finditer(r"\btotal[^\d\n]{0,20}([\d,]+(?:\.\d{2})?)", text, re.I))
    total = num(tots[-1].group(1)) if tots else None
    if tots: ev["total"] = tots[-1].group(0).strip()[:80]
    m = g("subtotal", r"subtotal[^\d\n]{0,10}([\d,]+\.\d{2})"); sub = num(m.group(1)) if m else None
    incl = bool(re.search(r"includ\w*\s+(?:tax|gst|vat)|inclusive of", text, re.I)); tax = None
    if not incl:
        m = g("tax", r"(?:sales tax|gst|vat|tax)[^\d\n]{0,12}(?:\d+(?:\.\d+)?%[:\s]*)?([\d,]+\.\d{2})"); tax = num(m.group(1)) if m else None
    if sub is None and total is not None: sub = total if incl else round(total - (tax or 0), 2)
    m = g("bank_last4", r"(?:account|a/c|iban)[^\n]*?(\d{4})\s*$"); bank = m.group(1) if m else None
    c = lambda v: 0.85 if v not in (None, "") else 0.0
    return dict(vendor_name=lines[0], vendor_email=vendor_email, invoice_number=inv, invoice_date=d, po_reference=po, currency="USD", line_items=[],
                subtotal=sub, tax=tax, tax_included=incl, total=total, bank_last4=bank,
                confidence=dict(vendor_name=.85, vendor_email=.99 if vendor_email else 0.0,
                                invoice_number=c(inv), invoice_date=c(d), total=c(total)), evidence=ev)


def _number(value):
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        text = str(value)
    elif isinstance(value, str):
        text = value.strip().replace(",", "").replace("$", "").replace("€", "").replace("£", "")
        if text.startswith("(") and text.endswith(")"):
            text = "-" + text[1:-1]
    else:
        return None
    try:
        number = Decimal(text)
        converted = float(number)
    except (InvalidOperation, ValueError, OverflowError):
        return None
    return converted if number.is_finite() and math.isfinite(converted) else None


def _text(value):
    return value.strip() or None if isinstance(value, str) else None


def _normalize_email(value):
    if not isinstance(value, str):
        return None
    address = value.strip()
    if not EMAIL_PATTERN.fullmatch(address):
        return None
    return address.lower()


def _normalise_date(value):
    if not isinstance(value, str) or not value.strip():
        return None
    value = value.strip()
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return _date(value)


def _normalise_result(result):
    line_items = result.get("line_items")
    if isinstance(line_items, list):
        normalized_items = []
        for item in line_items:
            if not isinstance(item, dict):
                normalized_items.append({
                    "description": None, "quantity": None, "unit_price": None, "amount": None
                })
                continue
            normalized_items.append({
                "description": _text(item.get("description")),
                "quantity": _number(item.get("quantity")),
                "unit_price": _number(item.get("unit_price")),
                "amount": _number(item.get("amount")),
            })
        line_items = normalized_items
    else:
        line_items = None

    confidence = result.get("confidence")
    if not isinstance(confidence, dict):
        confidence = {}
    confidence = {
        key: (value if value is not None and 0 <= value <= 1 else None)
        for key in ("vendor_name", "vendor_email", "invoice_number", "invoice_date", "total")
        for value in [_number(confidence.get(key))]
    }

    evidence = result.get("evidence")
    if not isinstance(evidence, dict):
        evidence = {}
    evidence = {key: _text(value) for key, value in evidence.items() if isinstance(key, str)}

    bank_last4 = result.get("bank_last4")
    if isinstance(bank_last4, (int, float)) and not isinstance(bank_last4, bool):
        bank_last4 = str(bank_last4)
    else:
        bank_last4 = _text(bank_last4)

    raw_vendor_email = result.get("vendor_email")
    normalized_vendor_email = _normalize_email(raw_vendor_email)
    return {
        "vendor_name": _text(result.get("vendor_name")),
        "vendor_email": normalized_vendor_email,
        "vendor_email_invalid": bool(raw_vendor_email and not normalized_vendor_email),
        "invoice_number": _text(result.get("invoice_number")),
        "invoice_date": _normalise_date(result.get("invoice_date")),
        "po_reference": _text(result.get("po_reference")),
        "currency": _text(result.get("currency")),
        "line_items": line_items,
        "subtotal": _number(result.get("subtotal")),
        "tax": _number(result.get("tax")),
        "tax_included": result.get("tax_included") if isinstance(result.get("tax_included"), bool) else None,
        "total": _number(result.get("total")),
        "bank_last4": bank_last4,
        "confidence": confidence,
        "evidence": evidence,
    }


_EXTRACTORS = {
    "anthropic": _claude,
    "gemini": _gemini,
    "groq": _groq,
    "local": _local,
}


def _fallback_chain(mime):
    primary = backend()
    chain = [primary]
    for name, env_key in (
        ("groq", "GROQ_API_KEY"),
        ("gemini", "GEMINI_API_KEY"),
        ("anthropic", "ANTHROPIC_API_KEY"),
    ):
        if os.getenv(env_key) and name not in chain:
            chain.append(name)
    if mime == "application/pdf" and "local" not in chain:
        chain.append("local")
    return chain


def _extract_timeout():
    try:
        timeout = float(os.getenv("EXTRACT_TIMEOUT", "90"))
    except ValueError as exc:
        raise ExtractionError("EXTRACT_TIMEOUT must be a positive number of seconds") from exc
    if not math.isfinite(timeout) or timeout <= 0:
        raise ExtractionError("EXTRACT_TIMEOUT must be a positive number of seconds")
    return timeout


async def extract(data, mime):
    timeout = _extract_timeout()
    errors = []
    for name in _fallback_chain(mime):
        for attempt in range(2):
            try:
                result = await asyncio.wait_for(_EXTRACTORS[name](data, mime), timeout=timeout)
                if not isinstance(result, dict):
                    raise InvalidExtractorJSON("Extractor result must be a JSON object")
                return _normalise_result(result), name
            except InvalidExtractorJSON as exc:
                if attempt == 0:
                    continue
                errors.append(f"{name}: invalid JSON after retry ({exc})")
                break
            except asyncio.TimeoutError:
                errors.append(f"{name}: timed out after {timeout:g} seconds")
                break
            except Exception as exc:
                errors.append(f"{name}: {type(exc).__name__}: {exc}")
                break
    raise ExtractionError("No extractor succeeded. " + "; ".join(errors))
