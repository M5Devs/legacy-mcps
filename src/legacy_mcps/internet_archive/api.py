"""HTTP helpers for the Internet Archive's non-S3 (public) endpoints.

Item metadata and search are served by ``archive.org`` itself rather than the
S3 gateway, so they do not need credentials. The S3 gateway is used only for
writes and is handled in :mod:`legacy_mcps.internet_archive.client`.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from legacy_mcps.errors import (
    ConnectionToolError,
    InvalidInputError,
    NotFoundError,
    RateLimitedError,
    TimeoutToolError,
)

#: Public, unauthenticated JSON API used for item metadata.
METADATA_URL = "https://archive.org/metadata/{identifier}"
#: Public, unauthenticated Solr-backed search API.
SEARCH_URL = "https://archive.org/advancedsearch.php"

DEFAULT_TIMEOUT = 30.0
DEFAULT_USER_AGENT = "legacy-mcps/0.1 (+https://github.com/M5Devs/legacy-mcps)"


class _UrllibTransport:
    """Minimal URL opener seam so tests can substitute a fake transport."""

    def open_json(
        self, url: str, *, params: dict[str, Any] | None = None, timeout: float
    ) -> Any:
        if params:
            url = f"{url}?{urllib.parse.urlencode(params, doseq=True)}"
        request = urllib.request.Request(url, headers={"User-Agent": DEFAULT_USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            raise _http_error(url, exc) from exc
        except TimeoutError as exc:
            raise TimeoutToolError(
                f"Timed out after {timeout:g}s waiting for {url}.", detail=str(exc)
            ) from exc
        except urllib.error.URLError as exc:
            reason = exc.reason
            if isinstance(reason, TimeoutError):
                raise TimeoutToolError(
                    f"Timed out after {timeout:g}s waiting for {url}.", detail=str(reason)
                ) from exc
            raise ConnectionToolError(
                f"Could not reach {url}.", detail=str(reason)
            ) from exc
        except OSError as exc:
            raise ConnectionToolError(f"Could not reach {url}.", detail=str(exc)) from exc

        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ConnectionToolError(
                f"{url} returned a non-JSON response.",
                detail=raw[:200],
            ) from exc


def _http_error(url: str, exc: urllib.error.HTTPError) -> Exception:
    if exc.code == 404:
        return NotFoundError(f"No resource at {url}.", detail="HTTP 404")
    if exc.code in (429, 503):
        retry_after = exc.headers.get("Retry-After") if exc.headers else None
        detail = f"HTTP {exc.code}"
        if retry_after:
            detail = f"{detail}; Retry-After: {retry_after}"
        return RateLimitedError(
            f"The Internet Archive is rate limiting requests to {url}.", detail=detail
        )
    return ConnectionToolError(
        f"{url} returned HTTP {exc.code}.", detail=exc.reason or str(exc)
    )


def fetch_metadata(
    identifier: str,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    transport: _UrllibTransport | None = None,
) -> dict[str, Any]:
    """Fetch the raw metadata document for an item."""
    identifier = (identifier or "").strip()
    if not identifier:
        raise InvalidInputError("An item 'identifier' is required.")

    transport = transport or _UrllibTransport()
    payload = transport.open_json(
        METADATA_URL.format(identifier=urllib.parse.quote(identifier, safe="")),
        timeout=timeout,
    )
    if not isinstance(payload, dict):
        raise ConnectionToolError(
            f"Unexpected metadata response type: {type(payload).__name__}."
        )
    # archive.org answers `{}` (HTTP 200) for identifiers that do not exist.
    if not payload:
        raise NotFoundError(
            f"Item '{identifier}' was not found on archive.org.",
            detail="empty metadata document",
        )
    files = payload.get("files")
    if not isinstance(payload.get("metadata", {}), dict) or not (
        files is None or isinstance(files, list)
    ):
        raise ConnectionToolError(
            "Metadata response has unexpected 'metadata' or 'files' types.",
            detail=json.dumps(payload)[:200],
        )
    return payload


def search_items(
    query: str,
    *,
    rows: int = 10,
    page: int = 1,
    fields: list[str] | None = None,
    sort: str | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    transport: _UrllibTransport | None = None,
) -> dict[str, Any]:
    """Run an advancedsearch query and return the ``response`` block."""
    query = (query or "").strip()
    if not query:
        raise InvalidInputError("A search 'query' is required.")
    if rows < 1:
        raise InvalidInputError("'rows' must be at least 1.")
    if page < 1:
        raise InvalidInputError("'page' must be at least 1.")

    selected = fields or ["identifier", "title", "mediatype", "year", "creator"]
    params: dict[str, Any] = {
        "q": query,
        "fl[]": selected,
        "rows": rows,
        "page": page,
        "output": "json",
    }
    if sort:
        params["sort[]"] = sort

    transport = transport or _UrllibTransport()
    payload = transport.open_json(SEARCH_URL, params=params, timeout=timeout)
    if not isinstance(payload, dict) or not isinstance(payload.get("response"), dict):
        raise ConnectionToolError(
            "Search API returned an unexpected payload.",
            detail=json.dumps(payload)[:200] if payload is not None else "null",
        )
    return payload["response"]
