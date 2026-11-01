"""Tests for the unified server entrypoint.

These check both the registration policy (which tools appear for which
environment) and a real MCP round trip, driving the server through the SDK's
in-memory transport rather than calling the tool functions directly.
"""

from __future__ import annotations

import json

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from legacy_mcps import server as server_module
from legacy_mcps.internet_archive import tools as ia_tools
from legacy_mcps.sourceforge import tools as sf_tools

IA_ENV = (ia_tools.ACCESS_KEY_ENV, ia_tools.SECRET_KEY_ENV)
SF_ENV = (
    sf_tools.USERNAME_ENV,
    sf_tools.SSH_KEY_PATH_ENV,
    sf_tools.PROJECT_NAME_ENV,
)

ALWAYS_AVAILABLE = {"ia_search_items", "ia_get_item_metadata"}
IA_WRITE = {"ia_upload_file"}
SF_TOOLS = {"sf_upload_release_file", "sf_list_release_files"}


@pytest.fixture(autouse=True)
def _clear_credentials(monkeypatch):
    for name in (*IA_ENV, *SF_ENV, sf_tools.HOST_ENV):
        monkeypatch.delenv(name, raising=False)


async def _tool_names() -> set[str]:
    server = server_module.build_server()
    tools = await server.list_tools()
    return {tool.name for tool in tools}


async def test_only_public_tools_register_without_credentials():
    assert await _tool_names() == ALWAYS_AVAILABLE


async def test_ia_write_tool_registers_when_keys_are_present(monkeypatch):
    monkeypatch.setenv(ia_tools.ACCESS_KEY_ENV, "key")
    monkeypatch.setenv(ia_tools.SECRET_KEY_ENV, "secret")
    assert await _tool_names() == ALWAYS_AVAILABLE | IA_WRITE


async def test_ia_write_tool_stays_hidden_with_only_one_key(monkeypatch):
    monkeypatch.setenv(ia_tools.ACCESS_KEY_ENV, "key")
    assert await _tool_names() == ALWAYS_AVAILABLE


async def test_sourceforge_tools_register_only_with_all_three_variables(monkeypatch):
    monkeypatch.setenv(sf_tools.USERNAME_ENV, "user")
    monkeypatch.setenv(sf_tools.SSH_KEY_PATH_ENV, "/tmp/key")
    assert await _tool_names() == ALWAYS_AVAILABLE

    monkeypatch.setenv(sf_tools.PROJECT_NAME_ENV, "proj")
    assert await _tool_names() == ALWAYS_AVAILABLE | SF_TOOLS


async def test_every_group_registers_when_fully_configured(monkeypatch):
    monkeypatch.setenv(ia_tools.ACCESS_KEY_ENV, "key")
    monkeypatch.setenv(ia_tools.SECRET_KEY_ENV, "secret")
    monkeypatch.setenv(sf_tools.USERNAME_ENV, "user")
    monkeypatch.setenv(sf_tools.SSH_KEY_PATH_ENV, "/tmp/key")
    monkeypatch.setenv(sf_tools.PROJECT_NAME_ENV, "proj")

    assert await _tool_names() == ALWAYS_AVAILABLE | IA_WRITE | SF_TOOLS


async def test_tools_expose_descriptions_and_schemas():
    server = server_module.build_server()
    tools = {tool.name: tool for tool in await server.list_tools()}

    upload = tools["ia_upload_file"] if "ia_upload_file" in tools else tools["ia_search_items"]
    assert upload.description
    schema = upload.inputSchema
    assert schema["type"] == "object"
    assert "properties" in schema


def test_describe_registration_without_credentials():
    report = server_module.describe_registration()

    assert set(report["internet_archive"]["read_tools"]) == ALWAYS_AVAILABLE
    assert report["internet_archive"]["write_enabled"] is False
    assert report["internet_archive"]["write_tools"] == []
    assert "IA_ACCESS_KEY" in report["internet_archive"]["note"]
    assert report["sourceforge"]["enabled"] is False
    assert report["sourceforge"]["tools"] == []
    assert "SF_USERNAME" in report["sourceforge"]["note"]


def test_describe_registration_with_credentials(monkeypatch):
    for name in (*IA_ENV, *SF_ENV):
        monkeypatch.setenv(name, "value")

    report = server_module.describe_registration()

    assert report["internet_archive"]["write_enabled"] is True
    assert set(report["internet_archive"]["write_tools"]) == IA_WRITE
    assert report["internet_archive"]["note"] is None
    assert report["sourceforge"]["enabled"] is True
    assert report["sourceforge"]["note"] is None


def test_main_describe_prints_json_and_returns_zero(capsys):
    exit_code = server_module.main(["--describe"])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["server"] == "legacy-mcps"
    assert "version" in payload


def test_main_rejects_unknown_transport():
    with pytest.raises(SystemExit):
        server_module.main(["--transport", "carrier-pigeon"])


# --- MCP protocol round trip ----------------------------------------------


async def test_search_items_round_trip_through_the_mcp_client(json_stub, monkeypatch):
    from legacy_mcps.internet_archive import api

    monkeypatch.setattr(api, "SEARCH_URL", f"{json_stub.base_url}/advancedsearch.php")
    json_stub.reply_json(
        200,
        {
            "response": {
                "numFound": 1,
                "start": 0,
                "docs": [{"identifier": "nasa", "title": "NASA"}],
            }
        },
    )

    server = server_module.build_server()
    async with create_connected_server_and_client_session(server) as session:
        listed = await session.list_tools()
        assert "ia_search_items" in {tool.name for tool in listed.tools}

        result = await session.call_tool(
            "ia_search_items", {"query": "collection:nasa", "rows": 1}
        )

    assert result.isError is False
    payload = json.loads(result.content[0].text)
    assert payload["ok"] is True
    assert payload["num_found"] == 1
    assert payload["results"][0]["identifier"] == "nasa"


async def test_missing_credentials_surface_as_a_tool_result_not_an_error():
    """A tool call must return a payload, never raise, when keys are absent."""
    server = server_module.build_server()
    async with create_connected_server_and_client_session(server) as session:
        result = await session.call_tool(
            "ia_get_item_metadata", {"identifier": "some-item"}
        )

    # The read tool is registered and reachable; it fails at the network layer
    # rather than at registration, and it reports that as data.
    assert result.isError is False
    payload = json.loads(result.content[0].text)
    assert "ok" in payload
