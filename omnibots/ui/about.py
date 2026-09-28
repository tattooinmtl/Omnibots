"""Help → About OmniBots (the title bar's "About", the tray): version, build, update check, credits."""

from __future__ import annotations

import threading

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QPixmap
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout

from omnibots import version as v
from omnibots.ui import theme
from omnibots.ui.omi_face import render_face


class AboutWindow(QDialog):
    _checked = Signal(object)                        # the version on GitHub (or None), from a worker thread

    def __init__(self, parent=None, latest=v.latest, update=None, layout=None):
        super().__init__(parent)
        self.latest_fn = latest
        self.update_fn = update                         # A16.d: () -> None, the app's "update now" (None: not offered)
        from omnibots.updater import install_layout
        self.layout = layout or install_layout()
        self.setWindowTitle("About OmniBots")
        self.setStyleSheet(theme.stylesheet() + f"QDialog {{ background: {theme.BG0}; }}")
        self.setMinimumWidth(520)
        root = QVBoxLayout(self)
        root.setContentsMargins(26, 24, 26, 20)
        root.setSpacing(10)
        top = QHBoxLayout()
        face = QLabel()
        face.setPixmap(QPixmap.fromImage(render_face(96, "happy")))
        top.addWidget(face)
        head = QVBoxLayout()
        name = QLabel("OmniBots")
        name.setStyleSheet("font-size: 26px; font-weight: 700;")
        self.version = QLabel(f"Version <b>{v.VERSION}</b>")
        self.version.setStyleSheet(f"font-size: 15px; color: {theme.ACCENT_CYAN};")
        b = v.build()
        self.build = QLabel(f"Build {b['commit']}{' (' + b['dirty'] + ')' if b.get('dirty') else ''} · {b['date']}" if b else
                            "Build: not a git checkout")
        self.build.setObjectName("dim")
        self.build.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        for w in (name, self.version, self.build):
            head.addWidget(w)
        head.addStretch(1)
        top.addLayout(head, 1)
        root.addLayout(top)
        tag = QLabel("A local team of AI bots: Omi plans your goal, staffs it with worker bots, checks their work, "
                     "and asks you before anything destructive.")
        tag.setWordWrap(True)
        tag.setObjectName("dim")
        root.addWidget(tag)
        row = QHBoxLayout()
        self.check = QPushButton("Check for updates")
        self.check.clicked.connect(self.check_updates)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        row.addWidget(self.check)
        self.update_btn = QPushButton("Update now")
        self.update_btn.setToolTip("Backs up, closes OmniBots, runs the installer, and starts it again")
        self.update_btn.clicked.connect(self.update_now)
        self.update_btn.hide()
        row.addWidget(self.update_btn)
        row.addWidget(self.status, 1)
        root.addLayout(row)
        credits = QLabel(f"Made by <b>{v.AUTHOR}</b> · <a style='color:{theme.ACCENT_CYAN}' href='mailto:{v.EMAIL}'>{v.EMAIL}</a><br>"
                         f"<a style='color:{theme.ACCENT_CYAN}' href='{v.WEBSITE}'>omnibots.globalwarningnetworks.com</a> · "
                         f"<a style='color:{theme.ACCENT_CYAN}' href='https://github.com/{v.REPO}'>GitHub</a><br>"
                         f"<span style='color:{theme.TEXT_DIM}'>{v.COPYRIGHT}</span>")
        credits.setTextFormat(Qt.TextFormat.RichText)
        credits.setOpenExternalLinks(True)
        credits.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        root.addSpacing(6)
        root.addWidget(credits)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        root.addWidget(close, 0, Qt.AlignmentFlag.AlignRight)
        self._checked.connect(self._show_result)

    def check_updates(self) -> None:
        """Ask GitHub in the background (never freezes the window)."""
        self.check.setEnabled(False)
        self.status.setText("Checking GitHub…")
        threading.Thread(target=lambda: self._checked.emit(self.latest_fn()), daemon=True).start()

    def _show_result(self, remote) -> None:
        self.check.setEnabled(True)
        if remote is None:
            self.status.setText(f"<span style='color:{theme.AMBER}'>Couldn't reach GitHub. Try again later.</span>")
        elif v.newer(remote, v.VERSION):
            if self.layout.get("kind") == "installer" and self.update_fn is not None:
                self.status.setText(f"<span style='color:{theme.GREEN}'>Version {remote} is out.</span>")
                self.update_btn.show()
            else:
                self.status.setText(f"<span style='color:{theme.GREEN}'>Version {remote} is out.</span> "
                                    "Update: <code>git pull</code> then <code>pip install -r requirements.lock</code>")
        elif v.newer(v.VERSION, remote):
            self.status.setText(f"You're ahead of GitHub ({remote}): this copy has changes not pushed yet.")
        else:
            self.status.setText(f"<span style='color:{theme.GREEN}'>✔ You're up to date ({v.VERSION}).</span>")


    def update_now(self) -> None:
        self.update_btn.setEnabled(False)
        self.status.setText("Getting ready to update…")
        self.update_fn()

def open_about(parent=None, update=None) -> AboutWindow:
    w = AboutWindow(parent, update=update)
    w.show()
    return w
