"""SourceForge File Release System (FRS) client.

SourceForge publishes project releases over SSH at ``frs.sourceforge.net``.
Each project's release tree lives under ``/home/frs/project/<project>/`` and
whatever is written there is mirrored to the public download pages. There is no
API for uploads: it is plain SFTP.

This module wraps :mod:`paramiko` so that a caller can upload a release file and
list what is already published, without hand-rolling SSH key handling.

Notes on SourceForge behaviour that shaped this module:

* The remote release root is ``/home/frs/project/<project>`` and the *project*
  component is supplied by ``SF_PROJECT_NAME``; callers pass only the path
  below it (e.g. ``"releases/1.2.0"``).
* Only SFTP *write* operations are guaranteed to invalidate the download
  mirrors, so uploaded files are written to a temporary name and then renamed
  into place, which also makes a partially transferred file invisible.
* Filenames may not start with ``.`` or a space, may not end with a space, and
  may not contain ``&:%?/*$|{;^}<>"'`` or non-ASCII characters.
"""

from __future__ import annotations

import errno
import os
import posixpath
import socket
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

from legacy_mcps.errors import (
    AmbiguousWriteError,
    ConnectionToolError,
    FileNotFoundToolError,
    InvalidInputError,
    NotFoundError,
    RemoteRejectedError,
    TimeoutToolError,
)

DEFAULT_HOST = "frs.sourceforge.net"
DEFAULT_PORT = 22
#: SourceForge's FRS tree for every project.
FRS_ROOT = "/home/frs/project"
DEFAULT_TIMEOUT = 60.0

#: Characters SourceForge rejects in a release path component.
_FORBIDDEN_NAME_CHARS = set('&:%?/*$|{;^}<>"\'')
#: SourceForge caps a single FRS path at this many characters.
MAX_PATH_LENGTH = 255


def validate_remote_path(remote_path: str) -> str:
    """Validate a release path below the project root.

    Accepts relative paths such as ``releases/1.2.0``. Absolute paths and
    ``..`` segments are rejected so a tool call can never write outside the
    project's own release tree. Nothing is trimmed or rewritten: a name that
    begins or ends with whitespace is rejected rather than silently renamed.
    """
    if remote_path is None or remote_path == "":
        raise InvalidInputError(
            "A 'remote_path' below the project release root is required."
        )
    if remote_path.startswith("/"):
        raise InvalidInputError(
            "'remote_path' must be relative to the project release root.",
            detail=f"got {remote_path!r}",
        )
    normalised = posixpath.normpath(remote_path)
    if normalised.startswith("..") or "/../" in f"/{remote_path}/":
        raise InvalidInputError(
            "'remote_path' may not contain '..' segments.",
            detail=f"got {remote_path!r}",
        )
    for component in normalised.split("/"):
        if component in ("", "."):
            continue
        if component.startswith(".") or component.startswith(" "):
            raise InvalidInputError(
                "SourceForge release names may not start with '.' or a space.",
                detail=f"offending component: {component!r}",
            )
        if component.endswith(" "):
            raise InvalidInputError(
                "SourceForge release names may not end with a space.",
                detail=f"offending component: {component!r}",
            )
        try:
            component.encode("ascii")
        except UnicodeEncodeError:
            raise InvalidInputError(
                "SourceForge release names must be ASCII.",
                detail=f"offending component: {component!r}",
            ) from None
        bad = sorted({char for char in component if char in _FORBIDDEN_NAME_CHARS})
        if bad:
            raise InvalidInputError(
                "SourceForge release names may not contain "
                "& : % ? / * $ | { ; ^ } < > \" '",
                detail=f"offending character(s): {''.join(bad)!r}",
            )
    return normalised


@dataclass(frozen=True)
class RemoteFile:
    """One entry in a remote directory listing."""

    name: str
    size_bytes: int
    mode: int

    @property
    def is_directory(self) -> bool:
        return stat.S_ISDIR(self.mode)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "size_bytes": self.size_bytes,
            "is_directory": self.is_directory,
        }


class SourceForgeFRSClient:
    """Uploads and lists files in a SourceForge project's release tree.

    The SSH connection is opened lazily and reused until :meth:`close`, so a
    server process that makes several tool calls pays for the handshake once.
    """

    def __init__(
        self,
        username: str,
        key_path: str | os.PathLike[str],
        project: str,
        *,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        root: str = FRS_ROOT,
        password: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        allow_agent: bool = True,
        look_for_keys: bool = True,
        allow_unknown_host_key: bool = False,
        ssh_client: Any | None = None,
    ) -> None:
        if not username:
            raise InvalidInputError("A SourceForge username is required.")
        if not project:
            raise InvalidInputError("A SourceForge project name is required.")
        if ssh_client is None:
            key_file = Path(key_path).expanduser()
            if not key_file.exists():
                raise FileNotFoundToolError(
                    f"SSH private key not found: {key_file}"
                )
            if not key_file.is_file():
                raise FileNotFoundToolError(f"Not a regular file: {key_file}")
            self._key_path: Path | None = key_file
        else:
            self._key_path = None

        self.username = username
        self.project = project.strip()
        self.host = host
        self.port = port
        self.root = root.rstrip("/")
        self._password = password
        self.timeout = timeout
        self.allow_agent = allow_agent
        self.look_for_keys = look_for_keys
        self.allow_unknown_host_key = allow_unknown_host_key
        self._ssh_client = ssh_client
        self._sftp: Any | None = None
        self._owns_client = ssh_client is None

    # -- connection management --------------------------------------------

    @property
    def project_root(self) -> str:
        """Absolute path of the project's release root on the SFTP server."""
        return f"{self.root}/{self.project}"

    def _connect(self) -> Any:
        try:
            import paramiko
        except ImportError as exc:  # pragma: no cover - dependency is declared
            raise ConnectionToolError(
                "paramiko is required for SourceForge uploads. "
                "Install it with 'pip install paramiko'.",
                detail=str(exc),
            ) from exc

        client = self._ssh_client
        if client is None:
            client = paramiko.SSHClient()
            client.load_system_host_keys()
            policy = (
                paramiko.AutoAddPolicy()
                if self.allow_unknown_host_key
                else paramiko.RejectPolicy()
            )
            client.set_missing_host_key_policy(policy)
            self._ssh_client = client

        transport = getattr(client, "_transport", None)
        if transport is not None and transport.is_active():
            return client

        pkey = self._load_private_key(paramiko)
        try:
            kwargs: dict[str, Any] = {
                "hostname": self.host,
                "port": self.port,
                "username": self.username,
                "timeout": self.timeout,
                "banner_timeout": self.timeout,
                "auth_timeout": self.timeout,
                "allow_agent": self.allow_agent,
                "look_for_keys": self.look_for_keys,
            }
            if pkey is not None:
                # Passing the loaded key rather than key_filename avoids
                # paramiko's RSA/ECDSA/Ed25519 probing, which replaces the real
                # AuthenticationException with an unrelated key-parse error.
                kwargs["pkey"] = pkey
            if self._password and pkey is None:
                kwargs["password"] = self._password
            client.connect(**kwargs)
        except Exception as exc:
            raise self._translate_ssh_error(exc, action="connect") from exc
        return client

    def _load_private_key(self, paramiko: Any) -> Any:
        """Load the configured private key, or return ``None`` if unset."""
        if self._key_path is None:
            return None
        try:
            # Pass the passphrase positionally: paramiko renamed the keyword from
            # ``passphrase`` (3.2-4.x) to ``password`` (5.x).
            return paramiko.PKey.from_path(self._key_path, self._password)
        except Exception as exc:
            raise InvalidInputError(
                f"Could not read the SSH private key at {self._key_path}: {exc}",
                detail=f"{type(exc).__name__}: {exc}",
            ) from exc

    def _sftp_session(self) -> Any:
        if self._sftp is not None:
            return self._sftp
        client = self._connect()
        try:
            self._sftp = client.open_sftp()
        except Exception as exc:
            raise self._translate_ssh_error(exc, action="open an SFTP session") from exc
        return self._sftp

    def _reset(self) -> None:
        """Drop a broken SFTP session so the next call reconnects."""
        if self._sftp is not None:
            try:
                self._sftp.close()
            except Exception:
                pass
        self._sftp = None

    def _translate_ssh_error(
        self, exc: BaseException, *, action: str, during_write: bool = False
    ) -> Exception:
        try:
            import paramiko
        except ImportError:  # pragma: no cover - dependency is declared
            return ConnectionToolError(f"{type(exc).__name__}: {exc}")

        if isinstance(exc, paramiko.AuthenticationException):
            return RemoteRejectedError(
                "SourceForge rejected the SSH credentials. Check SF_USERNAME, "
                "SF_SSH_KEY_PATH and that the public key is registered in your "
                "SourceForge account settings.",
                detail=str(exc),
            )
        if isinstance(exc, paramiko.BadHostKeyException):
            return ConnectionToolError(
                f"The SSH host key for {self.host} did not match the known_hosts "
                "entry. Verify the host key before retrying.",
                detail=str(exc),
            )
        if isinstance(exc, paramiko.ssh_exception.SSHException) and (
            "not found in known_hosts" in str(exc)
        ):
            return ConnectionToolError(
                f"The SSH host key for {self.host} is not in known_hosts, so the "
                "connection was refused. Verify the host key first (for example "
                "with 'ssh-keyscan'), or set allow_unknown_host_key=True to trust "
                "it on first use.",
                detail=str(exc),
            )
        if isinstance(exc, paramiko.ssh_exception.NoValidConnectionsError):
            return ConnectionToolError(
                f"Could not open a TCP connection to {self.host}:{self.port}.",
                detail=str(exc),
            )
        if isinstance(exc, socket.timeout) or (
            isinstance(exc, paramiko.ssh_exception.SSHException)
            and "timed out" in str(exc).lower()
        ):
            if during_write:
                return AmbiguousWriteError(
                    "The upload timed out and its outcome is unknown: the file "
                    "may already be published. List the release directory before "
                    "retrying.",
                    detail=str(exc),
                )
            return TimeoutToolError(
                f"Timed out talking to {self.host}:{self.port}.", detail=str(exc)
            )
        if isinstance(exc, IOError):
            errno_value = getattr(exc, "errno", None)
            if errno_value == errno.ENOENT or "No such file" in str(exc):
                return NotFoundError(
                    f"Remote path not found on {self.host}.", detail=str(exc)
                )
            if errno_value in (errno.EACCES, errno.EPERM) or "Permission denied" in str(exc):
                return RemoteRejectedError(
                    "SourceForge denied the operation. The account must be a "
                    "project admin or have the 'release technician' permission.",
                    detail=str(exc),
                )
        if during_write and isinstance(
            exc, (EOFError, ConnectionError, paramiko.ssh_exception.SSHException)
        ):
            return AmbiguousWriteError(
                "The connection dropped during the upload and its outcome is "
                "unknown: the file may already be published. List the release "
                "directory before retrying.",
                detail=f"{type(exc).__name__}: {exc}",
            )
        return ConnectionToolError(
            f"Could not {action} on {self.host}: {exc}",
            detail=f"{type(exc).__name__}: {exc}",
        )

    # -- public API --------------------------------------------------------

    def ensure_remote_directory(self, remote_dir: str) -> str:
        """Create ``remote_dir`` (and parents) below the project root."""
        remote_dir = validate_remote_path(remote_dir)
        sftp = self._sftp_session()
        target = f"{self.project_root}/{remote_dir}"
        # Walk from the filesystem root: the project directory itself may not
        # exist yet, and mkdir cannot create a directory whose parent is absent.
        parts = [part for part in f"{self.root}/{self.project}/{remote_dir}".split("/") if part]
        current = "/"
        for part in parts:
            current = posixpath.join(current, part)
            try:
                sftp.stat(current)
            except IOError as exc:
                if getattr(exc, "errno", None) != errno.ENOENT:
                    raise self._translate_ssh_error(
                        exc, action="inspect a directory"
                    ) from exc
                try:
                    sftp.mkdir(current)
                except Exception as mkdir_exc:
                    # A concurrent creator wins the race; treat that as success.
                    try:
                        sftp.stat(current)
                    except Exception:
                        raise self._translate_ssh_error(
                            mkdir_exc, action="create a directory"
                        ) from mkdir_exc
        return target

    def upload_file(
        self,
        local_path: str | os.PathLike[str],
        remote_dir: str,
        *,
        remote_name: str | None = None,
        overwrite: bool = False,
        progress_callback: Any | None = None,
    ) -> dict[str, Any]:
        """Upload a local file into a release directory on SourceForge."""
        path = Path(local_path).expanduser()
        if not path.exists():
            raise FileNotFoundToolError(f"Local file not found: {path}")
        if not path.is_file():
            raise FileNotFoundToolError(f"Not a regular file: {path}")

        name = remote_name if remote_name is not None else path.name
        if not name:
            raise InvalidInputError("'remote_name' must not be blank.")
        name = validate_remote_path(name)
        if "/" in name or name == ".":
            raise InvalidInputError(
                "'remote_name' must be a single file name, not a path.",
                detail=f"got {name!r}",
            )
        directory = self.ensure_remote_directory(remote_dir)

        destination = posixpath.join(directory, name)
        staging = f"{destination}.part-{os.getpid()}"

        sftp = self._sftp_session()
        try:
            if not overwrite and self._exists(sftp, destination):
                raise InvalidInputError(
                    f"{destination} already exists on SourceForge.",
                    detail="pass overwrite=True to replace it",
                )
            try:
                sftp.put(str(path), staging, callback=progress_callback)
                if overwrite:
                    try:
                        # posix-rename@openssh.com replaces the target atomically,
                        # so the published file is never missing mid-swap.
                        sftp.posix_rename(staging, destination)
                    except IOError:
                        # Server lacks the extension: fall back to remove+rename.
                        if self._exists(sftp, destination):
                            sftp.remove(destination)
                        sftp.rename(staging, destination)
                else:
                    sftp.rename(staging, destination)
            except Exception as exc:
                raise self._translate_ssh_error(
                    exc, action="upload a file", during_write=True
                ) from exc
        finally:
            self._cleanup_staging(sftp, staging)

        size = path.stat().st_size
        relative = posixpath.relpath(destination, self.project_root)
        return {
            "ok": True,
            "project": self.project,
            "remote_path": destination,
            "filename": name,
            "size_bytes": size,
            "download_url": (
                f"https://sourceforge.net/projects/{quote(self.project)}/files/"
                f"{quote(relative)}/download"
            ),
            "note": (
                "SourceForge mirrors new releases within a few minutes; the "
                "download URL only serves the file after mirror propagation."
            ),
        }

    def list_files(self, remote_dir: str = "", *, limit: int = 200) -> dict[str, Any]:
        """List the entries in a release directory (the root by default)."""
        if limit < 1:
            raise InvalidInputError("'limit' must be at least 1.")
        if remote_dir:
            remote_dir = validate_remote_path(remote_dir)
            directory = f"{self.project_root}/{remote_dir}"
        else:
            directory = self.project_root

        sftp = self._sftp_session()
        try:
            entries = sftp.listdir_attr(directory)
        except IOError as exc:
            raise self._translate_ssh_error(exc, action="list a directory") from exc

        files = [
            RemoteFile(
                name=entry.filename,
                size_bytes=int(getattr(entry, "st_size", 0) or 0),
                mode=int(getattr(entry, "st_mode", 0) or 0),
            )
            for entry in entries
            if not entry.filename.startswith(".")
        ]
        files.sort(key=lambda item: (not item.is_directory, item.name.lower()))
        truncated = len(files) > limit
        return {
            "ok": True,
            "project": self.project,
            "remote_dir": directory,
            "count": len(files),
            "truncated": truncated,
            "entries": [entry.to_dict() for entry in files[:limit]],
        }

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _exists(sftp: Any, path: str) -> bool:
        try:
            sftp.stat(path)
        except IOError:
            return False
        return True

    @staticmethod
    def _cleanup_staging(sftp: Any, staging: str) -> None:
        try:
            sftp.remove(staging)
        except Exception:
            pass

    def close(self) -> None:
        self._reset()
        if self._owns_client and self._ssh_client is not None:
            try:
                self._ssh_client.close()
            except Exception:
                pass
            self._ssh_client = None
