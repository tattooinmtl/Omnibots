"""Keep Windows awake while jobs are running (PLAN.md A0.c.03, §4.5).

SetThreadExecutionState is per-thread: the request lasts as long as the
calling thread lives and doesn't reset it. So acquire/release must always run
on the same long-lived thread; the engine thread owns this object.
Reference-counted: the PC may sleep again only when the last job releases.
"""

from __future__ import annotations

import ctypes
import logging
import sys
import threading

log = logging.getLogger(__name__)

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001


class KeepAwake:
    def __init__(self, enabled: bool = True):
        self.enabled = enabled and sys.platform == "win32"
        self._count = 0
        self._owner: int | None = None

    @property
    def active(self) -> bool:
        return self._count > 0

    def _set(self, flags: int) -> bool:
        if not self.enabled:
            return False
        prev = ctypes.windll.kernel32.SetThreadExecutionState(flags)
        if prev == 0:
            log.warning("SetThreadExecutionState failed")
            return False
        return True

    def _check_thread(self) -> None:
        tid = threading.get_ident()
        if self._owner is None:
            self._owner = tid
        elif self._owner != tid:
            raise RuntimeError("KeepAwake must be used from one thread (the engine thread)")

    def acquire(self) -> None:
        self._check_thread()
        self._count += 1
        if self._count == 1 and self._set(ES_CONTINUOUS | ES_SYSTEM_REQUIRED):
            log.info("keep-awake ON (jobs running)")

    def release(self) -> None:
        self._check_thread()
        if self._count == 0:
            return
        self._count -= 1
        if self._count == 0 and self._set(ES_CONTINUOUS):
            log.info("keep-awake OFF (idle)")

    def release_all(self) -> None:
        if self._count:
            self._count = 1
            self.release()
