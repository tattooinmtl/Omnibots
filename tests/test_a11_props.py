"""A11.d.05 (user, 2026-09-26): action extras + job badges must NEVER cover Omi's face.
Checked pixel by pixel: render with and without each prop/badge, and nothing inside the face
screen may change. Plus the mappings, the legend and the message board panel."""

from __future__ import annotations

import os

from qt_helpers import qapp  # noqa: E402

import pytest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from omnibots.ui.omi_face import render_face  # noqa: E402
from omnibots.ui.props import ACTIONS, JOBS, action_for_tool, job_for_role  # noqa: E402

app = qapp()

SIZE = 240                                  # 1 px = 1 unit of Omi's box
SCREEN = (60, 90, 180, 182)                 # the face screen (eyes + mouth), slightly inset


def changed_pixels(a, b, box) -> int:
    x0, y0, x1, y1 = box
    n = 0
    for y in range(y0, y1, 2):
        for x in range(x0, x1, 2):
            if a.pixel(x, y) != b.pixel(x, y):
                n += 1
    return n


@pytest.mark.parametrize("action", list(ACTIONS))
@pytest.mark.parametrize("t", [0.0, 0.4, 0.9, 1.3, 2.1, 3.7])          # props animate: check through the motion
def test_action_extras_never_cover_the_face(action, t):
    base = render_face(SIZE, "happy", t=t)
    with_prop = render_face(SIZE, "happy", t=t, prop=action)
    assert changed_pixels(base, with_prop, (0, 0, SIZE, SIZE)) > 20, "the prop should be visible"
    assert changed_pixels(base, with_prop, SCREEN) == 0, f"{action} covers the face at t={t}"


@pytest.mark.parametrize("job", list(JOBS))
def test_job_badges_sit_in_the_top_left_corner_only(job):
    base = render_face(SIZE, "happy", t=0.6)
    with_badge = render_face(SIZE, "happy", t=0.6, badge=job)
    assert changed_pixels(base, with_badge, (0, 0, 60, 60)) > 20
    assert changed_pixels(base, with_badge, (60, 0, SIZE, SIZE)) == 0        # nothing outside the corner
    assert changed_pixels(base, with_badge, (0, 60, 60, SIZE)) == 0


def test_role_to_job_and_tool_to_action():
    assert job_for_role("boss") == "boss" and job_for_role("Senior Python developer") == "coder"
    assert job_for_role("market researcher") == "researcher" and job_for_role("blog writer") == "writer"
    assert job_for_role("night shift") == "night" and job_for_role("something odd") == "worker"
    assert action_for_tool("write_file", {"path": "app.py"}) == "coding"
    assert action_for_tool("write_file", {"path": "REPORT.md"}) == "writing"
    assert action_for_tool("read_file") == "reading" and action_for_tool("git_push") == "deploying"
    assert action_for_tool("browser_navigate") == "browsing" and action_for_tool("nope") is None


def test_settings_legend_lists_every_badge_and_extra():
    from PySide6.QtWidgets import QLabel
    from omnibots.ui.settings_window import SettingsWindow
    w = SettingsWindow()
    texts = " ".join(l.text() for l in w.findChildren(QLabel))
    assert all(title.split(" (")[0].split(" /")[0] in texts for title, _ in JOBS.values())
    assert all(label in texts for label, _ in ACTIONS.values())
    assert "investigat," not in texts                                          # stems shown as words


def test_board_panel_filters():
    from omnibots.ui.widgets import BoardEntry, BoardPanel
    b = BoardPanel("b1")
    b.show()
    entries = [BoardEntry("1", "omi", "Omi", "boss", "b1", "Coder", "A2A_MESSAGE", "use /site"),
               BoardEntry("2", "b2", "Scout", "researcher", "omi", "Omi", "CLAIM_SUBMITTED", "claim"),
               BoardEntry("3", "b4", "Nightly", "devops", None, None, "APPROVAL_REQUEST", "push?"),
               BoardEntry("4", "b1", "Coder", "coder", "omi", "Omi", "HELP_REQUEST", "which folder?")]
    for e in entries:
        b.add(e)
    visible = lambda: [e.time for e, w in b.entries if w.isVisible()]        # noqa: E731
    assert visible() == ["1", "2", "3", "4"]
    b.set_filter("This bot")
    assert visible() == ["1", "4"]
    b.set_filter("Claims")
    assert visible() == ["2"]
    b.set_filter("Approvals")
    assert visible() == ["3"]
    b.set_filter("Problems")
    assert visible() == ["4"]
