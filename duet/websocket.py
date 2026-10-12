"""A WebSocket client with nothing but the standard library — enough for DevTools.

Chrome speaks its DevTools protocol over a pipe on file descriptors 3 and 4, or
over a WebSocket on a local port. Windows cannot hand a child descriptors 3 and
4, so there the WebSocket is the only way in, and the standard library has no
client for one. This is the part of RFC 6455 that talking to a local Chrome
needs, and no more:

- the HTTP/1.1 upgrade, with the server's Sec-WebSocket-Accept checked, so a
  port that answers with something other than a WebSocket is an error and not
  a hang;
- client frames masked with a fresh key each, as a server must refuse them
  otherwise; payload lengths in all three encodings (7-bit up to 125, 16-bit
  up to 65535, 64-bit beyond — a full-page screenshot is megabytes);
- text messages, reassembled from fragments, with control frames allowed in
  between the fragments as the RFC says they may be;
- ping answered with pong, and close answered with close and then reported.

No TLS, no extensions, no compression, no proxy: the only server this ever
talks to is a Chrome on 127.0.0.1 that this process started.
"""

from __future__ import annotations

import base64
import hashlib
import os
import socket
import struct
import time
from typing import List, Optional, Tuple

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

OP_CONTINUATION = 0x0
OP_TEXT = 0x1
OP_BINARY = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA
CONTROL_OPS = (OP_CLOSE, OP_PING, OP_PONG)

# A response header bigger than this is not Chrome answering an upgrade.
MAX_HANDSHAKE = 64 * 1024


class WebSocketError(Exception):
    """The other side is not speaking WebSocket, or broke the protocol."""


class Closed(WebSocketError):
    """The connection is gone: a close frame, or the socket ending."""


def accept_key(key: str) -> str:
    """What the server must answer for this Sec-WebSocket-Key (RFC 6455 §4.2.2)."""
    digest = hashlib.sha1((key + GUID).encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")


def mask(payload: bytes, key: bytes) -> bytes:
    """XOR `payload` with the 4-byte `key`, repeating. Its own inverse."""
    if not payload:
        return b""
    repeated = (key * (len(payload) // 4 + 1))[:len(payload)]
    return (int.from_bytes(payload, "big") ^ int.from_bytes(repeated, "big")).to_bytes(
        len(payload), "big")


def encode_frame(opcode: int, payload: bytes, fin: bool = True,
                 mask_key: Optional[bytes] = None, masked: bool = True) -> bytes:
    """One frame. A client's are always masked; `masked=False` is for tests
    that need to play the server."""
    head = bytearray([(0x80 if fin else 0) | (opcode & 0x0F)])
    length = len(payload)
    bit = 0x80 if masked else 0
    if length <= 125:
        head.append(bit | length)
    elif length <= 0xFFFF:
        head.append(bit | 126)
        head += struct.pack("!H", length)
    else:
        head.append(bit | 127)
        head += struct.pack("!Q", length)
    if not masked:
        return bytes(head) + payload
    key = mask_key if mask_key is not None else os.urandom(4)
    return bytes(head) + key + mask(payload, key)


class FrameReader:
    """Frames out of a byte stream that arrives in arbitrary pieces."""

    def __init__(self) -> None:
        # A bytearray, because a full-page screenshot arrives as a hundred
        # 64 KiB reads, and bytes += would copy the whole buffer on each one.
        self._buf = bytearray()

    def feed(self, data: bytes) -> None:
        self._buf += data

    def next_frame(self) -> Optional[Tuple[bool, int, bytes]]:
        """(fin, opcode, payload) for the next whole frame, or None if it has
        not all arrived yet."""
        buf = self._buf
        if len(buf) < 2:
            return None
        first, second = buf[0], buf[1]
        if first & 0x70:
            raise WebSocketError("a frame uses an extension nobody agreed to (RSV bits %#x)"
                                 % (first & 0x70))
        fin, opcode = bool(first & 0x80), first & 0x0F
        masked, length = bool(second & 0x80), second & 0x7F
        offset = 2
        if length == 126:
            if len(buf) < offset + 2:
                return None
            length = struct.unpack("!H", buf[offset:offset + 2])[0]
            offset += 2
        elif length == 127:
            if len(buf) < offset + 8:
                return None
            length = struct.unpack("!Q", buf[offset:offset + 8])[0]
            offset += 8
        key = b""
        if masked:
            if len(buf) < offset + 4:
                return None
            key = bytes(buf[offset:offset + 4])
            offset += 4
        if len(buf) < offset + length:
            return None
        payload = bytes(buf[offset:offset + length])
        del buf[:offset + length]
        if masked:
            payload = mask(payload, key)
        if opcode in CONTROL_OPS and (not fin or length > 125):
            raise WebSocketError("a control frame was fragmented or longer than 125 bytes")
        return fin, opcode, payload


class WebSocket:
    """One client connection: send text, receive text, answer pings.

    `receive` returns None when nothing complete arrived in time, so a caller
    can check on the server process between waits — the same shape as a
    select() on the pipe transport.
    """

    def __init__(self, sock: socket.socket):
        self.sock = sock
        self.reader = FrameReader()
        self._fragments: List[bytes] = []
        self._fragment_op: Optional[int] = None
        self._closed = False

    @classmethod
    def connect(cls, host: str, port: int, path: str, timeout: float = 10.0) -> "WebSocket":
        """Open the TCP connection and upgrade it. Raises WebSocketError or OSError."""
        sock = socket.create_connection((host, port), timeout=timeout)
        try:
            leftover = cls._handshake(sock, host, port, path, timeout)
        except BaseException:
            sock.close()
            raise
        ws = cls(sock)
        ws.reader.feed(leftover)
        return ws

    @staticmethod
    def _handshake(sock: socket.socket, host: str, port: int, path: str,
                   timeout: float) -> bytes:
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = ("GET %s HTTP/1.1\r\n"
                   "Host: %s:%d\r\n"
                   "Upgrade: websocket\r\n"
                   "Connection: Upgrade\r\n"
                   "Sec-WebSocket-Key: %s\r\n"
                   "Sec-WebSocket-Version: 13\r\n"
                   "\r\n") % (path or "/", host, port, key)
        sock.sendall(request.encode("ascii"))
        deadline = time.time() + timeout
        response = b""
        while b"\r\n\r\n" not in response:
            left = deadline - time.time()
            if left <= 0:
                raise WebSocketError("no answer to the WebSocket upgrade within %.0fs" % timeout)
            sock.settimeout(left)
            try:
                chunk = sock.recv(4096)
            except socket.timeout:
                continue
            if not chunk:
                raise WebSocketError("the connection closed during the WebSocket upgrade")
            response += chunk
            if len(response) > MAX_HANDSHAKE:
                raise WebSocketError("the answer to the WebSocket upgrade is not HTTP")
        head, _, leftover = response.partition(b"\r\n\r\n")
        lines = head.decode("latin-1").split("\r\n")
        status = lines[0].split(" ", 2)
        if len(status) < 2 or status[1] != "101":
            raise WebSocketError("the WebSocket upgrade was refused: %s" % lines[0])
        headers = {}
        for line in lines[1:]:
            name, sep, value = line.partition(":")
            if sep:
                headers[name.strip().lower()] = value.strip()
        if headers.get("sec-websocket-accept") != accept_key(key):
            raise WebSocketError("the server answered the upgrade with the wrong "
                                 "Sec-WebSocket-Accept")
        return leftover

    # -- sending ----------------------------------------------------------
    def _send_frame(self, opcode: int, payload: bytes) -> None:
        self.sock.settimeout(None)
        self.sock.sendall(encode_frame(opcode, payload))

    def send_text(self, text: str) -> None:
        if self._closed:
            raise Closed("the connection is already closed")
        try:
            self._send_frame(OP_TEXT, text.encode("utf-8"))
        except OSError as exc:
            raise Closed("the connection broke while sending: %s" % exc)

    # -- receiving --------------------------------------------------------
    def receive(self, timeout: float) -> Optional[str]:
        """The next whole text message, or None if none arrived within `timeout`.

        Raises Closed when the server closes or the socket ends, and
        WebSocketError when what arrives is not a valid frame sequence.
        """
        deadline = time.time() + timeout
        while True:
            message = self._next_message()
            if message is not None:
                return message
            left = deadline - time.time()
            if left <= 0:
                return None
            self.sock.settimeout(left)
            try:
                chunk = self.sock.recv(1 << 16)
            except socket.timeout:
                return None
            except OSError as exc:
                self._closed = True
                raise Closed("the connection broke: %s" % exc)
            if not chunk:
                self._closed = True
                raise Closed("the connection closed without a close frame")
            self.reader.feed(chunk)

    def _next_message(self) -> Optional[str]:
        """A whole message from what has been read so far, handling control frames."""
        while True:
            frame = self.reader.next_frame()
            if frame is None:
                return None
            fin, opcode, payload = frame
            if opcode == OP_PING:
                try:
                    self._send_frame(OP_PONG, payload)
                except OSError:
                    pass
                continue
            if opcode == OP_PONG:
                continue
            if opcode == OP_CLOSE:
                self._answer_close(payload)
                code = struct.unpack("!H", payload[:2])[0] if len(payload) >= 2 else None
                reason = payload[2:].decode("utf-8", "replace")
                raise Closed("the server closed the connection%s%s" % (
                    " (%d)" % code if code is not None else "",
                    ": " + reason if reason else ""))
            if opcode == OP_CONTINUATION:
                if self._fragment_op is None:
                    raise WebSocketError("a continuation frame with no message to continue")
                self._fragments.append(payload)
            elif opcode in (OP_TEXT, OP_BINARY):
                if self._fragment_op is not None:
                    raise WebSocketError("a new message began before the last one ended")
                self._fragment_op = opcode
                self._fragments = [payload]
            else:
                raise WebSocketError("unknown opcode %#x" % opcode)
            if not fin:
                continue
            data, op = b"".join(self._fragments), self._fragment_op
            self._fragments, self._fragment_op = [], None
            if op == OP_TEXT:
                return data.decode("utf-8", "replace")
            # DevTools never sends binary messages; one that does is skipped.

    def _answer_close(self, payload: bytes) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._send_frame(OP_CLOSE, payload[:2])
        except OSError:
            pass

    def close(self) -> None:
        """Say goodbye and drop the socket. Never raises."""
        if not self._closed:
            self._closed = True
            try:
                self.sock.settimeout(1.0)
                self.sock.sendall(encode_frame(OP_CLOSE, struct.pack("!H", 1000)))
            except OSError:
                pass
        try:
            self.sock.close()
        except OSError:
            pass
