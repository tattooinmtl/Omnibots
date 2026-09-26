"""Logging: logs/app.log (everything) and logs/error.log (errors), rotating.

Every line passes through a redactor before it is written, so API keys and
tokens never land on disk even if a caller logs them by mistake. Two layers:
  1. known secret values registered at runtime (register_secret), and
  2. patterns that look like keys (sk-..., nvapi-..., atr_..., Bearer ...,
     key=/token=/secret=/password= assignments, long opaque tokens).
"""

from __future__ import annotations

import logging
import re
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path

REDACTED = "[REDACTED]"

_PATTERNS = [
    re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_\-]{12,}"),
    re.compile(r"\bnvapi-[A-Za-z0-9_\-]{12,}"),
    re.compile(r"\batr_[A-Za-z0-9_\-]{12,}"),
    re.compile(r"\bgsk_[A-Za-z0-9]{12,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{5,}"),  # JWT
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{8,}"),
    re.compile(
        r"(?i)(\b[\w\-]*(?:api[_\-]?key|apikey|token|secret|password|passwd)[\w\-]*\b\s*[=:]\s*[\"']?)"
        r"([^\s\"',;]{4,})"
    ),
    # Long opaque strings (40+ chars of key-ish alphabet, with at least one digit).
    re.compile(r"\b(?=[A-Za-z_\-]*\d)[A-Za-z0-9_\-]{40,}\b"),
]

_known: set[str] = set()
_known_lock = threading.Lock()


def register_secret(value: str | None) -> None:
    """Redact this exact value from all future log lines (e.g. a loaded API key)."""
    if value and len(value) >= 6:
        with _known_lock:
            _known.add(value)


def redact(text: str) -> str:
    with _known_lock:
        known = sorted(_known, key=len, reverse=True)
    for value in known:
        text = text.replace(value, REDACTED)
    for pat in _PATTERNS:
        if pat.groups >= 2:
            text = pat.sub(lambda m: m.group(1) + REDACTED, text)
        else:
            text = pat.sub(REDACTED, text)
    return text


class RedactingFormatter(logging.Formatter):
    """Redacts the fully formatted line, which covers args and tracebacks too."""

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


FORMAT = "%(asctime)s %(levelname)-7s %(threadName)s %(name)s: %(message)s"


def setup_logging(logs_dir: Path, level: str = "INFO") -> None:
    logs_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(level.upper())
    for h in list(root.handlers):
        root.removeHandler(h)
        h.close()

    fmt = RedactingFormatter(FORMAT)
    app = RotatingFileHandler(logs_dir / "app.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8")
    app.setFormatter(fmt)
    err = RotatingFileHandler(logs_dir / "error.log", maxBytes=2_000_000, backupCount=5, encoding="utf-8")
    err.setLevel(logging.ERROR)
    err.setFormatter(fmt)
    console = logging.StreamHandler()
    console.setLevel(logging.WARNING)
    console.setFormatter(fmt)
    for h in (app, err, console):
        root.addHandler(h)
