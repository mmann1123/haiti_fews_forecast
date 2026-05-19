#!/usr/bin/env python3
"""
Compare missing-observation rates and release lag between WB RTFP and FEWS NET.

Definitions:
  - "Expected months" for a (market, commodity) = number of months from the
    first observation to the last observation of that series across BOTH
    sources combined. We then ask, of those expected months, how many
    actually exist in WB vs. in FEWS.
  - "Release lag" = (today - max(price_date)) for each source. We also report
    the WB release version date (when the file was published) vs. its latest
    data date, to separate publication cadence from data lag.

Writes:
  output/missing_summary.csv          per (commodity, market) coverage stats
  output/missing_by_commodity.png     bar chart: % missing per commodity, WB vs FEWS
  output/release_lag.txt              text summary of release/data lag
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import duckdb
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

# Reuse the same mapping as compare.py.
from compare import COMMODITY_MAP, MARKET_MAP, load_fews, load_wb

REPO = Path(__file__).resolve().parents[2]
DB_PATH = REPO / "FEWS_Price_data" / "database" / "fews_haiti.duckdb"
OUT = Path(__file__).resolve().parent / "output"
OUT.mkdir(exist_ok=True)


def months_between(a: pd.Timestamp, b: pd.Timestamp) -> int:
    """Inclusive month count between two month-start timestamps."""
    return (b.year - a.year) * 12 + (b.month - a.month) + 1


def build_coverage(fews: pd.DataFrame, wb: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for cm, (prod, src, unit) in COMMODITY_MAP.items():
        f = fews[
            (fews["product"] == prod)
            & (fews["product_source"] == src)
            & (fews["unit"] == unit)
        ].copy()
        f["month"] = f["period_date"].dt.to_period("M").dt.to_timestamp()

        w = wb[wb["cm_name"] == cm].copy()
        w["month"] = w["price_date"].dt.to_period("M").dt.to_timestamp()

        for wb_mkt, fews_mkt in MARKET_MAP.items():
            ff = f[(f["fews_market"] == fews_mkt)].dropna(subset=["price_htg"])
            ww = w[(w["wb_market"] == wb_mkt)].dropna(subset=["price"])
            if ff.empty and ww.empty:
                continue

            fews_months = set(ff["month"])
            wb_months = set(ww["month"])
            all_months = fews_months | wb_months
            if not all_months:
                continue
            mn, mx = min(all_months), max(all_months)
            expected = months_between(mn, mx)

            rows.append(
                {
                    "wb_market": wb_mkt,
                    "fews_market": fews_mkt,
                    "cm_name": cm,
                    "span_start": mn.date(),
                    "span_end": mx.date(),
                    "expected_months": expected,
                    "fews_obs": len(fews_months),
                    "wb_obs": len(wb_months),
                    "fews_missing": expected - len(fews_months),
                    "wb_missing": expected - len(wb_months),
                    "fews_pct_missing": (expected - len(fews_months)) / expected * 100,
                    "wb_pct_missing": (expected - len(wb_months)) / expected * 100,
                }
            )
    return pd.DataFrame(rows)


def plot_missing(cov: pd.DataFrame, path: Path) -> None:
    by_cm = (
        cov.groupby("cm_name")
        .agg(
            expected=("expected_months", "sum"),
            fews_missing=("fews_missing", "sum"),
            wb_missing=("wb_missing", "sum"),
        )
        .assign(
            fews_pct=lambda d: d["fews_missing"] / d["expected"] * 100,
            wb_pct=lambda d: d["wb_missing"] / d["expected"] * 100,
        )
        .sort_values("fews_pct", ascending=False)
    )
    x = np.arange(len(by_cm))
    w = 0.4
    fig, ax = plt.subplots(figsize=(10, 5))
    ax.bar(x - w / 2, by_cm["fews_pct"], width=w, label="FEWS", color="#1f77b4")
    ax.bar(x + w / 2, by_cm["wb_pct"],   width=w, label="WB",   color="#d62728")
    ax.set_xticks(x)
    ax.set_xticklabels(by_cm.index, rotation=30, ha="right")
    ax.set_ylabel("% of expected market-months missing")
    ax.set_title("Missing observations by commodity (pooled across 9 overlapping markets)")
    ax.legend()
    for i, (fp, wp) in enumerate(zip(by_cm["fews_pct"], by_cm["wb_pct"])):
        ax.text(i - w / 2, fp + 0.5, f"{fp:.0f}%", ha="center", va="bottom", fontsize=8)
        ax.text(i + w / 2, wp + 0.5, f"{wp:.0f}%", ha="center", va="bottom", fontsize=8)
    fig.tight_layout()
    fig.savefig(path, dpi=130)
    plt.close(fig)
    return by_cm


def release_lag_summary(con: duckdb.DuckDBPyConnection) -> str:
    today = date.today()

    # WB: release version + latest data date
    wb_release = con.execute(
        "SELECT MAX(wb_release_date), MAX(price_date) FROM wb_rtfp_prices"
    ).fetchone()
    wb_release_date, wb_data_date = wb_release

    # FEWS: latest period_date + most recent import
    fews_data_date = con.execute(
        "SELECT MAX(period_date) FROM price_observations"
    ).fetchone()[0]
    fews_import = con.execute(
        "SELECT MAX(import_date) FROM import_log WHERE status='success'"
    ).fetchone()[0]

    # Per-commodity latest data date (does WB or FEWS lead?)
    per_cm = con.execute(
        f"""
        WITH fews AS (
            SELECT p.name||' ('||p.product_source||', '||u.name||')' AS fews_series,
                   MAX(po.period_date) AS fews_last
            FROM price_observations po
            JOIN products p ON po.product_id = p.id
            JOIN units    u ON po.unit_id    = u.id
            WHERE (p.name, p.product_source, u.name) IN (
                {",".join(f"('{prod}','{src}','{unit}')" for prod,src,unit in COMMODITY_MAP.values())}
            )
            GROUP BY 1
        ),
        wb AS (
            SELECT cm_name, MAX(price_date) AS wb_last
            FROM wb_rtfp_prices GROUP BY 1
        )
        SELECT cm_name, fews_series, fews_last, wb_last
        FROM wb FULL OUTER JOIN fews ON FALSE
        """
    ).df()  # Fallback: just hand-join in pandas below.

    cm_lines = []
    for cm, (prod, src, unit) in COMMODITY_MAP.items():
        fews_last = con.execute(
            """
            SELECT MAX(po.period_date)
            FROM price_observations po
            JOIN products p ON po.product_id=p.id
            JOIN units    u ON po.unit_id=u.id
            WHERE p.name=? AND p.product_source=? AND u.name=?
            """,
            [prod, src, unit],
        ).fetchone()[0]
        wb_last = con.execute(
            "SELECT MAX(price_date) FROM wb_rtfp_prices WHERE cm_name=?",
            [cm],
        ).fetchone()[0]
        cm_lines.append(
            f"  {cm:12s}  WB last: {wb_last}   FEWS last: {fews_last}"
        )

    out = []
    out.append("== Release / data lag (today = {}) ==".format(today))
    out.append("")
    out.append("WB RTFP (Haiti, catalog 4494)")
    out.append(f"  Release version date:  {wb_release_date}")
    out.append(f"  Latest data point:     {wb_data_date}")
    if wb_release_date and wb_data_date:
        out.append(
            f"  Data lag at release:   {months_between(pd.Timestamp(wb_data_date), pd.Timestamp(wb_release_date)) - 1} months"
        )
        out.append(f"  Days since release:    {(today - wb_release_date).days}")
    if wb_data_date:
        out.append(f"  Data lag vs today:     {(today - wb_data_date).days} days")
    out.append("")
    out.append("FEWS NET")
    out.append(f"  Latest data point:     {fews_data_date}")
    out.append(f"  Latest import_log run: {fews_import}")
    if fews_data_date:
        out.append(f"  Data lag vs today:     {(today - fews_data_date).days} days")
    out.append("")
    out.append("Latest data point per commodity:")
    out.extend(cm_lines)
    return "\n".join(out)


def main() -> None:
    con = duckdb.connect(str(DB_PATH), read_only=True)
    fews = load_fews(con)
    wb = load_wb(con)

    cov = build_coverage(fews, wb)
    cov.to_csv(OUT / "missing_summary.csv", index=False)

    by_cm = plot_missing(cov, OUT / "missing_by_commodity.png")

    pooled = cov.agg(
        expected=("expected_months", "sum"),
        fews_obs=("fews_obs", "sum"),
        wb_obs=("wb_obs", "sum"),
        fews_missing=("fews_missing", "sum"),
        wb_missing=("wb_missing", "sum"),
    ).iloc[0:1]
    expected = cov["expected_months"].sum()
    print("== Pooled across all (market, commodity) within shared spans ==")
    print(f"  Expected market-months: {expected:,}")
    print(
        f"  FEWS observations:      {cov['fews_obs'].sum():,}  "
        f"({cov['fews_missing'].sum():,} missing, "
        f"{cov['fews_missing'].sum()/expected*100:.1f}%)"
    )
    print(
        f"  WB   observations:      {cov['wb_obs'].sum():,}  "
        f"({cov['wb_missing'].sum():,} missing, "
        f"{cov['wb_missing'].sum()/expected*100:.1f}%)"
    )
    print()
    print("== By commodity ==")
    print(by_cm[["expected", "fews_missing", "wb_missing", "fews_pct", "wb_pct"]].to_string())
    print()

    lag_text = release_lag_summary(con)
    (OUT / "release_lag.txt").write_text(lag_text)
    print(lag_text)
    con.close()


if __name__ == "__main__":
    main()
