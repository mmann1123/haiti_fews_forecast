#!/usr/bin/env python3
"""
World Bank Real-Time Food Prices (RTFP) Haiti Downloader
========================================================
Pulls Haiti rows from the World Bank Microdata RTFP catalog
(https://microdata.worldbank.org/catalog/4494). The catalog is the
Haiti-specific RTFP dataset (idno HTI_2021_RTFP_v02_M), publishing one
monthly market-level CSV per release.

The WB pipeline re-publishes the full panel each release and back-fills prior
months when its modeled prices update, so an incremental "since-last-date" pull
would silently miss revisions. We instead use a 2-step approach:

    1. Discover the latest release filename (HTI_RTFP_mkt_2007_YYYY-MM-DD.zip).
    2. Download the ZIP for that release, extract the CSV, melt to long.

WB's microdata download requires accepting terms in a session cookie before
the /catalog/4494/download/<id> endpoints will serve the file, so we drive the
HTML "Accept" form once and reuse the session.

The released CSV is WIDE: one row per (market, price_date), with one column
per commodity (rice, beans_fao, maize_meal, ...). The plain commodity column
matches the OHLC "close" value (e.g. rice == c_rice). We melt to long form so
the database can store one row per (market, commodity, date).

Usage:
    from worldbank_rtfp_downloader import WorldBankRTFPClient
    client = WorldBankRTFPClient()
    title, url, version_date = client.get_latest_version()
    df = client.download_country(url)  # already Haiti-only
"""

from __future__ import annotations

import io
import os
import re
import sys
import zipfile
from datetime import date
from typing import Optional

import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

CATALOG_ID = "4494"
CATALOG_URL = f"https://microdata.worldbank.org/index.php/catalog/{CATALOG_ID}"
GET_MICRODATA_PAGE = f"{CATALOG_URL}/get_microdata"
ACCEPT_TERMS_URL = f"https://microdata.worldbank.org/catalog/{CATALOG_ID}/get_microdata"
DOWNLOAD_URL_TEMPLATE = (
    f"https://microdata.worldbank.org/catalog/{CATALOG_ID}/download/{{download_id}}"
)
# We want the per-market price file, not the "details" zip with QA tables.
DATAFILE_PREFIX = "HTI_RTFP_mkt_"

REQUEST_TIMEOUT = 300
MAX_RETRIES = 3

# Commodities the WB RTFP publishes for Haiti. The bare column == the OHLC
# close value; we also expose o/h/l prefixes when present.
WB_COMMODITIES = (
    "beans_fao",
    "maize_meal",
    "oil",
    "pasta",
    "rice",
    "rice_fao",
    "sorghum_fao",
    "sugar",
    "wheat_fao",
    "wheat_flour",
)


class WorldBankRTFPClient:
    """Client for the World Bank Microdata RTFP catalog (Haiti, catalog 4494)."""

    def __init__(self) -> None:
        self.session = requests.Session()
        retry = Retry(
            total=MAX_RETRIES,
            backoff_factor=2,
            status_forcelist=[429, 500, 502, 503, 504],
        )
        adapter = HTTPAdapter(max_retries=retry)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)
        # WB returns the terms-form to anonymous users; mimic a browser UA so
        # CloudFlare doesn't 403 us.
        self.session.headers.update(
            {"User-Agent": "haiti-fews-forecast/1.0 (+sync_worldbank)"}
        )
        self._terms_accepted = False

    # ------------------------------------------------------------------
    # Session: accept the WB terms-of-use form once per run
    # ------------------------------------------------------------------

    def _accept_terms(self) -> None:
        if self._terms_accepted:
            return
        # Prime the session cookies on the catalog page first.
        self.session.get(CATALOG_URL, timeout=REQUEST_TIMEOUT).raise_for_status()
        # Grab the terms form (carries the ncsrf token we have to echo back).
        resp = self.session.get(GET_MICRODATA_PAGE, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        m = re.search(r'name="ncsrf"\s+value="([^"]+)"', resp.text)
        if not m:
            raise RuntimeError(
                "WB RTFP: could not find ncsrf token on terms-of-use form"
            )
        ncsrf = m.group(1)
        post = self.session.post(
            ACCEPT_TERMS_URL,
            data={"ncsrf": ncsrf, "accept": "Accept"},
            timeout=REQUEST_TIMEOUT,
        )
        post.raise_for_status()
        # The POST response is the page that now lists download links; cache it
        # so get_latest_version() doesn't refetch.
        self._download_page_html = post.text
        self._terms_accepted = True

    # ------------------------------------------------------------------
    # Step 1: discover the latest release
    # ------------------------------------------------------------------

    def get_latest_version(self) -> tuple[str, str, str]:
        """
        Return (filename, download_url, version_date) for the most recent
        HTI_RTFP_mkt_*.zip release. version_date is YYYY-MM-DD parsed from
        the filename.
        """
        self._accept_terms()
        html = self._download_page_html

        # Each file row puts the filename in a >...< text span just before
        # one or more <a href=".../catalog/4494/download/<id>"> buttons.
        pairs = []
        for m in re.finditer(
            rf">({re.escape(DATAFILE_PREFIX)}[\w\-]+\.zip)<", html
        ):
            fn = m.group(1)
            nxt = re.search(
                r"/catalog/{}/download/(\d+)".format(CATALOG_ID),
                html[m.end() : m.end() + 5000],
            )
            if nxt:
                pairs.append((fn, nxt.group(1)))

        if not pairs:
            raise RuntimeError(
                f"WB RTFP: no {DATAFILE_PREFIX}*.zip download links found on "
                f"{GET_MICRODATA_PAGE}"
            )

        # Pick the lexicographically-largest filename — the dates are
        # zero-padded YYYY-MM-DD so this is the most recent release.
        pairs.sort()
        fn, download_id = pairs[-1]
        version_date = self._extract_date(fn)
        url = DOWNLOAD_URL_TEMPLATE.format(download_id=download_id)
        print(f"[INFO] WB RTFP latest: {fn} (version {version_date}) -> {url}")
        return fn, url, version_date

    @staticmethod
    def _extract_date(title: str) -> str:
        m = re.search(r"(\d{4}-\d{2}-\d{2})", title or "")
        if m:
            return m.group(1)
        return date.today().isoformat()

    # ------------------------------------------------------------------
    # Step 2: bulk download + melt to long form
    # ------------------------------------------------------------------

    def download_country(self, zip_url: str) -> pd.DataFrame:
        """
        Download the release ZIP, extract the first CSV, and return a long
        DataFrame with columns:
            iso3, country, adm1_name, adm2_name, mkt_name, lat, lon,
            price_date, currency, cm_name, price, o_price, h_price, l_price
        """
        self._accept_terms()
        print(f"[INFO] WB RTFP: downloading {zip_url}")
        resp = self.session.get(zip_url, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()

        with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
            csv_names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
            if not csv_names:
                raise FileNotFoundError(
                    f"WB RTFP: no CSV inside ZIP (members: {zf.namelist()})"
                )
            csv_name = csv_names[0]
            print(f"[INFO] WB RTFP: extracting {csv_name}")
            with zf.open(csv_name) as f:
                wide = pd.read_csv(f)

        print(
            f"[INFO] WB RTFP: wide release {len(wide):,} rows x {len(wide.columns)} cols"
        )
        return self._melt_long(wide)

    @staticmethod
    def _melt_long(wide: pd.DataFrame) -> pd.DataFrame:
        """
        Wide-to-long melt over WB_COMMODITIES. Carries the OHLC siblings
        (o_, h_, l_) onto each row so downstream code has the full picture.

        Drops rows where the close price is NA (a market-month with no
        observation for that commodity).
        """
        id_cols = [
            c
            for c in (
                "ISO3",
                "country",
                "adm1_name",
                "adm2_name",
                "mkt_name",
                "lat",
                "lon",
                "price_date",
                "currency",
            )
            if c in wide.columns
        ]

        frames = []
        for cm in WB_COMMODITIES:
            if cm not in wide.columns:
                continue
            cols = id_cols + [cm]
            for pref in ("o_", "h_", "l_"):
                col = f"{pref}{cm}"
                if col in wide.columns:
                    cols.append(col)
            sub = wide[cols].copy()
            sub = sub.rename(
                columns={
                    cm: "price",
                    f"o_{cm}": "o_price",
                    f"h_{cm}": "h_price",
                    f"l_{cm}": "l_price",
                }
            )
            sub["cm_name"] = cm
            frames.append(sub)

        if not frames:
            return pd.DataFrame()

        long = pd.concat(frames, ignore_index=True, sort=False)
        long = long.rename(columns={"ISO3": "iso3"})
        long["iso3"] = long["iso3"].astype(str).str.upper()
        long["price_date"] = pd.to_datetime(long["price_date"], errors="coerce").dt.date
        for c in ("price", "o_price", "h_price", "l_price", "lat", "lon"):
            if c in long.columns:
                long[c] = pd.to_numeric(long[c], errors="coerce")

        long = long.dropna(subset=["price", "price_date", "mkt_name", "cm_name"])
        long = long.reset_index(drop=True)
        n_markets = long["mkt_name"].nunique()
        n_cm = long["cm_name"].nunique()
        print(
            f"[INFO] WB RTFP: long form {len(long):,} rows, "
            f"{n_markets} markets, {n_cm} commodities"
        )
        return long

    def test_connection(self) -> bool:
        try:
            self.get_latest_version()
            print("[OK] WB RTFP catalog reachable")
            return True
        except Exception as exc:
            print(f"[ERROR] WB RTFP connection test failed: {exc}")
            return False


def main():
    client = WorldBankRTFPClient()
    title, url, version_date = client.get_latest_version()
    df = client.download_country(url)
    if df.empty:
        print("[WARN] empty WB RTFP frame")
        sys.exit(1)
    print(f"\nRelease: {title} ({version_date})")
    print(f"Rows: {len(df):,}")
    print(f"Markets: {sorted(df['mkt_name'].unique())}")
    print(f"Commodities: {sorted(df['cm_name'].unique())}")
    print(f"Date range: {df['price_date'].min()} .. {df['price_date'].max()}")


if __name__ == "__main__":
    main()
