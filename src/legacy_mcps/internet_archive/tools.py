"""MCP tool definitions for the Internet Archive.

Tools are thin: they resolve credentials, delegate to the client/API modules,
and convert every failure into a JSON payload. None of them raise, so a bad
call can never take down the MCP session.
"""

from __future__ import annotations

from typing import Any

from legacy_mcps.errors import (
    ERROR_MISSING_CREDENTIALS,
    MissingCredentialsError,
    error_payload,
    read_env,
    require_env,
    to_error_payload,
)
from legacy_mcps.internet_archive import api
from legacy_mcps.internet_archive.client import InternetArchiveS3Client

#: Environment variables read by the Internet Archive tools.
ACCESS_KEY_ENV = "IA_ACCESS_KEY"
SECRET_KEY_ENV = "IA_SECRET_KEY"

_CREDENTIAL_HINT = (
    "Create S3 keys at https://archive.org/account/s3.php and export them as "
    "IA_ACCESS_KEY and IA_SECRET_KEY."
)


def has_credentials() -> bool:
    """True when both IA keys are present and non-blank."""
    return len(read_env(ACCESS_KEY_ENV, SECRET_KEY_ENV)) == 2


def _client() -> InternetArchiveS3Client:
    keys = require_env(ACCESS_KEY_ENV, SECRET_KEY_ENV, hint=_CREDENTIAL_HINT)
    return InternetArchiveS3Client(keys[ACCESS_KEY_ENV], keys[SECRET_KEY_ENV])


def upload_file(
    identifier: str,
    file_path: str,
    remote_name: str | None = None,
    metadata: dict[str, Any] | None = None,
    queue_derive: bool = True,
    keep_old_version: bool = False,
) -> dict[str, Any]:
    """Upload a local file to an Internet Archive item.

    Creates the item when it does not exist yet. ``metadata`` entries become
    ``x-archive-meta-*`` headers; ``title``, ``mediatype`` and ``collection``
    are the ones IA actually uses to build the item's details page.

    Args:
        identifier: Target item identifier, e.g. ``my-upload-2026``.
        file_path: Path to the local file to upload.
        remote_name: Name to store the file under. Defaults to the local name.
        metadata: Item metadata, e.g. ``{"title": "My Item",
            "mediatype": "texts", "collection": ["opensource"]}``.
        queue_derive: Let IA run its derive pipeline (thumbnails, OCR, ...).
        keep_old_version: Keep the previous copy of a replaced file in
            ``history/files/``.

    Returns:
        A dict with ``ok=True`` and the item/key/checksums, or an error payload
        with ``ok=False`` and a machine-readable ``error`` code.
    """
    try:
        client = _client()
    except MissingCredentialsError as exc:
        return exc.to_dict()
    try:
        return client.upload_file(
            identifier,
            file_path,
            remote_name=remote_name,
            metadata=metadata,
            queue_derive=queue_derive,
            keep_old_version=keep_old_version,
        )
    except Exception as exc:  # noqa: BLE001 - tools must never raise
        return to_error_payload(exc)
    finally:
        client.close()


def get_item_metadata(
    identifier: str,
    include_files: bool = True,
    file_limit: int = 100,
) -> dict[str, Any]:
    """Fetch an Internet Archive item's metadata and file listing.

    Uses the public ``archive.org/metadata/<id>`` endpoint, so no credentials
    are required.

    Args:
        identifier: Item identifier to look up.
        include_files: Include the item's file list in the response.
        file_limit: Maximum number of file entries to return.

    Returns:
        A dict with ``ok=True`` plus ``item``, ``metadata``, ``file_count`` and
        ``files``, or an error payload with ``ok=False``.
    """
    try:
        document = api.fetch_metadata(identifier)
        files = document.get("files") or []
        payload: dict[str, Any] = {
            "ok": True,
            "identifier": document.get("metadata", {}).get("identifier", identifier),
            "metadata": document.get("metadata", {}),
            "server": document.get("server"),
            "directory": document.get("dir"),
            "file_count": len(files),
            "item_size_bytes": document.get("item_size"),
        }
        if include_files:
            payload["files"] = files[: max(file_limit, 0)]
        return payload
    except Exception as exc:  # noqa: BLE001 - tools must never raise
        return to_error_payload(exc)


def search_items(
    query: str,
    rows: int = 10,
    page: int = 1,
    fields: list[str] | None = None,
    sort: str | None = None,
) -> dict[str, Any]:
    """Search archive.org with a Lucene-style query.

    Uses the public ``advancedsearch.php`` endpoint, so no credentials are
    required.

    Args:
        query: Query such as ``collection:nasa AND mediatype:image``.
        rows: Number of results per page (1-1000).
        page: One-based page number.
        fields: Metadata fields to return. Defaults to a small useful set.
        sort: Sort expression, e.g. ``"downloads desc"``.

    Returns:
        A dict with ``ok=True``, ``num_found``, ``page`` and ``results``, or an
        error payload with ``ok=False``.
    """
    if rows > 1000:
        return error_payload(
            "invalid_input", "'rows' must be 1000 or fewer per request."
        )
    try:
        response = api.search_items(
            query, rows=rows, page=page, fields=fields, sort=sort
        )
        return {
            "ok": True,
            "query": query,
            "page": page,
            "num_found": response.get("numFound", 0),
            "start": response.get("start", 0),
            "results": response.get("docs", []),
        }
    except Exception as exc:  # noqa: BLE001 - tools must never raise
        return to_error_payload(exc)


def missing_credentials_message() -> dict[str, Any]:
    """Describe what is missing when the module is disabled."""
    return {
        "enabled": False,
        "reason": ERROR_MISSING_CREDENTIALS,
        "message": (
            f"Internet Archive write tools are disabled because {ACCESS_KEY_ENV} "
            f"and {SECRET_KEY_ENV} are not both set. {_CREDENTIAL_HINT} "
            "Read-only tools (get_item_metadata, search_items) remain available."
        ),
    }
