"""Build data/fba_form_factors.csv: a form-factor x marketplace FBA fee lookup.

The New-product tab estimates a new SKU's Amazon fulfilment fee by borrowing
the median fee of comparable live products (nearest-neighbour by form factor).
Fees cluster tightly within a marketplace, so this lands within a few cents.

Usage:
    python build_fba_lut.py            # uses the newest data/fee_history snapshot
"""
from __future__ import annotations

import glob
import re
from pathlib import Path

import pandas as pd

DATA_DIR = Path(__file__).resolve().parent / "data"
FEE = "expected-domestic-fulfilment-fee-per-unit"


def form_factor(name: str) -> str:
    n = str(name).lower()
    if re.search(r"gumm|gomm|gomin|soft bite|gums\b|gummibär", n):
        return "Gummies (pouch)"
    if re.search(r"\bpulver|powder|poudre|polvo|polvere|\btub\b", n):
        return "Powder (tub)"
    if re.search(r"\böl\b|\boil\b|olio|aceite|huile|softgel|perlas|perle", n):
        return "Oil / softgel"
    if re.search(r"kaps|caps|cáps|gél|capsule|comprim|tablet|tabletten|stück|stk", n):
        return "Capsule / tablet bottle"
    return "Other supplement"


def main() -> None:
    snaps = sorted(glob.glob(str(DATA_DIR / "fee_history" / "products_*.csv")))
    path = snaps[-1] if snaps else str(DATA_DIR / "products.csv")
    df = pd.read_csv(path)
    df["form"] = df["product-name"].map(form_factor)
    lut = df.groupby(["form", "amazon-store"])[FEE].median().round(2).unstack()
    lut.loc["— any (marketplace median)"] = df.groupby("amazon-store")[FEE].median().round(2)
    lut.to_csv(DATA_DIR / "fba_form_factors.csv")
    print(f"fba_form_factors.csv from {Path(path).name}: "
          f"{lut.shape[0]} form factors x {lut.shape[1]} marketplaces")


if __name__ == "__main__":
    main()
