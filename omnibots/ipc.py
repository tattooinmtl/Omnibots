"""Single instance + control channel over a named pipe (ADR-2).

- A QLockFile in the home folder decides who is the primary instance (no race
  when two launches start at the same time).
- The primary listens on a QLocalServer (a named pipe on Windows).
- Anyone else connects, sends one JSON line, reads one JSON line back.

Protocol (one JSON object per line):
  -> {"cmd": "show"}              <- {"ok": true}
  -> {"cmd": "status"}            <- {"ok": true, "status": {...}}
  -> {"cmd": "goal", "text": ...} <- {"ok": false, "error": "not implemented yet"} (until A7)
  -> {"cmd": "stop"}              <- {"ok": true}
Phase B (the Omni plugin) uses this same channel.
"""

from __future__ import annotations

import getpass
import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any, Callable

from PySide6.QtCore import QLockFile, QObject
from PySide6.QtNetwork import QLocalServer, QLocalSocket

log = logging.getLogger(__name__)

Handler = Callable[[dict[str, Any]], dict[str, Any]]


def pipe_name() -> str:
    # Per Windows user, so two users on one PC never collide. OMNIBOTS_PIPE
    # overrides it (tests use their own pipe so they never touch a real instance).
    override = os.environ.get("OMNIBOTS_PIPE", "").strip()
    if override:
        return override
    user = re.sub(r"[^A-Za-z0-9_.-]", "_", getpass.getuser())
    return f"omnibots-{user}"


def allow_foreground_handoff() -> None:
    """Let the primary instance bring its window to the front (Windows focus rules)."""
    if sys.platform == "win32":
        import ctypes

        ctypes.windll.user32.AllowSetForegroundWindow(-1)  # ASFW_ANY


def send_command(cmd: dict[str, Any], *, name: str | None = None, timeout_ms: int = 3000) -> dict[str, Any] | None:
    """Client side. Returns the reply, or None when no instance is listening."""
    sock = QLocalSocket()
    sock.connectToServer(name or pipe_name())
    if not sock.waitForConnected(timeout_ms):
        return None
    if cmd.get("cmd") == "show":
        allow_foreground_handoff()
    sock.write((json.dumps(cmd) + "\n").encode("utf-8"))
    sock.flush()
    sock.waitForBytesWritten(timeout_ms)
    buf = b""
    while b"\n" not in buf:
        if not sock.waitForReadyRead(timeout_ms):
            break
        buf += bytes(sock.readAll())
    sock.disconnectFromServer()
    if not buf.strip():
        return {"ok": False, "error": "no reply"}
    return json.loads(buf.split(b"\n", 1)[0].decode("utf-8"))


class SingleInstance(QObject):
    def __init__(self, lock_path: Path, handlers: dict[str, Handler], name: str | None = None):
        super().__init__()
        self.name = name or pipe_name()
        self.handlers = handlers
        self._lock = QLockFile(str(lock_path))
        self._lock.setStaleLockTime(0)  # a crashed owner's lock is detected by PID instead of age
        self._server: QLocalServer | None = None

    def acquire(self) -> bool:
        """True if we are the primary instance (and now listening)."""
        if not self._lock.tryLock(200):
            self._lock.removeStaleLockFile()
            if not self._lock.tryLock(200):
                return False
        QLocalServer.removeServer(self.name)  # clear a pipe left by a crash
        self._server = QLocalServer(self)
        self._server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
        if not self._server.listen(self.name):
            log.error("could not listen on pipe %s: %s", self.name, self._server.errorString())
            return True  # we still own the lock; run without the control channel
        self._server.newConnection.connect(self._on_connection)
        log.info("listening on pipe %s", self.name)
        return True

    def release(self) -> None:
        if self._server:
            self._server.close()
            self._server = None
        if self._lock.isLocked():
            self._lock.unlock()

    def _on_connection(self) -> None:
        assert self._server is not None
        while self._server.hasPendingConnections():
            sock = self._server.nextPendingConnection()
            sock.setProperty("buf", b"")
            sock.readyRead.connect(lambda s=sock: self._on_ready(s))
            sock.disconnected.connect(sock.deleteLater)

    def _on_ready(self, sock: QLocalSocket) -> None:
        buf = (sock.property("buf") or b"") + bytes(sock.readAll())
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            reply = self._dispatch(line)
            sock.write((json.dumps(reply) + "\n").encode("utf-8"))
            sock.flush()
        sock.setProperty("buf", buf)

    def _dispatch(self, line: bytes) -> dict[str, Any]:
        try:
            msg = json.loads(line.decode("utf-8"))
            cmd = msg.get("cmd")
        except Exception:
            return {"ok": False, "error": "bad json"}
        handler = self.handlers.get(cmd)
        if handler is None:
            return {"ok": False, "error": f"unknown command: {cmd}"}
        try:
            return handler(msg)
        except Exception as exc:
            log.exception("ipc command %s failed", cmd)
            return {"ok": False, "error": str(exc)}
