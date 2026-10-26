"""Stub HTTP server that serves pre-recorded engine responses during conformance tests.

Implements the replayer-contract defined in vendor/spec/replayer-contract/v1/README.md.

Lookup algorithm: sha256(normalizedPath + "|" + stableJson(body)).
A miss returns HTTP 404 with envelope: {error, message, path, bodyHash}.
"""

from __future__ import annotations

import hashlib
import http.server
import json
import threading
from typing import Any


def _stable_json(v: Any) -> str:
    """Serialize v as compact JSON with all object keys sorted lexicographically.

    Arrays are not reordered; only object keys are sorted. This is the
    canonical form used for the replayer lookup key.
    """
    if isinstance(v, dict):
        return (
            "{"
            + ",".join(
                json.dumps(k) + ":" + _stable_json(v[k]) for k in sorted(v.keys())
            )
            + "}"
        )
    if isinstance(v, list):
        return "[" + ",".join(_stable_json(item) for item in v) + "]"
    return json.dumps(v, separators=(",", ":"), ensure_ascii=False)


def recording_key(path: str, body: Any) -> str:
    """Compute the replayer lookup key for a given path and body.

    Exported so the conformance test can pre-populate the recording map
    with entries that match the requests the SDK will send.
    """
    normalized = path.rstrip("/")
    stable = _stable_json(body)
    return hashlib.sha256((normalized + "|" + stable).encode()).hexdigest()


class _Handler(http.server.BaseHTTPRequestHandler):
    """HTTP request handler that looks up incoming requests in a recording map."""

    server: "_ReplayerServer"

    def do_POST(self) -> None:
        """Handle POST requests by looking up the recording and returning the response."""
        path = self.path.rstrip("/")
        length = int(self.headers.get("content-length", "0"))
        raw = self.rfile.read(length) if length > 0 else b"{}"

        try:
            body = json.loads(raw)
        except json.JSONDecodeError as exc:
            self._send_json(400, {"error": "invalid_json", "message": str(exc)})
            return

        body_hash = hashlib.sha256(_stable_json(body).encode()).hexdigest()
        key = recording_key(path, body)

        rec = self.server.recordings.get(key)
        if rec is None:
            self._send_json(
                404,
                {
                    "error": "no_recording",
                    "message": f"no recording for path={path}",
                    "path": path,
                    "bodyHash": body_hash,
                },
            )
            return

        response_bytes = json.dumps(rec["body"], separators=(",", ":")).encode()
        self.send_response(rec["statusCode"])
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response_bytes)))
        self.end_headers()
        self.wfile.write(response_bytes)

    def _send_json(self, status: int, data: dict) -> None:
        """Send a JSON response with the given status code."""
        body = json.dumps(data, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: Any) -> None:
        """Suppress default access log output."""


class _ReplayerServer(http.server.HTTPServer):
    """HTTPServer subclass that carries the recordings map."""

    recordings: dict[str, dict]


class Replayer:
    """Stub HTTP server backed by a recording map.

    Keys in the recording map are sha256(normalizedPath + "|" + stableJSON(body)).
    Each value is a dict with ``statusCode`` (int) and ``body`` (any JSON-serializable value).

    Usage::

        r = Replayer(recordings)
        try:
            # use r.url as the base URL for HTTP calls
        finally:
            r.close()
    """

    def __init__(self, recordings: dict[str, dict]) -> None:
        """Start the replayer on an ephemeral port and serve in a daemon thread."""
        server = _ReplayerServer(("127.0.0.1", 0), _Handler)
        server.recordings = recordings
        self._server = server
        self._thread = threading.Thread(target=server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def url(self) -> str:
        """Base URL of the running server (e.g. 'http://127.0.0.1:NNNNN')."""
        host, port = self._server.server_address
        return f"http://{host}:{port}"

    def close(self) -> None:
        """Shut down the HTTP server and wait for the serving thread to exit."""
        self._server.shutdown()
        self._thread.join()
        # shutdown() stops the serve loop; the listening socket is released
        # only by server_close(), and the suite creates one replayer per case.
        self._server.server_close()
