"""Filtering HTTP proxy for scoped network grants.

The proxy enforces a per-call host:port allow-list for proxy-aware tools. It
supports absolute-form HTTP requests and HTTPS `CONNECT` tunnels.
"""

import select
import socket
import socketserver
import threading
import urllib.parse
from collections.abc import Iterable
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler
from typing import cast

BUFFER_SIZE_BYTES = 64 * 1024
SOCKET_TIMEOUT_SECONDS = 30
WILDCARD_ALLOWED_HOST = "*"


class NetworkProxyError(Exception):
    """Raised when the filtering proxy cannot be started or used."""


@dataclass(frozen=True)
class ProxyDecision:
    host: str
    port: int
    allowed: bool
    method: str


class FilteringProxy:
    def __init__(self, allowed_hosts: Iterable[str], host: str = "127.0.0.1"):
        self._allowed_hosts = frozenset(
            _normalize_allowed_host(host) for host in allowed_hosts
        )
        self._host = host
        self._server: _ThreadedProxyServer | None = None
        self._thread: threading.Thread | None = None
        self._decisions: list[ProxyDecision] = []

    @property
    def url(self) -> str:
        if self._server is None:
            raise NetworkProxyError("proxy has not been started")
        host, port = cast(tuple[str, int], self._server.server_address)
        return f"http://{host}:{port}"

    def __enter__(self) -> "FilteringProxy":
        self.start()
        return self

    def __exit__(self, *_args) -> None:
        self.stop()

    @property
    def decisions(self) -> list[ProxyDecision]:
        return list(self._decisions)

    def summary(self) -> str:
        if not self._decisions:
            return "proxy decisions: none"

        allowed_count = sum(1 for decision in self._decisions if decision.allowed)
        blocked_count = len(self._decisions) - allowed_count
        targets = ", ".join(
            f"{decision.method} {decision.host}:{decision.port} "
            f"{'allowed' if decision.allowed else 'blocked'}"
            for decision in self._decisions
        )
        return (
            f"proxy decisions: {allowed_count} allowed, "
            f"{blocked_count} blocked ({targets})"
        )

    def start(self) -> None:
        if self._server is not None:
            return

        self._server = _ThreadedProxyServer(
            (self._host, 0),
            _FilteringProxyHandler,
            self._allowed_hosts,
            self._decisions,
        )
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="tartarus-nix-filtering-proxy",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        if self._server is None:
            return

        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=SOCKET_TIMEOUT_SECONDS)
        self._server = None
        self._thread = None


class _ThreadedProxyServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(
        self,
        address,
        handler,
        allowed_hosts: frozenset[str],
        decisions: list[ProxyDecision],
    ):
        super().__init__(address, handler)
        self.allowed_hosts = allowed_hosts
        self.decisions = decisions


class _FilteringProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_CONNECT(self) -> None:
        host, port = _split_host_port(self.path, default_port=443)
        if not self._is_allowed(host, port):
            self.send_error(403, "host not allowed")
            return

        try:
            with socket.create_connection(
                (host, port), timeout=SOCKET_TIMEOUT_SECONDS
            ) as upstream:
                self.send_response(200, "Connection established")
                self.end_headers()
                self._tunnel(upstream)
        except OSError as error:
            self.send_error(502, f"upstream connection failed: {error}")

    def do_GET(self) -> None:
        self._proxy_http_request()

    def do_HEAD(self) -> None:
        self._proxy_http_request()

    def do_POST(self) -> None:
        self._proxy_http_request()

    def do_PUT(self) -> None:
        self._proxy_http_request()

    def do_DELETE(self) -> None:
        self._proxy_http_request()

    def do_PATCH(self) -> None:
        self._proxy_http_request()

    def log_message(self, format: str, *args) -> None:
        pass

    def _proxy_http_request(self) -> None:
        parsed_url = urllib.parse.urlsplit(self.path)
        if not parsed_url.scheme or not parsed_url.hostname:
            self.send_error(400, "proxy requires absolute-form URL")
            return

        port = parsed_url.port or _default_port(parsed_url.scheme)
        if not self._is_allowed(parsed_url.hostname, port):
            self.send_error(403, "host not allowed")
            return

        target_path = urllib.parse.urlunsplit(
            ("", "", parsed_url.path or "/", parsed_url.query, "")
        )
        body = self.rfile.read(_content_length(self.headers))
        request = self._build_upstream_request(target_path, body)

        try:
            with socket.create_connection(
                (parsed_url.hostname, port),
                timeout=SOCKET_TIMEOUT_SECONDS,
            ) as upstream:
                upstream.sendall(request)
                self._relay_response(upstream)
        except OSError as error:
            self.send_error(502, f"upstream request failed: {error}")

    def _build_upstream_request(self, target_path: str, body: bytes) -> bytes:
        request_line = f"{self.command} {target_path} HTTP/1.1\r\n"
        headers = []
        for name, value in self.headers.items():
            if name.lower() in {"proxy-connection", "connection"}:
                continue
            headers.append(f"{name}: {value}\r\n")
        headers.append("Connection: close\r\n")
        return (request_line + "".join(headers) + "\r\n").encode("iso-8859-1") + body

    def _relay_response(self, upstream: socket.socket) -> None:
        while True:
            chunk = upstream.recv(BUFFER_SIZE_BYTES)
            if not chunk:
                return
            self.connection.sendall(chunk)

    def _tunnel(self, upstream: socket.socket) -> None:
        sockets = [self.connection, upstream]
        for active_socket in sockets:
            active_socket.settimeout(SOCKET_TIMEOUT_SECONDS)

        while True:
            readable, _, _ = select.select(sockets, [], [], SOCKET_TIMEOUT_SECONDS)
            if not readable:
                return
            for active_socket in readable:
                chunk = active_socket.recv(BUFFER_SIZE_BYTES)
                if not chunk:
                    return
                target = (
                    upstream if active_socket is self.connection else self.connection
                )
                target.sendall(chunk)

    def _is_allowed(self, host: str, port: int) -> bool:
        server = cast(_ThreadedProxyServer, self.server)
        allowed = (
            WILDCARD_ALLOWED_HOST in server.allowed_hosts
            or _normalize_allowed_host(f"{host}:{port}") in server.allowed_hosts
        )
        server.decisions.append(
            ProxyDecision(
                host=host.lower(), port=port, allowed=allowed, method=self.command
            )
        )
        return allowed


def _split_host_port(value: str, default_port: int) -> tuple[str, int]:
    if ":" not in value:
        return value, default_port
    host, port = value.rsplit(":", 1)
    return host, int(port)


def _default_port(scheme: str) -> int:
    if scheme == "https":
        return 443
    return 80


def _content_length(headers) -> int:
    value = headers.get("Content-Length")
    if value is None:
        return 0
    return int(value)


def _normalize_allowed_host(value: str) -> str:
    if value == WILDCARD_ALLOWED_HOST:
        return WILDCARD_ALLOWED_HOST
    host, port = _split_host_port(value.lower(), default_port=443)
    return f"{host}:{port}"
