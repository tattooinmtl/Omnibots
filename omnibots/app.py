"""Entry point: python -m omnibots [options]

  python -m omnibots                 start the app (or focus the running one)
  python -m omnibots --send status   send a control command to the running app
  python -m omnibots --exit-after 5  start, then exit cleanly after N seconds (tests)
  python -m omnibots --no-window     no main window (tests / headless checks)
"""

from __future__ import annotations

import argparse
import json
import time
import logging
import sys

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from omnibots import __version__
from omnibots.engine import Engine
from omnibots.ipc import SingleInstance, send_command
from omnibots.logging_setup import setup_logging
from omnibots.paths import get_paths
from omnibots.settings import load_settings

log = logging.getLogger("omnibots")

# Exit codes
OK, ALREADY_RUNNING, NOT_RUNNING, STARTUP_FAILED = 0, 3, 4, 5


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="omnibots", description="OmniBots desktop app")
    p.add_argument("--send", metavar="CMD", help="send a command (show|status|goal|stop|approvals|approve|deny) to the running app and print the reply")
    p.add_argument("--text", default="", help="text for --send goal/tell, or the approval id for --send approve/deny")
    p.add_argument("--id", default="", help="bot id for --send tell/pause_bot/resume_bot/stop_bot")
    p.add_argument("--exit-after", type=float, default=0, metavar="SECONDS", help="exit cleanly after N seconds")
    p.add_argument("--no-window", action="store_true", help="don't show the main window")
    p.add_argument("--version", action="version", version=f"omnibots {__version__}")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    paths = get_paths()
    settings = load_settings(paths.settings_file)
    setup_logging(paths.logs, settings["app"]["log_level"])

    if sys.platform == "win32":
        # Own taskbar identity, so Windows shows Omi instead of python.exe's icon.
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("OmniBots.App")
    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationName("OmniBots")
    from omnibots.ui.omi_icon import omi_icon

    app.setWindowIcon(omi_icon())
    app.setQuitOnLastWindowClosed(True)

    # ── client mode ────────────────────────────────────────────────────
    if args.send:
        msg = {"cmd": args.send}
        if args.text:
            msg["text"] = args.text
        if args.id:
            msg["id"] = args.id
        reply = send_command(msg)
        if reply is None:
            print(json.dumps({"ok": False, "error": "OmniBots is not running"}))
            return NOT_RUNNING
        print(json.dumps(reply))
        return OK if reply.get("ok") else 1

    # ── single instance ────────────────────────────────────────────────
    window = None
    live = None
    tray = None
    engine = Engine(
        paths.db_file,
        keep_awake=settings["keep_awake"]["enabled"],
        shutdown_timeout=float(settings["engine"]["shutdown_timeout_seconds"]),
        omni_install_root=settings["omni"]["install_root"],
        home=paths.home,
        orchestrator_settings=settings.get("orchestrator", {}),
        mcp_risk=settings.get("mcp_risk", {}),
        budgets=settings.get("budgets", {}),
    )

    def cmd_show(_msg):
        if window is not None:
            window.bring_to_front()
        return {"ok": True}

    def cmd_status(_msg):
        status = engine.submit(engine.status()).result(timeout=5)
        return {"ok": True, "status": status}

    def run(coro, timeout=30):
        return engine.submit(coro).result(timeout=timeout)

    def cmd_goal(msg):
        text = str(msg.get("text") or "").strip()
        if not text:
            return {"ok": False, "error": "goal needs text"}
        return {"ok": True, "project_id": run(engine.start_goal(text))}

    def cmd_tell(msg):
        bot, text = str(msg.get("id") or "omi"), str(msg.get("text") or "").strip()
        if not text:
            return {"ok": False, "error": "tell needs text"}
        run(engine.tell(bot, text))
        return {"ok": True}

    def team_cmd(name):
        def handler(msg):
            fn = getattr(engine.team, name)
            if name.endswith("_bot"):
                bot = str(msg.get("id") or "")
                return {"ok": bool(run(fn(bot))), "bot": bot}
            return {"ok": True, **run(fn(panic=True) if name == "panic" else fn(), timeout=60)}
        return handler

    def cmd_bots(_msg):
        async def listing():
            return [{"id": b.id, "name": b.name, "role": b.role, "status": b.status, "chain": b.chain[:2]}
                    for b in await engine.registry.list()]
        return {"ok": True, "bots": engine.submit(listing()).result(timeout=5)}

    def cmd_approvals(_msg):
        return {"ok": True, "pending": engine.approvals.list_pending()}

    def _decide(msg, approved):
        aid = str(msg.get("id") or msg.get("text") or "")
        done = engine.submit(engine.approvals.decide(aid, approved, "user via control pipe")).result(timeout=5)
        return {"ok": bool(done), **({} if done else {"error": f"no pending approval {aid!r}"})}

    def cmd_preapprove(msg):
        # --text '{"tool": "web_fetch", "domain": "api.github.com", "count": 5, "minutes": 60}'
        try:
            spec = json.loads(msg.get("text") or "{}")
            return {"ok": True, "scope": engine.preapprove(str(spec["tool"]), match=str(spec.get("match", "")),
                                                           domain=str(spec.get("domain", "")), count=int(spec.get("count", 1)),
                                                           minutes=float(spec.get("minutes", 60)))}
        except (ValueError, KeyError, TypeError) as exc:
            return {"ok": False, "error": f"bad pre-approval: {exc}"}

    def cmd_snapshot(msg):
        """Save a bot window as a PNG in logs/ (for checking the live UI). --id picks the bot (default Omi)."""
        if window is None:
            return {"ok": False, "error": "no window (started with --no-window)"}
        bot = str(msg.get("id") or "omi")
        w = live.windows.get(bot) if bot != "omi" else window
        if w is None:
            return {"ok": False, "error": f"no open window for {bot}"}
        out = paths.home / "logs" / f"window-{bot}-{int(time.time())}.png"
        w.grab().save(str(out))
        return {"ok": True, "path": str(out)}

    def cmd_tray(msg):
        """The tray menu as the user would see it now (rebuilt, timed), for checking it live."""
        if tray is None:
            return {"ok": False, "error": "no tray (no window, or the system tray is unavailable)"}
        t0 = time.perf_counter()
        tray.rebuild()
        ms = round((time.perf_counter() - t0) * 1000, 1)

        def items(menu):
            out = []
            for a in menu.actions():
                if a.isSeparator():
                    out.append("---")
                elif a.menu():
                    out.append({a.text(): items(a.menu())})
                else:
                    out.append(a.text() + ("" if a.isEnabled() else "  (off)"))
            return out
        path = [x.strip() for x in str(msg.get("text") or "").split(" / ") if x.strip()]
        if path:                               # --text "Working (1) / Omi — boss / Pause": click that item
            menu, act = tray.menu, None
            for i, step in enumerate(path):
                act = next((a for a in menu.actions() if not a.isSeparator() and a.text().startswith(step)), None)
                if act is None:
                    return {"ok": False, "error": f"no menu item {step!r}"}
                if i < len(path) - 1:
                    menu = act.menu()
            if not act.isEnabled():
                return {"ok": False, "error": f"{act.text()!r} is disabled"}
            act.trigger()
            return {"ok": True, "clicked": act.text()}
        return {"ok": True, "rebuild_ms": ms, "tooltip": tray.icon.toolTip(), "visible": tray.icon.isVisible(), "menu": items(tray.menu)}

    def cmd_budget(_msg):
        return {"ok": True, "budget": run(engine.budget.snapshot(), timeout=10)}

    def cmd_stop(_msg):
        QTimer.singleShot(0, app.quit)
        return {"ok": True}

    instance = SingleInstance(
        paths.lock_file,
        {"show": cmd_show, "status": cmd_status, "goal": cmd_goal, "stop": cmd_stop, "approvals": cmd_approvals, "bots": cmd_bots,
         "approve": lambda m: _decide(m, True), "deny": lambda m: _decide(m, False), "tell": cmd_tell,
         "pause": team_cmd("pause"), "resume": team_cmd("resume"), "halt": team_cmd("stop"), "start": team_cmd("start"),
         "restart": team_cmd("restart"), "panic": lambda m: {"ok": True, **run(engine.team.stop(panic=True), timeout=60)},
         "pause_bot": team_cmd("pause_bot"), "resume_bot": team_cmd("resume_bot"), "stop_bot": team_cmd("stop_bot"),
         "preapprove": cmd_preapprove, "budget": cmd_budget, "snapshot": cmd_snapshot, "tray": cmd_tray},
    )
    if not instance.acquire():
        reply = send_command({"cmd": "show"})
        log.info("another instance is running; asked it to show (reply=%s)", reply)
        print("OmniBots is already running; brought its window to the front.")
        return ALREADY_RUNNING

    log.info("OmniBots %s starting (home=%s)", __version__, paths.home)

    # ── engine + window ────────────────────────────────────────────────
    try:
        engine.start()
    except Exception as exc:
        log.exception("startup failed")
        print(f"OmniBots failed to start: {exc}", file=sys.stderr)
        instance.release()
        return STARTUP_FAILED

    if not args.no_window:
        # A11: Omi's bot window is the main window, live-wired to the engine (ui/live.py)
        from omnibots.ui.live import LiveUI

        live = LiveUI(engine)
        window = live.open_bot("omi")

        # A11.b.01: the tray is the control center; closing a window only hides it
        from PySide6.QtWidgets import QSystemTrayIcon
        if QSystemTrayIcon.isSystemTrayAvailable():
            from omnibots.ui.bot_window import BotWindow
            from omnibots.ui.tray import Tray

            tray = Tray(engine, live)
            tray.show()
            window.quit_on_close = False
            app.setQuitOnLastWindowClosed(False)
            BotWindow.on_first_hide = lambda: tray.icon.showMessage(
                "OmniBots is still running", "Your bots keep working. Click Omi in the tray for the menu, double-click to reopen.",
                omi_icon(), 5000)
        else:
            log.warning("no system tray: closing Omi's window quits the app")

    app.aboutToQuit.connect(engine.stop)
    if args.exit_after > 0:
        QTimer.singleShot(int(args.exit_after * 1000), app.quit)
    if args.no_window:
        app.setQuitOnLastWindowClosed(False)

    code = app.exec()
    engine.stop()  # no-op if aboutToQuit already ran it
    instance.release()
    log.info("OmniBots exited cleanly (code %s)", code)
    return code
