"""Makes test invoices in ./test_invoices (needs: pip install -r requirements-dev.txt)."""
import os
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

OUTPUT_DIR = os.getenv("TEST_INVOICE_DIR", "test_invoices")

def make(name, lines):
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    c = canvas.Canvas(os.path.join(OUTPUT_DIR, name), pagesize=A4)
    y = 800
    for size, text in lines:
        c.setFont("Helvetica-Bold" if size > 11 else "Helvetica", size)
        c.drawString(50, y, text)
        y -= size + 8
    c.save()

make("1_clean_northwind.pdf", [
    (18, "Northwind Office Supplies"), (11, "INVOICE  INV-5001"), (11, "Date: 02 Nov 2026"),
    (11, "Customer PO: PO-1001"), (11, ""), (11, "A4 paper reams    100 x 12.50 = 1,250.00"),
    (11, "Toner cartridges    5 x 85.00 =   425.00"), (11, "Subtotal: 1,675.00"),
    (11, "Sales tax 8%: 134.00"), (13, "TOTAL DUE: 1,809.00"), (11, "Pay to account ending 7710")])

make("2_bundled_tax_apex.pdf", [
    (18, "Apex Logistics"), (11, "Tax Invoice AL-9001"), (11, "Invoice date: 04/11/2026"),
    (11, "Re: freight lot 4, Mumbai to Delhi (no PO number quoted)"), (11, ""),
    (11, "Freight services (prices include tax)   3,540.00"), (13, "Total payable: 3,540.00"),
    (11, "Bank account ending 3021")])

make("3_missing_total_meridian.pdf", [
    (18, "Meridian Print & Packaging"), (11, "Invoice date: 04 Nov 2026"), (11, ""),
    (11, "Retail boxes, 5,000 units"), (11, "Amount to be confirmed"), (11, "Bank account ending 1204")])

test_vendor_email = os.getenv("TEST_VENDOR_EMAIL", "vendor@example.com")
make("5_missing_with_contact_email.pdf", [
    (18, "Meridian Print & Packaging"), (11, "Invoice date: 04 Nov 2026"),
    (11, "Accounts: " + test_vendor_email), (11, ""),
    (11, "Retail boxes, 5,000 units"), (11, "Amount to be confirmed"),
    (11, "Bank account ending 1204")])
print("Created 4 PDFs in test_invoices/")

# A photo-style "scan" (image, no text layer) to test the Gemini image path
from PIL import Image, ImageDraw, ImageFont
try: f = ImageFont.load_default(size=26)
except TypeError: f = ImageFont.load_default()
img = Image.new("RGB", (900, 700), "white"); d = ImageDraw.Draw(img)
for i, s in enumerate(["Lakeshore Facilities", "Invoice LF-4410   Date 03 Nov 2026", "Quarterly HVAC maintenance   1,800.00",
                       "Tax 8%   144.00", "TOTAL 1,944.00", "Pay to account ending 8836"]):
    d.text((60, 70 + i * 70), s, fill=(30, 30, 30), font=f)
img.rotate(1.5, fillcolor="white").save(os.path.join(OUTPUT_DIR, "4_scanned_photo_lakeshore.png"))
print("Created a scanned-style PNG too")
