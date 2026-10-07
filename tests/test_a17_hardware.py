"""A17.g (0.10.0): diagnose and fix the PC, program boards.

pc_check really runs on this PC (read-only); pc_fix goes through the real approval gate (it asks even when nothing
else would) and then really runs; board tools run the real esptool/mpremote (no board is needed for these calls)."""

from __future__ import annotations

import asyncio
import shutil
import sys

import pytest

from omnibots.runtime.hardware import CHECKS, hardware_tools, identify, writes_board
from omnibots.runtime.tools import ToolContext

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows PowerShell checks")
TOOLS = {t.name: t for t in hardware_tools()}


def run(name, args, ws):
    return asyncio.run(TOOLS[name].fn(args, ToolContext(bot_id="b", workspace=ws)))


def test_pc_check_reads_this_pc(tmp_path):
    out = run("pc_check", {"area": "overview"}, tmp_path)
    assert out.startswith("[pc_check overview]") and "Windows:" in out and "Memory:" in out and "Drive C:" in out
    assert "GB free" in run("pc_check", {"area": "disks"}, tmp_path)
    assert "Top processes by memory" in run("pc_check", {"area": "memory"}, tmp_path)
    assert "area must be one of" in run("pc_check", {"area": "registry"}, tmp_path)
    assert TOOLS["pc_check"].risk == "R0" and set(CHECKS) >= {"events", "network", "services", "startup", "updates", "temp"}


def test_pc_fix_always_asks_then_runs_the_exact_command(tmp_path):
    from mock_provider import MockProviders, sse
    from test_a3_runtime import Harness, call
    from omnibots.db import Database
    from omnibots.runtime.approvals import ApprovalCenter

    async def go():
        db = Database(tmp_path / "db.sqlite")
        await db.open()
        with MockProviders() as mock:
            mock.script("fake", sse("", tool_calls=[call("pc_fix", command="Write-Output 'flushed'", why="clear the DNS cache")]),
                        sse("done"))
            h = Harness(tmp_path, mock, db=db)
            h.bot.approvals = h.approvals = ApprovalCenter(db, ask_from="R5")
            h.bot.tools.add(TOOLS["pc_fix"])
            task = asyncio.create_task(h.bot.run("fix my DNS"))
            for _ in range(500):
                await asyncio.sleep(0.02)
                if h.approvals.list_pending():
                    break
            pending = h.approvals.list_pending()
            await h.approvals.decide(pending[0]["id"], True, "ok")
            res = await asyncio.wait_for(task, 60)
        await db.close()
        return pending, res, h
    pending, res, h = asyncio.run(go())
    assert pending[0]["rehearsal"]["runs_on_this_pc"] == "Write-Output 'flushed'"
    assert res.status == "done"
    tool_msgs = [m for m in res.messages if m.get("role") == "tool"]
    assert any("exit code 0" in str(m.get("content")) and "flushed" in str(m.get("content")) for m in tool_msgs)


def test_boards_are_named_by_their_usb_ids():
    assert identify("Silicon Labs CP210x USB to UART Bridge (COM5)", r"USB\VID_10C4&PID_EA60\0001") == {
        "port": "COM5", "name": "Silicon Labs CP210x USB to UART Bridge (COM5)", "usb": "10c4:ea60", "chip": "CP2102",
        "board": "ESP32 dev board (CP2102 bridge)", "confidence": "low"}
    assert identify("USB Serial Device (COM7)", r"USB\VID_2E8A&PID_0005&MI_00\6")["board"] == "Pico running MicroPython or CircuitPython"
    assert identify("Mystery (COM9)", r"USB\VID_1234&PID_5678")["confidence"] == "none"


def test_writing_to_a_board_always_asks_reading_and_compiling_dont():
    w = lambda tool, a: TOOLS["board_run"].risk_for({"tool": tool, "args": a}, ToolContext(bot_id="b", workspace=None))
    assert w("mpremote", "connect COM5 fs cp main.py :main.py") == "R5"
    assert w("mpremote", "connect COM5 run blink.py") == "R5"
    assert w("esptool", "--port COM5 write-flash 0x0 fw.bin") == "R5" and w("esptool", "--port COM5 erase-flash") == "R5"
    assert w("arduino-cli", "upload -p COM5 --fqbn esp32:esp32:esp32 sketch") == "R5"
    assert w("pio", "run -t upload") == "R5"
    assert w("mpremote", "connect COM5 ls") == "R1" and w("esptool", "--port COM5 chip-id") == "R1"
    assert w("arduino-cli", "compile --fqbn esp32:esp32:esp32 sketch") == "R1" and w("pio", "run") == "R1"
    assert writes_board("mpremote", "devs") is False


def test_board_tools_really_run(tmp_path):
    listed = run("board_list", {}, tmp_path)
    assert listed.startswith("no boards") or ":" in listed
    if shutil.which("esptool") or __import__("importlib").util.find_spec("esptool"):
        out = run("board_run", {"tool": "esptool", "args": "version"}, tmp_path)
        assert out.startswith("exit code 0") and "esptool" in out.lower()
    if __import__("importlib").util.find_spec("mpremote"):
        assert run("board_run", {"tool": "mpremote", "args": "devs"}, tmp_path).startswith("exit code 0")
    assert "tool must be one of" in run("board_run", {"tool": "rm", "args": "-rf /"}, tmp_path)
