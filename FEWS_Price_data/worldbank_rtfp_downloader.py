#!/usr/bin/env python3
"""
World Bank Real-Time Food Prices (RTFP) Haiti Downloader
========================================================
Pulls Haiti rows from the World Bank Microdata RTFP catalog
(https://microdata.worldbank.org/catalog/4494).

The WB pipeline re-publishes the full panel each release and back-fills prior
months when its modeled prices update, so an incremental "since-last-date" pull
would silently miss revisions. We instead use a 2-step approach:

    1. Hit the catalog's data_files endpoint to discover the latest release.
    2. Download the ZIP for that release, extract the CSV, and filter to Haiti.

Callers decide whether to skip the bulk download when the version date hasn't
moved (see `sync_worldbank.py`).

Usage:
    from worldbank_rtfp_downloader import WorldBankRTFPClient
    client = WorldBankRTFPClient()
    title, url, version_date = client.get_latest_version()
    df = client.download_country(url, iso3="HTI")
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

# Default endpoint for catalog 4494 (RTFP). Override with WB_RTFP_FILES_ENDPOINT
# if WB moves the catalog or you want to point at a mirror.
DEFAULT_FILES_ENDPOINT = (
    "https://microdata.worldbank.org/index.php/api/catalog/4494/data_files"
)
REQUEST_TIMEOUT = 300
MAX_RETRIES = 3


class WorldBankRTFPClient:
    """Client for the World Bank Microdata RTFP catalog."""

    def __init__(self, files_endpoint: Optional[str] = None):
        self.files_endpoint = (
            files_endpoint
            or os.getenv("WB_RTFP_FILES_ENDPOINT")
            or DEFAULT_FILES_ENDPOINT
        )

        self.session = requests.Session()
        retry = Retry(
            total=MAX_RETRIES,
            backoff_factor=2,
            status_forcelist=[429, 500, 502, 503, 504],
        )
        adapter = HTTPAdapter(max_retries=retry)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

    # ------------------------------------------------------------------
    # Step 1: discover the latest release
    # ------------------------------------------------------------------

    def _fetch_file_listing(self) -> dict:
        print(f"[INFO] WB RTFP: querying {self.files_endpoint}")
        resp = self.session.get(self.files_endpoint, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        return resp.json()

    @staticmethod
    def _iter_file_entries(listing: dict):
        """
        Normalize the listing into (title, download_url) pairs.

        WB has shipped the listing in two shapes over the years:
            - {"files": [{"title": ..., "links": {"download": ...}}, ...]}
            - {"files": {"title": [...], "links": {"download": [...]}}}
        Handle both.
        """
        files = listing.get("files", listing)
        if isinstance(files, list):
            for entry in files:
                title = entry.get("title", "") or entry.get("name", "")
                url = (entry.get("links") or {}).get("download") or entry.get(
                    "download_url", ""
                )
                if url:
                    yield title, url
            return

        if isinstance(files, dict):
            titles = files.get("title") or files.get("titles") or []
            links = files.get("links") or {}
            urls = links.get("download") or files.get("download") or []
            for title, url in zip(titles, urls):
                if url:
                    yield title, url

    def get_latest_version(self) -> tuple[str, str, str]:
        """
        Return (title, download_url, version_date) for the most recent data
        release. Skips entries whose title looks like documentation
        ("information", "readme", "codebook", "documentation").

        version_date is YYYY-MM-DD parsed out of the title when present,
        otherwise today's date as a fallback.
        """
        listing = self._fetch_file_listing()
        skip_keywords = ("information", "readme", "codebook", "documentation")

        for title, url in self._iter_file_entries(listing):
            if any(kw in (title or "").lower() for kw in skip_keywords):
                continue
            version_date = self._extract_date(title)
            print(f"[INFO] WB RTFP latest: {title!r} (version {version_date})")
            return title, url, version_date

        raise RuntimeError(
            "WB RTFP: no data file found in catalog listing "
            f"(endpoint={self.files_endpoint})"
        )

    @staticmethod
    def _extract_date(title: str) -> str:
        m = re.search(r"(\d{4}-\d{2}-\d{2})", title or "")
        if m:
            return m.group(1)
        m = re.search(r"(\d{4})[-_](\d{2})", title or "")
        if m:
            return f"{m.group(1)}-{m.group(2)}-01"
        return date.today().isoformat()

    # ------------------------------------------------------------------
    # Step 2: bulk download + filter
    # ------------------------------------------------------------------

    def download_country(self, zip_url: str, iso3: str = "HTI") -> pd.DataFrame:
        """Download the release ZIP, extract the first CSV, return Haiti rows."""
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
                df = pd.read_csv(f)

        print(f"[INFO] WB RTFP: full release {len(df):,} rows x {len(df.columns)} cols")
        return self._filter_country(df, iso3)

    @staticmethod
    def _filter_country(df: pd.DataFrame, iso3: str) -> pd.DataFrame:
        iso3 = iso3.upper()
        iso_col = next(
            (c for c in df.columns if c.lower() in ("iso3", "iso_3", "country_iso3")),
            None,
        )
        if not iso_col:
            raise KeyError(
                f"WB RTFP: no ISO3 column in release. Columns: {list(df.columns)}"
            )

        sub = df[df[iso_col].astype(str).str.upper() == iso3].copy()
        n_markets = sub["mkt_name"].nunique() if "mkt_name" in sub.columns else "?"
        print(f"[INFO] WB RTFP: {iso3} = {len(sub):,} rows, {n_markets} markets")
        return sub

    def test_connection(self) -> bool:
        try:
            self.get_latest_version()
            print("[OK] WB RTFP catalog reachable")
            return True
        except Exception as exc:
            print(f"[ERROR] WB RTFP connection test failed: {exc}")
            return False


def main():
    """Smoke test: list latest version, download Haiti rows, print summary."""
    client = WorldBankRTFPClient()
    title, url, version_date = client.get_latest_version()
    df = client.download_country(url, iso3="HTI")
    if df.empty:
        print("[WARN] no Haiti rows in latest release")
        sys.exit(1)
    print(f"\nLatest release: {title} ({version_date})")
    print(f"Haiti rows: {len(df):,}")
    if "mkt_name" in df.columns:
        print(f"Markets: {sorted(df['mkt_name'].unique())}")
    if "price_date" in df.columns:
        print(f"Date range: {df['price_date'].min()} .. {df['price_date'].max()}")


if __name__ == "__main__":
    main()
