#!/usr/bin/env python3
"""
prep_fews_export.py -- export FEWS Haiti monthly mean retail prices to CSV.

Computes the monthly mean across all reporting markets per (product,
product_source) pair and writes a long-form CSV the correlations.R script
can consume directly.

Run from anywhere:
    python3 prep_fews_export.py [--db /path/to/fews_haiti.duckdb]

Output: experiments/intl_beans/fews_haiti_prices.csv (long form)
    month, fews_commodity, product_source, n_markets, price_htg, price_usd
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import duckdb
import pandas as pd

DEFAULT_DB = (
    Path(__file__).resolve().parents[2]
    / "FEWS_Price_data"
    / "database"
    / "fews_haiti.duckdb"
)
OUT_PATH = Path(__file__).resolve().parent / "fews_haiti_prices.csv"

# We average across all markets that reported in a given month. This matches
# what the dashboard's "Market Average" view shows on Tab 1, so the lag-scan
# correlations stay comparable to the chart the user sees.
QUERY = """
SELECT
    date_trunc('month', po.period_date)::DATE AS month,
    p.name                                    AS fews_commodity,
    p.product_source                          AS product_source,
    COUNT(DISTINCT po.market_id)              AS n_markets,
    AVG(po.value)                             AS price_htg,
    AVG(po.common_currency_price)             AS price_usd
FROM price_observations po
JOIN products p ON po.product_id = p.id
WHERE po.value IS NOT NULL
GROUP BY 1, 2, 3
ORDER BY 2, 3, 1
"""


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_DB,
        help=f"Path to fews_haiti.duckdb (default: {DEFAULT_DB})",
    )
    args = ap.parse_args()

    if not args.db.exists():
        print(f"[ERROR] DB not found: {args.db}", file=sys.stderr)
        sys.exit(1)

    con = duckdb.connect(str(args.db), read_only=True)
    df: pd.DataFrame = con.execute(QUERY).df()
    con.close()

    if df.empty:
        print("[ERROR] FEWS query returned no rows", file=sys.stderr)
        sys.exit(1)

    df["month"] = pd.to_datetime(df["month"]).dt.strftime("%Y-%m-%d")
    df.to_csv(OUT_PATH, index=False, float_format="%.4f")
    print(
        f"Wrote {OUT_PATH} ({len(df):,} rows; "
        f"{df['fews_commodity'].nunique()} products; "
        f"{df['month'].min()} .. {df['month'].max()})"
    )


if __name__ == "__main__":
    main()
