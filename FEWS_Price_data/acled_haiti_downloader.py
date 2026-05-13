#!/usr/bin/env python3
"""
ACLED Haiti Event Data Downloader
=================================
Pulls Haiti conflict events from the ACLED Data Export API.

Authentication: ACLED uses OAuth2 password-grant. Register a myACLED account
at https://acleddata.com/register/ and set:

    export ACLED_USERNAME="..."         # your myACLED username
    export ACLED_PASSWORD="..."         # your myACLED password

The client POSTs to https://acleddata.com/oauth/token to obtain a bearer
token (valid 24h), caches it in memory, and uses it for subsequent reads.

Endpoint docs: https://acleddata.com/api-documentation/getting-started

Usage:
    from acled_haiti_downloader import ACLEDClient
    client = ACLEDClient()
    df = client.get_events(start_date="2018-01-01", end_date="2025-12-31")
"""

from __future__ import annotations

import os
import sys
import time
from typing import Optional

import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

TOKEN_URL = "https://acleddata.com/oauth/token"
READ_URL = "https://acleddata.com/api/acled/read"
OAUTH_CLIENT_ID = "acled"
REQUEST_TIMEOUT = 120
MAX_RETRIES = 3
PAGE_SIZE = 5000  # ACLED default; documented as the max per request


class ACLEDClient:
    """Client for the ACLED Data Export API (OAuth2 password grant)."""

    def __init__(
        self,
        username: Optional[str] = None,
        password: Optional[str] = None,
    ):
        self.username = username or os.getenv("ACLED_USERNAME")
        self.password = password or os.getenv("ACLED_PASSWORD")
        if not self.username or not self.password:
            raise RuntimeError(
                "ACLED credentials missing. Set ACLED_USERNAME and ACLED_PASSWORD "
                "env vars (register a myACLED account at "
                "https://acleddata.com/register/)."
            )

        self.session = requests.Session()
        retry_strategy = Retry(
            total=MAX_RETRIES,
            backoff_factor=2,
            status_forcelist=[429, 500, 502, 503, 504],
        )
        adapter = HTTPAdapter(max_retries=retry_strategy)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

        self._access_token: Optional[str] = None
        self._token_expires_at: float = 0.0

    # ------------------------------------------------------------------
    # OAuth
    # ------------------------------------------------------------------

    def _fetch_token(self) -> str:
        """Exchange username/password for an access token. Caches in memory."""
        # Matches ACLED's official Python example verbatim -- no `scope` param.
        payload = {
            "username": self.username,
            "password": self.password,
            "grant_type": "password",
            "client_id": OAUTH_CLIENT_ID,
        }
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        resp = self.session.post(
            TOKEN_URL, headers=headers, data=payload, timeout=REQUEST_TIMEOUT
        )
        if resp.status_code != 200:
            raise RuntimeError(
                f"ACLED OAuth token request failed: {resp.status_code} {resp.text}"
            )
        body = resp.json()
        token = body.get("access_token")
        if not token:
            raise RuntimeError(f"ACLED OAuth response missing access_token: {body}")
        # Refresh ~5 min before expiry to avoid edge cases on long syncs.
        ttl = int(body.get("expires_in", 86400)) - 300
        self._access_token = token
        self._token_expires_at = time.time() + max(ttl, 60)
        return token

    def _auth_headers(self) -> dict:
        if not self._access_token or time.time() >= self._token_expires_at:
            self._fetch_token()
        # Content-Type included to match ACLED's official example.
        return {
            "Authorization": f"Bearer {self._access_token}",
            "Content-Type": "application/json",
        }

    # ------------------------------------------------------------------
    # Data fetch
    # ------------------------------------------------------------------

    def _fetch_page(self, params: dict) -> list:
        response = self.session.get(
            READ_URL,
            params=params,
            headers=self._auth_headers(),
            timeout=REQUEST_TIMEOUT,
        )
        # If our token went stale mid-sync, fetch a new one and retry once.
        if response.status_code == 401:
            self._access_token = None
            response = self.session.get(
                READ_URL,
                params=params,
                headers=self._auth_headers(),
                timeout=REQUEST_TIMEOUT,
            )
        if response.status_code >= 400:
            # Surface ACLED's actual error text (e.g., "Access denied",
            # "Consent must be accepted...", "Please fill in all the required
            # fields"). Without this, requests' default error string only
            # shows the URL.
            raise RuntimeError(
                f"ACLED {response.status_code} on {response.url}\n"
                f"Response body: {response.text[:1000]}"
            )
        payload = response.json()
        if not payload.get("success", True) and "error" in payload:
            raise RuntimeError(f"ACLED API error: {payload['error']}")
        return payload.get("data", [])

    def get_events(
        self,
        start_date: str,
        end_date: str,
        country: str = "Haiti",
    ) -> pd.DataFrame:
        """
        Fetch ACLED events for a country between two ISO dates (inclusive).

        Paginates server-side (PAGE_SIZE rows/request) until a short page is returned.
        Returns a DataFrame with one row per event. Empty DataFrame if no data.
        """
        # _format=json is required per ACLED's official examples; without it
        # the endpoint may default to a different response shape.
        base_params = {
            "_format": "json",
            "country": country,
            "event_date": f"{start_date}|{end_date}",
            "event_date_where": "BETWEEN",
            "limit": PAGE_SIZE,
        }

        all_rows: list = []
        page = 1
        while True:
            params = dict(base_params, page=page)
            print(f"[INFO] ACLED page {page} ({country} {start_date}..{end_date})...")
            rows = self._fetch_page(params)
            print(f"       got {len(rows)} rows")
            all_rows.extend(rows)
            if len(rows) < PAGE_SIZE:
                break
            page += 1
            time.sleep(0.5)  # gentle pacing

        if not all_rows:
            return pd.DataFrame()

        df = pd.DataFrame(all_rows)

        # Normalize the columns we care about; tolerate ACLED schema drift.
        keep = [
            "event_id_cnty",
            "event_date",
            "event_type",
            "sub_event_type",
            "admin1",
            "admin2",
            "latitude",
            "longitude",
            "fatalities",
        ]
        for col in keep:
            if col not in df.columns:
                df[col] = None
        df = df[keep].copy()

        df["event_date"] = pd.to_datetime(df["event_date"], errors="coerce").dt.date
        df["latitude"] = pd.to_numeric(df["latitude"], errors="coerce")
        df["longitude"] = pd.to_numeric(df["longitude"], errors="coerce")
        df["fatalities"] = pd.to_numeric(df["fatalities"], errors="coerce").fillna(0).astype(int)

        # Drop rows with no usable event id or date (rare but possible)
        df = df.dropna(subset=["event_id_cnty", "event_date"]).reset_index(drop=True)
        return df

    def test_connection(self) -> bool:
        """Tiny smoke test: fetch a bearer token and pull one event."""
        try:
            self._fetch_token()
            params = {"_format": "json", "country": "Haiti", "limit": 1}
            self._fetch_page(params)
            print("[OK] ACLED API connection successful")
            return True
        except Exception as exc:
            print(f"[ERROR] ACLED connection test failed: {exc}")
            return False


def main():
    """Smoke-test entry point: pull the last 90 days and print a summary."""
    from datetime import datetime, timedelta

    end = datetime.utcnow().date()
    start = end - timedelta(days=90)

    client = ACLEDClient()
    if not client.test_connection():
        sys.exit(1)

    df = client.get_events(start.isoformat(), end.isoformat())
    print(f"\nFetched {len(df)} events for Haiti {start}..{end}")
    if not df.empty:
        print("\nBy event_type:")
        print(df["event_type"].value_counts())
        print(f"\nTotal fatalities: {df['fatalities'].sum()}")


if __name__ == "__main__":
    main()
