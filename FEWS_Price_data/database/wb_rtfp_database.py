#!/usr/bin/env python3
"""
World Bank RTFP Database Manager
================================
Upserts WB Real-Time Food Prices rows into DuckDB. Shares the DuckDB file with
the FEWS and ACLED managers. Schema lives in database/schema.sql.

WB re-publishes the full panel each release and back-fills prior months when
its modeled prices update, so we upsert on the natural key
(iso3, mkt_name, cm_name, unit, price_type, currency, price_date) — revised
values overwrite, untouched rows stay put.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import duckdb
import pandas as pd

DEFAULT_DB_PATH = Path(
    os.getenv("FEWS_DB_PATH", Path(__file__).parent / "fews_haiti.duckdb")
)

# Columns we expect from the WB release. Anything missing is filled with NULL
# so schema drift in the upstream CSV doesn't break the upsert.
EXPECTED_COLS = [
    "iso3", "country", "adm1_name", "adm2_name",
    "mkt_name", "lat", "lon",
    "cm_name", "currency",
    "price_date", "price", "o_price", "h_price", "l_price",
]


class WBRTFPDatabase:
    """Database manager for World Bank RTFP price data."""

    def __init__(
        self,
        db_path: Optional[Path] = None,
        con: Optional[duckdb.DuckDBPyConnection] = None,
    ):
        self.db_path = db_path or DEFAULT_DB_PATH
        self.con = con
        self._owns_con = con is None

    def connect(self):
        if self.con is None:
            self.con = duckdb.connect(str(self.db_path))
            self._owns_con = True
        return self

    def close(self):
        if self.con and self._owns_con:
            self.con.close()
            self.con = None

    def __enter__(self):
        return self.connect()

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    # ------------------------------------------------------------------
    # Release bookkeeping
    # ------------------------------------------------------------------

    def get_last_release_date(self) -> Optional[str]:
        row = self.con.execute(
            "SELECT MAX(release_date) FROM wb_rtfp_release_log"
        ).fetchone()
        return str(row[0]) if row and row[0] else None

    def log_release(
        self,
        release_date: str,
        title: str,
        url: str,
        rows_ingested: int,
    ) -> None:
        # Upsert so re-runs of the same release update the row count / title.
        self.con.execute(
            """
            INSERT INTO wb_rtfp_release_log (
                release_date, release_title, download_url, rows_ingested
            ) VALUES (?, ?, ?, ?)
            ON CONFLICT (release_date) DO UPDATE SET
                release_title = EXCLUDED.release_title,
                download_url  = EXCLUDED.download_url,
                rows_ingested = EXCLUDED.rows_ingested,
                ingested_at   = now()
            """,
            [release_date, title, url, rows_ingested],
        )

    # ------------------------------------------------------------------
    # Upsert
    # ------------------------------------------------------------------

    def sync_dataframe(self, df: pd.DataFrame, release_date: str) -> dict:
        stats = {"inserted": 0, "updated": 0, "errors": 0}
        if df.empty:
            return stats

        normalized = self._normalize(df, release_date)
        before = self.con.execute("SELECT COUNT(*) FROM wb_rtfp_prices").fetchone()[0]

        df_to_insert = normalized  # noqa: F841 (DuckDB resolves this by name)
        self.con.execute(
            """
            INSERT INTO wb_rtfp_prices (
                iso3, country, adm1_name, adm2_name,
                mkt_name, lat, lon,
                cm_name, currency,
                price_date, price, o_price, h_price, l_price,
                wb_release_date
            )
            SELECT
                iso3, country, adm1_name, adm2_name,
                mkt_name, lat, lon,
                cm_name, currency,
                price_date, price, o_price, h_price, l_price,
                wb_release_date
            FROM df_to_insert
            ON CONFLICT (iso3, mkt_name, cm_name, price_date)
            DO UPDATE SET
                country         = EXCLUDED.country,
                adm1_name       = EXCLUDED.adm1_name,
                adm2_name       = EXCLUDED.adm2_name,
                lat             = EXCLUDED.lat,
                lon             = EXCLUDED.lon,
                currency        = EXCLUDED.currency,
                price           = EXCLUDED.price,
                o_price         = EXCLUDED.o_price,
                h_price         = EXCLUDED.h_price,
                l_price         = EXCLUDED.l_price,
                wb_release_date = EXCLUDED.wb_release_date,
                imported_at     = now()
            """
        )

        after = self.con.execute("SELECT COUNT(*) FROM wb_rtfp_prices").fetchone()[0]
        stats["inserted"] = after - before
        stats["updated"] = len(normalized) - stats["inserted"]
        return stats

    @staticmethod
    def _normalize(df: pd.DataFrame, release_date: str) -> pd.DataFrame:
        out = df.copy()
        # Case-insensitive column rename to canonical names.
        rename = {}
        lower_map = {c.lower(): c for c in out.columns}
        for canon in EXPECTED_COLS:
            if canon in out.columns:
                continue
            if canon.lower() in lower_map:
                rename[lower_map[canon.lower()]] = canon
        if rename:
            out = out.rename(columns=rename)

        for col in EXPECTED_COLS:
            if col not in out.columns:
                out[col] = None

        out["iso3"] = out["iso3"].astype(str).str.upper()
        out["price_date"] = pd.to_datetime(out["price_date"], errors="coerce").dt.date
        for num in ("price", "o_price", "h_price", "l_price", "lat", "lon"):
            out[num] = pd.to_numeric(out[num], errors="coerce")

        out["wb_release_date"] = pd.to_datetime(release_date, errors="coerce").date()

        out = out.dropna(subset=["price_date", "mkt_name", "cm_name", "price"]).reset_index(drop=True)
        return out[EXPECTED_COLS + ["wb_release_date"]]

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------

    def get_stats(self) -> dict:
        n_rows = self.con.execute("SELECT COUNT(*) FROM wb_rtfp_prices").fetchone()[0]
        date_range = self.con.execute(
            "SELECT MIN(price_date), MAX(price_date) FROM wb_rtfp_prices"
        ).fetchone()
        n_markets = self.con.execute(
            "SELECT COUNT(DISTINCT mkt_name) FROM wb_rtfp_prices"
        ).fetchone()[0]
        n_cm = self.con.execute(
            "SELECT COUNT(DISTINCT cm_name) FROM wb_rtfp_prices"
        ).fetchone()[0]
        last_release = self.get_last_release_date()
        return {
            "total_rows": n_rows,
            "date_min": str(date_range[0]) if date_range[0] else None,
            "date_max": str(date_range[1]) if date_range[1] else None,
            "markets": n_markets,
            "commodities": n_cm,
            "last_release": last_release,
        }
