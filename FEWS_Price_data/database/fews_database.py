#!/usr/bin/env python3
"""
FEWS NET Database Manager
=========================
Manages the DuckDB database for storing Haiti market price data from FEWS NET API.

This module provides the FEWSDatabase class for:
- Creating and initializing the database schema
- Upserting dimension tables (markets, products, units, sources)
- Syncing price observations from the API
- Tracking import history
"""

import os
import duckdb
import pandas as pd
from datetime import datetime
from pathlib import Path
from typing import Optional

# Default database path. Honor FEWS_DB_PATH so Cloud Run can point to /tmp.
DEFAULT_DB_PATH = Path(os.getenv("FEWS_DB_PATH", Path(__file__).parent / "fews_haiti.duckdb"))
SCHEMA_PATH = Path(__file__).parent / "schema.sql"


class FEWSDatabase:
    """Database manager for FEWS NET Haiti price data."""

    def __init__(self, db_path: Optional[Path] = None, con: Optional[duckdb.DuckDBPyConnection] = None):
        """
        Initialize database connection.

        Args:
            db_path: Path to DuckDB file. Defaults to fews_haiti.duckdb in same directory.
            con: Existing DuckDB connection to reuse. If provided, this manager will
                NOT open or close the connection — useful when sharing a single
                writable connection (e.g. with Streamlit's cache_resource) to avoid
                file-lock conflicts.
        """
        self.db_path = db_path or DEFAULT_DB_PATH
        self.con = con
        self._owns_con = con is None

    def connect(self):
        """Open database connection (no-op if one was injected)."""
        if self.con is None:
            self.con = duckdb.connect(str(self.db_path))
            self._owns_con = True
        return self

    def close(self):
        """Close database connection (no-op if connection was injected)."""
        if self.con and self._owns_con:
            self.con.close()
            self.con = None

    def __enter__(self):
        return self.connect()

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def create_tables(self):
        """Create all tables from schema.sql."""
        if not SCHEMA_PATH.exists():
            raise FileNotFoundError(f"Schema file not found: {SCHEMA_PATH}")

        schema_sql = SCHEMA_PATH.read_text()

        # DuckDB can execute multiple statements
        self.con.execute(schema_sql)
        print(f"[OK] Database schema created: {self.db_path}")

    def get_or_create_market(self, row: dict) -> int:
        """Get or create a market record, returning the internal ID."""
        fews_id = row.get("market_id")

        # Check if exists
        result = self.con.execute(
            "SELECT id FROM markets WHERE fews_id = ?", [fews_id]
        ).fetchone()

        if result:
            return result[0]

        # Insert new market
        self.con.execute("""
            INSERT INTO markets (fews_id, fnid, name, admin_1, admin_2, country_code, latitude, longitude)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, [
            fews_id,
            row.get("fnid"),
            row.get("market"),
            row.get("admin_1"),
            row.get("admin_2"),
            row.get("country_code", "HT"),
            row.get("latitude"),
            row.get("longitude"),
        ])

        # Get the inserted ID
        result = self.con.execute(
            "SELECT id FROM markets WHERE fews_id = ?", [fews_id]
        ).fetchone()
        return result[0]

    def get_or_create_product(self, row: dict) -> int:
        """Get or create a product record, returning the internal ID."""
        name = row.get("product")
        source = row.get("product_source")

        # Check if exists
        result = self.con.execute(
            "SELECT id FROM products WHERE name = ? AND product_source = ?",
            [name, source]
        ).fetchone()

        if result:
            return result[0]

        # Insert new product
        self.con.execute("""
            INSERT INTO products (name, cpcv2, cpcv2_description, product_source, is_staple_food)
            VALUES (?, ?, ?, ?, ?)
        """, [
            name,
            row.get("cpcv2"),
            row.get("cpcv2_description"),
            source,
            row.get("is_staple_food", False),
        ])

        result = self.con.execute(
            "SELECT id FROM products WHERE name = ? AND product_source = ?",
            [name, source]
        ).fetchone()
        return result[0]

    def get_or_create_unit(self, row: dict) -> int:
        """Get or create a unit record, returning the internal ID."""
        name = row.get("unit")

        # Check if exists
        result = self.con.execute(
            "SELECT id FROM units WHERE name = ?", [name]
        ).fetchone()

        if result:
            return result[0]

        # Insert new unit
        self.con.execute("""
            INSERT INTO units (name, unit_type, common_unit)
            VALUES (?, ?, ?)
        """, [
            name,
            row.get("unit_type"),
            row.get("common_unit"),
        ])

        result = self.con.execute(
            "SELECT id FROM units WHERE name = ?", [name]
        ).fetchone()
        return result[0]

    def get_or_create_source(self, row: dict) -> Optional[int]:
        """Get or create a data source record, returning the internal ID."""
        fews_id = row.get("datasourceorganization")
        if pd.isna(fews_id):
            return None

        fews_id = int(fews_id)

        # Check if exists
        result = self.con.execute(
            "SELECT id FROM data_sources WHERE fews_id = ?", [fews_id]
        ).fetchone()

        if result:
            return result[0]

        # Insert new source
        self.con.execute("""
            INSERT INTO data_sources (fews_id, name, document_name)
            VALUES (?, ?, ?)
        """, [
            fews_id,
            row.get("source_organization"),
            row.get("source_document"),
        ])

        result = self.con.execute(
            "SELECT id FROM data_sources WHERE fews_id = ?", [fews_id]
        ).fetchone()
        return result[0]

    def upsert_price_observation(self, row: dict, market_id: int, product_id: int,
                                  unit_id: int, source_id: Optional[int]) -> bool:
        """
        Insert or update a price observation.

        Returns True if inserted, False if skipped (duplicate).
        """
        period_date = row.get("period_date")
        price_type = row.get("price_type", "Retail")

        # Check if exists
        result = self.con.execute("""
            SELECT id FROM price_observations
            WHERE market_id = ? AND product_id = ? AND unit_id = ?
              AND period_date = ? AND price_type = ?
        """, [market_id, product_id, unit_id, period_date, price_type]).fetchone()

        # Parse modified timestamp
        api_modified = row.get("modified")
        if api_modified and not pd.isna(api_modified):
            try:
                api_modified = pd.to_datetime(api_modified)
            except Exception:
                api_modified = None
        else:
            api_modified = None

        if result:
            # Update existing record
            self.con.execute("""
                UPDATE price_observations SET
                    value = ?,
                    exchange_rate = ?,
                    common_unit_price = ?,
                    common_currency_price = ?,
                    collection_status = ?,
                    api_modified_at = ?,
                    imported_at = CURRENT_TIMESTAMP
                WHERE id = ?
            """, [
                row.get("value"),
                row.get("exchange_rate"),
                row.get("common_unit_price"),
                row.get("common_currency_price"),
                row.get("collection_status"),
                api_modified,
                result[0],
            ])
            return False  # Updated, not inserted

        # Insert new record
        self.con.execute("""
            INSERT INTO price_observations (
                market_id, product_id, unit_id, source_id,
                period_date, start_date, price_type, currency, value,
                exchange_rate, common_unit_price, common_currency_price,
                collection_status, fews_dataseries_id, api_modified_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, [
            market_id,
            product_id,
            unit_id,
            source_id,
            period_date,
            row.get("start_date"),
            price_type,
            row.get("currency", "HTG"),
            row.get("value"),
            row.get("exchange_rate"),
            row.get("common_unit_price"),
            row.get("common_currency_price"),
            row.get("collection_status"),
            row.get("dataseries"),
            api_modified,
        ])
        return True  # Inserted

    def sync_dataframe(self, df: pd.DataFrame) -> dict:
        """
        Sync a DataFrame of price data to the database.

        Args:
            df: DataFrame with FEWS NET API data

        Returns:
            dict with counts: {'inserted': n, 'updated': n, 'skipped': n}
        """
        stats = {"inserted": 0, "updated": 0, "skipped": 0, "errors": 0}

        for idx, row in df.iterrows():
            try:
                row_dict = row.to_dict()

                # Get or create dimension records
                market_id = self.get_or_create_market(row_dict)
                product_id = self.get_or_create_product(row_dict)
                unit_id = self.get_or_create_unit(row_dict)
                source_id = self.get_or_create_source(row_dict)

                # Upsert price observation
                inserted = self.upsert_price_observation(
                    row_dict, market_id, product_id, unit_id, source_id
                )

                if inserted:
                    stats["inserted"] += 1
                else:
                    stats["updated"] += 1

            except Exception as e:
                stats["errors"] += 1
                if stats["errors"] <= 5:  # Only print first 5 errors
                    print(f"[WARN] Error processing row {idx}: {e}")

        return stats

    def log_import(self, records_fetched: int, stats: dict,
                   start_date: Optional[str], end_date: Optional[str],
                   status: str = "success", error_message: Optional[str] = None):
        """Log an import operation."""
        self.con.execute("""
            INSERT INTO import_log (
                records_fetched, records_inserted, records_updated, records_skipped,
                date_range_start, date_range_end, status, error_message
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, [
            records_fetched,
            stats.get("inserted", 0),
            stats.get("updated", 0),
            stats.get("skipped", 0),
            start_date,
            end_date,
            status,
            error_message,
        ])

    def get_last_sync_date(self) -> Optional[str]:
        """Get the latest period_date actually present in price_observations."""
        result = self.con.execute(
            "SELECT MAX(period_date) FROM price_observations"
        ).fetchone()

        if result and result[0]:
            return str(result[0])
        return None

    def get_stats(self) -> dict:
        """Get database statistics."""
        stats = {}

        stats["total_observations"] = self.con.execute(
            "SELECT COUNT(*) FROM price_observations"
        ).fetchone()[0]

        stats["total_markets"] = self.con.execute(
            "SELECT COUNT(*) FROM markets"
        ).fetchone()[0]

        stats["total_products"] = self.con.execute(
            "SELECT COUNT(*) FROM products"
        ).fetchone()[0]

        date_range = self.con.execute(
            "SELECT MIN(period_date), MAX(period_date) FROM price_observations"
        ).fetchone()
        stats["date_min"] = str(date_range[0]) if date_range[0] else None
        stats["date_max"] = str(date_range[1]) if date_range[1] else None

        return stats

    def query(self, sql: str) -> pd.DataFrame:
        """Execute a query and return results as DataFrame."""
        return self.con.execute(sql).fetchdf()
