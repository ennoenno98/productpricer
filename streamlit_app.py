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
def load_data(report: bytes | None = None, report_name: str = "") -> tuple[pd.DataFrame, dict]:
    """Build the pricing dataset; an uploaded fee report replaces the bundled one.

    Returns (dataset, info). `info` describes the uploaded report merge
    (rows, countries, COGS matches) and is empty for the bundled data.
    """
    bundled = pd.read_csv(DATA_DIR / "products.csv").rename(columns=COLUMN_RENAMES)
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
    products = products.merge(
        spend[["country", "sku", "spend_per_unit"]], on=["country", "sku"], how="left"
    )
    # No ads recorded for the SKU -> 0, matching the XLOOKUP(...,0) in Excel.
    products["spend_per_unit_eur"] = products["spend_per_unit"].fillna(0.0)

    # Referral fee scales with price: derive the rate from Amazon's estimate
    # at the current price so it can be re-applied to any simulated price.
    products["referral_rate"] = (
        products["referral_fee"] / products["current_price"]
    ).where(products["current_price"] > 0, 0.15)
    return products, info


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
    st.header("Data basis")
    _meta = load_metadata().get("marketing_spend", {})
    st.markdown(
        f"**Ad spend per unit:** {_meta.get('source', 'Amazon Ads exports')}, "
        f"**{spend_period_label()}**. "
        "Spend per unit = total ad spend ÷ total units ordered in that window."
    )

data = None
report_info = None
if report_file is not None:
    try:
        data, report_info = load_data(report_file.getvalue(), report_file.name)
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
        st.sidebar.download_button(
            "⬇️ Merged products.csv",
            products_csv_for_repo(data),
            file_name="products.csv",
            mime="text/csv",
            help=(
                "The uploaded report combined with COGS. Commit this as "
                "data/products.csv to make the update permanent — uploads only "
                "last for the current session."
            ),
        )
if data is None:
    data, _ = load_data()

_order = list(COUNTRY_NAMES)
countries = sorted(
    data["country"].unique(),
    key=lambda c: (_order.index(c) if c in _order else len(_order), c),
)
tab_sheet, tab_calc, tab_fees = st.tabs(
    ["📋 Country pricing sheet", "🧮 Product calculator", "📦 FBA fee changes"]
)


# ===== Tab 1: per-country pricing sheet =====
with tab_sheet:
    country = st.selectbox(
        "Country",
        countries,
        format_func=lambda c: f"{COUNTRY_NAMES.get(c, c)} ({c})",
        key="sheet_country",
    )
    vat = VAT_RATES.get(country, 0.20)
    cm2_target, cm3_target = country_targets(country, plan_month)
    st.caption(
        f"VAT {vat * 100:.1f}% (fixed) · plan targets for "
        f"{COUNTRY_NAMES.get(country, country)}: CM2 ≥ {cm2_target * 100:.1f}%, "
        f"CM3 ≥ {cm3_target * 100:.1f}% "
        f"({target_month if plan_month else 'full year'}, AP26)."
    )

    cdf = localize_costs(data[data["country"] == country], eur_to_gbp)
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
    base["new_price"] = base["sku"].map(overrides).fillna(base["current_price"])
    base = compute_margins(base, "current_price", vat, "_cur")
    base = compute_margins(base, "new_price", vat, "")
    base["price_change"] = base["new_price"] - base["current_price"]
    base["cm3_delta"] = base["cm3"] - base["cm3_cur"]
    base["status"] = pd.cut(
        base["cm3_pct"],
        bins=[-float("inf"), cm3_target, cm2_target, float("inf")],
        labels=["🔴 below CM3 target", "🟡 ok", "🟢 above CM2 target"],
    )

    n_changed = int((base["price_change"].abs() > 0.004).sum())
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Products", len(base))
    m2.metric("Prices changed", n_changed)
    m3.metric(
        "Avg CM3 % (new)",
        f"{base['cm3_pct'].mean() * 100:.1f}%",
        delta=f"{(base['cm3_pct'].mean() - base['cm3_pct_cur'].mean()) * 100:+.1f} pp",
    )
    m4.metric("Below CM3 target", int((base["cm3_pct"] < cm3_target).sum()))

    show = base[
        [
            "sku", "product_name", "status", "current_price", "new_price",
            "price_change", "cm1", "cm1_pct", "cm2", "cm2_pct",
            "cm3", "cm3_pct", "cm3_delta",
        ]
    ].reset_index(drop=True)

    edited = st.data_editor(
        show,
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
        },
    )

    # Persist edits and recompute the margin columns immediately.
    new_overrides = {}
    for _, row in edited.iterrows():
        if pd.notna(row["new_price"]) and abs(row["new_price"] - base.loc[base["sku"] == row["sku"], "current_price"].iloc[0]) > 0.004:
            new_overrides[row["sku"]] = float(row["new_price"])
    if new_overrides != overrides:
        st.session_state[price_key] = new_overrides
        st.rerun()

    col_r, col_d = st.columns([1, 3])
    with col_r:
        if st.button("↩️ Reset all prices", key=f"reset_{country}"):
            st.session_state[price_key] = {}
            st.rerun()
    with col_d:
        st.download_button(
            "⬇️ Download price sheet (CSV)",
            show.to_csv(index=False).encode("utf-8"),
            file_name=f"pricing_{country}.csv",
            mime="text/csv",
        )


# ===== Tab 2: single-product calculator (mirrors "Margen Calc AMZ") =====
with tab_calc:
    c1, c2 = st.columns([1, 3])
    with c1:
        calc_country = st.selectbox(
            "Country",
            countries,
            format_func=lambda c: f"{COUNTRY_NAMES.get(c, c)} ({c})",
            key="calc_country",
        )
    ccdf = localize_costs(data[data["country"] == calc_country], eur_to_gbp)
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
    st.caption(
        f"VAT {vat * 100:.1f}% (fixed) · plan targets for "
        f"{COUNTRY_NAMES.get(calc_country, calc_country)}: CM2 ≥ "
        f"{calc_cm2_target * 100:.1f}%, CM3 ≥ {calc_cm3_target * 100:.1f}% "
        f"({target_month if plan_month else 'full year'}, AP26)."
    )

    i1, i2 = st.columns(2)
    with i1:
        target_price = st.number_input(
            f"Target price gross ({cur})",
            0.0, 500.0, float(row["current_price"]), 0.1,
        )
    with i2:
        discount = st.number_input(
            "Target discount on current price (%)", 0.0, 90.0, 0.0, 1.0,
            help="Used only if the target price equals the current price.",
        )
    if abs(target_price - row["current_price"]) < 0.004 and discount > 0:
        target_price = row["current_price"] * (1 - discount / 100)

    one = pd.DataFrame([row])
    one["target_price"] = target_price
    one = compute_margins(one, "current_price", vat, "_cur")
    one = compute_margins(one, "target_price", vat, "")
    r = one.iloc[0]

    _plan_c = calc_country if calc_country in plan["cm2_target"] else None
    plan_pct = {
        "cogs": -plan["cogs_target"].get(_plan_c) if _plan_c else None,
        "cm1": plan["cm1_target"].get(_plan_c) if _plan_c else None,
        "cm2": calc_cm2_target,
        "ads": -plan["demand_capture_target"].get(_plan_c) if _plan_c else None,
        "cm3": calc_cm3_target,
    }
    lines = [
        ("Price gross", r["target_price"], None, r["current_price"], None, None),
        ("Price net", r["net"], None, r["net_cur"], None, None),
        ("− Product cost (COGS)", -r["cogs"], None, -r["cogs"], None, plan_pct["cogs"]),
        ("CM1", r["cm1"], r["cm1_pct"], r["cm1_cur"], r["cm1_pct_cur"], plan_pct["cm1"]),
        ("− Amazon FBA fulfilment", -r["fba_fee"], None, -r["fba_fee"], None, None),
        ("− Amazon referral fee", -r["referral"], None, -r["referral_cur"], None, None),
        ("CM2", r["cm2"], r["cm2_pct"], r["cm2_cur"], r["cm2_pct_cur"], plan_pct["cm2"]),
        ("− Ad spend / unit *", -r["marketing_per_unit"], None, -r["marketing_per_unit"], None, plan_pct["ads"]),
        ("CM3", r["cm3"], r["cm3_pct"], r["cm3_cur"], r["cm3_pct_cur"], plan_pct["cm3"]),
    ]

    def _pct(v):
        return f"{v * 100:.1f}%" if v is not None and pd.notna(v) else ""

    table = pd.DataFrame(
        {
            "Item": [l[0] for l in lines],
            f"Target ({cur})": [l[1] for l in lines],
            "Target %": [_pct(l[2]) for l in lines],
            f"Current ({cur})": [l[3] for l in lines],
            "Current %": [_pct(l[4]) for l in lines],
            "Plan %": [_pct(l[5]) for l in lines],
        }
    )

    k1, k2, k3 = st.columns(3)
    for col, label in ((k1, "cm1"), (k2, "cm2"), (k3, "cm3")):
        pct = r[f"{label}_pct"] * 100 if pd.notna(r[f"{label}_pct"]) else float("nan")
        col.metric(
            label.upper(),
            f"{cur}{r[label]:.2f}  ·  {pct:.1f}%",
            delta=f"{r[label] - r[f'{label}_cur']:+.2f} vs current",
        )

    t1, t2 = st.columns([1.2, 1])
    with t1:
        st.dataframe(
            table,
            hide_index=True,
            width="stretch",
            column_config={
                f"Target ({cur})": st.column_config.NumberColumn(format="%.2f"),
                f"Current ({cur})": st.column_config.NumberColumn(format="%.2f"),
            },
        )
        st.caption(f"\\* Amazon Ads spend ÷ units ordered, {spend_period_label()}.")
        max_ads = r["cm2"] - calc_cm3_target * r["net"]
        st.info(
            f"**Headroom to CM3 target ({calc_cm3_target * 100:.1f}%):** at this price you "
            f"can spend up to **{cur}{max(max_ads, 0):.2f}** per unit "
            f"({max(max_ads / r['net'], 0) * 100:.1f}% of net) on ads + discounts."
        )
        if pd.notna(r["cm2_pct"]) and r["cm2_pct"] < calc_cm2_target:
            st.error(
                f"CM2 ({r['cm2_pct'] * 100:.1f}%) is below the country's plan "
                f"target of {calc_cm2_target * 100:.1f}%."
            )

    with t2:
        # Where the net price goes: cost stack vs what's left, current vs target.
        segments = [
            ("COGS", "#8496B0", [r["cogs"], r["cogs"]]),
            ("FBA", "#B4C7E7", [r["fba_fee"], r["fba_fee"]]),
            ("Referral", "#D6DCE5", [r["referral_cur"], r["referral"]]),
            ("Ad spend", "#F4B183", [r["marketing_per_unit"], r["marketing_per_unit"]]),
            ("CM3", None, [r["cm3_cur"], r["cm3"]]),
        ]
        scenarios = ["Current", "Target"]
        fig = go.Figure()
        for name, color, values in segments:
            colors = (
                ["#70AD47" if v >= 0 else "#C00000" for v in values]
                if name == "CM3"
                else color
            )
            fig.add_bar(
                y=scenarios,
                x=values,
                name=name,
                orientation="h",
                marker_color=colors,
                text=[f"{v:.2f}" for v in values],
                textposition="inside",
                insidetextanchor="middle",
                textangle=0,
            )
        fig.update_layout(
            barmode="relative",
            title=f"Net price breakdown ({cur}/unit)",
            height=280,
            margin={"t": 50, "b": 10, "l": 10, "r": 10},
            legend={"orientation": "h", "y": -0.15},
            xaxis={"title": None},
            yaxis={"title": None, "categoryorder": "array", "categoryarray": ["Target", "Current"]},
        )
        st.plotly_chart(fig, width="stretch")
        st.caption(
            f"Net price = costs + CM3 (green = profit, red = loss). "
            f"Ad spend per unit: Amazon Ads, {spend_period_label()}."
        )

# ===== Tab 3: FBA fee changes (uploaded report vs bundled baseline) =====
with tab_fees:
    if report_info is None:
        st.info(
            "Upload the **current FBA fee preview report** in the sidebar "
            "(→ Update data) to compare its fees against the baseline bundled "
            "in the repo (`data/products.csv`) and see which products got more "
            "or less expensive to fulfil."
        )
    else:
        baseline, _ = load_data()
        old = baseline[
            ["country", "sku", "product_name", "currency", "fba_fee", "current_price"]
        ]
        new = data[["country", "sku", "product_name", "currency", "fba_fee", "current_price"]]
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
                        "#C00000" if v > 0 else "#70AD47" for v in top["fba_delta"][::-1]
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
            return "background-color: #FBDBD7" if v > 0 else "background-color: #DEEDD3"

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
            "Baseline: `data/products.csv` in the repo. Fees are per unit in the "
            "marketplace currency; a fee increase reduces CM2 and CM3 by the same "
            "amount. Download the merged products.csv in the sidebar and commit it "
            "to make the uploaded report the new baseline."
        )
        st.download_button(
            "⬇️ Download fee comparison (CSV)",
            show_fees.to_csv(index=False).encode("utf-8"),
            file_name="fba_fee_changes.csv",
            mime="text/csv",
        )

st.caption(
    "CM1 = net price − COGS · CM2 = CM1 − FBA − referral fee · CM3 = CM2 − ad spend per unit "
    f"(Amazon Ads, {spend_period_label()}). "
    "Referral fees are recalculated from the simulated price (rate derived from Amazon's fee "
    "preview); FBA fees and ad spend per unit are held constant. Margins are % of net price. "
    "VAT is fixed per country; margin targets are country-specific from the AP26 plan "
    "('Amazon Margins' tab)."
)
