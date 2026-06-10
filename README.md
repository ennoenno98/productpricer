# Product Pricer — Amazon

Password-gated Streamlit dashboard for the marketing team. Pick a country,
change prices directly in the table, and see the impact on **CM1 / CM2 / CM3**
live. Modelled on the *"Margen Calc AMZ"* sheet of `Margin_Check_V5.xlsx`.

## Features

- **Country pricing sheet** — one editable sheet per marketplace
  (DE, IT, ES, FR, GB, IE, NL, BE). Edit the *New price* column; CM1/CM2/CM3
  (€ and %) recalculate instantly. Status flags products below the CM3 target,
  summary metrics show portfolio impact, and the sheet can be downloaded as CSV.
- **Product calculator** — single-product deep dive: target price or discount,
  full margin breakdown (the Excel calculator's layout), waterfall chart, and
  "how much can I spend on ads/discounts and stay on target".
- **Adjustable assumptions** (sidebar): CM2 / CM3 targets, VAT per country,
  EUR→GBP rate.
- **FBA fee changes tab** — after uploading a current fee preview report, this
  tab compares it against the baseline in the repo and highlights products
  whose FBA fulfilment fee went up (red) or down (green): summary counts, a
  chart of the largest changes, a filterable detail table (including new and
  disappeared products), and a CSV download. A fee change hits CM2/CM3 1:1.
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
| `data/products.csv` | Amazon **FBA fee preview report** + COGS (workbook sheet "AMZ Fees") | `python extract_from_excel.py Margin_Check.xlsx`, or export the fee preview from Seller Central and append the COGS column |
| `data/marketing_spend.csv` | Amazon Ads spend ÷ units ordered per SKU × country (workbook sheet "AMZ Marketing data") | same script |
| `data/metadata.json` | Ad-spend period (window + export date), written by the script | same script |

**Marketing spend period:** the PPC exports in the workbook are
**trailing-90-day** Amazon Ads reports pulled on **2026-03-09** (i.e. roughly
2025-12-09 → 2026-03-09). The window is inferred from the export file names
(e.g. `UK PPC 90 days.csv`) and the export date from the files' "Date
modified"; both are stored in `data/metadata.json` and shown in the dashboard
(sidebar "Data basis", CM3 column tooltips, and chart/table captions).

SKUs with no ads data get marketing spend of 0 (same as the Excel lookup).
The marketing export's "UK" is mapped to the fee report's "GB".

## Run locally

```bash
pip install -r requirements.txt
export DASHBOARD_PASSWORD="choose-a-password"
streamlit run streamlit_app.py
```

## Deploy (Render)

`render.yaml` is included — same setup as the Margin-Analytics dashboard.
Set `DASHBOARD_PASSWORD` in the Render service environment.
