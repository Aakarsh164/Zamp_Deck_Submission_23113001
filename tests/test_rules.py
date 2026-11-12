import sqlite3
import time
import unittest

import rules
from samples import inv


SCHEMA = """
create table runs(
    id text primary key, created_at real, filename text, source text, file_hash text,
    vendor_id text, po_id text, invoice_number text, invoice_date text, total real,
    action text, recommendation text, status text, decision text
)
"""


class DecisionRulesTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.execute(SCHEMA)
        self.invoice = inv(
            "Northwind Office Supplies", "NW-1001", "2026-11-02", "PO-1001",
            [("Paper", 10, 10)], 0, "7710",
        )

    def tearDown(self):
        self.connection.close()

    def add_prior(self, invoice_number="NW-0999", total=100, po_id="PO-1001",
                  file_hash="prior-hash"):
        self.connection.execute(
            "insert into runs values(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("prior", time.time(), "prior.pdf", "upload", file_hash, "V001",
             po_id, invoice_number, "2026-11-01", total, "REVIEW", "approve",
             "approved", "{}"),
        )
        self.connection.commit()

    def decide(self, invoice=None, trust=0, file_hash="new-hash"):
        invoice = invoice or self.invoice
        vid, vendor_score = rules.match_vendor(invoice.get("vendor_name"))
        po, method, po_confidence = rules.resolve_po(invoice, vid, self.connection)
        checks = [
            rules.r1(invoice),
            rules.confidence_check(invoice),
            rules.vendor_check(vid, vendor_score, invoice.get("vendor_name")),
            *rules.po_checks(invoice, vid, po, method),
            rules.dup_check(invoice, vid, file_hash, self.connection),
            rules.bank_check(invoice, vid),
            rules.arithmetic(invoice),
        ]
        return rules.decide(
            invoice, vid, vendor_score, po, method, po_confidence, checks, trust
        )

    def test_confirmed_duplicate_is_auto_rejected_with_explanation(self):
        self.add_prior(invoice_number="NW-1001", total=100)

        decision = self.decide()

        self.assertEqual(decision["action"], "AUTO_REJECT")
        self.assertIn("confirmed duplicate", decision["decision_explanation"].lower())
        self.assertTrue(any("Automatically rejected" in r for r in decision["reasons"]))

    def test_same_number_with_changed_amount_escalates(self):
        self.add_prior(invoice_number="NW-1001", total=90)

        decision = self.decide()

        self.assertEqual(decision["action"], "ESCALATE")
        self.assertNotEqual(decision["action"], "AUTO_REJECT")

    def test_same_number_with_unavailable_amount_escalates(self):
        self.add_prior(invoice_number="NW-1001", total=100)
        invoice = dict(self.invoice, total=None)

        decision = self.decide(invoice)

        self.assertEqual(decision["action"], "ESCALATE")

    def test_similar_invoice_number_and_po_with_changed_amount_escalates(self):
        self.add_prior(invoice_number="NW-1002", total=100)
        invoice = dict(self.invoice, invoice_number="NW-1001", total=105)

        decision = self.decide(invoice)

        self.assertEqual(decision["action"], "ESCALATE")
        self.assertNotEqual(decision["action"], "AUTO_REJECT")

    def test_same_uploaded_file_is_auto_rejected(self):
        self.add_prior(invoice_number="NW-0999", total=100, file_hash="same-hash")

        decision = self.decide(file_hash="same-hash")

        self.assertEqual(decision["action"], "AUTO_REJECT")

    def test_over_po_invoice_stays_in_human_review(self):
        invoice = inv(
            "Meridian Print & Packaging", "MP-88", "2026-11-01", "PO-1004",
            [("Boxes", 1, 20000)], 0, "1204",
        )

        decision = self.decide(invoice)

        self.assertEqual(decision["action"], "REVIEW")
        self.assertTrue(any("exceeds remaining" in r for r in decision["reasons"]))

    def test_low_extraction_confidence_requires_human_review(self):
        invoice = dict(self.invoice)
        invoice["confidence"] = dict(self.invoice["confidence"], total=0.65)

        decision = self.decide(invoice, trust=5)

        self.assertEqual(decision["action"], "REVIEW")
        self.assertTrue(any("human must verify" in c["detail"] for c in decision["checks"]))

    def test_medium_extraction_confidence_requires_additional_verification(self):
        invoice = dict(self.invoice)
        invoice["confidence"] = dict(self.invoice["confidence"], total=0.80)

        decision = self.decide(invoice, trust=5)

        self.assertEqual(decision["action"], "REVIEW")
        self.assertTrue(any("additional human verification" in c["detail"] for c in decision["checks"]))

    def test_malformed_confidence_cannot_auto_approve(self):
        invoice = dict(self.invoice, confidence="invalid")

        decision = self.decide(invoice, trust=5)

        self.assertEqual(decision["action"], "REVIEW")
        self.assertEqual(decision["confidence"], 0)

    def test_clean_trusted_invoice_can_still_auto_approve(self):
        invoice = dict(self.invoice)
        invoice["confidence"] = {
            "vendor_name": 0.98,
            "invoice_number": 0.97,
            "invoice_date": 0.95,
            "total": 0.98,
        }

        decision = self.decide(invoice, trust=5)

        self.assertEqual(decision["action"], "AUTO_APPROVE")
        self.assertTrue(decision["decision_explanation"])

    def test_registered_sender_domain_passes(self):
        check = rules.sender_domain_check(
            "Northwind AP <billing@northwind.example.com>", "V001"
        )

        self.assertEqual(check["status"], "pass")

    def test_lookalike_sender_domain_with_different_tld_is_a_hard_stop(self):
        check = rules.sender_domain_check(
            "billing@northwind.example.net", "V001"
        )

        self.assertEqual(check["status"], "fail")
        self.assertTrue(check["hard"])
        self.assertIn("lookalike", check["detail"])
        self.assertTrue(check["fix"])

    def test_sender_domain_edit_distance_two_or_less_is_a_hard_stop(self):
        check = rules.sender_domain_check(
            "billing@nortwind.example.com", "V001"
        )

        self.assertEqual(check["status"], "fail")
        self.assertTrue(check["hard"])
        self.assertIn("lookalike", check["detail"])

    def test_uploaded_invoice_sender_check_is_neutral(self):
        check = rules.sender_domain_check(None, "V001")

        self.assertEqual(check["status"], "not_applicable")
        self.assertIn("Not applicable", check["detail"])


if __name__ == "__main__":
    unittest.main()
