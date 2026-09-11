"""A minimal Chrome DevTools Protocol client, on the standard library alone.

WHY NOT A LIBRARY. This agent has to come up on a freshly-flashed Raspberry Pi
with nothing but the OS image. Debian enforces PEP 668, so a dependency means a
venv, a wheel build for ARM, and a provisioning step that can fail on a host
with no console attached — for a wall whose only recovery path is SSH. Every
dependency is therefore a way the fleet goes dark during an upgrade. The whole
protocol surface this agent needs is six commands, and the transport under them
is one WebSocket frame codec, so the codec is written here and unit-tested
rather than installed.

WHY CDP AT ALL, RATHER THAN RESTARTING CHROMIUM ON EACH NAVIGATION. Pointing a
wall at a new board by restarting the browser costs a black screen, a fresh
profile load and the loss of any session the page held. CDP's ``Page.navigate``
moves the tab in place, which is what "load a URL" has to mean for a surface
somebody is looking at. The DevTools HTTP endpoints alone are not enough:
``/json/list`` reads targets, but navigating an EXISTING target is a WebSocket
command and there is no HTTP equivalent.

THE HTTP HALF IS STILL USED FOR DISCOVERY, and reading it is not optional.
``webSocketDebuggerUrl`` is minted per target and changes whenever the target
does, so a cached socket URL is a socket URL that points at a tab which no
longer exists — reconnecting per call is the correct trade for an agent that
issues a handful of commands a minute.

READ THE TARGET LIST AS JSON, NEVER AS TEXT. Its key ORDER is not a contract; a
pattern that assumes ``type`` precedes ``url`` reads the wrong field the day
Chromium reorders them, and reads it confidently.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import secrets
import socket
import struct
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

_LOGGER = logging.getLogger(__name__)

# Frame opcodes we handle. Anything else closes the connection rather than
# being skipped: an unrecognised frame means the stream is no longer being
# parsed correctly, and continuing would return a payload assembled from the
# wrong bytes.
_OP_CONTINUATION = 0x0
_OP_TEXT = 0x1
_OP_BINARY = 0x2
_OP_CLOSE = 0x8
_OP_PING = 0x9
_OP_PONG = 0xA


class CDPError(RuntimeError):
    """Any failure to reach, or be understood by, the browser."""


def encode_frame(payload: bytes, opcode: int = _OP_TEXT) -> bytes:
    """One masked client frame.

    CLIENT FRAMES MUST BE MASKED — RFC 6455 §5.1, and Chromium enforces it: an
    unmasked client frame is answered with a close, not with an error anybody
    can read. The mask key must come from a CSPRNG, so ``secrets`` rather than
    ``random``; the masking is not a security boundary but a proxy-poisoning
    mitigation, and a predictable key removes the only property it has.

    Fragmentation is not produced here. A single frame may carry a 64-bit
    length, which covers every payload this agent sends (the largest is a URL),
    and one frame is one fewer state machine to get wrong.
    """
    length = len(payload)
    header = bytearray()
    header.append(0x80 | opcode)  # FIN set: never fragmented on the way out.
    if length < 126:
        header.append(0x80 | length)
    elif length < (1 << 16):
        header.append(0x80 | 126)
        header.extend(struct.pack("!H", length))
    else:
        header.append(0x80 | 127)
        header.extend(struct.pack("!Q", length))
    mask = secrets.token_bytes(4)
    header.extend(mask)
    masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
    return bytes(header) + masked


class _FrameReader:
    """Reassembles server frames off a blocking socket.

    Kept as a class with an explicit ``recv_exact`` so the codec can be tested
    against a byte stream with no socket in play — the frame parser is the part
    that has subtle failure modes (a 127-length payload, a continuation after a
    ping, a close arriving mid-message) and those are exactly the cases a live
    browser produces rarely and a test produces on demand.
    """

    def __init__(self, sock: socket.socket) -> None:
        self._sock = sock
        self._buffer = b""

    def recv_exact(self, count: int) -> bytes:
        while len(self._buffer) < count:
            chunk = self._sock.recv(65536)
            if not chunk:
                raise CDPError("browser closed the DevTools socket")
            self._buffer += chunk
        out, self._buffer = self._buffer[:count], self._buffer[count:]
        return out

    def read_message(self) -> tuple[int, bytes]:
        """The next complete application message, following continuations.

        A control frame (ping/close) may legally arrive BETWEEN the fragments
        of a data message, so control handling lives inside the assembly loop
        rather than before it. Getting that wrong produces a payload with a
        ping frame's bytes spliced into the middle of it, which then fails to
        parse as JSON and reads like a browser bug.
        """
        opcode: int | None = None
        payload = b""
        while True:
            first, second = self.recv_exact(2)
            fin = bool(first & 0x80)
            frame_op = first & 0x0F
            masked = bool(second & 0x80)
            length = second & 0x7F
            if length == 126:
                (length,) = struct.unpack("!H", self.recv_exact(2))
            elif length == 127:
                (length,) = struct.unpack("!Q", self.recv_exact(8))
            if masked:
                # A server frame must NOT be masked. Reading it as if it were
                # would succeed and hand back plausible-looking garbage.
                raise CDPError("server sent a masked frame")
            data = self.recv_exact(length) if length else b""

            if frame_op == _OP_PING:
                yield_pong = encode_frame(data, _OP_PONG)
                self._sock.sendall(yield_pong)
                continue
            if frame_op == _OP_PONG:
                continue
            if frame_op == _OP_CLOSE:
                raise CDPError("browser closed the DevTools socket")
            if frame_op == _OP_CONTINUATION:
                if opcode is None:
                    raise CDPError("continuation frame with nothing to continue")
            elif frame_op in (_OP_TEXT, _OP_BINARY):
                if opcode is not None:
                    raise CDPError("new data frame inside an unfinished message")
                opcode = frame_op
            else:
                raise CDPError(f"unsupported websocket opcode {frame_op:#x}")

            payload += data
            if fin:
                assert opcode is not None
                return opcode, payload


class CDPSession:
    """One short-lived WebSocket conversation with one browser target."""

    def __init__(self, ws_url: str, timeout: float = 10.0) -> None:
        self._ws_url = ws_url
        self._timeout = timeout
        self._sock: socket.socket | None = None
        self._reader: _FrameReader | None = None
        self._next_id = 0

    def __enter__(self) -> CDPSession:
        self._connect()
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()

    def _connect(self) -> None:
        parts = urllib.parse.urlsplit(self._ws_url)
        if parts.scheme != "ws":
            # Chromium serves DevTools over plain ws on loopback only. A wss
            # URL here means the endpoint is not the one we think it is.
            raise CDPError(f"unexpected DevTools scheme: {parts.scheme!r}")
        host = parts.hostname or "127.0.0.1"
        port = parts.port or 80
        path = parts.path or "/"
        if parts.query:
            path = f"{path}?{parts.query}"

        sock = socket.create_connection((host, port), timeout=self._timeout)
        sock.settimeout(self._timeout)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        handshake = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        ).encode("ascii")
        sock.sendall(handshake)

        reader = _FrameReader(sock)
        # The response header ends at the first blank line. Read it a byte at a
        # time rather than by chunk: anything past that line is already the
        # first frame, and a chunked read would swallow it into a buffer this
        # parser never looks at again.
        header = b""
        while not header.endswith(b"\r\n\r\n"):
            byte = sock.recv(1)
            if not byte:
                raise CDPError("DevTools closed during the websocket handshake")
            header += byte
        status = header.split(b"\r\n", 1)[0].decode("latin-1")
        if "101" not in status:
            raise CDPError(f"DevTools refused the upgrade: {status}")

        self._sock = sock
        self._reader = reader

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.sendall(encode_frame(b"", _OP_CLOSE))
            except OSError:
                pass
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
            self._reader = None

    def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Send one command and return its result.

        CDP multiplexes events onto the same socket, so the reply to command N
        is not necessarily the next message — a ``Page.navigate`` produces a
        burst of lifecycle events either side of its own result. Messages
        without our id are dropped rather than queued: this session lives for
        one command and has no consumer for them.
        """
        if self._sock is None or self._reader is None:
            raise CDPError("session is not connected")
        self._next_id += 1
        message_id = self._next_id
        body = json.dumps({"id": message_id, "method": method,
                           "params": params or {}}).encode("utf-8")
        self._sock.sendall(encode_frame(body))
        while True:
            _opcode, payload = self._reader.read_message()
            try:
                message = json.loads(payload.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as err:
                raise CDPError(f"unparseable DevTools message: {err}") from err
            if message.get("id") != message_id:
                continue
            if "error" in message:
                error = message["error"]
                raise CDPError(
                    f"{method} failed: {error.get('message', error)}"
                )
            return message.get("result") or {}


class Browser:
    """The DevTools endpoint of one Chromium instance."""

    def __init__(self, port: int = 9222, host: str = "127.0.0.1",
                 timeout: float = 5.0) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def _http_json(self, path: str) -> Any:
        url = f"{self.base_url}{path}"
        try:
            with urllib.request.urlopen(url, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, json.JSONDecodeError,
                UnicodeDecodeError) as err:
            raise CDPError(f"DevTools unreachable at {url}: {err}") from err

    def version(self) -> dict[str, Any]:
        return self._http_json("/json/version")

    def targets(self) -> list[dict[str, Any]]:
        raw = self._http_json("/json/list")
        return [item for item in raw if isinstance(item, dict)]

    def page_target(self) -> dict[str, Any] | None:
        """The page the wall is showing, or None.

        THREE OUTCOMES, KEPT APART, and the caller must keep them apart too. A
        page target is an answer. NO page target on a reachable endpoint is a
        different answer — normal for a moment mid-navigation — and returns
        None. An unreachable endpoint RAISES, because a wall whose browser is
        gone and a wall that is between pages are not the same condition and
        collapsing them into one falsy value is how a dead panel reads healthy.
        """
        for target in self.targets():
            if target.get("type") == "page":
                return target
        return None

    def _session(self) -> CDPSession:
        target = self.page_target()
        if target is None:
            raise CDPError("no page target: the browser has no open tab")
        ws_url = target.get("webSocketDebuggerUrl")
        if not ws_url:
            raise CDPError("page target carries no webSocketDebuggerUrl")
        return CDPSession(ws_url, timeout=max(self.timeout, 10.0))

    def current_url(self) -> str | None:
        """What Chromium is PAINTING — a different question from what it was told
        to paint. It must never fall back to the configured start URL: a
        cross-check that mirrors the thing it checks asserts nothing at all."""
        target = self.page_target()
        if target is None:
            return None
        return target.get("url") or None

    def navigate(self, url: str) -> None:
        with self._session() as session:
            session.call("Page.navigate", {"url": url})

    def reload(self, ignore_cache: bool = False) -> None:
        with self._session() as session:
            session.call("Page.reload", {"ignoreCache": ignore_cache})

    def screenshot_png(self) -> bytes:
        with self._session() as session:
            result = session.call("Page.captureScreenshot", {"format": "png"})
        data = result.get("data")
        if not data:
            raise CDPError("captureScreenshot returned no data")
        return base64.b64decode(data)

    def clear_cache(self) -> None:
        with self._session() as session:
            session.call("Network.clearBrowserCache")

    def clear_cookies(self) -> None:
        with self._session() as session:
            session.call("Network.clearBrowserCookies")

    def evaluate(self, expression: str) -> Any:
        with self._session() as session:
            result = session.call(
                "Runtime.evaluate",
                {"expression": expression, "returnByValue": True,
                 "awaitPromise": False},
            )
        return (result.get("result") or {}).get("value")

    def bring_to_front(self) -> None:
        with self._session() as session:
            session.call("Page.bringToFront")
