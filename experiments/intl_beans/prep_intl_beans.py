#!/usr/bin/env python3
"""
prep_intl_beans.py -- one-shot fetcher for USDA NASS US dry-bean prices.

Note on commodity match: NASS does NOT track Black dry beans separately
at the national level. The closest available series is the aggregate
class "BEANS, DRY EDIBLE, (EXCL CHICKPEAS)" -- a US producer-price index
for all dry-bean varieties together (Black, Pinto, Navy, Kidney, ...).
Black beans are typically ~10-15% of US dry-bean production by volume,
so this aggregate is a noisy proxy for the Black-specific series we'd
ideally want. Coverage: 2019-onward, monthly.

Run from bayesian_analysis/ with NASS_API_KEY in env:
    python3 prep_intl_beans.py

Output: bayesian_analysis/intl_beans_usda.csv with columns
    month, source, unit, price_usd_kg
where month is the first-of-month timestamp.
"""
import os
import sys
import requests
import pandas as pd
from pathlib import Path

NASS_KEY = os.environ.get("NASS_API_KEY", "").strip()
OUT_PATH = Path("intl_beans_usda.csv")
NASS_URL = "https://quickstats.nass.usda.gov/api/api_GET/"
CWT_TO_KG = 1.0 / 45.359237  # 1 CWT (US hundredweight) = 45.359 Kg


def _instructions_and_exit() -> None:
    """Print signup steps and exit with non-zero so the caller knows we bailed."""
    print(
        "ERROR: NASS_API_KEY environment variable is not set.\n"
        "\n"
        "Sign up (instant, free) at:\n"
        "    https://quickstats.nass.usda.gov/api\n"
        "\n"
        "Then add to ~/.bashrc (or ~/.zshrc):\n"
        "    export NASS_API_KEY='<your-key>'\n"
        "And reload:\n"
        "    source ~/.bashrc\n"
        "\n"
        "Or set just for this command:\n"
        "    NASS_API_KEY='<your-key>' python3 prep_intl_beans.py\n",
        file=sys.stderr,
    )
    sys.exit(2)


def fetch_nass() -> pd.DataFrame:
    """Query NASS for monthly Black-dry-bean price-received and return a DataFrame."""
    # NASS-validated filters. `commodity_desc=BEANS` + `class_desc=DRY EDIBLE,
    # (EXCL CHICKPEAS)` is the most-Black-bean-like aggregate NASS exposes
    # nationally. `short_desc` pins the units to $ / CWT (avoiding "PCT OF
    # PARITY" mirrors that share the class).
    params = dict(
        key                = NASS_KEY,
        commodity_desc     = "BEANS",
        class_desc         = "DRY EDIBLE, (EXCL CHICKPEAS)",
        statisticcat_desc  = "PRICE RECEIVED",
        agg_level_desc     = "NATIONAL",
        short_desc         = "BEANS, DRY EDIBLE, (EXCL CHICKPEAS) - PRICE RECEIVED, MEASURED IN $ / CWT",
        format             = "JSON",
    )
    r = requests.get(NASS_URL, params=params, timeout=60)
    r.raise_for_status()
    payload = r.json()
    if "data" not in payload:
        raise RuntimeError(f"Unexpected NASS payload: keys={list(payload.keys())}")
    return pd.DataFrame(payload["data"])


def tidy(raw: pd.DataFrame) -> pd.DataFrame:
    """Parse NASS rows into monthly first-of-month USD/Kg prices."""
    # NASS returns one row per (year, period). For monthly data, period is the
    # month name in upper case ("JAN", "FEB", ..., "DEC"). Other rows (year
    # totals, season ranges) have period values like "YEAR" -- drop those.
    month_map = {"JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
                 "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12}
    rows = raw[raw["reference_period_desc"].str.upper().isin(month_map.keys())].copy()
    if rows.empty:
        # Fallback for newer schemas where period name lives in `period_desc`
        rows = raw[raw["period_desc"].str.upper().isin(month_map.keys())].copy()
        rows["reference_period_desc"] = rows["period_desc"]
    rows["year"]  = rows["year"].astype(int)
    rows["month_num"] = rows["reference_period_desc"].str.upper().map(month_map)
    rows["month"] = pd.to_datetime(
        rows["year"].astype(str) + "-" + rows["month_num"].astype(str) + "-01"
    )
    # Drop rows with non-numeric values (NASS uses "(D)" for disclosure-suppressed cells).
    rows["price_usd_cwt"] = pd.to_numeric(rows["Value"].str.replace(",", ""),
                                          errors="coerce")
    rows = rows.dropna(subset=["price_usd_cwt"]).copy()
    rows["price_usd_kg"] = rows["price_usd_cwt"] * CWT_TO_KG
    rows["source"] = "USDA NASS Dry Edible Beans (excl chickpeas) Price Received"
    rows["unit"]   = "USD/Kg (converted from $/CWT)"
    out = rows[["month", "source", "unit", "price_usd_kg"]].sort_values("month")
    return out.reset_index(drop=True)


def main() -> None:
    if not NASS_KEY:
        _instructions_and_exit()
    print("Fetching NASS US dry-edible-bean (excl chickpeas) monthly prices...")
    raw = fetch_nass()
    print(f"  raw rows returned: {len(raw)}")
    df = tidy(raw)
    df["month"] = df["month"].dt.strftime("%Y-%m-%d")
    df.to_csv(OUT_PATH, index=False, float_format="%.6f")
    print(f"\nWrote {OUT_PATH} ({len(df)} monthly rows)")
    print(f"Date range: {df['month'].min()} to {df['month'].max()}")
    print(f"Price (USD/Kg) range: {df['price_usd_kg'].min():.3f} to "
          f"{df['price_usd_kg'].max():.3f}")
    print("\nFirst rows:")
    print(df.head().to_string(index=False))
    print("\nLast rows:")
    print(df.tail().to_string(index=False))


if __name__ == "__main__":
    main()
