"""Live bug (user, 2026-09-26): Omi asked "What's the goal?" and waited; everything typed in his
chat went out as USER_STEER (read only between steps), so he never got an answer, and the
chat just repeated "Got it, adjusting course." Now a bot waiting on its question gets the chat
text as the answer, and the chat shows no canned note for it.

Runs the REAL engine and the REAL LiveUI; only the model is a local mock server."""

from __future__ import annotations

import time
from pathlib import Path

from qt_helpers import qapp

from mock_provider import MockProviders, sse
from omnibots.bots.profile import BOSS_ID
from omnibots.engine import Engine
from omnibots.omni.config import OmniConfig, ProviderInfo
from omnibots.omni.locate import OmniLocation

app = qapp()


def call(name, **args):
    return {"name": name, "args": args}


def wait_for(cond, secs=15.0):
    end = time.monotonic() + secs
    while time.monotonic() < end:
        app.processEvents()
        if cond():
            return True
        time.sleep(0.05)
    return False


def test_typing_while_omi_waits_on_his_question_answers_it(tmp_path):
    from omnibots.ui.live import LiveUI
    from omnibots.omni.personality import load
    from omnibots.ui.taunts import Taunts

    with MockProviders() as mock:
        mock.script("boss",
                    sse("", tool_calls=[call("ask_user", question="What's the goal?", timeout_seconds=30)]),
                    sse("Thanks! Building the promo site in a folder now."))
        eng = Engine(tmp_path / "db" / "omnibots.sqlite", keep_awake=False, home=tmp_path, omni_install_root="")
        eng.start()
        try:
            providers = {"boss": ProviderInfo(name="boss", base_url=mock.url("boss"), api_key="k", key_source="settings",
                                              raw={"reasoningParam": "none"})}
            eng.omni = OmniConfig(OmniLocation(Path("."), Path(".")), providers,
                                  {"boss/m": {"provider": "boss", "id": "boss", "maxTokens": 256}}, None, None, [], {})
            eng.submit(eng.registry.update(BOSS_ID, chain=["boss/m"])).result(timeout=10)
            live = LiveUI(eng, taunts=Taunts(load(None), seed=1))
            w = live.open_bot("omi")
            live.on_prompt("omi", "hello omi ready to work")                 # idle Omi: a new goal
            assert wait_for(lambda: eng.bot_states.get("omi") == "waiting_answer"), eng.bot_states
            assert wait_for(lambda: w.card.card.status == "Waiting for your answer")
            tray = eng.submit(eng.ui_tray()).result(timeout=10)
            assert next(b for b in tray["bots"] if b["id"] == "omi")["group"] == "question"

            before = len(w.chat.findChildren(type(w.card.name)))
            w.chat.input.setText("make a promotional website in a folder")
            w.chat._send()                                                  # typed while he waits: the answer
            assert wait_for(lambda: len([r for r in mock.requests if r["provider"] == "boss"]) >= 2)
            second = [r for r in mock.requests if r["provider"] == "boss"][1]["body"]["messages"]
            assert any("The user answered: make a promotional website in a folder" in str(m.get("content")) for m in second)
            wait_for(lambda: False, 0.5)
            labels = [l.text() for l in w.chat.findChildren(type(w.card.name))][before:]
            assert not any("adjusting course" in t or "before my next step" in t for t in labels), labels
        finally:
            eng.stop()
