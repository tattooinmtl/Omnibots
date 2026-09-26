"""A0.a.03: logs never contain secrets."""

from __future__ import annotations

import logging

from omnibots.logging_setup import REDACTED, redact, register_secret, setup_logging

FAKE_KEYS = [
    "sk-TESTSECRET1234567890abcdef",
    "nvapi-AbCdEf1234567890GhIjKl",
    "atr_live_9f8e7d6c5b4a3210",
    "gsk_abcdefghijklmnop1234",
]


def test_redact_patterns():
    for key in FAKE_KEYS:
        assert key not in redact(f"calling with {key} now")
    assert "hunter2pass" not in redact("password=hunter2pass")
    assert "abc.def.ghi123" not in redact("Authorization: Bearer abc.def.ghi123")
    assert redact("api_key: 'xyz98765'").endswith(f"'{REDACTED}'")
    # Ordinary text is left alone.
    assert redact("bot_1234 finished job task_42 in 3.2s") == "bot_1234 finished job task_42 in 3.2s"


def test_registered_secret_is_redacted_even_without_a_pattern():
    register_secret("plainlookingvalue")
    assert "plainlookingvalue" not in redact("the value is plainlookingvalue.")


def test_nothing_secret_reaches_the_log_files(tmp_path):
    """A0.99: grep the real log files for test keys."""
    setup_logging(tmp_path, "DEBUG")
    log = logging.getLogger("omnibots.test")
    for key in FAKE_KEYS:
        log.info("provider key is %s", key)
        log.error("failed with key=%s", key)
    try:
        raise ValueError(f"boom with {FAKE_KEYS[0]}")
    except ValueError:
        log.exception("traceback test")
    for h in logging.getLogger().handlers:
        h.flush()
    text = (tmp_path / "app.log").read_text(encoding="utf-8") + (tmp_path / "error.log").read_text(encoding="utf-8")
    assert "provider key is" in text and "traceback test" in text
    for key in FAKE_KEYS:
        assert key not in text, f"secret leaked: {key}"
    logging.getLogger().handlers.clear()
