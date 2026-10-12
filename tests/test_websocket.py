"""The standard-library WebSocket client duet page uses where the pipe cannot go.

Framing is checked byte for byte against RFC 6455, and the connection is
checked against a fake server on the other end of a socket pair: the upgrade,
fragmented messages with control frames in between, ping answered with pong,
and close answered with close. The real-Chrome tests in test_page_chrome.py
run over this transport as well.
"""

import base64
import socket
import struct
import threading

import pytest

from duet import websocket
from duet.websocket import (OP_BINARY, OP_CLOSE, OP_CONTINUATION, OP_PING, OP_PONG, OP_TEXT,
                            Closed, FrameReader, WebSocket, WebSocketError, encode_frame)

KEY = b"\x37\xfa\x21\x3d"


def server_frame(opcode, payload, fin=True):
    """A frame as a server sends it: unmasked."""
    return encode_frame(opcode, payload, fin=fin, masked=False)


def read_one(data):
    reader = FrameReader()
    reader.feed(data)
    return reader.next_frame()


# -- framing -----------------------------------------------------------------

def test_the_rfc_example_of_a_masked_hello():
    """RFC 6455 §5.7: a single-frame masked text message containing "Hello"."""
    frame = encode_frame(OP_TEXT, b"Hello", mask_key=KEY)
    assert frame == bytes([0x81, 0x85, 0x37, 0xfa, 0x21, 0x3d,
                           0x7f, 0x9f, 0x4d, 0x51, 0x58])


def test_masking_is_its_own_inverse_and_uses_the_key():
    payload = bytes(range(256)) * 3
    masked = websocket.mask(payload, KEY)
    assert masked != payload
    assert websocket.mask(masked, KEY) == payload
    assert masked[0] == payload[0] ^ KEY[0] and masked[5] == payload[5] ^ KEY[1]


def test_every_client_frame_gets_a_fresh_key():
    first, second = encode_frame(OP_TEXT, b"same"), encode_frame(OP_TEXT, b"same")
    assert first[1] & 0x80 and second[1] & 0x80
    assert first[2:6] != second[2:6]


@pytest.mark.parametrize("length, header", [
    (0, 2), (1, 2), (125, 2),             # 7-bit length
    (126, 4), (65535, 4),                 # 16-bit length after 126
    (65536, 10), (200000, 10),            # 64-bit length after 127
])
def test_lengths_in_all_three_encodings(length, header):
    payload = bytes(i % 251 for i in range(length))
    frame = encode_frame(OP_TEXT, payload, mask_key=KEY)
    assert len(frame) == header + 4 + length
    marker = frame[1] & 0x7F
    if length <= 125:
        assert marker == length
    elif length <= 65535:
        assert marker == 126 and struct.unpack("!H", frame[2:4])[0] == length
    else:
        assert marker == 127 and struct.unpack("!Q", frame[2:10])[0] == length
    assert read_one(frame) == (True, OP_TEXT, payload)
    # And unmasked, the way the server sends them.
    assert read_one(server_frame(OP_TEXT, payload)) == (True, OP_TEXT, payload)


def test_a_frame_arriving_a_byte_at_a_time_is_whole_only_at_the_end():
    payload = b"x" * 70000
    frame = server_frame(OP_TEXT, payload)
    reader = FrameReader()
    for byte in frame[:12]:                # the header, a byte at a time
        reader.feed(bytes([byte]))
        assert reader.next_frame() is None
    reader.feed(frame[12:-1])
    assert reader.next_frame() is None
    reader.feed(frame[-1:])
    assert reader.next_frame() == (True, OP_TEXT, payload)
    assert reader.next_frame() is None


def test_a_control_frame_may_not_be_fragmented_or_long():
    with pytest.raises(WebSocketError):
        read_one(server_frame(OP_PING, b"x", fin=False))
    with pytest.raises(WebSocketError):
        read_one(server_frame(OP_PING, b"x" * 126))


def test_reserved_bits_nobody_negotiated_are_an_error():
    frame = bytearray(server_frame(OP_TEXT, b"hi"))
    frame[0] |= 0x40
    with pytest.raises(WebSocketError):
        read_one(bytes(frame))


def test_the_accept_key_is_the_rfc_one():
    """RFC 6455 §1.3's worked example."""
    assert websocket.accept_key("dGhlIHNhbXBsZSBub25jZQ==") == "s3pPLMBiTxaQ9kYGzzhZRbK+xOo="


# -- a connection, against a fake server -------------------------------------

class FakeServer:
    """The far end of a socket pair, speaking just enough server WebSocket."""

    def __init__(self, sock):
        self.sock = sock
        self.reader = FrameReader()

    def handshake(self, status="101 Switching Protocols", accept=None, extra=b""):
        request = b""
        while b"\r\n\r\n" not in request:
            request += self.sock.recv(4096)
        lines = request.decode("latin-1").split("\r\n")
        self.request_line = lines[0]
        headers = {l.split(":", 1)[0].lower(): l.split(":", 1)[1].strip()
                   for l in lines[1:] if ":" in l}
        self.headers = headers
        answer = accept if accept is not None else websocket.accept_key(
            headers["sec-websocket-key"])
        self.sock.sendall(("HTTP/1.1 %s\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                           "Sec-WebSocket-Accept: %s\r\n\r\n" % (status, answer)).encode()
                          + extra)

    def frame(self):
        """The next frame the client sent, which must be masked."""
        while True:
            got = self.reader.next_frame()
            if got is not None:
                return got
            if len(self.reader._buf) >= 2:
                assert self.reader._buf[1] & 0x80, "an unmasked client frame"
            chunk = self.sock.recv(1 << 16)
            assert chunk, "the client hung up"
            self.reader.feed(chunk)

    def send(self, data):
        self.sock.sendall(data)


@pytest.fixture
def pair():
    client, server = socket.socketpair()
    yield client, FakeServer(server)
    client.close()
    server.close()


def connect_over(client_sock, server, **handshake):
    """WebSocket.connect, minus the TCP connect: the handshake over the pair."""
    thread = threading.Thread(target=server.handshake, kwargs=handshake)
    thread.start()
    try:
        leftover = WebSocket._handshake(client_sock, "127.0.0.1", 9222,
                                        "/devtools/browser/abc", 5)
    finally:
        thread.join(5)
    ws = WebSocket(client_sock)
    ws.reader.feed(leftover)
    return ws


def test_the_upgrade_request_is_a_websocket_one(pair):
    client, server = pair
    connect_over(client, server)
    assert server.request_line == "GET /devtools/browser/abc HTTP/1.1"
    assert server.headers["upgrade"].lower() == "websocket"
    assert server.headers["sec-websocket-version"] == "13"
    assert len(base64.b64decode(server.headers["sec-websocket-key"])) == 16


def test_a_refused_upgrade_is_an_error_not_a_hang(pair):
    client, server = pair
    with pytest.raises(WebSocketError) as caught:
        connect_over(client, server, status="404 Not Found")
    assert "404" in str(caught.value)


def test_a_wrong_accept_key_is_an_error(pair):
    client, server = pair
    with pytest.raises(WebSocketError):
        connect_over(client, server, accept="bm90IHRoZSByaWdodCBrZXk=")


def test_a_frame_sent_with_the_upgrade_answer_is_not_lost(pair):
    client, server = pair
    ws = connect_over(client, server, extra=server_frame(OP_TEXT, b'{"id":1}'))
    assert ws.receive(1) == '{"id":1}'


def test_text_goes_out_masked_and_comes_back(pair):
    client, server = pair
    ws = connect_over(client, server)
    ws.send_text('{"id": 1, "method": "Browser.getVersion"}')
    assert server.frame() == (True, OP_TEXT, b'{"id": 1, "method": "Browser.getVersion"}')
    # Bigger than a socket buffer, so the server has to read while it is sent.
    big = '{"data": "%s"}' % ("A" * 300000)
    got = []
    reading = threading.Thread(target=lambda: got.append(server.frame()))
    reading.start()
    ws.send_text(big)
    reading.join(10)
    assert got == [(True, OP_TEXT, big.encode())]


def test_a_fragmented_message_with_control_frames_between_is_one_message(pair):
    client, server = pair
    ws = connect_over(client, server)
    server.send(server_frame(OP_TEXT, b'{"id": 7, ', fin=False)
                + server_frame(OP_PING, b"are you there")
                + server_frame(OP_CONTINUATION, b'"result": ', fin=False)
                + server_frame(OP_PONG, b"unasked")
                + server_frame(OP_CONTINUATION, b'{}}'))
    assert ws.receive(2) == '{"id": 7, "result": {}}'
    # The ping in the middle was answered with a pong carrying its payload.
    assert server.frame() == (True, OP_PONG, b"are you there")


def test_a_binary_message_is_skipped_not_returned(pair):
    client, server = pair
    ws = connect_over(client, server)
    server.send(server_frame(OP_BINARY, b"\x00\x01") + server_frame(OP_TEXT, b"next"))
    assert ws.receive(1) == "next"


def test_nothing_within_the_timeout_is_none_not_an_error(pair):
    client, server = pair
    ws = connect_over(client, server)
    assert ws.receive(0.05) is None
    server.send(server_frame(OP_TEXT, b"later"))
    assert ws.receive(1) == "later"


def test_a_close_is_answered_and_reported(pair):
    client, server = pair
    ws = connect_over(client, server)
    server.send(server_frame(OP_CLOSE, struct.pack("!H", 1001) + b"going away"))
    with pytest.raises(Closed) as caught:
        ws.receive(1)
    assert "1001" in str(caught.value) and "going away" in str(caught.value)
    fin, opcode, payload = server.frame()
    assert opcode == OP_CLOSE and payload == struct.pack("!H", 1001)
    with pytest.raises(Closed):
        ws.send_text("too late")


def test_a_socket_that_just_ends_is_closed(pair):
    client, server = pair
    ws = connect_over(client, server)
    server.sock.close()
    with pytest.raises(Closed):
        ws.receive(1)


def test_a_continuation_with_nothing_to_continue_is_an_error(pair):
    client, server = pair
    ws = connect_over(client, server)
    server.send(server_frame(OP_CONTINUATION, b"orphan"))
    with pytest.raises(WebSocketError):
        ws.receive(1)


def test_closing_sends_a_normal_close(pair):
    client, server = pair
    ws = connect_over(client, server)
    ws.close()
    assert server.frame() == (True, OP_CLOSE, struct.pack("!H", 1000))
    ws.close()                              # twice is fine


def test_connect_speaks_to_a_real_listening_socket():
    """The whole of connect(), TCP included, against a server on 127.0.0.1."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    seen = {}

    def serve():
        conn, _ = listener.accept()
        server = FakeServer(conn)
        server.handshake()
        seen["host"] = server.headers["host"]
        seen["frame"] = server.frame()
        server.send(server_frame(OP_TEXT, b'{"id": 1, "result": {}}'))
        server.frame()                      # the close
        conn.close()

    thread = threading.Thread(target=serve)
    thread.start()
    try:
        ws = WebSocket.connect("127.0.0.1", port, "/devtools/browser/x", timeout=5)
        ws.send_text('{"id": 1}')
        assert ws.receive(5) == '{"id": 1, "result": {}}'
        ws.close()
    finally:
        thread.join(5)
        listener.close()
    assert seen["host"] == "127.0.0.1:%d" % port
    assert seen["frame"] == (True, OP_TEXT, b'{"id": 1}')
