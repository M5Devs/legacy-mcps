# legacy-mcps

A [Model Context Protocol](https://modelcontextprotocol.io/) server that gives AI
agents tools for managing files and data on two legacy platforms:

- **Internet Archive** — search items, read item metadata, and upload files to
  items over the archive.org S3 gateway.
- **SourceForge** — upload release files to a project's File Release System
  (FRS) tree and list what is published, over SSH/SFTP.

The server is credential-aware: read-only tools that need no authentication are
always available, and write tools are registered only when the matching
credentials are present in the environment.

## Install

```bash
pip install -e .          # runtime dependencies only
pip install -e '.[dev]'   # plus pytest
```

Requires Python 3.10 or newer.

## Run

```bash
legacy-mcps                       # stdio transport (default)
legacy-mcps --transport sse       # also: streamable-http
legacy-mcps --describe            # print which tools are enabled, then exit
```

`--describe` is the quickest way to check configuration. It prints the
registration decision as JSON without opening a transport:

```json
{
  "server": "legacy-mcps",
  "version": "0.1.0",
  "internet_archive": {
    "read_tools": ["ia_search_items", "ia_get_item_metadata"],
    "write_tools": ["ia_upload_file"],
    "enabled": true,
    "write_enabled": true,
    "note": null
  },
  "sourceforge": {
    "tools": ["sf_upload_release_file", "sf_list_release_files"],
    "enabled": true,
    "note": null
  }
}
```

### Client configuration

An MCP client launches the server as a subprocess and talks to it over stdio.
For example, in a `mcpServers` block:

```json
{
  "mcpServers": {
    "legacy-mcps": {
      "command": "legacy-mcps",
      "env": {
        "IA_ACCESS_KEY": "...",
        "IA_SECRET_KEY": "...",
        "SF_USERNAME": "your-sourceforge-username",
        "SF_SSH_KEY_PATH": "/home/you/.ssh/sourceforge_rsa",
        "SF_PROJECT_NAME": "your-project"
      }
    }
  }
}
```

## Configuration

Every credential is read from the environment at tool-registration time and at
call time. Nothing is stored on disk.

| Variable           | Used by        | Meaning |
| ------------------ | -------------- | ------- |
| `IA_ACCESS_KEY`    | Internet Archive | S3 access key from <https://archive.org/account/s3.php> |
| `IA_SECRET_KEY`    | Internet Archive | S3 secret key from the same page |
| `SF_USERNAME`      | SourceForge    | Your SourceForge account name |
| `SF_SSH_KEY_PATH`  | SourceForge    | Path to the private key whose public half is registered with SourceForge |
| `SF_PROJECT_NAME`  | SourceForge    | The unix name of the project, e.g. `my-project` |
| `SF_HOST`          | SourceForge    | SSH host, defaults to `frs.sourceforge.net` |

Which tools appear depends on what is set:

| Credentials present                              | Tools registered |
| ------------------------------------------------ | ---------------- |
| none                                             | `ia_search_items`, `ia_get_item_metadata` |
| `IA_ACCESS_KEY` + `IA_SECRET_KEY`                | the above, plus `ia_upload_file` |
| `SF_USERNAME` + `SF_SSH_KEY_PATH` + `SF_PROJECT_NAME` | `sf_upload_release_file`, `sf_list_release_files` |

## Tools

### Internet Archive

| Tool | Credentials | Description |
| ---- | ----------- | ----------- |
| `ia_search_items` | none | Search archive.org with a Lucene-style query, e.g. `collection:nasa AND mediatype:image`. |
| `ia_get_item_metadata` | none | Fetch an item's metadata and file listing by identifier. |
| `ia_upload_file` | `IA_ACCESS_KEY`, `IA_SECRET_KEY` | Upload a local file to an item, creating the item if it does not exist. |

`ia_upload_file` takes `identifier`, `file_path`, an optional `remote_name`,
optional `metadata` (which becomes `x-archive-meta-*` headers — `title`,
`mediatype` and `collection` are the fields the site uses to build the item's
details page), `queue_derive` and `keep_old_version`.

Uploads go to the custom S3 endpoint `https://s3.us.archive.org`. The
`x-archive-*` headers are injected into the request *before* SigV4 signing, so
they are covered by the signature the gateway verifies.

### SourceForge

| Tool | Credentials | Description |
| ---- | ----------- | ----------- |
| `sf_upload_release_file` | `SF_USERNAME`, `SF_SSH_KEY_PATH`, `SF_PROJECT_NAME` | Upload a local file into a release directory such as `releases/1.2.0`, creating the directory if needed. |
| `sf_list_release_files` | same | List files and directories in a release directory. An empty `remote_dir` lists the project release root. |

Uploads land in `/home/frs/project/<project>/<remote_dir>/` on
`frs.sourceforge.net`. Each file is written to a temporary name and then renamed
into place, so a partial transfer is never visible and the mirror sees a single
completed write. `overwrite=False` (the default) refuses to replace an existing
file.

## Response shape

Tools never raise and never tear down the MCP session. Every call returns a JSON
object:

```json
{ "ok": true, "remote_path": "/home/frs/project/my-project/releases/1.2.0/app.tar.gz", "size_bytes": 4096, "download_url": "https://sourceforge.net/projects/my-project/files/releases/1.2.0/app.tar.gz/download" }
```

```json
{ "ok": false, "error": "missing_credentials", "message": "Missing required environment variable(s): SF_USERNAME, SF_PROJECT_NAME.", "retryable": false }
```

`error` is one of a small stable set: `missing_credentials`, `invalid_input`,
`file_not_found`, `remote_rejected`, `not_found`, `rate_limited`, `timeout`,
`connection_error`, `unexpected_error`. `retryable` is true for `rate_limited`,
`timeout` and `connection_error`, so an agent can retry without guessing.

## Project layout

```
src/legacy_mcps/
├── errors.py                  # error codes, error payloads, credential helpers
├── server.py                  # unified entrypoint; credential-aware registration
├── internet_archive/
│   ├── api.py                 # public metadata + search HTTP endpoints
│   ├── client.py              # S3 client: SigV4 signing, x-archive-* headers, upload
│   └── tools.py               # MCP tool wrappers
└── sourceforge/
    ├── client.py              # SFTP client: upload, list, path validation
    └── tools.py               # MCP tool wrappers
```

## Tests

```bash
pip install -e '.[dev]'
pytest
```

The suite is deliberately built on real protocol code rather than mocks:

- **Internet Archive** tests drive a real `boto3` client against a loopback HTTP
  server standing in for the S3 gateway, then re-derive the SigV4 signature from
  the recorded request. That checks the property the real gateway enforces — that
  the injected `x-archive-meta-*` headers are part of the signed header set.
- **SourceForge** tests run against an in-process SSH/SFTP server (paramiko)
  rooted in a temporary directory, so the stage-then-rename write path, directory
  creation, overwrite handling and error mapping are all exercised end to end.
- **Server** tests drive the FastMCP server through the SDK's in-memory
  transport, including a full `list_tools` / `call_tool` round trip.

## License

MIT — see [LICENSE](LICENSE).
