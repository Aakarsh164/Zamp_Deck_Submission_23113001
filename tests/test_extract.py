import asyncio
import os
import runpy
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import extract


ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIR = ROOT / "test_invoices" / "extractor-reliability"
FIXTURE_SCRIPT = ROOT / "make_test_pdfs.py"


class ExtractionTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
        with patch.dict(os.environ, {"TEST_INVOICE_DIR": str(FIXTURE_DIR)}):
            runpy.run_path(str(FIXTURE_SCRIPT))

    def payload(self):
        return {
            "vendor_name": "Northwind Office Supplies",
            "invoice_number": "INV-5001",
            "invoice_date": "2026-11-02",
            "po_reference": "PO-1001",
            "currency": "USD",
            "line_items": [{"description": "Paper", "quantity": "100", "unit_price": "$12.50", "amount": "1,250.00"}],
            "subtotal": "1,675.00",
            "tax": "134.00",
            "tax_included": False,
            "total": "$1,809.00",
            "bank_last4": "7710",
            "confidence": {"vendor_name": "0.95", "total": 1.2},
            "evidence": {"total": "TOTAL DUE: $1,809.00"},
        }

    def test_normalizes_numbers_dates_and_invalid_fields(self):
        normalized = extract._normalise_result({
            **self.payload(),
            "vendor_email": "Billing@Vendor.Example",
            "invoice_date": "not a date",
            "tax": "not a number",
            "line_items": [{"description": 4, "quantity": "five", "unit_price": "$12.50", "amount": None}],
        })

        self.assertIsNone(normalized["invoice_date"])
        self.assertIsNone(normalized["tax"])
        self.assertEqual(normalized["total"], 1809.0)
        self.assertEqual(normalized["vendor_email"], "billing@vendor.example")
        self.assertEqual(normalized["line_items"][0], {
            "description": None, "quantity": None, "unit_price": 12.5, "amount": None,
        })
        self.assertEqual(normalized["confidence"], {
            "vendor_name": 0.95, "vendor_email": None, "invoice_number": None,
            "invoice_date": None, "total": None,
        })

    async def test_local_extractor_reads_generated_test_invoice(self):
        path = FIXTURE_DIR / "1_clean_northwind.pdf"
        with patch.dict(os.environ, {"EXTRACTOR": "local"}):
            result, extractor_name = await extract.extract(path.read_bytes(), "application/pdf")

        self.assertEqual(extractor_name, "local")
        self.assertEqual(result["vendor_name"], "Northwind Office Supplies")
        self.assertEqual(result["invoice_number"], "INV-5001")
        self.assertEqual(result["invoice_date"], "2026-11-02")
        self.assertEqual(result["po_reference"], "PO-1001")
        self.assertEqual(result["total"], 1809.0)
        self.assertEqual(result["bank_last4"], "7710")

    async def test_local_extractor_reads_the_printed_contact_email(self):
        path = FIXTURE_DIR / "5_missing_with_contact_email.pdf"
        with patch.dict(os.environ, {
            "EXTRACTOR": "local", "TEST_VENDOR_EMAIL": "Billing@Meridian.Example",
            "TEST_INVOICE_DIR": str(FIXTURE_DIR),
        }):
            runpy.run_path(str(FIXTURE_SCRIPT))
            result, extractor_name = await extract.extract(path.read_bytes(), "application/pdf")

        self.assertEqual(extractor_name, "local")
        self.assertEqual(result["vendor_email"], "billing@meridian.example")
        self.assertIsNone(result["invoice_number"])
        self.assertIsNone(result["total"])

    def test_invalid_vendor_email_is_normalized_to_null(self):
        normalized = extract._normalise_result({
            **self.payload(), "vendor_email": "accounts at meridian example",
        })

        self.assertIsNone(normalized["vendor_email"])
        self.assertTrue(normalized["vendor_email_invalid"])

    def test_normalizes_and_preserves_vendor_email_confidence(self):
        normalized = extract._normalise_result({
            **self.payload(),
            "vendor_email": "Billing@Meridian.Example.Com",
            "confidence": {"vendor_name": 0.97, "vendor_email": "0.96"},
        })

        self.assertEqual(normalized["vendor_email"], "billing@meridian.example.com")
        self.assertEqual(normalized["confidence"]["vendor_email"], 0.96)
        self.assertFalse(normalized["vendor_email_invalid"])

    async def test_retries_once_when_provider_returns_invalid_json(self):
        provider = AsyncMock(side_effect=[
            extract.InvalidExtractorJSON("bad JSON"),
            self.payload(),
        ])
        with patch.dict(os.environ, {"EXTRACTOR": "gemini", "GEMINI_API_KEY": "fixture"}), \
             patch.dict(extract._EXTRACTORS, {"gemini": provider}):
            result, extractor_name = await extract.extract(b"image", "image/png")

        self.assertEqual(extractor_name, "gemini")
        self.assertEqual(result["total"], 1809.0)
        self.assertEqual(provider.await_count, 2)

    async def test_rate_limit_falls_back_to_next_configured_provider(self):
        primary = AsyncMock(side_effect=RuntimeError("rate limit"))
        fallback = AsyncMock(return_value=self.payload())
        with patch.dict(os.environ, {
            "EXTRACTOR": "gemini", "GEMINI_API_KEY": "fixture", "GROQ_API_KEY": "fixture",
        }), patch.dict(extract._EXTRACTORS, {"gemini": primary, "groq": fallback}):
            result, extractor_name = await extract.extract(b"image", "image/png")

        self.assertEqual(extractor_name, "groq")
        self.assertEqual(result["vendor_name"], "Northwind Office Supplies")
        primary.assert_awaited_once()
        fallback.assert_awaited_once()

    async def test_timeout_is_reported_as_extraction_error(self):
        async def slow_provider(data, mime):
            await asyncio.sleep(0.1)
            return self.payload()

        with patch.dict(os.environ, {
            "EXTRACTOR": "gemini", "GEMINI_API_KEY": "fixture", "EXTRACT_TIMEOUT": "0.01",
        }), patch.dict(extract._EXTRACTORS, {"gemini": slow_provider}):
            with self.assertRaisesRegex(extract.ExtractionError, "timed out"):
                await extract.extract(b"image", "image/png")


if __name__ == "__main__":
    unittest.main()
