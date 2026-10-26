"""Every call finishes within the client's timeout, however the server behaves.

httpx times each phase of a request separately, and its read timeout restarts
with each chunk that arrives, so these tests run against a real socket server
that misbehaves on purpose: it trickles its headers, trickles its body, or
goes silent after the headers. Each one would hold a call for the full
misbehavior (seconds) if only the per-phase timeouts applied.
"""

from __future__ import annotations

import json
import math
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Iterator

import httpx
import pytest

from ezclassify_rest_client import (
    EzClassifyClient,
    EzClassifyClientError,
    EzClassifyContext,
    RequestTimeoutError,
    connect_context,
)

# The client's budget, and how far past it a call may finish: thread
# scheduling and socket teardown, not another read.
BUDGET = 0.5
SLACK = 0.4
# How long the server keeps misbehaving before it gives up. Long against the
# budget, so a call that is not held to its deadline is plainly late.
MISBEHAVE_FOR = 3.0
TRICKLE_EVERY = 0.05

_DESCRIPTION = {
    "id": "ctx",
    "version": "1.0.0",
    "scheme": {"id": "levels", "version": "1.0.0", "resolvedVersion": "1.0.0"},
    "schemes": [
        {"selector": "levels", "id": "levels", "version": "1.0.0", "roles": []}
    ],
}

Behavior = Callable[[socket.socket, bytes, threading.Event], None]


def _answer(conn: socket.socket, body: dict, delay: float = 0.0) -> None:
    payload = json.dumps(body).encode()
    time.sleep(delay)
    conn.sendall(
        b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
        + f"Content-Length: {len(payload)}\r\n\r\n".encode()
        + payload
    )


def _trickle(conn: socket.socket, stop: threading.Event, data: bytes) -> None:
    """Send ``data`` a byte at a time, each well inside a per-phase timeout."""
    deadline = time.monotonic() + MISBEHAVE_FOR
    for byte in data:
        if stop.is_set() or time.monotonic() > deadline:
            return
        conn.sendall(bytes([byte]))
        time.sleep(TRICKLE_EVERY)


def trickle_body(conn: socket.socket, request: bytes, stop: threading.Event) -> None:
    conn.sendall(
        b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
        b"Content-Length: 4096\r\n\r\n"
    )
    _trickle(conn, stop, b'{"contexts": ["' + b"a" * 4000)


def trickle_headers(conn: socket.socket, request: bytes, stop: threading.Event) -> None:
    _trickle(conn, stop, b"HTTP/1.1 200 OK\r\nX-Padding: " + b"a" * 4000)


def silent_after_headers(
    conn: socket.socket, request: bytes, stop: threading.Event
) -> None:
    conn.sendall(
        b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
        b"Content-Length: 64\r\n\r\n"
    )
    stop.wait(MISBEHAVE_FOR)


def drain_slowly(conn: socket.socket, request: bytes, stop: threading.Event) -> None:
    """Take the request a slice at a time and never answer.

    Each slice frees enough of the window for the client's next send to go
    out well inside a per-phase timeout, so only a deadline over the whole
    write stops it.
    """
    deadline = time.monotonic() + MISBEHAVE_FOR
    while not stop.is_set() and time.monotonic() < deadline:
        if not conn.recv(256 * 1024):
            return
        time.sleep(TRICKLE_EVERY)


def describe_then_trickle(
    conn: socket.socket, request: bytes, stop: threading.Event
) -> None:
    """Answer the Context description, then trickle every operation."""
    if request.startswith(b"GET "):
        _answer(conn, _DESCRIPTION)
    else:
        trickle_body(conn, request, stop)


class Server:
    """A local HTTP/1.1 server that answers each request with one behavior."""

    def __init__(self, behavior: Behavior, *, read_requests: bool = True) -> None:
        self._behavior = behavior
        self._read_requests = read_requests
        self._stop = threading.Event()
        self._listener = socket.create_server(("127.0.0.1", 0))
        self._listener.settimeout(0.1)
        self._threads: list[threading.Thread] = []
        self.requests: list[bytes] = []
        self.url = f"http://127.0.0.1:{self._listener.getsockname()[1]}"
        self._accept = threading.Thread(target=self._serve, daemon=True)
        self._accept.start()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            thread = threading.Thread(target=self._handle, args=(conn,), daemon=True)
            thread.start()
            self._threads.append(thread)

    def _handle(self, conn: socket.socket) -> None:
        with conn:
            conn.settimeout(MISBEHAVE_FOR)
            try:
                if not self._read_requests:
                    self._behavior(conn, b"", self._stop)
                    return
                while not self._stop.is_set():
                    request = self._read_request(conn)
                    if request is None:
                        return
                    self.requests.append(request)
                    self._behavior(conn, request, self._stop)
            except OSError:
                return

    @staticmethod
    def _read_request(conn: socket.socket) -> bytes | None:
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = conn.recv(65536)
            if not chunk:
                return None
            data += chunk
        head, _, body = data.partition(b"\r\n\r\n")
        for line in head.split(b"\r\n"):
            name, _, value = line.partition(b":")
            if name.strip().lower() == b"content-length":
                while len(body) < int(value):
                    chunk = conn.recv(65536)
                    if not chunk:
                        return None
                    body += chunk
        return data

    def close(self) -> None:
        self._stop.set()
        self._listener.close()
        self._accept.join(2)
        for thread in self._threads:
            thread.join(MISBEHAVE_FOR + 1)


@pytest.fixture
def serve() -> Iterator[Callable[..., Server]]:
    servers: list[Server] = []

    def start(behavior: Behavior, *, read_requests: bool = True) -> Server:
        server = Server(behavior, read_requests=read_requests)
        servers.append(server)
        return server

    yield start
    for server in servers:
        server.close()


def _elapsed(call: Callable[[], object]) -> tuple[BaseException, float]:
    started = time.monotonic()
    with pytest.raises(BaseException) as raised:
        call()
    return raised.value, time.monotonic() - started


def _assert_timed_out(error: BaseException, elapsed: float) -> None:
    assert isinstance(error, RequestTimeoutError), repr(error)
    assert BUDGET - 0.05 <= elapsed < BUDGET + SLACK, elapsed


class TestTheDeadlineCoversTheWholeExchange:
    def test_a_trickled_body_times_out_within_the_budget(self, serve):
        server = serve(trickle_body)
        with EzClassifyClient(server.url, timeout=BUDGET) as client:
            _assert_timed_out(*_elapsed(client.list_contexts))

    def test_trickled_headers_time_out_within_the_budget(self, serve):
        server = serve(trickle_headers)
        with EzClassifyClient(server.url, timeout=BUDGET) as client:
            _assert_timed_out(*_elapsed(client.ping))

    def test_a_stall_after_the_headers_is_reported_as_a_timeout(self, serve):
        server = serve(silent_after_headers)
        with EzClassifyClient(server.url, timeout=BUDGET) as client:
            _assert_timed_out(*_elapsed(lambda: client.list_context_versions("ctx")))

    def test_connecting_to_a_context_times_out_within_the_budget(self, serve):
        server = serve(trickle_body)
        error, elapsed = _elapsed(
            lambda: connect_context(server.url, context_id="ctx", timeout=BUDGET)
        )
        _assert_timed_out(error, elapsed)

    def test_a_context_operation_times_out_within_the_budget(self, serve):
        server = serve(describe_then_trickle)
        with connect_context(
            server.url, context_id="ctx", context_version="1.0.0", timeout=BUDGET
        ) as ctx:
            _assert_timed_out(*_elapsed(lambda: ctx.scheme.validate("SECRET")))

    def test_a_request_the_server_drains_slowly_times_out_within_the_budget(
        self, serve
    ):
        # Far more than the socket buffers hold, so sending it waits on the
        # server, which keeps taking a little at a time.
        server = serve(drain_slowly, read_requests=False)
        with EzClassifyContext(
            server.url, context_id="ctx", context_version="1.0.0", timeout=BUDGET
        ) as ctx:
            text = "S" * (32 * 1024 * 1024)
            _assert_timed_out(*_elapsed(lambda: ctx.scheme.validate(text)))

    def test_callers_joined_on_one_request_all_finish_within_the_budget(self, serve):
        server = serve(describe_then_trickle)
        with connect_context(
            server.url, context_id="ctx", context_version="1.0.0", timeout=BUDGET
        ) as ctx:
            with ThreadPoolExecutor(3) as pool:
                outcomes = list(
                    pool.map(
                        lambda _: _elapsed(lambda: ctx.scheme.validate("SECRET")),
                        range(3),
                    )
                )
        for error, elapsed in outcomes:
            _assert_timed_out(error, elapsed)
        assert sum(r.startswith(b"POST ") for r in server.requests) == 1

    def test_each_request_gets_its_own_budget(self, serve):
        # Each answer takes most of the budget, so a deadline that carried
        # over to the next request on the pooled connection would fail it.
        server = serve(lambda conn, request, stop: _answer(conn, {"contexts": []}, 0.3))
        with EzClassifyClient(server.url, timeout=BUDGET) as client:
            assert client.list_contexts() == []
            assert client.list_contexts() == []
        assert len(server.requests) == 2


class TestTheTimeoutError:
    def test_it_is_a_client_error_and_a_builtin_timeout(self, serve):
        server = serve(silent_after_headers)
        with EzClassifyClient(server.url, timeout=BUDGET) as client:
            with pytest.raises(TimeoutError) as raised:
                client.list_contexts()
        assert isinstance(raised.value, EzClassifyClientError)
        assert raised.value.status_code == 0
        assert "/v1/ctx" in str(raised.value)
        assert "0.5s" in str(raised.value)

    def test_a_refused_connection_is_not_a_timeout(self):
        listener = socket.create_server(("127.0.0.1", 0))
        url = f"http://127.0.0.1:{listener.getsockname()[1]}"
        listener.close()
        with EzClassifyClient(url, timeout=BUDGET) as client:
            with pytest.raises(EzClassifyClientError) as raised:
                client.list_contexts()
        assert not isinstance(raised.value, RequestTimeoutError)


class TestAnUnboundedTimeoutIsRefused:
    UNBOUNDED = [0, 0.0, -1, -0.5, math.inf, -math.inf, math.nan, 1e10, 10**400]
    NOT_A_NUMBER = [None, True, "30", b"30"]

    @pytest.mark.parametrize("timeout", UNBOUNDED)
    def test_a_value_that_is_not_positive_and_finite(self, timeout):
        for build in _builders():
            with pytest.raises(ValueError, match="timeout"):
                build(timeout)

    @pytest.mark.parametrize("timeout", NOT_A_NUMBER)
    def test_a_value_that_is_not_a_number(self, timeout):
        for build in _builders():
            with pytest.raises(TypeError, match="timeout"):
                build(timeout)

    def test_a_whole_number_of_seconds_is_accepted(self):
        EzClassifyClient("http://127.0.0.1:9", timeout=5).close()
        EzClassifyContext("http://127.0.0.1:9", context_id="ctx", timeout=5).close()


def _builders() -> list[Callable[[object], object]]:
    """Every public way to set a timeout, refused before any request is sent."""
    url = "http://127.0.0.1:9"
    return [
        lambda timeout: EzClassifyClient(url, timeout=timeout),  # type: ignore[arg-type]
        lambda timeout: EzClassifyContext(url, context_id="ctx", timeout=timeout),  # type: ignore[arg-type]
        lambda timeout: connect_context(url, context_id="ctx", timeout=timeout),  # type: ignore[arg-type]
    ]


_PROXY_VARIABLES = [
    f"{name}{suffix}"
    for name in ("http", "https", "all", "no")
    for suffix in ("_proxy", "_PROXY")
]


@pytest.fixture
def proxy_environment(monkeypatch) -> Callable[..., None]:
    """Replace every proxy variable with exactly the ones a test names."""
    for name in _PROXY_VARIABLES:
        monkeypatch.delenv(name, raising=False)

    def use(**variables: str) -> None:
        for name, value in variables.items():
            monkeypatch.setenv(name, value)

    return use


class TestTheEnvironmentProxy:
    def test_a_proxy_named_in_the_environment_carries_the_request(
        self, serve, proxy_environment
    ):
        proxy = serve(lambda conn, request, stop: _answer(conn, {"contexts": ["p"]}))
        proxy_environment(HTTP_PROXY=proxy.url)
        with EzClassifyClient("http://ezclassify.invalid", timeout=BUDGET) as client:
            assert client.list_contexts() == ["p"]
        assert proxy.requests[0].startswith(b"GET http://ezclassify.invalid/v1/ctx ")

    def test_no_proxy_sends_the_request_direct(self, serve, proxy_environment):
        server = serve(lambda conn, request, stop: _answer(conn, {"contexts": ["d"]}))
        proxy_environment(HTTP_PROXY="http://127.0.0.1:9", NO_PROXY="127.0.0.1")
        with EzClassifyClient(server.url, timeout=BUDGET) as client:
            assert client.list_contexts() == ["d"]
        assert server.requests[0].startswith(b"GET /v1/ctx ")

    @pytest.mark.parametrize(
        "no_proxy",
        [
            "",
            "*",
            "api.example.com",
            "example.com",
            ".example.com",
            ".api.example.com",
            "xample.com",
            "api.example.com:8080",
            "api.example.com:9090",
            "http://api.example.com",
            "https://api.example.com",
            "http://api.example.com:8080",
            "all://*example.com",
            "other.org, example.com",
            "localhost",
            "10.0.0.1",
            "::1",
        ],
    )
    @pytest.mark.parametrize(
        "base_url",
        [
            "http://api.example.com:8080",
            "https://api.example.com",
            "http://localhost:3000",
            "http://10.0.0.1",
            "http://[::1]:8080",
        ],
    )
    def test_the_proxy_chosen_is_the_one_httpx_would_choose(
        self, proxy_environment, no_proxy, base_url
    ):
        from ezclassify_rest_client._deadline import _environment_proxy

        proxy_environment(
            HTTP_PROXY="http://proxy.invalid:3128",
            HTTPS_PROXY="http://secure-proxy.invalid:3128",
            NO_PROXY=no_proxy,
        )
        url = httpx.URL(base_url)
        with httpx.Client() as reference:
            chosen = reference._transport_for_url(url)
            expected = None
            for pattern, transport in reference._mounts.items():
                if transport is chosen and transport is not None:
                    expected = transport._pool._proxy_url.host.decode()
        proxy = _environment_proxy(url)

        assert (proxy.url.host if proxy else None) == expected
