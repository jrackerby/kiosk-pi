"""The WebSocket frame codec.

THE CODEC IS THE PART WITH SUBTLE FAILURE MODES and the part a live browser
exercises least predictably: a 127-length payload only appears on a large
screenshot, a control frame interleaved into a fragmented message only appears
when Chromium happens to ping mid-transfer. Both are produced here on demand.
"""

from __future__ import annotations

import json
import struct

import pytest

from pikioskd.cdp import (
    CDPError,
    _FrameReader,
    _OP_BINARY,
    _OP_CLOSE,
    _OP_CONTINUATION,
    _OP_PING,
    _OP_TEXT,
    encode_frame,
)


class FakeSocket:
    """A socket that hands back a scripted byte stream and records sends."""

    def __init__(self, inbound: bytes) -> None:
        self._inbound = inbound
        self.sent = b""

    def recv(self, size: int) -> bytes:
        chunk, self._inbound = self._inbound[:size], self._inbound[size:]
        return chunk

    def sendall(self, data: bytes) -> None:
        self.sent += data


def server_frame(payload: bytes, opcode: int = _OP_TEXT, fin: bool = True) -> bytes:
    """A server frame: never masked, per RFC 6455 §5.1."""
    header = bytearray([(0x80 if fin else 0) | opcode])
    length = len(payload)
    if length < 126:
        header.append(length)
    elif length < (1 << 16):
        header.append(126)
        header.extend(struct.pack("!H", length))
    else:
        header.append(127)
        header.extend(struct.pack("!Q", length))
    return bytes(header) + payload


def test_client_frames_are_masked():
    """Chromium answers an unmasked client frame with a close, not an error.

    The mask bit is the single most common omission in a hand-rolled client and
    it presents as "the browser hung up", miles from the cause.
    """
    frame = encode_frame(b"hello")
    assert frame[1] & 0x80, "client frame is not masked"


def test_masking_round_trips():
    frame = encode_frame(b"hello")
    mask = frame[2:6]
    body = frame[6:]
    assert bytes(b ^ mask[i % 4] for i, b in enumerate(body)) == b"hello"


def test_two_frames_use_different_masks():
    """A fixed or predictable mask removes the only property masking has."""
    masks = {encode_frame(b"x")[2:6] for _ in range(20)}
    assert len(masks) > 1


@pytest.mark.parametrize("size", [0, 1, 125, 126, 127, 65535, 65536, 200_000])
def test_length_encodings_round_trip(size):
    """125/126 and 65535/65536 are the boundaries between the three encodings.

    A screenshot is the only payload that reaches the 8-byte form in practice,
    so the boundary is asserted here rather than discovered on a 4K panel.
    """
    payload = b"a" * size
    reader = _FrameReader(FakeSocket(server_frame(payload)))
    opcode, out = reader.read_message()
    assert opcode == _OP_TEXT
    assert out == payload


def test_fragmented_message_is_reassembled():
    stream = (server_frame(b"one ", _OP_TEXT, fin=False)
              + server_frame(b"two ", _OP_CONTINUATION, fin=False)
              + server_frame(b"three", _OP_CONTINUATION, fin=True))
    _opcode, out = _FrameReader(FakeSocket(stream)).read_message()
    assert out == b"one two three"


def test_a_ping_between_fragments_is_answered_and_not_spliced_in():
    """The failure this catches is silent.

    A control frame is legal BETWEEN the fragments of a data message. A reader
    that handles pings only before the assembly loop splices the ping's bytes
    into the payload, which then fails to parse as JSON and reads like a
    browser bug.
    """
    stream = (server_frame(b'{"id":1,', _OP_TEXT, fin=False)
              + server_frame(b"\x01\x02", _OP_PING)
              + server_frame(b'"result":{}}', _OP_CONTINUATION, fin=True))
    sock = FakeSocket(stream)
    _opcode, out = _FrameReader(sock).read_message()
    assert json.loads(out) == {"id": 1, "result": {}}
    assert sock.sent, "the ping was not answered with a pong"
    assert (sock.sent[0] & 0x0F) == 0xA


def test_binary_opcode_survives():
    _opcode, out = _FrameReader(FakeSocket(server_frame(b"\x89PNG", _OP_BINARY))
                                ).read_message()
    assert out == b"\x89PNG"


def test_close_frame_raises_rather_than_returning_empty():
    """An empty payload and a closed socket are different conditions.

    Returning b"" for a close makes a hung-up browser read as a page that
    answered with nothing, and the caller then reports an empty URL rather
    than an unreachable browser.
    """
    with pytest.raises(CDPError):
        _FrameReader(FakeSocket(server_frame(b"", _OP_CLOSE))).read_message()


def test_a_masked_server_frame_is_refused():
    """Reading it anyway would succeed and hand back plausible garbage."""
    frame = bytearray(server_frame(b"hello"))
    frame[1] |= 0x80
    with pytest.raises(CDPError):
        _FrameReader(FakeSocket(bytes(frame) + b"\x00" * 4)).read_message()


def test_truncated_stream_raises():
    with pytest.raises(CDPError):
        _FrameReader(FakeSocket(server_frame(b"hello")[:4])).read_message()


def test_the_assertions_above_can_fail():
    """LAW §4: prove the checks are capable of failing."""
    with pytest.raises(AssertionError):
        assert server_frame(b"x")[1] & 0x80  # a server frame must NOT be masked
    with pytest.raises(AssertionError):
        # A reader that stopped following continuations would return only the
        # first fragment; assert that that is detectably wrong.
        stream = (server_frame(b"one", _OP_TEXT, fin=False)
                  + server_frame(b"two", _OP_CONTINUATION, fin=True))
        assert _FrameReader(FakeSocket(stream)).read_message()[1] == b"one"
