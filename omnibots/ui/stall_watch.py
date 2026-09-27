"""Catch UI freezes in the act (live bug 2026-09-26: "the app freezes for 7 to 10 sec").

A heartbeat timer on the UI thread; a watchdog thread notices when it stops beating for more than
`limit` seconds and writes the UI thread's stack to logs/ui-stalls.log, so a freeze is never a
mystery again: the log says exactly which line the window was stuck on.
"""

from __future__ import annotations

import sys
import threading
import time
import traceback
from pathlib import Path

from PySide6.QtCore import QTimer


class StallWatch:
    def __init__(self, log_file: Path, limit: float = 1.0):
        self.log_file, self.limit = Path(log_file), limit
        self.beat = time.monotonic()
        self.stalls = 0
        self._ui = threading.get_ident()
        self._timer = QTimer()
        self._timer.timeout.connect(self._tick)
        self._timer.start(100)
        self._stop = threading.Event()
        threading.Thread(target=self._watch, name="ui-stall-watch", daemon=True).start()

    def _tick(self) -> None:
        self.beat = time.monotonic()

    def _watch(self) -> None:
        reported_for = None
        while not self._stop.wait(0.2):
            stuck = time.monotonic() - self.beat
            if stuck < self.limit:
                reported_for = None
                continue
            if reported_for == self.beat:               # one report per freeze
                continue
            reported_for = self.beat
            frame = sys._current_frames().get(self._ui)
            stack = "".join(traceback.format_stack(frame)[-16:]) if frame else "(no stack)\n"
            self.stalls += 1
            try:
                with self.log_file.open("a", encoding="utf-8") as f:
                    f.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} the window froze for over {stuck:.1f}s here:\n{stack}")
            except OSError:
                pass

    def stop(self) -> None:
        self._stop.set()
        self._timer.stop()
