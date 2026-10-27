"""Settings → Startup: start OmniBots when the user signs in to Windows, on or off (A11.q.01)."""

from __future__ import annotations

from html import escape

from PySide6.QtWidgets import QCheckBox, QLabel, QVBoxLayout, QWidget

from omnibots import autostart
from omnibots.ui import theme


class StartupPage(QWidget):
    def __init__(self, *, key: str = autostart.RUN_KEY, name: str = autostart.NAME):
        super().__init__()
        self.key, self.name = key, name
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16)
        title = QLabel("Startup")
        title.setStyleSheet("font-size: 16px; font-weight: 600;")
        root.addWidget(title)
        note = QLabel("Start OmniBots by itself when you sign in to Windows, so routines and bots are ready. "
                      "This adds one entry for your Windows user only (Task Manager → Startup apps shows it "
                      "as OmniBots); turning it off removes it.")
        note.setWordWrap(True)
        note.setObjectName("dim")
        root.addWidget(note)
        self.on = QCheckBox("Start OmniBots when I sign in to Windows")
        self.minimized = QCheckBox("Start quietly in the tray (no intro, no window)")
        root.addWidget(self.on)
        root.addWidget(self.minimized)
        self.status = QLabel()
        self.status.setWordWrap(True)
        root.addWidget(self.status)
        root.addStretch(1)
        self.reload()
        self.on.toggled.connect(lambda _c: self.apply())
        self.minimized.toggled.connect(lambda _c: self.apply())

    def reload(self) -> None:
        enabled = autostart.is_enabled(self.key, self.name)
        for box in (self.on, self.minimized):
            box.blockSignals(True)
        self.on.setChecked(enabled)
        self.minimized.setChecked(autostart.is_minimized(self.key, self.name) if enabled else True)
        self.minimized.setEnabled(enabled)
        for box in (self.on, self.minimized):
            box.blockSignals(False)
        self.status.clear()

    def apply(self) -> None:
        try:
            if self.on.isChecked():
                autostart.enable(self.minimized.isChecked(), self.key, self.name)
                self._say("On: OmniBots starts when you sign in" +
                          (", in the tray." if self.minimized.isChecked() else ", with Omi's window."))
            else:
                autostart.disable(self.key, self.name)
                self._say("Off: OmniBots won't start by itself.")
        except OSError as exc:
            self._say(f"Windows refused the change: {exc.strerror or exc}", bad=True)
        self.minimized.setEnabled(self.on.isChecked())

    def _say(self, text: str, *, bad: bool = False) -> None:
        color = theme.RED if bad else theme.GREEN
        mark = "✖" if bad else "✔"
        self.status.setText(f"<span style='color:{color}'>{mark} {escape(text)}</span>")
