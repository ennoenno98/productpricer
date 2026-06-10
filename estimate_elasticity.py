"""Estimate everyday price elasticity per SKU x marketplace.

Usage:
    python estimate_elasticity.py                      # download from Novadata
    python estimate_elasticity.py --from-file f.csv.gz # offline/testing

Method
------
Daily series from the Novadata margin export (12 months): implied price =
Product Sales / Units. Days are flagged as promo days from two sources:
  1. Promotion windows in data/promotions.csv (Seller Central report),
     attributed to marketplaces by matching the promo price against the
     median implied price inside the window (+-6%; EUR->GBP for amazon.co.uk).
  2. Inferred price dips: implied price <= 93% of the centered 28-day
     rolling median (catches promos missing from the report).

Per qualifying series, OLS on log-log with controls:
    ln(units) ~ ln(price) + promo_day + ln(1+ad_spend) + month + day-of-week
The ln(price) coefficient is the everyday elasticity; promo lift is absorbed
by the promo_day dummy so deal visibility doesn't inflate price sensitivity.

Estimates are shrunk toward the country's trimmed mean (precision-weighted),
clipped to [-9, -0.3], and written to data/elasticity.csv together with a
"_default" row per country for SKUs without their own estimate.
"""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

DATA_DIR = Path(__file__).resolve().parent / "data"

DOWNLOAD_URL = (
    "https://app.novadata.io/resources/data-export-download/"
    "d664ab4b-047a-471b-a993-27dd42d0a91b"
)
MARKETPLACE_TO_COUNTRY = {
    "amazon.de": "DE", "amazon.it": "IT", "amazon.es": "ES", "amazon.fr": "FR",
    "amazon.co.uk": "GB", "amazon.ie": "IE", "amazon.nl": "NL",
    "amazon.com.be": "BE", "amazon.se": "SE", "amazon.pl": "PL",
}
EUR_TO_GBP = 0.85          # promo prices are EUR; amazon.co.uk prices GBP
PROMO_PRICE_TOL = 0.06     # +-6% to attribute a promo window to a marketplace
DIP_THRESHOLD = 0.93       # inferred promo: price <= 93% of rolling median
MIN_DAYS = 120             # series quality gates
MIN_PRICE_LEVELS = 3       # distinct non-promo price points (0.10 grid)
MIN_RANGE = 0.04           # non-promo price range as share of mean
CLIP = (-9.0, -0.3)        # plausible elasticity range
TRIM = 0.2                 # trimmed-mean share for country priors


def load_daily(path_or_buf) -> pd.DataFrame:
    df = pd.read_csv(path_or_buf, compression="infer")
    df["Period"] = pd.to_datetime(df["Period"], utc=True).dt.tz_localize(None)
    df["country"] = (
        df["Marketplace Name"].astype(str).str.lower().map(MARKETPLACE_TO_COUNTRY)
    )
    d = df[(df["Units"] > 0) & (df["Product Sales"] > 0)].dropna(subset=["country"]).copy()
    d["price"] = d["Product Sales"] / d["Units"]
    d["ads"] = pd.to_numeric(d["Advertising Costs"], errors="coerce").abs().fillna(0)
    return d[["country", "SKU", "Child ASIN", "Period", "Units", "price", "ads"]]


def flag_promo_days(d: pd.DataFrame) -> pd.DataFrame:
    d = d.sort_values(["country", "SKU", "Period"]).copy()
    d["promo"] = False

    promos_path = DATA_DIR / "promotions.csv"
    n_attr = 0
    if promos_path.exists():
        promos = pd.read_csv(promos_path, parse_dates=["start", "end"])
        promos = promos[
            promos["promotion_type"].isin(["Best Deal", "Price Discount", "Sales Discount"])
            & (promos["promo_price"] > 0)
        ]
        for _, p in promos.iterrows():
            win = d[
                (d["Child ASIN"] == p["asin"])
                & (d["Period"] >= p["start"].normalize())
                & (d["Period"] <= p["end"].normalize())
            ]
            if win.empty:
                continue
            med = win.groupby("country")["price"].median()
            for country, price in med.items():
                target = p["promo_price"] * (EUR_TO_GBP if country == "GB" else 1.0)
                if abs(price / target - 1) <= PROMO_PRICE_TOL:
                    d.loc[win[win["country"] == country].index, "promo"] = True
                    n_attr += 1
    print(f"promo windows attributed to marketplaces: {n_attr}")

    # Inferred dips vs centered rolling median (fallback for unreported promos).
    med = (
        d.groupby(["country", "SKU"])["price"]
        .transform(lambda s: s.rolling(28, center=True, min_periods=7).median())
    )
    d["promo"] |= d["price"] <= DIP_THRESHOLD * med
    print(f"promo-flagged sale days: {d['promo'].sum()} of {len(d)} ({d['promo'].mean():.1%})")
    return d


def fit_series(sub: pd.DataFrame) -> tuple[float, float] | None:
    y = np.log(sub["Units"].to_numpy(dtype=float))
    mo = pd.get_dummies(sub["Period"].dt.month, drop_first=True, dtype=float)
    dw = pd.get_dummies(sub["Period"].dt.dayofweek, drop_first=True, dtype=float)
    X = np.column_stack(
        [
            np.ones(len(y)),
            np.log(sub["price"].to_numpy()),
            sub["promo"].to_numpy(dtype=float),
            np.log1p(sub["ads"].to_numpy()),
            mo.to_numpy(),
            dw.to_numpy(),
        ]
    )
    if len(y) <= X.shape[1] + 10:
        return None
    beta, *_ = np.linalg.lstsq(X, y, rcond=None)
    resid = y - X @ beta
    with np.errstate(invalid="ignore"):
        se = np.sqrt(
            np.diag(np.linalg.pinv(X.T @ X)) * (resid @ resid) / (len(y) - X.shape[1])
        )
    return float(beta[1]), float(se[1])


def trimmed_mean(values: np.ndarray, trim: float = TRIM) -> float:
    v = np.sort(values)
    k = int(len(v) * trim)
    return float(v[k : len(v) - k].mean()) if len(v) > 2 * k else float(v.mean())


def main() -> None:
    if len(sys.argv) == 3 and sys.argv[1] == "--from-file":
        d = load_daily(sys.argv[2])
    else:
        import requests

        resp = requests.get(DOWNLOAD_URL, timeout=300)
        resp.raise_for_status()
        d = load_daily(io.BytesIO(resp.content))

    d = flag_promo_days(d)

    rows = []
    for (country, sku), sub in d.groupby(["country", "SKU"]):
        nonpromo = sub[~sub["promo"]]
        if len(sub) < MIN_DAYS or len(nonpromo) < MIN_DAYS // 2:
            continue
        levels = ((nonpromo["price"] * 10).round() / 10).nunique()
        rng = (nonpromo["price"].max() - nonpromo["price"].min()) / nonpromo["price"].mean()
        if levels < MIN_PRICE_LEVELS or rng < MIN_RANGE:
            continue
        fit = fit_series(sub)
        if fit is None or not np.isfinite(fit[0]) or not np.isfinite(fit[1]) or fit[1] <= 0:
            continue
        rows.append(
            {
                "country": country, "sku": sku, "elasticity_raw": fit[0],
                "se": fit[1], "days": len(sub), "promo_days": int(sub["promo"].sum()),
            }
        )
    est = pd.DataFrame(rows)
    print(f"series estimated: {len(est)}")

    # Precision-weighted shrinkage toward the country's trimmed mean.
    out = []
    for country, grp in est.groupby("country"):
        usable = grp[(grp["elasticity_raw"] < 0) & (grp["se"] < 3)]
        mu = trimmed_mean(usable["elasticity_raw"].to_numpy()) if len(usable) >= 5 else -3.0
        tau2 = max(float(np.var(usable["elasticity_raw"])) - float(np.mean(usable["se"] ** 2)), 0.05) \
            if len(usable) >= 5 else 1.0
        for _, r in grp.iterrows():
            w = (1 / r["se"] ** 2) / (1 / r["se"] ** 2 + 1 / tau2)
            shrunk = w * r["elasticity_raw"] + (1 - w) * mu
            out.append(
                {
                    **r,
                    "elasticity": float(np.clip(shrunk, *CLIP)),
                    "method": "estimated",
                }
            )
        out.append(
            {
                "country": country, "sku": "_default",
                "elasticity_raw": mu, "se": np.nan, "days": 0, "promo_days": 0,
                "elasticity": float(np.clip(mu, *CLIP)), "method": "country-default",
            }
        )
    res = pd.DataFrame(out).sort_values(["country", "sku"])
    res.to_csv(DATA_DIR / "elasticity.csv", index=False)
    summary = res[res["method"] == "estimated"].groupby("country")["elasticity"].agg(["count", "median"])
    print(summary.round(2).to_string())

    meta_path = DATA_DIR / "metadata.json"
    meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
    meta["elasticity"] = {
        "source": "Novadata daily export (12m) + Seller Central promotions report",
        "method": (
            "log-log OLS per SKU x marketplace with promo-day, ad-spend and "
            "seasonality controls; precision-weighted shrinkage to country "
            "trimmed mean; everyday elasticity (promo lift modelled separately)"
        ),
        "estimated_series": int((res["method"] == "estimated").sum()),
        "generated": pd.Timestamp.today().date().isoformat(),
    }
    meta_path.write_text(json.dumps(meta, indent=2) + "\n")


if __name__ == "__main__":
    main()
