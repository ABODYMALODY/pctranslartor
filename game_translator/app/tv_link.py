"""Link to the Game Translator app on an Android TV.

Pairing:  the TV app shows a 6-digit code. The PC shouts "GT_DISCOVER <code>" to the home network (UDP);
          the TV with that code answers, the PC connects (TCP) and gets a token to reconnect later.
Streaming: one JSON line per update. A "group" message replaces everything the TV shows for that group:
          {"type": "group", "id": "subtitles", "layers": [{"x", "y", "w", "h", "png"}]}
          x, y, w, h are fractions of the screen; png is base64. An empty list clears the group.
The TV draws the pictures over the console's HDMI picture. All rendering is done here on the PC.
"""
from __future__ import annotations

import base64
import json
import socket
import threading
import time
from typing import Callable

DISCOVERY_PORT = 47800
LINK_PORT = 47801
PROTOCOL = 1


def _broadcast_addresses() -> list[str]:
    addresses = {"255.255.255.255"}
    try:
        for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
            if not ip.startswith("127."):
                addresses.add(ip.rsplit(".", 1)[0] + ".255")
    except OSError:
        pass
    return sorted(addresses)


def discover(code: str, timeout: float = 5.0) -> dict | None:
    """Find the TV showing this code. Returns {"host", "port", "name"} or None."""
    code = code.strip()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.settimeout(0.5)
    message = f"GT_DISCOVER {code}".encode()
    deadline = time.time() + timeout
    try:
        while time.time() < deadline:
            for address in _broadcast_addresses():
                try:
                    sock.sendto(message, (address, DISCOVERY_PORT))
                except OSError:
                    pass
            try:
                data, (host, _) = sock.recvfrom(4096)
            except socket.timeout:
                continue
            if data.startswith(b"GT_HERE "):
                info = json.loads(data[len(b"GT_HERE "):].decode("utf-8"))
                if str(info.get("code")) == code:
                    return {"host": host, "port": int(info.get("port", LINK_PORT)), "name": info.get("name", "TV")}
    finally:
        sock.close()
    return None


def _handshake(host: str, port: int, hello: dict) -> tuple[socket.socket, dict]:
    sock = socket.create_connection((host, port), timeout=5)
    sock.sendall((json.dumps(hello) + "\n").encode("utf-8"))
    reader = sock.makefile("r", encoding="utf-8")
    line = reader.readline()
    if not line:
        sock.close()
        raise ConnectionError("The TV closed the connection.")
    reply = json.loads(line)
    if reply.get("type") != "welcome":
        sock.close()
        raise ConnectionError(reply.get("reason", "The TV refused the connection."))
    sock.settimeout(10)
    return sock, reply


def pair(code: str) -> dict:
    """Find the TV by its code and get a token. Returns {"host", "port", "name", "token"}."""
    found = discover(code)
    if not found:
        raise ConnectionError("No TV answered. Is the Game Translator app open on the TV, on the same network?")
    sock, reply = _handshake(found["host"], found["port"], {
        "type": "hello", "protocol": PROTOCOL, "code": code.strip(), "pc": socket.gethostname(),
    })
    sock.close()
    return {**found, "name": reply.get("tv", found["name"]), "token": reply["token"]}


class TvLink:
    """Keeps one TV connected (reconnects by itself) and sends it the newest layers."""

    def __init__(self, on_status: Callable[[str, bool], None]) -> None:
        self._on_status = on_status  # (text, online)
        self._pending: dict[str, bytes] = {}  # group -> newest message (older ones are dropped)
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.tv: dict | None = None

    # ---------------------------------------------------------------- public

    def start(self, tv: dict) -> None:
        self.stop()
        self.tv = tv
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="tv-link", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=2)
        self._thread = None

    def send_group(self, group: str, layers: list[tuple[tuple[float, float, float, float], bytes]]) -> None:
        """layers: [((x, y, w, h) as screen fractions, png bytes)]. Replaces the whole group on the TV."""
        message = {
            "type": "group",
            "id": group,
            "layers": [
                {"x": round(x, 5), "y": round(y, 5), "w": round(w, 5), "h": round(h, 5),
                 "png": base64.b64encode(png).decode("ascii")}
                for (x, y, w, h), png in layers
            ],
        }
        with self._lock:
            self._pending[group] = (json.dumps(message) + "\n").encode("utf-8")
        self._wake.set()

    def clear(self, group: str) -> None:
        self.send_group(group, [])

    def clear_all(self) -> None:
        with self._lock:
            self._pending["*"] = b'{"type": "clear_all"}\n'
        self._wake.set()

    # ---------------------------------------------------------------- thread

    def _run(self) -> None:
        sock: socket.socket | None = None
        tv = self.tv or {}
        while not self._stop.is_set():
            if sock is None:
                try:
                    sock, reply = _handshake(tv["host"], tv["port"], {
                        "type": "hello", "protocol": PROTOCOL, "token": tv.get("token", ""), "pc": socket.gethostname(),
                    })
                    self._on_status(f"Connected to {reply.get('tv', tv.get('name', 'TV'))}", True)
                except (OSError, ConnectionError, ValueError) as exc:
                    self._on_status(f"TV offline — retrying ({exc})", False)
                    self._stop.wait(3)
                    continue

            self._wake.wait(timeout=2)
            self._wake.clear()
            with self._lock:
                batch = list(self._pending.values())
                self._pending.clear()
            try:
                if not batch:
                    batch = [b'{"type": "ping"}\n']  # notice a dead connection early
                for message in batch:
                    sock.sendall(message)
            except OSError as exc:
                self._on_status(f"TV connection lost — reconnecting ({exc})", False)
                try:
                    sock.close()
                except OSError:
                    pass
                sock = None
        if sock:
            try:
                sock.sendall(b'{"type": "clear_all"}\n')
                sock.close()
            except OSError:
                pass
        self._on_status("TV disconnected", False)
