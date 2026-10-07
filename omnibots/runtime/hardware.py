"""Your hardware (PLAN.md A17.g): diagnose and fix the PC, and program microcontrollers.

pc_check    read-only checks through PowerShell/CIM, one area at a time: overview, disks, memory (top processes),
            startup, services, events (the last day's errors), network, updates, temp. R0: it reads the system's
            state, never files' contents and never secrets.
pc_fix      runs ONE PowerShell command the bot proposes, ALWAYS after the user approves the exact command on a card.
            admin=true runs it elevated, so Windows' own UAC prompt asks a second time. Its output comes back.
board_list  serial ports and USB boards identified by USB vendor/product id (ESP32 bridges and native USB, Pico,
            Arduino), plus a Pico in BOOTSEL mode (a drive). R0.
board_run   one board tool call: mpremote (MicroPython: run code, copy files, ls, repl-less exec), esptool (chip id,
            flash), arduino-cli or pio (compile, upload). Writing to a board ALWAYS asks; the card shows the command.
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from omnibots.runtime.tools import Tool, ToolContext

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
MAX_OUT = 12000

CHECKS: dict[str, str] = {
    "overview": r"""
$os = Get-CimInstance Win32_OperatingSystem; $cs = Get-CimInstance Win32_ComputerSystem; $cpu = Get-CimInstance Win32_Processor | Select-Object -First 1
"Windows: $($os.Caption) $($os.Version) build $($os.BuildNumber)"
"Up since: $($os.LastBootUpTime) ($([int]((Get-Date) - $os.LastBootUpTime).TotalHours) h)"
"CPU: $($cpu.Name.Trim()), $($cpu.NumberOfLogicalProcessors) threads, load $($cpu.LoadPercentage)%"
"Memory: $([math]::Round(($os.TotalVisibleMemorySize - $os.FreePhysicalMemory)/1MB,1)) of $([math]::Round($os.TotalVisibleMemorySize/1MB,1)) GB used"
Get-CimInstance Win32_LogicalDisk -Filter "DriveType=3" | ForEach-Object { "Drive $($_.DeviceID) $([math]::Round($_.FreeSpace/1GB,1)) GB free of $([math]::Round($_.Size/1GB,1)) GB" }
""",
    "disks": r"""
Get-CimInstance Win32_LogicalDisk -Filter "DriveType=3" | ForEach-Object { "Drive $($_.DeviceID) [$($_.VolumeName)] $($_.FileSystem): $([math]::Round($_.FreeSpace/1GB,1)) GB free of $([math]::Round($_.Size/1GB,1)) GB ($([int](100*$_.FreeSpace/$_.Size))% free)" }
try { Get-PhysicalDisk | ForEach-Object { "Disk $($_.FriendlyName): $($_.MediaType), health $($_.HealthStatus), $([math]::Round($_.Size/1GB)) GB" } } catch { "Disk health: not available" }
""",
    "memory": r"""
$os = Get-CimInstance Win32_OperatingSystem
"Memory: $([math]::Round(($os.TotalVisibleMemorySize - $os.FreePhysicalMemory)/1MB,1)) of $([math]::Round($os.TotalVisibleMemorySize/1MB,1)) GB used"
"Top processes by memory:"
Get-Process | Sort-Object WorkingSet64 -Descending | Select-Object -First 15 | ForEach-Object { "  $($_.ProcessName) (pid $($_.Id)): $([math]::Round($_.WorkingSet64/1MB)) MB, CPU $([math]::Round($_.CPU)) s" }
""",
    "startup": r"""
Get-CimInstance Win32_StartupCommand | ForEach-Object { "$($_.Name) [$($_.Location)]: $($_.Command)" }
""",
    "services": r"""
"Automatic services that are not running:"
Get-CimInstance Win32_Service -Filter "StartMode='Auto' AND State<>'Running'" | ForEach-Object { "  $($_.Name) ($($_.DisplayName)): $($_.State), exit $($_.ExitCode)" }
""",
    "events": r"""
$since = (Get-Date).AddDays(-1)
foreach ($log in 'System','Application') {
  "--- $log (errors and critical, last 24 h) ---"
  try { Get-WinEvent -FilterHashtable @{LogName=$log; Level=1,2; StartTime=$since} -MaxEvents 25 -ErrorAction Stop |
        ForEach-Object { "$($_.TimeCreated.ToString('MM-dd HH:mm')) $($_.ProviderName) [$($_.Id)]: $(($_.Message -split "`n")[0])" } }
  catch { "  none" }
}
""",
    "network": r"""
Get-NetIPConfiguration | Where-Object { $_.IPv4DefaultGateway } | ForEach-Object { "Adapter $($_.InterfaceAlias): $($_.IPv4Address.IPAddress), gateway $($_.IPv4DefaultGateway.NextHop), DNS $($_.DNSServer.ServerAddresses -join ', ')" }
$gw = (Get-NetIPConfiguration | Where-Object { $_.IPv4DefaultGateway } | Select-Object -First 1).IPv4DefaultGateway.NextHop
if ($gw) { "Gateway ping: $(if (Test-Connection $gw -Count 2 -Quiet) {'ok'} else {'no answer'})" }
"Internet (1.1.1.1): $(if (Test-Connection 1.1.1.1 -Count 2 -Quiet) {'ok'} else {'no answer'})"
try { "DNS lookup of microsoft.com: $((Resolve-DnsName microsoft.com -ErrorAction Stop | Select-Object -First 1).IPAddress)" } catch { "DNS lookup failed: $_" }
""",
    "updates": r"""
"Pending reboot: $((Test-Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\RebootRequired') -or (Test-Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending'))"
"Latest installed updates:"
Get-HotFix | Sort-Object InstalledOn -Descending | Select-Object -First 8 | ForEach-Object { "  $($_.HotFixID) $($_.Description) $($_.InstalledOn)" }
""",
    "temp": r"""
foreach ($p in @($env:TEMP, "$env:WINDIR\Temp", "$env:LOCALAPPDATA\CrashDumps")) {
  if (Test-Path $p) { $s = (Get-ChildItem $p -Recurse -Force -ErrorAction SilentlyContinue | Measure-Object Length -Sum).Sum; "$p : $([math]::Round($s/1MB)) MB" }
}
"Recycle Bin: $([math]::Round(((New-Object -ComObject Shell.Application).NameSpace(10).Items() | Measure-Object -Property Size -Sum).Sum/1MB)) MB"
""",
}


async def powershell(script: str, timeout: float = 90, elevated: bool = False) -> tuple[int, str]:
    def go():
        if elevated:                                       # Windows' UAC prompt; output comes back through a file
            with tempfile.TemporaryDirectory(prefix="omnibots-fix-") as tmp:
                out = Path(tmp) / "out.txt"
                body = Path(tmp) / "fix.ps1"
                body.write_text(f"& {{\n{script}\n}} *> '{out}'\nexit $LASTEXITCODE\n", encoding="utf-8-sig")
                outer = (f"$p = Start-Process powershell -Verb RunAs -Wait -PassThru -WindowStyle Hidden -ArgumentList "
                         f"'-NoProfile','-ExecutionPolicy','Bypass','-File','{body}'; exit $p.ExitCode")
                try:
                    r = subprocess.run(["powershell", "-NoProfile", "-Command", outer], capture_output=True, timeout=timeout,
                                       creationflags=NO_WINDOW)
                except subprocess.TimeoutExpired:
                    return -2, "timed out"
                text = out.read_text(encoding="utf-8", errors="replace") if out.exists() else ""
                return r.returncode, (text or r.stderr.decode("utf-8", "replace") or "(no output; was the UAC prompt declined?)")
        try:
            r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                                "[Console]::OutputEncoding=[Text.Encoding]::UTF8; " + script],
                               capture_output=True, timeout=timeout, creationflags=NO_WINDOW)
        except subprocess.TimeoutExpired:
            return -2, "timed out"
        return r.returncode, (r.stdout.decode("utf-8", "replace") + r.stderr.decode("utf-8", "replace")).strip()
    return await asyncio.to_thread(go)


# ── boards ───────────────────────────────────────────────────────────────
USB_IDS = {   # vid:pid -> (chip, guess, confidence); the same table as OmniOne's server/boards/detect.js
    "10c4:ea60": ("CP2102", "ESP32 dev board (CP2102 bridge)", "low"),
    "10c4:ea70": ("CP2105", "ESP32 dev board (CP2105 bridge)", "low"),
    "1a86:7523": ("CH340", "Arduino clone or ESP32 board (CH340 bridge)", "low"),
    "1a86:7522": ("CH340", "Arduino clone or ESP32 board (CH340 bridge)", "low"),
    "1a86:55d4": ("CH9102", "ESP32 board (CH9102 bridge)", "low"),
    "1a86:5523": ("CH341", "Arduino clone (CH341 bridge)", "low"),
    "0403:6001": ("FT232R", "FTDI serial: Arduino or ESP32 board", "low"),
    "0403:6015": ("FT231X", "FTDI serial device", "low"),
    "303a:1001": ("ESP32-S2/S3", "ESP32-S2 or S3 (native USB)", "medium"),
    "303a:0002": ("ESP32-S2", "ESP32-S2 in download mode", "high"),
    "303a:1002": ("ESP32-S3", "ESP32-S3 in download mode", "high"),
    "303a:0009": ("ESP32-C3", "ESP32-C3", "high"),
    "2e8a:0003": ("RP2040", "Pico in BOOTSEL mode: flash by copying a .uf2", "high"),
    "2e8a:0005": ("RP2040", "Pico running MicroPython or CircuitPython", "high"),
    "2e8a:000a": ("RP2040", "Pico running a USB-serial sketch", "high"),
    "2e8a:00c0": ("RP2040", "Pico running MicroPython", "high"),
    "2341:0043": ("ATmega16U2", "Arduino Uno", "high"),
    "2341:0001": ("ATmega8U2", "Arduino Uno", "high"),
    "2341:0042": ("ATmega16U2", "Arduino Mega 2560", "high"),
    "2341:8036": ("ATmega32U4", "Arduino Leonardo", "high"),
    "2341:804d": ("SAMD21", "Arduino Zero", "high"),
}

LIST_PS = r"""
Get-CimInstance Win32_PnPEntity | Where-Object { $_.Name -match '\(COM\d+\)' -or $_.DeviceID -match 'VID_2E8A&PID_0003' } |
  ForEach-Object { [pscustomobject]@{ name = $_.Name; id = $_.DeviceID } } | ConvertTo-Json -Compress
"""


def identify(name: str, device_id: str) -> dict[str, Any]:
    m = re.search(r"VID_([0-9A-F]{4})&PID_([0-9A-F]{4})", device_id or "", re.I)
    vidpid = f"{m.group(1)}:{m.group(2)}".lower() if m else ""
    port = re.search(r"\((COM\d+)\)", name or "")
    chip, guess, conf = USB_IDS.get(vidpid, ("?", "unknown serial device", "none"))
    return {"port": port.group(1) if port else None, "name": name, "usb": vidpid, "chip": chip, "board": guess, "confidence": conf}


BOARD_TOOLS = {
    "mpremote": [sys.executable, "-m", "mpremote"],
    "esptool": [sys.executable, "-m", "esptool"],
    "arduino-cli": ["arduino-cli"],
    "pio": ["pio"],
}
# board tool calls that change a board: flashing, erasing, uploading, copying or running code on it. These ALWAYS ask
# (R5: firmware is overwritten). Everything else (listing, reading, compiling in the project) is local work.
WRITES = {
    "mpremote": r"\b(cp|rm|rmdir|mkdir|run|exec|eval|mip|install|reset|bootloader|edit|touch|mount|rtc)\b",
    "esptool": r"\b(write[-_]flash|erase[-_]flash|erase[-_]region|write[-_]mem|merge[-_]bin|burn|load[-_]ram|run)\b",
    "arduino-cli": r"\b(upload|burn-bootloader)\b",
    "pio": r"(-t|--target)\s*(upload|uploadfs|erase|program)\b|\bdevice monitor\b",
}


def board_tool_argv(tool: str, args: str) -> list[str]:
    import shlex
    base = BOARD_TOOLS.get(tool)
    if base is None:
        raise ValueError(f"tool must be one of {', '.join(BOARD_TOOLS)}")
    if base[0] not in (sys.executable,) and not shutil.which(base[0]):
        raise ValueError(f"{tool} is not installed")
    return [*base, *shlex.split(args, posix=True)]


def writes_board(tool: str, args: str) -> bool:
    return bool(re.search(WRITES.get(tool, r"."), args))


def hardware_tools() -> list[Tool]:
    async def pc_check(args: dict[str, Any], ctx: ToolContext) -> str:
        area = str(args.get("area") or "overview").lower()
        if area not in CHECKS:
            return f"ERROR: area must be one of {', '.join(CHECKS)}"
        code, out = await powershell(CHECKS[area])
        await ctx.event("console", f"  🩺 pc_check {area}")
        return f"[pc_check {area}]\n{out[:MAX_OUT] or '(nothing)'}"

    async def pc_fix(args: dict[str, Any], ctx: ToolContext) -> str:
        cmd = str(args.get("command") or "").strip()
        if not cmd:
            return "ERROR: give the PowerShell command to run"
        admin = bool(args.get("admin"))
        await ctx.event("console", f"  🛠 pc_fix{' (admin)' if admin else ''}: {cmd[:160]}")
        code, out = await powershell(cmd, timeout=float(args.get("timeout_seconds") or 300), elevated=admin)
        from types import SimpleNamespace
        ctx.record_run(f"pc_fix {cmd}", SimpleNamespace(exit_code=code, timed_out=code == -2, output=out))
        return f"exit code {code}\n{out[:MAX_OUT]}"

    async def board_list(args: dict[str, Any], ctx: ToolContext) -> str:
        code, out = await powershell(LIST_PS, timeout=30)
        try:
            rows = json.loads(out) if out.strip() else []
        except ValueError:
            return f"ERROR: couldn't list devices: {out[:300]}"
        rows = rows if isinstance(rows, list) else [rows]
        found = [identify(r.get("name", ""), r.get("id", "")) for r in rows]
        if not found:
            return "no boards or serial ports found (plugged in with a DATA cable? some cables only charge)"
        return "\n".join(f"{f['port'] or 'drive'}: {f['board']} (chip {f['chip']}, usb {f['usb'] or '?'}, confidence {f['confidence']})"
                         for f in found)

    async def board_run(args: dict[str, Any], ctx: ToolContext) -> str:
        tool, a = str(args.get("tool") or ""), str(args.get("args") or "")
        try:
            argv = board_tool_argv(tool, a)
        except ValueError as exc:
            return f"ERROR: {exc}"

        def go():
            try:
                r = subprocess.run(argv, cwd=str(ctx.workspace), capture_output=True, timeout=float(args.get("timeout_seconds") or 300),
                                   creationflags=NO_WINDOW)
                return r.returncode, (r.stdout.decode("utf-8", "replace") + r.stderr.decode("utf-8", "replace")).strip()
            except subprocess.TimeoutExpired:
                return -2, "timed out"
        await ctx.event("console", f"  🔌 {tool} {a[:160]}")
        code, out = await asyncio.to_thread(go)
        from types import SimpleNamespace
        ctx.record_run(f"{tool} {a}", SimpleNamespace(exit_code=code, timed_out=code == -2, output=out))
        return f"exit code {code}\n{out[-MAX_OUT:]}"

    def board_risk(a: dict[str, Any], ctx) -> str:
        return "R5" if writes_board(str(a.get("tool") or ""), str(a.get("args") or "")) else "R1"

    async def fix_card(a, c):
        return {"runs_on_this_pc": str(a.get("command") or ""), "as_administrator": bool(a.get("admin")), "why": str(a.get("why") or "")}

    async def board_card(a, c):
        return {"board_tool": str(a.get("tool") or ""), "arguments": str(a.get("args") or ""), "why": str(a.get("why") or "")}

    s = {"type": "string"}
    return [
        Tool("pc_check", "Check this PC (read-only): area overview | disks | memory | startup | services | events | network | "
             "updates | temp. Use it to diagnose before proposing any fix.",
             {"type": "object", "properties": {"area": {"type": "string", "enum": list(CHECKS)}}}, "R0", pc_check,
             timeout=120, path_arg=None, summary=lambda a: f"pc_check {a.get('area', 'overview')}"),
        Tool("pc_fix", "Fix something on this PC with ONE PowerShell command, after diagnosing with pc_check. The user sees "
             "and approves the exact command first; admin=true also brings up Windows' administrator prompt. Prefer the "
             "smallest safe fix; never delete user files.",
             {"type": "object", "properties": {"command": s, "why": s, "admin": {"type": "boolean"}, "timeout_seconds": {"type": "integer"}},
              "required": ["command", "why"]},
             "R3", pc_fix, timeout=620, path_arg=None, always_ask=True, rehearse=fix_card,
             summary=lambda a: f"pc_fix{' (ADMIN)' if a.get('admin') else ''}: {str(a.get('command', ''))[:140]}"),
        Tool("board_list", "List the microcontroller boards and serial ports plugged into this PC (ESP32, Pico, Arduino), "
             "identified by their USB ids.", {"type": "object", "properties": {}}, "R0", board_list, timeout=60, path_arg=None,
             summary=lambda a: "board_list"),
        Tool("board_run", "Run a board tool in your project folder: tool mpremote (MicroPython: 'connect COM5 run main.py', "
             "'connect COM5 fs cp main.py :main.py', 'connect COM5 ls'), esptool ('--port COM5 chip-id', 'write-flash 0x0 fw.bin'), "
             "arduino-cli ('compile --fqbn esp32:esp32:esp32 sketch', 'upload -p COM5 …') or pio ('run', 'run -t upload'). "
             "Reading a board runs freely; writing to it or uploading asks the user first.",
             {"type": "object", "properties": {"tool": {"type": "string", "enum": list(BOARD_TOOLS)}, "args": s, "why": s,
                                               "timeout_seconds": {"type": "integer"}}, "required": ["tool", "args"]},
             "R1", board_run, timeout=620, path_arg=None, classify=board_risk, rehearse=board_card,
             summary=lambda a: f"board_run {a.get('tool')} {str(a.get('args', ''))[:140]}"),
    ]
