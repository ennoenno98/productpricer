"""Build data/fba_packaging.csv: estimated FBA fee per packaging type x marketplace.

The New-product tab estimates a new SKU's Amazon fulfilment fee from live
products in the same packaging. Which SKU uses which packaging is maintained by
the product team in data/packaging.csv (SKU -> packaging); the fee per
packaging and marketplace is the median of those SKUs' current FBA fees.

Output (long format): packaging, marketplace, fba_fee (median), n_skus.
Multipack SKUs (VMP_<base>_<n>_PK) are excluded; their fees reflect the bundle.

Usage:
    python build_fba_lut.py            # uses the newest data/fee_history snapshot
"""
from __future__ import annotations

import glob
from pathlib import Path

import pandas as pd

DATA_DIR = Path(__file__).resolve().parent / "data"
FEE = "expected-domestic-fulfilment-fee-per-unit"


def main() -> None:
    snaps = sorted(glob.glob(str(DATA_DIR / "fee_history" / "products_*.csv")))
    path = snaps[-1] if snaps else str(DATA_DIR / "products.csv")
    fees = pd.read_csv(path)
    fees = fees[~fees["sku"].astype(str).str.match(r"^VMP_.+_\d+_PK$")]
    pack = pd.read_csv(DATA_DIR / "packaging.csv")
    pack = pack[pack["packaging"] != "Unknown"]
    x = fees.merge(pack[["sku", "packaging"]], on="sku").dropna(subset=[FEE])
    lut = (
        x.groupby(["packaging", "amazon-store"])[FEE]
        .agg(fba_fee="median", n_skus="count")
        .reset_index()
        .rename(columns={"amazon-store": "marketplace"})
    )
    lut["fba_fee"] = lut["fba_fee"].round(2)
    lut.to_csv(DATA_DIR / "fba_packaging.csv", index=False)
    print(f"fba_packaging.csv from {Path(path).name}: "
          f"{lut['packaging'].nunique()} packaging types, {len(lut)} rows")


if __name__ == "__main__":
    main()
