"""SourceForge support: File Release System uploads over SFTP."""

from __future__ import annotations

from legacy_mcps.sourceforge.client import (
    DEFAULT_HOST,
    FRS_ROOT,
    SourceForgeFRSClient,
)
from legacy_mcps.sourceforge.tools import (
    HOST_ENV,
    PROJECT_NAME_ENV,
    SSH_KEY_PATH_ENV,
    USERNAME_ENV,
    has_credentials,
    list_release_files,
    upload_release_file,
)

__all__ = [
    "DEFAULT_HOST",
    "FRS_ROOT",
    "HOST_ENV",
    "PROJECT_NAME_ENV",
    "SSH_KEY_PATH_ENV",
    "USERNAME_ENV",
    "SourceForgeFRSClient",
    "has_credentials",
    "list_release_files",
    "upload_release_file",
]
