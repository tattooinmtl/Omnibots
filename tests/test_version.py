"""Version control (user, 2026-09-26): one version number, shown in About and on the website, checked against GitHub."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

from qt_helpers import qapp

from omnibots import __version__
from omnibots import version as v

ROOT = Path(__file__).resolve().parents[1]
app = qapp()


def test_one_version_everywhere():
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    assert pyproject == __version__ == v.VERSION                            # bump both together
    site = (ROOT / "website" / "index.html").read_text(encoding="utf-8")
    assert f"v{__version__}" in site                                       # the website's fallback number


def test_version_helpers():
    assert v.parse_version('__version__ = "1.2.3"\n') == "1.2.3" and v.parse_version("nothing") is None
    assert v.newer("0.10.0", "0.9.9") and v.newer("1.0.0", "0.2.0") and not v.newer("0.2.0", "0.2.0")
    assert v.version_line().startswith(f"OmniBots v{__version__}")
    assert v.AUTHOR == "Erik Boivin" and v.EMAIL == "erik.boivin@proton.me" and "omnibots.globalwarningnetworks.com" in v.COPYRIGHT


def test_about_shows_version_credits_and_checks_for_updates():
    from omnibots.ui.about import AboutWindow
    w = AboutWindow(latest=lambda: "9.9.9")
    labels = " ".join(l.text() for l in w.findChildren(type(w.version)))
    assert __version__ in w.version.text() and "Erik Boivin" in labels and "erik.boivin@proton.me" in labels
    assert "omnibots.globalwarningnetworks.com" in labels
    w._show_result("9.9.9")
    assert "9.9.9 is out" in w.status.text()
    w._show_result(__version__)
    assert "up to date" in w.status.text()
    w._show_result(None)
    assert "Couldn't reach GitHub" in w.status.text()
    assert re.search(r"Build \w+|not a git checkout", w.build.text())
