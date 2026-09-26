"""A4 live: Omi and a worker talk through the board on MiniMax (spends a few thousand tokens).

Skipped unless OMNIBOTS_LIVE=1:
    set OMNIBOTS_LIVE=1 && python -m pytest tests/test_a4_live.py -v
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("OMNIBOTS_LIVE") != "1", reason="live provider test; set OMNIBOTS_LIVE=1")
ROOT = Path(__file__).resolve().parents[1]


def test_boss_and_worker_converse_through_the_board(home):
    r = subprocess.run([sys.executable, str(ROOT / "tools" / "demo_two_bots.py")], cwd=ROOT, env=os.environ.copy(),
                       capture_output=True, text=True, encoding="utf-8", timeout=600)
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-2000:]
    conn = sqlite3.connect(home / "db" / "omnibots.sqlite")
    rows = conn.execute("SELECT sender_id, recipient_id, message_type, payload_json FROM messages ORDER BY id").fetchall()
    kinds = [k for _, _, k, _ in rows]
    assert "A2A_MESSAGE" in kinds and "CLAIM_SUBMITTED" in kinds and "TASK_COMPLETED" in kinds
    first_a2a = next(r for r in rows if r[2] == "A2A_MESSAGE")
    assert first_a2a[0] == "omi"                                             # the boss starts the conversation
    done = next(p for _, _, k, p in rows if k == "TASK_COMPLETED")
    assert "7006743" in done.replace(",", "")                                # 1234 × 5678 + 91, computed not guessed
    claim = conn.execute("SELECT e.kind, e.detail_json FROM evidence e").fetchall()
    assert claim and claim[0][0] == "command" and '"exit_code": 0' in claim[0][1]
