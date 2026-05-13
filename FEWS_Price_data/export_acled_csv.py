#!/usr/bin/env python3
"""
Export ACLED monthly features from DuckDB to CSV.

Writes two files:

    acled_national.csv
        month, acled_violent_events, acled_fatalities,
        acled_protest_blockade, acled_event_total

    acled_by_market.csv  (long format)
        month, market_name, admin_1, radius_km,
        acled_violent_events, acled_fatalities,
        acled_protest_blockade, acled_event_total

The R/NIMBLE forecast model (in a separate repo) reads `acled_national.csv`
as its `synthetic_acled.csv` replacement. `acled_by_market.csv` is for
future per-market modeling.

Usage:
    python export_acled_csv.py --out-dir ./out
    python export_acled_csv.py --out-dir ./out --gcs-bucket my-bucket
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import duckdb

DEFAULT_DB_PATH = Path(
    os.getenv("FEWS_DB_PATH", Path(__file__).parent / "database" / "fews_haiti.duckdb")
)

NATIONAL_FILENAME = "acled_national.csv"
MARKET_FILENAME = "acled_by_market.csv"


def export_national(con: duckdb.DuckDBPyConnection, out_path: Path) -> int:
    df = con.execute(
        """
        SELECT
            period_date AS month,
            acled_violent_events,
            acled_fatalities,
            acled_protest_blockade,
            acled_event_total
        FROM acled_features_national
        ORDER BY period_date
        """
    ).fetchdf()
    df.to_csv(out_path, index=False, date_format="%Y-%m-%d")
    return len(df)


def export_by_market(con: duckdb.DuckDBPyConnection, out_path: Path) -> int:
    df = con.execute(
        """
        SELECT
            f.period_date AS month,
            m.name AS market_name,
            m.admin_1,
            f.radius_km,
            f.acled_violent_events,
            f.acled_fatalities,
            f.acled_protest_blockade,
            f.acled_event_total
        FROM acled_features_market f
        JOIN markets m ON m.id = f.market_id
        ORDER BY m.name, f.period_date
        """
    ).fetchdf()
    df.to_csv(out_path, index=False, date_format="%Y-%m-%d")
    return len(df)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--out-dir", type=Path, default=Path("."))
    parser.add_argument(
        "--gcs-bucket",
        type=str,
        default=os.getenv("GCS_BUCKET"),
        help="If set, upload both CSVs to gs://<bucket>/<filename> after export.",
    )
    args = parser.parse_args()

    if not args.db_path.exists():
        print(f"[ERROR] DuckDB file not found: {args.db_path}", file=sys.stderr)
        sys.exit(1)

    args.out_dir.mkdir(parents=True, exist_ok=True)
    national_path = args.out_dir / NATIONAL_FILENAME
    market_path = args.out_dir / MARKET_FILENAME

    con = duckdb.connect(str(args.db_path), read_only=True)
    try:
        n_nat = export_national(con, national_path)
        n_mkt = export_by_market(con, market_path)
    finally:
        con.close()

    print(f"[OK] wrote {n_nat} rows -> {national_path}")
    print(f"[OK] wrote {n_mkt} rows -> {market_path}")

    if args.gcs_bucket:
        # Late import so the script works without google-cloud-storage locally.
        from database.gcs_sync import upload_blob

        for path in (national_path, market_path):
            upload_blob(path, args.gcs_bucket, path.name, content_type="text/csv")
            print(f"[OK] uploaded gs://{args.gcs_bucket}/{path.name}")


if __name__ == "__main__":
    main()
