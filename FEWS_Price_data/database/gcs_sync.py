"""
GCS persistence helpers for the DuckDB file.

The Cloud Run container filesystem is read-only except for /tmp, and any
container instance is ephemeral. We therefore keep the authoritative copy of
fews_haiti.duckdb in a GCS bucket:

    - On startup, download_db_from_gcs() pulls the file to local disk.
    - After a successful FEWS NET sync, upload_db_to_gcs() pushes the
      updated file back so the next container instance sees the new data.

Authentication uses Application Default Credentials. On Cloud Run this is the
service account attached to the service; locally, set
GOOGLE_APPLICATION_CREDENTIALS to point at a service-account key.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional


def _get_client():
    from google.cloud import storage

    return storage.Client()


def download_db_from_gcs(
    bucket: str,
    blob_name: str,
    local_path: Path,
) -> bool:
    """
    Download the DuckDB file from GCS to local_path.

    Returns True if a file was downloaded, False if the blob does not exist
    (the caller should then initialize an empty DB via schema.sql).
    """
    client = _get_client()
    blob = client.bucket(bucket).blob(blob_name)
    if not blob.exists(client):
        return False
    local_path.parent.mkdir(parents=True, exist_ok=True)
    blob.download_to_filename(str(local_path))
    return True


def upload_db_to_gcs(
    local_path: Path,
    bucket: str,
    blob_name: str,
) -> None:
    """Upload the DuckDB file at local_path to gs://<bucket>/<blob_name>."""
    client = _get_client()
    blob = client.bucket(bucket).blob(blob_name)
    blob.upload_from_filename(str(local_path))
