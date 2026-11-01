"""Shared pytest fixtures.

The SourceForge tests run against a real SSH server, served in-process by
paramiko on a loopback socket. That exercises the production code path
(transport, SFTP subsystem, put/rename/remove) instead of asserting against a
mock's call log, which is the only way to catch ordering and error-mapping bugs
in an upload that stages to a temporary name before renaming it into place.
"""

from __future__ import annotations

import os
import posixpath
import socket
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

import paramiko
import pytest
from paramiko import SFTPServer, SFTPServerInterface, SFTPHandle


class _ThreadingHTTPServer(ThreadingHTTPServer):
    """A threaded HTTP server.

    The stub gateways must handle several connections at once: clients keep
    their connection alive after a request, and a single-threaded server would
    block on that idle connection instead of accepting the next client.
    """

    daemon_threads = True
    allow_reuse_address = True


class _JailedSFTPInterface(SFTPServerInterface):
    """An SFTP interface that serves a real directory and cannot escape it.

    Paths are rewritten into the served directory, then handed to paramiko's
    default implementations, which are plain ``os`` calls. Every path that the
    client sends is absolute (``/home/frs/project/...``), so ``lstrip('/')``
    maps it under ``root``.
    """

    root: Path

    def _local(self, path: str) -> str:
        joined = posixpath.join(str(self.root), path.lstrip("/"))
        return os.path.normpath(joined)

    def canonicalize(self, path: str) -> str:
        return self._local(path)

    def open(self, path, flags, attr):
        target = self._local(path)
        mode = "rb" if flags & os.O_RDONLY else "wb"
        if flags & os.O_RDWR:
            mode = "r+b"
        if flags & os.O_APPEND:
            mode = "ab"
        try:
            fileobj = open(target, mode)
        except OSError as exc:
            return SFTPServer.convert_errno(exc.errno)
        return _LocalHandle(fileobj)

    def list_folder(self, path):
        target = self._local(path)
        try:
            names = os.listdir(target)
        except OSError as exc:
            return SFTPServer.convert_errno(exc.errno)
        out = []
        for name in names:
            try:
                attr = paramiko.SFTPAttributes.from_stat(os.stat(os.path.join(target, name)))
            except OSError as exc:
                return SFTPServer.convert_errno(exc.errno)
            attr.filename = name
            out.append(attr)
        return out

    def stat(self, path):
        try:
            return paramiko.SFTPAttributes.from_stat(os.stat(self._local(path)))
        except OSError as exc:
            return SFTPServer.convert_errno(exc.errno)

    def lstat(self, path):
        try:
            return paramiko.SFTPAttributes.from_stat(os.lstat(self._local(path)))
        except OSError as exc:
            return SFTPServer.convert_errno(exc.errno)

    def remove(self, path):
        try:
            os.remove(self._local(path))
        except OSError as exc:
            return SFTPServer.convert_errno(exc.errno)
        return paramiko.SFTP_OK

    def mkdir(self, path, attr):
        try:
            os.mkdir(self._local(path))
        except OSError as exc:
            return SFTPServer.convert_errno(exc.errno)
        return paramiko.SFTP_OK

    def rmdir(self, path):
        try:
            os.rmdir(self._local(path))
        except OSError as exc:
            return SFTPServer.convert_errno(exc.errno)
        return paramiko.SFTP_OK

    def rename(self, oldpath, newpath):
        try:
            os.replace(self._local(oldpath), self._local(newpath))
        except OSError as exc:
            return SFTPServer.convert_errno(exc.errno)
        return paramiko.SFTP_OK

    posix_rename = rename


class _LocalHandle(SFTPHandle):
    """SFTPHandle backed by a real file object.

    ``readfile`` and ``writefile`` are what the base class uses for the default
    read/write implementations, so no further overrides are needed.
    """

    def __init__(self, fileobj):
        super().__init__(flags=0)
        self._file = fileobj
        self.readfile = fileobj
        self.writefile = fileobj

    def close(self):
        try:
            self._file.close()
        except Exception:
            pass
        super().close()


class _AcceptKeyServer(paramiko.ServerInterface):
    """Accepts the single test public key for any username."""

    def __init__(self, authorized_key: paramiko.PKey):
        self._authorized_key = authorized_key

    def check_auth_publickey(self, username, key):
        if key == self._authorized_key:
            return paramiko.AUTH_SUCCESSFUL
        return paramiko.AUTH_FAILED

    def get_allowed_auths(self, username):
        return "publickey"

    def check_channel_request(self, kind, chanid):
        return paramiko.OPEN_SUCCEEDED


class InProcessSFTPServer:
    """A throwaway SSH/SFTP server on 127.0.0.1 serving ``root``."""

    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.host_key = paramiko.RSAKey.generate(2048)
        self.authorized_key = paramiko.RSAKey.generate(2048)
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._socket.bind(("127.0.0.1", 0))
        self._socket.listen(8)
        self.host, self.port = self._socket.getsockname()
        self._transports: list[paramiko.Transport] = []
        self._stop = threading.Event()
        threading.Thread(target=self._accept_loop, daemon=True).start()

    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            try:
                client_socket, _ = self._socket.accept()
            except OSError:
                return
            threading.Thread(
                target=self._handle_client, args=(client_socket,), daemon=True
            ).start()

    def _handle_client(self, client_socket: socket.socket) -> None:
        root = self.root

        class _Interface(_JailedSFTPInterface):
            def __init__(self, server, *args, **kwargs):
                super().__init__(server, *args, **kwargs)
                self.root = root

        transport = paramiko.Transport(client_socket)
        transport.add_server_key(self.host_key)
        transport.set_subsystem_handler("sftp", SFTPServer, _Interface)
        self._transports.append(transport)
        try:
            transport.start_server(server=_AcceptKeyServer(self.authorized_key))
            channel = transport.accept(10)
            if channel is None:
                return
            while transport.is_active() and not self._stop.is_set():
                time.sleep(0.05)
        except Exception:
            pass
        finally:
            try:
                transport.close()
            except Exception:
                pass

    def close(self) -> None:
        self._stop.set()
        for transport in self._transports:
            try:
                transport.close()
            except Exception:
                pass
        try:
            self._socket.close()
        except Exception:
            pass


@pytest.fixture
def sftp_server(tmp_path):
    """Start an in-process SFTP server rooted at ``tmp_path``."""
    server = InProcessSFTPServer(tmp_path)
    try:
        yield server
    finally:
        server.close()


@pytest.fixture
def sftp_key_file(tmp_path_factory, sftp_server) -> Path:
    """Write the server's authorized private key to disk for the client."""
    key_path = tmp_path_factory.mktemp("keys") / "id_rsa"
    sftp_server.authorized_key.write_private_key_file(str(key_path))
    key_path.chmod(0o600)
    return key_path


# --- Internet Archive S3 stub ---------------------------------------------


class S3StubServer:
    """A loopback HTTP server that impersonates the archive.org S3 gateway.

    Tests can point a real boto3 client at it, so the upload path under test
    includes botocore's request building, header injection and SigV4 signing.
    Every request is recorded for assertions.
    """

    def __init__(self):
        import http.server

        stub = self

        class _Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _dispatch(self, method: str) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                stub.requests.append(
                    {
                        "method": method,
                        "path": self.path,
                        "headers": {k.lower(): v for k, v in self.headers.items()},
                        "body": body,
                    }
                )
                status, headers, payload = stub.responder(method, self.path)
                self.send_response(status)
                for key, value in headers.items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                if payload:
                    self.wfile.write(payload)

            def do_PUT(self):
                self._dispatch("PUT")

            def do_GET(self):
                self._dispatch("GET")

            def do_DELETE(self):
                self._dispatch("DELETE")

            def log_message(self, *args):
                pass

        self.requests: list[dict] = []
        self.responder = self._default_responder
        self._server = _ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.host, self.port = self._server.server_address
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def endpoint_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def _default_responder(self, method: str, path: str):
        if method == "PUT":
            return 200, {"ETag": '"d41d8cd98f00b204e9800998ecf8427e"'}, b""
        return 200, {}, _LIST_EMPTY_XML

    def respond_with_error(self, code: str, status: int, message: str = "boom") -> None:
        body = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            f"<Error><Code>{code}</Code><Message>{message}</Message></Error>"
        ).encode()

        def responder(method: str, path: str):
            return status, {"Content-Type": "application/xml"}, body

        self.responder = responder

    def last_request(self) -> dict:
        assert self.requests, "no request was made to the S3 stub"
        return self.requests[-1]

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


_LIST_EMPTY_XML = (
    b'<?xml version="1.0" encoding="UTF-8"?>'
    b'<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
    b"<Name>bucket</Name><Prefix></Prefix><KeyCount>0</KeyCount>"
    b"<MaxKeys>1</MaxKeys><IsTruncated>false</IsTruncated>"
    b"</ListBucketResult>"
)


@pytest.fixture
def s3_stub():
    """Start the fake archive.org S3 gateway."""
    server = S3StubServer()
    try:
        yield server
    finally:
        server.close()


class JSONStubServer:
    """A loopback HTTP server that returns a canned JSON response."""

    def __init__(self):
        import http.server

        stub = self

        class _Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self):
                stub.requests.append({"path": self.path})
                status, body = stub.response
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.requests: list[dict] = []
        self.response: tuple[int, bytes] = (200, b"{}")
        self._server = _ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.host, self.port = self._server.server_address
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def reply_json(self, status: int, payload) -> None:
        import json

        self.response = (status, json.dumps(payload).encode())

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


@pytest.fixture
def json_stub():
    """Start a canned-response JSON HTTP server."""
    server = JSONStubServer()
    try:
        yield server
    finally:
        server.close()


def verify_sigv4(request: dict, *, secret_key: str, region: str = "us-east-1") -> None:
    """Recompute the SigV4 signature of a recorded request and assert it matches.

    This proves the injected ``x-archive-*`` headers were part of the signed
    header set, which is the property the Internet Archive gateway enforces.
    """
    import hashlib
    import hmac
    import re

    headers = request["headers"]
    authorization = headers["authorization"]
    match = re.match(
        r"AWS4-HMAC-SHA256 Credential=[^/]+/(\d{8})/([^/]+)/([^/]+)/aws4_request, "
        r"SignedHeaders=([^,]+), Signature=([0-9a-f]+)",
        authorization,
    )
    assert match, f"unexpected Authorization header: {authorization}"
    datestamp, region_name, service, signed_headers, signature = match.groups()

    canonical_headers = "".join(
        f"{name}:{headers[name].strip()}\n" for name in signed_headers.split(";")
    )
    payload_hash = hashlib.sha256(request["body"]).hexdigest()
    canonical_request = "\n".join(
        [
            request["method"],
            request["path"],
            "",
            canonical_headers,
            signed_headers,
            payload_hash,
        ]
    )
    scope = f"{datestamp}/{region_name}/{service}/aws4_request"
    string_to_sign = "\n".join(
        [
            "AWS4-HMAC-SHA256",
            headers["x-amz-date"],
            scope,
            hashlib.sha256(canonical_request.encode()).hexdigest(),
        ]
    )

    def _sign(key: bytes, message: str) -> bytes:
        return hmac.new(key, message.encode(), hashlib.sha256).digest()

    key = _sign(("AWS4" + secret_key).encode(), datestamp)
    for element in (region_name, service, "aws4_request"):
        key = _sign(key, element)
    expected = hmac.new(key, string_to_sign.encode(), hashlib.sha256).hexdigest()
    assert expected == signature, "SigV4 signature does not match the request"
