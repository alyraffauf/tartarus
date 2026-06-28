"""Shared test infrastructure: HTTP server, fake provider clients, etc."""

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class HelloHandler(BaseHTTPRequestHandler):
    """A tiny HTTP handler that answers every GET with a fixed body "hello"."""

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


class HttpServer:
    """Context manager that runs a ThreadingHTTPServer on a random port."""

    def __init__(self, handler=None):
        self._server = ThreadingHTTPServer(
            ("127.0.0.1", 0), handler if handler is not None else HelloHandler
        )
        self._thread: threading.Thread | None = None

    def __enter__(self):
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="tartarus-test-http-server",
            daemon=True,
        )
        self._thread.start()
        return self._server

    def __exit__(self, *_args):
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
