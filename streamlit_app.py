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
}

# VAT on food supplements per marketplace (from "SQL calc measures general").
VAT_DEFAULTS = {
    "DE": 0.07,
    "GB": 0.20,
    "FR": 0.055,
    "IT": 0.10,
    "ES": 0.10,
    "NL": 0.09,
    "BE": 0.06,
    "IE": 0.135,
}

EUR_TO_GBP_DEFAULT = 0.83  # COGS / ad spend are in EUR, GB prices in GBP
CM2_TARGET_DEFAULT = 0.35  # "marge2_target" from the workbook
CM3_TARGET_DEFAULT = 0.197  # CM3 target used in "Margen Calc AMZ"

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
@st.cache_data(show_spinner=False)
def load_data() -> pd.DataFrame:
    products = pd.read_csv(DATA_DIR / "products.csv")
    spend = pd.read_csv(DATA_DIR / "marketing_spend.csv")

    products = products.rename(
        columns={
            "amazon-store": "country",
            "product-name": "product_name",
            "your-price": "current_price",
            "estimated-referral-fee-per-unit": "referral_fee",
            "estimated-variable-closing-fee": "closing_fee",
            "expected-domestic-fulfilment-fee-per-unit": "fba_fee",
            "COGS": "cogs_eur",
        }
    )
    for col in ["current_price", "referral_fee", "closing_fee", "fba_fee", "cogs_eur"]:
        products[col] = pd.to_numeric(products[col], errors="coerce")

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
    return products


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

data = load_data()

with st.sidebar:
    st.header("Assumptions")
    cm2_target = st.number_input(
        "CM2 target (%)", 0.0, 100.0, CM2_TARGET_DEFAULT * 100, 0.5
    ) / 100
    cm3_target = st.number_input(
        "CM3 target (%)", 0.0, 100.0, CM3_TARGET_DEFAULT * 100, 0.5
    ) / 100
    eur_to_gbp = st.number_input(
        "EUR → GBP rate (GB COGS / ad spend)", 0.5, 1.5, EUR_TO_GBP_DEFAULT, 0.01
    )
    st.caption(
        "VAT per country can be adjusted below. Defaults are the food-supplement "
        "rates from the margin workbook."
    )

countries = sorted(data["country"].unique(), key=list(COUNTRY_NAMES).index)
tab_sheet, tab_calc = st.tabs(["📋 Country pricing sheet", "🧮 Product calculator"])


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
    vat_default = VAT_DEFAULTS.get(country, 0.20)
    with col_b:
        vat = st.number_input(
            "VAT (%)", 0.0, 30.0, vat_default * 100, 0.5, key=f"vat_{country}"
        ) / 100

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
            "cm3": st.column_config.NumberColumn(f"CM3 ({cur})", format="%.2f"),
            "cm3_pct": st.column_config.NumberColumn("CM3 %", format="percent"),
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

    vat = st.number_input(
        "VAT (%)", 0.0, 30.0, VAT_DEFAULTS.get(calc_country, 0.20) * 100, 0.5,
        key=f"calc_vat_{calc_country}",
    ) / 100

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

    lines = [
        ("Price gross", r["target_price"], None, r["current_price"], None),
        ("Price net", r["net"], None, r["net_cur"], None),
        ("− Product cost (COGS)", -r["cogs"], None, -r["cogs"], None),
        ("CM1", r["cm1"], r["cm1_pct"], r["cm1_cur"], r["cm1_pct_cur"]),
        ("− Amazon FBA fulfilment", -r["fba_fee"], None, -r["fba_fee"], None),
        ("− Amazon referral fee", -r["referral"], None, -r["referral_cur"], None),
        ("CM2", r["cm2"], r["cm2_pct"], r["cm2_cur"], r["cm2_pct_cur"]),
        ("− Marketing spend / unit", -r["marketing_per_unit"], None, -r["marketing_per_unit"], None),
        ("CM3", r["cm3"], r["cm3_pct"], r["cm3_cur"], r["cm3_pct_cur"]),
    ]
    table = pd.DataFrame(
        {
            "Item": [l[0] for l in lines],
            f"Target ({cur})": [l[1] for l in lines],
            "Target %": [f"{l[2] * 100:.1f}%" if l[2] is not None and pd.notna(l[2]) else "" for l in lines],
            f"Current ({cur})": [l[3] for l in lines],
            "Current %": [f"{l[4] * 100:.1f}%" if l[4] is not None and pd.notna(l[4]) else "" for l in lines],
        }
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
        max_ads = r["cm2"] - cm3_target * r["net"]
        st.info(
            f"**Headroom to CM3 target ({cm3_target * 100:.1f}%):** at this price you "
            f"can spend up to **{cur}{max(max_ads, 0):.2f}** per unit "
            f"({max(max_ads / r['net'], 0) * 100:.1f}% of net) on ads + discounts."
        )
        if pd.notna(r["cm2_pct"]) and r["cm2_pct"] < cm2_target:
            st.error(f"CM2 ({r['cm2_pct'] * 100:.1f}%) is below the {cm2_target * 100:.0f}% target.")

    with t2:
        fig = go.Figure(
            go.Waterfall(
                orientation="v",
                measure=["absolute", "relative", "total", "relative", "relative", "total", "relative", "total"],
                x=["Net price", "COGS", "CM1", "FBA", "Referral", "CM2", "Marketing", "CM3"],
                y=[r["net"], -r["cogs"], 0, -r["fba_fee"], -r["referral"], 0, -r["marketing_per_unit"], 0],
                text=[f"{v:.2f}" for v in [r["net"], -r["cogs"], r["cm1"], -r["fba_fee"], -r["referral"], r["cm2"], -r["marketing_per_unit"], r["cm3"]]],
                textposition="outside",
                decreasing={"marker": {"color": "#C0504D"}},
                increasing={"marker": {"color": "#1F3864"}},
                totals={"marker": {"color": "#4472C4"}},
            )
        )
        fig.update_layout(
            title=f"Margin waterfall at {cur}{target_price:.2f}",
            showlegend=False,
            height=420,
            margin={"t": 50, "b": 20},
        )
        st.plotly_chart(fig, width="stretch")

st.caption(
    "CM1 = net price − COGS · CM2 = CM1 − FBA − referral fee · CM3 = CM2 − ad spend per unit. "
    "Referral fees are recalculated from the simulated price (rate derived from Amazon's fee "
    "preview); FBA fees and ad spend per unit are held constant. Margins are % of net price."
)
