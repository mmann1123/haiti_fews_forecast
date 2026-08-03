"""Shared fixtures: a synthetic DuckDB with the production schema.

The dashboard modules live in FEWS_Price_data/dashboard and import each other
as top-level modules, so that directory goes on sys.path.
"""

import sys
from pathlib import Path

import duckdb
import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "FEWS_Price_data" / "dashboard"))

SCHEMA = REPO / "FEWS_Price_data" / "database" / "schema.sql"

# 6 lb marmite in kg, and a gallon in liters — same factors FEWS NET uses.
KG_PER_6LB = 2.72155
L_PER_GAL = 3.785412
FX = 0.0077  # HTG -> USD


@pytest.fixture()
def con():
    """In-memory DB with two markets, three products, mixed units.

    Charcoal is the tricky case: reported both per 6 lb (convertible to kg)
    and per bag (no standardized price, common columns NULL).
    """
    con = duckdb.connect(":memory:")
    con.execute(SCHEMA.read_text())

    con.execute(
        "INSERT INTO markets (id, fews_id, fnid, name) VALUES "
        "(1, 100, 'HT01', 'Port-au-Prince'), (2, 101, 'HT02', 'Cap Haitien'), "
        "(3, 102, 'HT03', 'Jacmel')"
    )
    con.execute(
        "INSERT INTO products (id, name, product_source) VALUES "
        "(1, 'Rice (Milled)', 'Local'), "
        "(2, 'Refined Vegetable Oil', 'Import'), "
        "(3, 'Charcoal', 'Local')"
    )
    con.execute(
        "INSERT INTO units (id, name, unit_type, common_unit) VALUES "
        "(1, '6_lb', 'Weight', 'kg'), "
        "(2, 'gal', 'Volume', 'L'), "
        "(3, 'bag', 'Volume', NULL)"
    )

    def obs(market_id, product_id, unit_id, date, value, per_common):
        con.execute(
            """
            INSERT INTO price_observations
                (market_id, product_id, unit_id, period_date, value,
                 exchange_rate, common_unit_price, common_currency_price)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                market_id, product_id, unit_id, date, value, FX,
                per_common,
                None if per_common is None else per_common * FX,
            ],
        )

    for month in ("2025-01-31", "2025-02-28", "2025-03-31"):
        # Rice in both markets, per marmite
        obs(1, 1, 1, month, 1100.0, 1100.0 / KG_PER_6LB)
        obs(2, 1, 1, month, 1200.0, 1200.0 / KG_PER_6LB)
        # Oil in one market, per gallon
        obs(1, 2, 2, month, 800.0, 800.0 / L_PER_GAL)
        # Charcoal: convertible 6 lb series AND non-convertible bag series
        obs(1, 3, 1, month, 150.0, 150.0 / KG_PER_6LB)
        obs(1, 3, 3, month, 2500.0, None)

    # The FEWS API publishes missing observations as float NaN, not SQL NULL.
    # One NaN row alongside real data — must not poison the month's AVG().
    nan = float("nan")
    obs(3, 1, 1, "2025-01-31", nan, nan)
    obs(2, 2, 2, "2025-01-31", nan, nan)

    yield con
    con.close()
