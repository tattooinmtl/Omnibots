"""Where the bots' projects go (PLAN.md A11.m.01): asked on the first start, changed in
Settings → Folders. Default C:\\omnibots_output; if the C: root can't be written (no rights),
%USERPROFILE%\\OmniBots Output instead."""

from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QFileDialog, QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout, QWidget,
)

from omnibots.ui import theme

PREFERRED = Path("C:/omnibots_output") if os.name == "nt" else Path.home() / "omnibots_output"
FALLBACK = Path.home() / "OmniBots Output"


def usable(folder: Path) -> str | None:
    """None if the bots can write there (the folder is created); otherwise why not."""
    try:
        folder = Path(folder)
        if not folder.is_absolute():
            return "use a full path, like C:\\omnibots_output"
        folder.mkdir(parents=True, exist_ok=True)
        probe = folder / ".omnibots-write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return None
    except OSError as exc:
        return f"can't write there ({exc.strerror or exc})"


def could_create(folder: Path) -> bool:
    """Could the bots use this folder? Checked WITHOUT leaving it behind (a probe folder next to it,
    removed at once). An existing folder is probed inside."""
    import uuid
    folder = Path(folder)
    if folder.is_dir():
        return usable(folder) is None
    parent = next((p for p in folder.parents if p.is_dir()), None)
    if parent is None:
        return False
    probe = parent / f".omnibots-probe-{uuid.uuid4().hex[:8]}"
    try:
        probe.mkdir()
        probe.rmdir()
        return True
    except OSError:
        return False


def default_output_folder() -> Path:
    return PREFERRED if could_create(PREFERRED) else FALLBACK


class FolderPicker(QWidget):
    """A path box + Browse…"""

    def __init__(self, start: Path, parent=None):
        super().__init__(parent)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        self.edit = QLineEdit(str(start))
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        row.addWidget(self.edit, 1)
        row.addWidget(browse)

    def _browse(self) -> None:
        p = QFileDialog.getExistingDirectory(self, "Where should the bots' projects go?", self.edit.text())
        if p:
            self.edit.setText(str(Path(p)))

    def folder(self) -> Path:
        return Path(self.edit.text().strip().strip('"'))


class OutputFolderDialog(QDialog):
    """First start: 'Where should the bots put their work?'"""

    def __init__(self, start: Path | None = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("OmniBots · Output folder")
        self.setStyleSheet(theme.stylesheet() + f"QDialog {{ background: {theme.BG0}; }}")
        self.setMinimumWidth(560)
        v = QVBoxLayout(self)
        v.setContentsMargins(20, 18, 20, 16)
        title = QLabel("Where should the bots put their work?")
        title.setStyleSheet("font-size: 18px; font-weight: 600;")
        v.addWidget(title)
        note = QLabel("Every goal gets its own folder here, named with the date and the goal "
                      "(e.g. “2026-09-26 OmniBots showcase site”). You can change this later in Settings → Folders, "
                      "and open any other folder from the File Explorer.")
        note.setWordWrap(True)
        note.setObjectName("dim")
        v.addWidget(note)
        self.picker = FolderPicker(start or default_output_folder())
        v.addWidget(self.picker)
        self.error = QLabel("")
        self.error.setStyleSheet(f"color: {theme.RED};")
        v.addWidget(self.error)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok)
        buttons.accepted.connect(self._ok)
        ok = buttons.button(QDialogButtonBox.StandardButton.Ok)
        ok.setDefault(True)                                   # Enter in the path box = OK
        self.picker.edit.returnPressed.connect(self._ok)
        v.addWidget(buttons)
        self.chosen: Path | None = None

    def _ok(self) -> None:
        folder = self.picker.folder()
        why = usable(folder)
        if why:
            self.error.setText(f"✖ {why}. Pick another folder.")
            return
        self.chosen = folder
        self.accept()


def ask_output_folder(start: Path | None = None) -> Path:
    """Show the dialog; closing it without choosing uses the default (the app still needs a folder)."""
    d = OutputFolderDialog(start)
    d.exec()
    return d.chosen or default_output_folder()
