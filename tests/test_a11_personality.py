"""A11.d: Omi's face and voice. The personality is read live from Omni's ui.mjs
(read-only), with a bundled snapshot fallback; the face renders every mood."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from qt_helpers import qapp  # noqa: E402

from omnibots.omni.personality import SNAPSHOT, _js_to_json, load, read_omni  # noqa: E402
from omnibots.ui.taunts import Taunts  # noqa: E402

OMNI_UI = Path.home() / ".omni" / "src" / "ui.mjs"


def test_js_literal_parser_handles_apostrophes_comments_and_bare_keys():
    src = """{
      coding: { cheer: ["didn't break it.", 'single "quoted"'], // a comment
                grumble: [`back\\ttick`, "x",], },
      /* block */ n: 3, ok: true, none: null,
    }"""
    assert _js_to_json(src) == {"coding": {"cheer": ["didn't break it.", 'single "quoted"'], "grumble": ["back\ttick", "x"]},
                                "n": 3, "ok": True, "none": None}


@pytest.mark.skipif(not OMNI_UI.exists(), reason="Omni not installed")
def test_reads_omis_voice_live_from_omni():
    d = read_omni(OMNI_UI)
    assert len(d["FUNNY_WORDS"]) >= 10 and all(w.endswith("ising") for w in d["FUNNY_WORDS"])
    assert {"coding", "reading", "searching", "running", "thinking", "provider"} <= set(d["ACTION_LINES"])
    assert d["IMPATIENT"] and load(OMNI_UI.parent.parent)["source"].endswith("ui.mjs")


def test_snapshot_fallback_when_omni_is_missing(tmp_path):
    d = load(tmp_path / "no-omni-here")
    assert d["source"] == "snapshot" and SNAPSHOT.exists() and d["FUNNY_WORDS"]


def test_taunts_map_events_to_omis_pools_without_repeats():
    t = Taunts(load(None), seed=1)
    p = t.p
    ok = t.react("tool", name="write_file", ok=True)
    bad = t.react("tool", name="run_shell", ok=False)
    assert ok.text in p["ACTION_LINES"]["coding"]["cheer"] and ok.mood == "joy" and ok.grawlix is None
    assert bad.text in p["ACTION_LINES"]["running"]["grumble"] and bad.mood == "sad" and bad.grawlix
    assert t.react("impatient").text in p["IMPATIENT"]
    assert t.react("provider").mood == "error"
    wait = t.react("approval", kind="waiting")
    assert wait.source == "omnibots" and wait.mood == "thinking"
    assert t.react("tool", name="unknown_tool") is None
    lines = [t.react("tool", name="read_file", ok=True).text for _ in range(4)]
    assert len(set(lines)) == 4                               # a pool of 4 cheers, no repeats
    assert t.status_verb().endswith("…")


def test_every_mood_renders():
    from PySide6.QtGui import QGuiApplication
    from omnibots.ui.omi_face import MOODS, render_face
    app = qapp()  # noqa: F841
    for mood in MOODS:
        for size in (16, 64, 240):
            img = render_face(size, mood, blink=0.5 if mood == "happy" else 0.0, t=1.0)
            assert img.width() == size and not img.isNull()
            assert img.pixelColor(size // 2, size // 2).alpha() > 0          # something is drawn in the middle
