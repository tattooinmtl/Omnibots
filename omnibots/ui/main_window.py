"""Main window, A0 placeholder. The real panels (A11) replace the body later."""

from __future__ import annotations

import sys

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QMainWindow, QVBoxLayout, QWidget

from omnibots import __version__


class MainWindow(QMainWindow):
    def __init__(self, home: str):
        super().__init__()
        self.setWindowTitle("OmniBots")
        self.resize(900, 600)
        body = QWidget()
        layout = QVBoxLayout(body)
        title = QLabel(f"<h1>OmniBots</h1><p>v{__version__}: foundations (A0)</p>")
        title.setTextFormat(Qt.TextFormat.RichText)
        self.status = QLabel("Starting engine…")
        self.status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        home_label = QLabel(f"Home: {home}")
        home_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.omni = QLabel("Omni: loading…")
        self.omni.setTextFormat(Qt.TextFormat.RichText)
        self.omni.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        for w in (title, self.status, home_label, self.omni):
            layout.addWidget(w)
        layout.addStretch(1)
        self.setCentralWidget(body)

    def set_status(self, text: str) -> None:
        self.status.setText(text)

    def set_omni(self, summary: dict) -> None:
        """Show the Omni bridge state. Keys are masked upstream; never shown here."""
        from html import escape

        if not summary.get("ok") and "providers" not in summary:
            self.omni.setText(f"<b style='color:#b00'>Omni not available:</b> {escape(summary.get('error', ''))}")
            return
        rows = []
        for name, p in summary["providers"].items():
            if p["error"]:
                state = f"<span style='color:#b00'>⚠ {escape(p['error'])}</span>"
            elif name in summary.get("with_key", []):
                state = f"<span style='color:#080'>✔ key {escape(p['key'])} ({escape(p['key_source'])})</span>"
            else:
                state = "<span style='color:#b60'>no key: set it in Omni with /apikey</span>"
            rows.append(f"<tr><td><b>{escape(name)}</b>&nbsp;&nbsp;</td><td>{state}</td></tr>")
        missing = "".join(f"<tr><td><b>{escape(n)}</b></td><td style='color:#b00'>missing from Omni</td></tr>" for n in summary["missing_providers"])
        skills = summary["skills"]
        errors = "".join(f"<li>{escape(e)}</li>" for e in summary.get("errors", []))
        self.omni.setText(
            f"<h3>Omni providers</h3><p>{escape(summary['omni_install_root'])}</p>"
            f"<table>{''.join(rows)}{missing}</table>"
            f"<p>{summary['models']} models · {skills['bundled']} bundled + {skills['external']} external skills · "
            f"MCP: {escape(', '.join(summary['mcp_servers']) or 'none')}</p>"
            + (f"<p style='color:#b60'>Warnings:</p><ul>{errors}</ul>" if errors else "")
        )

    def bring_to_front(self) -> None:
        if self.isMinimized():
            self.showNormal()
        self.show()
        self.raise_()
        self.activateWindow()
        if sys.platform == "win32":
            _force_foreground(int(self.winId()))


def _force_foreground(hwnd: int) -> bool:
    """Bring hwnd to the front despite Windows' focus-stealing rules.

    Windows refuses SetForegroundWindow from a background process while the
    user is typing elsewhere. Briefly attaching our input thread to the
    foreground window's thread is the standard, documented workaround. If
    Windows still refuses, flash the taskbar button so the user notices.
    """
    import ctypes
    from ctypes import wintypes

    user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
    if user32.SetForegroundWindow(hwnd):
        return True
    fg = user32.GetForegroundWindow()
    fg_thread = user32.GetWindowThreadProcessId(fg, None)
    our_thread = kernel32.GetCurrentThreadId()
    attached = fg_thread and fg_thread != our_thread and user32.AttachThreadInput(our_thread, fg_thread, True)
    try:
        user32.BringWindowToTop(hwnd)
        ok = bool(user32.SetForegroundWindow(hwnd))
    finally:
        if attached:
            user32.AttachThreadInput(our_thread, fg_thread, False)
    if not ok:
        class FLASHWINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.UINT), ("hwnd", wintypes.HWND), ("dwFlags", wintypes.DWORD),
                        ("uCount", wintypes.UINT), ("dwTimeout", wintypes.DWORD)]
        FLASHW_ALL, FLASHW_TIMERNOFG = 0x3, 0xC
        info = FLASHWINFO(ctypes.sizeof(FLASHWINFO), hwnd, FLASHW_ALL | FLASHW_TIMERNOFG, 0, 0)
        user32.FlashWindowEx(ctypes.byref(info))
    return ok
