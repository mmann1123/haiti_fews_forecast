#!/usr/bin/env python3
"""
prep_intl_prices.py -- one-shot fetcher for USDA NASS national price-received
series across all the agricultural commodities that have a plausible
Haiti-FEWS counterpart.

USDA only publishes US producer prices (price received by US farmers), so
each pairing below is "US producer vs Haiti retail." Useful for testing
whether global staple-price moves lead Haiti retail at any practical lag.

Run with NASS_API_KEY in env:
    NASS_API_KEY=... python3 prep_intl_prices.py

Output: experiments/intl_beans/intl_prices_usda.csv (long form)
    month, usda_slug, source, unit, price_usd_kg

`usda_slug` matches the keys correlations.R uses to join against the FEWS
side (see the PAIRS table in that script). All prices are converted to
USD/Kg so cross-commodity scales are comparable.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pandas as pd
import requests

NASS_KEY = os.environ.get("NASS_API_KEY", "").strip()
NASS_URL = "https://quickstats.nass.usda.gov/api/api_GET/"
OUT_PATH = Path(__file__).resolve().parent / "intl_prices_usda.csv"

# --- unit conversion ---------------------------------------------------------
# CWT (US hundredweight) = 100 lb = 45.359237 kg
CWT_KG = 1.0 / 45.359237
# short ton = 2000 lb = 907.18474 kg
TON_KG = 1.0 / 907.18474
# Bushels are crop-specific weights (lb/bu in US ag stats).
BU_KG = {
    "CORN":     0.45359237 / 56.0,   # 56 lb/bu
    "SORGHUM":  0.45359237 / 56.0,   # 56 lb/bu
    "WHEAT":    0.45359237 / 60.0,   # 60 lb/bu
    "SOYBEANS": 0.45359237 / 60.0,   # 60 lb/bu
    "OATS":     0.45359237 / 32.0,   # 32 lb/bu (kept for future use)
}

# --- series definitions ------------------------------------------------------
# Each entry: a slug used downstream + the NASS short_desc string that pins
# the exact series (the easiest way to disambiguate vs "pct of parity"
# mirror rows that share the same commodity/class). The `to_kg` lambda
# converts the raw "Value" column into USD/Kg.

SERIES = [
    {
        "slug": "beans_dry_edible",
        "params": {
            "commodity_desc":    "BEANS",
            "class_desc":        "DRY EDIBLE, (EXCL CHICKPEAS)",
            "statisticcat_desc": "PRICE RECEIVED",
            "agg_level_desc":    "NATIONAL",
            "short_desc":        "BEANS, DRY EDIBLE, (EXCL CHICKPEAS) - PRICE RECEIVED, MEASURED IN $ / CWT",
        },
        "unit_in": "$/CWT",
        "to_kg":   lambda v: v * CWT_KG,
    },
    {
        "slug": "corn",
        # NASS rejects `class_desc=GRAIN` + this short_desc together; the
        # short_desc alone is enough to pin the dollar-priced series.
        "params": {
            "commodity_desc":    "CORN",
            "statisticcat_desc": "PRICE RECEIVED",
            "agg_level_desc":    "NATIONAL",
            "short_desc":        "CORN, GRAIN - PRICE RECEIVED, MEASURED IN $ / BU",
        },
        "unit_in": "$/BU",
        "to_kg":   lambda v: v * BU_KG["CORN"],
    },
    {
        "slug": "wheat",
        "params": {
            "commodity_desc":    "WHEAT",
            "statisticcat_desc": "PRICE RECEIVED",
            "agg_level_desc":    "NATIONAL",
            "short_desc":        "WHEAT - PRICE RECEIVED, MEASURED IN $ / BU",
        },
        "unit_in": "$/BU",
        "to_kg":   lambda v: v * BU_KG["WHEAT"],
    },
    {
        "slug": "rice",
        "params": {
            "commodity_desc":    "RICE",
            "statisticcat_desc": "PRICE RECEIVED",
            "agg_level_desc":    "NATIONAL",
            "short_desc":        "RICE - PRICE RECEIVED, MEASURED IN $ / CWT",
        },
        "unit_in": "$/CWT",
        "to_kg":   lambda v: v * CWT_KG,
    },
    {
        "slug": "sorghum",
        # As with corn, drop class_desc.
        "params": {
            "commodity_desc":    "SORGHUM",
            "statisticcat_desc": "PRICE RECEIVED",
            "agg_level_desc":    "NATIONAL",
            "short_desc":        "SORGHUM, GRAIN - PRICE RECEIVED, MEASURED IN $ / CWT",
        },
        "unit_in": "$/CWT",
        "to_kg":   lambda v: v * CWT_KG,
    },
    {
        "slug": "soybeans",
        "params": {
            "commodity_desc":    "SOYBEANS",
            "statisticcat_desc": "PRICE RECEIVED",
            "agg_level_desc":    "NATIONAL",
            "short_desc":        "SOYBEANS - PRICE RECEIVED, MEASURED IN $ / BU",
        },
        "unit_in": "$/BU",
        "to_kg":   lambda v: v * BU_KG["SOYBEANS"],
    },
    # NASS does not publish a monthly national sugarcane producer-price series
    # — the only available rows are MARKETING YEAR annual aggregates, which
    # don't intersect FEWS's monthly cadence. Dropped until a monthly source
    # (e.g. ICE futures via a different API) is wired in.
    {
        "slug": "milk",
        "params": {
            "commodity_desc":    "MILK",
            "statisticcat_desc": "PRICE RECEIVED",
            "agg_level_desc":    "NATIONAL",
            "short_desc":        "MILK - PRICE RECEIVED, MEASURED IN $ / CWT",
        },
        "unit_in": "$/CWT",
        "to_kg":   lambda v: v * CWT_KG,
    },
]


def _instructions_and_exit() -> None:
    print(
        "ERROR: NASS_API_KEY environment variable is not set.\n"
        "\n"
        "Sign up (instant, free) at:\n"
        "    https://quickstats.nass.usda.gov/api\n"
        "\n"
        "Then either export it in your shell profile:\n"
        "    export NASS_API_KEY='<your-key>'\n"
        "Or inline:\n"
        "    NASS_API_KEY='<your-key>' python3 prep_intl_prices.py\n",
        file=sys.stderr,
    )
    sys.exit(2)


def fetch_one(spec: dict) -> list:
    params = {"key": NASS_KEY, "format": "JSON", **spec["params"]}
    resp = requests.get(NASS_URL, params=params, timeout=60)
    if resp.status_code != 200:
        print(
            f"  WARN {spec['slug']}: HTTP {resp.status_code} {resp.text[:200]}",
            file=sys.stderr,
        )
        return []
    payload = resp.json()
    if "data" not in payload:
        print(
            f"  WARN {spec['slug']}: no 'data' field. Keys: {list(payload.keys())}",
            file=sys.stderr,
        )
        return []
    return payload["data"]


def tidy(rows: list, spec: dict) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    month_map = {
        "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
        "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
    }
    # NASS sometimes uses reference_period_desc, sometimes period_desc.
    col = "reference_period_desc" if "reference_period_desc" in df.columns else "period_desc"
    df = df[df[col].str.upper().isin(month_map.keys())].copy()
    if df.empty:
        return pd.DataFrame()
    df["year"] = df["year"].astype(int)
    df["m"] = df[col].str.upper().map(month_map)
    df["month"] = pd.to_datetime(
        df["year"].astype(str) + "-" + df["m"].astype(str) + "-01"
    )
    # Strip thousand separators, drop NASS's "(D)" disclosure-suppressed cells.
    df["raw"] = pd.to_numeric(df["Value"].str.replace(",", ""), errors="coerce")
    df = df.dropna(subset=["raw"]).copy()
    df["price_usd_kg"] = df["raw"].apply(spec["to_kg"])
    df["usda_slug"] = spec["slug"]
    df["source"] = f"USDA NASS {spec['params']['commodity_desc']} Price Received"
    df["unit"] = f"USD/Kg (converted from {spec['unit_in']})"
    out = df[["month", "usda_slug", "source", "unit", "price_usd_kg"]]
    return out.sort_values("month").reset_index(drop=True)


def main() -> None:
    if not NASS_KEY:
        _instructions_and_exit()

    frames = []
    for spec in SERIES:
        print(f"Fetching {spec['slug']} ...")
        rows = fetch_one(spec)
        tidied = tidy(rows, spec)
        if tidied.empty:
            print("  -> 0 rows (skipped)")
            continue
        print(
            f"  -> {len(tidied):4d} months, "
            f"{tidied['month'].min().date()} .. {tidied['month'].max().date()}, "
            f"price range {tidied['price_usd_kg'].min():.3f}..{tidied['price_usd_kg'].max():.3f} USD/Kg"
        )
        frames.append(tidied)

    if not frames:
        print("[ERROR] no series fetched; aborting", file=sys.stderr)
        sys.exit(1)

    out = pd.concat(frames, ignore_index=True)
    out["month"] = out["month"].dt.strftime("%Y-%m-%d")
    out.to_csv(OUT_PATH, index=False, float_format="%.6f")
    print(
        f"\nWrote {OUT_PATH} ({len(out):,} rows across "
        f"{out['usda_slug'].nunique()} series)"
    )


if __name__ == "__main__":
    main()
