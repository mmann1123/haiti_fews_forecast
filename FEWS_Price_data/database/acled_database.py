#!/usr/bin/env python3
"""
ACLED Database Manager
======================
Upserts ACLED events into DuckDB and rebuilds monthly feature rollups
(national + per-market with a haversine buffer around each FEWS market).

Designed to share a connection / DuckDB file with the existing
FEWSDatabase manager. The schema for acled_events, acled_features_national,
and acled_features_market lives in database/schema.sql.
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

# Default buffer radius for market-level features (km). Matches the value
# stored in acled_features_market.radius_km.
DEFAULT_RADIUS_KM = 25.0

# ACLED event-type buckets used by feature rebuild. Keep these as Python
# constants (not hardcoded in SQL) so we can tweak without losing the audit
# trail in schema.sql.
VIOLENT_EVENT_TYPES = (
    "Battles",
    "Violence against civilians",
    "Explosions/Remote violence",
)
PROTEST_EVENT_TYPES = ("Protests", "Riots")
BLOCKADE_SUB_EVENT_PATTERN = "(?i)(blockade|roadblock)"


class ACLEDDatabase:
    """Database manager for ACLED Haiti events + features."""

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
    # Event upsert
    # ------------------------------------------------------------------

    def sync_dataframe(self, df: pd.DataFrame) -> dict:
        """
        Upsert ACLED events keyed on event_id_cnty.

        DuckDB's INSERT ... ON CONFLICT DO UPDATE works because the schema
        declares event_id_cnty UNIQUE. We bulk-load via a temp table to avoid
        per-row Python overhead.
        """
        stats = {"inserted": 0, "updated": 0, "errors": 0}
        if df.empty:
            return stats

        before = self.con.execute("SELECT COUNT(*) FROM acled_events").fetchone()[0]

        # Register the DataFrame and run a single MERGE-style upsert.
        # DuckDB resolves dataframe variables by name in the calling scope.
        df_to_insert = df  # noqa: F841 (referenced by DuckDB's df scan)
        self.con.execute(
            """
            INSERT INTO acled_events (
                event_id_cnty, event_date, event_type, sub_event_type,
                admin1, admin2, latitude, longitude, fatalities
            )
            SELECT
                event_id_cnty,
                event_date,
                event_type,
                sub_event_type,
                admin1,
                admin2,
                latitude,
                longitude,
                COALESCE(fatalities, 0)
            FROM df_to_insert
            ON CONFLICT (event_id_cnty) DO UPDATE SET
                event_date     = EXCLUDED.event_date,
                event_type     = EXCLUDED.event_type,
                sub_event_type = EXCLUDED.sub_event_type,
                admin1         = EXCLUDED.admin1,
                admin2         = EXCLUDED.admin2,
                latitude       = EXCLUDED.latitude,
                longitude      = EXCLUDED.longitude,
                fatalities     = EXCLUDED.fatalities,
                imported_at    = now()
            """
        )

        after = self.con.execute("SELECT COUNT(*) FROM acled_events").fetchone()[0]
        stats["inserted"] = after - before
        stats["updated"] = len(df) - stats["inserted"]
        return stats

    # ------------------------------------------------------------------
    # Feature rebuild
    # ------------------------------------------------------------------

    def rebuild_features(self, radius_km: float = DEFAULT_RADIUS_KM) -> dict:
        """
        Drop and rebuild both feature tables from acled_events.

        Cheap because feature tables stay small (~250 months x 11 markets).
        Returns row counts for the rebuilt tables.
        """
        self.con.execute("DELETE FROM acled_features_national")
        self.con.execute("DELETE FROM acled_features_market")

        violent = ", ".join(f"'{t}'" for t in VIOLENT_EVENT_TYPES)
        protest = ", ".join(f"'{t}'" for t in PROTEST_EVENT_TYPES)

        # National rollup. last_day() returns the calendar month-end so this
        # joins cleanly to price_observations.period_date.
        self.con.execute(
            f"""
            INSERT INTO acled_features_national (
                period_date,
                acled_violent_events,
                acled_fatalities,
                acled_protest_blockade,
                acled_event_total
            )
            SELECT
                last_day(event_date) AS period_date,
                SUM(CASE WHEN event_type IN ({violent}) THEN 1 ELSE 0 END)::INTEGER,
                SUM(COALESCE(fatalities, 0))::INTEGER,
                SUM(
                    CASE WHEN event_type IN ({protest})
                              OR regexp_matches(COALESCE(sub_event_type, ''), '{BLOCKADE_SUB_EVENT_PATTERN}')
                         THEN 1 ELSE 0 END
                )::INTEGER,
                COUNT(*)::INTEGER
            FROM acled_events
            GROUP BY 1
            ORDER BY 1
            """
        )

        # Per-market rollup using haversine distance to each market's lat/lon.
        # 6371 km = Earth radius. Markets table is small (~11 rows), events
        # ~25K rows -- the cross join is trivial.
        self.con.execute(
            f"""
            INSERT INTO acled_features_market (
                market_id,
                period_date,
                radius_km,
                acled_violent_events,
                acled_fatalities,
                acled_protest_blockade,
                acled_event_total
            )
            WITH joined AS (
                SELECT
                    m.id AS market_id,
                    last_day(e.event_date) AS period_date,
                    e.event_type,
                    e.sub_event_type,
                    e.fatalities,
                    6371 * 2 * asin(
                        sqrt(
                            pow(sin(radians((e.latitude  - m.latitude)  / 2)), 2)
                          + cos(radians(m.latitude)) * cos(radians(e.latitude))
                          * pow(sin(radians((e.longitude - m.longitude) / 2)), 2)
                        )
                    ) AS dist_km
                FROM acled_events e
                CROSS JOIN markets m
                WHERE e.latitude IS NOT NULL
                  AND e.longitude IS NOT NULL
                  AND m.latitude IS NOT NULL
                  AND m.longitude IS NOT NULL
            )
            SELECT
                market_id,
                period_date,
                {radius_km}::DOUBLE AS radius_km,
                SUM(CASE WHEN event_type IN ({violent}) THEN 1 ELSE 0 END)::INTEGER,
                SUM(COALESCE(fatalities, 0))::INTEGER,
                SUM(
                    CASE WHEN event_type IN ({protest})
                              OR regexp_matches(COALESCE(sub_event_type, ''), '{BLOCKADE_SUB_EVENT_PATTERN}')
                         THEN 1 ELSE 0 END
                )::INTEGER,
                COUNT(*)::INTEGER
            FROM joined
            WHERE dist_km <= {radius_km}
            GROUP BY market_id, period_date
            ORDER BY market_id, period_date
            """
        )

        n_nat = self.con.execute(
            "SELECT COUNT(*) FROM acled_features_national"
        ).fetchone()[0]
        n_mkt = self.con.execute(
            "SELECT COUNT(*) FROM acled_features_market"
        ).fetchone()[0]
        return {"national_rows": n_nat, "market_rows": n_mkt}

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def get_last_event_date(self) -> Optional[str]:
        """Latest event_date in acled_events, or None if table is empty."""
        row = self.con.execute("SELECT MAX(event_date) FROM acled_events").fetchone()
        return str(row[0]) if row and row[0] else None

    def get_stats(self) -> dict:
        n_events = self.con.execute("SELECT COUNT(*) FROM acled_events").fetchone()[0]
        date_range = self.con.execute(
            "SELECT MIN(event_date), MAX(event_date) FROM acled_events"
        ).fetchone()
        n_nat = self.con.execute(
            "SELECT COUNT(*) FROM acled_features_national"
        ).fetchone()[0]
        n_mkt = self.con.execute(
            "SELECT COUNT(*) FROM acled_features_market"
        ).fetchone()[0]
        return {
            "total_events": n_events,
            "date_min": str(date_range[0]) if date_range[0] else None,
            "date_max": str(date_range[1]) if date_range[1] else None,
            "national_rows": n_nat,
            "market_rows": n_mkt,
        }
