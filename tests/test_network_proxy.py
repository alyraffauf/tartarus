import http.client

from tartarus.network_proxy import FilteringProxy
from tests.helpers import HttpServer


def test_filtering_proxy_allows_listed_host():
    with HttpServer() as upstream:
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
    with HttpServer() as upstream:
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
    with HttpServer() as upstream:
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


def _proxy_address(proxy_url: str) -> tuple[str, int]:
    host_port = proxy_url.removeprefix("http://")
    host, port = host_port.rsplit(":", 1)
    return host, int(port)
