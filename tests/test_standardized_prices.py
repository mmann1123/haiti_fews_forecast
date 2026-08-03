"""Dashboard queries must return standardized per-kg/L prices with no
native-unit leakage — the marmite/bag/gallon values never surface directly."""

import pytest

import queries
from conftest import KG_PER_6LB, L_PER_GAL, FX

RICE_M1 = 1100.0 / KG_PER_6LB  # 404.18 HTG/kg
RICE_M2 = 1200.0 / KG_PER_6LB  # 440.93 HTG/kg
OIL = 800.0 / L_PER_GAL        # 211.34 HTG/L
CHARCOAL_KG = 150.0 / KG_PER_6LB


class TestMeanPrices:
    def test_htg_is_per_kg_not_per_marmite(self, con):
        df = queries.mean_prices(con, "Rice (Milled)")
        assert len(df) == 3
        expected = (RICE_M1 + RICE_M2) / 2
        assert df["mean_price_htg"].iloc[0] == pytest.approx(expected)

    def test_usd_is_per_kg(self, con):
        df = queries.mean_prices(con, "Rice (Milled)")
        expected = (RICE_M1 + RICE_M2) / 2 * FX
        assert df["mean_price_usd"].iloc[0] == pytest.approx(expected)

    def test_min_max_in_both_currencies(self, con):
        df = queries.mean_prices(con, "Rice (Milled)")
        assert df["min_price_htg"].iloc[0] == pytest.approx(RICE_M1)
        assert df["max_price_htg"].iloc[0] == pytest.approx(RICE_M2)
        assert df["min_price_usd"].iloc[0] == pytest.approx(RICE_M1 * FX)
        assert df["max_price_usd"].iloc[0] == pytest.approx(RICE_M2 * FX)

    def test_charcoal_bag_series_excluded(self, con):
        """The per-bag series (no standardized price) must not blend into the
        mean — this was wrong when the query averaged raw `value`."""
        df = queries.mean_prices(con, "Charcoal")
        assert df["mean_price_htg"].iloc[0] == pytest.approx(CHARCOAL_KG)
        assert (df["num_markets"] == 1).all()


class TestMarketPrices:
    def test_prices_are_standardized(self, con):
        df = queries.market_prices(con, "Refined Vegetable Oil")
        assert len(df) == 3
        assert df["price_htg"].iloc[0] == pytest.approx(OIL)
        assert df["price_usd"].iloc[0] == pytest.approx(OIL * FX)

    def test_bag_rows_dropped(self, con):
        df = queries.market_prices(con, "Charcoal")
        assert len(df) == 3  # only the 6_lb series, one market
        assert df["price_htg"].notna().all()


class TestCommonUnit:
    def test_weight_product_is_kg(self, con):
        assert queries.common_unit(con, "Rice (Milled)") == "kg"

    def test_volume_product_is_liter(self, con):
        assert queries.common_unit(con, "Refined Vegetable Oil") == "L"

    def test_charcoal_uses_convertible_series_unit(self, con):
        assert queries.common_unit(con, "Charcoal") == "kg"

    def test_unknown_product_defaults_to_kg(self, con):
        assert queries.common_unit(con, "No Such Product") == "kg"


class TestForecastingInput:
    def test_htg_series_is_per_kg(self, con):
        from forecasting import get_price_data

        df = get_price_data("ignored", "Rice (Milled)", currency="HTG", conn=con)
        pap = df[df["market_name"] == "Port-au-Prince"]
        assert pap["price"].iloc[0] == pytest.approx(RICE_M1)

    def test_usd_series_is_per_kg(self, con):
        from forecasting import get_price_data

        df = get_price_data("ignored", "Rice (Milled)", currency="USD", conn=con)
        pap = df[df["market_name"] == "Port-au-Prince"]
        assert pap["price"].iloc[0] == pytest.approx(RICE_M1 * FX)

    def test_non_convertible_rows_excluded(self, con):
        from forecasting import get_price_data

        df = get_price_data("ignored", "Charcoal", currency="HTG", conn=con)
        assert len(df) == 3
        assert df["price"].max() < 100  # bag price (2500) never leaks through
