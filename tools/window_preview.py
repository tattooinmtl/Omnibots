"""Screenshot a bot window with demo data (for design review).

  python tools/window_preview.py [out.png] [--bubble]
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QPA_FONTDIR", "C:/Windows/Fonts")

from PySide6.QtCore import QCoreApplication, QElapsedTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from omnibots.omni.personality import load  # noqa: E402
from omnibots.ui import theme  # noqa: E402
from omnibots.ui.bot_window import BotWindow  # noqa: E402
from omnibots.ui.taunts import Taunts  # noqa: E402
from omnibots.ui.widgets import BoardEntry, BotCard, ChatMessage  # noqa: E402

CODE = """import pandas as pd
import matplotlib.pyplot as plt

# Load the data
df = pd.read_csv('data.csv')

# Display basic info
print(df.describe())"""


def demo_workspace() -> Path:
    root = Path(tempfile.mkdtemp(prefix="omnibots-demo-")) / "Workspace"
    for d in (".vscode", "data", "docs", "projects/data-analysis/data", "projects/web-app", "scripts", "tests"):
        (root / d).mkdir(parents=True, exist_ok=True)
    for f, body in (("projects/data-analysis/data_analysis.py", CODE), ("projects/data-analysis/requirements.txt", "pandas\n"),
                    ("README.md", "# demo\n"), (".gitignore", "*.pyc\n"), ("pyproject.toml", "[project]\n")):
        (root / f).write_text(body, encoding="utf-8")
    return root


def pump(ms: int) -> None:
    t = QElapsedTimer()
    t.start()
    while t.elapsed() < ms:
        QCoreApplication.processEvents()


def main(out: str, bubble: bool) -> None:
    app = QApplication([])  # noqa: F841
    taunts = Taunts(load(Path.home() / ".omni"), seed=11)
    ws = demo_workspace()
    team = [("omi", "Omi", "boss", "happy"), ("b1", "Coder", "coder", "working"), ("b2", "Scout", "researcher", "thinking"),
            ("b3", "Quill", "writer", "sleepy"), ("b4", "Nightly", "devops", "paused")]
    card = BotCard(name="Omi", role="boss · orchestrator", status="Working", tagline=taunts.status_verb(),
                   seat="MiniMax seat 1 of 4", model="minimax.io/m3", usage_pct=18, accent=theme.ACCENT)
    w = BotWindow(card, ws, team=team)
    for line in ("> Initializing environment…", "> Loading tools… 24 in the pool", "> Connecting to MiniMax M3… seat 1 booked",
                 "> Ready.", "", "▸ write data_analysis.py (212 chars)", "  ↳ created data_analysis.py (8 lines, 212 bytes, ends with a newline)",
                 "$ python data_analysis.py", "[exit 0 · 0.4s]", "", "▸ submit_claim: analysis script ready", "  ↳ claim #3 submitted to Omi"):
        w.console.append(line)
    w.thinking.append("The user wants a quick analysis script. A CSV loader plus describe() covers the first pass; "
                      "plots can come after they confirm the columns.\n\nI'll write it into the project, run it in the "
                      "sandbox, then claim it with the real run as evidence.")
    w.chat.add(ChatMessage("bot", "I can help you with coding, analysis, research, file operations, and more. "
                                  "What would you like to work on today?", title="Hello! I'm Omi.",
                           chips=["</> Write code", "🗎 Analyze files", "🌐 Search the web", "⚡ Help with a project"]))
    w.chat.add(ChatMessage("user", "Can you help me create a Python script to analyze this data?"))
    w.chat.add(ChatMessage("bot", "Absolutely! Here's a script that loads the CSV, prints summary statistics, "
                                  "and is ready for plots.", code=("data_analysis.py", CODE)))
    for e in (
        BoardEntry("09:41", "user", "You", "user", "omi", "Omi", "A2A_MESSAGE", "Build a small site comparing 3 static hosts and deploy it."),
        BoardEntry("09:41", "omi", "Omi", "boss", None, None, "TASK_ASSIGNED", "Plan: 3 research jobs, 1 writer, 1 deploy. Scout takes Netlify + Vercel."),
        BoardEntry("09:44", "b2", "Scout", "researcher", "omi", "Omi", "CLAIM_SUBMITTED", "Netlify free tier: 100 GB bandwidth/month. Evidence: url_quote netlify.com/pricing"),
        BoardEntry("09:44", "omi", "Omi", "boss", "b2", "Scout", "CLAIM_ACCEPTED", "Quote matches the done-criteria. Nice receipts."),
        BoardEntry("09:46", "b1", "Coder", "coder", "omi", "Omi", "HELP_REQUEST", "Which folder should the site live in? /site or the project root?"),
        BoardEntry("09:46", "omi", "Omi", "boss", "b1", "Coder", "A2A_MESSAGE", "Use /site. Keep index.html + style.css only."),
        BoardEntry("09:52", "b4", "Nightly", "devops", None, None, "APPROVAL_REQUEST", "git_push live main → github.com/you/hosts-site (3 commits). Waiting for your click."),
    ):
        w.board.add(e)
    for name, role, status, action in (("Omi", "boss", "working", "reviewing Scout's claim"),
                                       ("Coder", "coder", "working", "coding site/index.html"),
                                       ("Scout", "researcher", "thinking", "reading vercel.com/pricing"),
                                       ("Quill", "writer", "idle", "idle — waiting for a job"),
                                       ("Nightly", "devops", "waiting_approval", "waiting for your click: git_push")):
        w.board.set_activity(name.lower(), name, role, status, action)
    for ev, kw in (("tool", dict(name="read_file", ok=True)), ("tool", dict(name="write_file", ok=True)),
                   ("tool", dict(name="run_shell", ok=False))):
        q = taunts.react(ev, **kw)
        w.face.react(q.emoji)
        w.face.t += 1.0                                   # spread them out so all three have popped
    w.face.set_action("coding")
    w.resize(1536, 1000)
    w.show()
    if bubble:
        w.face.say(taunts.react("tool", name="write_file", ok=True))
    pump(900)
    idx = w.files.model.index(str(ws / "projects"))
    w.files.tree.expand(idx)
    w.files.tree.expand(w.files.model.index(str(ws / "projects" / "data-analysis")))
    w.files.tree.setCurrentIndex(w.files.model.index(str(ws / "projects" / "data-analysis" / "data_analysis.py")))
    pump(700 if not bubble else 900)
    # a demo: show a neutral path (never the real temp folder, which carries the user's name)
    w.files.path_label.setText("🗀  " + r"C:\omnibots_output\2026-09-26 data analysis")
    pump(100)
    w.grab().save(out)
    print("saved", out)
    from omnibots.ui.settings_window import SettingsWindow
    sw = SettingsWindow()
    sw.tabs.setCurrentIndex(sw.tabs.count() - 1)            # the "Bots & icons" legend (Folders comes first)
    sw.resize(920, 1500)
    sw.show()
    pump(300)
    legend = out.replace(".png", "_settings.png")
    sw.grab().save(legend)
    print("saved", legend)


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    main(args[0] if args else "bot_window.png", "--bubble" in sys.argv)
