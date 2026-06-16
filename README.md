# Product Pricer — Amazon

Password-gated Streamlit dashboard for the marketing team. Pick a country,
change prices directly in the table, and see the impact on **CM1 / CM2 / CM3**
live. Modelled on the *"Margen Calc AMZ"* sheet of `Margin_Check_V5.xlsx`.

## Features

- **Country pricing sheet** — one editable sheet per marketplace
  (DE, IT, ES, FR, GB, IE, NL, BE). Edit the *New price* column; CM1/CM2/CM3
  (€ and %) recalculate instantly. Status flags products below the CM3 target,
  summary metrics show portfolio impact. **Bulk edit round-trip**: download the
  sheet as **Excel**, edit the *New price* column for many SKUs at once, and
  upload it back — prices are matched by SKU and applied as the scenario (rows
  left at the current price are ignored).
- **Product calculator** — single-product deep dive: target price or discount,
  full margin breakdown (the Excel calculator's layout), waterfall chart, and
  "how much can I spend on ads/discounts and stay on target".
- **Volume-weighted profit impact with price elasticity** — the pricing sheet
  shows units sold per SKU (Novadata, ad-spend window) and the total CM3 €
  change a price edit would produce at that volume. With the sidebar toggle
  on (default), volume scales as `(new price / current price)^elasticity`
  using each SKU's estimated everyday elasticity; toggled off, volume is held
  constant. The calculator shows the projected volume response per product.

## Price elasticity (`data/elasticity.csv`)

`estimate_elasticity.py` estimates everyday price elasticity per SKU ×
marketplace from 12 months of daily Novadata sales: log-log OLS of units on
implied price with controls for **promo days**, ad spend, month and weekday.
Promo days come from the Seller Central promotions report
(`extract_promotions.py` → `data/promotions.csv`; ASINs recovered from the
report's HYPERLINK formulas, marketplaces attributed by matching promo prices
to the daily implied price) plus inferred price dips (≤93% of the rolling
median). Separating promo days matters: raw elasticities run ≈ −4 median, but
everyday elasticity — the right number for permanent price changes — is
≈ −1.5 to −4 by country (829 SKU-series estimated). Estimates are shrunk
toward the country's trimmed mean (precision-weighted) and clipped to
[−9, −0.3]; SKUs without their own estimate use the country default.

Re-run after major assortment/price changes:
`python estimate_elasticity.py` (downloads the Novadata export) — or pass
`--from-file <export.csv.gz>`.
- **Country-specific plan targets** — CM2 and channel-margin (CM3) targets per
  marketplace come from the AP26 plan ("Amazon Margins" tab), including
  seasonal monthly CM3 targets selectable in the sidebar (`data/targets.json`,
  regenerated with `python extract_targets.py AP26_….xlsx`). Countries not in
  the plan (e.g. BE) fall back to the plan average. VAT is **fixed per
  country** (food-supplement rates), not a user input.
- **Adjustable assumptions** (sidebar): EUR→GBP rate.
- **FBA fee changes tab** — always compares the two newest fee report versions
  in `data/fee_history/` (dated snapshots; the versions being compared are
  named in the tab). When a report is uploaded in the sidebar, the comparison
  switches to *uploaded report vs committed baseline*. Highlights products
  whose FBA fulfilment fee went up (red) or down (green): summary counts, a
  chart of the largest changes, a filterable detail table (including new and
  disappeared products), and a CSV download. A fee change hits CM2/CM3 1:1.
- **Margins color-coded vs country targets** — in the pricing sheet, the
  CM1/CM2/CM3 % cells are green at/above the country's AP26 plan target and
  red below it.
- **FBA report upload** (sidebar → "Update data"): upload the current fee
  preview report from Seller Central (`.csv`, `.txt`, `.tsv` or `.xlsx`;
  tab/comma/semicolon separated and comma decimals are handled). It replaces
  prices and Amazon fees for the session; COGS is carried over from the
  bundled data per SKU when the report has no COGS column. A "Merged
  products.csv" download is offered — commit it as `data/products.csv` to make
  the update permanent (uploads only last for the browser session).

## Margin logic

```
Net price = gross price / (1 + VAT)
CM1 = net price − COGS
CM2 = CM1 − FBA fulfilment fee − referral fee − closing fee
CM3 = CM2 − marketing spend per unit
```

Two deliberate improvements over the Excel sheet:

1. **Referral fee scales with price.** Amazon's referral fee is a percentage of
   the sale price, so the dashboard derives the rate per SKU from the fee
   preview (`fee ÷ current price`) and re-applies it to the simulated price.
   The Excel used the static estimate, which understates fees on price increases.
2. **Margin % uses the simulated net price** as the base (the Excel divided by
   the *current* net price even when simulating a new one).

For GB, COGS and ad spend (both in EUR) are converted to GBP with the rate in
the sidebar (default 0.83).

## Data

| File | Source | Refresh |
|---|---|---|
| `data/fee_history/products_*.csv` | Amazon **FBA fee preview report** + COGS, one dated snapshot per version; the newest is the live dataset | **manual**: upload the report in the dashboard and click **💾 Save as new baseline** — it writes the dated snapshot and commits it to the repo via the GitHub API (set `GITHUB_TOKEN` on the server; optional `GITHUB_REPO`, `GITHUB_BRANCH`). Without a token it saves to local disk only (lost on redeploy) and you can download + commit manually |
| `data/products.csv` | initial baseline (workbook sheet "AMZ Fees"), fallback when `fee_history/` is empty | `python extract_from_excel.py Margin_Check.xlsx` |
| `data/marketing_spend.csv` | **Novadata daily margin export** (Amazon Ads spend ÷ units ordered, trailing 90 days, per SKU × marketplace) | **automatic**: daily GitHub Actions workflow `update_marketing_spend.yml` (07:00 UTC) commits the refresh; Render auto-deploys |
| `data/metadata.json` | Ad-spend period (window + export dates) | written by the update workflow |
| `data/targets.json` | Country margin targets from the AP26 plan | `python extract_targets.py AP26_….xlsx` |

**Update model:** ad spend and sales units refresh automatically every day
from the same Novadata export the Margin-Analytics dashboard uses. The only
manual update is the **FBA fee report** (which also carries prices); COGS and
plan targets change rarely and have extract scripts.

The ad-spend window and export date are stored in `data/metadata.json` and
shown in the dashboard (sidebar "Data basis", CM3 column tooltips, captions).
SKUs with no ads data get marketing spend of 0 (same as the Excel lookup).

## Run locally

```bash
pip install -r requirements.txt
export DASHBOARD_PASSWORD="choose-a-password"
streamlit run streamlit_app.py
```

## Deploy (Render)

`render.yaml` is included — same setup as the Margin-Analytics dashboard.
Set `DASHBOARD_PASSWORD` in the Render service environment, plus `GITHUB_TOKEN` (fine-grained PAT with contents:write on this repo) so the dashboard's save button can persist uploaded FBA reports.
