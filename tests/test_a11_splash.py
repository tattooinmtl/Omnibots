"""The launch intro (user, 2026-09-26): the 7 s video follows the real startup: it hurries when
everything is loaded, holds on its last frame while loading continues, and a click skips it."""

from __future__ import annotations

import time

from qt_helpers import qapp

from omnibots.ui.splash import HURRY_RATE, POSTER, VIDEO, Splash

app = qapp()


def pump_until(cond, secs):
    end = time.monotonic() + secs
    while time.monotonic() < end:
        app.processEvents()
        if cond():
            return True
        time.sleep(0.02)
    return False


def test_the_assets_are_small():
    assert VIDEO.exists() and VIDEO.stat().st_size < 1_200_000          # was 9.7 MB
    assert POSTER.exists() and POSTER.stat().st_size < 200_000


def test_loaded_early_the_video_hurries_and_finishes_before_7s():
    s = Splash()
    done = []
    s.finished.connect(lambda: done.append(time.monotonic()))
    t0 = time.monotonic()
    s.start()
    assert pump_until(lambda: s.frame is not None, 5), "no video frames"
    s.set_stage("Ready", 1.0)
    s.set_ready()
    assert s.player.playbackRate() == HURRY_RATE
    assert pump_until(lambda: done, 8)
    assert done[0] - t0 < 6.5                                           # sooner than the 7 s intro
    s.close()


def test_still_loading_it_holds_on_the_last_frame_then_finishes_when_ready():
    s = Splash()
    done = []
    s.finished.connect(lambda: done.append(1))
    s.start()
    s.set_stage("Loading tools, skills and MCP servers", 0.7)
    assert pump_until(lambda: s.video_ended, 12)                        # reached the end: holding
    assert not done and s.frame is not None and s.progress == 0.7
    pump_until(lambda: False, 0.5)
    assert not done                                                     # still waiting for the engine
    s.set_ready()
    assert pump_until(lambda: done, 2)
    s.close()


def test_a_click_skips_but_still_waits_for_the_engine_and_no_video_uses_the_poster():
    s = Splash(play=False)                                              # no video playback: the poster
    done = []
    s.finished.connect(lambda: done.append(1))
    s.start()
    assert s.video_ended and not s.poster.isNull()
    s.skip()
    pump_until(lambda: False, 0.2)
    assert not done
    s.set_ready("engine did not start in time")
    assert done and s.stage.startswith("✖")
    img = s.grab().toImage()
    assert img.width() == 960 and img.height() == 540
    s.close()
