"""One whole-request deadline for every exchange the client makes.

httpx times each phase of a request on its own (pool, connect, write, read),
and a read timeout restarts with every chunk that arrives. A server that
trickles its headers or its body a byte at a time therefore keeps a request
open indefinitely. The client's ``timeout`` is a deadline for the whole
exchange instead.

:func:`request_deadline` opens the deadline around one request.
:func:`deadline_transport` builds the transport each client sends through. It
holds every network wait to the time left on the open deadline: the pool
wait, each connection attempt, the TLS handshake, each socket write and each
socket read. Once the deadline passes, the next wait is refused rather than
started, so the request fails within its budget with an httpx timeout.

Two waits escape it. A blocking ``getaddrinfo`` is bounded by the operating
system's resolver configuration, not by the request. And through an
``https://`` proxy to an ``https://`` server, httpcore reads the inner TLS
layer in a loop that hands each socket read the time left when the loop
began, so a server trickling one TLS record can stretch a single read past
the deadline, though never indefinitely.
"""

from __future__ import annotations

import ipaddress
import socket
import ssl
import threading
import urllib.request
from contextlib import contextmanager
from contextvars import ContextVar
from time import monotonic
from typing import Any, Iterable, Iterator, Optional, Tuple

import httpcore
import httpx

# The monotonic time the open request must finish by, or None outside one.
# A request runs in its caller's thread, so a context variable keeps the
# deadlines of concurrent requests on one client apart.
_EXPIRES_AT: ContextVar[Optional[float]] = ContextVar(
    "ezclassify_request_deadline", default=None
)

# A TLS write goes out in pieces of one TLS record, each sent whole within
# the time left when it starts.
_WRITE_PIECE = 16 * 1024

# The same pool limits a default httpx client uses.
_MAX_CONNECTIONS = 100
_MAX_KEEPALIVE_CONNECTIONS = 20
_KEEPALIVE_EXPIRY = 5.0


def require_timeout(timeout: Any) -> float:
    """Check a client ``timeout`` and return it as seconds.

    The upper limit is the longest wait the platform's clocks can hold
    (``threading.TIMEOUT_MAX``, several centuries); a longer one would fail on
    every request instead of here.

    Raises:
        TypeError: ``timeout`` is not a number. ``None`` is refused here
            because it would leave a request without any deadline.
        ValueError: ``timeout`` is not positive and finite.
    """
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        raise TypeError(
            f"timeout must be a number of seconds, not {type(timeout).__name__}"
        )
    # Comparing, rather than converting first, keeps an enormous int from
    # overflowing; NaN fails both comparisons.
    if not 0 < timeout <= threading.TIMEOUT_MAX:
        raise ValueError(f"timeout must be positive and finite, not {timeout!r}")
    return float(timeout)


@contextmanager
def request_deadline(seconds: float) -> Iterator[None]:
    """Hold every network wait inside the block to ``seconds`` from now."""
    token = _EXPIRES_AT.set(monotonic() + seconds)
    try:
        yield
    finally:
        _EXPIRES_AT.reset(token)


def _remaining(timeout: Optional[float], expired: type[Exception]) -> Optional[float]:
    """Shorten one wait to the time left on the open deadline.

    Raises:
        expired: The deadline has already passed, so the wait is not started.
    """
    expires_at = _EXPIRES_AT.get()
    if expires_at is None:
        return timeout
    left = expires_at - monotonic()
    if left <= 0:
        raise expired("the request deadline passed")
    return left if timeout is None else min(timeout, left)


class _DeadlineStream(httpcore.NetworkStream):
    """A connection whose every wait ends by the deadline of the request using it.

    The deadline is read at each call, not when the connection opened, because
    a pooled connection serves one request after another.
    """

    def __init__(self, stream: httpcore.NetworkStream) -> None:
        self._stream = stream

    def read(self, max_bytes: int, timeout: Optional[float] = None) -> bytes:
        return self._stream.read(max_bytes, _remaining(timeout, httpcore.ReadTimeout))

    def write(self, buffer: bytes, timeout: Optional[float] = None) -> None:
        # The wrapped stream retries a partial send with the timeout it was
        # given, so a peer that drains slowly could keep one write going past
        # the deadline. A plain socket's sendall holds its timeout over the
        # whole call; over TLS, each record-sized piece is one write that does.
        if not buffer:
            return
        if self._stream.get_extra_info("ssl_object") is None:
            sock = self._stream.get_extra_info("socket")
            try:
                sock.settimeout(_remaining(timeout, httpcore.WriteTimeout))
                sock.sendall(buffer)
            except TimeoutError as exc:
                raise httpcore.WriteTimeout(str(exc)) from exc
            except OSError as exc:
                raise httpcore.WriteError(str(exc)) from exc
            return
        view = memoryview(buffer)
        for start in range(0, len(view), _WRITE_PIECE):
            self._stream.write(
                bytes(view[start : start + _WRITE_PIECE]),
                _remaining(timeout, httpcore.WriteTimeout),
            )

    def close(self) -> None:
        self._stream.close()

    def start_tls(
        self,
        ssl_context: ssl.SSLContext,
        server_hostname: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> httpcore.NetworkStream:
        return _DeadlineStream(
            self._stream.start_tls(
                ssl_context,
                server_hostname,
                _remaining(timeout, httpcore.ConnectTimeout),
            )
        )

    def get_extra_info(self, info: str) -> Any:
        return self._stream.get_extra_info(info)


class _DeadlineBackend(httpcore.NetworkBackend):
    """Opens connections whose waits end by the open request's deadline."""

    def __init__(self) -> None:
        self._backend = httpcore.SyncBackend()

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: Optional[float] = None,
        local_address: Optional[str] = None,
        socket_options: Optional[Iterable[httpcore.SOCKET_OPTION]] = None,
    ) -> httpcore.NetworkStream:
        # Resolving here and connecting to one address at a time gives every
        # attempt only the time that is left. Handing the host name down would
        # give each of its addresses the whole timeout.
        try:
            addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except OSError as exc:
            raise httpcore.ConnectError(str(exc)) from exc
        failure: Exception = httpcore.ConnectError(f"no address found for {host}")
        for *_, address in addresses:
            try:
                stream = self._backend.connect_tcp(
                    str(address[0]),
                    port,
                    _remaining(timeout, httpcore.ConnectTimeout),
                    local_address,
                    socket_options,
                )
            except httpcore.ConnectError as exc:
                failure = exc
                continue
            return _DeadlineStream(stream)
        raise failure


# httpcore raises its own exceptions; httpx callers expect httpx's. Most
# specific first, because the first match wins.
_HTTPX_ERRORS: Tuple[Tuple[type[Exception], type[httpx.TransportError]], ...] = (
    (httpcore.PoolTimeout, httpx.PoolTimeout),
    (httpcore.ConnectTimeout, httpx.ConnectTimeout),
    (httpcore.ReadTimeout, httpx.ReadTimeout),
    (httpcore.WriteTimeout, httpx.WriteTimeout),
    (httpcore.TimeoutException, httpx.TimeoutException),
    (httpcore.ConnectError, httpx.ConnectError),
    (httpcore.ReadError, httpx.ReadError),
    (httpcore.WriteError, httpx.WriteError),
    (httpcore.NetworkError, httpx.NetworkError),
    (httpcore.ProxyError, httpx.ProxyError),
    (httpcore.UnsupportedProtocol, httpx.UnsupportedProtocol),
    (httpcore.LocalProtocolError, httpx.LocalProtocolError),
    (httpcore.RemoteProtocolError, httpx.RemoteProtocolError),
    (httpcore.ProtocolError, httpx.ProtocolError),
)


@contextmanager
def _httpx_errors() -> Iterator[None]:
    try:
        yield
    except Exception as exc:
        for source, target in _HTTPX_ERRORS:
            if isinstance(exc, source):
                raise target(str(exc)) from exc
        raise


class _ResponseStream(httpx.SyncByteStream):
    def __init__(self, stream: Iterable[bytes]) -> None:
        self._stream = stream

    def __iter__(self) -> Iterator[bytes]:
        with _httpx_errors():
            yield from self._stream

    def close(self) -> None:
        close = getattr(self._stream, "close", None)
        if close is not None:
            close()


class _DeadlineTransport(httpx.BaseTransport):
    """An httpx transport over a connection pool built on :class:`_DeadlineBackend`."""

    def __init__(self, pool: httpcore.ConnectionPool) -> None:
        self._pool = pool

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        if not isinstance(request.stream, httpx.SyncByteStream):
            raise TypeError("a synchronous transport needs a synchronous request body")
        with _httpx_errors():
            # The pool waits for a free connection with the timeout it finds
            # here, before any network wait could shorten it.
            timeouts = dict(request.extensions.get("timeout", {}))
            timeouts["pool"] = _remaining(timeouts.get("pool"), httpcore.PoolTimeout)
            response = self._pool.handle_request(
                httpcore.Request(
                    method=request.method,
                    url=httpcore.URL(
                        scheme=request.url.raw_scheme,
                        host=request.url.raw_host,
                        port=request.url.port,
                        target=request.url.raw_path,
                    ),
                    headers=request.headers.raw,
                    content=request.stream,
                    extensions={**request.extensions, "timeout": timeouts},
                )
            )
        if not isinstance(response.stream, Iterable):
            raise TypeError("a synchronous pool answered with an asynchronous body")
        return httpx.Response(
            status_code=response.status,
            headers=response.headers,
            stream=_ResponseStream(response.stream),
            extensions=response.extensions,
        )

    def close(self) -> None:
        with _httpx_errors():
            self._pool.close()


def _environment_proxy(url: httpx.URL) -> Optional[httpx.Proxy]:
    """Find the proxy the environment names for ``url``, as httpx would.

    A client given its own transport no longer reads ``HTTP_PROXY``,
    ``HTTPS_PROXY``, ``ALL_PROXY`` and ``NO_PROXY`` itself, so the transport
    does. Each client talks to one origin, so one answer serves it.
    """
    proxies = urllib.request.getproxies()
    address = proxies.get(url.scheme) or proxies.get("all")
    if not address or _exempt(url, proxies.get("no", "")):
        return None
    return httpx.Proxy(address if "://" in address else f"http://{address}")


def _exempt(url: httpx.URL, no_proxy: str) -> bool:
    """Say whether ``NO_PROXY`` exempts ``url``, reading it the way httpx does.

    Each comma-separated entry is ``*`` for every host; a URL such as
    ``http://host:port``; an IP address or ``localhost``, for that host only;
    or a domain, for the domain and its subdomains (``.domain`` for its
    subdomains only). A port written in an entry has to match as well.
    """
    for entry in (item.strip() for item in no_proxy.split(",")):
        if entry == "*":
            return True
        if entry and _covers(_no_proxy_pattern(entry), url):
            return True
    return False


def _no_proxy_pattern(entry: str) -> httpx.URL:
    if "://" in entry:
        return httpx.URL(entry)
    address = entry.split("/")[0]
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        if entry.lower() == "localhost":
            return httpx.URL(f"all://{entry}")
        return httpx.URL(f"all://*{entry}")
    return httpx.URL(f"all://[{ip}]" if ip.version == 6 else f"all://{ip}")


def _covers(pattern: httpx.URL, url: httpx.URL) -> bool:
    if pattern.scheme != "all" and pattern.scheme != url.scheme:
        return False
    if pattern.port is not None and pattern.port != url.port:
        return False
    host = pattern.host
    if not host or host == "*":
        return True
    if host.startswith("*."):
        return url.host.endswith(host[1:]) and len(url.host) > len(host) - 1
    if host.startswith("*"):
        return url.host == host[1:] or url.host.endswith("." + host[1:])
    return url.host == host


def deadline_transport(base_url: str) -> httpx.BaseTransport:
    """Build the transport a client for ``base_url`` sends every request through.

    It verifies TLS and honors the proxy and certificate environment variables
    as a default httpx client does; the difference is that its connections
    keep to the deadline :func:`request_deadline` opens.

    Raises:
        ValueError: The environment names a proxy whose scheme httpx does not
            support (``httpx.Proxy`` refuses it).
    """
    ssl_context = httpx.create_ssl_context()
    backend = _DeadlineBackend()
    proxy = _environment_proxy(httpx.URL(base_url))
    if proxy is None:
        return _DeadlineTransport(
            httpcore.ConnectionPool(
                ssl_context=ssl_context,
                network_backend=backend,
                max_connections=_MAX_CONNECTIONS,
                max_keepalive_connections=_MAX_KEEPALIVE_CONNECTIONS,
                keepalive_expiry=_KEEPALIVE_EXPIRY,
            )
        )
    proxy_url = httpcore.URL(
        scheme=proxy.url.raw_scheme,
        host=proxy.url.raw_host,
        port=proxy.url.port,
        target=proxy.url.raw_path,
    )
    if proxy.url.scheme in ("http", "https"):
        return _DeadlineTransport(
            httpcore.HTTPProxy(
                proxy_url=proxy_url,
                proxy_auth=proxy.raw_auth,
                proxy_headers=proxy.headers.raw,
                ssl_context=ssl_context,
                network_backend=backend,
                max_connections=_MAX_CONNECTIONS,
                max_keepalive_connections=_MAX_KEEPALIVE_CONNECTIONS,
                keepalive_expiry=_KEEPALIVE_EXPIRY,
            )
        )
    # httpx.Proxy accepts only http, https, socks5 and socks5h.
    return _DeadlineTransport(
        httpcore.SOCKSProxy(
            proxy_url=proxy_url,
            proxy_auth=proxy.raw_auth,
            ssl_context=ssl_context,
            network_backend=backend,
            max_connections=_MAX_CONNECTIONS,
            max_keepalive_connections=_MAX_KEEPALIVE_CONNECTIONS,
            keepalive_expiry=_KEEPALIVE_EXPIRY,
        )
    )
