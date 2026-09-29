"""Settings → Doctor (PLAN.md A16.d.02): run the doctor at the press of a button.

The doctor runs on a worker thread, so the window never waits on it; the report comes back by a
Qt signal. It checks everything in omnibots/doctor/layout.json and repairs what it safely can.
"""

from __future__ import annotations

import threading
from html import escape
from pathlib import Path

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import QCheckBox, QHBoxLayout, QLabel, QPushButton, QTextBrowser, QVBoxLayout, QWidget

from omnibots.ui import theme

COLORS = {"ok": theme.GREEN, "fixed": theme.GREEN, "warn": theme.AMBER, "fail": theme.RED}
MARKS = {"ok": "✔", "fixed": "🔧", "warn": "⚠", "fail": "✖"}


class _Relay(QObject):
    done = Signal(object)


def report_html(report: dict, *, all_lines: bool = False) -> str:
    c = report.get("counts", {})
    mode = report.get("mode")
    where = {"omni": "Providers come from Omni (shared by both apps).",
             "standalone": "Omni isn't installed: providers come from OmniBots' own config, keys encrypted."}.get(mode, "")
    rows = [f"<p><b>{c.get('ok', 0)} ok · {c.get('fixed', 0)} repaired · {c.get('warn', 0)} warnings · "
            f"{c.get('fail', 0)} problems</b><br><span style='color:{theme.TEXT_DIM}'>{escape(where)}</span></p>"]
    shown = [f for f in report.get("findings", []) if all_lines or f["status"] != "ok"]
    if not shown:
        rows.append(f"<p style='color:{theme.GREEN}'>✔ Everything checks out.</p>")
    for f in shown:
        hint = f"<br><span style='color:{theme.TEXT_DIM}'>→ {escape(f['hint'])}</span>" if f.get("hint") else ""
        rows.append(f"<p style='margin:4px 0'><span style='color:{COLORS[f['status']]}'>{MARKS[f['status']]}</span> "
                    f"<span style='color:{theme.TEXT_DIM}'>[{escape(f['group'])}]</span> {escape(f['message'])}{hint}</p>")
    return "".join(rows)


class DoctorPage(QWidget):
    def __init__(self, engine=None, home: Path | None = None):
        super().__init__()
        self.engine, self.home = engine, home
        self.last: dict | None = None
        self._relay = _Relay()
        self._relay.done.connect(self._show)

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16)
        title = QLabel("Doctor")
        title.setStyleSheet("font-size: 16px; font-weight: 600;")
        root.addWidget(title)
        note = QLabel("Checks everything OmniBots and Omni need to work together: folders in ~/.omnibots and "
                      "AppData, settings, the database, Python packages, the provider config and keys, and old "
                      "copies left in other places. It creates and repairs what it safely can, and never deletes "
                      "anything. It never changes Omni's files. You can also ask Omi to call the doctor.")
        note.setWordWrap(True)
        note.setObjectName("dim")
        root.addWidget(note)

        row = QHBoxLayout()
        self.run_btn = QPushButton("Run doctor")
        self.run_btn.setObjectName("primary")
        self.run_btn.clicked.connect(self.run)
        self.fix = QCheckBox("Repair what it can")
        self.fix.setChecked(True)
        self.online = QCheckBox("Test provider keys online")
        self.show_all = QCheckBox("Show passed checks")
        self.show_all.toggled.connect(lambda _on: self.last and self._show(self.last))
        for w in (self.run_btn, self.fix, self.online, self.show_all):
            row.addWidget(w)
        row.addStretch(1)
        root.addLayout(row)

        self.out = QTextBrowser()
        self.out.setOpenLinks(False)
        self.out.setStyleSheet(f"background: {theme.BG2}; border: 1px solid {theme.BORDER}; border-radius: 12px; padding: 8px;")
        self.out.setHtml(f"<p style='color:{theme.TEXT_DIM}'>Press Run doctor.</p>")
        root.addWidget(self.out, 1)

    def run(self) -> None:
        self.run_btn.setEnabled(False)
        self.out.setHtml(f"<p style='color:{theme.TEXT_DIM}'>Checking…</p>")
        fix, online = self.fix.isChecked(), self.online.isChecked()
        threading.Thread(target=self._work, args=(fix, online), name="doctor", daemon=True).start()

    def _work(self, fix: bool, online: bool) -> None:
        try:
            if self.engine is not None and getattr(self.engine, "submit", None) and getattr(self.engine, "run_doctor", None):
                report = self.engine.submit(self.engine.run_doctor(fix=fix, online=online)).result(timeout=300)
            else:
                from omnibots.doctor import run_doctor
                report = run_doctor(home=self.home, fix=fix, online=online).to_dict()
        except Exception as exc:
            report = {"counts": {"fail": 1}, "findings": [{"id": "doctor", "group": "doctor", "status": "fail",
                                                            "message": f"the doctor could not run: {type(exc).__name__}: {exc}",
                                                            "hint": ""}]}
        self._relay.done.emit(report)

    def _show(self, report: dict) -> None:
        self.last = report
        self.out.setHtml(report_html(report, all_lines=self.show_all.isChecked()))
        self.run_btn.setEnabled(True)
