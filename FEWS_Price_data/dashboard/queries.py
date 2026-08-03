"""
Dashboard SQL queries, kept Streamlit-free so unit tests can import them.

All prices are standardized to common units: HTG values come from
`common_unit_price` (HTG per kg for weight-based products, HTG per liter for
volume-based ones) and USD values from `common_currency_price` (USD per the
same common unit). Observations that FEWS NET cannot standardize (e.g.
charcoal sold by the bag) have no common price and are excluded, so a
product reported in several native units is never averaged across them.

Missing values arrive from the API as float NaN, not SQL NULL — and one NaN
poisons a whole AVG() — so filters must use isfinite(), never IS NOT NULL.
"""

import pandas as pd


def mean_prices(con, commodity: str) -> pd.DataFrame:
    """Mean/min/max standardized price across all markets for a commodity."""
    df = con.execute(
        """
        SELECT
            po.period_date,
            AVG(po.common_unit_price) AS mean_price_htg,
            AVG(po.common_currency_price) AS mean_price_usd,
            MIN(po.common_unit_price) AS min_price_htg,
            MAX(po.common_unit_price) AS max_price_htg,
            MIN(po.common_currency_price) AS min_price_usd,
            MAX(po.common_currency_price) AS max_price_usd,
            COUNT(DISTINCT po.market_id) AS num_markets
        FROM price_observations po
        JOIN products p ON po.product_id = p.id
        WHERE p.name = ? AND isfinite(po.common_unit_price)
        GROUP BY po.period_date
        ORDER BY po.period_date
    """,
        [commodity],
    ).fetchdf()
    df["period_date"] = pd.to_datetime(df["period_date"])
    return df


def market_prices(con, commodity: str) -> pd.DataFrame:
    """Standardized price per market for a commodity."""
    df = con.execute(
        """
        SELECT
            m.name AS market,
            po.period_date,
            po.common_unit_price AS price_htg,
            po.common_currency_price AS price_usd
        FROM price_observations po
        JOIN markets m ON po.market_id = m.id
        JOIN products p ON po.product_id = p.id
        WHERE p.name = ? AND isfinite(po.common_unit_price)
        ORDER BY po.period_date, m.name
    """,
        [commodity],
    ).fetchdf()
    df["period_date"] = pd.to_datetime(df["period_date"])
    return df


def common_unit(con, commodity: str) -> str:
    """The common unit ('kg' or 'L') a commodity's prices are standardized to."""
    row = con.execute(
        """
        SELECT u.common_unit
        FROM price_observations po
        JOIN products p ON po.product_id = p.id
        JOIN units u ON po.unit_id = u.id
        WHERE p.name = ? AND isfinite(po.common_unit_price)
          AND u.common_unit IS NOT NULL
        GROUP BY u.common_unit
        ORDER BY COUNT(*) DESC
        LIMIT 1
    """,
        [commodity],
    ).fetchone()
    return row[0] if row else "kg"
