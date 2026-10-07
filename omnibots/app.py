"""Entry point: python -m omnibots [options]

  python -m omnibots                 start the app (or focus the running one)
  python -m omnibots --send status   send a control command to the running app
  python -m omnibots --exit-after 5  start, then exit cleanly after N seconds (tests)
  python -m omnibots --no-window     no main window (tests / headless checks)
"""

from __future__ import annotations

import argparse
from pathlib import Path
import json
import time
import logging
import os
import re
import sys

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from omnibots import __version__
from omnibots.engine import Engine
from omnibots.ipc import Deferred, SingleInstance, send_command
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
    p.add_argument("--wait-pid", type=int, default=0, metavar="PID", help=argparse.SUPPRESS)   # a restart waits for the old instance
    p.add_argument("--version", action="version", version=f"omnibots {__version__}")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    if args.wait_pid:
        from omnibots.backup import wait_for_pid
        wait_for_pid(args.wait_pid)
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

    output = settings["output"]["folder"]                      # A11.m.01 ("" = not chosen yet: asked below)
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
        output_dir=output or None,
        approvals_ask_from=str(settings.get("approvals", {}).get("ask_from", "R4")),
        skill_folders=list(settings.get("skills", {}).get("folders", [])),
        backup_settings=settings.get("backup", {}),
        initiative=settings.get("initiative", {}),
    )

    def cmd_show(_msg):
        if window is not None:
            window.bring_to_front()
        return {"ok": True}

    # A15.a.01: engine work answers later (a Deferred), so a pipe command never freezes the window.
    def later(coro, then, timeout=30):
        return Deferred(engine.submit(coro), then, timeout)

    def cmd_status(_msg):
        return later(engine.status(), lambda status: {"ok": True, "status": status}, timeout=5)

    def cmd_goal(msg):
        text = str(msg.get("text") or "").strip()
        if not text:
            return {"ok": False, "error": "goal needs text"}
        return later(engine.start_goal(text), lambda pid: {"ok": True, "project_id": pid})

    def cmd_tell(msg):
        bot, text = str(msg.get("id") or "omi"), str(msg.get("text") or "").strip()
        if not text:
            return {"ok": False, "error": "tell needs text"}
        return later(engine.tell(bot, text), lambda _: {"ok": True})

    def team_cmd(name):
        def handler(msg):
            fn = getattr(engine.team, name)
            if name.endswith("_bot"):
                bot = str(msg.get("id") or "")
                return later(fn(bot), lambda ok: {"ok": bool(ok), "bot": bot})
            return later(fn(panic=True) if name == "panic" else fn(), lambda r: {"ok": True, **r}, timeout=60)
        return handler

    def cmd_bots(_msg):
        async def listing():
            return [{"id": b.id, "name": b.name, "role": b.role, "status": b.status, "chain": b.chain[:2]}
                    for b in await engine.registry.list()]
        return later(listing(), lambda bots: {"ok": True, "bots": bots}, timeout=5)

    def cmd_approvals(_msg):
        return {"ok": True, "pending": engine.approvals.list_pending()}

    def _decide(msg, approved):
        aid = str(msg.get("id") or msg.get("text") or "")
        return later(engine.approvals.decide(aid, approved, "user via control pipe"),
                     lambda done: {"ok": bool(done), **({} if done else {"error": f"no pending approval {aid!r}"})}, timeout=5)

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

    def cmd_rate(msg):
        # --send rate --id <project_id> --text "up: nice work"   (or "down: …"; "claim 12 up: …" rates one claim)
        m = re.match(r"\s*(?:claim\s+(\d+)\s+)?(up|down|👍|👎)\s*:?\s*(.*)", str(msg.get("text") or ""), re.S | re.I)
        pid = str(msg.get("id") or "")
        if not m or not pid:
            return {"ok": False, "error": "rate needs --id <project_id> and --text 'up|down[: note]' (or 'claim N up: …')"}
        verdict = 1 if m.group(2).lower() in ("up", "👍") else -1
        claim = int(m.group(1)) if m.group(1) else None
        return later(engine.rate(pid, verdict, m.group(3).strip(), claim), lambda r: {"ok": True, **r}, timeout=10)

    def cmd_ceiling(msg):
        bot, level = str(msg.get("id") or ""), str(msg.get("text") or "").strip().upper()
        if not bot or not level:
            return {"ok": False, "error": "ceiling needs --id <bot> and --text R0..R5"}
        return later(engine.set_risk_ceiling(bot, level), lambda r: {"ok": True, "bot": bot, "ceiling": r}, timeout=10)

    def cmd_proposals(_msg):                         # A17.e.01: Omi's ideas waiting in the inbox
        return later(engine.ui_proposals(), lambda r: {"ok": True, "proposals": r}, timeout=10)

    def cmd_proposal(msg):
        pid, verb = str(msg.get("id") or ""), str(msg.get("text") or "").strip().lower()
        if not pid or verb not in ("accept", "reject"):
            return {"ok": False, "error": "proposal needs --id <proposal> and --text accept|reject"}
        return later(engine.decide_proposal(pid, verb == "accept"), lambda r: {"ok": True, "proposal": r}, timeout=20)

    def cmd_journal(_msg):
        return later(engine.ui_journal(14), lambda r: {"ok": True, "journal": r}, timeout=10)

    def cmd_budget(_msg):
        return later(engine.budget.snapshot(), lambda b: {"ok": True, "budget": b}, timeout=10)

    # Found 2026-09-28 (the Omni /omnibots launcher): a stop that came while the first-run folder window or the splash
    # was up was lost (app.quit does nothing before app.exec runs). Now it closes that window and startup ends there.
    stop_requested = {"now": False}

    def cmd_stop(_msg):
        stop_requested["now"] = True

        def go():
            modal = QApplication.activeModalWidget()
            if modal is not None:
                modal.reject()
            app.quit()
        QTimer.singleShot(0, go)
        return {"ok": True}

    instance = SingleInstance(
        paths.lock_file,
        {"show": cmd_show, "status": cmd_status, "goal": cmd_goal, "stop": cmd_stop, "approvals": cmd_approvals, "bots": cmd_bots,
         "approve": lambda m: _decide(m, True), "deny": lambda m: _decide(m, False), "tell": cmd_tell,
         "pause": team_cmd("pause"), "resume": team_cmd("resume"), "halt": team_cmd("stop"), "start": team_cmd("start"),
         "restart": team_cmd("restart"), "panic": lambda m: later(engine.team.stop(panic=True), lambda r: {"ok": True, **r}, timeout=60),
         "pause_bot": team_cmd("pause_bot"), "resume_bot": team_cmd("resume_bot"), "stop_bot": team_cmd("stop_bot"),
         "preapprove": cmd_preapprove, "budget": cmd_budget, "rate": cmd_rate, "ceiling": cmd_ceiling, "proposals": cmd_proposals, "proposal": cmd_proposal, "journal": cmd_journal, "snapshot": cmd_snapshot, "tray": cmd_tray},
    )
    if not instance.acquire():
        reply = send_command({"cmd": "show"})
        log.info("another instance is running; asked it to show (reply=%s)", reply)
        print("OmniBots is already running; brought its window to the front.")
        return ALREADY_RUNNING

    log.info("OmniBots %s starting (home=%s)", __version__, paths.home)
    # A16.c.03: a restore chosen in the tray happens now, before the database is opened
    from omnibots.backup import apply_pending_restore
    restored = apply_pending_restore(paths.home, int(settings.get("backup", {}).get("keep", 7)))
    if restored:
        log.info("backup: %s", restored)

    # ── A11.m.01: where the bots' projects go, asked once on the first real start ──
    # (headless and timed test runs never ask; their projects stay inside their own home)
    if not output and not args.no_window and args.exit_after <= 0:
        from omnibots.settings import save_setting
        from omnibots.ui.setup_dialog import ask_output_folder
        output = str(ask_output_folder())
        if stop_requested["now"]:                          # stopped while asking: nothing chosen, nothing saved
            log.info("stop requested during first-run setup; exiting")
            instance.release()
            return 0
        save_setting(paths.settings_file, "output", "folder", output)
        engine.set_output_dir(Path(output))

    # ── engine + window ────────────────────────────────────────────────
    splash = None
    if not args.no_window and args.exit_after <= 0 and settings["app"].get("splash", True):
        # the launch intro, synced to the real startup (ui/splash.py)
        from PySide6.QtCore import QEventLoop

        from omnibots.ui.splash import Splash
        splash = Splash()
        engine.signals.startup_stage.connect(splash.set_stage)
        splash.start()
    try:
        if splash is None:
            engine.start()
        else:
            engine.begin()
            waiting = QEventLoop()
            poll = QTimer()
            started_at = time.monotonic()

            def check():
                if engine.started or time.monotonic() - started_at > 30:
                    poll.stop()
                    err = getattr(engine, "_start_error", None)
                    splash.set_ready(None if engine.started and not err else f"OmniBots failed to start: {err or 'timed out'}")
            poll.timeout.connect(check)
            poll.start(50)
            splash.finished.connect(waiting.quit)
            waiting.exec()
            engine.wait_started(0.1)                        # raises the startup error, if any
    except Exception as exc:
        if splash is not None:
            splash.close()
        log.exception("startup failed")
        print(f"OmniBots failed to start: {exc}", file=sys.stderr)
        instance.release()
        return STARTUP_FAILED

    if not args.no_window:
        # A11: Omi's bot window is the main window, live-wired to the engine (ui/live.py)
        from omnibots.ui.live import LiveUI

        live = LiveUI(engine)
        window = live.open_bot("omi")
        from omnibots.ui.stall_watch import StallWatch
        stall_watch = StallWatch(paths.logs / "ui-stalls.log")   # a frozen window leaves its stack in the log
        if splash is not None:
            splash.close()                                  # Omi's window takes over from the intro
            window.bring_to_front()

        # A11.b.01: the tray is the control center; closing a window only hides it
        from PySide6.QtWidgets import QSystemTrayIcon
        if QSystemTrayIcon.isSystemTrayAvailable():
            from omnibots.ui.bot_window import BotWindow
            from omnibots.ui.tray import Tray

            def restart_app() -> None:
                """Start a fresh instance that waits for this one to exit, then quit (A16.c.03)."""
                import subprocess
                subprocess.Popen([sys.executable, "-m", "omnibots", "--wait-pid", str(os.getpid())],
                                 creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
                app.quit()

            def update_app() -> None:
                """A16.d: back up, stop what runs (asked first), then the updater takes over and the app quits."""
                from PySide6.QtWidgets import QMessageBox
                from omnibots.updater import install_layout, start_update
                layout = install_layout()
                busy = bool(engine.runner.active) or any(not t.done() for t in engine.orchestrator.boss_tasks.values())
                if busy and QMessageBox.question(None, "Update now?", "Bots are working. Update now? Their work is cut off and "
                                                 "carries on by itself after the update.") != QMessageBox.StandardButton.Yes:
                    return
                try:
                    engine.submit(engine.backup_now("before-update")).result(timeout=120)
                except Exception as exc:
                    if QMessageBox.question(None, "Backup failed", f"The backup failed ({exc}). Update anyway?") != QMessageBox.StandardButton.Yes:
                        return
                start_update(os.getpid(), layout)
                app.quit()

            tray = Tray(engine, live, restart_app=restart_app, home=paths.home, update_app=update_app)
            if restored:
                QTimer.singleShot(1500, lambda: tray.icon.showMessage("OmniBots", f"Backup: {restored}"))
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

    if stop_requested["now"]:                               # a stop during the splash: quit as soon as the loop runs
        QTimer.singleShot(0, app.quit)
    code = app.exec()
    engine.stop()  # no-op if aboutToQuit already ran it
    instance.release()
    log.info("OmniBots exited cleanly (code %s)", code)
    return code
