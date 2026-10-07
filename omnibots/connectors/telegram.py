"""Telegram (PLAN.md A17.h.01): the team in your phone, and in any browser through web.telegram.org.

Setup: make a bot with @BotFather (free, two minutes), put its token in the vault as `telegram_bot_token`, start
OmniBots, then send "/pair <code>" to your bot (Omi shows the code). Long polling (getUpdates) only: nothing listens
on the PC. Approvals and Omi's ideas come with buttons; photos and files you send go into the project.
"""

from __future__ import annotations

import asyncio
import logging
import tempfile
from pathlib import Path
from typing import Any

import httpx

from omnibots.connectors.bridge import ChatBridge, Outgoing

log = logging.getLogger(__name__)
API = "https://api.telegram.org"
MAX_FILE = 20 * 1024 * 1024            # what the Bot API lets a bot download


class TelegramBridge(ChatBridge):
    name = "Telegram"

    def __init__(self, engine, token: str, *, owner: str | None, save_owner=None, client_factory=None, poll_timeout: int = 25):
        super().__init__(engine, owner=owner, save_owner=save_owner)
        self.token = token
        self.client_factory = client_factory or (lambda: httpx.AsyncClient(timeout=httpx.Timeout(poll_timeout + 15)))
        self.poll_timeout = poll_timeout
        self.offset = 0
        self.me: dict[str, Any] = {}
        self._client: httpx.AsyncClient | None = None
        self.files_dir = Path(tempfile.mkdtemp(prefix="omnibots-telegram-"))

    async def _request(self, method: str, url: str, **kw) -> httpx.Response:
        if self._client is not None:                       # the polling loop's client
            return await self._client.request(method, url, **kw)
        async with self.client_factory() as c:              # outside the loop (tests, one-off sends)
            return await c.request(method, url, **kw)

    async def api(self, method: str, **params) -> Any:
        r = await self._request("POST", f"{API}/bot{self.token}/{method}", json=params)
        data = r.json() if r.content else {}
        if not data.get("ok"):
            raise RuntimeError(f"Telegram {method}: {data.get('description') or r.status_code}")
        return data.get("result")

    async def send(self, chat: str, msg: Outgoing) -> Any:
        params: dict[str, Any] = {"chat_id": chat, "text": msg.text[:4096], "disable_web_page_preview": True}
        if msg.buttons:
            params["reply_markup"] = {"inline_keyboard": [[{"text": b.text, "callback_data": b.data[:64]} for b in msg.buttons]]}
        try:
            sent = await self.api("sendMessage", **params)
            return (chat, sent.get("message_id"), msg.text)
        except (RuntimeError, httpx.HTTPError) as exc:
            log.warning("Telegram send failed: %s", exc)
            return None

    async def mark_done(self, handle: Any, note: str) -> None:
        chat, mid, text = handle
        try:
            await self.api("editMessageText", chat_id=chat, message_id=mid, text=f"{text}\n\n{note}"[:4096])
        except (RuntimeError, httpx.HTTPError):
            pass

    async def download(self, file_id: str, name: str) -> Path | None:
        try:
            f = await self.api("getFile", file_id=file_id)
            if int(f.get("file_size") or 0) > MAX_FILE:
                return None
            r = await self._request("GET", f"{API}/file/bot{self.token}/{f['file_path']}")
            if r.status_code >= 400:
                return None
            out = self.files_dir / (name or Path(f["file_path"]).name)
            out.write_bytes(r.content)
            return out
        except (RuntimeError, httpx.HTTPError, KeyError):
            return None

    async def handle(self, update: dict[str, Any]) -> None:
        if "callback_query" in update:
            q = update["callback_query"]
            chat = str(q.get("message", {}).get("chat", {}).get("id") or q.get("from", {}).get("id"))
            note = await self.on_button(chat, q.get("data", ""))
            try:
                await self.api("answerCallbackQuery", callback_query_id=q["id"], text=note[:190])
            except (RuntimeError, httpx.HTTPError):
                pass
            return
        msg = update.get("message") or update.get("edited_message")
        if not msg:
            return
        chat = str(msg["chat"]["id"])
        if msg["chat"].get("type") != "private":
            return                                          # groups are never obeyed
        text = msg.get("text") or msg.get("caption") or ""
        files: list[Path] = []
        if self.is_owner(chat):
            if msg.get("photo"):
                biggest = max(msg["photo"], key=lambda p: p.get("file_size", 0))
                p = await self.download(biggest["file_id"], f"photo-{msg['message_id']}.jpg")
                files += [p] if p else []
            for kind in ("document", "video", "audio", "voice"):
                if msg.get(kind):
                    d = msg[kind]
                    p = await self.download(d["file_id"], d.get("file_name") or f"{kind}-{msg['message_id']}")
                    files += [p] if p else []
        await self.on_message(chat, text, files)

    async def poll_once(self) -> int:
        updates = await self.api("getUpdates", offset=self.offset, timeout=self.poll_timeout,
                                 allowed_updates=["message", "edited_message", "callback_query"])
        for u in updates or []:
            self.offset = max(self.offset, int(u["update_id"]) + 1)
            try:
                await self.handle(u)
            except Exception:
                log.exception("Telegram update failed")
        return len(updates or [])

    async def run(self) -> None:
        """Poll until cancelled; back off on errors (a wrong token stops after saying why)."""
        async with self.client_factory() as c:
            self._client = c
            try:
                self.me = await self.api("getMe")
            except RuntimeError as exc:
                log.error("Telegram: the token doesn't work (%s); the connector is off", exc)
                return
            log.info("Telegram connected as @%s (%s)", self.me.get("username"), "paired" if self.owner else "waiting for /pair")
            wait = 1.0
            while True:
                try:
                    await self.poll_once()
                    wait = 1.0
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log.warning("Telegram poll failed: %s", exc)
                    await asyncio.sleep(wait)
                    wait = min(60.0, wait * 2)
