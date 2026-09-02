"""Product Pricer — Amazon pricing dashboard (Streamlit).

Per-country pricing sheet for the marketing team: change prices and see the
impact on CM1 / CM2 / CM3 live, plus a single-product margin calculator
modelled on the "Margen Calc AMZ" sheet of Margin_Check_V5.xlsx.

Data inputs (see data/ and extract_from_excel.py):
    data/products.csv         one row per SKU x marketplace (price, fees, COGS)
    data/marketing_spend.csv  ad spend per unit per SKU x marketplace

Run locally:
    pip install -r requirements.txt
    export DASHBOARD_PASSWORD="choose-a-password"
    streamlit run streamlit_app.py
"""
from __future__ import annotations

import io
import json
import os
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

REPO_ROOT = Path(__file__).resolve().parent
DATA_DIR = REPO_ROOT / "data"

st.set_page_config(page_title="Product Pricer", page_icon="💶", layout="wide")

COUNTRY_NAMES = {
    "DE": "Germany",
    "IT": "Italy",
    "ES": "Spain",
    "FR": "France",
    "GB": "United Kingdom",
    "IE": "Ireland",
    "NL": "Netherlands",
    "BE": "Belgium",
    "SE": "Sweden",
    "PL": "Poland",
}

# VAT on food supplements per marketplace (fixed, from the margin workbook's
# "SQL calc measures general" sheet — not a user input).
VAT_RATES = {
    "DE": 0.07,
    "GB": 0.20,
    "FR": 0.055,
    "IT": 0.10,
    "ES": 0.10,
    "NL": 0.09,
    "BE": 0.06,
    "IE": 0.135,
    "SE": 0.12,
    "PL": 0.08,
}

EUR_TO_GBP_DEFAULT = 0.83  # COGS / ad spend are in EUR, GB prices in GBP
MONTHS = [
    "January", "February", "March", "April", "May", "June", "July",
    "August", "September", "October", "November", "December",
]

CURRENCY_SYMBOL = {"GBP": "£", "EUR": "€"}


# ---------- Auth ----------
def _get_password() -> str | None:
    pw = os.environ.get("DASHBOARD_PASSWORD")
    if pw:
        return pw
    try:
        return st.secrets["DASHBOARD_PASSWORD"]
    except Exception:
        return None


def require_login() -> None:
    expected = _get_password()
    if not expected:
        st.error(
            "DASHBOARD_PASSWORD is not set. Configure it in the host's "
            "environment variables (Render: Service → Environment) or in "
            "`.streamlit/secrets.toml` for local dev."
        )
        st.stop()

    if st.session_state.get("auth_ok"):
        return

    st.title("Product Pricer")
    with st.form("login"):
        pw = st.text_input("Password", type="password")
        ok = st.form_submit_button("Sign in")
    if ok:
        if pw == expected:
            st.session_state["auth_ok"] = True
            st.rerun()
        else:
            st.error("Wrong password.")
    st.stop()


# ---------- Data ----------
COLUMN_RENAMES = {
    "amazon-store": "country",
    "product-name": "product_name",
    "your-price": "current_price",
    "estimated-referral-fee-per-unit": "referral_fee",
    "estimated-variable-closing-fee": "closing_fee",
    "expected-domestic-fulfilment-fee-per-unit": "fba_fee",
    "COGS": "cogs_eur",
    "cogs": "cogs_eur",
}
NUMERIC_COLS = ["current_price", "referral_fee", "closing_fee", "fba_fee", "cogs_eur"]
REQUIRED_REPORT_COLS = {
    "sku": "sku",
    "country": "amazon-store",
    "current_price": "your-price",
    "referral_fee": "estimated-referral-fee-per-unit",
    "fba_fee": "expected-domestic-fulfilment-fee-per-unit",
}


def _to_num(series: pd.Series) -> pd.Series:
    """Tolerant numeric parsing (handles '1,23' comma decimals and '--')."""
    return pd.to_numeric(
        series.astype(str).str.replace(",", ".", regex=False), errors="coerce"
    )


def fee_snapshot_files() -> list[Path]:
    """Committed FBA fee report versions, oldest -> newest (dated filenames)."""
    return sorted((DATA_DIR / "fee_history").glob("products_*.csv"))


@st.cache_data(show_spinner=False)
def load_snapshot(path_str: str) -> pd.DataFrame:
    df = pd.read_csv(path_str).rename(columns=COLUMN_RENAMES)
    for col in NUMERIC_COLS:
        if col in df.columns:
            df[col] = _to_num(df[col])
    return df


def _read_report(content: bytes, name: str) -> pd.DataFrame:
    """Read an Amazon FBA fee preview export (.csv / .txt / .tsv / .xlsx)."""
    if name.lower().endswith(".xlsx"):
        return pd.read_excel(io.BytesIO(content))
    for enc in ("utf-8-sig", "latin-1"):
        try:
            text = content.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    # sep=None sniffs the delimiter (Amazon ships tab-separated .txt files).
    return pd.read_csv(io.StringIO(text), sep=None, engine="python")


@st.cache_data(show_spinner=False)
def load_data(
    report: bytes | None = None, report_name: str = "", snapshot: str = ""
) -> tuple[pd.DataFrame, dict]:
    """Build the pricing dataset; an uploaded fee report replaces the committed one.

    `snapshot` is the latest fee_history filename (cache key + baseline source;
    falls back to data/products.csv when no snapshots exist). Returns
    (dataset, info); `info` describes the uploaded report merge and is empty
    for committed data.
    """
    base_path = DATA_DIR / "fee_history" / snapshot if snapshot else DATA_DIR / "products.csv"
    bundled = pd.read_csv(base_path).rename(columns=COLUMN_RENAMES)
    info: dict = {}

    if report is None:
        products = bundled
    else:
        raw = _read_report(report, report_name)
        raw.columns = [str(c).strip().lower() for c in raw.columns]
        raw = raw.rename(columns=COLUMN_RENAMES)
        missing = [
            orig for col, orig in REQUIRED_REPORT_COLS.items() if col not in raw.columns
        ]
        if missing:
            raise ValueError(
                "This doesn't look like an FBA fee preview report — missing "
                f"column(s): {', '.join(missing)}. Export it from Seller Central → "
                "Reports → Fulfilment by Amazon → Payments → Fee preview."
            )
        raw["country"] = raw["country"].astype(str).str.upper().replace({"UK": "GB"})
        for col in ["product_name", "brand", "asin", "sales-price"]:
            if col not in raw.columns:
                raw[col] = ""
        if "closing_fee" not in raw.columns:
            raw["closing_fee"] = 0.0
        if "currency" not in raw.columns:
            raw["currency"] = raw["country"].map(
                lambda c: "GBP" if c == "GB" else "EUR"
            )
        raw = raw.drop_duplicates(["country", "sku"], keep="first")

        # The raw Amazon report has no COGS: take it from the report if present,
        # otherwise carry it over from the bundled data (per SKU).
        if "cogs_eur" not in raw.columns or _to_num(raw["cogs_eur"]).isna().all():
            cogs_map = (
                bundled.dropna(subset=["cogs_eur"])
                .drop_duplicates("sku")
                .set_index("sku")["cogs_eur"]
            )
            raw["cogs_eur"] = raw["sku"].map(cogs_map)
        products = raw
        info = {
            "rows": len(products),
            "countries": sorted(products["country"].unique()),
            "cogs_missing": int(_to_num(products["cogs_eur"]).isna().sum()),
        }

    for col in NUMERIC_COLS:
        products[col] = _to_num(products[col])

    spend = pd.read_csv(DATA_DIR / "marketing_spend.csv")
    # Marketing export uses "UK" for the GB marketplace.
    spend["country"] = spend["country"].replace({"UK": "GB"})
    spend["spend_per_unit"] = pd.to_numeric(spend["spend_per_unit"], errors="coerce")
    spend["units_ordered"] = pd.to_numeric(spend["units_ordered"], errors="coerce")
    products = products.merge(
        spend[["country", "sku", "spend_per_unit", "units_ordered"]],
        on=["country", "sku"], how="left",
    )
    # No ads recorded for the SKU -> 0, matching the XLOOKUP(...,0) in Excel.
    products["spend_per_unit_eur"] = products["spend_per_unit"].fillna(0.0)
    # Units sold in the ad-spend window (Novadata), for volume-weighted impact.
    products["units_window"] = products["units_ordered"].fillna(0.0)

    # Referral fee scales with price: derive the rate from Amazon's estimate
    # at the current price so it can be re-applied to any simulated price.
    products["referral_rate"] = (
        products["referral_fee"] / products["current_price"]
    ).where(products["current_price"] > 0, 0.15)
    return products, info


def _github_config() -> tuple[str | None, str, str]:
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        try:
            token = st.secrets["GITHUB_TOKEN"]
        except Exception:
            token = None
    repo = os.environ.get("GITHUB_REPO", "ennoenno98/productpricer")
    # Default to the branch this deployment runs from (Render sets
    # RENDER_GIT_BRANCH), so saved snapshots land where the deploy reads them.
    branch = (
        os.environ.get("GITHUB_BRANCH")
        or os.environ.get("RENDER_GIT_BRANCH")
        or "main"
    )
    return token, repo, branch


def push_snapshot_to_github(filename: str, content: bytes) -> tuple[bool, str]:
    """Commit a fee snapshot to data/fee_history/ in the GitHub repo."""
    token, repo, branch = _github_config()
    if not token:
        return False, (
            "GITHUB_TOKEN is not configured on the server, so the snapshot is "
            "saved on local disk only — it will be lost on the next redeploy. "
            "Set GITHUB_TOKEN (fine-grained PAT with contents:write) in the "
            "host environment to make saving permanent, or download the "
            "snapshot and commit it manually."
        )
    import base64

    import requests

    url = f"https://api.github.com/repos/{repo}/contents/data/fee_history/{filename}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
    }
    payload = {
        "message": f"Add FBA fee snapshot {filename} (saved from dashboard)",
        "content": base64.b64encode(content).decode(),
        "branch": branch,
    }
    try:
        probe = requests.get(url, headers=headers, params={"ref": branch}, timeout=30)
        if probe.status_code == 200:
            payload["sha"] = probe.json()["sha"]  # same-day re-save: update
        resp = requests.put(url, headers=headers, json=payload, timeout=30)
    except requests.RequestException as exc:
        return False, f"GitHub commit failed: {exc}"
    if resp.status_code in (200, 201):
        return True, f"Committed to {repo}@{branch} — the deploy will pick it up."
    detail = resp.json().get("message", "") if resp.headers.get("content-type", "").startswith("application/json") else ""
    return False, f"GitHub commit failed ({resp.status_code} {detail})."


def save_snapshot(df: pd.DataFrame, filename: str) -> list[tuple[str, str]]:
    """Persist an uploaded report as the new baseline. Returns (level, msg) list."""
    content = products_csv_for_repo(df)
    messages: list[tuple[str, str]] = []
    try:
        target = DATA_DIR / "fee_history" / filename
        target.parent.mkdir(exist_ok=True)
        target.write_bytes(content)
        messages.append((
            "success",
            f"Saved `{filename}` as the new baseline — colleagues see it "
            "after a page refresh.",
        ))
    except OSError as exc:
        messages.append(("error", f"Could not write snapshot locally: {exc}"))
        return messages
    ok, msg = push_snapshot_to_github(filename, content)
    messages.append(("success" if ok else "warning", msg))
    return messages


def products_csv_for_repo(df: pd.DataFrame) -> bytes:
    """Serialize the dataset back to the data/products.csv schema."""
    inverse = {
        "country": "amazon-store",
        "product_name": "product-name",
        "current_price": "your-price",
        "referral_fee": "estimated-referral-fee-per-unit",
        "closing_fee": "estimated-variable-closing-fee",
        "fba_fee": "expected-domestic-fulfilment-fee-per-unit",
        "cogs_eur": "COGS",
    }
    cols = [
        "sku", "asin", "country", "product_name", "brand", "current_price",
        "sales-price", "currency", "referral_fee", "closing_fee", "fba_fee",
        "cogs_eur",
    ]
    out = df.copy()
    for c in cols:
        if c not in out.columns:
            out[c] = ""
    return out[cols].rename(columns=inverse).to_csv(index=False).encode("utf-8")


# Header for the editable "New price" column in the bulk-edit Excel template.
# Used both when writing the template and when parsing it back on upload.
def _new_price_header(cur: str) -> str:
    return f"New price ({cur})"


def pricing_template_xlsx(show: pd.DataFrame, country: str, cur: str) -> bytes:
    """Excel template for bulk price editing.

    The marketing team downloads this, types the new price into the (blank)
    "New price" column for the SKUs they want to change, and re-uploads it.
    Leaving "New price" blank keeps the current price. The "New price" column
    is pre-filled only where the current scenario already overrides a SKU.
    """
    cur_h = f"Current price ({cur})"
    new_h = _new_price_header(cur)
    cols = ["sku", "product_name", "current_price", "new_price",
            "cm1_pct", "cm2_pct", "cm3_pct"]
    rename = {
        "sku": "SKU", "product_name": "Product", "current_price": cur_h,
        "new_price": new_h, "cm1_pct": "CM1 %", "cm2_pct": "CM2 %",
        "cm3_pct": "CM3 %",
    }
    out = show[[c for c in cols if c in show.columns]].rename(columns=rename)
    # Blank "New price" where it equals current (no active edit) so the column
    # reads as an empty fill-in field rather than a copy of the current price.
    same = out[new_h].round(2).eq(out[cur_h].round(2))
    out[new_h] = out[new_h].where(~same, "")

    buf = io.BytesIO()
    sheet = f"Pricing {country}"
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        out.to_excel(writer, index=False, sheet_name=sheet)
        ws = writer.sheets[sheet]
        for i, width in enumerate([18, 60, 16, 16, 9, 9, 9]):
            ws.column_dimensions[chr(ord("A") + i)].width = width
        ws.freeze_panes = "A2"
    return buf.getvalue()


def parse_uploaded_pricing(
    file, current_by_sku: dict[str, float]
) -> tuple[dict[str, float], dict]:
    """Read an edited pricing template (xlsx/csv) -> {sku: new_price} overrides.

    Only prices that (a) match a SKU, (b) are non-blank, and (c) differ from the
    current price become overrides. Returns (overrides, info) where info also
    carries diagnostics (detected column, a parsed sample, wrong-column hint).
    """
    name = file.name.lower()
    raw = file.getvalue()
    if name.endswith((".xlsx", ".xls")):
        df = pd.read_excel(io.BytesIO(raw))
    else:
        # German Excel exports use ';' + comma decimals; let the python engine
        # sniff the delimiter, trying utf-8 then cp1252/latin-1.
        df = None
        for enc in ("utf-8-sig", "cp1252", "latin-1"):
            try:
                df = pd.read_csv(io.StringIO(raw.decode(enc)), sep=None, engine="python")
                break
            except (UnicodeDecodeError, ValueError):
                continue
        if df is None:
            raise ValueError("Could not decode the CSV file.")
    df.columns = [str(c).strip().lower() for c in df.columns]
    sku_col = next((c for c in df.columns if c == "sku" or c.startswith("sku")), None)
    price_col = next((c for c in df.columns if "new price" in c or "new_price" in c), None)
    if price_col is None:
        price_col = next((c for c in df.columns if c.startswith("new")), None)
    cur_col = next((c for c in df.columns if "current price" in c or c == "current_price"), None)
    if sku_col is None or price_col is None:
        raise ValueError(
            f"Could not find a 'SKU' and a 'New price' column (saw: "
            f"{', '.join(map(str, df.columns))}). Use the downloaded template "
            "and keep those column headers."
        )
    df["_sku"] = df[sku_col].astype(str).str.strip()
    df["_price"] = _to_num(df[price_col])
    df["_file_cur"] = _to_num(df[cur_col]) if cur_col else float("nan")

    overrides: dict[str, float] = {}
    matched = unmatched = changed = blank = invalid = 0
    wrong_col_hits = 0
    sample = None
    for _, r in df.iterrows():
        sku = r["_sku"]
        if sku in ("", "nan"):
            continue
        if sku not in current_by_sku:
            unmatched += 1
            continue
        matched += 1
        # Did they (accidentally) edit the Current price column instead?
        if (
            cur_col and pd.notna(r["_file_cur"])
            and abs(r["_file_cur"] - current_by_sku[sku]) > 0.004
        ):
            wrong_col_hits += 1
        if pd.isna(r["_price"]):
            blank += 1  # blank New price = keep current price
            continue
        price = round(float(r["_price"]), 2)
        if price < 0:
            invalid += 1
            continue
        if abs(price - current_by_sku[sku]) > 0.004:
            overrides[sku] = price
            changed += 1
            if sample is None:
                sample = f"{sku}: {current_by_sku[sku]:.2f} → {price:.2f}"
    info = {
        "matched": matched, "unmatched": unmatched, "changed": changed,
        "blank": blank, "invalid": invalid, "column": price_col,
        "sample": sample, "wrong_column": wrong_col_hits > matched / 2 > 0,
    }
    return overrides, info


@st.cache_data(show_spinner=False)
def load_metadata() -> dict:
    try:
        return json.loads((DATA_DIR / "metadata.json").read_text())
    except Exception:
        return {}


@st.cache_data(show_spinner=False)
def load_targets() -> dict:
    """Country margin targets from the AP26 plan (see extract_targets.py)."""
    return json.loads((DATA_DIR / "targets.json").read_text())


@st.cache_data(show_spinner=False)
def load_brand_targets() -> dict:
    """Brand GP1/GP2/GP3 targets from the AP26 'PnL per Brand' sheet."""
    try:
        return json.loads((DATA_DIR / "brand_targets.json").read_text())
    except Exception:
        return {"brands": {}}


@st.cache_data(show_spinner=False)
def load_fba_lut() -> pd.DataFrame:
    """Form-factor x marketplace FBA fee lookup (median of catalog fees)."""
    try:
        return pd.read_csv(DATA_DIR / "fba_form_factors.csv", index_col=0)
    except Exception:
        return pd.DataFrame()


def required_price(cogs: float, fba: float, vat: float, ref_rate: float,
                   target: float, ads_per_unit: float = 0.0) -> float:
    """Gross price at which margin (net basis) hits `target`, fees scaling with price.

    margin = net - cogs - fba - ref_rate*gross - ads;  net = gross/(1+vat).
    Solving margin/net = target for gross. Returns nan if the denominator is
    non-positive (target unreachable at any price).
    """
    fixed = cogs + fba + ads_per_unit
    denom = (1 - target) / (1 + vat) - ref_rate
    return fixed / denom if denom > 0 else float("nan")


def country_targets(country: str, month: str | None = None) -> tuple[float, float]:
    """(CM2 target, CM3 target) for a country from the AP26 plan.

    `month` selects the seasonal CM3 target; None means the full-year target.
    Countries not in the plan (e.g. BE) fall back to the plan average.
    """
    t = load_targets()
    cm2_map = t["cm2_target"]
    cm3_map = t["cm3_target_monthly"].get(month, t["cm3_target"]) if month else t["cm3_target"]
    cm2 = cm2_map.get(country, sum(cm2_map.values()) / len(cm2_map))
    cm3 = cm3_map.get(country, sum(cm3_map.values()) / len(cm3_map))
    return cm2, cm3


def spend_period_label() -> str:
    meta = load_metadata().get("marketing_spend", {})
    window = meta.get("window", "unknown period")
    start, end = meta.get("period_start"), meta.get("period_end")
    if start and end:
        return f"{window} ({start} → {end})"
    return window


@st.cache_data(show_spinner=False)
def _load_elasticity_cached(mtime: float) -> pd.DataFrame | None:
    try:
        return pd.read_csv(DATA_DIR / "elasticity.csv")
    except Exception:
        return None


def load_elasticity() -> pd.DataFrame | None:
    # Key the cache on the file's mtime so regenerating elasticity.csv
    # (estimate_elasticity.py) invalidates it instead of serving a stale copy.
    try:
        mtime = (DATA_DIR / "elasticity.csv").stat().st_mtime
    except OSError:
        return None
    return _load_elasticity_cached(mtime)


def attach_elasticity(df: pd.DataFrame) -> pd.DataFrame:
    """Merge per-SKU everyday elasticity; country default where not estimated."""
    df = df.copy()
    el = load_elasticity()
    if el is None:
        df["elasticity"] = np.nan
        df["elasticity_method"] = "none"
        df["elasticity_confidence"] = "n/a"
        return df
    if "confidence" not in el.columns:
        el["confidence"] = "low"
    sku_level = el[el["sku"] != "_default"][["country", "sku", "elasticity", "confidence"]]
    defaults = (
        el[el["sku"] == "_default"].set_index("country")["elasticity"].to_dict()
    )
    overall = float(np.nanmedian(list(defaults.values()))) if defaults else -3.0
    if not np.isfinite(overall):
        overall = -3.0
    df = df.merge(sku_level, on=["country", "sku"], how="left")
    df["elasticity_method"] = np.where(df["elasticity"].notna(), "estimated", "country-default")
    df["elasticity_confidence"] = df["confidence"].fillna("low")
    df = df.drop(columns="confidence")
    df["elasticity"] = df["elasticity"].fillna(
        df["country"].map(defaults).fillna(overall)
    )
    return df


def localize_costs(df: pd.DataFrame, eur_to_gbp: float) -> pd.DataFrame:
    """COGS and ad spend are in EUR; convert to GBP for the GB marketplace."""
    df = df.copy()
    fx = df["country"].map(lambda c: eur_to_gbp if c == "GB" else 1.0)
    df["cogs"] = df["cogs_eur"] * fx
    df["marketing_per_unit"] = df["spend_per_unit_eur"] * fx
    return df


def compute_margins(df: pd.DataFrame, price_col: str, vat: float, suffix: str) -> pd.DataFrame:
    """CM1/2/3 per unit at the gross price in `price_col`.

    CM1 = net price - COGS
    CM2 = CM1 - FBA fulfilment fee - referral fee (rate x gross) - closing fee
    CM3 = CM2 - marketing spend per unit
    Percentages are relative to the net price at the simulated price.
    """
    df = df.copy()
    gross = df[price_col]
    net = gross / (1 + vat)
    referral = gross * df["referral_rate"] + df["closing_fee"].fillna(0)
    cm1 = net - df["cogs"]
    cm2 = cm1 - df["fba_fee"] - referral
    cm3 = cm2 - df["marketing_per_unit"]
    df[f"net{suffix}"] = net
    df[f"referral{suffix}"] = referral
    df[f"cm1{suffix}"] = cm1
    df[f"cm2{suffix}"] = cm2
    df[f"cm3{suffix}"] = cm3
    for m in ["cm1", "cm2", "cm3"]:
        df[f"{m}_pct{suffix}"] = (df[f"{m}{suffix}"] / net).where(net > 0)
    return df


# ---------- UI ----------
require_login()

st.markdown(
    """
<style>
@import url('https://fonts.googleapis.com/css2?family=Literata:ital,wght@0,400;0,500;0,600;1,500&display=swap');
@import url('https://api.fontshare.com/v2/css?f[]=satoshi@400,500,700&display=swap');
:root { --vana-plum:#3c1826; --vana-orange:#ff5c3e; }
html, body, [class*="css"], .stMarkdown, .stDataFrame, button, input, textarea, select,
[data-testid="stMetricLabel"] {
  font-family: 'Satoshi', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
}
h1, h2, h3, [data-testid="stMetricValue"] {
  font-family: 'Literata', Georgia, 'Times New Roman', serif !important;
  letter-spacing: -.01em;
}
h1 { color: var(--vana-plum); font-weight: 600; }
[data-testid="stMetricValue"] { color: var(--vana-plum); }
/* selected tab in the brand accent */
[data-baseweb="tab-list"] [aria-selected="true"] { color: var(--vana-orange) !important; }
[data-baseweb="tab-highlight"] { background-color: var(--vana-orange) !important; }
</style>
""",
    unsafe_allow_html=True,
)
st.title("💶 Product Pricer — Amazon")

with st.sidebar:
    st.header("Update data")
    report_file = st.file_uploader(
        "Current FBA fee preview report",
        type=["csv", "txt", "tsv", "xlsx"],
        help=(
            "Seller Central → Reports → Fulfilment by Amazon → Fee preview. "
            "Replaces prices and Amazon fees for this session; COGS is carried "
            "over from the bundled data when the report has no COGS column."
        ),
    )
    _snaps_now = fee_snapshot_files()
    _gh_token, _, _gh_branch = _github_config()
    st.caption(
        f"Baseline in use: `{_snaps_now[-1].name if _snaps_now else 'products.csv'}` · "
        + (
            f"saves are permanent (committed to `{_gh_branch}`) ✅"
            if _gh_token
            else "⚠️ saves are **temporary** — GITHUB_TOKEN is not set, so a "
            "saved report disappears when the server restarts/redeploys"
        )
    )
    st.header("Plan targets (AP26)")
    target_month = st.selectbox(
        "CM3 target month",
        ["Full year"] + MONTHS,
        key="plan_month_select",
        help=(
            "The AP26 plan sets seasonal channel-margin targets (demand-capture "
            "spend varies by month). 'Full year' uses the annual target."
        ),
    )
    plan_month = None if target_month == "Full year" else target_month
    _t = load_targets()
    with st.expander("Country targets & VAT"):
        _ref = pd.DataFrame(
            {
                "CM2": _t["cm2_target"],
                "CM3": (
                    _t["cm3_target_monthly"].get(plan_month, _t["cm3_target"])
                    if plan_month else _t["cm3_target"]
                ),
                "VAT": {c: VAT_RATES[c] for c in _t["cm2_target"]},
            }
        )
        st.dataframe(
            _ref.style.format("{:.1%}"),
            width="stretch",
            column_config={"_index": st.column_config.TextColumn("Country")},
        )
        st.caption(f"Source: {_t['source']}. VAT is fixed per country.")
    st.header("Assumptions")
    eur_to_gbp = st.number_input(
        "EUR → GBP rate (GB COGS / ad spend)", 0.5, 1.5, EUR_TO_GBP_DEFAULT, 0.01
    )
    use_elasticity = st.checkbox(
        "Model volume response to price changes",
        value=load_elasticity() is not None,
        disabled=load_elasticity() is None,
        help=(
            "Everyday price elasticity per SKU, estimated from 12 months of "
            "daily sales with promo days, ad spend and seasonality controlled "
            "(see README). Volume in the profit-impact columns then scales as "
            "(new price / current price)^elasticity instead of staying constant."
        ),
    )
    st.header("Data basis")
    _meta = load_metadata().get("marketing_spend", {})
    st.markdown(
        f"**Ad spend per unit:** {_meta.get('source', 'Amazon Ads exports')}, "
        f"**{spend_period_label()}**. "
        "Spend per unit = total ad spend ÷ total units ordered in that window."
    )

_snaps = fee_snapshot_files()
_latest_snap = _snaps[-1].name if _snaps else ""

data = None
report_info = None
if report_file is not None:
    try:
        data, report_info = load_data(
            report_file.getvalue(), report_file.name, _latest_snap
        )
    except Exception as exc:
        st.sidebar.error(f"Could not read '{report_file.name}': {exc}")
    else:
        st.sidebar.success(
            f"Using uploaded report **{report_file.name}**: "
            f"{report_info['rows']} products in "
            f"{', '.join(report_info['countries'])}."
        )
        if report_info["cogs_missing"]:
            st.sidebar.warning(
                f"{report_info['cogs_missing']} product(s) have no COGS match — "
                "their margins show as empty."
            )
        _today = pd.Timestamp.today().date().isoformat()
        _snap_name = f"products_{_today}.csv"
        if st.sidebar.button(
            "💾 Save as new baseline",
            help=(
                "Stores the uploaded report (merged with COGS) as "
                f"data/fee_history/{_snap_name} and commits it to the GitHub "
                "repo so it survives redeploys and becomes the comparison "
                "baseline."
            ),
        ):
            for level, msg in save_snapshot(data, _snap_name):
                getattr(st.sidebar, level)(msg)
            load_snapshot.clear()
            load_data.clear()
        st.sidebar.download_button(
            "⬇️ Merged snapshot CSV",
            products_csv_for_repo(data),
            file_name=_snap_name,
            mime="text/csv",
            help=(
                "Manual alternative to the save button: download and commit "
                "into data/fee_history/ yourself."
            ),
        )
if data is None:
    data, _ = load_data(snapshot=_latest_snap)

_order = list(COUNTRY_NAMES)
countries = sorted(
    data["country"].unique(),
    key=lambda c: (_order.index(c) if c in _order else len(_order), c),
)
tab_sheet, tab_calc, tab_fees, tab_new = st.tabs(
    ["📋 Country pricing sheet", "🧮 Product calculator", "📦 FBA fee changes",
     "🚀 New product"]
)


# ===== Tab 1: per-country pricing sheet =====
with tab_sheet:
    col_a, col_b = st.columns([2, 1])
    with col_a:
        country = st.selectbox(
            "Country",
            countries,
            format_func=lambda c: f"{COUNTRY_NAMES.get(c, c)} ({c})",
            key="sheet_country",
        )
    disc_key = f"portfolio_discount_{country}"
    if st.session_state.pop(f"_reset_{disc_key}", False):
        st.session_state[disc_key] = 0.0
    with col_b:
        portfolio_discount = st.number_input(
            "Target discount on all products (%)",
            0.0, 90.0, 0.0, 1.0,
            key=disc_key,
            help=(
                "Applies to every product in this country: the scenario price "
                "becomes current price × (1 − discount). Individual edits in "
                "the “New price” column override the discount per SKU."
            ),
        )
    vat = VAT_RATES.get(country, 0.20)
    cm2_target, cm3_target = country_targets(country, plan_month)
    st.caption(
        f"VAT {vat * 100:.1f}% (fixed) · plan targets for "
        f"{COUNTRY_NAMES.get(country, country)}: CM2 ≥ {cm2_target * 100:.1f}%, "
        f"CM3 ≥ {cm3_target * 100:.1f}% "
        f"({target_month if plan_month else 'full year'}, AP26). "
        "Margin cells are green at/above target, red below."
    )

    cdf = attach_elasticity(localize_costs(data[data["country"] == country], eur_to_gbp))
    cur = CURRENCY_SYMBOL.get(cdf["currency"].iloc[0], "€")

    missing_cogs = cdf["cogs"].isna().sum()
    if missing_cogs:
        st.warning(
            f"{missing_cogs} product(s) have no COGS — their margins show as empty. "
            "See the data checklist in the README."
        )

    price_key = f"price_overrides_{country}"
    overrides: dict[str, float] = st.session_state.setdefault(price_key, {})

    base = cdf.copy()
    default_price = (base["current_price"] * (1 - portfolio_discount / 100)).round(2)
    base["new_price"] = base["sku"].map(overrides).fillna(default_price)
    base = compute_margins(base, "current_price", vat, "_cur")
    base = compute_margins(base, "new_price", vat, "")
    base["price_change"] = base["new_price"] - base["current_price"]
    base["cm3_delta"] = base["cm3"] - base["cm3_cur"]
    base["status"] = pd.cut(
        base["cm3_pct"],
        bins=[-float("inf"), cm3_target, cm2_target, float("inf")],
        labels=["🔴 below CM3 target", "🟡 ok", "🟢 above CM2 target"],
    )

    # Volume-weighted impact: units sold in the ad-spend window. With the
    # elasticity toggle on, volume scales as (new/current price)^elasticity.
    if use_elasticity:
        # Only apply the elasticity response when both prices are positive; a
        # 0 price would give 0 ** (negative elasticity) = inf and blow up the
        # portfolio totals. Clamp the multiplier to a sane band as a backstop.
        valid = (base["current_price"] > 0) & (base["new_price"] > 0)
        ratio = (base["new_price"] / base["current_price"]).where(valid, 1.0)
        vol_mult = (ratio ** base["elasticity"]).replace([np.inf, -np.inf], np.nan)
        vol_mult = vol_mult.fillna(1.0).clip(lower=0.0, upper=100.0)
        base["units_new"] = base["units_window"] * vol_mult
    else:
        base["units_new"] = base["units_window"]
    base["profit_delta"] = (
        base["cm3"] * base["units_new"] - base["cm3_cur"] * base["units_window"]
    )
    base["cm3_total"] = base["cm3"] * base["units_new"]

    n_changed = int((base["price_change"].abs() > 0.004).sum())
    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("Products", len(base))
    m2.metric("Prices changed", n_changed)
    _w = base.dropna(subset=["cm3", "cm3_cur", "net", "net_cur"])
    _rev_new = (_w["net"] * _w["units_new"]).sum()
    _rev_cur = (_w["net_cur"] * _w["units_window"]).sum()
    cm3_w_new = (_w["cm3"] * _w["units_new"]).sum() / _rev_new if _rev_new > 0 else float("nan")
    cm3_w_cur = (_w["cm3_cur"] * _w["units_window"]).sum() / _rev_cur if _rev_cur > 0 else float("nan")
    m3.metric(
        "CM3 % (revenue-weighted)",
        f"{cm3_w_new * 100:.1f}%",
        delta=f"{(cm3_w_new - cm3_w_cur) * 100:+.1f} pp",
        help=(
            "Σ(CM3 € × units) ÷ Σ(net price × units) — weighted by what actually "
            "sells, not a simple average over SKUs. Units from the ad-spend window"
            + (", scaled by elasticity at the new price." if use_elasticity else ".")
        ),
    )
    m4.metric("Below CM3 target", int((base["cm3_pct"] < cm3_target).sum()))
    m5.metric(
        "Δ CM3 € at sold volume",
        f"{cur}{base['profit_delta'].sum():+,.0f}",
        help=(
            f"Change in total CM3 vs current prices, at the units sold in the "
            f"ad-spend window ({spend_period_label()}). Assumes volume stays "
            "constant — price elasticity is not modelled."
        ),
    )

    show = base[
        [
            "sku", "product_name", "status", "current_price", "new_price",
            "price_change", "cm1", "cm1_pct", "cm2", "cm2_pct",
            "cm3", "cm3_pct", "cm3_delta", "units_window", "elasticity",
            "profit_delta",
        ]
    ].reset_index(drop=True)

    # Color-code margins vs the country's plan targets (green >= target);
    # scenario-result columns get a blue tint to set them apart from base data.
    _plan_all = load_targets()["cm1_target"]
    cm1_target = _plan_all.get(country, sum(_plan_all.values()) / len(_plan_all))
    SCENARIO_COLS = ["price_change", "cm1", "cm2", "cm3", "cm3_delta", "profit_delta"]

    def _vs_target(target: float):
        def _color(v):
            if pd.isna(v):
                return ""
            return (
                "background-color: #E4F2EA" if v >= target
                else "background-color: #FBE5E5"
            )
        return _color

    styled = (
        show.style
        .set_properties(subset=SCENARIO_COLS, **{"background-color": "#E3EEF6"})
        .map(_vs_target(cm1_target), subset=["cm1_pct"])
        .map(_vs_target(cm2_target), subset=["cm2_pct"])
        .map(_vs_target(cm3_target), subset=["cm3_pct"])
    )

    with st.expander("ℹ️ How this pricing scenario works"):
        st.markdown(
            f"""
- **Edit the “✏️ New price” column** — margins, flags and totals recalculate instantly. *Reset all prices* below the table reverts to current prices and clears the discount.
- **Target discount (top right)** applies to the whole country portfolio at once (scenario price = current × (1 − discount)); individual price edits override it per SKU.
- **Green / red cells**: margin % at or above vs below the {COUNTRY_NAMES.get(country, country)} plan targets (CM1 ≥ {cm1_target * 100:.0f}%, CM2 ≥ {cm2_target * 100:.1f}%, CM3 ≥ {cm3_target * 100:.1f}%, AP26{', ' + target_month if plan_month else ''}).
- **Blue columns** are scenario results at the new price. The referral fee re-scales with the price; COGS, FBA and ad spend per unit stay fixed.
- **Δ CM3 € total** = profit impact at the units sold in the window ({spend_period_label()}){", with volume scaled by each SKU's price elasticity" if use_elasticity else " — volume held constant (elasticity toggle is off)"}.
- White columns are current data: prices and fees from the latest FBA report, ad spend from Novadata (auto-updated daily).
- **Bulk edit in Excel**: download the price sheet (Excel) below; the **New price** column is blank. Type a new price next to any SKU you want to change (leave the rest blank — e.g. `=C2*0.9` for −10%), save, and upload it again. Prices are matched by SKU; blank rows keep the current price.
"""
        )

    edited = st.data_editor(
        styled,
        key=f"editor_{country}",
        width="stretch",
        height=560,
        hide_index=True,
        disabled=[c for c in show.columns if c != "new_price"],
        column_config={
            "sku": st.column_config.TextColumn("SKU", width="small"),
            "product_name": st.column_config.TextColumn("Product", width="large"),
            "status": st.column_config.TextColumn("Status", width="small"),
            "current_price": st.column_config.NumberColumn(
                f"Current price ({cur})", format="%.2f"
            ),
            "new_price": st.column_config.NumberColumn(
                f"✏️ New price ({cur})", format="%.2f", min_value=0.0, step=0.1
            ),
            "price_change": st.column_config.NumberColumn("Δ price", format="%+.2f"),
            "cm1": st.column_config.NumberColumn(f"CM1 ({cur})", format="%.2f"),
            "cm1_pct": st.column_config.NumberColumn("CM1 %", format="percent"),
            "cm2": st.column_config.NumberColumn(f"CM2 ({cur})", format="%.2f"),
            "cm2_pct": st.column_config.NumberColumn("CM2 %", format="percent"),
            "cm3": st.column_config.NumberColumn(
                f"CM3 ({cur})",
                format="%.2f",
                help=f"After ad spend per unit — Amazon Ads, {spend_period_label()}",
            ),
            "cm3_pct": st.column_config.NumberColumn(
                "CM3 %",
                format="percent",
                help=f"After ad spend per unit — Amazon Ads, {spend_period_label()}",
            ),
            "cm3_delta": st.column_config.NumberColumn("Δ CM3", format="%+.2f"),
            "units_window": st.column_config.NumberColumn(
                "Units sold",
                format="%d",
                help=f"Units ordered in the ad-spend window ({spend_period_label()}), from Novadata",
            ),
            "elasticity": st.column_config.NumberColumn(
                "Elasticity",
                format="%.1f",
                help=(
                    "Everyday price elasticity (12m daily sales, promo days / "
                    "ads / seasonality controlled). −2 means +1% price ≈ −2% "
                    "volume. SKUs without their own estimate use the country "
                    "average."
                ),
            ),
            "profit_delta": st.column_config.NumberColumn(
                f"Δ CM3 € total",
                format="%+.0f",
                help=(
                    "Change in total CM3 vs current prices at the window's "
                    "sold volume — with the elasticity toggle on, volume "
                    "scales with the price change; otherwise held constant"
                ),
            ),
        },
    )

    # Persist edits and recompute the margin columns immediately. An edit only
    # becomes a per-SKU override when it differs from the portfolio-discount
    # default, so adjusting the discount later still moves non-edited rows.
    _defaults = dict(zip(base["sku"], default_price))
    new_overrides = {}
    for _, row in edited.iterrows():
        if pd.notna(row["new_price"]) and abs(row["new_price"] - _defaults.get(row["sku"], row["new_price"])) > 0.004:
            new_overrides[row["sku"]] = float(row["new_price"])
    if new_overrides != overrides:
        st.session_state[price_key] = new_overrides
        st.rerun()

    col_r, col_d = st.columns([1, 3])
    with col_r:
        if st.button("↩️ Reset all prices", key=f"reset_{country}"):
            st.session_state[price_key] = {}
            st.session_state[f"_reset_{disc_key}"] = True
            st.session_state.pop(f"_uploaded_{country}", None)
            st.session_state.pop(f"editor_{country}", None)
            st.rerun()
    with col_d:
        st.download_button(
            "⬇️ Download price sheet (Excel)",
            pricing_template_xlsx(show, country, cur),
            file_name=f"pricing_{country}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            help=(
                "Bulk-edit the “New price” column in Excel, then upload it below "
                "to simulate the whole portfolio at once."
            ),
        )

    uploaded = st.file_uploader(
        "⬆️ Upload edited price sheet",
        type=["xlsx", "xls", "csv"],
        key=f"upload_pricing_{country}",
        help=(
            "Re-upload the downloaded template after editing the “New price” "
            "column. Prices are matched by SKU and applied as the scenario; "
            "rows left at the current price are ignored."
        ),
    )
    _file_id = getattr(uploaded, "file_id", uploaded.name if uploaded else None)
    if uploaded is not None and st.session_state.get(f"_uploaded_{country}") != _file_id:
        try:
            up_overrides, up_info = parse_uploaded_pricing(
                uploaded, dict(zip(base["sku"], base["current_price"]))
            )
        except Exception as exc:
            st.error(f"Could not read '{uploaded.name}': {exc}")
        else:
            st.session_state[f"_uploaded_{country}"] = _file_id
            st.session_state[price_key] = up_overrides
            # Clear the data-editor's cached edit-state so the table rebuilds
            # from the uploaded prices instead of replaying stale manual edits.
            st.session_state.pop(f"editor_{country}", None)
            if up_info["changed"]:
                level = "success"
                msg = (
                    f"Applied **{uploaded.name}**: {up_info['changed']} price "
                    f"change(s) from {up_info['matched']} matched SKU(s) "
                    f"(read from “{up_info['column']}”, e.g. {up_info['sample']})."
                )
            elif up_info["wrong_column"]:
                level = "warning"
                msg = (
                    f"**{uploaded.name}**: the “New price” column was blank/"
                    "unchanged, but the **Current price** column differs from "
                    "ours on many rows — it looks like the prices were typed "
                    "into the wrong column. Put the new prices in the "
                    f"“{_new_price_header(cur)}” column and re-upload."
                )
            else:
                level = "warning"
                msg = (
                    f"**{uploaded.name}** changed no prices — "
                    f"{up_info['matched']} SKU(s) matched but the “New price” "
                    f"column was blank or equal to the current price "
                    f"({up_info['blank']} blank). Type the new prices into the "
                    f"“{_new_price_header(cur)}” column and re-upload."
                )
            if up_info["unmatched"]:
                msg += f" {up_info['unmatched']} row(s) had no matching SKU and were skipped."
            if up_info["invalid"]:
                msg += f" {up_info['invalid']} row(s) had an invalid price and were skipped."
            st.session_state[f"_upload_msg_{country}"] = (level, msg)
            st.rerun()
    if st.session_state.get(f"_upload_msg_{country}"):
        _lvl, _msg = st.session_state.pop(f"_upload_msg_{country}")
        getattr(st, _lvl)(_msg)


# ===== Tab 2: single-product calculator (scenario vs plan) =====
CALC_CSS = """
<style>
.pp-head { color:#8a7680; font-size:0.72rem; letter-spacing:0.08em;
  text-transform:uppercase; font-weight:600; margin:0.8rem 0 0.4rem 0; }
.pp-cards { display:flex; gap:12px; margin:0.2rem 0 0.6rem 0; }
.pp-card { flex:1; border:1px solid #ece2e6; border-radius:12px;
  padding:12px 14px; background:#fff; }
.pp-card .lbl { display:flex; justify-content:space-between;
  color:#8a7680; font-size:0.78rem; font-weight:600; }
.pp-card .val { font-size:1.6rem; font-weight:700; margin:2px 0; }
.pp-chip { display:inline-block; border-radius:999px; padding:1px 9px;
  font-size:0.72rem; font-weight:600; white-space:nowrap; }
.pp-g { background:#e4f2ea; color:#1e8f5a; }
.pp-a { background:#fbf1dc; color:#d98a00; }
.pp-r { background:#fbe5e5; color:#d64545; }
.pp-bar { height:5px; border-radius:3px; background:#f0e9ec;
  margin-top:8px; overflow:hidden; }
.pp-bar div { height:100%; border-radius:3px; }
.pp-tbl { width:100%; border-collapse:collapse; font-size:0.85rem; }
.pp-tbl th { color:#8a7680; font-size:0.7rem; letter-spacing:0.06em;
  text-transform:uppercase; text-align:right; font-weight:600;
  padding:6px 10px; border-bottom:1px solid #ece2e6; }
.pp-tbl th:first-child, .pp-tbl td:first-child { text-align:left; }
.pp-tbl th:last-child, .pp-tbl td:last-child { text-align:left; }
.pp-tbl td { padding:7px 10px; border-bottom:1px solid #fbf7f2;
  text-align:right; color:#5b4650; }
.pp-tbl tr.sub td { background:#f4edf0; font-weight:700; color:#3c1826; }
.pp-tbl th.cur, .pp-tbl td.cur { background:#fbf7f2; }
.pp-tbl th.sc, .pp-tbl td.sc { background:#e3eef6; }
.pp-tbl th.cur { color:#7a6670; }
.pp-tbl th.sc { color:#c53a20; }
.pp-tbl tr.sub td.cur { background:#f1e9ec; }
.pp-tbl tr.sub td.sc { background:#d8e6f1; }
.pp-rc { color:#8a7680; font-size:0.78rem; }
.pp-rc b { color:#5b4650; }
</style>
"""


def _chip(text: str, tone: str) -> str:
    return f"<span class='pp-chip pp-{tone}'>{text}</span>"


def _gap_tone(gap_pp: float) -> str:
    if gap_pp >= -3:
        return "g" if gap_pp >= 0 else "g"
    return "a" if gap_pp >= -10 else "r"


with tab_calc:
    st.markdown(CALC_CSS, unsafe_allow_html=True)
    c1, c2, c3 = st.columns([1.1, 2.6, 1.6])
    with c1:
        calc_country = st.selectbox(
            "Country",
            countries,
            format_func=lambda c: f"{COUNTRY_NAMES.get(c, c)} ({c})",
            key="calc_country",
        )
    ccdf = attach_elasticity(
        localize_costs(data[data["country"] == calc_country], eur_to_gbp)
    )
    cur = CURRENCY_SYMBOL.get(ccdf["currency"].iloc[0], "€")
    with c2:
        prod_label = st.selectbox(
            "Product",
            ccdf["sku"] + " — " + ccdf["product_name"].str.slice(0, 90),
            key="calc_product",
        )
    sku = prod_label.split(" — ")[0]
    row = ccdf[ccdf["sku"] == sku].iloc[0]

    vat = VAT_RATES.get(calc_country, 0.20)
    calc_cm2_target, calc_cm3_target = country_targets(calc_country, plan_month)
    plan = load_targets()
    _plan_c = calc_country if calc_country in plan["cm2_target"] else None
    cm1_t = plan["cm1_target"].get(_plan_c, 0.71) if _plan_c else 0.71
    cogs_t = plan["cogs_target"].get(_plan_c, 0.29) if _plan_c else 0.29
    dc_t = (
        plan["demand_capture_target"].get(_plan_c)
        if _plan_c else max(calc_cm2_target - calc_cm3_target, 0.0)
    )
    with c3:
        st.markdown(
            f"<div style='text-align:right; color:#8a7680; font-size:0.78rem; "
            f"padding-top:1.9rem;'>VAT {vat * 100:.1f}% · CM2 ≥ "
            f"{calc_cm2_target * 100:.1f}% · CM3 ≥ {calc_cm3_target * 100:.1f}%"
            f"<br>{target_month if plan_month else 'full year'} · AP26</div>",
            unsafe_allow_html=True,
        )

    st.markdown("<div class='pp-head'>Pricing scenario — edit to simulate</div>", unsafe_allow_html=True)
    i1, i2 = st.columns(2)
    with i1:
        target_price = st.number_input(
            f"Target price gross ({cur})",
            0.0, 500.0, float(row["current_price"]), 0.1,
        )
        st.caption(f"Current price: {cur}{row['current_price']:.2f}")
    with i2:
        discount = st.number_input(
            "Target discount on current price (%)", 0.0, 90.0, 0.0, 1.0,
            help="Used only if the target price equals the current price.",
        )
        st.caption("Synced with price above")
    if abs(target_price - row["current_price"]) < 0.004 and discount > 0:
        target_price = row["current_price"] * (1 - discount / 100)

    one = pd.DataFrame([row])
    one["target_price"] = target_price
    one = compute_margins(one, "current_price", vat, "_cur")
    one = compute_margins(one, "target_price", vat, "")
    r = one.iloc[0]

    net = r["net"]
    sc = {
        "cm1": (r["cm1"], r["cm1_pct"], cm1_t),
        "cm2": (r["cm2"], r["cm2_pct"], calc_cm2_target),
        "cm3": (r["cm3"], r["cm3_pct"], calc_cm3_target),
    }

    cards = []
    for name, (val, pct, tgt) in sc.items():
        pct = float(pct) if pd.notna(pct) else float("nan")
        gap_pp = (pct - tgt) * 100
        tone = _gap_tone(gap_pp)
        color = {"g": "#1e8f5a", "a": "#d98a00", "r": "#d64545"}[tone]
        bar = {"g": "#1e8f5a", "a": "#d98a00", "r": "#d64545"}[tone]
        width = max(min(pct / tgt, 1.0), 0.0) * 100 if tgt > 0 and pd.notna(pct) else 0
        cards.append(
            f"<div class='pp-card'><div class='lbl'><span>{name.upper()}</span>"
            f"<span>plan ≥{tgt * 100:.1f}%</span></div>"
            f"<div class='val' style='color:{color}'>{pct * 100:.1f}%"
            f" &nbsp;{_chip(f'{gap_pp:+.1f}pp', tone)}</div>"
            f"<div class='pp-bar'><div style='width:{width:.0f}%;"
            f"background:{bar}'></div></div></div>"
        )
    st.markdown(f"<div class='pp-cards'>{''.join(cards)}</div>", unsafe_allow_html=True)

    if (
        use_elasticity and pd.notna(r.get("elasticity"))
        and row["current_price"] > 0 and target_price > 0
    ):
        _ratio = target_price / row["current_price"]
        _vol = _ratio ** r["elasticity"]
        _units0 = float(row.get("units_window", 0) or 0)
        _delta_total = r["cm3"] * _units0 * _vol - r["cm3_cur"] * _units0
        _conf = row.get("elasticity_confidence", "")
        _conf_txt = f", {_conf} confidence" if _conf and _conf not in ("n/a", "") else ""
        st.caption(
            f"Volume response: elasticity {r['elasticity']:.2f} "
            f"({row['elasticity_method']}{_conf_txt}) → price {_ratio - 1:+.1%} ⇒ "
            f"volume {_vol - 1:+.1%} · at {_units0:.0f} units sold in the window, "
            f"total CM3 changes by {cur}{_delta_total:+,.0f}."
        )

    # --- cost waterfall: scenario vs plan, with root-cause diagnostics ---
    gap1 = (sc["cm1"][1] - cm1_t) * 100 if pd.notna(sc["cm1"][1]) else 0.0
    gap2 = (sc["cm2"][1] - calc_cm2_target) * 100 if pd.notna(sc["cm2"][1]) else 0.0
    gap3 = (sc["cm3"][1] - calc_cm3_target) * 100 if pd.notna(sc["cm3"][1]) else 0.0
    cogs_pct = r["cogs"] / net if net > 0 else float("nan")
    fba_pct = r["fba_fee"] / net if net > 0 else float("nan")
    ref_pct = r["referral"] / net if net > 0 else float("nan")
    ads_pct = r["marketing_per_unit"] / net if net > 0 else float("nan")
    ads_ok = pd.notna(ads_pct) and dc_t is not None and ads_pct <= dc_t

    # Cascading attribution: each CM gap (pp of net) splits exactly into what
    # is inherited from the level above and what this level adds on top.
    #   CM2 gap = CM1 gap + fee contribution;  CM3 gap = CM2 gap + ads contribution
    own2 = gap2 - gap1  # fees vs their plan share (negative = fees heavier)
    own3 = gap3 - gap2  # ads vs demand-capture plan (negative = ads heavier)

    def _price_for(target: float, include_ads: bool) -> float:
        """Gross price needed to hit a margin target, fees scaling with price."""
        ref_rate = float(row.get("referral_rate", 0.15) or 0.15)
        closing = float(row.get("closing_fee", 0) or 0)
        fixed = float(row["cogs"]) + float(row["fba_fee"]) + closing
        if include_ads:
            fixed += float(row["marketing_per_unit"])
        denom = (1 - target) / (1 + vat) - ref_rate
        return fixed / denom if denom > 0 else float("nan")

    if gap1 >= 0:
        rc1 = "On plan"
    elif pd.notna(cogs_pct) and (cogs_pct - cogs_t) * 100 > 2 and gap1 < -4:
        rc1 = (f"<b>Price too low or COGS heavy</b> — COGS takes "
               f"{cogs_pct * 100:.1f}% of net vs {cogs_t * 100:.0f}% plan")
    else:
        rc1 = "Price slightly low" if gap1 > -4 else "<b>Price too low</b>"

    if gap2 >= 0:
        rc2 = "On plan"
    else:
        inh = f"{gap1:+.1f}pp inherited from CM1" if gap1 < -0.05 else None
        own = (f"fees add {own2:+.1f}pp vs plan share" if own2 < -0.5
               else "FBA &amp; Referral on plan")
        if inh and own2 >= -0.5:
            rc2 = f"<b>Price too low</b> — {own} ({inh})"
        elif inh:
            rc2 = f"{inh} · <b>{own}</b>"
        else:
            rc2 = f"<b>FBA &amp; Referral above plan share</b> ({own2:+.1f}pp)"

    rc_ads = ("Ads under control" if own3 >= -0.3
              else f"<b>Ads above plan</b> ({own3:+.1f}pp vs demand-capture plan)")

    if gap3 >= 0:
        rc3 = "On plan"
    elif own3 >= -0.3:
        rc3 = (f"<b>Inherited from CM2</b> ({gap2:+.1f}pp) — not ads "
               f"(ads {own3:+.1f}pp vs plan)")
    elif gap2 >= -0.05:
        rc3 = f"<b>Ads driving the gap</b> ({own3:+.1f}pp); CM2 was on plan"
    else:
        rc3 = (f"{gap2:+.1f}pp inherited from CM2 + "
               f"<b>ads {own3:+.1f}pp above plan</b>")

    def _money(v):
        return f"{v:,.2f}"

    def _p(v):
        return f"{v * 100:.1f}%" if v is not None and pd.notna(v) else "—"

    wrows = []

    def _row(item, now_eur, now_pct, sc_eur, sc_pct, plan_pct, chip="", rc="", sub=False):
        cls = " class='sub'" if sub else ""
        wrows.append(
            f"<tr{cls}><td>{item}</td>"
            f"<td class='cur'>{now_eur}</td><td class='cur'>{now_pct}</td>"
            f"<td class='sc'>{sc_eur}</td><td class='sc'>{sc_pct}</td>"
            f"<td>{plan_pct}</td><td>{chip}</td>"
            f"<td class='pp-rc'>{rc}</td></tr>"
        )

    net_cur = r["net_cur"]

    def _pc(v):
        return _p(v / net_cur) if net_cur > 0 and pd.notna(v) else "—"

    _row("Price gross", _money(r["current_price"]), "—",
         _money(r["target_price"]), "—", "—")
    _row("Price net", _money(net_cur), "—", _money(net), "—", "—")
    _row("− COGS", _money(-r["cogs"]), _pc(r["cogs"]),
         _money(-r["cogs"]), _p(cogs_pct), _p(cogs_t))
    _row("CM1", _money(r["cm1_cur"]), _p(r["cm1_pct_cur"]),
         _money(r["cm1"]), _p(sc["cm1"][1]), _p(cm1_t),
         _chip(f"{'↑' if gap1 >= 0 else '↓'} {gap1:+.1f}pp", _gap_tone(gap1)), rc1, sub=True)
    _row("− FBA", _money(-r["fba_fee"]), _pc(r["fba_fee"]),
         _money(-r["fba_fee"]), _p(fba_pct), "—")
    _row("− Referral fee", _money(-r["referral_cur"]), _pc(r["referral_cur"]),
         _money(-r["referral"]), _p(ref_pct), "—")
    _row("CM2", _money(r["cm2_cur"]), _p(r["cm2_pct_cur"]),
         _money(r["cm2"]), _p(sc["cm2"][1]), _p(calc_cm2_target),
         _chip(f"{'↑' if gap2 >= 0 else '↓'} {gap2:+.1f}pp", _gap_tone(gap2)), rc2, sub=True)
    _row("− Ad spend *", _money(-r["marketing_per_unit"]), _pc(r["marketing_per_unit"]),
         _money(-r["marketing_per_unit"]), _p(ads_pct), _p(dc_t),
         _chip("↑ below plan", "g") if ads_ok else _chip("↓ above plan", "r"), rc_ads)
    _row("CM3", _money(r["cm3_cur"]), _p(r["cm3_pct_cur"]),
         _money(r["cm3"]), _p(sc["cm3"][1]), _p(calc_cm3_target),
         _chip(f"{'↑' if gap3 >= 0 else '↓'} {gap3:+.1f}pp", _gap_tone(gap3)), rc3, sub=True)

    st.markdown(
        "<div class='pp-head'>Cost waterfall — scenario vs plan</div>"
        f"<table class='pp-tbl'><tr><th>Item</th><th class='cur'>Current ({cur})</th>"
        f"<th class='cur'>Current %</th><th class='sc'>Scenario ({cur})</th>"
        f"<th class='sc'>Scenario %</th><th>Plan %</th>"
        f"<th>Δ vs plan</th><th>Root cause</th></tr>{''.join(wrows)}</table>",
        unsafe_allow_html=True,
    )
    max_ads = r["cm2"] - calc_cm3_target * net
    st.caption(
        f"\\* Amazon Ads spend ÷ units ordered, {spend_period_label()}. "
        f"Headroom to CM3 target: up to {cur}{max(max_ads, 0):.2f}/unit "
        f"({max(max_ads / net, 0) * 100:.1f}% of net) for ads + discounts at this price. "
        "Plan benchmarks are percentages of net price (AP26)."
    )

    # One actionable conclusion from the cascade.
    if gap3 >= 0 and gap2 >= 0:
        st.success("On plan at this price — no action needed.")
    elif gap3 < 0 and own3 >= -0.3:
        _need = max(_price_for(calc_cm2_target, False), _price_for(calc_cm3_target, True))
        _extra = ""
        if pd.notna(_need) and use_elasticity and pd.notna(r.get("elasticity")) and row["current_price"] > 0:
            _vf = (_need / row["current_price"]) ** r["elasticity"]
            _extra = f" Expected volume effect at that price: {_vf - 1:+.1%}."
        st.warning(
            f"**Root cause: price, not ads.** Ads are {own3:+.1f}pp vs plan — "
            f"cutting them won't close the gap. Raising the gross price to "
            f"≥ {cur}{_need:.2f} hits both CM2 and CM3 targets.{_extra}"
        )
    elif gap3 < 0 and gap2 >= -0.05:
        st.warning(
            f"**Root cause: ad spend** ({own3:+.1f}pp above the demand-capture "
            f"plan). Margins above CM2 are on plan — fix is ads efficiency, "
            f"not price."
        )
    elif gap3 < 0:
        _need = max(_price_for(calc_cm2_target, False), _price_for(calc_cm3_target, True))
        st.warning(
            f"**Root cause: both price and ads.** {gap2:+.1f}pp comes in below "
            f"CM2 plan and ads add {own3:+.1f}pp on top. Price needed for "
            f"target: ≥ {cur}{_need:.2f}, plus ads efficiency."
        )


# ===== Tab 3: FBA fee changes (current vs previous fee report version) =====
with tab_fees:
    snaps = fee_snapshot_files()
    pair = None  # (old_df, new_df, old_label, new_label)
    if report_info is not None and snaps:
        pair = (
            load_snapshot(str(snaps[-1])),
            data,
            f"`{snaps[-1].name}` (committed baseline)",
            f"uploaded report `{report_file.name}` "
            f"({pd.Timestamp.today().date().isoformat()})",
        )
    elif len(snaps) >= 2:
        pair = (
            load_snapshot(str(snaps[-2])),
            load_snapshot(str(snaps[-1])),
            f"`{snaps[-2].name}` (previous version)",
            f"`{snaps[-1].name}` (current version)",
        )

    if pair is None:
        st.info(
            "Only one fee report version is on file "
            f"(`{snaps[-1].name if snaps else 'data/products.csv'}`). "
            "Upload the current FBA fee preview report in the sidebar "
            "(→ Update data), or commit a second dated snapshot to "
            "`data/fee_history/`, to see FBA fee changes here."
        )
    else:
        old_full, new_full, old_label, new_label = pair
        st.markdown(f"**Comparing:** {new_label} **vs** {old_label}")
        old = old_full[
            ["country", "sku", "product_name", "currency", "fba_fee", "current_price"]
        ]
        new = new_full[["country", "sku", "product_name", "currency", "fba_fee", "current_price"]]
        cmp = old.merge(
            new, on=["country", "sku"], how="outer",
            suffixes=("_old", "_new"), indicator=True,
        )
        cmp["fba_delta"] = cmp["fba_fee_new"] - cmp["fba_fee_old"]
        cmp["fba_delta_pct"] = (cmp["fba_delta"] / cmp["fba_fee_old"]).where(
            cmp["fba_fee_old"] > 0
        )

        def _status(row):
            if row["_merge"] == "right_only":
                return "🆕 new product"
            if row["_merge"] == "left_only":
                return "❌ not in report"
            if pd.isna(row["fba_delta"]) or abs(row["fba_delta"]) < 0.005:
                return "➖ unchanged"
            return "🔺 increase" if row["fba_delta"] > 0 else "🔻 decrease"

        cmp["status"] = cmp.apply(_status, axis=1)
        changed = cmp[cmp["status"].isin(["🔺 increase", "🔻 decrease"])]

        f1, f2, f3, f4, f5 = st.columns(5)
        f1.metric("Compared", int((cmp["_merge"] == "both").sum()))
        f2.metric("Fee increases", int((cmp["status"] == "🔺 increase").sum()))
        f3.metric("Fee decreases", int((cmp["status"] == "🔻 decrease").sum()))
        avg_delta = changed["fba_delta"].mean()
        f4.metric(
            "Avg change (changed only)",
            f"{avg_delta:+.2f}" if pd.notna(avg_delta) else "—",
        )
        f5.metric(
            "New / missing",
            f"{int((cmp['_merge'] == 'right_only').sum())} / "
            f"{int((cmp['_merge'] == 'left_only').sum())}",
        )

        g1, g2 = st.columns([1, 2])
        with g1:
            fee_countries = st.multiselect(
                "Countries",
                sorted(cmp["country"].unique()),
                default=sorted(cmp["country"].unique()),
                key="fee_countries",
            )
        with g2:
            only_changes = st.checkbox(
                "Show only changes (hide unchanged)", value=True, key="fee_only_changes"
            )

        view = cmp[cmp["country"].isin(fee_countries)]
        if only_changes:
            view = view[view["status"] != "➖ unchanged"]
        view = view.sort_values("fba_delta", key=lambda s: s.abs(), ascending=False)

        if changed.empty:
            st.success("No FBA fee changes between the uploaded report and the baseline.")
        else:
            top = changed.reindex(
                changed["fba_delta"].abs().sort_values(ascending=False).index
            ).head(12)
            fig = go.Figure(
                go.Bar(
                    y=(top["sku"] + " (" + top["country"] + ")")[::-1],
                    x=top["fba_delta"][::-1],
                    orientation="h",
                    marker_color=[
                        "#D64545" if v > 0 else "#1E8F5A" for v in top["fba_delta"][::-1]
                    ],
                    text=[f"{v:+.2f}" for v in top["fba_delta"][::-1]],
                    textposition="outside",
                )
            )
            fig.update_layout(
                title="Largest FBA fee changes (per unit)",
                height=max(260, 36 * len(top) + 80),
                margin={"t": 50, "b": 10, "l": 10, "r": 40},
                xaxis={"title": None},
            )
            st.plotly_chart(fig, width="stretch")

        show_fees = view[
            [
                "country", "sku", "status", "product_name_new", "fba_fee_old",
                "fba_fee_new", "fba_delta", "fba_delta_pct", "current_price_old",
                "current_price_new",
            ]
        ].copy()
        show_fees["product_name_new"] = show_fees["product_name_new"].fillna(
            view["product_name_old"]
        )

        def _delta_color(v):
            if pd.isna(v) or abs(v) < 0.005:
                return ""
            return "background-color: #FBE5E5" if v > 0 else "background-color: #E4F2EA"

        st.dataframe(
            show_fees.style.map(_delta_color, subset=["fba_delta", "fba_delta_pct"]),
            hide_index=True,
            width="stretch",
            height=520,
            column_config={
                "country": st.column_config.TextColumn("Country", width="small"),
                "sku": st.column_config.TextColumn("SKU", width="small"),
                "status": st.column_config.TextColumn("Status", width="small"),
                "product_name_new": st.column_config.TextColumn("Product", width="large"),
                "fba_fee_old": st.column_config.NumberColumn("FBA fee (baseline)", format="%.2f"),
                "fba_fee_new": st.column_config.NumberColumn("FBA fee (report)", format="%.2f"),
                "fba_delta": st.column_config.NumberColumn(
                    "Δ FBA fee",
                    format="%+.2f",
                    help="Per unit, in the marketplace currency. Hits CM2 and CM3 1:1.",
                ),
                "fba_delta_pct": st.column_config.NumberColumn("Δ %", format="percent"),
                "current_price_old": st.column_config.NumberColumn("Price (baseline)", format="%.2f"),
                "current_price_new": st.column_config.NumberColumn("Price (report)", format="%.2f"),
            },
        )
        st.caption(
            f"Comparing {new_label} against {old_label}. Fees are per unit in "
            "the marketplace currency; a fee increase reduces CM2 and CM3 by "
            "the same amount. To record a new version, upload the report and "
            "commit the merged snapshot from the sidebar into `data/fee_history/`."
        )
        st.download_button(
            "⬇️ Download fee comparison (CSV)",
            show_fees.to_csv(index=False).encode("utf-8"),
            file_name="fba_fee_changes.csv",
            mime="text/csv",
        )


# ===== Tab 4: New product (COGS input, FBA estimated, price to hit target) =====
with tab_new:
    st.markdown(
        "Price a product that isn't live yet. Enter its **COGS**, pick a "
        "**form factor** (its Amazon FBA fee is estimated from comparable "
        "products), choose the margin bar, and the tool solves the price you "
        "need in each country."
    )
    brands = load_brand_targets().get("brands", {})
    fba_lut = load_fba_lut()
    forms = list(fba_lut.index) if not fba_lut.empty else ["— any (marketplace median)"]

    c1, c2, c3 = st.columns(3)
    with c1:
        np_cogs = st.number_input("COGS (€ / unit)", 0.0, 500.0, 7.30, 0.10)
        np_form = st.selectbox("Form factor (estimates FBA)", forms)
    with c2:
        np_ref = st.number_input("Referral fee (%)", 0.0, 45.0, 15.0, 0.5,
                                 help="Amazon referral fee. Supplements are a flat 15%.") / 100
        basis = st.radio(
            "Hold to",
            ["Country plan", "Brand plan"],
            horizontal=True,
            help="Country plan = the company's per-country CM targets. Brand "
                 "plan = the AP26 brand GP targets (e.g. Wowtamins runs richer).",
        )
    with c3:
        np_brand = st.selectbox(
            "Brand", list(brands) or ["—"],
            index=(list(brands).index("Wowtamins") if "Wowtamins" in brands else 0),
            disabled=basis == "Country plan",
        )
        np_anchor = st.number_input(
            "Anchor / competitor price (€, optional)", 0.0, 500.0, 0.0, 0.1,
            help="A comparable product's price. If the required price is above "
                 "it, the product can't hit target at this COGS.",
        )
        np_fba_adj = st.number_input(
            "FBA adjustment (€)", -5.0, 10.0, 0.0, 0.1,
            help="Nudge the estimated FBA fee up/down (e.g. a heavier pack).",
        )

    # Which margin level and target value per country.
    if basis == "Brand plan" and np_brand in brands:
        gp2_t = brands[np_brand]["gp2"]
        gp3_t = brands[np_brand]["gp3"]
        bar_label = f"{np_brand} brand plan (GP2 {gp2_t*100:.1f}% · GP3 {gp3_t*100:.1f}%)"
    else:
        gp2_t = gp3_t = None  # per-country below
        bar_label = "Country plan (per-country CM2 / CM3)"
    st.caption(f"Target basis: **{bar_label}**. Referral {np_ref*100:.1f}%, "
               f"COGS €{np_cogs:.2f}. FBA is estimated per country from “{np_form}”.")

    rows = []
    gb_fx = eur_to_gbp
    for c in countries:
        vat_c = VAT_RATES.get(c, 0.20)
        # FBA estimate: form-factor value, fall back to marketplace median.
        fba = np.nan
        if not fba_lut.empty and c in fba_lut.columns:
            fba = fba_lut.loc[np_form, c] if np_form in fba_lut.index else np.nan
            if pd.isna(fba) and "— any (marketplace median)" in fba_lut.index:
                fba = fba_lut.loc["— any (marketplace median)", c]
        if pd.isna(fba):
            continue
        fba = float(fba) + np_fba_adj
        cur_c = "£" if c == "GB" else "€"
        # GB fees/prices are GBP; COGS is EUR -> convert to GBP for a GB listing.
        cogs_c = np_cogs * gb_fx if c == "GB" else np_cogs
        cc2, cc3 = (country_targets(c, plan_month) if basis == "Country plan"
                    else (gp2_t, gp3_t))
        p_cm2 = required_price(cogs_c, fba, vat_c, np_ref, cc2)
        p_cm3 = required_price(cogs_c, fba, vat_c, np_ref, cc3)
        need = max(p_cm2, p_cm3)
        anchor_c = (np_anchor * gb_fx if c == "GB" else np_anchor) if np_anchor > 0 else np.nan
        verdict = ""
        if np_anchor > 0 and np.isfinite(need):
            verdict = "🟢 fits" if need <= anchor_c + 1e-6 else "🔴 too high"
        rows.append({
            "country": f"{COUNTRY_NAMES.get(c, c)} ({c})", "_c": c, "cur": cur_c,
            "vat": vat_c * 100, "fba": fba,
            "price_cm2": p_cm2, "price_cm3": p_cm3, "required": need,
            "anchor": anchor_c if np_anchor > 0 else np.nan, "verdict": verdict,
        })
    res = pd.DataFrame(rows)
    if res.empty:
        st.info("No FBA reference available for this form factor.")
    else:
        show = res[["country", "vat", "fba", "price_cm2", "price_cm3",
                    "required", "anchor", "verdict"]]
        st.dataframe(
            show, hide_index=True, width="stretch",
            column_config={
                "country": st.column_config.TextColumn("Country"),
                "vat": st.column_config.NumberColumn("VAT %", format="%.1f"),
                "fba": st.column_config.NumberColumn("FBA ≈", format="%.2f",
                    help="Estimated fulfilment fee from comparable products"),
                "price_cm2": st.column_config.NumberColumn("Price for CM2/GP2", format="%.2f"),
                "price_cm3": st.column_config.NumberColumn("Price for CM3/GP3", format="%.2f"),
                "required": st.column_config.NumberColumn("Required price", format="%.2f",
                    help="The higher of the two targets, in the marketplace currency"),
                "anchor": st.column_config.NumberColumn("Anchor", format="%.2f"),
                "verdict": st.column_config.TextColumn("vs anchor"),
            },
        )
        st.caption(
            "Required price is the gross price that meets **both** targets "
            "(referral scales with price; FBA, COGS fixed). GB shown in £ with "
            f"COGS converted at {gb_fx:.2f}. Prices are estimates until a real "
            "FBA fee preview exists for the live listing."
        )

        # Detail: waterfall for one country at a chosen price.
        st.markdown("##### Check a specific price")
        d1, d2 = st.columns([1, 2])
        with d1:
            det_c = st.selectbox("Country", res["_c"].tolist(),
                                 format_func=lambda c: COUNTRY_NAMES.get(c, c))
        drow = res[res["_c"] == det_c].iloc[0]
        cur_d = drow["cur"]
        with d2:
            default_p = float(drow["required"]) if np.isfinite(drow["required"]) else 20.0
            det_price = st.number_input(
                f"Price to test ({cur_d})", 0.0, 500.0, round(default_p, 2), 0.1)
        vat_d = drow["vat"] / 100
        fba_d = drow["fba"]
        cogs_d = np_cogs * gb_fx if det_c == "GB" else np_cogs
        cc2, cc3 = (country_targets(det_c, plan_month) if basis == "Country plan"
                    else (gp2_t, gp3_t))
        cm1_t = load_targets()["cm1_target"].get(det_c, 0.71) if basis == "Country plan" \
            else brands.get(np_brand, {}).get("gp1", 0.71)
        net = det_price / (1 + vat_d)
        referral = det_price * np_ref
        cm1 = net - cogs_d
        cm2 = cm1 - fba_d - referral
        ads_head = cm2 - cc3 * net
        m = st.columns(4)
        m[0].metric("Net", f"{cur_d}{net:.2f}")
        m[1].metric("CM1 / GP1", f"{cm1/net*100:.1f}%",
                    delta=f"{(cm1/net - cm1_t)*100:+.1f}pp vs {cm1_t*100:.0f}%")
        m[2].metric("CM2 / GP2", f"{cm2/net*100:.1f}%",
                    delta=f"{(cm2/net - cc2)*100:+.1f}pp vs {cc2*100:.1f}%")
        m[3].metric("Ad headroom to CM3/GP3", f"{cur_d}{max(ads_head,0):.2f}",
                    help=f"Per unit for ads + discounts before CM3/GP3 target "
                         f"{cc3*100:.1f}% is breached ({max(ads_head/net,0)*100:.1f}% of net)")
        breakeven_cogs = net * (1 - cc2) - fba_d - referral
        be_txt = (f"{cur_d}{breakeven_cogs:.2f}" if np.isfinite(breakeven_cogs) else "—")
        cogs_shown = cogs_d
        st.caption(
            f"At {cur_d}{det_price:.2f}: to still meet CM2/GP2 the COGS could be up to "
            f"**{be_txt}** (this product: {cur_d}{cogs_shown:.2f}). "
            + ("✅ CM2/GP2 target met." if cm2/net >= cc2 else
               f"⚠️ CM2/GP2 short by {(cc2 - cm2/net)*100:.1f}pp — raise price or cut COGS.")
        )
