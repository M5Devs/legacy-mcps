"""Tests for the Internet Archive module.

Upload tests drive a real boto3 client against a loopback HTTP server that
stands in for ``s3.us.archive.org``, then re-derive the SigV4 signature from the
recorded request. That checks the property the real gateway enforces: the
injected ``x-archive-meta-*`` headers are part of the signed header set.
"""

from __future__ import annotations

import pytest

from conftest import verify_sigv4
from legacy_mcps.internet_archive import api, tools
from legacy_mcps.internet_archive.client import (
    InternetArchiveS3Client,
    build_metadata_headers,
    encode_metadata_value,
    translate_boto_error,
    validate_identifier,
)
from legacy_mcps.errors import (
    InvalidInputError,
    MissingCredentialsError,
    NotFoundError,
    RateLimitedError,
    RemoteRejectedError,
    TimeoutToolError,
)

ACCESS_KEY = "testaccesskey"
SECRET_KEY = "testsecretkey"


@pytest.fixture(autouse=True)
def _clear_ia_env(monkeypatch):
    """Make the ambient environment irrelevant to these tests."""
    monkeypatch.delenv(tools.ACCESS_KEY_ENV, raising=False)
    monkeypatch.delenv(tools.SECRET_KEY_ENV, raising=False)


def _client(stub) -> InternetArchiveS3Client:
    return InternetArchiveS3Client(
        ACCESS_KEY, SECRET_KEY, endpoint_url=stub.endpoint_url
    )


# --- identifier and metadata validation -----------------------------------


@pytest.mark.parametrize(
    "identifier",
    ["abc", "my-item-2026", "Some_Item.1", "a" * 80],
)
def test_validate_identifier_accepts_legal_identifiers(identifier):
    assert validate_identifier(identifier) == identifier


def test_validate_identifier_strips_user_prefix():
    assert validate_identifier("@my-item") == "my-item"


@pytest.mark.parametrize(
    "identifier",
    ["ab", "a" * 81, "has space", "has/slash", "-leading-dash", "_leading_underscore", ""],
)
def test_validate_identifier_rejects_illegal_identifiers(identifier):
    with pytest.raises(InvalidInputError):
        validate_identifier(identifier)


def test_build_metadata_headers_single_and_list_values():
    headers = build_metadata_headers(
        {"title": "My Item", "collection": ["opensource", "nasa"], "mediatype": "texts"}
    )
    assert headers["x-archive-meta-title"] == "uri(My%20Item)"
    assert headers["x-archive-meta-mediatype"] == "texts"
    assert headers["x-archive-meta00-collection"] == "opensource"
    assert headers["x-archive-meta01-collection"] == "nasa"


def test_build_metadata_headers_keeps_ascii_values_unquoted():
    assert build_metadata_headers({"mediatype": "texts"}) == {
        "x-archive-meta-mediatype": "texts"
    }


def test_build_metadata_headers_rejects_reserved_keys():
    with pytest.raises(InvalidInputError):
        build_metadata_headers({"identifier": "spoofed"})


def test_encode_metadata_value_quotes_non_ascii():
    assert encode_metadata_value("caf\u00e9") == "uri(caf%C3%A9)"


# --- upload against the stub gateway --------------------------------------


def test_upload_file_creates_item_with_signed_metadata(s3_stub, tmp_path):
    payload = b"legacy payload"
    source = tmp_path / "release.bin"
    source.write_bytes(payload)

    client = _client(s3_stub)
    result = client.upload_file(
        "test-item-1",
        source,
        metadata={"title": "Release Notes", "mediatype": "texts"},
    )

    assert result["ok"] is True
    assert result["item"] == "test-item-1"
    assert result["key"] == "release.bin"
    assert result["size_bytes"] == len(payload)
    assert result["details_url"] == "https://archive.org/details/test-item-1"

    request = s3_stub.last_request()
    assert request["method"] == "PUT"
    assert request["path"] == "/test-item-1/release.bin"
    assert request["body"] == payload
    headers = request["headers"]
    assert headers["x-archive-auto-make-bucket"] == "1"
    assert headers["x-archive-queue-derive"] == "1"
    assert headers["x-archive-size-hint"] == str(len(payload))
    assert headers["x-archive-meta-title"] == "uri(Release%20Notes)"
    assert headers["x-archive-meta-mediatype"] == "texts"

    verify_sigv4(request, secret_key=SECRET_KEY)


def test_upload_file_does_not_send_aws_crc32_headers(s3_stub, tmp_path):
    source = tmp_path / "plain.txt"
    source.write_bytes(b"x")

    _client(s3_stub).upload_file("test-item-2", source)

    headers = s3_stub.last_request()["headers"]
    assert "x-amz-checksum-crc32" not in headers
    assert "x-amz-sdk-checksum-algorithm" not in headers


def test_upload_file_honours_remote_name_and_queue_derive_flag(s3_stub, tmp_path):
    source = tmp_path / "local.bin"
    source.write_bytes(b"data")

    _client(s3_stub).upload_file(
        "test-item-3",
        source,
        remote_name="releases/1.2.0/app.bin",
        queue_derive=False,
        keep_old_version=True,
    )

    request = s3_stub.last_request()
    assert request["path"] == "/test-item-3/releases/1.2.0/app.bin"
    assert request["headers"]["x-archive-queue-derive"] == "0"
    assert request["headers"]["x-archive-keep-old-version"] == "1"


def test_upload_file_reports_checksums(s3_stub, tmp_path):
    import hashlib

    payload = b"checksum me"
    source = tmp_path / "hashed.bin"
    source.write_bytes(payload)

    result = _client(s3_stub).upload_file("test-item-4", source)

    assert result["md5"] == hashlib.md5(payload).hexdigest()
    assert result["sha256"] == hashlib.sha256(payload).hexdigest()


def test_upload_file_rejects_missing_local_file(s3_stub, tmp_path):
    from legacy_mcps.errors import FileNotFoundToolError

    with pytest.raises(FileNotFoundToolError):
        _client(s3_stub).upload_file("test-item-5", tmp_path / "nope.bin")


def test_upload_file_rejects_invalid_identifier(s3_stub, tmp_path):
    source = tmp_path / "f.bin"
    source.write_bytes(b"x")
    with pytest.raises(InvalidInputError):
        _client(s3_stub).upload_file("no", source)


def test_upload_does_not_leak_metadata_headers_into_the_next_upload(s3_stub, tmp_path):
    source = tmp_path / "f.bin"
    source.write_bytes(b"x")
    client = _client(s3_stub)

    client.upload_file("test-item-6", source, metadata={"title": "First"})
    client.upload_file("test-item-6", source)

    assert "x-archive-meta-title" not in s3_stub.last_request()["headers"]


# --- S3 error translation -------------------------------------------------


def test_upload_translates_slowdown_to_rate_limited(s3_stub, tmp_path):
    s3_stub.respond_with_error("SlowDown", 503, "Please reduce your request rate")
    source = tmp_path / "f.bin"
    source.write_bytes(b"x")

    with pytest.raises(RateLimitedError) as excinfo:
        _client(s3_stub).upload_file("test-item-7", source)
    assert excinfo.value.code == "rate_limited"
    assert excinfo.value.to_dict()["retryable"] is True


def test_upload_translates_access_denied_to_remote_rejected(s3_stub, tmp_path):
    s3_stub.respond_with_error("AccessDenied", 403, "Access Denied")
    source = tmp_path / "f.bin"
    source.write_bytes(b"x")

    with pytest.raises(RemoteRejectedError):
        _client(s3_stub).upload_file("test-item-8", source)


def test_upload_translates_no_such_bucket_to_not_found(s3_stub, tmp_path):
    s3_stub.respond_with_error("NoSuchBucket", 404, "no such bucket")
    source = tmp_path / "f.bin"
    source.write_bytes(b"x")

    with pytest.raises(NotFoundError):
        _client(s3_stub).upload_file("test-item-9", source)


def test_upload_wraps_connection_failure_as_connection_error():
    # Port 1 on loopback refuses connections, which exercises the botocore
    # EndpointConnectionError branch without needing a dead host.
    from legacy_mcps.errors import ConnectionToolError

    client = InternetArchiveS3Client(
        ACCESS_KEY,
        SECRET_KEY,
        endpoint_url="http://127.0.0.1:1",
        max_attempts=1,
        timeout=1.0,
    )
    with pytest.raises(ConnectionToolError):
        client.upload_file("test-item-10", __file__)


@pytest.mark.parametrize(
    ("code", "status", "expected"),
    [
        ("SlowDown", 503, RateLimitedError),
        ("AccessDenied", 403, RemoteRejectedError),
        ("NoSuchBucket", 404, NotFoundError),
        ("InvalidArgument", 400, InvalidInputError),
        ("SomethingElse", 500, RemoteRejectedError),
    ],
)
def test_translate_boto_error_maps_s3_codes(code, status, expected):
    from botocore.exceptions import ClientError

    error = ClientError(
        {
            "Error": {"Code": code, "Message": "m"},
            "ResponseMetadata": {"HTTPStatusCode": status},
        },
        "PutObject",
    )
    assert isinstance(translate_boto_error(error, identifier="item"), expected)


def test_translate_boto_error_maps_timeout():
    from botocore.exceptions import ReadTimeoutError

    assert isinstance(
        translate_boto_error(ReadTimeoutError(endpoint_url="http://x")), TimeoutToolError
    )


# --- item existence -------------------------------------------------------


def test_item_exists_true_when_listing_succeeds(s3_stub):
    assert _client(s3_stub).item_exists("test-item-11") is True


def test_item_exists_false_when_bucket_is_missing(s3_stub):
    s3_stub.respond_with_error("NoSuchBucket", 404, "missing")
    assert _client(s3_stub).item_exists("test-item-12") is False


def test_constructor_requires_both_keys():
    with pytest.raises(InvalidInputError):
        InternetArchiveS3Client("", SECRET_KEY)
    with pytest.raises(InvalidInputError):
        InternetArchiveS3Client(ACCESS_KEY, "")


# --- public metadata/search API -------------------------------------------


def test_fetch_metadata_reads_public_endpoint(json_stub, monkeypatch):
    monkeypatch.setattr(api, "METADATA_URL", f"{json_stub.base_url}/metadata/{{identifier}}")
    json_stub.reply_json(
        200,
        {
            "metadata": {"identifier": "nasa", "title": "NASA"},
            "files": [{"name": "a.jpg"}],
            "server": "ia801607.us.archive.org",
            "dir": "/6/items/nasa",
            "item_size": 1234,
        },
    )

    document = api.fetch_metadata("nasa")

    assert document["metadata"]["title"] == "NASA"
    assert json_stub.requests[-1]["path"] == "/metadata/nasa"


def test_fetch_metadata_raises_not_found_for_empty_document(json_stub, monkeypatch):
    monkeypatch.setattr(api, "METADATA_URL", f"{json_stub.base_url}/metadata/{{identifier}}")
    json_stub.reply_json(200, {})
    with pytest.raises(NotFoundError):
        api.fetch_metadata("does-not-exist")


def test_fetch_metadata_requires_identifier():
    with pytest.raises(InvalidInputError):
        api.fetch_metadata("  ")


def test_search_items_builds_expected_query(json_stub, monkeypatch):
    monkeypatch.setattr(api, "SEARCH_URL", f"{json_stub.base_url}/advancedsearch.php")
    json_stub.reply_json(
        200, {"response": {"numFound": 1, "start": 0, "docs": [{"identifier": "x"}]}}
    )

    response = api.search_items("collection:nasa", rows=5, page=2, sort="downloads desc")

    assert response["numFound"] == 1
    path = json_stub.requests[-1]["path"]
    assert "q=collection%3Anasa" in path
    assert "rows=5" in path
    assert "page=2" in path
    assert "sort%5B%5D=downloads+desc" in path


def test_search_items_requires_query():
    with pytest.raises(InvalidInputError):
        api.search_items("   ")


def test_search_items_rejects_bad_paging():
    with pytest.raises(InvalidInputError):
        api.search_items("x", rows=0)
    with pytest.raises(InvalidInputError):
        api.search_items("x", page=0)


# --- tool layer -----------------------------------------------------------


def test_tool_upload_returns_missing_credentials_payload():
    result = tools.upload_file("test-item-13", "/tmp/whatever")
    assert result["ok"] is False
    assert result["error"] == "missing_credentials"
    assert tools.ACCESS_KEY_ENV in result["message"]


def test_tool_upload_succeeds_with_credentials(monkeypatch, s3_stub, tmp_path):
    monkeypatch.setenv(tools.ACCESS_KEY_ENV, ACCESS_KEY)
    monkeypatch.setenv(tools.SECRET_KEY_ENV, SECRET_KEY)
    monkeypatch.setattr(
        tools,
        "_client",
        lambda: _client(s3_stub),
    )
    source = tmp_path / "tool.bin"
    source.write_bytes(b"tool payload")

    result = tools.upload_file("test-item-14", str(source), metadata={"title": "T"})

    assert result["ok"] is True
    assert result["key"] == "tool.bin"


def test_tool_upload_reports_remote_failure_as_payload(monkeypatch, s3_stub, tmp_path):
    s3_stub.respond_with_error("AccessDenied", 403, "denied")
    monkeypatch.setattr(tools, "_client", lambda: _client(s3_stub))
    source = tmp_path / "tool.bin"
    source.write_bytes(b"x")

    result = tools.upload_file("test-item-15", str(source))

    assert result["ok"] is False
    assert result["error"] == "remote_rejected"


def test_tool_get_item_metadata_shapes_response(json_stub, monkeypatch):
    monkeypatch.setattr(api, "METADATA_URL", f"{json_stub.base_url}/metadata/{{identifier}}")
    json_stub.reply_json(
        200,
        {
            "metadata": {"identifier": "nasa"},
            "files": [{"name": f"f{i}"} for i in range(5)],
        },
    )

    result = tools.get_item_metadata("nasa", file_limit=3)

    assert result["ok"] is True
    assert result["identifier"] == "nasa"
    assert result["file_count"] == 5
    assert len(result["files"]) == 3


def test_tool_get_item_metadata_omits_files_when_not_requested(json_stub, monkeypatch):
    monkeypatch.setattr(api, "METADATA_URL", f"{json_stub.base_url}/metadata/{{identifier}}")
    json_stub.reply_json(200, {"metadata": {"identifier": "nasa"}, "files": [{"name": "a"}]})

    result = tools.get_item_metadata("nasa", include_files=False)

    assert "files" not in result


def test_tool_get_item_metadata_returns_error_payload(json_stub, monkeypatch):
    monkeypatch.setattr(api, "METADATA_URL", f"{json_stub.base_url}/metadata/{{identifier}}")
    json_stub.reply_json(200, {})

    result = tools.get_item_metadata("missing-item")

    assert result["ok"] is False
    assert result["error"] == "not_found"


def test_tool_search_shapes_response(json_stub, monkeypatch):
    monkeypatch.setattr(api, "SEARCH_URL", f"{json_stub.base_url}/advancedsearch.php")
    json_stub.reply_json(
        200, {"response": {"numFound": 2, "start": 0, "docs": [{"identifier": "a"}]}}
    )

    result = tools.search_items("nasa")

    assert result["ok"] is True
    assert result["num_found"] == 2
    assert result["results"] == [{"identifier": "a"}]


def test_tool_search_rejects_oversized_rows():
    result = tools.search_items("nasa", rows=5000)
    assert result["ok"] is False
    assert result["error"] == "invalid_input"


def test_has_credentials_reflects_environment(monkeypatch):
    assert tools.has_credentials() is False
    monkeypatch.setenv(tools.ACCESS_KEY_ENV, ACCESS_KEY)
    assert tools.has_credentials() is False
    monkeypatch.setenv(tools.SECRET_KEY_ENV, SECRET_KEY)
    assert tools.has_credentials() is True
    monkeypatch.setenv(tools.SECRET_KEY_ENV, "   ")
    assert tools.has_credentials() is False


def test_require_env_message_lists_every_missing_variable(monkeypatch):
    monkeypatch.setenv(tools.ACCESS_KEY_ENV, ACCESS_KEY)
    with pytest.raises(MissingCredentialsError) as excinfo:
        tools._client()
    error = excinfo.value
    assert error.variables == [tools.SECRET_KEY_ENV]
    assert error.detail == f"unset or blank: {tools.SECRET_KEY_ENV}"
    assert error.to_dict()["error"] == "missing_credentials"
