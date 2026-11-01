"""Unified MCP server exposing the Internet Archive and SourceForge tools.

Tool registration is credential-aware:

* ``search_items`` and ``get_item_metadata`` are always registered, because the
  public archive.org endpoints need no credentials.
* The Internet Archive write tool is registered only when both ``IA_ACCESS_KEY``
  and ``IA_SECRET_KEY`` are present.
* Both SourceForge tools are registered only when ``SF_USERNAME``,
  ``SF_SSH_KEY_PATH`` and ``SF_PROJECT_NAME`` are present.

Run ``python -m legacy_mcps.server --describe`` to print the registration
decision without starting a transport.
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

from mcp.server.fastmcp import FastMCP

from legacy_mcps import __version__
from legacy_mcps.errors import MissingCredentialsError
from legacy_mcps.internet_archive import tools as ia_tools
from legacy_mcps.sourceforge import tools as sf_tools

SERVER_NAME = "legacy-mcps"

INSTRUCTIONS = """\
Tools for managing files and data on legacy platforms.

Internet Archive (archive.org)
  - search_items / get_item_metadata work without credentials.
  - upload_file needs IA_ACCESS_KEY and IA_SECRET_KEY. Items are addressed by
    identifier; uploading to a new identifier creates the item.

SourceForge (sourceforge.net)
  - upload_release_file / list_release_files need SF_USERNAME, SF_SSH_KEY_PATH
    and SF_PROJECT_NAME. Uploads land in the project's File Release System tree
    and are mirrored to the public download pages.

Every tool returns a JSON object. Success carries ok=true; failure carries
ok=false, an "error" code and a human-readable "message". Errors never abort
the session, so a failed call is safe to retry or report.
"""


def build_server() -> FastMCP:
    """Create the FastMCP server with every credential-available tool bound."""
    server = FastMCP(name=SERVER_NAME, instructions=INSTRUCTIONS)

    # -- always available: public archive.org read APIs --------------------
    server.tool(
        name="ia_search_items",
        description=(
            "Search archive.org items. Example query: "
            "'collection:nasa AND mediatype:image'. No credentials required."
        ),
    )(ia_tools.search_items)

    server.tool(
        name="ia_get_item_metadata",
        description=(
            "Fetch an archive.org item's metadata and file listing by "
            "identifier. No credentials required."
        ),
    )(ia_tools.get_item_metadata)

    # -- Internet Archive writes ------------------------------------------
    if ia_tools.has_credentials():
        server.tool(
            name="ia_upload_file",
            description=(
                "Upload a local file to an archive.org item, creating the item "
                "if needed. Set metadata such as title, mediatype and "
                "collection so the item is usable on the site."
            ),
        )(ia_tools.upload_file)

    # -- SourceForge -------------------------------------------------------
    if sf_tools.has_credentials():
        server.tool(
            name="sf_upload_release_file",
            description=(
                "Upload a local file into a SourceForge project's File Release "
                "System directory, e.g. 'releases/1.2.0'."
            ),
        )(sf_tools.upload_release_file)

        server.tool(
            name="sf_list_release_files",
            description=(
                "List files and directories in a SourceForge release directory. "
                "An empty remote_dir lists the project release root."
            ),
        )(sf_tools.list_release_files)

    return server


def describe_registration() -> dict[str, Any]:
    """Report which tool groups are enabled, without starting a transport."""
    ia_enabled = ia_tools.has_credentials()
    sf_enabled = sf_tools.has_credentials()
    return {
        "server": SERVER_NAME,
        "version": __version__,
        "internet_archive": {
            "read_tools": ["ia_search_items", "ia_get_item_metadata"],
            "write_tools": ["ia_upload_file"] if ia_enabled else [],
            "enabled": True,
            "write_enabled": ia_enabled,
            "note": None if ia_enabled else ia_tools.missing_credentials_message()["message"],
        },
        "sourceforge": {
            "tools": ["sf_upload_release_file", "sf_list_release_files"]
            if sf_enabled
            else [],
            "enabled": sf_enabled,
            "note": None if sf_enabled else sf_tools.missing_credentials_message()["message"],
        },
    }


def main(argv: list[str] | None = None) -> int:
    """Entry point for the ``legacy-mcps`` console script."""
    parser = argparse.ArgumentParser(
        prog=SERVER_NAME,
        description="MCP server for legacy platforms (Internet Archive, SourceForge).",
    )
    parser.add_argument(
        "--transport",
        choices=("stdio", "sse", "streamable-http"),
        default="stdio",
        help="MCP transport to serve (default: stdio).",
    )
    parser.add_argument(
        "--describe",
        action="store_true",
        help="Print which tools are enabled for the current environment and exit.",
    )
    args = parser.parse_args(argv)

    if args.describe:
        import json

        print(json.dumps(describe_registration(), indent=2))
        return 0

    try:
        server = build_server()
    except MissingCredentialsError as exc:  # pragma: no cover - defensive
        print(exc.message, file=sys.stderr)
        return 2
    server.run(transport=args.transport)
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised via the console script
    raise SystemExit(main())
