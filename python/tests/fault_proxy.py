"""A TCP proxy between a client and a real store that can lose what a network loses: the reply,
the request, or the connection in the middle of a call."""

from __future__ import annotations

import contextlib
import socket
import threading
import time
from collections.abc import Callable
from urllib.parse import urlsplit, urlunsplit


class _Link:
    def __init__(self, client: socket.socket, upstream: socket.socket) -> None:
        self.client, self.upstream = client, upstream
        self.to_upstream: list[bytes] = []
        self.to_client: list[bytes] = []

    def close(self) -> None:
        for sock in (self.client, self.upstream):
            with contextlib.suppress(OSError):
                sock.shutdown(socket.SHUT_RDWR)
            sock.close()


class FaultProxy:
    def __init__(self, target: str) -> None:
        parts = urlsplit(target)
        assert parts.hostname and parts.port
        self._target = (parts.hostname, parts.port)
        self._lock = threading.Lock()
        self._mode = "pass"
        self._links: set[_Link] = set()
        self._server = socket.create_server(("127.0.0.1", 0))
        port = self._server.getsockname()[1]
        credentials = parts.netloc.rpartition("@")[0]
        netloc = f"{credentials}@127.0.0.1:{port}" if credentials else f"127.0.0.1:{port}"
        #: The store's URL with the proxy's host and port.
        self.url = urlunsplit(parts._replace(netloc=netloc))
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self) -> None:
        while True:
            try:
                client, _ = self._server.accept()
            except OSError:
                return
            try:
                upstream = socket.create_connection(self._target)
            except OSError:
                client.close()
                continue
            link = _Link(client, upstream)
            with self._lock:
                self._links.add(link)
            for source, to_store in ((client, True), (upstream, False)):
                threading.Thread(
                    target=self._pump, args=(link, source, to_store), daemon=True
                ).start()

    def _pump(self, link: _Link, source: socket.socket, to_store: bool) -> None:
        try:
            while chunk := source.recv(65536):
                with self._lock:
                    if to_store and self._mode == "hold-requests":
                        link.to_upstream.append(chunk)
                    elif not to_store and self._mode == "hold-replies":
                        link.to_client.append(chunk)
                    else:
                        (link.upstream if to_store else link.client).sendall(chunk)
        except OSError:
            pass
        with self._lock:
            self._links.discard(link)
        link.close()

    def hold_replies(self) -> None:
        """Requests still reach the store, and its replies wait here."""
        with self._lock:
            self._mode = "hold-replies"

    def hold_requests(self) -> None:
        """Requests wait here: the store sees nothing."""
        with self._lock:
            self._mode = "hold-requests"

    def resume(self) -> None:
        """Deliver what was held, in order, and forward again."""
        with self._lock:
            self._mode = "pass"
            for link in self._links:
                try:
                    for chunk in link.to_upstream:
                        link.upstream.sendall(chunk)
                    for chunk in link.to_client:
                        link.client.sendall(chunk)
                except OSError:
                    pass
                link.to_upstream.clear()
                link.to_client.clear()

    def cut(self) -> None:
        """Close every connection. What was held is lost, and new connections are forwarded."""
        with self._lock:
            self._mode = "pass"
            links, self._links = self._links, set()
        for link in links:
            link.close()

    def held_reply_bytes(self) -> int:
        """Above zero, the store has answered a call nobody heard."""
        with self._lock:
            return sum(len(chunk) for link in self._links for chunk in link.to_client)

    def held_request_bytes(self) -> int:
        with self._lock:
            return sum(len(chunk) for link in self._links for chunk in link.to_upstream)

    def close(self) -> None:
        self._server.close()
        self.cut()


def until(done: Callable[[], bool], what: str, timeout_s: float = 10.0) -> None:
    """Poll until `done` holds, so a test waits for an event and not for a guessed delay."""
    deadline = time.monotonic() + timeout_s
    while not done():
        if time.monotonic() > deadline:
            raise TimeoutError(f"timed out waiting for {what}")
        time.sleep(0.01)
