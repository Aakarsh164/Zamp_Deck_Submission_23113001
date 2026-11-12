def inv(vendor, num, date, po, items, tax, bank, **over):
    li = [dict(description=d, quantity=q, unit_price=p, amount=round(q * p, 2)) for d, q, p in items]
    sub = round(sum(i["amount"] for i in li), 2)
    d = dict(vendor_name=vendor, invoice_number=num, invoice_date=date, po_reference=po, currency="USD",
             line_items=li, subtotal=sub, tax=tax, tax_included=False, total=round(sub + tax, 2),
             bank_last4=bank,
             confidence=dict(vendor_name=.98, invoice_number=.97, invoice_date=.95, total=.98), evidence={})
    d.update(over)
    return d

SAMPLES = {
    "clean": dict(label="Clean invoice (Northwind)", data=inv(
        "Northwind Office Supplies", "INV-2041", "2026-11-02", "PO-1001",
        [("A4 paper reams", 200, 12.5), ("Toner cartridges", 20, 85)], 336, "7710")),
    "dupe": dict(label="Near-duplicate (run the clean one first)", data=inv(
        "Northwind Office Supplies", "INV-2O41", "2026-11-04", "PO-1001",
        [("A4 paper reams", 200, 12.5), ("Toner cartridges", 20, 85)], 336, "7710")),
    "split": dict(label="Split PO, part 2 (Apex)", data=inv(
        "Apex Logistics", "AL-7788", "2026-11-03", "PO-1002", [("Freight lot 2", 1, 5500)], 0, "3021")),
    "over": dict(label="Over PO balance (Apex)", data=inv(
        "Apex Logistics", "AL-7791", "2026-11-05", "PO-1002", [("Freight lot 3", 1, 7300)], 0, "3021")),
    "bank": dict(label="Changed bank details (BrightCloud)", data=inv(
        "BrightCloud SaaS", "BC-0912", "2026-11-01", "PO-1003", [("Annual platform licence", 1, 2400)], 0, "9999")),
    "missing": dict(label="Missing invoice number and total (Meridian)", data=inv(
        "Meridian Print & Packaging", None, "2026-11-04", None, [("Retail boxes", 5000, 3.2)], 0, "1204",
        total=None, confidence=dict(vendor_name=.95, invoice_number=.2, invoice_date=.9, total=.3))),
    "implied": dict(label="No PO reference (Lakeshore)", data=inv(
        "Lakeshore Facilities", "LF-3310", "2026-11-03", None, [("Quarterly HVAC maintenance", 1, 1800)], 144, "8836")),
}
