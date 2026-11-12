# Mock procurement data. Edit freely.
VENDORS = {
    "V001": dict(name="Northwind Office Supplies", email="", tol=0.02, bank="7710", active=True,
                 domains=["northwind.example.com"]),
    "V002": dict(name="Apex Logistics", email="", tol=0.03, bank="3021", active=True,
                 domains=["apex.example.com"]),
    "V003": dict(name="BrightCloud SaaS", email="", tol=0.00, bank="5582", active=True,
                 domains=["brightcloud.example.com"]),
    "V004": dict(name="Meridian Print & Packaging", email="", tol=0.05, bank="1204", active=True,
                 domains=["meridian.example.com"]),
    "V005": dict(name="Lakeshore Facilities", email="", tol=0.02, bank="8836", active=True,
                 domains=["lakeshore.example.com"]),
    "V006": dict(name="Orion Consulting", email="", tol=0.01, bank="6643", active=True,
                 domains=["orion.example.com"]),
}
# invoiced = amount already billed before this system started

POS = {
    "PO-1001": dict(vendor="V001", amount=4800, invoiced=0),
    "PO-1008": dict(vendor="V001", amount=1200, invoiced=0),
    "PO-1002": dict(vendor="V002", amount=12000, invoiced=5000),
    "PO-1007": dict(vendor="V002", amount=3500, invoiced=3500),
    "PO-1003": dict(vendor="V003", amount=2400, invoiced=0),
    "PO-1004": dict(vendor="V004", amount=18500, invoiced=0),
    "PO-1005": dict(vendor="V005", amount=6000, invoiced=0),
    "PO-1006": dict(vendor="V006", amount=25000, invoiced=0),
}
DUP_WINDOW_DAYS = 90
NEAR_DUP_DAYS = 30
AUTO_CAP = 10000        # never auto-approve above this, whatever the trust
TRUST_THRESHOLD = 5     # points needed for auto-approval of clean invoices
AUTO_APPROVE_CONFIDENCE = 0.90
HUMAN_REVIEW_CONFIDENCE = 0.70
