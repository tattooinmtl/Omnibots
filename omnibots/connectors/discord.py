"""Discord (PLAN.md A17.h.02): the team in a Discord direct message, on the phone or in any browser.

Setup: make an application + bot at discord.com/developers (free), put its token in the vault as `discord_bot_token`,
share a server with the bot (or add it as an app), and put YOUR Discord user id in settings.toml [discord]
owner_user_id (Discord → Settings → Advanced → Developer Mode, then right-click yourself → Copy User ID).
OmniBots opens a DM with you and reads it over the REST API every few seconds: nothing listens on the PC (buttons
would need an inbound server, so approvals and ideas are answered with a ✅ or ❌ reaction).
"""

from __future__ import annotations

import asyncio
import logging
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from omnibots.connectors.bridge import ChatBridge, Outgoing

log = logging.getLogger(__name__)
API = "https://discord.com/api/v10"
YES, NO = "✅", "❌"
MAX_FILE = 20 * 1024 * 1024


class DiscordBridge(ChatBridge):
    name = "Discord"

    def __init__(self, engine, token: str, *, owner: str | None, client_factory=None, poll_seconds: float = 3.0):
        super().__init__(engine, owner=owner)
        self.token = token
        self.client_factory = client_factory or (lambda: httpx.AsyncClient(timeout=httpx.Timeout(30.0)))
        self.poll_seconds = poll_seconds
        self.dm: str | None = None
        self.me: dict[str, Any] = {}
        self.last_id: str | None = None
        self.waiting: dict[str, str] = {}            # discord message id -> "ap:<id>" / "pr:<id>" (answered by reaction)
        self._client: httpx.AsyncClient | None = None
        self.files_dir = Path(tempfile.mkdtemp(prefix="omnibots-discord-"))

    async def _request(self, method: str, path: str, **kw) -> httpx.Response:
        headers = {"Authorization": f"Bot {self.token}", "User-Agent": "OmniBots (https://omnibots.globalwarningnetworks.com, 1)"}
        url = path if path.startswith("http") else f"{API}{path}"
        for _ in range(3):
            if self._client is not None:
                r = await self._client.request(method, url, headers=headers, **kw)
            else:
                async with self.client_factory() as c:
                    r = await c.request(method, url, headers=headers, **kw)
            if r.status_code == 429:                        # rate limited: wait what Discord says
                await asyncio.sleep(min(10.0, float((r.json() if r.content else {}).get("retry_after", 1))))
                continue
            return r
        return r

    async def api(self, method: str, path: str, **kw) -> Any:
        r = await self._request(method, path, **kw)
        if r.status_code >= 400:
            msg = (r.json() if r.content else {}).get("message", "") if "json" in r.headers.get("content-type", "") else r.text[:200]
            raise RuntimeError(f"Discord {method} {path.split('?')[0]}: HTTP {r.status_code} {msg}")
        return r.json() if r.content and "json" in r.headers.get("content-type", "") else None

    async def open_dm(self) -> str:
        if not self.dm:
            ch = await self.api("POST", "/users/@me/channels", json={"recipient_id": self.owner})
            self.dm = str(ch["id"])
        return self.dm

    async def send(self, chat: str, msg: Outgoing) -> Any:
        try:
            dm = await self.open_dm()
            text = msg.text
            if msg.buttons:
                text += f"\n\nReact {YES} for “{msg.buttons[0].text.strip('✔▶ ')}” or {NO} for “{msg.buttons[1].text.strip('✖ ')}”."
            sent = await self.api("POST", f"/channels/{dm}/messages", json={"content": text[:2000]})
            if msg.buttons and msg.key:
                for emoji in (YES, NO):
                    await self.api("PUT", f"/channels/{dm}/messages/{sent['id']}/reactions/{quote(emoji)}/@me")
                self.waiting[str(sent["id"])] = msg.key
            self.last_id = max(self.last_id or "0", str(sent["id"]), key=int)
            return (dm, str(sent["id"]), text)
        except (RuntimeError, httpx.HTTPError, KeyError) as exc:
            log.warning("Discord send failed: %s", exc)
            return None

    async def mark_done(self, handle: Any, note: str) -> None:
        dm, mid, text = handle
        self.waiting.pop(mid, None)
        try:
            await self.api("PATCH", f"/channels/{dm}/messages/{mid}", json={"content": f"{text}\n\n{note}"[:2000]})
        except (RuntimeError, httpx.HTTPError):
            pass

    async def _owner_reacted(self, dm: str, mid: str, emoji: str) -> bool:
        users = await self.api("GET", f"/channels/{dm}/messages/{mid}/reactions/{quote(emoji)}")
        return any(str(u.get("id")) == self.owner for u in users or [])

    async def poll_once(self) -> int:
        dm = await self.open_dm()
        params = {"limit": 50} | ({"after": self.last_id} if self.last_id else {})
        msgs = await self.api("GET", f"/channels/{dm}/messages", params=params) or []
        if self.last_id is None:                          # first poll: start from now, don't replay the history
            self.last_id = max((m["id"] for m in msgs), default="0", key=int)
            return 0
        n = 0
        for m in sorted(msgs, key=lambda x: int(x["id"])):
            self.last_id = max(self.last_id, m["id"], key=int)
            if str(m.get("author", {}).get("id")) != self.owner:
                continue                                    # our own messages, or anyone else's
            files = []
            for a in m.get("attachments") or []:
                if int(a.get("size") or 0) <= MAX_FILE:
                    r = await self._request("GET", a["url"])
                    if r.status_code < 400:
                        p = self.files_dir / a.get("filename", f"file-{a['id']}")
                        p.write_bytes(r.content)
                        files.append(p)
            await self.on_message(self.owner, m.get("content") or "", files)
            n += 1
        for mid, key in list(self.waiting.items()):          # answers by reaction
            for emoji, verdict in ((YES, "1"), (NO, "0")):
                if await self._owner_reacted(dm, mid, emoji):
                    await self.on_button(self.owner, f"{key}:{verdict}")
                    break
        return n

    async def run(self) -> None:
        if not self.owner:
            log.error("Discord: settings.toml [discord] owner_user_id is empty; the connector is off")
            return
        async with self.client_factory() as c:
            self._client = c
            try:
                self.me = await self.api("GET", "/users/@me")
                await self.open_dm()
            except RuntimeError as exc:
                log.error("Discord: %s; the connector is off", exc)
                return
            log.info("Discord connected as %s", self.me.get("username"))
            wait = self.poll_seconds
            while True:
                try:
                    await self.poll_once()
                    wait = self.poll_seconds
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log.warning("Discord poll failed: %s", exc)
                    wait = min(60.0, wait * 2)
                await asyncio.sleep(wait)
