
"""Tests of the whole-request deadline (core.gleif.DeadlineWatch).

A reply that trickles in byte by byte used to outlast the deadline by
far wherever urllib3 or http.client make many socket reads in one call:
the status line and headers, and a gzip or chunked body. A raw-socket
server on 127.0.0.1 sends such replies to the real GLEIF and OpenFIGI
clients; nothing else touches the network.
"""

import gzip
import json
import socket
import threading
import time

import pytest

from core import gleif, openfigi
from core.gleif import DeadlineExceeded, GleifClient

BODY = json.dumps({"data": []}).encode()
#: The deadline is DEADLINE seconds away and each read may take
#: TIMEOUT: before the watch, these replies ran some 26 s.
DEADLINE = 1.5
TIMEOUT = 1.0
BOUND = DEADLINE + gleif._WATCH_GRACE + TIMEOUT + 1.5


class _Server:
    """Answers each request with ``reply(connection)`` on 127.0.0.1."""

    def __init__(self, reply):
        self.reply = reply
        self.requests = 0
        self._sock = socket.socket()
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(8)
        self.url = f"http://127.0.0.1:{self._sock.getsockname()[1]}"
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                connection, _ = self._sock.accept()
            except OSError:
                return
            threading.Thread(
                target=self._answer, args=(connection,), daemon=True,
            ).start()

    def _answer(self, connection):
        with connection:
            try:
                while self._read_request(connection):
                    self.requests += 1
                    self.reply(connection)
            except OSError:
                pass

    @staticmethod
    def _read_request(connection):
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = connection.recv(65536)
            if not chunk:
                return False
            data += chunk
        head, _, rest = data.partition(b"\r\n\r\n")
        for line in head.split(b"\r\n"):
            name, _, value = line.partition(b":")
            if name.strip().lower() == b"content-length":
                length = int(value)
                while len(rest) < length:
                    rest += connection.recv(65536)
        return True

    def close(self):
        self._sock.close()


def _trickle(connection, data, interval=0.1):
    for index in range(len(data)):
        connection.sendall(data[index:index + 1])
        time.sleep(interval)


def _head(*lines):
    return (
        "\r\n".join(("HTTP/1.1 200 OK",) + lines) + "\r\n\r\n"
    ).encode("latin-1")


def _slow_header(connection):
    connection.sendall(b"HTTP/1.1 200 OK\r\n")
    _trickle(connection, b"X-Slow: " + b"a" * 250 + b"\r\n")


def _slow_gzip_body(connection):
    # A long original file name in the gzip header: urllib3 reads it all
    # inside one read1 call before it has a decoded byte to return.
    body = gzip.compress(BODY)
    body = (
        body[:3] + bytes([body[3] | 8]) + body[4:10] + b"n" * 250 + b"\0"
        + body[10:]
    )
    connection.sendall(_head(
        f"Content-Length: {len(body)}", "Content-Encoding: gzip",
    ))
    connection.sendall(body[:5])
    _trickle(connection, body[5:])


def _slow_chunked_trailer(connection):
    connection.sendall(_head("Transfer-Encoding: chunked"))
    connection.sendall(b"%x\r\n" % len(BODY) + BODY + b"\r\n0\r\n")
    _trickle(connection, b"X-Trailer: " + b"t" * 250 + b"\r\n\r\n")


def _fast(connection):
    connection.sendall(_head(f"Content-Length: {len(BODY)}") + BODY)


@pytest.fixture(autouse=True)
def _no_proxy(monkeypatch):
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.lower(), raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")


@pytest.fixture
def serve(monkeypatch):
    servers = []

    def start(reply):
        server = _Server(reply)
        servers.append(server)
        monkeypatch.setattr(gleif, "GLEIF_BASE_URL", server.url)
        monkeypatch.setattr(openfigi, "OPENFIGI_BASE_URL", server.url)
        return server

    yield start
    for server in servers:
        server.close()


@pytest.fixture(params=["native", "linux"])
def shut_down_reads(request, monkeypatch):
    """Reads a socket the watch shut down as here, and as on Linux.

    On Linux, where the app runs, the read returns EOF (0 bytes) where
    Windows makes it fail, so a reply can end as if complete.
    """
    if request.param == "native":
        return
    shut = set()
    real_shut_down = gleif.DeadlineWatch._shut_down
    real_readinto = socket.SocketIO.readinto

    def shut_down(watch):
        sock = getattr(watch._connection, "sock", None)
        if sock is not None:
            shut.add(sock)
        real_shut_down(watch)

    def readinto(stream, buffer):
        if stream._sock in shut:
            return 0
        try:
            return real_readinto(stream, buffer)
        except OSError:
            if stream._sock in shut:
                return 0
            raise

    monkeypatch.setattr(gleif.DeadlineWatch, "_shut_down", shut_down)
    monkeypatch.setattr(socket.SocketIO, "readinto", readinto)


@pytest.mark.parametrize("reply", [
    _slow_header, _slow_gzip_body, _slow_chunked_trailer,
])
def test_a_trickling_gleif_reply_stops_soon_after_the_deadline(
    serve, shut_down_reads, reply,
):
    serve(reply)
    with GleifClient(timeout=TIMEOUT) as client:
        client.deadline = time.monotonic() + DEADLINE
        started = time.monotonic()
        with pytest.raises(DeadlineExceeded):
            client._request("/lei-records", {}, attempts=1)
    assert time.monotonic() - started < BOUND


def test_a_trickling_openfigi_reply_stops_soon_after_the_deadline(
    serve, shut_down_reads,
):
    serve(_slow_header)
    started = time.monotonic()
    with pytest.raises(DeadlineExceeded):
        openfigi.resolve_isin_to_names(
            "US0378331005", deadline=time.monotonic() + DEADLINE,
        )
    assert time.monotonic() - started < BOUND


def test_the_watch_leaves_a_pooled_connection_alone(serve):
    server = serve(_fast)
    with GleifClient(timeout=TIMEOUT) as client:
        client.deadline = time.monotonic() + 0.2
        assert client._request("/lei-records", {}) == {"data": []}
        # Past the first request's deadline and grace: its watch must
        # not shut down the kept-alive connection the next one reuses.
        time.sleep(0.2 + gleif._WATCH_GRACE + 0.3)
        client.deadline = time.monotonic() + 10
        assert client._request("/lei-records", {}, attempts=1) == {
            "data": []
        }
    assert server.requests == 2

