"""A11.c: the bot window builds, takes messages and console lines, and closing hides it."""

from __future__ import annotations

import os

from qt_helpers import qapp  # noqa: E402

from PySide6.QtWidgets import QApplication  # noqa: E402

from omnibots.ui.bot_window import BotWindow  # noqa: E402
from omnibots.ui.taunts import Quip  # noqa: E402
from omnibots.ui.widgets import BotCard, ChatMessage  # noqa: E402

app = qapp()


def test_bot_window_builds_and_behaves(tmp_path):
    (tmp_path / "a.py").write_text("print(1)\n", encoding="utf-8")
    w = BotWindow(BotCard(name="Coder", role="coder", status="Working", usage_pct=42),
                  tmp_path, team=[("omi", "Omi", "boss", "happy"), ("c", "Coder", "coder", "working")], bot_id="c")
    w.show()
    w.console.append("$ python a.py")
    w.thinking.append("reasoning…")
    w.chat.add(ChatMessage("bot", "hi", chips=["a", "b"], code=("a.py", "print(1)")))
    sent = []
    w.chat.prompt_sent.connect(sent.append)
    w.chat.input.setText("use pytest instead")
    w.chat._send()
    assert sent == ["use pytest instead"]
    assert "python a.py" in w.console.view.toPlainText()
    w.face.say(Quip("shipped it.", "joy"))
    assert w.face.bubble is not None and w.card.usage.value == 42
    img = w.grab()
    assert img.width() > 1000
    w.close()                                         # closing never stops bots: it only hides
    assert not w.isVisible()
