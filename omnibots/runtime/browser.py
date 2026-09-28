"""The browser tools (PLAN.md A10.a.01, ADR-9 "APIs before clicking").

A bot that needs the web gets a Chromium controlled through Playwright, with a
PERSISTENT profile per bot (and per account) under `profiles/<bot>/<account>/`,
so cookies and logins survive between jobs (the account-handoff flow, A10.c.02,
fills them once). One page at a time per bot.

Safety:
  - Navigation is guarded by the same public-URL check as web_fetch: a bot can't
    drive the browser to loopback / private / link-local addresses (SSRF) unless
    the job explicitly allows a local dev server.
  - Everything the page says (text, titles, element labels) is tagged
    [UNTRUSTED WEB CONTENT]: it is data, never instructions (indirect prompt
    injection).
  - Typing into password fields uses fill_secret({{secret:name}}): the value
    comes from the vault in the tool layer, is never shown to the model, and is
    redacted everywhere. Using it is R3.
  - navigate/read/screenshot are R2; click/type/press/select are R3 (they act on
    a live site as the user). submit-like clicks the model must justify.
Screenshots are saved into the bot's workspace so they can back a claim or an
approval rehearsal.
"""

from __future__ import annotations

import asyncio
import base64
from pathlib import Path
from typing import Any

from omnibots.runtime.tools import Tool, ToolContext
from omnibots.runtime.web_tools import assert_public_url, html_to_text
from omnibots.security.untrusted import wrap

MAX_TEXT = 12000


class BrowserSession:
    """One bot's Chromium context + page, opened lazily and reused across tool calls."""

    def __init__(self, profile_dir: Path, *, headless: bool = True):
        self.profile_dir = profile_dir
        self.headless = headless
        self._pw = None
        self._ctx = None
        self._page = None
        self._lock = asyncio.Lock()
        self.current_url = ""            # updated on every navigation, so the vault can host-check a fill_secret

    async def page(self):
        async with self._lock:
            if self._page is not None and not self._page.is_closed():
                return self._page
            if self._pw is None:
                from playwright.async_api import async_playwright
                self._pw = await async_playwright().start()
            self.profile_dir.mkdir(parents=True, exist_ok=True)
            # a persistent context = the bot's saved cookies/logins for this account
            self._ctx = await self._pw.chromium.launch_persistent_context(
                str(self.profile_dir), headless=self.headless, viewport={"width": 1280, "height": 900},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) OmniBots/0.1 Chrome/153 Safari/537.36",
                args=["--disable-blink-features=AutomationControlled"])
            self._page = self._ctx.pages[0] if self._ctx.pages else await self._ctx.new_page()
            self._page.set_default_timeout(20000)
            return self._page

    async def close(self) -> None:
        async with self._lock:
            try:
                if self._ctx is not None:
                    await self._ctx.close()
            finally:
                if self._pw is not None:
                    await self._pw.stop()
                self._pw = self._ctx = self._page = None


async def _describe_page(page) -> str:
    """A compact, model-friendly view: URL, title, visible text, and the interactive elements."""
    title = await page.title()
    body = ""
    try:
        body = await page.inner_text("body")
    except Exception:
        body = html_to_text(await page.content())
    body = body.strip()
    if len(body) > MAX_TEXT:
        body = body[:MAX_TEXT] + "\n…[page text truncated]"
    controls = await page.eval_on_selector_all(
        "a[href], button, input, textarea, select, [role=button], [role=link]",
        """els => els.slice(0, 60).map((e, i) => {
            const label = (e.innerText || e.value || e.getAttribute('aria-label') || e.getAttribute('placeholder')
                           || e.getAttribute('name') || e.getAttribute('title') || '').trim().slice(0, 60);
            const type = e.tagName.toLowerCase() + (e.type ? ':'+e.type : '');
            return {i, type, label};
        }).filter(c => c.label || c.type.startsWith('input'))""")
    lines = [f"[{c['i']}] {c['type']} — {c['label']}" for c in controls]
    # the element labels are page text too: inside the wrapper (A16.a)
    return wrap(f"{body}\n\nINTERACTIVE ELEMENTS (use the [n] index with click/type):\n" + "\n".join(lines),
                source=str(page.url), header=f"TITLE: {title}")


async def _nth(page, index: int):
    els = await page.query_selector_all("a[href], button, input, textarea, select, [role=button], [role=link]")
    if index < 0 or index >= len(els):
        raise IndexError(f"no element [{index}] (the page has {len(els)}; call browser_read again)")
    return els[index]


def browser_tools(session_for) -> list[Tool]:
    """`session_for(ctx) -> BrowserSession`: one persistent session per bot."""
    s = {"type": "string"}
    i = {"type": "integer"}

    async def _shot(page, ctx: ToolContext, note: str = "") -> str:
        data = await page.screenshot(full_page=False)
        name = f"screenshot-{int(asyncio.get_running_loop().time() * 1000) % 100000}.png"
        (ctx.workspace / name).write_bytes(data)
        return f"{name} ({len(data)} bytes){' — ' + note if note else ''}"

    async def navigate(args: dict[str, Any], ctx: ToolContext) -> str:
        url = str(args.get("url") or "")
        try:
            await assert_public_url(url, allow_loopback=bool(args.get("allow_internal")))
        except ValueError as exc:
            return f"ERROR: {exc}"
        sess = session_for(ctx)
        page = await sess.page()
        try:
            await page.goto(url, wait_until="domcontentloaded")
        except Exception as exc:
            return f"ERROR: navigate failed: {type(exc).__name__}: {str(exc)[:200]}"
        sess.current_url = page.url
        await ctx.event("terminal", f"→ {page.url}")
        return await _describe_page(page)

    async def read(args: dict[str, Any], ctx: ToolContext) -> str:
        page = await session_for(ctx).page()
        if page.url in ("about:blank", ""):
            return "the browser has no page yet; use browser_navigate first"
        return await _describe_page(page)

    async def screenshot(args: dict[str, Any], ctx: ToolContext) -> str:
        page = await session_for(ctx).page()
        return await _shot(page, ctx, "saved to your workspace")

    async def click(args: dict[str, Any], ctx: ToolContext) -> str:
        page = await session_for(ctx).page()
        try:
            el = await _nth(page, int(args.get("index", -1)))
            await el.scroll_into_view_if_needed()
            await el.click()
            await page.wait_for_load_state("domcontentloaded")
        except Exception as exc:
            return f"ERROR: click failed: {type(exc).__name__}: {str(exc)[:200]}"
        await asyncio.sleep(0.3)
        return await _describe_page(page)

    async def type_text(args: dict[str, Any], ctx: ToolContext) -> str:
        page = await session_for(ctx).page()
        try:
            el = await _nth(page, int(args.get("index", -1)))
            await el.scroll_into_view_if_needed()
            await el.fill(str(args.get("text", "")))
        except Exception as exc:
            return f"ERROR: type failed: {type(exc).__name__}: {str(exc)[:200]}"
        if args.get("submit"):
            await el.press("Enter")
            await page.wait_for_load_state("domcontentloaded")
            await asyncio.sleep(0.3)
            return await _describe_page(page)
        return f"typed into [{args.get('index')}]"

    async def fill_secret(args: dict[str, Any], ctx: ToolContext) -> str:
        # `text` arrives already resolved from the vault by the tool layer (secret_args).
        page = await session_for(ctx).page()
        try:
            el = await _nth(page, int(args.get("index", -1)))
            await el.fill(str(args.get("text", "")))
        except Exception as exc:
            return f"ERROR: fill_secret failed: {type(exc).__name__}: {str(exc)[:200]}"
        return f"filled [{args.get('index')}] with the stored credential"

    async def press(args: dict[str, Any], ctx: ToolContext) -> str:
        page = await session_for(ctx).page()
        await page.keyboard.press(str(args.get("key") or "Enter"))
        await page.wait_for_load_state("domcontentloaded")
        await asyncio.sleep(0.3)
        return await _describe_page(page)

    async def rehearse_click(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        page = await session_for(ctx).page()
        try:
            el = await _nth(page, int(args.get("index", -1)))
            label = (await el.inner_text() or await el.get_attribute("value") or "").strip()[:80]
        except Exception:
            label = "(unknown)"
        return {"url": page.url, "element": f"[{args.get('index')}] {label}", "screenshot": await _shot(page, ctx)}

    return [
        Tool("browser_navigate", "Open a URL in your browser and get the page text + a numbered list of clickable elements. Public sites only.",
             {"type": "object", "properties": {"url": s, "allow_internal": {"type": "boolean"}}, "required": ["url"]},
             "R2", navigate, timeout=60, path_arg=None, summary=lambda a: f"browser: open {str(a.get('url',''))[:70]}"),
        Tool("browser_read", "Re-read the current browser page (text + numbered elements). Call it after a click or type to see what changed.",
             {"type": "object", "properties": {}}, "R2", read, timeout=40, path_arg=None, summary=lambda a: "browser: read page"),
        Tool("browser_screenshot", "Save a screenshot of the current page into your workspace (evidence, or to look at with describe_image).",
             {"type": "object", "properties": {}}, "R2", screenshot, timeout=40, path_arg=None, summary=lambda a: "browser: screenshot"),
        Tool("browser_click", "Click the element with this [index] from the page's element list. Acts on the live site (needs approval).",
             {"type": "object", "properties": {"index": i}, "required": ["index"]},
             "R3", click, timeout=60, path_arg=None, rehearse=rehearse_click, summary=lambda a: f"browser: click element [{a.get('index')}]"),
        Tool("browser_type", "Type text into the input/textarea with this [index]. submit:true presses Enter after. Acts on the live site.",
             {"type": "object", "properties": {"index": i, "text": s, "submit": {"type": "boolean"}}, "required": ["index", "text"]},
             "R3", type_text, timeout=45, path_arg=None, summary=lambda a: f"browser: type into [{a.get('index')}]"),
        Tool("browser_fill_secret", "Type a stored credential into a field: text must be {{secret:name}} (e.g. a password). The value is never shown to you.",
             {"type": "object", "properties": {"index": i, "text": s}, "required": ["index", "text"]},
             "R3", fill_secret, timeout=45, path_arg=None, secret_args=("text",),
             secret_target=lambda a, ctx: (session_for(ctx).current_url if ctx else None),   # host-check against the live page
             summary=lambda a: f"browser: fill [{a.get('index')}] with a stored secret"),
        Tool("browser_press", "Press a keyboard key on the page (e.g. Enter, Escape, PageDown). Acts on the live site.",
             {"type": "object", "properties": {"key": s}, "required": ["key"]},
             "R3", press, timeout=45, path_arg=None, summary=lambda a: f"browser: press {a.get('key')}"),
    ]
