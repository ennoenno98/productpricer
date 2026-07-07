"""Estimate everyday price elasticity per SKU x marketplace.

Usage:
    python estimate_elasticity.py                      # download from Novadata
    python estimate_elasticity.py --from-file f.csv.gz # offline/testing

Method
------
Daily series from the Novadata margin export (12 months). The regressor is the
POSTED price, reconstructed by rounding implied price (Product Sales / Units)
to a 0.10 grid. Rounding severs the mechanical link where Units — the ln(Units)
regressand — also sits in the denominator of Sales/Units, which would otherwise
bias the slope negative (division bias). Extreme implied-price days (bundles,
mis-recorded revenue) are dropped so one leverage point can't set the fit.

Days are flagged as promo either from Seller Central promotion windows
(data/promotions.csv, matched to marketplaces by price) or as price dips
<= 93% of a trailing 35-CALENDAR-day median of NON-promo prices — so a
sustained reported promo cannot drag its own baseline down and mask itself,
and sparse-sales SKUs use real elapsed time rather than an N-row window.

Per qualifying series, OLS on log-log with controls:
    ln(units) ~ ln(posted_price) + promo_day + ln(1+ad_spend) + month + dow
solved via SVD with an explicit rank check: rank-deficient (unidentified)
designs are dropped, and standard errors come from the same decomposition, so a
near-collinear price column yields a large SE (failing the gate) rather than a
spuriously small one.

Estimates are shrunk (empirical Bayes) toward the country's trimmed-mean prior,
or a GLOBAL prior when a country has too few series (no hardcoded constant).
Series whose raw estimate has the wrong sign (>= 0) are NOT reported as
estimated — they fall back to the prior and are marked low confidence, rather
than being clipped up to -0.3 and passed off as a real coefficient. Every row
carries a `confidence` flag.

CAVEAT: price here is observational — sellers/Amazon set it in response to
demand — so these are ASSOCIATIONAL elasticities, not experimentally identified
causal effects. Validate with a real price test before large moves.
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
PRICE_GRID = 0.10          # round implied price to posted-price grid (division-bias fix)
OUTLIER_LOG = 0.5          # drop days where |ln(price / series median)| exceeds this
DIP_WINDOW = "35D"         # trailing CALENDAR window for the regular-price baseline
DIP_MIN_OBS = 5            # min non-promo observations in the baseline window
DIP_THRESHOLD = 0.93       # inferred promo: price <= 93% of regular baseline
MIN_DAYS = 120             # series quality gates
MIN_PRICE_LEVELS = 3       # distinct non-promo posted prices
MIN_RANGE = 0.04           # non-promo price range as share of mean
CLIP = (-9.0, -0.3)        # plausible elasticity range
TRIM = 0.2                 # trimmed-mean share for priors
MIN_COUNTRY_SERIES = 5     # below this, a country uses the global prior


def load_daily(path_or_buf) -> pd.DataFrame:
    df = pd.read_csv(path_or_buf, compression="infer")
    df["Period"] = pd.to_datetime(df["Period"], utc=True).dt.tz_localize(None)
    df["country"] = (
        df["Marketplace Name"].astype(str).str.lower().map(MARKETPLACE_TO_COUNTRY)
    )
    d = df[(df["Units"] > 0) & (df["Product Sales"] > 0)].dropna(subset=["country"]).copy()
    implied = d["Product Sales"] / d["Units"]
    d["implied_price"] = implied
    # Posted price on a 0.10 grid: removes the sub-cent Sales/Units movement
    # that shares the Units denominator with the ln(Units) regressand.
    d["price"] = (implied / PRICE_GRID).round() * PRICE_GRID
    d["ads"] = pd.to_numeric(d["Advertising Costs"], errors="coerce").abs().fillna(0)
    d = d[d["price"] > 0]
    return d[["country", "SKU", "Child ASIN", "Period", "Units", "price",
              "implied_price", "ads"]]


def drop_price_outliers(d: pd.DataFrame) -> pd.DataFrame:
    """Drop days whose implied price is far from the series median (bundles,
    mis-recorded revenue). A single such day would otherwise both inflate the
    (max-min)/mean range gate and act as a high-leverage point in the fit."""
    med = d.groupby(["country", "SKU"])["implied_price"].transform("median")
    keep = np.log(d["implied_price"] / med).abs() <= OUTLIER_LOG
    return d[keep].copy()


def flag_promo_days(d: pd.DataFrame) -> pd.DataFrame:
    d = d.sort_values(["country", "SKU", "Period"]).copy()
    d["promo"] = False

    promos_path = DATA_DIR / "promotions.csv"
    n_attr = 0
    if promos_path.exists():
        promos = pd.read_csv(promos_path, parse_dates=["start", "end"])
        # Coerce promo_price to numeric (handles comma decimals / stray symbols)
        # so a string dtype cannot crash the comparison or the FX arithmetic.
        promos["promo_price"] = pd.to_numeric(
            promos["promo_price"].astype(str).str.replace(",", ".", regex=False),
            errors="coerce",
        )
        promos = promos[
            promos["promotion_type"].isin(["Best Deal", "Price Discount", "Sales Discount"])
            & (promos["promo_price"] > 0)
            & promos["start"].notna()
            & promos["end"].notna()
        ]
        # Normalize ASIN keys on both sides (whitespace/case) before matching.
        d["asin_key"] = d["Child ASIN"].astype(str).str.strip().str.upper()
        for _, p in promos.iterrows():
            asin = str(p["asin"]).strip().upper()
            win = d[
                (d["asin_key"] == asin)
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
        d = d.drop(columns="asin_key")
    print(f"promo windows attributed to marketplaces: {n_attr}")

    # Regular-price baseline: trailing 35-calendar-day median of the
    # report-non-promo prices only. Excluding already-flagged promo days stops a
    # sustained promo from pulling its own baseline down; the calendar window
    # keeps the reference meaningful for sparse-sales SKUs.
    def _regular(g: pd.DataFrame) -> pd.Series:
        ser = pd.Series(
            g["price"].where(~g["promo"]).to_numpy(dtype=float),
            index=pd.DatetimeIndex(g["Period"]),
        )
        ref = ser.rolling(DIP_WINDOW, min_periods=DIP_MIN_OBS).median()
        return pd.Series(ref.to_numpy(), index=g.index)

    d["regular"] = d.groupby(["country", "SKU"], group_keys=False).apply(_regular)
    d["promo"] = d["promo"] | (d["price"] <= DIP_THRESHOLD * d["regular"])
    print(f"promo-flagged sale days: {d['promo'].sum()} of {len(d)} ({d['promo'].mean():.1%})")
    return d.drop(columns="regular")


def fit_series(sub: pd.DataFrame) -> tuple[float, float] | None:
    """Log-log OLS via SVD with a rank check.

    Returns (elasticity, se) or None when the design is rank-deficient (the
    price coefficient is not identified). The SE is derived from the same SVD,
    so a near-collinear price column produces a large SE rather than the
    deflated one that pinv(X.T@X) with a mismatched cutoff would give.
    """
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
    n, p = X.shape
    if n <= p + 10:
        return None
    U, s, Vt = np.linalg.svd(X, full_matrices=False)
    if s.size == 0:
        return None
    tol = max(n, p) * np.finfo(float).eps * s[0]
    if s[-1] <= tol:
        return None  # rank-deficient → price effect not separately identified
    beta = Vt.T @ ((U.T @ y) / s)
    resid = y - X @ beta
    sigma2 = float(resid @ resid) / (n - p)
    # diag((X'X)^-1)_j = sum_k V[j,k]^2 / s_k^2
    cov_diag = (Vt.T ** 2) @ (1.0 / s ** 2) * sigma2
    return float(beta[1]), float(np.sqrt(cov_diag[1]))


def trimmed_mean(values: np.ndarray, trim: float = TRIM) -> float:
    v = np.sort(values)
    k = int(len(v) * trim)
    return float(v[k : len(v) - k].mean()) if len(v) > 2 * k else float(v.mean())


def _prior(raws: np.ndarray, ses: np.ndarray) -> tuple[float, float]:
    """Empirical-Bayes (mu, tau2) from a set of raw estimates. Uses the
    unbiased sample variance (ddof=1) for the between-series component."""
    mu = trimmed_mean(raws)
    var = float(np.var(raws, ddof=1)) if len(raws) > 1 else 1.0
    tau2 = max(var - float(np.mean(ses ** 2)), 0.05)
    return mu, tau2


def _confidence(raw: float, se: float, w: float, pooled: bool) -> str:
    if raw >= 0 or pooled or se >= 2.0 or w < 0.3:
        return "low"
    if w >= 0.7 and se < 0.6:
        return "high"
    return "medium"


def main() -> None:
    if len(sys.argv) == 3 and sys.argv[1] == "--from-file":
        d = load_daily(sys.argv[2])
    else:
        import requests

        resp = requests.get(DOWNLOAD_URL, timeout=300)
        resp.raise_for_status()
        d = load_daily(io.BytesIO(resp.content))

    d = drop_price_outliers(d)
    d = flag_promo_days(d)

    cols = ["country", "sku", "elasticity_raw", "se", "days", "promo_days",
            "elasticity", "method", "confidence"]
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
        rows.append({
            "country": country, "sku": sku, "elasticity_raw": fit[0],
            "se": fit[1], "days": len(sub), "promo_days": int(sub["promo"].sum()),
        })
    est = pd.DataFrame(rows)
    print(f"series estimated: {len(est)}")
    if est.empty:
        pd.DataFrame(columns=cols).to_csv(DATA_DIR / "elasticity.csv", index=False)
        return

    # Global prior fallback for marketplaces with too few usable series.
    usable_all = est[(est["elasticity_raw"] < 0) & (est["se"] < 3)]
    if len(usable_all) >= MIN_COUNTRY_SERIES:
        g_mu, g_tau2 = _prior(usable_all["elasticity_raw"].to_numpy(),
                              usable_all["se"].to_numpy())
    else:
        g_mu, g_tau2 = -2.0, 1.0

    out = []
    for country, grp in est.groupby("country"):
        usable = grp[(grp["elasticity_raw"] < 0) & (grp["se"] < 3)]
        pooled = len(usable) < MIN_COUNTRY_SERIES
        mu, tau2 = (g_mu, g_tau2) if pooled else _prior(
            usable["elasticity_raw"].to_numpy(), usable["se"].to_numpy()
        )
        for _, r in grp.iterrows():
            w = (1 / r["se"] ** 2) / (1 / r["se"] ** 2 + 1 / tau2)
            if r["elasticity_raw"] >= 0:
                # Wrong sign = no demand response identified: use the prior,
                # don't clip a positive raw up to -0.3 and call it estimated.
                elasticity, method = float(np.clip(mu, *CLIP)), "prior-fallback"
            else:
                shrunk = w * r["elasticity_raw"] + (1 - w) * mu
                elasticity, method = float(np.clip(shrunk, *CLIP)), "estimated"
            out.append({
                **r, "elasticity": elasticity, "method": method,
                "confidence": _confidence(r["elasticity_raw"], r["se"], w, pooled),
            })
        out.append({
            "country": country, "sku": "_default", "elasticity_raw": mu,
            "se": np.nan, "days": 0, "promo_days": 0,
            "elasticity": float(np.clip(mu, *CLIP)),
            "method": "global-prior" if pooled else "country-default",
            "confidence": "low",
        })
    res = pd.DataFrame(out)[cols].sort_values(["country", "sku"])
    res.to_csv(DATA_DIR / "elasticity.csv", index=False)
    est_mask = res["method"] == "estimated"
    summary = res[est_mask].groupby("country")["elasticity"].agg(["count", "median"])
    print(summary.round(2).to_string())

    meta_path = DATA_DIR / "metadata.json"
    meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
    meta["elasticity"] = {
        "source": "Novadata daily export (12m) + Seller Central promotions report",
        "method": (
            "log-log OLS (SVD + rank check) per SKU x marketplace on posted "
            "price (0.10 grid) with promo-day, ad-spend and seasonality "
            "controls; empirical-Bayes shrinkage to country/global prior; "
            "wrong-sign fits fall back to prior; associational, not causal"
        ),
        "estimated_series": int(est_mask.sum()),
        "generated": pd.Timestamp.today().date().isoformat(),
    }
    meta_path.write_text(json.dumps(meta, indent=2) + "\n")


if __name__ == "__main__":
    main()
