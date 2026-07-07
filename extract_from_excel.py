"""Regenerate data/*.csv from a Margin_Check workbook.

Usage:
    pip install openpyxl
    python extract_from_excel.py path/to/Margin_Check_V5.xlsx

Reads:
    "AMZ Fees"            -> data/products.csv   (Amazon FBA fee preview + COGS)
    "AMZ Marketing data"  -> data/marketing_spend.csv (ad spend per unit)
                             + data/metadata.json (spend period, from export dates)
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import re
import sys
from pathlib import Path

import openpyxl

SPEND_WINDOW_DAYS = 90  # Amazon Ads exports are pulled as trailing-90-day reports

DATA_DIR = Path(__file__).resolve().parent / "data"

PRODUCT_COLS = [
    "sku", "asin", "amazon-store", "product-name", "brand", "your-price",
    "sales-price", "currency", "estimated-referral-fee-per-unit",
    "estimated-variable-closing-fee", "expected-domestic-fulfilment-fee-per-unit",
    "COGS",
]


def _num(value):
    """Some exports use comma decimal separators inside text cells."""
    if value in (None, ""):
        return ""
    try:
        return float(str(value).replace(",", "."))
    except ValueError:
        return ""


def main(xlsx_path: str) -> None:
    wb = openpyxl.load_workbook(xlsx_path, data_only=True)
    DATA_DIR.mkdir(exist_ok=True)

    ws = wb["AMZ Fees"]
    rows = list(ws.values)
    header = list(rows[0])
    idx = {h: header.index(h) for h in PRODUCT_COLS}
    out = [[r[idx[h]] for h in PRODUCT_COLS] for r in rows[1:] if r[idx["sku"]]]
    with open(DATA_DIR / "products.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(PRODUCT_COLS)
        w.writerows(out)
    print(f"products.csv: {len(out)} rows")

    ws = wb["AMZ Marketing data"]
    rows = list(ws.values)
    header = list(rows[0])
    i_sku = header.index("SKU")
    i_ctry = header.index("Country")
    i_spu = header.index("Spend per Unit")
    i_spend = header.index("Spend(EUR)")
    i_units = header.index("Amazon Sale.Units ordered")
    i_src = header.index("Source.Name")
    i_mod = header.index("Date modified")
    seen = {}
    export_date = None
    window_days = SPEND_WINDOW_DAYS
    for r in rows[1:]:
        if r[i_sku] is None or r[i_ctry] is None:
            continue
        seen[(r[i_ctry], r[i_sku])] = (
            _num(r[i_spu]), _num(r[i_spend]), _num(r[i_units]),
        )
        if isinstance(r[i_mod], dt.datetime):
            d = r[i_mod].date()
            export_date = d if export_date is None else max(export_date, d)
        # e.g. "UK PPC 90 days.csv" encodes the report window
        m = re.search(r"(\d+)\s*days", str(r[i_src] or ""), re.IGNORECASE)
        if m:
            window_days = int(m.group(1))
    with open(DATA_DIR / "marketing_spend.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["country", "sku", "spend_per_unit", "ad_spend_eur", "units_ordered"])
        for (c, s), vals in sorted(seen.items()):
            w.writerow([c, s, *vals])
    print(f"marketing_spend.csv: {len(seen)} rows")

    meta = {
        "marketing_spend": {
            "source": "Amazon Ads PPC exports (Sponsored Products), per marketplace",
            "window": f"trailing {window_days} days",
            "export_date": export_date.isoformat() if export_date else None,
            "period_start": (
                (export_date - dt.timedelta(days=window_days)).isoformat()
                if export_date else None
            ),
            "period_end": export_date.isoformat() if export_date else None,
            "note": (
                "Spend per unit = total ad spend / total units ordered over the "
                "window. Window inferred from the export file names; export date "
                "from the files' 'Date modified'."
            ),
        }
    }
    (DATA_DIR / "metadata.json").write_text(json.dumps(meta, indent=2) + "\n")
    print(f"metadata.json: window={window_days}d, exported {export_date}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(sys.argv[1])
