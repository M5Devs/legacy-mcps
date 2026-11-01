"""Internet Archive S3-like API client.

The Internet Archive exposes an S3-compatible gateway at
``https://s3.us.archive.org`` where every *item* is a bucket and every *file*
in the item is a key. Item-level metadata (title, mediatype, collection, ...)
travels in ``x-archive-meta-*`` request headers rather than in an XML payload,
so this module injects those headers through botocore's ``before-sign`` event
and lets botocore sign them like any other header.

Header layout, quoting, and the ``auto-make-bucket`` / ``queue-derive``
behaviour mirror the official ``internetarchive`` client library so items
created here are indistinguishable from items created by ``ia upload``.
"""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import quote

from legacy_mcps.errors import (
    AmbiguousWriteError,
    ConnectionToolError,
    FileNotFoundToolError,
    InvalidInputError,
    NotFoundError,
    RateLimitedError,
    RemoteRejectedError,
    TimeoutToolError,
)

#: Internet Archive's S3-like endpoint. The region is a placeholder: IA checks
#: that a SigV4 credential scope is present but not which region it names.
DEFAULT_ENDPOINT_URL = "https://s3.us.archive.org"
DEFAULT_REGION = "us-east-1"
DEFAULT_TIMEOUT = 60.0
DEFAULT_MAX_ATTEMPTS = 5

#: Characters IA permits in an item identifier.
_LEGAL_IDENTIFIER_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
)
#: Metadata keys IA owns; letting a caller set them would corrupt the item.
_RESERVED_METADATA_KEYS = frozenset({"identifier", "item_size", "item_last_updated"})

_CHECKSUM_CHUNK = 1024 * 1024

#: S3 error codes that mean "the service is busy, come back later".
_RATE_LIMIT_CODES = frozenset({"SlowDown", "RequestTimeout", "ServiceUnavailable"})
#: S3 error codes that mean "the object is not there".
_NOT_FOUND_CODES = frozenset({"NoSuchBucket", "NoSuchKey", "NoSuchUpload"})
#: S3 error codes that mean "the credentials or the request were refused".
_ACCESS_CODES = frozenset(
    {"AccessDenied", "InvalidAccessKeyId", "SignatureDoesNotMatch", "InvalidSecurity"}
)
#: S3 error codes caused by a malformed request from the caller.
_BAD_REQUEST_CODES = frozenset(
    {"InvalidBucketName", "InvalidArgument", "EntityTooLarge", "InvalidRequest"}
)


def _needs_uri_quoting(value: str) -> bool:
    """True when a metadata value cannot travel raw inside an HTTP header.

    Mirrors ``internetarchive.iarequest.needs_quote``: non-ASCII bytes and any
    whitespace must be percent-encoded inside a ``uri(...)`` wrapper.
    """
    try:
        value.encode("ascii")
    except UnicodeEncodeError:
        return True
    return re.search(r"\s", value) is not None


def encode_metadata_value(value: Any) -> str:
    """Render a metadata value for use in an ``x-archive-meta-*`` header."""
    if isinstance(value, bool):
        text = "true" if value else "false"
    else:
        text = str(value)
    if _needs_uri_quoting(text):
        return f"uri({quote(text, safe='')})"
    return text


def validate_identifier(identifier: str) -> str:
    """Validate an Internet Archive item identifier and return it normalised.

    IA rules: 3-80 characters, alphanumeric plus ``.``/``_``/``-``, and the
    first character must be alphanumeric. A leading ``@`` marks a user item and
    is stripped.
    """
    identifier = (identifier or "").strip()
    if identifier.startswith("@"):
        identifier = identifier[1:]
    if not identifier:
        raise InvalidInputError("An item 'identifier' is required.")
    if not 3 <= len(identifier) <= 80:
        raise InvalidInputError(
            "Item identifier must be between 3 and 80 characters.",
            detail=f"got {len(identifier)} characters: {identifier!r}",
        )
    bad = sorted({char for char in identifier if char not in _LEGAL_IDENTIFIER_CHARS})
    if bad:
        raise InvalidInputError(
            "Item identifier may only contain letters, digits, '.', '_' and '-'.",
            detail=f"offending character(s): {''.join(bad)!r}",
        )
    if not identifier[0].isalnum():
        raise InvalidInputError(
            "Item identifier must start with a letter or digit.",
            detail=f"starts with {identifier[0]!r}",
        )
    return identifier


def build_metadata_headers(metadata: dict[str, Any] | None) -> dict[str, str]:
    """Turn ``{key: value | [values]}`` into ``x-archive-metaNN-key`` headers.

    List values expand into zero-padded indices (``meta00``, ``meta01``, ...)
    so IA writes them as repeated XML elements, exactly as the official client
    does. Single values use the unindexed ``x-archive-meta-<key>`` form.
    """
    headers: dict[str, str] = {}
    for key, value in (metadata or {}).items():
        name = str(key).strip()
        if not name:
            raise InvalidInputError("Metadata keys must be non-empty strings.")
        if name.lower() in _RESERVED_METADATA_KEYS:
            raise InvalidInputError(
                f"Metadata key {name!r} is managed by the Internet Archive and "
                "cannot be set explicitly."
            )
        stem = name.replace("_", "--")
        values = list(value) if isinstance(value, (list, tuple)) else [value]
        for index, item in enumerate(values):
            if item is None or item == "":
                continue
            if len(values) == 1:
                header = f"x-archive-meta-{stem}".lower()
            else:
                header = f"x-archive-meta{index:02d}-{stem}".lower()
            headers[header] = encode_metadata_value(item)
    return headers


def checksums_for(path: Path) -> tuple[str, str, int]:
    """Return ``(md5, sha256, size_bytes)`` for a local file."""
    md5 = hashlib.md5()
    sha256 = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(_CHECKSUM_CHUNK):
            md5.update(chunk)
            sha256.update(chunk)
            size += len(chunk)
    return md5.hexdigest(), sha256.hexdigest(), size


def translate_boto_error(
    exc: BaseException, *, identifier: str = "", during_write: bool = False
) -> Exception:
    """Map a botocore exception onto this package's error hierarchy.

    ``during_write`` marks a failure that happened while an upload was in
    flight. A timeout or a dropped connection there does not tell us whether the
    bytes arrived, so the result is reported as an ambiguous write rather than a
    plain timeout/connection error, and is not advertised as safe to retry.
    """
    try:
        from botocore import exceptions as boto_errors
    except ImportError:  # pragma: no cover - boto3 is a declared dependency
        return ConnectionToolError(f"{type(exc).__name__}: {exc}")

    if isinstance(exc, boto_errors.ParamValidationError):
        return InvalidInputError(f"Invalid request parameters: {exc}", detail=str(exc))

    if isinstance(exc, (boto_errors.ReadTimeoutError, boto_errors.ConnectTimeoutError)):
        if during_write:
            return AmbiguousWriteError(
                "The upload to the Internet Archive timed out and its outcome is "
                "unknown: the file may already be stored. Check the item before "
                "retrying.",
                detail=str(exc),
            )
        return TimeoutToolError(
            "The Internet Archive S3 gateway did not respond in time.",
            detail=str(exc),
        )

    if isinstance(
        exc,
        (
            boto_errors.EndpointConnectionError,
            boto_errors.ConnectionClosedError,
            boto_errors.ConnectionError,
            boto_errors.SSLError,
            boto_errors.HTTPClientError,
        ),
    ):
        if during_write:
            return AmbiguousWriteError(
                "The connection to the Internet Archive dropped during the "
                "upload and its outcome is unknown: the file may already be "
                "stored. Check the item before retrying.",
                detail=str(exc),
            )
        return ConnectionToolError(
            "Could not reach the Internet Archive S3 gateway.", detail=str(exc)
        )

    if isinstance(exc, boto_errors.ClientError):
        response = getattr(exc, "response", None) or {}
        error = response.get("Error") or {}
        code = str(error.get("Code", "Unknown"))
        status = (response.get("ResponseMetadata") or {}).get("HTTPStatusCode")
        message = str(error.get("Message") or exc)
        target = f"item '{identifier}'" if identifier else "the Internet Archive"
        detail = f"HTTP {status}: {message}"

        if code in _RATE_LIMIT_CODES or status in (429, 503):
            return RateLimitedError(
                f"{target} is rate limiting uploads (S3 error {code}). "
                "Retry with exponential backoff.",
                detail=detail,
            )
        if status == 404 or code in _NOT_FOUND_CODES:
            return NotFoundError(
                f"{target} was not found (S3 error {code}).", detail=detail
            )
        if status in (401, 403) or code in _ACCESS_CODES:
            return RemoteRejectedError(
                "The Internet Archive rejected the credentials, or the request "
                f"was not permitted (S3 error {code}). Check IA_ACCESS_KEY and "
                "IA_SECRET_KEY, and that the account may write to this item.",
                detail=detail,
            )
        if status == 400 or code in _BAD_REQUEST_CODES:
            return InvalidInputError(
                f"The Internet Archive rejected the request (S3 error {code}): {message}",
                detail=detail,
            )
        return RemoteRejectedError(
            f"The Internet Archive returned an error (S3 error {code}): {message}",
            detail=detail,
        )

    if isinstance(exc, boto_errors.BotoCoreError):
        return ConnectionToolError(
            f"S3 client error: {exc}", detail=f"{type(exc).__name__}: {exc}"
        )

    return ConnectionToolError(f"{type(exc).__name__}: {exc}")


class InternetArchiveS3Client:
    """Credential-scoped wrapper around a boto3 S3 client.

    An upload that carries ``x-archive-meta-*`` headers needs a botocore event
    handler that is unique to that call. Rather than mutate a shared client,
    each such upload builds its own short-lived client, so concurrent uploads
    cannot sign each other's headers.
    """

    def __init__(
        self,
        access_key: str,
        secret_key: str,
        *,
        endpoint_url: str = DEFAULT_ENDPOINT_URL,
        region: str = DEFAULT_REGION,
        timeout: float = DEFAULT_TIMEOUT,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        client: Any | None = None,
    ) -> None:
        if not access_key or not secret_key:
            raise InvalidInputError(
                "Both an access key and a secret key are required to talk to "
                "the Internet Archive S3 gateway."
            )
        self._access_key = access_key
        self._secret_key = secret_key
        self.endpoint_url = endpoint_url
        self.region = region
        self.timeout = timeout
        self.max_attempts = max_attempts
        self._injected = client is not None
        self._client = client if client is not None else self._build_client()

    # -- client construction ----------------------------------------------

    def _build_client(self) -> Any:
        try:
            import boto3
            from botocore.config import Config
        except ImportError as exc:  # pragma: no cover - dependency is declared
            raise ConnectionToolError(
                "boto3 is required for Internet Archive uploads. "
                "Install it with 'pip install boto3'.",
                detail=str(exc),
            ) from exc

        config = Config(
            signature_version="s3v4",
            s3={"addressing_style": "path"},
            retries={"max_attempts": self.max_attempts, "mode": "standard"},
            connect_timeout=self.timeout,
            read_timeout=self.timeout,
            # The IA gateway implements a subset of S3 and does not expect the
            # CRC32 headers botocore now adds by default, so send none.
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
        )
        return boto3.client(
            "s3",
            endpoint_url=self.endpoint_url,
            aws_access_key_id=self._access_key,
            aws_secret_access_key=self._secret_key,
            region_name=self.region,
            config=config,
        )

    def _client_for_write(self) -> tuple[Any, bool]:
        """Return ``(client, must_close)`` for a single upload call."""
        if self._injected:
            return self._client, False
        return self._build_client(), True

    # -- public API --------------------------------------------------------

    def upload_file(
        self,
        identifier: str,
        file_path: str | os.PathLike[str],
        *,
        remote_name: str | None = None,
        metadata: dict[str, Any] | None = None,
        queue_derive: bool = True,
        keep_old_version: bool = False,
        size_hint: int | None = None,
    ) -> dict[str, Any]:
        """Upload one local file into an item, creating the item if needed."""
        identifier = validate_identifier(identifier)
        path = Path(file_path).expanduser()
        if not path.exists():
            raise FileNotFoundToolError(f"Local file not found: {path}")
        if not path.is_file():
            raise FileNotFoundToolError(f"Not a regular file: {path}")

        key = (remote_name or path.name).strip().lstrip("/")
        if not key:
            raise InvalidInputError("'remote_name' must not be blank.")

        md5, sha256, size = checksums_for(path)

        headers = build_metadata_headers(metadata)
        headers["x-archive-auto-make-bucket"] = "1"
        headers["x-archive-queue-derive"] = "1" if queue_derive else "0"
        if keep_old_version:
            headers["x-archive-keep-old-version"] = "1"
        headers["x-archive-size-hint"] = str(size if size_hint is None else size_hint)

        client, must_close = self._client_for_write()
        unique_id = f"legacy-mcps-meta-{id(headers)}"

        def _inject(request: Any, **_: Any) -> None:
            for header, value in headers.items():
                request.headers[header] = value

        events = getattr(getattr(client, "meta", None), "events", None)
        if events is None:  # pragma: no cover - defensive
            raise ConnectionToolError(
                "The S3 client exposes no botocore event emitter, so Internet "
                "Archive metadata headers cannot be injected."
            )

        events.register("before-sign.s3.PutObject", _inject, unique_id=unique_id)
        try:
            with path.open("rb") as body:
                response = client.put_object(Bucket=identifier, Key=key, Body=body)
        except Exception as exc:
            raise translate_boto_error(
                exc, identifier=identifier, during_write=True
            ) from exc
        finally:
            try:
                events.unregister("before-sign.s3.PutObject", unique_id=unique_id)
            except Exception:  # pragma: no cover - unregister is best effort
                pass
            if must_close:
                close = getattr(client, "close", None)
                if callable(close):
                    close()

        etag = ""
        if hasattr(response, "get"):
            etag = str(response.get("ETag", "")).strip('"')

        return {
            "ok": True,
            "item": identifier,
            "key": key,
            "size_bytes": size,
            "md5": md5,
            "sha256": sha256,
            "etag": etag,
            "details_url": f"https://archive.org/details/{identifier}",
            "download_url": f"https://archive.org/download/{identifier}/{quote(key, safe='/')}",
        }

    def item_exists(self, identifier: str) -> bool:
        """Return ``True`` when the item exists and is readable."""
        identifier = validate_identifier(identifier)
        try:
            self._client.list_objects_v2(Bucket=identifier, MaxKeys=1)
        except Exception as exc:
            translated = translate_boto_error(exc, identifier=identifier)
            if isinstance(translated, NotFoundError):
                return False
            raise translated from exc
        return True

    def close(self) -> None:
        close = getattr(self._client, "close", None)
        if callable(close):
            close()
