#!/usr/bin/env python3
"""
World Bank RTFP sync orchestrator (Cloud Run Job entry point).

Flow:
    1. Download DuckDB from GCS (if GCS_BUCKET set).
    2. Hit the WB catalog to discover the latest release version.
    3. If that version was already ingested AND --force is not set, exit early.
    4. Otherwise download the ZIP, filter to Haiti (ISO3 = HTI), upsert.
    5. Upload the DuckDB file back to GCS.

The 2-step version check is the whole point: WB back-fills prior months when
its modeled prices update, so we cannot trust a "since-last-date" pull, but we
also don't want to download ~hundreds of MB every cron tick. Checking the
release date first gives us both correctness and bandwidth.

Required env vars:
    FEWS_DB_PATH   -- writable path for the DuckDB file (e.g., /tmp/fews_haiti.duckdb)
    GCS_BUCKET     -- bucket for persistence (omit for local-only runs)
    GCS_BLOB_NAME  -- optional; defaults to fews_haiti.duckdb
    WB_RTFP_FILES_ENDPOINT -- optional override for the catalog files endpoint

Usage:
    python sync_worldbank.py sync            # skip if release unchanged
    python sync_worldbank.py sync --force    # re-download even if unchanged
    python sync_worldbank.py stats
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from database.fews_database import FEWSDatabase  # noqa: E402
from database.wb_rtfp_database import WBRTFPDatabase  # noqa: E402
from worldbank_rtfp_downloader import WorldBankRTFPClient  # noqa: E402

DB_PATH = Path(
    os.getenv("FEWS_DB_PATH", Path(__file__).parent / "database" / "fews_haiti.duckdb")
)
GCS_BUCKET = os.getenv("GCS_BUCKET")
GCS_BLOB_NAME = os.getenv("GCS_BLOB_NAME", "fews_haiti.duckdb")
ISO3 = "HTI"


def _download_db_if_needed() -> None:
    if not GCS_BUCKET:
        print("[INFO] GCS_BUCKET not set; using local DuckDB at", DB_PATH)
        return
    if DB_PATH.exists():
        print("[INFO] DuckDB already present at", DB_PATH)
        return
    from database.gcs_sync import download_db_from_gcs

    print(f"[INFO] downloading gs://{GCS_BUCKET}/{GCS_BLOB_NAME} -> {DB_PATH}")
    found = download_db_from_gcs(GCS_BUCKET, GCS_BLOB_NAME, DB_PATH)
    if not found:
        print("[WARN] blob not found in GCS; will initialize an empty DuckDB")


def _upload_db() -> None:
    if not GCS_BUCKET:
        print("[INFO] GCS_BUCKET not set; skipping upload")
        return
    from database.gcs_sync import upload_db_to_gcs

    print(f"[INFO] uploading DuckDB -> gs://{GCS_BUCKET}/{GCS_BLOB_NAME}")
    upload_db_to_gcs(DB_PATH, GCS_BUCKET, GCS_BLOB_NAME)


def cmd_sync(args) -> None:
    _download_db_if_needed()
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    # Ensure full schema (including WB RTFP tables) exists.
    with FEWSDatabase(db_path=DB_PATH) as fews_db:
        fews_db.create_tables()

    client = WorldBankRTFPClient()
    title, url, version_date = client.get_latest_version()

    with WBRTFPDatabase(db_path=DB_PATH) as wb:
        last = wb.get_last_release_date()
        if last and last >= version_date and not args.force:
            print(
                f"[INFO] WB release {version_date} already ingested "
                f"(last={last}); skipping bulk download. Use --force to override."
            )
            return

        df = client.download_country(url)
        if df.empty:
            print(f"[ERROR] WB release contains no {ISO3} rows; aborting")
            sys.exit(1)

        stats = wb.sync_dataframe(df, release_date=version_date)
        wb.log_release(version_date, title, url, rows_ingested=len(df))
        print(
            f"[OK] WB RTFP upsert: inserted={stats['inserted']} "
            f"updated={stats['updated']} (release={version_date})"
        )

    _upload_db()
    print("[DONE] WB RTFP sync complete")


def cmd_stats(_args) -> None:
    _download_db_if_needed()
    with WBRTFPDatabase(db_path=DB_PATH) as wb:
        s = wb.get_stats()
    print(f"  Total rows:     {s['total_rows']:,}")
    print(f"  Date range:     {s['date_min']} .. {s['date_max']}")
    print(f"  Markets:        {s['markets']}")
    print(f"  Commodities:    {s['commodities']}")
    print(f"  Last release:   {s['last_release']}")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd")

    sync = sub.add_parser("sync", help="ingest the latest WB RTFP release")
    sync.add_argument(
        "--force",
        action="store_true",
        help="re-download even if the release version is already ingested",
    )

    sub.add_parser("stats", help="show WB RTFP DB statistics")

    # Allow bare `python sync_worldbank.py` to mean `sync`.
    p.add_argument("--force", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--stats", action="store_true", help=argparse.SUPPRESS)

    args = p.parse_args()
    if args.stats or args.cmd == "stats":
        cmd_stats(args)
    else:
        cmd_sync(args)


if __name__ == "__main__":
    main()
