"""Internet Archive support: S3 uploads plus the public metadata/search APIs."""

from __future__ import annotations

from legacy_mcps.internet_archive.client import InternetArchiveS3Client
from legacy_mcps.internet_archive.tools import (
    ACCESS_KEY_ENV,
    SECRET_KEY_ENV,
    get_item_metadata,
    has_credentials,
    search_items,
    upload_file,
)

__all__ = [
    "ACCESS_KEY_ENV",
    "SECRET_KEY_ENV",
    "InternetArchiveS3Client",
    "get_item_metadata",
    "has_credentials",
    "search_items",
    "upload_file",
]
