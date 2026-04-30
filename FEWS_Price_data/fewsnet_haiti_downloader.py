#!/usr/bin/env python3
"""
FEWS NET Haiti Price Data Downloader
=====================================
Downloads market price data for Haiti from the FEWS NET Data Warehouse API.

The FEWS NET price data is PUBLIC and requires NO authentication!

Requirements:
    pip install requests pandas

Usage:
    python fewsnet_haiti_downloader.py

Available Data:
    - 11 markets across Haiti (Port-au-Prince, Cap Haitien, Gonaives, etc.)
    - 43 commodities (rice, beans, maize, oil, sugar, fuel, etc.)
    - Data from January 2005 to present (~68,000+ records)
    - Monthly collection frequency
"""

import sys
import time
from datetime import datetime, timedelta
from io import StringIO
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import pandas as pd

# Configuration
BASE_URL = "https://fdw.fews.net/api"
OUTPUT_DIR = Path(__file__).parent / "data"
COUNTRY_CODE = "HT"  # Haiti
REQUEST_TIMEOUT = 300  # 5 minutes
MAX_RETRIES = 3


class FEWSNETClient:
    """Client for the FEWS NET Data Warehouse API (public, no auth required)."""

    def __init__(self):
        self.session = requests.Session()
        # Configure retries
        retry_strategy = Retry(
            total=MAX_RETRIES,
            backoff_factor=2,  # Wait 2, 4, 8 seconds between retries
            status_forcelist=[429, 500, 502, 503, 504],
        )
        adapter = HTTPAdapter(max_retries=retry_strategy)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

    def _make_request(self, endpoint, params=None, format="json"):
        """Make a request to the FEWS NET API with retries."""
        url = f"{BASE_URL}/{endpoint}/"
        if params is None:
            params = {}
        params["format"] = format

        for attempt in range(MAX_RETRIES):
            try:
                response = self.session.get(url, params=params, timeout=REQUEST_TIMEOUT)
                response.raise_for_status()

                if format == "json":
                    return response.json()
                return response.text
            except requests.exceptions.Timeout:
                if attempt < MAX_RETRIES - 1:
                    wait = (attempt + 1) * 10
                    print(f"[WARN] Timeout, retrying in {wait}s... (attempt {attempt + 1}/{MAX_RETRIES})")
                    time.sleep(wait)
                else:
                    raise
            except requests.exceptions.RequestException:
                if attempt < MAX_RETRIES - 1:
                    wait = (attempt + 1) * 5
                    print(f"[WARN] Request failed, retrying in {wait}s... (attempt {attempt + 1}/{MAX_RETRIES})")
                    time.sleep(wait)
                else:
                    raise

    def get_market_prices(
        self,
        country_code="HT",
        start_date=None,
        end_date=None,
        product=None,
        market=None,
        limit=None,
    ):
        """
        Fetch market price data.

        Args:
            country_code: ISO country code (default: 'HT' for Haiti)
            start_date: Start date (YYYY-MM-DD format)
            end_date: End date (YYYY-MM-DD format)
            product: Filter by product name (e.g., 'Beans (Black)')
            market: Filter by market name (e.g., 'Port-au-Prince')
            limit: Maximum number of records to return

        Returns:
            pandas DataFrame with market price data
        """
        params = {"country_code": country_code}

        if start_date:
            params["start_date"] = start_date
        if end_date:
            params["end_date"] = end_date
        if product:
            params["product"] = product
        if market:
            params["market"] = market
        if limit:
            params["limit"] = limit

        print(f"[INFO] Fetching market prices...")
        print(f"       Parameters: {params}")

        json_data = self._make_request("marketpricefacts", params, format="json")
        df = pd.DataFrame(json_data)

        print(f"[OK] Retrieved {len(df)} records")
        return df

    def get_markets(self, country_code="HT"):
        """Get list of markets for a country."""
        print(f"[INFO] Fetching markets for {country_code}...")
        data = self._make_request("market", {"country_code": country_code}, format="json")
        df = pd.DataFrame(data)
        print(f"[OK] Found {len(df)} markets")
        return df

    def get_commodities(self, country_code="HT"):
        """Get list of commodities available for a country."""
        print(f"[INFO] Fetching commodities for {country_code}...")

        # Get sample to extract unique products
        data = self._make_request(
            "marketpricefacts",
            {"country_code": country_code, "limit": 10000},
            format="json"
        )
        df = pd.DataFrame(data)

        if "product" in df.columns:
            products = sorted(df["product"].unique())
            print(f"[OK] Found {len(products)} unique commodities")
            return pd.DataFrame({"product": products})
        return pd.DataFrame()

    def test_connection(self):
        """Test API connection using the faster markets endpoint."""
        print("[INFO] Testing API connection (this may take a moment)...")
        try:
            # Use markets endpoint - it's faster than price data
            self._make_request("market", {"country_code": "HT"}, format="json")
            print("[OK] API connection successful!")
            return True
        except Exception as e:
            print(f"[ERROR] Connection test failed: {e}")
            return False


def download_haiti_data(client, start_date=None, end_date=None, output_file=None):
    """Download Haiti market price data."""

    if end_date is None:
        end_date = datetime.now().strftime("%Y-%m-%d")
    if start_date is None:
        start_date = "2005-01-01"  # All historical data

    print(f"\n{'='*60}")
    print("Downloading Haiti Market Price Data")
    print(f"{'='*60}")
    print(f"Date range: {start_date} to {end_date}")

    df = client.get_market_prices(
        country_code=COUNTRY_CODE,
        start_date=start_date,
        end_date=end_date,
    )

    if df.empty:
        print("[WARN] No data retrieved")
        return df

    # Display summary
    print(f"\n{'='*60}")
    print("Data Summary")
    print(f"{'='*60}")
    print(f"Total records: {len(df)}")

    if "product" in df.columns:
        print(f"\nCommodities ({df['product'].nunique()}):")
        for prod in sorted(df["product"].unique()):
            count = len(df[df["product"] == prod])
            print(f"  - {prod}: {count} records")

    if "market" in df.columns:
        print(f"\nMarkets ({df['market'].nunique()}):")
        for market in sorted(df["market"].unique()):
            count = len(df[df["market"] == market])
            print(f"  - {market}: {count} records")

    if "period_date" in df.columns:
        df["period_date"] = pd.to_datetime(df["period_date"])
        print(f"\nDate range in data:")
        print(f"  Earliest: {df['period_date'].min().strftime('%Y-%m-%d')}")
        print(f"  Latest: {df['period_date'].max().strftime('%Y-%m-%d')}")

    # Save to CSV
    if output_file is None:
        OUTPUT_DIR.mkdir(exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d")
        output_file = OUTPUT_DIR / f"haiti_fewsnet_prices_{timestamp}.csv"

    df.to_csv(output_file, index=False, encoding="utf-8-sig")
    print(f"\n[OK] Data saved to: {output_file}")

    return df


def explore_available_data(client):
    """Explore what data is available for Haiti."""
    print(f"\n{'='*60}")
    print("Exploring Available Haiti Data")
    print(f"{'='*60}")

    # Get markets
    print("\n--- Markets ---")
    markets_df = client.get_markets(COUNTRY_CODE)
    if not markets_df.empty and "name" in markets_df.columns:
        for _, row in markets_df.iterrows():
            admin = row.get("admin_1", "")
            print(f"  - {row['name']} ({admin})")

    # Get commodities
    print("\n--- Commodities ---")
    commodities_df = client.get_commodities(COUNTRY_CODE)
    if not commodities_df.empty:
        for _, row in commodities_df.iterrows():
            print(f"  - {row['product']}")

    return commodities_df, markets_df


def main():
    print("=" * 60)
    print("FEWS NET Haiti Price Data Downloader")
    print("=" * 60)
    print("\nThis API is PUBLIC - no authentication required!")

    # Initialize client
    client = FEWSNETClient()

    # Test connection
    if not client.test_connection():
        print("\n[ERROR] Could not connect to API.")
        sys.exit(1)

    # Menu
    print(f"\n{'='*60}")
    print("Options:")
    print("  1. Download all Haiti price data (2005-present)")
    print("  2. Download Haiti price data (custom date range)")
    print("  3. Explore available commodities and markets")
    print("  4. Download data for specific commodity")
    print("  5. Download data for specific market")
    print("  6. Exit")
    print(f"{'='*60}")

    choice = input("\nSelect option (1-6): ").strip()

    if choice == "1":
        download_haiti_data(client)

    elif choice == "2":
        start = input("Start date (YYYY-MM-DD): ").strip()
        end = input("End date (YYYY-MM-DD): ").strip()
        download_haiti_data(client, start_date=start, end_date=end)

    elif choice == "3":
        explore_available_data(client)

    elif choice == "4":
        explore_available_data(client)
        product = input("\nEnter commodity name (exact match): ").strip()
        df = client.get_market_prices(product=product)
        if not df.empty:
            OUTPUT_DIR.mkdir(exist_ok=True)
            safe_name = product.replace(" ", "_").replace("(", "").replace(")", "")
            output_file = OUTPUT_DIR / f"haiti_{safe_name}.csv"
            df.to_csv(output_file, index=False, encoding="utf-8-sig")
            print(f"[OK] Saved to: {output_file}")

    elif choice == "5":
        explore_available_data(client)
        market = input("\nEnter market name (exact match): ").strip()
        df = client.get_market_prices(market=market)
        if not df.empty:
            OUTPUT_DIR.mkdir(exist_ok=True)
            safe_name = market.replace(" ", "_").replace(",", "")
            output_file = OUTPUT_DIR / f"haiti_{safe_name}.csv"
            df.to_csv(output_file, index=False, encoding="utf-8-sig")
            print(f"[OK] Saved to: {output_file}")

    elif choice == "6":
        print("Goodbye!")
        sys.exit(0)

    else:
        print("[ERROR] Invalid option")
        sys.exit(1)


if __name__ == "__main__":
    main()
