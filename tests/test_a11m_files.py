"""A11.m.03-05 (user, 2026-09-26): the File Explorer's File and Edit actions, the built-in editor,
Open folder → the bots work there, and Settings → Folders."""

from __future__ import annotations

import asyncio
import concurrent.futures
import sys
import time
from pathlib import Path

import pytest
from qt_helpers import qapp

from omnibots.ui.files import EditorTabs, FileOps
from omnibots.ui.widgets import ChatPanel

app = qapp()


def pump(secs=0.3):
    end = time.monotonic() + secs
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.02)


def test_file_ops(tmp_path):
    a = FileOps.new_file(tmp_path, "notes.md")
    with pytest.raises(FileExistsError):
        FileOps.new_file(tmp_path, "notes.md")
    d = FileOps.new_folder(tmp_path, "assets")
    b = FileOps.rename(a, "readme.md")
    assert b.name == "readme.md" and not a.exists()
    with pytest.raises(ValueError):
        FileOps.rename(b, "bad:name.md")
    c = FileOps.duplicate(b)
    assert c.name == "readme copy.md"
    FileOps.copy([b])
    pasted = FileOps.paste(d)
    assert pasted == [d / "readme.md"] and b.exists()
    FileOps.copy([c], cut=True)
    moved = FileOps.paste(d)
    assert moved == [d / "readme copy.md"] and not c.exists() and FileOps.clipboard == ([], False)
    again = FileOps.paste(d)                                   # a cut pastes once
    assert again == []


@pytest.mark.skipif(sys.platform != "win32", reason="the Windows Recycle Bin")
def test_delete_goes_to_the_real_recycle_bin(tmp_path):
    import win32com.client
    marker = f"omnibots-recycle-test-{int(time.time() * 1000)}.txt"
    f = tmp_path / marker
    f.write_text("restore me", encoding="utf-8")
    FileOps.to_recycle_bin([f])
    assert not f.exists()
    shell = win32com.client.Dispatch("Shell.Application")
    items = [i for i in shell.NameSpace(10).Items() if i.Name.startswith(marker.rsplit(".", 1)[0])]
    assert items, "the file is not in the Recycle Bin"
    for i in items:                                             # clean up: empty just our test file from the bin
        import os
        os.remove(i.Path)


def test_editor_tabs_open_edit_save_and_reload(tmp_path):
    f = tmp_path / "index.html"
    f.write_text("<h1>Hello</h1>\n", encoding="utf-8")
    asked, where = [], [tmp_path / "copy.html"]
    tabs = EditorTabs(ChatPanel("#2f7dff"), ask=lambda t, x: asked.append(x) or "cancel", ask_path=lambda start: where[0])
    ed = tabs.open_file(f)
    assert tabs.count() == 2 and tabs.tabText(0).endswith("Chat") and tabs.open_file(f) is ed      # no duplicate tab
    assert ed.text.toPlainText() == "<h1>Hello</h1>\n" and ed.title == "index.html"
    ed.text.appendPlainText("<p>edited</p>")
    assert ed.dirty and tabs.tabText(1) == "index.html ●"
    assert tabs.close_tab(1) is False and asked                                     # unsaved: asked, Cancel keeps it
    assert tabs.save() == f and not ed.dirty and "<p>edited</p>" in f.read_text(encoding="utf-8")
    f.write_text("<h1>changed by a bot</h1>\n", encoding="utf-8")                   # a bot edits the open file
    pump(1.0)
    assert ed.text.toPlainText() == "<h1>changed by a bot</h1>\n"
    assert tabs.save(save_as=True) == where[0] and ed.path == where[0] and where[0].exists()
    img = tmp_path / "logo.png"
    from PySide6.QtGui import QImage
    QImage(8, 8, QImage.Format.Format_ARGB32).save(str(img))
    assert tabs.open_file(img).is_image
    assert tabs.close_tab(0) is False                                               # Chat can't be closed


class FakeEngine:
    signals = None
    computers = None

    def __init__(self, tmp):
        self.runner = type("R", (), {"active": {}})()
        self.tmp, self.calls, self.out = tmp, [], None
        self.bots = [{"id": "omi", "name": "Omi", "role": "boss", "description": "", "status": "idle", "model": "m",
                      "seat": None, "usage_pct": 0.0, "workspace": str(tmp), "active": False}]

    def submit(self, coro):
        f = concurrent.futures.Future()
        f.set_result(asyncio.run(coro))
        return f

    async def ui_bots(self):
        return self.bots

    async def ui_history(self, bot_id):
        return {"events": [], "board": []}

    async def start_goal(self, text, info=None):
        self.calls.append((text, info))
        return "proj_1"

    def set_output_dir(self, folder):
        self.out = folder


def test_open_folder_makes_the_next_goal_work_there_and_menus_exist(tmp_path):
    from omnibots.omni.personality import load
    from omnibots.ui.live import LiveUI
    from omnibots.ui.taunts import Taunts
    eng = FakeEngine(tmp_path)
    live = LiveUI(eng, taunts=Taunts(load(None), seed=2))
    w = live.open_bot("omi")
    mine = tmp_path / "my_site"
    mine.mkdir()
    w.pick_folder = lambda start: str(mine)
    w.acts["open_folder"].trigger()
    assert live.work_folder == mine and w.files.root == mine and w.files.path_label.text().startswith("📌")
    w.chat.input.setText("add a contact page")
    w.chat._send()
    assert eng.calls == [("add a contact page", {"folder": str(mine)})]
    files = [a.text() for a in w.file_menu.actions() if not a.isSeparator()]
    edits = [a.text() for a in w.edit_menu.actions() if not a.isSeparator()]
    assert {"New session  (saves this chat to Recent sessions)", "Close session", "Recent sessions",
            "New project  (the next goal gets a fresh folder)", "New file…", "Open file…",
            "Open folder…  (the bots work there)", "Create folder…"} <= set(files)
    assert {"Save", "Save as…", "Close tab"} <= set(files)
    assert {"Undo", "Redo", "Cut", "Copy", "Paste", "Find…", "Rename…", "Delete  (to the Recycle Bin)", "Duplicate",
            "Copy path", "Show in Windows Explorer"} <= set(edits)
    # Delete asks first; No keeps the file
    victim = mine / "old.txt"
    victim.write_text("x", encoding="utf-8")
    pump(0.5)
    w.files.select(victim)
    w.confirm = lambda t, x: False
    w.acts["delete"].trigger()
    assert victim.exists()


def test_settings_folders_tab_saves_and_tells_the_engine(tmp_path):
    from omnibots.settings import DEFAULT_SETTINGS_TOML, load_settings
    from omnibots.ui.settings_window import SettingsWindow
    p = tmp_path / "settings.toml"
    p.write_text(DEFAULT_SETTINGS_TOML, encoding="utf-8")
    eng = FakeEngine(tmp_path)
    s = SettingsWindow(engine=eng, settings_path=p)
    new = tmp_path / "bots out"
    s.folders.picker.edit.setText(str(new))
    s.folders.apply()
    assert load_settings(p)["output"]["folder"] == str(new) and eng.out == new and new.is_dir()
    s.folders.picker.edit.setText("relative/path")
    s.folders.apply()
    assert load_settings(p)["output"]["folder"] == str(new) and "full path" in s.folders.status.text()
