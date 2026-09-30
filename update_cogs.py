"""Refresh data/cogs.csv (SKU -> COGS in EUR) from Novadata.

Runs weekly via .github/workflows/update_cogs.yml. The dashboard reads COGS
from data/cogs.csv first and only falls back to the COGS carried in the FBA fee
snapshot, so costs stay current without re-uploading fee reports.

Sources (first one given wins):
    NOVADATA_COGS_EXPORT_URL   env var: a Novadata data-export link (CSV/XLSX)
                               that includes SKU + COGS columns
    --from-file PATH [PATH..]  local CSV/XLSX export, or Novadata COGS-worksheet
                               JSON ({"rows": [{"sku", "currentCogs", ...}]})

Novadata does not track multipack or bundle SKUs, so those are derived for
every SKU in the latest fee snapshot that still has no cost:
    VMP_<base>_<n>_PK   ->  n x COGS(base)
    VV-BUN-<a>+<b>...   ->  COGS(VV-VITA-<a>) + COGS(VV-VITA-<b>) + ...

Usage:
    python update_cogs.py
    python update_cogs.py --from-file worksheet_page1.json worksheet_page2.json
"""
from __future__ import annotations

import io
import json
import os
import re
import sys
from datetime import date
from pathlib import Path

import pandas as pd

DATA_DIR = Path(__file__).resolve().parent / "data"
COGS_PATH = DATA_DIR / "cogs.csv"
COGS_COL_PAT = re.compile(r"^(current\s*cogs|cogs|cost of goods( sold)?|unit cost|landed cost)$", re.I)
SKU_COL_PAT = re.compile(r"^(seller\s*)?sku$", re.I)


def _parse_table(df: pd.DataFrame) -> dict[str, float]:
    cols = {c: str(c).strip() for c in df.columns}
    sku_col = next((c for c, n in cols.items() if SKU_COL_PAT.match(n)), None)
    cogs_col = next((c for c, n in cols.items() if COGS_COL_PAT.match(n)), None)
    if sku_col is None or cogs_col is None:
        raise ValueError(
            f"Need a SKU and a COGS column; found: {', '.join(cols.values())}"
        )
    # Prefer EUR rows when the export mixes currencies (dashboard COGS is EUR).
    cur_col = next((c for c, n in cols.items() if n.lower() == "currency"), None)
    if cur_col is not None and (df[cur_col].astype(str).str.upper() == "EUR").any():
        df = df[df[cur_col].astype(str).str.upper() == "EUR"]
    per_col = next((c for c, n in cols.items() if n.lower() in ("periodstart", "period start", "period")), None)
    if per_col is not None:
        # Undated rows are Novadata's default cost (before the first period):
        # sort them first so the latest dated period wins.
        df = df.sort_values(per_col, na_position="first")
    vals = pd.to_numeric(df[cogs_col].astype(str).str.replace(",", ".", regex=False), errors="coerce")
    out = {}
    for sku, v in zip(df[sku_col].astype(str).str.strip(), vals):
        if pd.notna(v) and v > 0:
            out[sku] = float(v)
    return out


def _parse_worksheet_json(obj: dict) -> dict[str, float]:
    rows = [r for r in obj.get("rows", []) if r.get("currentCogs") is not None]
    if rows and any(str(r.get("currency", "EUR")).upper() == "EUR" for r in rows):
        rows = [r for r in rows if str(r.get("currency", "EUR")).upper() == "EUR"]
    rows.sort(key=lambda r: r.get("periodStart") or "")
    return {str(r["sku"]).strip(): float(r["currentCogs"]) for r in rows}


def load_source(content: bytes, name: str) -> dict[str, float]:
    name = name.lower()
    text = content.lstrip()[:1]
    if name.endswith(".json") or text == b"{":
        return _parse_worksheet_json(json.loads(content))
    if name.endswith((".xlsx", ".xls")):
        return _parse_table(pd.read_excel(io.BytesIO(content)))
    for enc in ("utf-8-sig", "cp1252"):
        try:
            return _parse_table(
                pd.read_csv(io.StringIO(content.decode(enc)), sep=None, engine="python")
            )
        except UnicodeDecodeError:
            continue
    raise ValueError("Could not decode the COGS export")


def derive_missing(cogs: dict[str, float], skus: list[str]) -> dict[str, tuple[float, str]]:
    """Costs for multipack / bundle SKUs Novadata doesn't carry."""
    derived: dict[str, tuple[float, str]] = {}
    for sku in skus:
        if sku in cogs:
            continue
        m = re.match(r"^VMP_(.+)_(\d+)_PK$", sku)
        if m and m.group(1) in cogs:
            n = int(m.group(2))
            derived[sku] = (round(n * cogs[m.group(1)], 2), f"{n}x {m.group(1)}")
            continue
        m = re.match(r"^VV-BUN-([\d+]+)", sku)
        if m:
            parts = [f"VV-VITA-{p}" for p in m.group(1).split("+") if p]
            if parts and all(p in cogs for p in parts):
                derived[sku] = (round(sum(cogs[p] for p in parts), 2), " + ".join(parts))
    return derived


def main() -> None:
    sources: list[tuple[bytes, str]] = []
    if "--from-file" in sys.argv:
        for p in sys.argv[sys.argv.index("--from-file") + 1:]:
            sources.append((Path(p).read_bytes(), p))
    else:
        url = os.environ.get("NOVADATA_COGS_EXPORT_URL")
        if not url:
            print("NOVADATA_COGS_EXPORT_URL is not set; nothing to update.")
            return
        import requests

        resp = requests.get(url, timeout=300)
        resp.raise_for_status()
        sources.append((resp.content, url.split("?")[0]))

    cogs: dict[str, float] = {}
    for content, name in sources:
        cogs.update(load_source(content, name))
    if not cogs:
        sys.exit("No COGS values found in the source; aborting (cogs.csv unchanged).")

    snaps = sorted((DATA_DIR / "fee_history").glob("products_*.csv"))
    snap = pd.read_csv(snaps[-1]) if snaps else pd.DataFrame(columns=["sku", "COGS"])
    skus = snap["sku"].astype(str).unique().tolist()
    snap_has_cogs = set(snap.loc[snap["COGS"].notna(), "sku"].astype(str))
    derived = derive_missing(cogs, skus)

    rows = [{"sku": s, "cogs_eur": round(v, 4), "source": "novadata"} for s, v in cogs.items()]
    rows += [{"sku": s, "cogs_eur": v, "source": f"derived: {how}"} for s, (v, how) in derived.items()]
    out = pd.DataFrame(rows).sort_values("sku")
    out.to_csv(COGS_PATH, index=False)

    # SKUs with no cost anywhere (not in Novadata, not derivable, and no COGS
    # carried in the fee snapshot the dashboard falls back to).
    uncovered = sorted(set(skus) - set(out["sku"]) - snap_has_cogs)
    meta_path = DATA_DIR / "metadata.json"
    meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
    meta["cogs"] = {
        "source": "Novadata COGS (EUR), refreshed weekly by update_cogs.yml",
        "updated": date.today().isoformat(),
        "skus_from_novadata": len(cogs),
        "skus_derived": len(derived),
        "catalog_skus_without_cogs": uncovered,
    }
    meta_path.write_text(json.dumps(meta, indent=2) + "\n")
    print(f"cogs.csv: {len(cogs)} from Novadata + {len(derived)} derived; "
          f"{len(uncovered)} catalog SKU(s) still without COGS: {uncovered[:10]}")


if __name__ == "__main__":
    main()
