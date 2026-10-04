from __future__ import annotations

import json

import httpcore
import httpx
import pytest
from leanwarp_cloud import LeanWarpCloud, OperationTimeout
from leanwarp_cloud import transport as deadline_transport
from leanwarp_cloud.transport import DeadlineTransport


class HeaderStream(httpcore.NetworkStream):
    """One header byte per read, through the real HTTP/1.1 parser."""

    def __init__(self, clock):
        body = json.dumps({"operation_id": "op-1", "state": "running", "result": None}).encode()
        headers = f"HTTP/1.1 200 OK\r\nContent-Length: {len(body)}\r\n\r\n".encode()
        self.parts = [bytes([byte]) for byte in headers] + [body]
        self.clock, self.reads, self.closed = clock, [], False

    def read(self, max_bytes, timeout=None):
        self.reads.append((self.clock[0], timeout))
        self.clock[0] += min(1, timeout) if timeout is not None else 1
        if timeout is not None and timeout <= 1:
            raise httpcore.ReadTimeout("bounded read")
        return self.parts.pop(0) if self.parts else b""

    def write(self, buffer, timeout=None):
        pass

    def close(self):
        self.closed = True


class Backend(httpcore.NetworkBackend):
    def __init__(self, stream):
        self.stream = stream

    def connect_tcp(self, *args, **kwargs):
        return self.stream


def test_trickling_headers_stop_at_poll_deadline_and_close_connection(monkeypatch):
    clock = [1000.0]
    stream = HeaderStream(clock)
    monkeypatch.setattr(deadline_transport.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        deadline_transport.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds)
    )
    monkeypatch.setattr(deadline_transport, "getproxies", dict)
    with (
        LeanWarpCloud(
            "test",
            base_url="http://127.0.0.1",
            retries=0,
            transport=DeadlineTransport("http://127.0.0.1", network_backend=Backend(stream)),
        ) as cloud,
        pytest.raises(OperationTimeout),
    ):
        cloud.wait("op-1", timeout=20)
    assert clock[0] == pytest.approx(1020)
    assert all(start < 1020 for start, _ in stream.reads)
    assert stream.closed


def test_core_body_errors_keep_httpx_contract_and_close(monkeypatch):
    monkeypatch.setattr(deadline_transport, "getproxies", dict)

    class BrokenBody(HeaderStream):
        def read(self, max_bytes, timeout=None):
            if self.parts:
                self.parts = []
                return b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n"
            raise httpcore.ReadError("body failed")

    stream = BrokenBody([0])
    with (
        LeanWarpCloud(
            "test",
            base_url="http://127.0.0.1",
            retries=0,
            transport=DeadlineTransport("http://127.0.0.1", network_backend=Backend(stream)),
        ) as cloud,
        pytest.raises(httpx.ReadError),
    ):
        cloud.versions()
    assert stream.closed


def test_default_transport_preserves_https_proxy_auth_and_bypass(monkeypatch):
    monkeypatch.setattr(
        deadline_transport,
        "getproxies",
        lambda: {"https": "http://user:pass@proxy.example:8080", "no": "direct.example"},
    )
    calls = []
    monkeypatch.setattr(
        deadline_transport.httpcore, "HTTPProxy", lambda **kwargs: calls.append(kwargs)
    )
    DeadlineTransport("https://api.example")
    assert calls[0]["proxy_url"] == "http://proxy.example:8080"
    assert calls[0]["proxy_auth"] == (b"user", b"pass")
    with DeadlineTransport("https://direct.example") as direct:
        assert isinstance(direct.pool, httpcore.ConnectionPool)
    assert len(calls) == 1


def test_pooled_connection_uses_new_request_deadline(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(deadline_transport.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(deadline_transport, "getproxies", dict)

    class ReusableStream(HeaderStream):
        def write(self, buffer, timeout=None):
            if buffer.startswith(b"GET "):
                body = b'{"operation_id":"op-1","state":"completed"}'
                self.parts = [
                    f"HTTP/1.1 200 OK\r\nContent-Length: {len(body)}\r\n\r\n".encode() + body
                ]

        def read(self, max_bytes, timeout=None):
            clock[0] += 0.1
            return self.parts.pop(0)

        def get_extra_info(self, name):
            return False if name == "is_readable" else None

    class CountingBackend(Backend):
        count = 0

        def connect_tcp(self, *args, **kwargs):
            self.count += 1
            return super().connect_tcp(*args, **kwargs)

    backend = CountingBackend(ReusableStream(clock))
    with LeanWarpCloud(
        "test",
        base_url="http://127.0.0.1",
        retries=0,
        transport=DeadlineTransport("http://127.0.0.1", network_backend=backend),
    ) as cloud:
        assert cloud.operation("op-1", deadline=1000.5)["state"] == "completed"
        clock[0] = 1001.0
        assert cloud.operation("op-1", deadline=1006.0)["state"] == "completed"
    assert backend.count == 1


@pytest.mark.parametrize(
    ("origin", "bypass", "direct"),
    [
        ("http://localhost:7777", "localhost:7777", True),
        ("http://localhost:8888", "localhost:7777", False),
        ("https://api.example", "https://api.example", True),
        ("https://api.example", "http://api.example", False),
        ("https://api.example", "example", True),
        ("https://example", ".example", False),
        ("https://api.example", ".example", True),
        ("http://127.0.0.1", "127.0.0.1", True),
        ("http://[::1]", "::1", True),
        ("https://api.example", "*", True),
        ("https://api.example", "all://*", True),
        ("https://api.example", "https://*", True),
        ("https://api.example", "http://*", False),
    ],
)
def test_environment_proxy_normalization_and_full_origin_bypass(
    monkeypatch, origin, bypass, direct
):
    monkeypatch.setattr(
        deadline_transport, "getproxies", lambda: {"all": "proxy.example:8080", "no": bypass}
    )
    assert deadline_transport._environment_proxy(httpx.URL(origin)) == (
        None if direct else "http://proxy.example:8080"
    )
