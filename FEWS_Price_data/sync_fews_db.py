#!/usr/bin/env python3
"""
FEWS NET Database Sync Script
=============================
Synchronizes Haiti market price data from FEWS NET API to local DuckDB database.

Usage:
    # Initialize database (create tables)
    python sync_fews_db.py --init

    # Full sync (all historical data from 2005)
    python sync_fews_db.py --full

    # Incremental sync (only new data since last sync)
    python sync_fews_db.py --sync

    # Show database statistics
    python sync_fews_db.py --stats

    # Query the database
    python sync_fews_db.py --query "SELECT * FROM v_latest_prices LIMIT 10"

Requirements:
    pip install duckdb pandas requests
"""

import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent))

from database.fews_database import FEWSDatabase
from fewsnet_haiti_downloader import FEWSNETClient


def init_database():
    """Initialize the database with schema."""
    print("=" * 60)
    print("Initializing FEWS NET Database")
    print("=" * 60)

    with FEWSDatabase() as db:
        db.create_tables()
        print("[OK] Database initialized successfully")
        print(f"     Location: {db.db_path}")


def full_sync():
    """Perform a full sync of all historical data."""
    print("=" * 60)
    print("Full Sync - All Historical Data")
    print("=" * 60)

    # Initialize API client
    client = FEWSNETClient()
    if not client.test_connection():
        print("[ERROR] Could not connect to FEWS NET API")
        sys.exit(1)

    # Fetch all data
    start_date = "2005-01-01"
    end_date = datetime.now().strftime("%Y-%m-%d")

    print(f"\n[INFO] Fetching data from {start_date} to {end_date}...")
    print("       This may take several minutes for large datasets.\n")

    try:
        df = client.get_market_prices(
            country_code="HT",
            start_date=start_date,
            end_date=end_date,
        )
    except Exception as e:
        print(f"[ERROR] Failed to fetch data: {e}")
        sys.exit(1)

    if df.empty:
        print("[WARN] No data retrieved from API")
        return

    print(f"[OK] Fetched {len(df)} records from API")

    # Sync to database
    print("\n[INFO] Syncing to database...")

    with FEWSDatabase() as db:
        # Ensure tables exist
        db.create_tables()

        # Sync data
        stats = db.sync_dataframe(df)

        # Log the import
        db.log_import(
            records_fetched=len(df),
            stats=stats,
            start_date=start_date,
            end_date=end_date,
            status="success"
        )

        print(f"\n{'='*60}")
        print("Sync Complete")
        print(f"{'='*60}")
        print(f"  Records fetched:  {len(df)}")
        print(f"  Records inserted: {stats['inserted']}")
        print(f"  Records updated:  {stats['updated']}")
        print(f"  Errors:           {stats['errors']}")

        # Show final stats
        db_stats = db.get_stats()
        print(f"\n  Database totals:")
        print(f"    Observations: {db_stats['total_observations']}")
        print(f"    Markets:      {db_stats['total_markets']}")
        print(f"    Products:     {db_stats['total_products']}")
        print(f"    Date range:   {db_stats['date_min']} to {db_stats['date_max']}")


def incremental_sync():
    """Perform an incremental sync (only new data since last sync)."""
    print("=" * 60)
    print("Incremental Sync")
    print("=" * 60)

    with FEWSDatabase() as db:
        # Ensure tables exist
        db.create_tables()

        # Get last sync date
        last_sync = db.get_last_sync_date()

        if last_sync:
            # Start from the day after last sync
            start_date = (datetime.strptime(last_sync, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
            print(f"[INFO] Last sync: {last_sync}")
            print(f"[INFO] Fetching data from {start_date}")
        else:
            print("[INFO] No previous sync found, performing full sync")
            start_date = "2005-01-01"

        end_date = datetime.now().strftime("%Y-%m-%d")

        if start_date > end_date:
            print("[INFO] Database is up to date, nothing to sync")
            return

        # Initialize API client
        client = FEWSNETClient()
        if not client.test_connection():
            print("[ERROR] Could not connect to FEWS NET API")
            sys.exit(1)

        # Fetch new data
        print(f"\n[INFO] Fetching data from {start_date} to {end_date}...")

        try:
            df = client.get_market_prices(
                country_code="HT",
                start_date=start_date,
                end_date=end_date,
            )
        except Exception as e:
            print(f"[ERROR] Failed to fetch data: {e}")
            db.log_import(
                records_fetched=0,
                stats={},
                start_date=start_date,
                end_date=end_date,
                status="failed",
                error_message=str(e)
            )
            sys.exit(1)

        if df.empty:
            print("[INFO] No new data available")
            db.log_import(
                records_fetched=0,
                stats={"inserted": 0, "updated": 0},
                start_date=start_date,
                end_date=end_date,
                status="success"
            )
            return

        print(f"[OK] Fetched {len(df)} records")

        # Sync data
        print("\n[INFO] Syncing to database...")
        stats = db.sync_dataframe(df)

        # Log the import
        db.log_import(
            records_fetched=len(df),
            stats=stats,
            start_date=start_date,
            end_date=end_date,
            status="success"
        )

        print(f"\n{'='*60}")
        print("Sync Complete")
        print(f"{'='*60}")
        print(f"  Records fetched:  {len(df)}")
        print(f"  Records inserted: {stats['inserted']}")
        print(f"  Records updated:  {stats['updated']}")
        print(f"  Errors:           {stats['errors']}")


def show_stats():
    """Show database statistics."""
    print("=" * 60)
    print("FEWS NET Database Statistics")
    print("=" * 60)

    try:
        with FEWSDatabase() as db:
            stats = db.get_stats()

            print(f"\n  Total observations: {stats['total_observations']:,}")
            print(f"  Total markets:      {stats['total_markets']}")
            print(f"  Total products:     {stats['total_products']}")
            print(f"  Date range:         {stats['date_min']} to {stats['date_max']}")

            # Show markets
            print("\n  Markets:")
            markets = db.query("SELECT name, admin_1 FROM markets ORDER BY name")
            for _, row in markets.iterrows():
                print(f"    - {row['name']} ({row['admin_1']})")

            # Show products
            print("\n  Products:")
            products = db.query("SELECT DISTINCT name FROM products ORDER BY name")
            for _, row in products.iterrows():
                print(f"    - {row['name']}")

            # Show recent imports
            print("\n  Recent imports:")
            imports = db.query("""
                SELECT import_date, records_fetched, records_inserted, status
                FROM import_log
                ORDER BY import_date DESC
                LIMIT 5
            """)
            for _, row in imports.iterrows():
                print(f"    {row['import_date']}: {row['records_fetched']} fetched, "
                      f"{row['records_inserted']} inserted ({row['status']})")

    except Exception as e:
        print(f"[ERROR] {e}")
        print("\nHint: Run 'python sync_fews_db.py --init' to initialize the database")
        sys.exit(1)


def run_query(sql: str):
    """Run a custom SQL query."""
    print("=" * 60)
    print("Query Results")
    print("=" * 60)

    try:
        with FEWSDatabase() as db:
            result = db.query(sql)
            print(f"\n{result.to_string()}")
            print(f"\n({len(result)} rows)")
    except Exception as e:
        print(f"[ERROR] Query failed: {e}")
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(
        description="Sync FEWS NET Haiti price data to local DuckDB database",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python sync_fews_db.py --init          Initialize database
  python sync_fews_db.py --full          Full historical sync
  python sync_fews_db.py --sync          Incremental sync
  python sync_fews_db.py --stats         Show statistics
  python sync_fews_db.py --query "SELECT * FROM v_latest_prices"
        """
    )

    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--init", action="store_true", help="Initialize database schema")
    group.add_argument("--full", action="store_true", help="Full sync (all historical data)")
    group.add_argument("--sync", action="store_true", help="Incremental sync (new data only)")
    group.add_argument("--stats", action="store_true", help="Show database statistics")
    group.add_argument("--query", type=str, metavar="SQL", help="Run a SQL query")

    args = parser.parse_args()

    if args.init:
        init_database()
    elif args.full:
        full_sync()
    elif args.sync:
        incremental_sync()
    elif args.stats:
        show_stats()
    elif args.query:
        run_query(args.query)


if __name__ == "__main__":
    main()
