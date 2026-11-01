"""Tests for the SourceForge module.

The upload and listing tests run against the in-process SSH/SFTP server defined
in ``conftest.py``, so they exercise real SSH authentication, the SFTP
subsystem, and the stage-then-rename write path. Only the failure paths that a
live server cannot produce on demand (authentication rejected, host key
mismatch) use a substituted transport.
"""

from __future__ import annotations

import errno
import threading
from pathlib import Path

import paramiko
import pytest

from conftest import InProcessSFTPServer
from legacy_mcps.errors import (
    AmbiguousWriteError,
    ConnectionToolError,
    FileNotFoundToolError,
    InvalidInputError,
    NotFoundError,
    RemoteRejectedError,
)
from legacy_mcps.sourceforge import tools
from legacy_mcps.sourceforge.client import (
    FRS_ROOT,
    SourceForgeFRSClient,
    validate_remote_path,
)

PROJECT = "myproj"
USERNAME = "testuser"


@pytest.fixture(autouse=True)
def _clear_sf_env(monkeypatch):
    for name in (tools.USERNAME_ENV, tools.SSH_KEY_PATH_ENV, tools.PROJECT_NAME_ENV, tools.HOST_ENV):
        monkeypatch.delenv(name, raising=False)


def _client(sftp_server, key_path: Path, **kwargs) -> SourceForgeFRSClient:
    """A client pointed at the in-process server, with the FRS root at /home/frs/project."""
    kwargs.setdefault("allow_unknown_host_key", True)
    return SourceForgeFRSClient(
        username=USERNAME,
        key_path=key_path,
        project=PROJECT,
        host=sftp_server.host,
        port=sftp_server.port,
        root=FRS_ROOT,
        timeout=10.0,
        allow_agent=False,
        look_for_keys=False,
        **kwargs,
    )


# --- path validation ------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    ["releases/1.2.0", "releases", "1.2.0/binary", "a/b/c", "release-notes.txt"],
)
def test_validate_remote_path_accepts_relative_paths(path):
    assert validate_remote_path(path) == path


@pytest.mark.parametrize(
    "path",
    [
        "/absolute/path",
        "../escape",
        "releases/../../etc",
        ".hidden",
        "releases/ bad",
        "releases/bad ",
        "bad:colon",
        "bad*star",
        "bad?question",
        "caf\u00e9",
        "",
    ],
)
def test_validate_remote_path_rejects_dangerous_paths(path):
    with pytest.raises(InvalidInputError):
        validate_remote_path(path)


def test_validate_remote_path_rejects_outer_whitespace():
    # Whitespace is significant in a release name, so it is rejected rather
    # than silently stripped.
    with pytest.raises(InvalidInputError):
        validate_remote_path("  releases/1.2.0  ")


# --- real upload and listing ----------------------------------------------


def test_upload_creates_directories_and_places_file(sftp_server, sftp_key_file, tmp_path):
    payload = b"sourceforge release payload"
    source = tmp_path / "release.tar.gz"
    source.write_bytes(payload)

    client = _client(sftp_server, sftp_key_file)
    try:
        result = client.upload_file(str(source), "releases/1.2.0")
    finally:
        client.close()

    assert result["ok"] is True
    assert result["remote_path"] == f"{FRS_ROOT}/{PROJECT}/releases/1.2.0/release.tar.gz"
    assert result["size_bytes"] == len(payload)
    assert result["download_url"] == (
        f"https://sourceforge.net/projects/{PROJECT}/files/releases/1.2.0/release.tar.gz/download"
    )

    landed = sftp_server.root / "home/frs/project" / PROJECT / "releases/1.2.0/release.tar.gz"
    assert landed.read_bytes() == payload


def test_upload_leaves_no_staging_file_behind(sftp_server, sftp_key_file, tmp_path):
    source = tmp_path / "artifact.bin"
    source.write_bytes(b"data")

    client = _client(sftp_server, sftp_key_file)
    try:
        client.upload_file(str(source), "releases")
    finally:
        client.close()

    directory = sftp_server.root / "home/frs/project" / PROJECT / "releases"
    assert [entry.name for entry in directory.iterdir()] == ["artifact.bin"]


def test_upload_honours_remote_name(sftp_server, sftp_key_file, tmp_path):
    source = tmp_path / "local-name.bin"
    source.write_bytes(b"data")

    client = _client(sftp_server, sftp_key_file)
    try:
        result = client.upload_file(str(source), "releases", remote_name="published.bin")
    finally:
        client.close()

    assert result["filename"] == "published.bin"
    assert (sftp_server.root / "home/frs/project" / PROJECT / "releases/published.bin").exists()


def test_upload_refuses_to_overwrite_without_flag(sftp_server, sftp_key_file, tmp_path):
    source = tmp_path / "artifact.bin"
    source.write_bytes(b"first")

    client = _client(sftp_server, sftp_key_file)
    try:
        client.upload_file(str(source), "releases")
        with pytest.raises(InvalidInputError):
            client.upload_file(str(source), "releases")
    finally:
        client.close()

    landed = sftp_server.root / "home/frs/project" / PROJECT / "releases/artifact.bin"
    assert landed.read_bytes() == b"first"


def test_upload_overwrites_when_asked(sftp_server, sftp_key_file, tmp_path):
    source = tmp_path / "artifact.bin"
    source.write_bytes(b"first")

    client = _client(sftp_server, sftp_key_file)
    try:
        client.upload_file(str(source), "releases")
        source.write_bytes(b"second version")
        result = client.upload_file(str(source), "releases", overwrite=True)
    finally:
        client.close()

    assert result["size_bytes"] == len(b"second version")
    landed = sftp_server.root / "home/frs/project" / PROJECT / "releases/artifact.bin"
    assert landed.read_bytes() == b"second version"


def test_upload_rejects_missing_local_file(sftp_server, sftp_key_file, tmp_path):
    client = _client(sftp_server, sftp_key_file)
    try:
        with pytest.raises(FileNotFoundToolError):
            client.upload_file(str(tmp_path / "nope.bin"), "releases")
    finally:
        client.close()


def test_upload_rejects_escaping_remote_directory(sftp_server, sftp_key_file, tmp_path):
    source = tmp_path / "artifact.bin"
    source.write_bytes(b"data")

    client = _client(sftp_server, sftp_key_file)
    try:
        with pytest.raises(InvalidInputError):
            client.upload_file(str(source), "../../etc")
    finally:
        client.close()


def test_upload_rejects_remote_name_with_separator(sftp_server, sftp_key_file, tmp_path):
    """A 'remote_name' must be a bare file name, not a path."""
    source = tmp_path / "artifact.bin"
    source.write_bytes(b"data")

    client = _client(sftp_server, sftp_key_file)
    try:
        with pytest.raises(InvalidInputError):
            client.upload_file(str(source), "releases", remote_name="sub/file.bin")
    finally:
        client.close()


def test_invalid_remote_name_does_not_create_directories(
    sftp_server, sftp_key_file, tmp_path
):
    """Validation runs before the directory is made, so a bad name leaves no trace."""
    source = tmp_path / "artifact.bin"
    source.write_bytes(b"data")

    client = _client(sftp_server, sftp_key_file)
    try:
        with pytest.raises(InvalidInputError):
            client.upload_file(str(source), "releases/9.9.9", remote_name="sub/file.bin")
    finally:
        client.close()

    assert not (sftp_server.root / "home/frs/project" / PROJECT / "releases/9.9.9").exists()


def test_overwrite_falls_back_when_posix_rename_unsupported(tmp_path):
    """Without the posix-rename extension the client still replaces the file."""
    server = InProcessSFTPServer(tmp_path, supports_posix_rename=False)
    try:
        key_path = tmp_path / "id_rsa"
        server.authorized_key.write_private_key_file(str(key_path))
        key_path.chmod(0o600)

        source = tmp_path / "artifact.bin"
        source.write_bytes(b"first")

        client = _client(server, key_path)
        try:
            client.upload_file(str(source), "releases")
            source.write_bytes(b"second version")
            result = client.upload_file(str(source), "releases", overwrite=True)
        finally:
            client.close()

        assert result["size_bytes"] == len(b"second version")
        landed = server.root / "home/frs/project" / PROJECT / "releases/artifact.bin"
        assert landed.read_bytes() == b"second version"
    finally:
        server.close()


def test_download_url_percent_encodes_path(sftp_server, sftp_key_file, tmp_path):
    """Spaces and '#' in a release name must not break the download URL."""
    source = tmp_path / "artifact.bin"
    source.write_bytes(b"data")

    client = _client(sftp_server, sftp_key_file)
    try:
        result = client.upload_file(str(source), "releases", remote_name="my file#1.bin")
    finally:
        client.close()

    assert result["download_url"] == (
        f"https://sourceforge.net/projects/{PROJECT}/files/"
        "releases/my%20file%231.bin/download"
    )


def test_concurrent_uploads_use_distinct_staging_paths(
    sftp_server, sftp_key_file, tmp_path, monkeypatch
):
    """Two uploads of the same name must not share one staging path.

    Each thread drives its own client (and so its own SSH session), which is how
    concurrent uploads actually happen. The ``put`` wrapper holds both threads at
    the staging write until they have both arrived, so the two staging paths are
    chosen while both uploads are in flight.
    """
    source = tmp_path / "artifact.bin"
    source.write_bytes(b"payload")

    clients = [_client(sftp_server, sftp_key_file) for _ in range(2)]
    staging_paths: list[str] = []
    lock = threading.Lock()
    real_put = paramiko.SFTPClient.put
    barrier = threading.Barrier(2)

    def record_put(self, localpath, remotepath, callback=None, confirm=True):
        with lock:
            staging_paths.append(remotepath)
        barrier.wait(timeout=30)
        return real_put(self, localpath, remotepath, callback=callback, confirm=confirm)

    # The destination is forced to look absent so neither writer stops at the
    # overwrite guard before the other has staged its bytes.
    monkeypatch.setattr(paramiko.SFTPClient, "put", record_put)
    monkeypatch.setattr(
        SourceForgeFRSClient, "_exists", staticmethod(lambda sftp, path: False)
    )

    def upload(client):
        client.upload_file(str(source), "releases")

    try:
        threads = [
            threading.Thread(target=upload, args=(client,)) for client in clients
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        assert all(not thread.is_alive() for thread in threads)
    finally:
        for client in clients:
            client.close()

    assert len(staging_paths) == 2
    assert staging_paths[0] != staging_paths[1]
    assert all(".part-" in path for path in staging_paths)


def test_plain_oserror_during_upload_is_ambiguous_write(
    sftp_server, sftp_key_file, tmp_path, monkeypatch
):
    """A bare OSError while writing is not safe to retry blindly."""
    source = tmp_path / "artifact.bin"
    source.write_bytes(b"payload")

    client = _client(sftp_server, sftp_key_file)

    def failing_put(self, localpath, remotepath, callback=None, confirm=True):
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(paramiko.SFTPClient, "put", failing_put)
    try:
        with pytest.raises(AmbiguousWriteError) as excinfo:
            client.upload_file(str(source), "releases")
        assert excinfo.value.to_dict()["retryable"] is False
    finally:
        client.close()


@pytest.mark.parametrize(
    "failure",
    [
        # An OSError that carries an errno is a real I/O failure, not a missing
        # extension: the broad "catch every OSError" version masked exactly this.
        OSError(errno.ECONNRESET, "Connection reset by peer"),
        paramiko.ssh_exception.SSHException("Socket is closed"),
        # paramiko also maps these statuses to an errno-less IOError, so an
        # errno check alone is not enough: the message must say "unsupported".
        OSError("Failure"),  # SFTP_FAILURE
        OSError("Connection lost"),  # SFTP_CONNECTION_LOST
        OSError("No connection"),  # SFTP_NO_CONNECTION
    ],
)
def test_failure_during_posix_rename_is_not_masked(
    sftp_server, sftp_key_file, tmp_path, monkeypatch, failure
):
    """A real failure during the atomic rename must surface, not fall back."""
    source = tmp_path / "artifact.bin"
    source.write_bytes(b"first")

    client = _client(sftp_server, sftp_key_file)
    try:
        client.upload_file(str(source), "releases")

        rename_calls: list[tuple[str, str]] = []

        def dropping_posix_rename(self, oldpath, newpath):
            raise failure

        def recording_rename(self, oldpath, newpath):
            rename_calls.append((oldpath, newpath))

        monkeypatch.setattr(paramiko.SFTPClient, "posix_rename", dropping_posix_rename)
        monkeypatch.setattr(paramiko.SFTPClient, "rename", recording_rename)

        source.write_bytes(b"second version")
        with pytest.raises(AmbiguousWriteError):
            client.upload_file(str(source), "releases", overwrite=True)

        # The generic failure was re-raised, so the non-atomic fallback never ran
        # and the previously published file was left untouched.
        assert rename_calls == []
    finally:
        client.close()

    landed = sftp_server.root / "home/frs/project" / PROJECT / "releases/artifact.bin"
    assert landed.read_bytes() == b"first"


def test_posix_rename_unsupported_detection():
    """Only a genuine 'unsupported' status counts as a missing extension."""
    from legacy_mcps.sourceforge.client import _posix_rename_unsupported

    # SFTP_OP_UNSUPPORTED -> IOError("Operation unsupported"), errno None.
    assert _posix_rename_unsupported(IOError("Operation unsupported")) is True
    assert _posix_rename_unsupported(OSError("Operation unsupported")) is True
    # An errno-carrying failure is a real error, not a missing extension.
    assert _posix_rename_unsupported(IOError(5, "Input/output error")) is False
    assert _posix_rename_unsupported(IOError(13, "Permission denied")) is False
    assert _posix_rename_unsupported(ConnectionError("reset")) is False
    assert _posix_rename_unsupported(RuntimeError("Operation unsupported")) is False
    # These paramiko statuses also arrive with errno None, so the message must
    # be checked too -- otherwise a dropped connection would trigger the
    # remove-then-rename fallback and could delete the published file.
    assert _posix_rename_unsupported(IOError("Failure")) is False  # SFTP_FAILURE
    assert _posix_rename_unsupported(IOError("No connection")) is False
    assert _posix_rename_unsupported(IOError("Connection lost")) is False


def test_list_files_reports_directories_and_files(sftp_server, sftp_key_file, tmp_path):
    source = tmp_path / "release.bin"
    source.write_bytes(b"0123456789")

    client = _client(sftp_server, sftp_key_file)
    try:
        client.upload_file(str(source), "releases/2.0.0")
        result = client.list_files("releases")
    finally:
        client.close()

    names = [entry["name"] for entry in result["entries"]]
    assert names == ["2.0.0"]
    assert result["entries"][0]["is_directory"] is True
    assert result["count"] == 1


def test_list_files_at_project_root(sftp_server, sftp_key_file, tmp_path):
    source = tmp_path / "top.bin"
    source.write_bytes(b"x")

    client = _client(sftp_server, sftp_key_file)
    try:
        client.upload_file(str(source), "releases")
        result = client.list_files()
    finally:
        client.close()

    assert result["remote_dir"] == f"{FRS_ROOT}/{PROJECT}"
    assert [entry["name"] for entry in result["entries"]] == ["releases"]


def test_list_files_hides_dotfiles(sftp_server, sftp_key_file):
    directory = sftp_server.root / "home/frs/project" / PROJECT
    directory.mkdir(parents=True)
    (directory / ".hidden").write_text("x")
    (directory / "visible").write_text("y")

    client = _client(sftp_server, sftp_key_file)
    try:
        result = client.list_files()
    finally:
        client.close()

    assert [entry["name"] for entry in result["entries"]] == ["visible"]


def test_list_files_reports_truncation(sftp_server, sftp_key_file):
    directory = sftp_server.root / "home/frs/project" / PROJECT
    directory.mkdir(parents=True)
    for index in range(5):
        (directory / f"file{index}.bin").write_text("x")

    client = _client(sftp_server, sftp_key_file)
    try:
        result = client.list_files(limit=2)
    finally:
        client.close()

    assert result["truncated"] is True
    assert result["count"] == 5
    assert len(result["entries"]) == 2


def test_list_files_missing_directory_raises_not_found(sftp_server, sftp_key_file):
    client = _client(sftp_server, sftp_key_file)
    try:
        with pytest.raises(NotFoundError):
            client.list_files("does/not/exist")
    finally:
        client.close()


def test_connection_is_reused_across_calls(sftp_server, sftp_key_file, tmp_path):
    source = tmp_path / "one.bin"
    source.write_bytes(b"x")

    client = _client(sftp_server, sftp_key_file)
    try:
        client.upload_file(str(source), "releases")
        first_session = client._sftp
        client.list_files("releases")
        assert client._sftp is first_session
    finally:
        client.close()


# --- credential and configuration errors ----------------------------------


def test_client_requires_existing_key_file(tmp_path):
    with pytest.raises(FileNotFoundToolError):
        SourceForgeFRSClient(
            username=USERNAME,
            key_path=tmp_path / "missing-key",
            project=PROJECT,
        )


def test_client_requires_username_and_project(tmp_path):
    key = tmp_path / "id_rsa"
    key.write_text("placeholder")
    with pytest.raises(InvalidInputError):
        SourceForgeFRSClient(username="", key_path=key, project=PROJECT)
    with pytest.raises(InvalidInputError):
        SourceForgeFRSClient(username=USERNAME, key_path=key, project="")


def test_rejected_key_maps_to_remote_rejected(sftp_server, tmp_path):
    """A key the server does not accept must surface as an auth failure."""
    wrong_key = tmp_path / "wrong"
    paramiko.RSAKey.generate(2048).write_private_key_file(str(wrong_key))
    wrong_key.chmod(0o600)

    client = SourceForgeFRSClient(
        username=USERNAME,
        key_path=wrong_key,
        project=PROJECT,
        host=sftp_server.host,
        port=sftp_server.port,
        timeout=5.0,
        allow_agent=False,
        look_for_keys=False,
        allow_unknown_host_key=True,
    )
    try:
        with pytest.raises(RemoteRejectedError) as excinfo:
            client.list_files()
        assert "SF_SSH_KEY_PATH" in excinfo.value.message
    finally:
        client.close()


def test_refused_connection_maps_to_connection_error(tmp_path):
    key = tmp_path / "id_rsa"
    paramiko.RSAKey.generate(2048).write_private_key_file(str(key))
    key.chmod(0o600)

    client = SourceForgeFRSClient(
        username=USERNAME,
        key_path=key,
        project=PROJECT,
        host="127.0.0.1",
        port=1,
        timeout=2.0,
        allow_agent=False,
        look_for_keys=False,
    )
    try:
        with pytest.raises(ConnectionToolError):
            client.list_files()
    finally:
        client.close()


def test_write_timeout_maps_to_ambiguous_write(tmp_path):
    """A timeout during a write is not safe to retry blindly."""
    key = tmp_path / "id_rsa"
    paramiko.RSAKey.generate(2048).write_private_key_file(str(key))
    key.chmod(0o600)

    client = SourceForgeFRSClient(username=USERNAME, key_path=key, project=PROJECT)
    try:
        translated = client._translate_ssh_error(
            paramiko.ssh_exception.SSHException("timed out"),
            action="upload a file",
            during_write=True,
        )
        assert isinstance(translated, AmbiguousWriteError)
        assert translated.to_dict()["retryable"] is False

        # The same error on a read path stays a plain, retryable timeout.
        translated = client._translate_ssh_error(
            paramiko.ssh_exception.SSHException("timed out"), action="list a directory"
        )
        assert translated.to_dict()["error"] == "timeout"
        assert translated.to_dict()["retryable"] is True
    finally:
        client.close()


# --- tool layer -----------------------------------------------------------


def test_tool_upload_returns_missing_credentials_payload():
    result = tools.upload_release_file("/tmp/whatever", "releases")
    assert result["ok"] is False
    assert result["error"] == "missing_credentials"
    assert tools.PROJECT_NAME_ENV in result["message"]


def test_tool_list_returns_missing_credentials_payload():
    result = tools.list_release_files()
    assert result["ok"] is False
    assert result["error"] == "missing_credentials"


def test_tool_upload_succeeds_with_environment(
    monkeypatch, sftp_server, sftp_key_file, tmp_path
):
    monkeypatch.setenv(tools.USERNAME_ENV, USERNAME)
    monkeypatch.setenv(tools.SSH_KEY_PATH_ENV, str(sftp_key_file))
    monkeypatch.setenv(tools.PROJECT_NAME_ENV, PROJECT)
    monkeypatch.setenv(tools.HOST_ENV, sftp_server.host)

    monkeypatch.setattr(
        tools,
        "_client",
        lambda: _client(sftp_server, sftp_key_file),
    )

    source = tmp_path / "release.bin"
    source.write_bytes(b"payload")

    result = tools.upload_release_file(str(source), "releases/3.0.0")

    assert result["ok"] is True
    assert result["remote_path"].endswith("releases/3.0.0/release.bin")


def test_tool_list_reports_failure_as_payload(monkeypatch, sftp_server, sftp_key_file):
    monkeypatch.setattr(tools, "_client", lambda: _client(sftp_server, sftp_key_file))

    result = tools.list_release_files("missing/dir")

    assert result["ok"] is False
    assert result["error"] == "not_found"


def test_tool_upload_reports_missing_local_file_as_payload(
    monkeypatch, sftp_server, sftp_key_file, tmp_path
):
    monkeypatch.setattr(tools, "_client", lambda: _client(sftp_server, sftp_key_file))

    result = tools.upload_release_file(str(tmp_path / "gone.bin"), "releases")

    assert result["ok"] is False
    assert result["error"] == "file_not_found"


def test_tool_upload_reports_missing_key_file_as_payload(monkeypatch, tmp_path):
    """A typo in SF_SSH_KEY_PATH must surface as file_not_found, not raise."""
    monkeypatch.setenv(tools.USERNAME_ENV, USERNAME)
    monkeypatch.setenv(tools.SSH_KEY_PATH_ENV, str(tmp_path / "no-such-key"))
    monkeypatch.setenv(tools.PROJECT_NAME_ENV, PROJECT)

    source = tmp_path / "artifact.bin"
    source.write_bytes(b"data")

    result = tools.upload_release_file(str(source), "releases")

    assert result["ok"] is False
    assert result["error"] == "file_not_found"
    assert result["retryable"] is False


def test_tool_list_reports_missing_key_file_as_payload(monkeypatch, tmp_path):
    monkeypatch.setenv(tools.USERNAME_ENV, USERNAME)
    monkeypatch.setenv(tools.SSH_KEY_PATH_ENV, str(tmp_path / "no-such-key"))
    monkeypatch.setenv(tools.PROJECT_NAME_ENV, PROJECT)

    result = tools.list_release_files()

    assert result["ok"] is False
    assert result["error"] == "file_not_found"


def test_unknown_host_key_is_rejected_by_default(monkeypatch, sftp_server, tmp_path):
    """Without an opt-in, a host whose key is not in known_hosts is refused."""
    key = tmp_path / "id_rsa"
    sftp_server.authorized_key.write_private_key_file(str(key))
    key.chmod(0o600)

    client = SourceForgeFRSClient(
        username=USERNAME,
        key_path=key,
        project=PROJECT,
        host=sftp_server.host,
        port=sftp_server.port,
        timeout=5.0,
        allow_agent=False,
        look_for_keys=False,
    )
    try:
        with pytest.raises(ConnectionToolError) as excinfo:
            client.list_files()
        assert "host key" in excinfo.value.message.lower()
    finally:
        client.close()


def test_has_credentials_requires_all_three(monkeypatch):
    assert tools.has_credentials() is False
    monkeypatch.setenv(tools.USERNAME_ENV, USERNAME)
    monkeypatch.setenv(tools.SSH_KEY_PATH_ENV, "/tmp/key")
    assert tools.has_credentials() is False
    monkeypatch.setenv(tools.PROJECT_NAME_ENV, PROJECT)
    assert tools.has_credentials() is True
