"""A9.c.04 (user, 2026-10-27): Settings → Tokens adds, tests and removes site tokens without
the value ever showing. A stand-in vault (same interface) and a real local HTTP server."""

from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QLabel, QLineEdit

from qt_helpers import qapp

from omnibots.security import token_catalog
from omnibots.security.token_catalog import CATALOG, Site, check
from omnibots.security.vault import SecretInfo
from omnibots.ui.settings_window import SettingsWindow
from omnibots.ui.tokens_page import TokensPage

qapp()

TOKEN = "github_pat_TESTvalue_1234567890abcdef"


class FakeVault:
    def __init__(self):
        self.values: dict[str, str] = {}
        self.meta: dict[str, SecretInfo] = {}

    async def set(self, name, value, *, kind="", note="", hosts=None):
        self.values[name] = value
        self.meta[name] = SecretInfo(name, kind, note, list(hosts or []))
        return self.meta[name]

    async def delete(self, name):
        self.values.pop(name, None)
        return self.meta.pop(name, None) is not None

    def names(self):
        return sorted(self.meta.values(), key=lambda s: s.name)

    def value(self, name):
        return self.values.get(name)


class Who:
    """Answers like a site's "who am I" call: 200 for the right token, 401 otherwise."""

    def __init__(self, body: dict | None = None):
        self.seen: list[str] = []
        outer = self
        reply = json.dumps(body or {"login": "tester"}).encode()

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                auth = self.headers.get("Authorization") or ""
                outer.seen.append(auth)
                ok = auth == f"Bearer {TOKEN}"
                self.send_response(200 if ok else 401)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(reply if ok else b"{}")

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_port}/user"

    def close(self):
        self.srv.shutdown()


@pytest.fixture
def who():
    w = Who()
    yield w
    w.close()


def page(vault=None) -> TokensPage:
    return TokensPage(vault=vault or FakeVault(), run=asyncio.run)


def select(p: TokensPage, key: str) -> None:
    for i in range(p.list.count()):
        if p.list.item(i).data(256) == key:
            p.list.setCurrentRow(i)
            return
    raise AssertionError(key)


def visible_text(p: TokensPage) -> str:
    texts = [w.text() for w in p.findChildren(QLabel)] + [p.list.item(i).text() for i in range(p.list.count())]
    texts += [w.text() for w in p.findChildren(QLineEdit) if w.echoMode() != QLineEdit.EchoMode.Password]
    return "\n".join(texts)


def test_catalog_names_match_the_tools_and_every_site_is_scoped():
    keys = {s.key for s in CATALOG}
    # the names hosting.py / connectors already look up
    assert {"github_token", "vercel_token", "netlify_token", "cloudflare_token", "sftp_password",
            "telegram_bot_token", "discord_bot_token"} <= keys
    assert len(keys) == len(CATALOG)
    for s in CATALOG:
        if s.test_url and "{token}" not in s.test_url:
            assert s.hosts, s.key
        if s.test_url:
            from urllib.parse import urlparse
            host = urlparse(s.test_url.replace("{token}", "x")).hostname
            assert any(host == h or host.endswith("." + h) for h in s.hosts), s.key


def test_check_reports_who_and_never_the_token(who):
    site = Site("t", "Local", ("127.0.0.1",), test_url=who.url, who=("login",))
    ok, text = asyncio.run(check(site, TOKEN))
    assert ok and text == "Local accepts it: tester"
    ok, text = asyncio.run(check(site, "wrong-token-value"))
    assert not ok and "refused" in text and "wrong-token-value" not in text
    assert who.seen == [f"Bearer {TOKEN}", "Bearer wrong-token-value"]


def test_check_refuses_a_host_the_token_is_not_for(who):
    site = Site("t", "Local", ("api.github.com",), test_url=who.url)
    ok, text = asyncio.run(check(site, TOKEN))
    assert not ok and "refused" in text
    assert who.seen == []                                  # nothing was sent


def test_check_ok_false_with_http_200_is_a_refusal():
    w = Who({"ok": False, "error": "invalid_auth"})
    try:
        site = Site("t", "Slackish", ("127.0.0.1",), test_url=w.url)
        ok, text = asyncio.run(check(site, TOKEN))
        assert not ok and "invalid_auth" in text
    finally:
        w.close()


def test_save_test_and_remove_a_catalog_token(who, monkeypatch):
    local = Site("github_token", "GitHub", ("127.0.0.1",), test_url=who.url, who=("login",))
    monkeypatch.setattr(token_catalog, "CATALOG", (local,) + tuple(s for s in CATALOG if s.key != "github_token"))
    monkeypatch.setattr("omnibots.ui.tokens_page.CATALOG", token_catalog.CATALOG)
    monkeypatch.setattr("omnibots.ui.tokens_page.by_key",
                        lambda k: next((s for s in token_catalog.CATALOG if s.key == k), None))
    v = FakeVault()
    p = page(v)
    select(p, "github_token")
    assert p.name.isReadOnly() and p.hosts.text() == "127.0.0.1"
    p.value.setText(f"  {TOKEN}\n")
    p.save()
    assert v.values == {"github_token": TOKEN}
    assert v.meta["github_token"].hosts == ["127.0.0.1"]
    assert p.value.text() == ""                            # the box empties after saving
    assert "tester" in p.status.text()                     # saving tested it
    assert TOKEN not in visible_text(p)
    assert p.list.currentItem().text().startswith("✔")

    p.confirm = lambda *_a: True
    p.remove()
    assert v.values == {} and p.list.currentItem().text().startswith("○")


def test_custom_token_needs_a_name_and_asks_before_any_host():
    v = FakeVault()
    p = page(v)
    select(p, "")
    assert not p.name.isReadOnly()
    p.name.setText("bad name!")
    p.value.setText("value-123456")
    p.save()
    assert v.values == {} and "letters" in p.status.text()

    p.name.setText("shop_api")
    p.hosts.setText("")
    asked = []
    p.confirm = lambda *_a: asked.append(1) or False
    p.save()
    assert asked and v.values == {}

    p.hosts.setText("API.Shop.example, cdn.shop.example")
    p.value.setText("value-123456")
    p.save()
    assert v.meta["shop_api"].hosts == ["api.shop.example", "cdn.shop.example"]
    keys = [p.list.item(i).data(256) for i in range(p.list.count())]
    assert "shop_api" in keys                              # a custom one gets its own row


def test_paste_fills_the_masked_box_and_clears_the_clipboard():
    p = page()
    cb = QGuiApplication.clipboard()
    cb.setText(TOKEN)
    p.paste()
    assert p.value.text() == TOKEN and p.value.echoMode() == QLineEdit.EchoMode.Password
    assert cb.text() == ""


def test_spaces_in_the_value_are_refused():
    v = FakeVault()
    p = page(v)
    select(p, "vercel_token")
    p.value.setText("two words")
    p.save()
    assert v.values == {}


def test_auto_logins_are_hidden_and_the_tab_is_in_settings():
    v = FakeVault()
    asyncio.run(v.set("computer_ada", "s3cret-login", kind="computer"))
    p = page(v)
    assert all(p.list.item(i).data(256) != "computer_ada" for i in range(p.list.count()))
    w = SettingsWindow()
    assert "Tokens" in [w.tabs.tabText(i) for i in range(w.tabs.count())]
