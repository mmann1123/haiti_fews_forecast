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
    db_path: str, product_name: str, currency: str = "HTG",
    conn: Optional["duckdb.DuckDBPyConnection"] = None,
) -> pd.DataFrame:
    """
    Query historical price data for a given product from the database.

    Args:
        db_path: Path to DuckDB database (ignored if `conn` is provided)
        product_name: Name of the product/commodity
        currency: Currency for prices ('HTG' or 'USD')
        conn: Existing DuckDB connection to reuse. When omitted, a new
            connection is opened and closed locally. Pass an existing
            connection to avoid same-process file-lock conflicts.

    Returns:
        DataFrame with columns: date, market, price, market_name
    """
    owns_conn = conn is None
    if owns_conn:
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
    if owns_conn:
        conn.close()

    # Convert date to datetime
    df["date"] = pd.to_datetime(df["date"])

    return df


def _splice_manual(
    df: pd.DataFrame,
    extra_rows: List[dict],
    currency: str,
    fx_rate: Optional[float] = None,
) -> pd.DataFrame:
    """Append in-memory user observations into a get_price_data DF.

    Each entry in `extra_rows` is a dict with keys:
        date    -- date-like (str / date / Timestamp)
        market  -- specific market name or the string "All markets"
        price   -- float in entry currency
        currency -- "HTG" or "USD" (the currency the user typed in)

    "All markets" broadcasts a single price to every market present in `df`,
    so it influences both per-market fits and the market-average. Entries in
    a currency that doesn't match the active forecast currency are converted
    using `fx_rate` (HTG per USD); if conversion is impossible the entry is
    dropped with a warning. On (market_name, date) conflict the manual row
    wins so the user override is what Prophet actually fits.
    """
    if not extra_rows or df.empty:
        return df

    existing_markets = df["market_name"].unique().tolist()
    if not existing_markets:
        return df
    market_id_by_name = dict(df[["market_name", "market"]].drop_duplicates().values)

    rows = []
    for obs in extra_rows:
        price = obs.get("price")
        if price is None:
            continue
        entry_ccy = obs.get("currency", currency)
        if entry_ccy != currency:
            if not fx_rate or fx_rate <= 0:
                # Can't convert -- skip rather than poison the fit.
                continue
            # fx_rate is HTG per USD.
            if currency == "USD":
                price = price / fx_rate
            else:
                price = price * fx_rate

        target_markets = (
            existing_markets
            if obs.get("market") == "All markets"
            else [obs.get("market")]
        )
        date = pd.to_datetime(obs["date"])
        for m in target_markets:
            if m not in market_id_by_name:
                continue
            rows.append(
                {
                    "date": date,
                    "market": market_id_by_name[m],
                    "market_name": m,
                    "price": float(price),
                }
            )

    if not rows:
        return df

    extra_df = pd.DataFrame(rows)
    combined = pd.concat([df, extra_df], ignore_index=True)
    # Manual entries are appended last so keep="last" makes them override.
    combined = combined.drop_duplicates(subset=["market_name", "date"], keep="last")
    combined = combined.sort_values(["market_name", "date"]).reset_index(drop=True)
    return combined


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

        model = Prophet(
            seasonality_mode="additive",
            yearly_seasonality="auto",
            weekly_seasonality=False,
            daily_seasonality=False,
            interval_width=0.95,
            changepoint_prior_scale=0.5,
            changepoint_range=0.95,
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
    # Match period_date convention (month-end) used elsewhere in the dashboard
    future = model.make_future_dataframe(periods=periods, freq="ME")

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
        # Average across ALL markets (matches Tab 1's get_mean_prices behavior).
        # available_markets is unused here on purpose — keeping the parameter for
        # backwards compatibility with callers.
        avg_df = df.groupby("date")["price"].mean().reset_index()
        avg_df.columns = ["ds", "y"]

        n_obs = len(avg_df)

        model = Prophet(
            seasonality_mode="additive",
            yearly_seasonality="auto",
            weekly_seasonality=False,
            daily_seasonality=False,
            interval_width=0.95,
            changepoint_prior_scale=0.5,
            changepoint_range=0.95,
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
    db_path: str, product_name: str, currency: str = "HTG", min_months: int = 24,
    conn: Optional["duckdb.DuckDBPyConnection"] = None,
    extra_rows: Optional[List[dict]] = None,
    fx_rate: Optional[float] = None,
) -> Tuple[Dict[str, ForecastResult], Dict[str, Dict]]:
    """
    Fit Prophet models for all markets with sufficient data, plus market average.

    Args:
        db_path: Path to DuckDB database
        product_name: Name of the product/commodity
        currency: Currency for prices ('HTG' or 'USD')
        min_months: Minimum months of data required
        extra_rows: Optional in-memory observations to splice into training
            data (see `_splice_manual` for the expected schema). Used by the
            dashboard's "Add a recent price" feature so what-if values feed
            the fit without touching the DB.
        fx_rate: HTG per USD, used only to convert `extra_rows` entries whose
            currency differs from `currency`.

    Returns:
        Tuple of (results_dict, availability_dict)
    """
    # Get price data
    df = get_price_data(db_path, product_name, currency, conn=conn)

    if extra_rows:
        df = _splice_manual(df, extra_rows, currency, fx_rate=fx_rate)

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

    # Fit market average model on all markets in the data (mirrors Tab 1).
    # Run independent of the per-market 24-month gate so the average is
    # available even when no single market passes individually.
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
