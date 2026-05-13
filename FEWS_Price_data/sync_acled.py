#!/usr/bin/env python3
"""
ACLED sync orchestrator (Cloud Run Job entry point).

Flow:
    1. Download DuckDB from GCS (if GCS_BUCKET set).
    2. Pull new ACLED events since the last event_date in the table
       (or a full historical pull from --start when --full).
    3. Upsert events, rebuild monthly feature tables.
    4. Export acled_national.csv and acled_by_market.csv.
    5. Upload the DuckDB file and both CSVs back to GCS.

Required env vars:
    ACLED_USERNAME, ACLED_PASSWORD -- myACLED credentials (OAuth2 password grant)
    FEWS_DB_PATH                  -- writable path for the DuckDB file (e.g., /tmp/fews_haiti.duckdb)
    GCS_BUCKET                    -- bucket for persistence (omit for local-only runs)
    GCS_BLOB_NAME                 -- optional; defaults to fews_haiti.duckdb

Usage:
    python sync_acled.py --incremental
    python sync_acled.py --full --start 2018-01-01
    python sync_acled.py --stats
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

# Make sibling modules importable when run as a script from any cwd.
sys.path.insert(0, str(Path(__file__).parent))

from acled_haiti_downloader import ACLEDClient  # noqa: E402
from database.acled_database import ACLEDDatabase  # noqa: E402
from database.fews_database import FEWSDatabase  # noqa: E402
from export_acled_csv import (  # noqa: E402
    MARKET_FILENAME,
    NATIONAL_FILENAME,
    export_by_market,
    export_national,
)

DB_PATH = Path(
    os.getenv("FEWS_DB_PATH", Path(__file__).parent / "database" / "fews_haiti.duckdb")
)
GCS_BUCKET = os.getenv("GCS_BUCKET")
GCS_BLOB_NAME = os.getenv("GCS_BLOB_NAME", "fews_haiti.duckdb")

# Earliest date we'll pull on a full sync. ACLED Haiti coverage is sparse
# before 2018; matching the FEWS series back to 2005 is fine but mostly empty.
DEFAULT_FULL_START = "2018-01-01"


def _download_db_if_needed() -> None:
    """Pull the canonical DuckDB file from GCS to DB_PATH (no-op if missing)."""
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


def _upload_artifacts(csv_dir: Path) -> None:
    if not GCS_BUCKET:
        print("[INFO] GCS_BUCKET not set; skipping upload")
        return
    from database.gcs_sync import upload_blob, upload_db_to_gcs

    print(f"[INFO] uploading DuckDB -> gs://{GCS_BUCKET}/{GCS_BLOB_NAME}")
    upload_db_to_gcs(DB_PATH, GCS_BUCKET, GCS_BLOB_NAME)

    for name in (NATIONAL_FILENAME, MARKET_FILENAME):
        local = csv_dir / name
        print(f"[INFO] uploading {local} -> gs://{GCS_BUCKET}/{name}")
        upload_blob(local, GCS_BUCKET, name, content_type="text/csv")


def _pull_events(client: ACLEDClient, start: str, end: str):
    print(f"[INFO] ACLED pull window: {start} .. {end}")
    df = client.get_events(start_date=start, end_date=end)
    print(f"[OK] fetched {len(df)} events")
    return df


def cmd_sync(args) -> None:
    _download_db_if_needed()
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    # Ensure schema exists (idempotent). Uses the existing FEWS migrator
    # which runs the whole schema.sql, including the new ACLED tables.
    with FEWSDatabase(db_path=DB_PATH) as fews_db:
        fews_db.create_tables()

    # Decide the pull window.
    if args.full:
        start = args.start or DEFAULT_FULL_START
    else:
        with ACLEDDatabase(db_path=DB_PATH) as adb:
            last = adb.get_last_event_date()
        if last:
            # Re-pull last 7 days to catch ACLED's late corrections.
            start_dt = datetime.fromisoformat(last) - timedelta(days=7)
            start = start_dt.date().isoformat()
            print(f"[INFO] last event in DB: {last}; pulling from {start}")
        else:
            start = args.start or DEFAULT_FULL_START
            print(f"[INFO] no prior events; full pull from {start}")
    end = args.end or datetime.utcnow().date().isoformat()

    client = ACLEDClient()
    if not client.test_connection():
        sys.exit(1)
    events = _pull_events(client, start, end)

    with ACLEDDatabase(db_path=DB_PATH) as adb:
        if not events.empty:
            stats = adb.sync_dataframe(events)
            print(f"[OK] upsert: inserted={stats['inserted']} updated={stats['updated']}")
        else:
            print("[INFO] no events to upsert")

        rebuild = adb.rebuild_features()
        print(
            f"[OK] features rebuilt: national={rebuild['national_rows']} "
            f"market={rebuild['market_rows']}"
        )

        # Export CSVs (use the same DuckDB connection -- read-only is fine).
        out_dir = Path(args.out_dir).resolve()
        out_dir.mkdir(parents=True, exist_ok=True)
        n_nat = export_national(adb.con, out_dir / NATIONAL_FILENAME)
        n_mkt = export_by_market(adb.con, out_dir / MARKET_FILENAME)
        print(f"[OK] wrote CSVs: national={n_nat} rows, market={n_mkt} rows")

    _upload_artifacts(out_dir)
    print("[DONE] ACLED sync complete")


def cmd_stats(_args) -> None:
    _download_db_if_needed()
    with ACLEDDatabase(db_path=DB_PATH) as adb:
        s = adb.get_stats()
    print(f"  Total events:    {s['total_events']:,}")
    print(f"  Date range:      {s['date_min']} .. {s['date_max']}")
    print(f"  National rows:   {s['national_rows']}")
    print(f"  Market rows:     {s['market_rows']}")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd")

    sync = sub.add_parser(
        "sync",
        help="incremental sync (default if no subcommand)",
    )
    sync.add_argument("--full", action="store_true", help="full historical pull")
    sync.add_argument("--incremental", action="store_true", help="alias for default")
    sync.add_argument("--start", type=str, default=None)
    sync.add_argument("--end", type=str, default=None)
    sync.add_argument(
        "--out-dir",
        type=str,
        default=os.getenv("ACLED_CSV_DIR", "/tmp"),
        help="local directory for the exported CSVs (default /tmp)",
    )

    sub.add_parser("stats", help="show DB statistics")

    # Allow `python sync_acled.py --full` without an explicit subcommand.
    p.add_argument("--full", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--incremental", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--start", type=str, default=None, help=argparse.SUPPRESS)
    p.add_argument("--end", type=str, default=None, help=argparse.SUPPRESS)
    p.add_argument(
        "--out-dir",
        type=str,
        default=os.getenv("ACLED_CSV_DIR", "/tmp"),
        help=argparse.SUPPRESS,
    )
    p.add_argument("--stats", action="store_true", help=argparse.SUPPRESS)

    args = p.parse_args()

    if args.stats or args.cmd == "stats":
        cmd_stats(args)
    else:
        cmd_sync(args)


if __name__ == "__main__":
    main()
