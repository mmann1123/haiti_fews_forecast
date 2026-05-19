"""
FEWS NET Haiti Price Dashboard
==============================
Interactive dashboard for visualizing Haiti market price data from FEWS NET.

Run locally:
    streamlit run app.py

Deploy to Streamlit Cloud:
    1. Push to GitHub
    2. Connect repo at share.streamlit.io
    3. Set app path: FEWS_Price_data/dashboard/app.py
"""

import os
import re
import sys
import traceback
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import streamlit as st
import duckdb
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import requests
from pathlib import Path
from forecasting import fit_all_models, generate_all_forecasts, ForecastResult

FEWS_HAITI_FEED_URL = "https://fews.net/taxonomy/term/514/feed"
FEWS_GLOBAL_PRICE_WATCH_FEED_URL = "https://fews.net/taxonomy/term/15/feed"

# Page config
st.set_page_config(
    page_title="Haiti Food Price Monitor",
    page_icon="🌾",
    layout="wide",
    initial_sidebar_state="expanded",
)

# Database path - works for local, Streamlit Cloud, and Cloud Run.
# In Cloud Run the image filesystem is read-only except /tmp, so we set
# FEWS_DB_PATH=/tmp/fews_haiti.duckdb and download the real DB from GCS at startup.
FEWS_ROOT = Path(__file__).parent.parent
DB_PATH = Path(os.getenv("FEWS_DB_PATH", FEWS_ROOT / "database" / "fews_haiti.duckdb"))

# GCS-backed persistence (only active when GCS_BUCKET is set)
GCS_BUCKET = os.getenv("GCS_BUCKET")
GCS_BLOB_NAME = os.getenv("GCS_BLOB_NAME", "fews_haiti.duckdb")

# FEWS NET publishes monthly survey data with roughly a 2-3 month lag (median
# ~80 days from period_date to availability — measured in the WB vs FEWS
# comparison experiment). We use this to gate the Update button: if we already
# hold a period_date newer than (today - lag), polling the API again is futile.
FEWS_RELEASE_LAG_DAYS = 80
# Minimum gap between user-triggered API hits, even when data could plausibly
# be new. Stops accidental double-clicks from re-pulling.
MIN_REFRESH_INTERVAL = timedelta(hours=24)

# Add parent directory so we can import sync modules
if str(FEWS_ROOT) not in sys.path:
    sys.path.insert(0, str(FEWS_ROOT))


def _db_needs_init() -> bool:
    """Check if the database is missing or has no price data.

    Uses the cached read-write connection so we don't conflict with our own
    DuckDB file lock on Streamlit re-runs.
    """
    if not DB_PATH.exists():
        return True
    try:
        con = get_connection()
        # If price_observations doesn't exist yet, treat as needs-init.
        exists = con.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_name = 'price_observations'"
        ).fetchone()
        if not exists:
            return True
        count = con.execute("SELECT COUNT(*) FROM price_observations").fetchone()[0]
        return count == 0
    except Exception:
        return True


def _init_database():
    """Pull FEWS NET data and populate the database."""
    from database.fews_database import FEWSDatabase
    from fewsnet_haiti_downloader import FEWSNETClient
    from datetime import datetime

    client = FEWSNETClient()
    if not client.test_connection():
        st.error("Could not connect to FEWS NET API. Check internet connectivity.")
        st.stop()

    start_date = "2005-01-01"
    end_date = datetime.now().strftime("%Y-%m-%d")

    df = client.get_market_prices(
        country_code="HT",
        start_date=start_date,
        end_date=end_date,
    )

    if df.empty:
        st.error("FEWS NET API returned no data.")
        st.stop()

    con = get_connection()
    with FEWSDatabase(con=con) as db:
        db.create_tables()
        stats = db.sync_dataframe(df)
        db.log_import(
            records_fetched=len(df),
            stats=stats,
            start_date=start_date,
            end_date=end_date,
            status="success",
        )

    return len(df), stats


def _incremental_sync():
    """Pull only new data since the last sync."""
    from database.fews_database import FEWSDatabase
    from fewsnet_haiti_downloader import FEWSNETClient
    from datetime import datetime, timedelta

    # Reuse the cached read-write connection to avoid DuckDB file-lock conflicts.
    con = get_connection()

    with FEWSDatabase(con=con) as db:
        last_sync = db.get_last_sync_date()

    if last_sync:
        start_date = (datetime.strptime(last_sync, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
    else:
        start_date = "2005-01-01"

    end_date = datetime.now().strftime("%Y-%m-%d")
    if start_date > end_date:
        return 0, {"inserted": 0, "updated": 0, "errors": 0}

    client = FEWSNETClient()
    if not client.test_connection():
        st.error("Could not connect to FEWS NET API. Check internet connectivity.")
        st.stop()

    df = client.get_market_prices(
        country_code="HT",
        start_date=start_date,
        end_date=end_date,
    )

    if df.empty:
        return 0, {"inserted": 0, "updated": 0, "errors": 0}

    with FEWSDatabase(con=con) as db:
        db.create_tables()
        stats = db.sync_dataframe(df)
        db.log_import(
            records_fetched=len(df),
            stats=stats,
            start_date=start_date,
            end_date=end_date,
            status="success",
        )

    # Clear cached data so queries pick up new records
    st.cache_data.clear()

    return len(df), stats


def _bootstrap_db_from_gcs() -> None:
    """If GCS_BUCKET is configured, download the canonical DuckDB file to DB_PATH.

    Runs once per container start. Safe no-op when GCS_BUCKET is unset (local dev)
    or when the blob does not yet exist (a fresh deployment will then fall through
    to _init_database below and seed an empty DB from FEWS NET).
    """
    if not GCS_BUCKET:
        return
    if DB_PATH.exists():
        return  # already populated this container
    from database.gcs_sync import download_db_from_gcs

    try:
        download_db_from_gcs(GCS_BUCKET, GCS_BLOB_NAME, DB_PATH)
    except Exception as exc:
        st.warning(f"Could not download DuckDB from gs://{GCS_BUCKET}/{GCS_BLOB_NAME}: {exc}")


def _push_db_to_gcs() -> None:
    """Checkpoint DuckDB to the main file, then upload it to GCS.

    The CHECKPOINT step is critical: writes from `_incremental_sync` go into
    `fews_haiti.duckdb.wal` first, and only the main `.duckdb` file is what we
    upload. Without an explicit checkpoint, the upload sends pre-write bytes
    and the user's changes are silently lost on the next container start.
    """
    if not GCS_BUCKET:
        return
    from database.gcs_sync import upload_db_to_gcs

    # Flush WAL into the main file before reading it for upload.
    try:
        get_connection().execute("CHECKPOINT")
    except Exception as exc:
        # If checkpoint fails we still try the upload — it's better to send a
        # stale snapshot than skip entirely — but record the failure.
        print(
            f"[WARN] DuckDB CHECKPOINT failed before GCS upload: {exc!r}",
            file=sys.stderr,
            flush=True,
        )

    upload_db_to_gcs(DB_PATH, GCS_BUCKET, GCS_BLOB_NAME)
    st.session_state["last_gcs_upload_at"] = datetime.now(timezone.utc)


_bootstrap_db_from_gcs()


@st.cache_resource
def get_connection():
    """Get database connection (cached, read-write so sync can reuse it)."""
    return duckdb.connect(str(DB_PATH))


# Auto-initialize database if needed
if _db_needs_init():
    st.info("Database not found or empty. Pulling FEWS NET price data...")
    with st.spinner("Downloading Haiti market prices from FEWS NET (2005-present). This may take a few minutes..."):
        n_records, sync_stats = _init_database()
    st.success(f"Loaded {n_records:,} records ({sync_stats['inserted']:,} inserted).")
    # Push the freshly seeded DB to GCS so future container starts can skip the full pull.
    try:
        _push_db_to_gcs()
    except Exception as exc:
        print(
            f"[ERROR] GCS upload after seed failed: {exc!r}",
            file=sys.stderr,
            flush=True,
        )
        traceback.print_exc(file=sys.stderr)
        st.warning(f"Could not upload seeded DB to GCS: {exc}")
    st.cache_data.clear()


@st.cache_data(ttl=3600)
def get_commodities():
    """Get list of available commodities, with agricultural products first."""
    con = get_connection()
    df = con.execute(
        """
        SELECT DISTINCT p.name
        FROM products p
        JOIN price_observations po ON p.id = po.product_id
        ORDER BY p.name
    """
    ).fetchdf()

    # Non-agricultural products to put at the bottom
    non_agricultural = {"Charcoal", "Diesel", "Gasoline", "Kerosene"}

    commodities = df["name"].tolist()

    # Sort: agricultural first (alphabetically), then non-agricultural (alphabetically)
    agricultural = sorted([c for c in commodities if c not in non_agricultural])
    fuel_items = sorted([c for c in commodities if c in non_agricultural])

    return agricultural + fuel_items


@st.cache_data(ttl=3600)
def get_markets():
    """Get list of available markets."""
    con = get_connection()
    df = con.execute(
        """
        SELECT DISTINCT m.name
        FROM markets m
        JOIN price_observations po ON m.id = po.market_id
        ORDER BY m.name
    """
    ).fetchdf()
    return df["name"].tolist()


@st.cache_data(ttl=3600)
def get_mean_prices(commodity: str):
    """Get mean price across all markets for a commodity."""
    con = get_connection()
    df = con.execute(
        """
        SELECT
            po.period_date,
            AVG(po.value) AS mean_price_htg,
            AVG(po.common_currency_price) AS mean_price_usd,
            MIN(po.value) AS min_price_htg,
            MAX(po.value) AS max_price_htg,
            COUNT(DISTINCT po.market_id) AS num_markets
        FROM price_observations po
        JOIN products p ON po.product_id = p.id
        WHERE p.name = ?
        GROUP BY po.period_date
        ORDER BY po.period_date
    """,
        [commodity],
    ).fetchdf()
    df["period_date"] = pd.to_datetime(df["period_date"])
    return df


@st.cache_data(ttl=3600)
def get_market_prices(commodity: str):
    """Get individual market prices for a commodity."""
    con = get_connection()
    df = con.execute(
        """
        SELECT
            m.name AS market,
            po.period_date,
            po.value AS price_htg,
            po.common_currency_price AS price_usd
        FROM price_observations po
        JOIN markets m ON po.market_id = m.id
        JOIN products p ON po.product_id = p.id
        WHERE p.name = ?
        ORDER BY po.period_date, m.name
    """,
        [commodity],
    ).fetchdf()
    df["period_date"] = pd.to_datetime(df["period_date"])
    return df


@st.cache_data(ttl=3600)
def get_date_range():
    """Get the date range of available data."""
    con = get_connection()
    result = con.execute(
        """
        SELECT MIN(period_date) AS min_date, MAX(period_date) AS max_date
        FROM price_observations
    """
    ).fetchone()
    return pd.to_datetime(result[0]), pd.to_datetime(result[1])


@st.cache_data(ttl=3600)
def fetch_fews_feed(feed_url: str, limit: int = 1):
    """Fetch the latest items from a FEWS NET RSS feed.

    Returns a list of dicts with keys: title, link, pub_date (formatted), summary.
    Returns [] on any network/parse failure.
    """
    try:
        resp = requests.get(feed_url, timeout=10)
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
    except Exception:
        return []

    items = []
    for item in root.findall(".//item")[:limit]:
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        raw_date = (item.findtext("pubDate") or "").strip()
        try:
            pub_date = parsedate_to_datetime(raw_date).strftime("%B %d, %Y")
        except Exception:
            pub_date = raw_date

        summary = re.sub(r"<[^>]+>", "", item.findtext("description") or "").strip()
        summary = re.sub(r"\s+", " ", summary)
        if len(summary) > 400:
            summary = summary[:400].rsplit(" ", 1)[0] + "…"

        items.append({"title": title, "link": link, "pub_date": pub_date, "summary": summary})
    return items


def _render_feed_card(label: str, feed_url: str):
    items = fetch_fews_feed(feed_url, limit=1)
    if not items:
        return
    item = items[0]

    with st.container(border=True):
        st.markdown(f"**📰 {label}**")
        if item["title"] and item["link"]:
            st.markdown(f"#### [{item['title']}]({item['link']})")
        elif item["title"]:
            st.markdown(f"#### {item['title']}")
        if item["pub_date"]:
            st.caption(f"Published: {item['pub_date']}")
        if item["summary"]:
            st.write(item["summary"])
        if item["link"]:
            st.link_button("Read the full report on FEWS NET ↗", item["link"])


def render_fews_report_card():
    """Render two side-by-side cards: latest Haiti report and latest Global Price Watch."""
    haiti_col, gpw_col = st.columns(2)
    with haiti_col:
        _render_feed_card("Latest FEWS NET Haiti report", FEWS_HAITI_FEED_URL)
    with gpw_col:
        _render_feed_card("Latest FEWS NET Global Price Watch", FEWS_GLOBAL_PRICE_WATCH_FEED_URL)


def calculate_statistics(df: pd.DataFrame, price_col: str) -> dict:
    """Calculate summary statistics for the price data."""
    if df.empty:
        return {}

    latest = df.iloc[-1]
    stats = {
        "current_price": latest[price_col],
        "current_date": latest["period_date"].strftime("%Y-%m-%d"),
    }

    # Month-over-month change
    if len(df) >= 2:
        prev_month = df.iloc[-2][price_col]
        if prev_month and prev_month > 0:
            stats["mom_change"] = ((latest[price_col] - prev_month) / prev_month) * 100

    # Year-over-year change (12 months ago)
    if len(df) >= 13:
        prev_year = df.iloc[-13][price_col]
        if prev_year and prev_year > 0:
            stats["yoy_change"] = ((latest[price_col] - prev_year) / prev_year) * 100

    # 12-month moving average
    if len(df) >= 12:
        stats["moving_avg_12m"] = df[price_col].tail(12).mean()

    return stats


@st.cache_data(ttl=600)
def _get_data_freshness() -> dict:
    """Return data-freshness state for the sidebar gate.

    - latest_period_date: max(period_date) in price_observations (the data
      itself, which advances when FEWS publishes a new month).
    - last_import_at: timestamp of the most recent successful import_log entry
      (when the API was last polled).
    """
    con = get_connection()
    latest = con.execute(
        "SELECT MAX(period_date) FROM price_observations"
    ).fetchone()[0]
    last_import = con.execute(
        """
        SELECT MAX(import_date)
        FROM import_log
        WHERE status = 'success'
        """
    ).fetchone()[0]
    return {
        "latest_period_date": latest,
        "last_import_at": last_import,
    }


def _next_expected_release(latest_period_date) -> "datetime | None":
    """Best-guess date the *next* FEWS release should appear.

    FEWS publishes month M's data ~80 days after month-end M. Given the latest
    period_date we hold (which is month-end), the next month's data should land
    around (latest_period_date + 1 month + lag).
    """
    if latest_period_date is None:
        return None
    # latest_period_date is a date or datetime; treat the next month as +31 days
    # for the rough estimate (we only care about week-level precision).
    return latest_period_date + timedelta(days=31 + FEWS_RELEASE_LAG_DAYS)


def _refresh_is_futile(freshness: dict) -> tuple[bool, str]:
    """Decide if clicking Update right now is wasted work.

    Returns (futile, reason_text). When futile=True, the reason explains why
    so we can show it in the button tooltip and on the gate label.
    """
    latest = freshness.get("latest_period_date")
    last_import = freshness.get("last_import_at")
    now = datetime.now(timezone.utc)

    if latest is None:
        return False, "No data yet — sync needed."

    # If we already hold a period_date close enough to today that the next
    # FEWS release is still in the future, polling won't return anything new.
    age_days = (now.date() - latest).days
    next_release = _next_expected_release(latest)
    if age_days < FEWS_RELEASE_LAG_DAYS:
        msg = (
            f"FEWS publishes with a ~{FEWS_RELEASE_LAG_DAYS}-day lag. "
            f"Latest month in DB: {latest}. "
            f"Next release expected ~{next_release if next_release else 'unknown'}."
        )
        return True, msg

    # Even if FEWS could plausibly have new data, throttle accidental rapid clicks.
    if last_import is not None:
        # DuckDB returns naive timestamps; treat as UTC.
        if last_import.tzinfo is None:
            last_import = last_import.replace(tzinfo=timezone.utc)
        if now - last_import < MIN_REFRESH_INTERVAL:
            mins = int((now - last_import).total_seconds() // 60)
            return True, (
                f"Last API check ran {mins} min ago. "
                f"Refreshes are rate-limited to once per "
                f"{int(MIN_REFRESH_INTERVAL.total_seconds() // 3600)}h."
            )

    return False, f"FEWS may have a new release (latest month in DB: {latest})."


def _render_sidebar_status(freshness: dict) -> None:
    """Persistent GCS + freshness banner at the top of the sidebar."""
    if GCS_BUCKET:
        last_upload = st.session_state.get("last_gcs_upload_at")
        upload_note = (
            f"  \nLast upload: {last_upload.strftime('%Y-%m-%d %H:%M UTC')}"
            if last_upload
            else ""
        )
        st.sidebar.success(
            f"GCS persistence ON  \n`gs://{GCS_BUCKET}/{GCS_BLOB_NAME}`{upload_note}"
        )
    else:
        st.sidebar.error(
            "GCS persistence is OFF — updates will be lost on container "
            "restart. Set the `GCS_BUCKET` env var on the Cloud Run service."
        )

    latest = freshness.get("latest_period_date")
    last_import = freshness.get("last_import_at")
    next_release = _next_expected_release(latest)
    lines = []
    if latest:
        lines.append(f"**Latest data point:** {latest}")
    if next_release:
        lines.append(f"**Next FEWS release ~** {next_release}")
    if last_import:
        if last_import.tzinfo is None:
            last_import = last_import.replace(tzinfo=timezone.utc)
        age_h = (datetime.now(timezone.utc) - last_import).total_seconds() / 3600
        lines.append(f"**Last refresh check:** {age_h:.1f}h ago")
    if lines:
        st.sidebar.info("  \n".join(lines))


def main():
    # Header
    st.title("🌾 Haiti Food Price Monitor")

    # Sidebar controls
    st.sidebar.header("Settings")
    freshness = _get_data_freshness()
    _render_sidebar_status(freshness)

    # Commodity selector
    commodities = get_commodities()
    default_idx = (
        commodities.index("Beans (black)") if "Beans (black)" in commodities else 0
    )
    selected_commodity = st.sidebar.selectbox(
        "Select Commodity", commodities, index=default_idx
    )

    # Currency toggle
    currency = st.sidebar.radio("Currency", ["HTG (Haitian Gourde)", "USD"], index=0)
    use_usd = currency == "USD"
    price_col = "mean_price_usd" if use_usd else "mean_price_htg"
    market_price_col = "price_usd" if use_usd else "price_htg"
    currency_symbol = "$" if use_usd else "HTG "

    # Date range
    min_date, max_date = get_date_range()
    date_range = st.sidebar.date_input(
        "Date Range",
        value=(min_date, max_date),
        min_value=min_date,
        max_value=max_date,
        key="date_range",
    )

    futile, futile_reason = _refresh_is_futile(freshness)
    if futile:
        st.sidebar.caption(futile_reason)
    force_refresh = False
    if futile:
        force_refresh = st.sidebar.checkbox(
            "Force refresh anyway",
            value=False,
            help="Hit the FEWS NET API even though no new data is expected.",
        )
    sidebar_refresh = st.sidebar.button(
        "🔄 Update Data & Models",
        help=(
            futile_reason
            if futile and not force_refresh
            else "Download latest FEWS NET data, then re-train Prophet models."
        ),
        disabled=futile and not force_refresh,
    )
    if sidebar_refresh:
        with st.spinner("Downloading latest data from FEWS NET..."):
            n_fetched, sync_stats = _incremental_sync()
        if n_fetched > 0:
            st.sidebar.success(
                f"Synced {n_fetched:,} records ({sync_stats['inserted']:,} new)."
            )
        else:
            st.sidebar.info("Data is already up to date.")
        # Persist the updated DB back to GCS so it survives container restarts.
        try:
            _push_db_to_gcs()
        except Exception as exc:
            print(
                f"[ERROR] GCS upload after sidebar sync failed: {exc!r}",
                file=sys.stderr,
                flush=True,
            )
            traceback.print_exc(file=sys.stderr)
            st.sidebar.warning(f"Sync saved locally but GCS upload failed: {exc}")
        st.session_state.forecast_models = {}
        st.cache_data.clear()
        # Reset the date-range widget so it re-initialises against the new
        # (min, max) returned by the freshly-uncached get_date_range(),
        # otherwise the user keeps seeing data clipped to the pre-sync window.
        st.session_state.pop("date_range", None)
        st.rerun()

    # Get data
    mean_df = get_mean_prices(selected_commodity)
    market_df = get_market_prices(selected_commodity)

    # Filter by date range
    if len(date_range) == 2:
        start_date, end_date = date_range
        mean_df = mean_df[
            (mean_df["period_date"] >= pd.to_datetime(start_date))
            & (mean_df["period_date"] <= pd.to_datetime(end_date))
        ]
        market_df = market_df[
            (market_df["period_date"] >= pd.to_datetime(start_date))
            & (market_df["period_date"] <= pd.to_datetime(end_date))
        ]

    # Statistics sidebar
    st.sidebar.markdown("---")
    st.sidebar.header("Summary Statistics")

    stats = calculate_statistics(mean_df, price_col)
    if stats:
        st.sidebar.metric(
            "Current Price (Mean)",
            f"{currency_symbol}{stats['current_price']:.2f}",
            delta=(
                f"{stats.get('mom_change', 0):.1f}% MoM"
                if "mom_change" in stats
                else None
            ),
        )

        if "yoy_change" in stats:
            st.sidebar.metric("Year-over-Year Change", f"{stats['yoy_change']:.1f}%")

        if "moving_avg_12m" in stats:
            st.sidebar.metric(
                "12-Month Moving Avg", f"{currency_symbol}{stats['moving_avg_12m']:.2f}"
            )

        st.sidebar.caption(f"As of {stats['current_date']}")

    # Main content - tabs
    tab1, tab2, tab3 = st.tabs(
        ["📈 Price Trend", "🏪 Market Comparison", "🔮 Price Forecast"]
    )

    with tab1:
        st.subheader(f"Mean Price: {selected_commodity}")

        if mean_df.empty:
            st.warning("No data available for the selected filters.")
        else:
            # Create complete monthly date range and interpolate missing values
            plot_df = mean_df.copy()
            plot_df = plot_df.set_index("period_date")

            # Create complete monthly date range
            full_range = pd.date_range(
                start=plot_df.index.min(),
                end=plot_df.index.max(),
                freq="MS",  # Month start
            )
            # Shift to month end to match data
            full_range = full_range + pd.offsets.MonthEnd(0)

            # Reindex to full range, marking which rows are interpolated
            plot_df = plot_df.reindex(full_range)
            plot_df["is_interpolated"] = plot_df[price_col].isna()

            # Interpolate missing values
            for col in [
                "mean_price_htg",
                "mean_price_usd",
                "min_price_htg",
                "max_price_htg",
            ]:
                if col in plot_df.columns:
                    plot_df[col] = plot_df[col].interpolate(method="linear")

            plot_df = plot_df.reset_index().rename(columns={"index": "period_date"})

            # Calculate min/max in selected currency
            if use_usd:
                ratio = plot_df["mean_price_usd"] / plot_df["mean_price_htg"]
                min_price = plot_df["min_price_htg"] * ratio
                max_price = plot_df["max_price_htg"] * ratio
            else:
                min_price = plot_df["min_price_htg"]
                max_price = plot_df["max_price_htg"]

            # Create figure
            fig = go.Figure()

            # Add min bound (invisible line for fill reference)
            fig.add_trace(
                go.Scatter(
                    x=plot_df["period_date"],
                    y=min_price,
                    mode="lines",
                    line=dict(width=0),
                    showlegend=False,
                    hoverinfo="skip",
                )
            )

            # Add max bound with fill to min
            fig.add_trace(
                go.Scatter(
                    x=plot_df["period_date"],
                    y=max_price,
                    mode="lines",
                    line=dict(width=0),
                    fill="tonexty",
                    fillcolor="rgba(31, 119, 180, 0.15)",
                    name="Price Range (Min-Max)",
                    hoverinfo="skip",
                )
            )

            # Split data into actual and interpolated segments for different colors
            # Add actual data points (blue)
            actual_mask = ~plot_df["is_interpolated"]
            fig.add_trace(
                go.Scatter(
                    x=plot_df.loc[actual_mask, "period_date"],
                    y=plot_df.loc[actual_mask, price_col],
                    mode="lines+markers",
                    name="Actual Price",
                    line=dict(color="#1f77b4", width=2),
                    marker=dict(size=4),
                    hovertemplate=f"Date: %{{x|%Y-%m}}<br>Price: {currency_symbol}%{{y:.2f}}<extra></extra>",
                )
            )

            # Add interpolated segments (red dashed)
            # Find runs of interpolated points and connect them to actual points
            interp_mask = plot_df["is_interpolated"]
            if interp_mask.any():
                # Create segments that include interpolated points and their neighbors
                plot_df["segment"] = (~interp_mask).cumsum()
                for seg_id in plot_df.loc[interp_mask, "segment"].unique():
                    # Get interpolated points in this segment
                    seg_mask = (plot_df["segment"] == seg_id) & interp_mask
                    seg_indices = plot_df[seg_mask].index.tolist()

                    if seg_indices:
                        # Include one point before and after for continuity
                        start_idx = max(0, seg_indices[0] - 1)
                        end_idx = min(len(plot_df) - 1, seg_indices[-1] + 1)
                        seg_data = plot_df.iloc[start_idx : end_idx + 1]

                        fig.add_trace(
                            go.Scatter(
                                x=seg_data["period_date"],
                                y=seg_data[price_col],
                                mode="lines",
                                line=dict(color="red", width=2, dash="dot"),
                                showlegend=False,
                                hovertemplate=f"Date: %{{x|%Y-%m}}<br>Price: {currency_symbol}%{{y:.2f}} (interpolated)<extra></extra>",
                            )
                        )

                # Add legend entry for interpolated
                fig.add_trace(
                    go.Scatter(
                        x=[None],
                        y=[None],
                        mode="lines",
                        line=dict(color="red", width=2, dash="dot"),
                        name="Interpolated (missing data)",
                    )
                )

            fig.update_layout(
                xaxis_title="Date",
                yaxis_title=f"Price ({currency.split()[0]})",
                hovermode="x unified",
                legend=dict(orientation="h", yanchor="bottom", y=1.02),
                margin=dict(l=0, r=0, t=30, b=0),
            )

            st.plotly_chart(fig, use_container_width=True)

            render_fews_report_card()

            # Show data table
            with st.expander("View Data"):
                display_df = mean_df[["period_date", price_col, "num_markets"]].copy()
                display_df.columns = [
                    "Date",
                    f"Mean Price ({currency.split()[0]})",
                    "# Markets",
                ]
                display_df["Date"] = display_df["Date"].dt.strftime("%Y-%m")
                st.dataframe(display_df.tail(24), use_container_width=True)

    with tab2:
        st.subheader(f"Market Comparison: {selected_commodity}")

        # Market selector
        markets = get_markets()
        selected_markets = st.multiselect(
            "Select Markets to Compare",
            markets,
            default=markets[:5],  # Default to first 5 markets
        )

        if market_df.empty:
            st.warning("No data available for the selected filters.")
        elif not selected_markets:
            st.info("Select one or more markets to compare.")
        else:
            # Filter to selected markets
            filtered_market_df = market_df[market_df["market"].isin(selected_markets)]

            # Pivot for easier plotting
            pivot_df = filtered_market_df.pivot(
                index="period_date", columns="market", values=market_price_col
            ).reset_index()

            # Create figure
            fig = go.Figure()

            # Add mean line (bold)
            fig.add_trace(
                go.Scatter(
                    x=mean_df["period_date"],
                    y=mean_df[price_col],
                    mode="lines",
                    name="Mean (All Markets)",
                    line=dict(color="black", width=3),
                    hovertemplate=f"Mean: {currency_symbol}%{{y:.2f}}<extra></extra>",
                )
            )

            # Add individual market lines
            colors = px.colors.qualitative.Set2
            for i, market in enumerate(selected_markets):
                if market in pivot_df.columns:
                    fig.add_trace(
                        go.Scatter(
                            x=pivot_df["period_date"],
                            y=pivot_df[market],
                            mode="lines",
                            name=market,
                            line=dict(color=colors[i % len(colors)], width=1.5),
                            opacity=0.7,
                            hovertemplate=f"{market}: {currency_symbol}%{{y:.2f}}<extra></extra>",
                        )
                    )

            fig.update_layout(
                xaxis_title="Date",
                yaxis_title=f"Price ({currency.split()[0]})",
                hovermode="x unified",
                legend=dict(orientation="h", yanchor="bottom", y=1.02),
                margin=dict(l=0, r=0, t=30, b=0),
            )

            st.plotly_chart(fig, use_container_width=True)

            render_fews_report_card()

            # Show latest prices table
            with st.expander("Latest Prices by Market"):
                latest_date = filtered_market_df["period_date"].max()
                latest_df = filtered_market_df[
                    filtered_market_df["period_date"] == latest_date
                ][["market", market_price_col]].copy()
                latest_df.columns = ["Market", f"Price ({currency.split()[0]})"]
                latest_df = latest_df.sort_values(
                    f"Price ({currency.split()[0]})", ascending=False
                )
                st.dataframe(latest_df, use_container_width=True)

    with tab3:
        st.subheader(f"12-Month Price Forecast: {selected_commodity}")
        st.markdown(
            "*Forecasts generated using Facebook Prophet with automatic seasonality detection*"
        )

        # Initialize session state for model caching
        if "forecast_models" not in st.session_state:
            st.session_state.forecast_models = {}
        if "forecast_product" not in st.session_state:
            st.session_state.forecast_product = None
        if "forecast_currency" not in st.session_state:
            st.session_state.forecast_currency = None

        # Check if we need to refresh models
        need_refresh = (
            st.session_state.forecast_product != selected_commodity
            or st.session_state.forecast_currency != currency
        )

        # Controls
        forecast_horizon = st.slider(
            "Forecast Horizon (Months)",
            min_value=1,
            max_value=12,
            value=8,
            help="Number of months to forecast into the future",
        )

        # Fit models if needed
        if need_refresh or not st.session_state.forecast_models:
            with st.spinner("Training Prophet models... This may take a minute."):
                results, availability = fit_all_models(
                    str(DB_PATH),
                    selected_commodity,
                    currency="USD" if use_usd else "HTG",
                    min_months=24,
                    conn=get_connection(),
                )

                st.session_state.forecast_models = results
                st.session_state.forecast_availability = availability
                st.session_state.forecast_product = selected_commodity
                st.session_state.forecast_currency = currency
        else:
            results = st.session_state.forecast_models
            availability = st.session_state.forecast_availability

        if not results:
            st.warning(
                "No data available for forecasting. This commodity may not have sufficient historical data."
            )
        else:
            # Generate forecasts
            forecasts = generate_all_forecasts(results, periods=forecast_horizon)

            # Market selector with availability info
            available_markets = [
                m
                for m in results.keys()
                if results[m].success and m != "Market Average"
            ]

            # Create market options with tooltips
            market_options = []
            disabled_markets = []

            for market_name, info in availability.items():
                if info["sufficient"]:
                    market_options.append(market_name)
                else:
                    disabled_markets.append(market_name)

            # View selector
            view_mode = st.radio(
                "View Mode", ["Market Average", "Individual Markets"], horizontal=True
            )

            if view_mode == "Market Average":
                # Show market average forecast
                if "Market Average" in forecasts:
                    forecast_df = forecasts["Market Average"]
                    model = results["Market Average"].model

                    # Split historical and future using the training data's last date
                    train_end = model.history["ds"].max()
                    historical_df = forecast_df[forecast_df["ds"] <= train_end]
                    future_df = forecast_df[forecast_df["ds"] > train_end]

                    # Create figure
                    fig = go.Figure()

                    # Historical actuals (raw observations the model was trained on)
                    actuals = model.history[["ds", "y"]].tail(36)
                    fig.add_trace(
                        go.Scatter(
                            x=actuals["ds"],
                            y=actuals["y"],
                            mode="lines+markers",
                            name="Historical (Actual)",
                            line=dict(color="blue", width=2),
                            marker=dict(size=4),
                            hovertemplate=f"Date: %{{x}}<br>Price: {currency_symbol}%{{y:.2f}}<extra></extra>",
                        )
                    )

                    # Forecast
                    fig.add_trace(
                        go.Scatter(
                            x=future_df["ds"],
                            y=future_df["yhat"],
                            mode="lines",
                            name="Forecast",
                            line=dict(color="red", width=2, dash="dash"),
                            hovertemplate=f"Date: %{{x}}<br>Forecast: {currency_symbol}%{{y:.2f}}<extra></extra>",
                        )
                    )

                    # 95% confidence interval
                    fig.add_trace(
                        go.Scatter(
                            x=future_df["ds"].tolist() + future_df["ds"].tolist()[::-1],
                            y=future_df["yhat_upper"].tolist()
                            + future_df["yhat_lower"].tolist()[::-1],
                            fill="toself",
                            fillcolor="rgba(255, 0, 0, 0.1)",
                            line=dict(color="rgba(255, 0, 0, 0)"),
                            name="95% Confidence",
                            hoverinfo="skip",
                            showlegend=True,
                        )
                    )

                    # 80% confidence interval
                    fig.add_trace(
                        go.Scatter(
                            x=future_df["ds"].tolist() + future_df["ds"].tolist()[::-1],
                            y=(
                                future_df["yhat"]
                                + (future_df["yhat_upper"] - future_df["yhat"]) * 0.8
                            ).tolist()
                            + (
                                future_df["yhat"]
                                - (future_df["yhat"] - future_df["yhat_lower"]) * 0.8
                            ).tolist()[::-1],
                            fill="toself",
                            fillcolor="rgba(255, 0, 0, 0.2)",
                            line=dict(color="rgba(255, 0, 0, 0)"),
                            name="80% Confidence",
                            hoverinfo="skip",
                            showlegend=True,
                        )
                    )

                    fig.update_layout(
                        title=f"Market Average Forecast - {selected_commodity}",
                        xaxis_title="Date",
                        yaxis_title=f"Price ({currency.split()[0]})",
                        hovermode="x unified",
                        legend=dict(orientation="h", yanchor="bottom", y=1.02),
                        height=500,
                    )

                    st.plotly_chart(fig, use_container_width=True)

                    render_fews_report_card()

                    # Forecast table
                    with st.expander("📋 Forecast Table"):
                        table_df = future_df[
                            ["ds", "yhat", "yhat_lower", "yhat_upper"]
                        ].copy()
                        table_df.columns = [
                            "Date",
                            "Forecast",
                            "95% Lower",
                            "95% Upper",
                        ]
                        table_df["Date"] = table_df["Date"].dt.strftime("%Y-%m")
                        for col in ["Forecast", "95% Lower", "95% Upper"]:
                            table_df[col] = table_df[col].apply(
                                lambda x: f"{currency_symbol}{x:.2f}"
                            )
                        st.dataframe(table_df, use_container_width=True)

                    # Prophet components
                    with st.expander("🔍 Model Components (Trend & Seasonality)"):
                        from prophet.plot import plot_components_plotly

                        components_fig = plot_components_plotly(model, forecast_df)
                        st.plotly_chart(components_fig, use_container_width=True)
                        st.caption(
                            "Prophet automatically detects and separates trend and seasonal patterns"
                        )

                else:
                    st.error("Market average forecast failed to generate.")
                    if "Market Average" in results and results["Market Average"].error:
                        st.error(f"Error: {results['Market Average'].error}")

            else:  # Individual Markets
                selected_forecast_markets = st.multiselect(
                    "Select Markets to Display",
                    market_options,
                    default=(
                        market_options[:3]
                        if len(market_options) >= 3
                        else market_options
                    ),
                    help="Choose which markets to show in the forecast",
                )

                if not selected_forecast_markets:
                    st.info("Select at least one market to view forecasts.")
                else:
                    # Get historical market data for selected markets
                    historical_market_df = market_df[
                        market_df["market"].isin(selected_forecast_markets)
                    ]

                    # Create combined plot
                    fig = go.Figure()
                    colors = px.colors.qualitative.Set2

                    for i, market_name in enumerate(selected_forecast_markets):
                        color = colors[i % len(colors)]

                        # Convert color to rgba format if needed
                        if color.startswith("#"):
                            # Hex color - convert to rgb
                            rgb = px.colors.hex_to_rgb(color)
                            rgba_fill = f"rgba({rgb[0]}, {rgb[1]}, {rgb[2]}, 0.1)"
                        elif color.startswith("rgb("):
                            # Already rgb format - extract values and add alpha
                            rgb_values = (
                                color.replace("rgb(", "").replace(")", "").split(",")
                            )
                            rgba_fill = f"rgba({rgb_values[0]}, {rgb_values[1]}, {rgb_values[2]}, 0.1)"
                        else:
                            # Fallback
                            rgba_fill = "rgba(128, 128, 128, 0.1)"

                        # Get historical data for this market
                        market_historical = historical_market_df[
                            historical_market_df["market"] == market_name
                        ]

                        if len(market_historical) > 0:
                            # Show historical actual data
                            fig.add_trace(
                                go.Scatter(
                                    x=market_historical["period_date"].tail(36),
                                    y=market_historical[market_price_col].tail(36),
                                    mode="lines",
                                    name=f"{market_name}",
                                    line=dict(color=color, width=2),
                                    legendgroup=market_name,
                                    hovertemplate=f"{market_name}<br>Date: %{{x}}<br>Price: {currency_symbol}%{{y:.2f}}<extra></extra>",
                                )
                            )

                        if market_name in forecasts:
                            forecast_df = forecasts[market_name]

                            train_end = results[market_name].model.history["ds"].max()
                            future_df = forecast_df[forecast_df["ds"] > train_end]

                            # Forecast
                            fig.add_trace(
                                go.Scatter(
                                    x=future_df["ds"],
                                    y=future_df["yhat"],
                                    mode="lines",
                                    name=f"{market_name} (Forecast)",
                                    line=dict(color=color, width=2, dash="dash"),
                                    legendgroup=market_name,
                                    showlegend=False,
                                    hovertemplate=f"{market_name} Forecast<br>Date: %{{x}}<br>Price: {currency_symbol}%{{y:.2f}}<extra></extra>",
                                )
                            )

                            # Confidence interval (lighter)
                            fig.add_trace(
                                go.Scatter(
                                    x=future_df["ds"].tolist()
                                    + future_df["ds"].tolist()[::-1],
                                    y=future_df["yhat_upper"].tolist()
                                    + future_df["yhat_lower"].tolist()[::-1],
                                    fill="toself",
                                    fillcolor=rgba_fill,
                                    line=dict(color="rgba(255,255,255,0)"),
                                    showlegend=False,
                                    legendgroup=market_name,
                                    hoverinfo="skip",
                                )
                            )
                        else:
                            # Show info that forecast is not available but historical data is shown
                            if (
                                market_name in results
                                and not results[market_name].success
                            ):
                                st.warning(
                                    f"**{market_name}**: Forecast unavailable ({results[market_name].error}), but historical data is shown"
                                )
                            elif market_name not in results:
                                st.warning(
                                    f"**{market_name}**: Insufficient data for forecasting, but historical data is shown"
                                )

                    fig.update_layout(
                        title=f"Individual Market Forecasts - {selected_commodity}",
                        xaxis_title="Date",
                        yaxis_title=f"Price ({currency.split()[0]})",
                        hovermode="x unified",
                        legend=dict(orientation="v", yanchor="top", y=1),
                        height=600,
                    )

                    st.plotly_chart(fig, use_container_width=True)

                    render_fews_report_card()

                    # Forecast comparison table
                    with st.expander("📋 Forecast Comparison Table"):
                        comparison_data = []
                        for market_name in selected_forecast_markets:
                            if market_name in forecasts:
                                forecast_df = forecasts[market_name]
                                train_end = results[market_name].model.history["ds"].max()
                                future_df = forecast_df[forecast_df["ds"] > train_end]

                                for _, row in future_df.iterrows():
                                    comparison_data.append(
                                        {
                                            "Market": market_name,
                                            "Date": row["ds"].strftime("%Y-%m"),
                                            "Forecast": f"{currency_symbol}{row['yhat']:.2f}",
                                            "95% Lower": f"{currency_symbol}{row['yhat_lower']:.2f}",
                                            "95% Upper": f"{currency_symbol}{row['yhat_upper']:.2f}",
                                        }
                                    )

                        if comparison_data:
                            comparison_df = pd.DataFrame(comparison_data)
                            st.dataframe(comparison_df, use_container_width=True)

    # Footer
    st.markdown("---")
    cap_col, logo_col = st.columns([3, 1])
    with cap_col:
        st.caption(
            "Created by [Michael Mann, PhD](https://mmann1123.github.io/). "
            "Data source: [FEWS NET Data Warehouse](https://fdw.fews.net/) — "
            "price data collected by CNSA/FEWS NET Haiti, used under the "
            "[FEWS NET data attribution policy](https://fews.net/data-attribution). "
            "This site is independent and not affiliated with or endorsed by FEWS NET. "
            "Forecasts are produced by this dashboard, not by FEWS NET, and are "
            "provided without warranty of accuracy or fitness for any purpose."
        )
    with logo_col:
        logo_path = Path(__file__).parent / "static" / "gwugeog.png"
        if logo_path.exists():
            import base64
            b64 = base64.b64encode(logo_path.read_bytes()).decode()
            st.markdown(
                f'<a href="https://geography.columbian.gwu.edu/" target="_blank" rel="noopener">'
                f'<img src="data:image/png;base64,{b64}" width="280" alt="GW Geography &amp; Environment"></a>',
                unsafe_allow_html=True,
            )


if __name__ == "__main__":
    main()
