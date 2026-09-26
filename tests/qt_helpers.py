"""One offscreen Qt app for UI tests, WITHOUT touching os.environ: setting QT_QPA_PLATFORM
at import time leaked into every subprocess pytest started later (the A0 tests launch the
real app and look for its real window)."""

from __future__ import annotations


def qapp():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication(["pytest", "-platform", "offscreen"])
