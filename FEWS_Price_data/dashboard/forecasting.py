"""
Price forecasting module using Facebook Prophet for time series prediction.

This module provides functionality to:
- Fit Prophet models per market for a given commodity
- Generate market-average forecasts
- Handle data availability requirements (minimum 24 months)
- Produce forecasts with confidence intervals
"""

import pandas as pd
import duckdb
from prophet import Prophet
from typing import Dict, List, Tuple, Optional
import logging

# Configure logging
logging.getLogger("prophet").setLevel(logging.WARNING)
logging.getLogger("cmdstanpy").setLevel(logging.WARNING)


class ForecastResult:
    """Container for forecast results and metadata."""

    def __init__(
        self,
        market_name: str,
        success: bool,
        forecast: Optional[pd.DataFrame] = None,
        model: Optional[Prophet] = None,
        error: Optional[str] = None,
        n_observations: int = 0,
    ):
        self.market_name = market_name
        self.success = success
        self.forecast = forecast
        self.model = model
        self.error = error
        self.n_observations = n_observations


def get_price_data(
    db_path: str, product_name: str, currency: str = "HTG"
) -> pd.DataFrame:
    """
    Query historical price data for a given product from the database.

    Args:
        db_path: Path to DuckDB database
        product_name: Name of the product/commodity
        currency: Currency for prices ('HTG' or 'USD')

    Returns:
        DataFrame with columns: date, market, price, market_name
    """
    conn = duckdb.connect(db_path, read_only=True)

    # Determine price column based on currency
    price_col = "value" if currency == "HTG" else "common_currency_price"

    query = f"""
    SELECT 
        po.period_date as date,
        m.id as market,
        m.name as market_name,
        po.{price_col} as price
    FROM price_observations po
    JOIN products p ON po.product_id = p.id
    JOIN markets m ON po.market_id = m.id
    WHERE p.name = ?
    AND po.{price_col} IS NOT NULL
    ORDER BY m.name, po.period_date
    """

    df = conn.execute(query, [product_name]).fetchdf()
    conn.close()

    # Convert date to datetime
    df["date"] = pd.to_datetime(df["date"])

    return df


def check_data_availability(df: pd.DataFrame, min_months: int = 24) -> Dict[str, Dict]:
    """
    Check which markets have sufficient data for forecasting.

    Args:
        df: DataFrame with price data
        min_months: Minimum number of months required

    Returns:
        Dictionary with market names as keys and metadata as values
    """
    availability = {}

    for market_name in df["market_name"].unique():
        market_df = df[df["market_name"] == market_name].copy()

        # Count observations and calculate month span
        n_obs = len(market_df)
        date_range = (market_df["date"].max() - market_df["date"].min()).days / 30.44

        # Check if sufficient data
        sufficient = n_obs >= min_months and date_range >= min_months

        availability[market_name] = {
            "n_observations": n_obs,
            "months_span": round(date_range, 1),
            "sufficient": sufficient,
            "reason": (
                None
                if sufficient
                else f"Only {n_obs} observations ({date_range:.1f} months)"
            ),
        }

    return availability


def prepare_prophet_data(df: pd.DataFrame, market_name: str) -> pd.DataFrame:
    """
    Prepare data in Prophet's required format (ds, y columns).

    Args:
        df: DataFrame with price data
        market_name: Name of the market to filter

    Returns:
        DataFrame with columns 'ds' (datetime) and 'y' (price)
    """
    market_df = df[df["market_name"] == market_name].copy()

    # Rename columns for Prophet
    prophet_df = market_df[["date", "price"]].copy()
    prophet_df.columns = ["ds", "y"]

    # Sort by date
    prophet_df = prophet_df.sort_values("ds").reset_index(drop=True)

    # Remove any duplicates (keep last)
    prophet_df = prophet_df.drop_duplicates(subset=["ds"], keep="last")

    return prophet_df


def fit_prophet_model(df: pd.DataFrame, market_name: str) -> ForecastResult:
    """
    Fit a Prophet model for a specific market.

    Args:
        df: DataFrame with price data
        market_name: Name of the market

    Returns:
        ForecastResult object with model and metadata
    """
    try:
        # Prepare data
        prophet_df = prepare_prophet_data(df, market_name)
        n_obs = len(prophet_df)

        # Initialize Prophet with auto-detected seasonality
        model = Prophet(
            seasonality_mode="multiplicative",  # Multiplicative for prices (percentage changes)
            yearly_seasonality="auto",
            weekly_seasonality=False,  # Monthly data, so no weekly pattern
            daily_seasonality=False,  # Monthly data, so no daily pattern
            interval_width=0.95,  # 95% confidence intervals
            changepoint_prior_scale=0.05,  # Default flexibility for trend changes
        )

        # Fit model
        model.fit(prophet_df)

        return ForecastResult(
            market_name=market_name, success=True, model=model, n_observations=n_obs
        )

    except Exception as e:
        return ForecastResult(
            market_name=market_name,
            success=False,
            error=str(e),
            n_observations=len(df[df["market_name"] == market_name]),
        )


def generate_forecast(model: Prophet, periods: int = 8) -> pd.DataFrame:
    """
    Generate future forecasts using a fitted Prophet model.

    Args:
        model: Fitted Prophet model
        periods: Number of months to forecast

    Returns:
        DataFrame with forecast results including confidence intervals
    """
    # Create future dataframe
    future = model.make_future_dataframe(periods=periods, freq="MS")  # MS = month start

    # Generate forecast
    forecast = model.predict(future)

    return forecast


def fit_market_average_model(
    df: pd.DataFrame, available_markets: List[str]
) -> ForecastResult:
    """
    Fit a Prophet model on market-average prices.

    Args:
        df: DataFrame with price data
        available_markets: List of markets with sufficient data

    Returns:
        ForecastResult object for market average
    """
    try:
        # Filter to available markets only
        avg_df = df[df["market_name"].isin(available_markets)].copy()

        # Calculate average price per date
        avg_df = avg_df.groupby("date")["price"].mean().reset_index()
        avg_df.columns = ["ds", "y"]

        n_obs = len(avg_df)

        # Initialize and fit Prophet model
        model = Prophet(
            seasonality_mode="multiplicative",
            yearly_seasonality="auto",
            weekly_seasonality=False,
            daily_seasonality=False,
            interval_width=0.95,
            changepoint_prior_scale=0.05,
        )

        model.fit(avg_df)

        return ForecastResult(
            market_name="Market Average",
            success=True,
            model=model,
            n_observations=n_obs,
        )

    except Exception as e:
        return ForecastResult(
            market_name="Market Average", success=False, error=str(e), n_observations=0
        )


def fit_all_models(
    db_path: str, product_name: str, currency: str = "HTG", min_months: int = 24
) -> Tuple[Dict[str, ForecastResult], Dict[str, Dict]]:
    """
    Fit Prophet models for all markets with sufficient data, plus market average.

    Args:
        db_path: Path to DuckDB database
        product_name: Name of the product/commodity
        currency: Currency for prices ('HTG' or 'USD')
        min_months: Minimum months of data required

    Returns:
        Tuple of (results_dict, availability_dict)
    """
    # Get price data
    df = get_price_data(db_path, product_name, currency)

    if len(df) == 0:
        return {}, {}

    # Check data availability
    availability = check_data_availability(df, min_months)

    # Get markets with sufficient data
    available_markets = [m for m, info in availability.items() if info["sufficient"]]

    results = {}

    # Fit individual market models
    for market_name in available_markets:
        result = fit_prophet_model(df, market_name)
        results[market_name] = result

    # Fit market average model if we have at least one market
    if available_markets:
        avg_result = fit_market_average_model(df, available_markets)
        results["Market Average"] = avg_result

    return results, availability


def generate_all_forecasts(
    results: Dict[str, ForecastResult], periods: int = 8
) -> Dict[str, pd.DataFrame]:
    """
    Generate forecasts for all successfully fitted models.

    Args:
        results: Dictionary of ForecastResult objects
        periods: Number of months to forecast

    Returns:
        Dictionary mapping market names to forecast DataFrames
    """
    forecasts = {}

    for market_name, result in results.items():
        if result.success and result.model is not None:
            try:
                forecast = generate_forecast(result.model, periods)
                forecasts[market_name] = forecast
            except Exception as e:
                # Log error but continue with other markets
                print(f"Error generating forecast for {market_name}: {e}")

    return forecasts
