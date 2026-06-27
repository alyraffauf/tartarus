import http.client
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from tartarus.network_proxy import FilteringProxy


class _HelloHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def do_GET(self):
        body = b"hello"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args) -> None:
        pass


def test_filtering_proxy_allows_listed_host():
    with _HttpServer() as upstream:
        upstream_host, upstream_port = upstream.server_address

        with FilteringProxy([f"{upstream_host}:{upstream_port}"]) as proxy:
            proxy_host, proxy_port = _proxy_address(proxy.url)
            connection = http.client.HTTPConnection(proxy_host, proxy_port)
            connection.request("GET", f"http://{upstream_host}:{upstream_port}/")
            response = connection.getresponse()

            assert response.status == 200
            assert response.read() == b"hello"
            assert proxy.summary() == (
                f"proxy decisions: 1 allowed, 0 blocked "
                f"(GET {upstream_host}:{upstream_port} allowed)"
            )


def test_filtering_proxy_denies_unlisted_host():
    with _HttpServer() as upstream:
        upstream_host, upstream_port = upstream.server_address

        with FilteringProxy(["example.com:80"]) as proxy:
            proxy_host, proxy_port = _proxy_address(proxy.url)
            connection = http.client.HTTPConnection(proxy_host, proxy_port)
            connection.request("GET", f"http://{upstream_host}:{upstream_port}/")
            response = connection.getresponse()

            assert response.status == 403
            assert proxy.summary() == (
                f"proxy decisions: 0 allowed, 1 blocked "
                f"(GET {upstream_host}:{upstream_port} blocked)"
            )


def test_filtering_proxy_wildcard_allows_any_host():
    with _HttpServer() as upstream:
        upstream_host, upstream_port = upstream.server_address

        with FilteringProxy(["*"]) as proxy:
            proxy_host, proxy_port = _proxy_address(proxy.url)
            connection = http.client.HTTPConnection(proxy_host, proxy_port)
            connection.request("GET", f"http://{upstream_host}:{upstream_port}/")
            response = connection.getresponse()

            assert response.status == 200
            assert response.read() == b"hello"
            assert proxy.summary() == (
                f"proxy decisions: 1 allowed, 0 blocked "
                f"(GET {upstream_host}:{upstream_port} allowed)"
            )


class _HttpServer:
    def __enter__(self):
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _HelloHandler)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="tartarus-nix-test-http-server",
            daemon=True,
        )
        self._thread.start()
        return self._server

    def __exit__(self, *_args):
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


def _proxy_address(proxy_url: str) -> tuple[str, int]:
    host_port = proxy_url.removeprefix("http://")
    host, port = host_port.rsplit(":", 1)
    return host, int(port)
