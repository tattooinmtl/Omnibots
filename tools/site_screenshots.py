"""Screenshots for the install website (website/assets/img), rendered with demo data.

  python tools/site_screenshots.py            (uses the real Windows renderer: run it on a desktop)

Paths shown are neutral demo paths, never the real temp folders (they carry the user's name).
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QCoreApplication, QElapsedTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

app = QApplication.instance() or QApplication(sys.argv[:1])

from omnibots.ui import theme  # noqa: E402
from omnibots.ui.bot_window import BotWindow  # noqa: E402
from omnibots.ui.widgets import BoardEntry, BotCard, ChatMessage  # noqa: E402

OUT = ROOT / "website" / "assets" / "img"
DEMO = r"C:\omnibots_output\2026-09-26 omnibots showcase site"
TEAM = [("omi", "Omi", "boss", "happy"), ("fe", "Frontend", "coder", "working"), ("ds", "Designer", "designer", "happy"),
        ("qa", "QA", "tester", "thinking"), ("sc", "Scout", "researcher", "happy")]


def pump(ms: int) -> None:
    t = QElapsedTimer()
    t.start()
    while t.elapsed() < ms:
        QCoreApplication.processEvents()


def site_folder() -> Path:
    ws = Path(tempfile.mkdtemp(prefix="obsite-"))
    (ws / "assets" / "css").mkdir(parents=True)
    (ws / "assets" / "img").mkdir(parents=True)
    (ws / "attachments").mkdir()
    (ws / "index.html").write_text(
        "<!doctype html>\n<html lang=\"en\">\n<head>\n  <meta charset=\"utf-8\">\n  <title>OmniBots</title>\n"
        "  <link rel=\"stylesheet\" href=\"assets/css/style.css\">\n</head>\n<body>\n  <header class=\"hero\">\n"
        "    <h1>One team of bots.</h1>\n    <p>Plan, build and check any job, together.</p>\n"
        "    <a class=\"btn\" href=\"#install\">Install OmniBots</a>\n  </header>\n</body>\n</html>\n", encoding="utf-8")
    (ws / "assets" / "css" / "style.css").write_text(":root { --bg: #070b18; --cyan: #6fe3ff; }\nbody { background: var(--bg); }\n",
                                                     encoding="utf-8")
    (ws / "README.md").write_text("# OmniBots showcase site\n", encoding="utf-8")
    (ws / "GOAL.md").write_text("# Goal\n\nA website to showcase OmniBots.\n", encoding="utf-8")
    (ws / "attachments" / "brand-brief.pdf").write_bytes(b"%PDF-1.4")
    return ws


def window(ws: Path, bot: str = "Omi", role: str = "boss", status: str = "Working") -> BotWindow:
    card = BotCard(name=bot, role=role, status=status, tagline="Percolating…", seat="MiniMax seat 1",
                   model="minimax.io/m3", usage_pct=18.0, accent=theme.role_color(role))
    w = BotWindow(card, ws, team=TEAM, bot_id="omi")
    w.resize(1536, 1000)
    for bid, name, r, st, act in (("omi", "Omi", "boss", "working", "reviewing Frontend's claim"),
                                  ("fe", "Frontend", "coder", "working", "coding index.html"),
                                  ("ds", "Designer", "designer", "idle", "idle"),
                                  ("qa", "QA", "tester", "working", "looking at a screenshot")):
        w.board.set_activity(bid, name, r, st, act)
    for e in (BoardEntry("10:02", "omi", "Omi", "boss", "fe", "Frontend", "TASK_ASSIGNED", "Build index.html with the hero and install section"),
              BoardEntry("10:09", "fe", "Frontend", "coder", "omi", "Omi", "CLAIM_SUBMITTED", "index.html + style.css written; ran the page check"),
              BoardEntry("10:10", "omi", "Omi", "boss", "fe", "Frontend", "CLAIM_ACCEPTED", "Evidence matches the done-criteria."),
              BoardEntry("10:11", "qa", "QA", "tester", "omi", "Omi", "PROGRESS_UPDATE", "Screenshots at 1440 and 390 px look right.")):
        w.board.add(e)
    for line in ("> Loading tools… 24 in the pool", "> Connecting to MiniMax M3… seat 1 booked", "▸ plan_goal: showcase site",
                 "  ↳ 4 jobs planned (html, css, assets, QA)", "▸ find_skills: css html web design",
                 "  ↳ design-taste-frontend, html-js-css", "▸ assign_job: Frontend → index.html", "▸ wait_for_mention",
                 "  ↳ [CLAIM_SUBMITTED from Frontend] index.html + style.css", "▸ accept_claim #4"):
        w.console.append(line)
    for line in ("The user wants a modern dark look. Frontend has the design skills loaded, so it can own the CSS.",
                 "", "QA should check 1440 px and 390 px before I accept the whole goal.",
                 "", "Next: review_work, then report back to the user."):
        w.thinking.append(line)
    return w


def neutral(w: BotWindow) -> None:
    w.files.path_label.setText("🗀  " + DEMO)


def shoot_editor(ws: Path) -> None:
    w = window(ws)
    w.show()
    w.chat.add(ChatMessage("user", "Give the site a modern dark look."))
    w.chat.add(ChatMessage("bot", "On it! Frontend is styling it now; QA checks the result on desktop and phone."))
    w.tabs.open_file(ws / "index.html")
    w.tabs.open_file(ws / "assets" / "css" / "style.css")
    w.tabs.setCurrentIndex(1)
    w.face.set_action("coding")
    pump(900)
    neutral(w)
    pump(150)
    w.grab().save(str(OUT / "editor.png"))
    w.close()


def shoot_approvals(ws: Path) -> None:
    w = window(ws)
    w.show()
    w.chat.add(ChatMessage("user", "Use the attached brand brief for the colors.\n📎 brand-brief.pdf"))
    w.chat.add(ChatMessage("bot", "Got it: the brief is in the project's attachments/ folder. Colors are applied."))
    w.chat.add_approval({"id": "ap_demo", "bot_id": "fe", "tool": "run_shell", "risk": "R5",
                         "summary": "run_shell: rm assets/img/old-logo.png (deletes files)"}, lambda *a: None)
    w.chat.attach(ws / "attachments" / "brand-brief.pdf")
    w.chat.input.setText("and make the buttons glow a little")
    w.face.set_action("waiting")
    pump(900)
    neutral(w)
    pump(150)
    w.grab().save(str(OUT / "approvals.png"))
    w.close()


def shoot_tray() -> None:
    from omnibots.ui.tray import Tray

    class Live:
        def bring_to_front(self):
            pass

        def open_bot(self, b):
            pass

    class Eng:
        signals = None

        def submit(self, coro):
            import asyncio
            import concurrent.futures
            f = concurrent.futures.Future()
            f.set_result(asyncio.run(coro))
            return f

        async def ui_tray(self):
            b = lambda i, g, r, job="": {"id": i, "name": i.capitalize(), "role": r, "group": g, "job": job,
                                        "active": g != "idle", "paused": False, "memory": ""}
            return {"team": "working", "running": True, "paused": False, "interrupted": 0, "seats_used": 3, "seats_total": 4,
                    "tokens_left": 1_480_000_000, "approvals": [{"id": "a", "bot_id": "frontend", "tool": "run_shell", "risk": "R5",
                                                                  "summary": "rm assets/img/old-logo.png"}],
                    "bots": [b("omi", "working", "boss", "the showcase site"), b("frontend", "approval", "coder", "index.html"),
                             b("qa", "thinking", "tester", "check the pages"), b("designer", "idle", "designer")]}
    t = Tray(Eng(), Live(), confirm=lambda *a: False, open_settings=lambda: None, quit_app=lambda: None)
    t.rebuild()
    t.menu.adjustSize()
    t.menu.show()
    pump(300)
    t.menu.grab().save(str(OUT / "tray.png"))
    t.menu.hide()


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    ws = site_folder()
    shoot_editor(ws)
    shoot_approvals(ws)
    shoot_tray()
    print("saved to", OUT)
