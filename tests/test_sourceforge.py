"""Tests for the SourceForge module.

The upload and listing tests run against the in-process SSH/SFTP server defined
in ``conftest.py``, so they exercise real SSH authentication, the SFTP
subsystem, and the stage-then-rename write path. Only the failure paths that a
live server cannot produce on demand (authentication rejected, host key
mismatch) use a substituted transport.
"""

from __future__ import annotations

from pathlib import Path

import paramiko
import pytest

from legacy_mcps.errors import (
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


def test_has_credentials_requires_all_three(monkeypatch):
    assert tools.has_credentials() is False
    monkeypatch.setenv(tools.USERNAME_ENV, USERNAME)
    monkeypatch.setenv(tools.SSH_KEY_PATH_ENV, "/tmp/key")
    assert tools.has_credentials() is False
    monkeypatch.setenv(tools.PROJECT_NAME_ENV, PROJECT)
    assert tools.has_credentials() is True
