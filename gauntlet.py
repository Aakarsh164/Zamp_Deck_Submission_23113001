"""Hostile invoice cases run on scratch in-memory DBs; live data is never touched."""
import sqlite3, time, json, hashlib
import rules
from samples import inv

SCHEMA = """create table runs(id text primary key, created_at real, filename text, source text, file_hash text,
vendor_id text, po_id text, invoice_number text, invoice_date text, total real, action text, recommendation text,
status text, decision text)"""
N = "Northwind Office Supplies"
P = "paper"

def analyze(ex, c, pts, fh, sender=None):
    vid, vs = rules.match_vendor(ex.get("vendor_name"))
    po, m, pc = rules.resolve_po(ex, vid, c)
    checks = [rules.r1(ex), rules.confidence_check(ex), rules.vendor_check(vid, vs, ex.get("vendor_name")),
              rules.sender_domain_check(sender, vid)] + rules.po_checks(ex, vid, po, m) + \
             [rules.dup_check(ex, vid, fh, c), rules.bank_check(ex, vid), rules.arithmetic(ex)]
    return rules.decide(ex, vid, vs, po, m, pc, checks, pts)

# (label, expected action, vendor trust points, invoice, prior approved invoices, is_clean, note if missed)
CASES = [
 ("Clean invoice, new vendor", "REVIEW", 0, inv(N, "INV-3001", "2026-11-02", "PO-1001", [(P, 100, 12.5)], 100, "7710"), [], True, ""),
 ("Clean invoice, trusted vendor", "AUTO_APPROVE", 5, inv(N, "INV-3002", "2026-11-02", "PO-1001", [(P, 100, 12.5)], 100, "7710"), [], True, ""),
 ("Slightly over PO inside tolerance, trusted vendor (near-miss still escalates)", "REVIEW", 5,
  inv(N, "INV-3003", "2026-11-02", "PO-1008", [("Chairs", 1, 1220)], 0, "7710"), [], True, ""),
 ("Over tolerance (8% above PO)", "REVIEW", 0, inv("Meridian Print & Packaging", "MP-88", "2026-11-01", "PO-1004", [("Boxes", 1, 20000)], 0, "1204"), [], False, ""),
 ("Split PO: cumulative invoices overrun the PO", "REVIEW", 0, inv("Apex Logistics", "AL-8001", "2026-11-04", "PO-1002", [("Freight", 1, 2000)], 0, "3021"),
  [("V002", "PO-1002", "AL-7000", 5500, "2026-10-20")], False, ""),
 ("Exact duplicate", "AUTO_REJECT", 0, inv(N, "INV-3010", "2026-11-02", "PO-1001", [(P, 100, 12.5)], 100, "7710"),
  [("V001", "PO-1001", "INV-3010", 1350, "2026-11-02")], False, ""),
 ("Near-duplicate (invoice number tweaked)", "ESCALATE", 0, inv(N, "INV-3O10", "2026-11-03", "PO-1001", [(P, 100, 12.5)], 100, "7710"),
  [("V001", "PO-1001", "INV-3010", 1350, "2026-11-02")], False, ""),
 ("Changed bank details, trusted vendor", "ESCALATE", 5, inv("BrightCloud SaaS", "BC-1001", "2026-11-01", "PO-1003", [("Licence", 1, 2400)], 0, "1111"), [], False, ""),
 ("Missing invoice number and total", "HOLD_FOR_INFO", 0, inv("Lakeshore Facilities", None, "2026-11-03", "PO-1005", [("Cleaning", 1, 1500)], 0, "8836", total=None), [], False, ""),
 ("Unknown vendor", "ESCALATE", 0, inv("Quickfix Services", "QF-17", "2026-11-02", None, [("Repairs", 1, 900)], 0, "0000"), [], False, ""),
 ("Total does not match line items", "REVIEW", 0, inv("Orion Consulting", "OC-12", "2026-11-02", "PO-1006", [("Consulting", 10, 500)], 0, "6643", total=5600), [], False, ""),
 ("Resubmitted with a 5% corrected total", "ESCALATE", 0, inv(N, "INV-3020", "2026-11-03", "PO-1001", [(P, 100, 13.2)], 100, "7710"),
  [("V001", "PO-1001", "INV-3019", 1350, "2026-11-02")], False,
  "Same vendor, same PO and an invoice number one character apart, but the amount differs by 5%, outside my 0.5% near-duplicate window. A human would query it; my rule does not."),
 ("Lookalike vendor name with the real vendor's bank digits", "ESCALATE", 5,
  inv("Northwind Office Supply Ltd", "NW-3101", "2026-11-06", "PO-1001", [(P, 100, 12.5)], 100, "7710"), [], False,
  "This case has no sender-domain evidence, as with a file upload; fuzzy vendor-name matching accepts the near match, and matching bank digits do not distinguish an impostor."),
 ("Line item priced 40% above the usual unit price", "REVIEW", 5,
  inv(N, "NW-3102", "2026-11-07", "PO-1001", [("A4 paper reams", 100, 17.5)], 0, "7710"), [], False,
  "The rules check invoice arithmetic and the PO total, but do not compare vendor/item unit prices with price history or a threshold. A consistent high line price can therefore pass."),
 ("Invoice date far in the future", "REVIEW", 5,
  inv(N, "NW-3103", "2099-12-31", "PO-1001", [(P, 100, 12.5)], 100, "7710"), [], False,
  "The PO master has no creation date, and the rules only require an invoice date to be present; they do not compare it with PO existence or a plausibility window."),
]
SENDER_CASE = (
    "Lookalike sender domain with a different TLD",
    "ESCALATE",
    5,
    inv(N, "NW-3104", "2026-11-08", "PO-1001", [(P, 100, 12.5)], 100, "7710"),
    [],
    False,
    "The sender domain imitates Northwind's configured domain but uses a different top-level domain; the sender-domain rule should hard-stop it.",
    "billing@northwind.example.net",
)

def run():
    out = []
    case_specs = [(*case, None) for case in CASES] + [SENDER_CASE]
    for label, exp, pts, ex, pri, clean, note, sender in case_specs:
        c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row; c.execute(SCHEMA)
        for vid, po, num, tot, date in pri:
            c.execute("insert into runs values(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (num, time.time() - 5 * 86400, "prior", "email", "h-" + num, vid, po, num, date, tot, "REVIEW", "approve", "approved", "{}"))
        fh = hashlib.sha256(json.dumps(ex, sort_keys=True).encode()).hexdigest()
        d = analyze(ex, c, pts, fh, sender)
        got = d["action"]
        out.append(dict(label=label, expected=exp, got=got, ok=got == exp, clean=clean,
                        note=note or ("The current checks did not detect the hostile condition."
                                      if got != exp else "")))
    misses = [
        {key: item[key] for key in ("label", "expected", "got", "note")}
        for item in out if not item["ok"]
    ]
    return dict(cases=out, misses=misses, total=len(out),
                correct=sum(o["ok"] for o in out),
                false_escalations=sum(
                    1 for o in out if o["clean"] and o["got"] in ("ESCALATE", "HOLD_FOR_INFO")
                ))
