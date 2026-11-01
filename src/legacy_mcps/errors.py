"""Shared error types and credential validation helpers.

Every tool in this package returns a JSON-serialisable ``dict`` instead of
raising, so that a failing call never tears down the MCP session. The
``LegacyMCPError`` hierarchy exists so that the backend modules can map their
own failure modes onto a small, stable set of ``error`` codes an agent can
branch on.
"""

from __future__ import annotations

import os
from collections.abc import Iterable

#: Required environment variables are absent or blank.
ERROR_MISSING_CREDENTIALS = "missing_credentials"
#: A path, identifier or argument supplied by the caller is not usable.
ERROR_INVALID_INPUT = "invalid_input"
#: The local file to upload does not exist or cannot be read.
ERROR_FILE_NOT_FOUND = "file_not_found"
#: The remote service refused the request (auth failure, permission, quota...).
ERROR_REMOTE_REJECTED = "remote_rejected"
#: The remote item/collection/path does not exist.
ERROR_NOT_FOUND = "not_found"
#: The request was rate limited or the server asked us to slow down.
ERROR_RATE_LIMITED = "rate_limited"
#: The network call timed out.
ERROR_TIMEOUT = "timeout"
#: DNS/TCP/TLS level failure, or the connection dropped mid-transfer.
ERROR_CONNECTION = "connection_error"
#: A write failed in a way that leaves the remote state unknown, so it is unsafe
#: to blindly retry: the transfer may have completed before the failure.
ERROR_AMBIGUOUS_WRITE = "ambiguous_write"
#: Anything we could not classify. ``detail`` carries the raw message.
ERROR_UNEXPECTED = "unexpected_error"

RETRYABLE_ERRORS = frozenset({ERROR_RATE_LIMITED, ERROR_TIMEOUT, ERROR_CONNECTION})


class LegacyMCPError(Exception):
    """Base class for all errors raised inside this package."""

    code = ERROR_UNEXPECTED

    def __init__(self, message: str, *, detail: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail

    def to_dict(self) -> dict[str, object]:
        """Render the error as the payload returned to the MCP client."""
        payload: dict[str, object] = {
            "ok": False,
            "error": self.code,
            "message": self.message,
            "retryable": self.code in RETRYABLE_ERRORS,
        }
        if self.detail:
            payload["detail"] = self.detail
        return payload


class MissingCredentialsError(LegacyMCPError):
    """Raised when one or more required environment variables are unset."""

    code = ERROR_MISSING_CREDENTIALS

    def __init__(self, variables: Iterable[str], *, hint: str | None = None) -> None:
        names = list(variables)
        listed = ", ".join(names)
        message = f"Missing required environment variable(s): {listed}."
        if hint:
            message = f"{message} {hint}"
        super().__init__(message, detail=f"unset or blank: {listed}")
        self.variables = names


class InvalidInputError(LegacyMCPError):
    code = ERROR_INVALID_INPUT


class FileNotFoundToolError(LegacyMCPError):
    code = ERROR_FILE_NOT_FOUND


class RemoteRejectedError(LegacyMCPError):
    code = ERROR_REMOTE_REJECTED


class NotFoundError(LegacyMCPError):
    code = ERROR_NOT_FOUND


class RateLimitedError(LegacyMCPError):
    code = ERROR_RATE_LIMITED


class TimeoutToolError(LegacyMCPError):
    code = ERROR_TIMEOUT


class ConnectionToolError(LegacyMCPError):
    code = ERROR_CONNECTION


class AmbiguousWriteError(LegacyMCPError):
    """A write failed after the remote state may already have changed.

    A timeout or a dropped connection during an upload does not tell us whether
    the bytes arrived. Retrying is not obviously safe: the first attempt may
    have completed. Callers should reconcile the remote state before retrying.
    """

    code = ERROR_AMBIGUOUS_WRITE


# --- credential helpers ----------------------------------------------------


def read_env(*names: str) -> dict[str, str]:
    """Return the named environment variables, ignoring unset/blank ones."""
    found: dict[str, str] = {}
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            found[name] = value
    return found


def require_env(*names: str, hint: str | None = None) -> dict[str, str]:
    """Return the named environment variables, or raise if any are missing."""
    found = read_env(*names)
    missing = [name for name in names if name not in found]
    if missing:
        raise MissingCredentialsError(missing, hint=hint)
    return found


def error_payload(
    code: str, message: str, *, detail: str | None = None
) -> dict[str, object]:
    """Build an error payload without constructing an exception."""
    payload: dict[str, object] = {
        "ok": False,
        "error": code,
        "message": message,
        "retryable": code in RETRYABLE_ERRORS,
    }
    if detail:
        payload["detail"] = detail
    return payload


def to_error_payload(exc: BaseException) -> dict[str, object]:
    """Convert any exception into the stable error payload shape."""
    if isinstance(exc, LegacyMCPError):
        return exc.to_dict()
    return error_payload(
        ERROR_UNEXPECTED,
        f"{type(exc).__name__}: {exc}",
        detail=repr(exc),
    )
