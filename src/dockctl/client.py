"""Talk to the daemon over its Unix socket (one JSON request per connection)."""

from __future__ import annotations

import json
import socket
from typing import Any, Iterator

from .daemon import SOCKET_PATH


class NotRunning(RuntimeError):
    pass


def _connect(timeout: float | None) -> socket.socket:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(str(SOCKET_PATH))
    except (FileNotFoundError, ConnectionRefusedError):
        s.close()
        raise NotRunning("daemon not running — start it with `dockctl daemon start`") from None
    return s


def request(cmd: str, _timeout: float | None = 10, **kw: Any) -> dict[str, Any]:
    """Send one command. _timeout is the socket timeout; everything in kw goes to the daemon."""
    s = _connect(_timeout)
    try:
        s.sendall((json.dumps({"cmd": cmd, **kw}) + "\n").encode())
        f = s.makefile("rb")
        line = f.readline()
        if not line:
            raise RuntimeError("daemon closed the connection")
        return json.loads(line)
    finally:
        s.close()


def running() -> bool:
    try:
        request("status", _timeout=2)
        return True
    except (NotRunning, OSError, RuntimeError):
        return False


def subscribe() -> Iterator[dict[str, Any]]:
    s = _connect(None)
    try:
        s.sendall(b'{"cmd": "subscribe"}\n')
        f = s.makefile("rb")
        first = f.readline()
        if not first:
            return
        for line in f:
            yield json.loads(line)
    finally:
        s.close()
