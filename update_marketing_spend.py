"""Refresh data/marketing_spend.csv from the Novadata daily margin export.

Runs daily via .github/workflows/update_marketing_spend.yml (and on demand
with workflow_dispatch). Downloads the same rolling-12-month export the
Margin-Analytics repo uses, takes the trailing window, and recomputes ad
spend per unit per SKU x marketplace. Sales units come along as the
denominator, so both ad spend and sales volumes refresh automatically —
only the FBA fee report stays a manual upload.

Usage:
    python update_marketing_spend.py                  # download from Novadata
    python update_marketing_spend.py --from-file f.csv.gz   # offline/testing
"""
from __future__ import annotations

import io
import json
import sys
from datetime import timedelta
from pathlib import Path

import pandas as pd
import requests

DATA_DIR = Path(__file__).resolve().parent / "data"

# Novadata daily margin export (rolling 12 months, daily granularity).
DOWNLOAD_URL = (
    "https://app.novadata.io/resources/data-export-download/"
    "d664ab4b-047a-471b-a993-27dd42d0a91b"
)
WINDOW_DAYS = 90  # matches the trailing-90-day basis of the original PPC exports

MARKETPLACE_TO_COUNTRY = {
    "amazon.de": "DE",
    "amazon.it": "IT",
    "amazon.es": "ES",
    "amazon.fr": "FR",
    "amazon.co.uk": "GB",
    "amazon.ie": "IE",
    "amazon.nl": "NL",
    "amazon.com.be": "BE",
    "amazon.se": "SE",
    "amazon.pl": "PL",
}


def main() -> None:
    if len(sys.argv) == 3 and sys.argv[1] == "--from-file":
        df = pd.read_csv(sys.argv[2])
    else:
        resp = requests.get(DOWNLOAD_URL, timeout=300)
        resp.raise_for_status()
        df = pd.read_csv(io.BytesIO(resp.content), compression="infer")

    df["Period"] = pd.to_datetime(df["Period"], utc=True).dt.date
    end = df["Period"].max()
    start = end - timedelta(days=WINDOW_DAYS)
    window = df[df["Period"] > start].copy()

    window["country"] = (
        window["Marketplace Name"].astype(str).str.strip().str.lower()
        .map(MARKETPLACE_TO_COUNTRY)
    )
    unmapped = window[window["country"].isna()]["Marketplace Name"].unique()
    if len(unmapped):
        print(f"WARNING: unmapped marketplaces skipped: {list(unmapped)}")
    window = window.dropna(subset=["country", "SKU"])

    grouped = (
        window.groupby(["country", "SKU"])
        .agg(
            ad_spend_eur=("Advertising Costs", "sum"),
            units_ordered=("Units", "sum"),
        )
        .reset_index()
    )
    # Novadata reports advertising costs as negative values.
    grouped["ad_spend_eur"] = grouped["ad_spend_eur"].abs().round(2)
    grouped["spend_per_unit"] = (
        (grouped["ad_spend_eur"] / grouped["units_ordered"])
        .where(grouped["units_ordered"] > 0)
        .round(6)
    )

    out = grouped.rename(columns={"SKU": "sku"})[
        ["country", "sku", "spend_per_unit", "ad_spend_eur", "units_ordered"]
    ].sort_values(["country", "sku"])
    out.to_csv(DATA_DIR / "marketing_spend.csv", index=False)
    print(f"marketing_spend.csv: {len(out)} rows, {start} -> {end}")

    meta_path = DATA_DIR / "metadata.json"
    meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
    meta["marketing_spend"] = {
        "source": "Novadata daily margin export (Amazon Ads spend, auto-updated)",
        "window": f"trailing {WINDOW_DAYS} days",
        "export_date": end.isoformat(),
        "period_start": start.isoformat(),
        "period_end": end.isoformat(),
        "note": (
            "Spend per unit = total ad spend / total units ordered over the "
            "window, per SKU x marketplace. Refreshed daily by the "
            "update_marketing_spend GitHub Actions workflow. SKUs with spend "
            "but zero units are treated as having no per-unit spend."
        ),
    }
    meta_path.write_text(json.dumps(meta, indent=2) + "\n")
    print(f"metadata.json: period {start} -> {end}")


if __name__ == "__main__":
    main()
