"""MCP tool definitions for SourceForge.

Like the Internet Archive tools, these never raise: every failure becomes a
JSON payload with ``ok=False`` and a stable ``error`` code.
"""

from __future__ import annotations

from typing import Any

from legacy_mcps.errors import (
    ERROR_MISSING_CREDENTIALS,
    read_env,
    require_env,
    to_error_payload,
)
from legacy_mcps.sourceforge.client import (
    DEFAULT_HOST,
    SourceForgeFRSClient,
)

#: Environment variables read by the SourceForge tools.
USERNAME_ENV = "SF_USERNAME"
SSH_KEY_PATH_ENV = "SF_SSH_KEY_PATH"
PROJECT_NAME_ENV = "SF_PROJECT_NAME"
HOST_ENV = "SF_HOST"

_CREDENTIAL_HINT = (
    "Register your public key under SourceForge account settings and export "
    "SF_USERNAME, SF_SSH_KEY_PATH and SF_PROJECT_NAME."
)


def has_credentials() -> bool:
    """True when the username, key path and project name are all set."""
    required = (USERNAME_ENV, SSH_KEY_PATH_ENV, PROJECT_NAME_ENV)
    return len(read_env(*required)) == len(required)


def _client() -> SourceForgeFRSClient:
    env = require_env(
        USERNAME_ENV, SSH_KEY_PATH_ENV, PROJECT_NAME_ENV, hint=_CREDENTIAL_HINT
    )
    host = read_env(HOST_ENV).get(HOST_ENV, DEFAULT_HOST)
    return SourceForgeFRSClient(
        username=env[USERNAME_ENV],
        key_path=env[SSH_KEY_PATH_ENV],
        project=env[PROJECT_NAME_ENV],
        host=host,
    )


def upload_release_file(
    local_path: str,
    remote_dir: str,
    remote_name: str | None = None,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Upload a release file into a SourceForge project's FRS tree.

    Args:
        local_path: Path to the local file to publish.
        remote_dir: Directory below the project release root, e.g.
            ``"releases/1.2.0"``. Created if it does not exist.
        remote_name: Name to store the file under. Defaults to the local name.
        overwrite: Replace an existing file of the same name.

    Returns:
        A dict with ``ok=True`` and the remote path plus the public download
        URL, or an error payload with ``ok=False``.
    """
    try:
        client = _client()
    except Exception as exc:  # noqa: BLE001 - tools must never raise
        return to_error_payload(exc)
    try:
        return client.upload_file(
            local_path, remote_dir, remote_name=remote_name, overwrite=overwrite
        )
    except Exception as exc:  # noqa: BLE001 - tools must never raise
        return to_error_payload(exc)
    finally:
        client.close()


def list_release_files(remote_dir: str = "", limit: int = 200) -> dict[str, Any]:
    """List files and directories in a SourceForge release directory.

    Args:
        remote_dir: Directory below the project release root. Empty means the
            project's release root itself.
        limit: Maximum number of entries to return.

    Returns:
        A dict with ``ok=True``, ``count`` and ``entries``, or an error payload
        with ``ok=False``.
    """
    try:
        client = _client()
    except Exception as exc:  # noqa: BLE001 - tools must never raise
        return to_error_payload(exc)
    try:
        return client.list_files(remote_dir, limit=limit)
    except Exception as exc:  # noqa: BLE001 - tools must never raise
        return to_error_payload(exc)
    finally:
        client.close()


def missing_credentials_message() -> dict[str, Any]:
    """Describe what is missing when the module is disabled."""
    return {
        "enabled": False,
        "reason": ERROR_MISSING_CREDENTIALS,
        "message": (
            "SourceForge tools are disabled because SF_USERNAME, "
            "SF_SSH_KEY_PATH and SF_PROJECT_NAME are not all set. "
            f"{_CREDENTIAL_HINT}"
        ),
    }
