"""The Ledger: proof-carrying results (PLAN.md ADR-12, A4.a.05, A4.a.07).

A worker never just says "done": it submits a claim with evidence. Evidence is
stored BY REFERENCE (a path, URL, command, or artifact id) plus a small detail
dict, never the full blob. Evidence is checked when it's submitted (the file
exists, the command recorded an exit code, the quote is non-empty). The boss
accepts or rejects each claim with a reason, and every step is posted to the
board.
"""

from __future__ import annotations

import asyncio
import html as htmllib
import json
import re
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

from omnibots.board.bus import MessageBus
from omnibots.board.types import topic_bot, topic_job
from omnibots.runtime.tools import Tool, ToolContext

EVIDENCE_KINDS = {"file", "diff", "test", "url_quote", "screenshot", "command"}


class EvidenceError(ValueError):
    pass


def _norm_cmd(s: str) -> str:
    s = re.sub(r"\s+", " ", str(s or "")).strip().lower()
    s = re.sub(r"^\$\s*", "", s)
    return re.sub(r"^(run_shell|run_python)[:\s]+", "", s)


def match_run(ref: str, runs: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The latest command this bot really ran that the evidence `ref` names (A4.a.08)."""
    want = _norm_cmd(ref)
    if len(want) < 3:
        return None
    for run in reversed(runs):
        if run.get("tool"):
            # a tool call (A17 re-audit) only backs evidence that names that tool, so a vague "pytest" can't match a
            # read_file of pytest.ini; MCP tools also match by their own name ("render_image" for mcp__blender__render_image)
            names = {_norm_cmd(run["tool"]), _norm_cmd(run["tool"].split("__")[-1])}
            if any(want == n or want.startswith(n + " ") for n in names if n):
                return run
            continue
        have = _norm_cmd(run["command"])
        if want == have or want in have or (len(have) >= 6 and have in want):
            return run
    return None


def check_evidence(items: list[dict[str, Any]], workspace: Path | None,
                   runs: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """`runs`: the commands this bot really ran in this job. When given, command/test
    evidence must name one of them; the REAL exit code and output replace what the bot
    claimed (a bot can't cite a command it never ran, or misreport its result)."""
    if not items:
        raise EvidenceError("a claim needs at least one piece of evidence")
    out = []
    for i, e in enumerate(items):
        kind, ref = e.get("kind"), str(e.get("ref") or "").strip()
        detail = {k: v for k, v in e.items() if k not in ("kind", "ref")}
        if kind not in EVIDENCE_KINDS:
            raise EvidenceError(f"evidence #{i + 1}: kind must be one of {sorted(EVIDENCE_KINDS)}")
        if not ref:
            raise EvidenceError(f"evidence #{i + 1}: 'ref' is required (path, URL, command or artifact id)")
        if kind in ("file", "screenshot", "diff") and workspace is not None:
            p = Path(ref) if Path(ref).is_absolute() else workspace / ref
            if not p.exists():
                raise EvidenceError(f"evidence #{i + 1}: file {ref!r} does not exist")
            detail.setdefault("bytes", p.stat().st_size)
        if kind in ("command", "test") and runs is not None:
            run = match_run(ref, runs)
            if run is None:
                ran = "; ".join(r["command"] for r in runs[-5:]) or "none"
                raise EvidenceError(f"evidence #{i + 1}: you did not run {ref!r} in this job (commands you ran: {ran}). "
                                    "Run it with your tools first, then cite it.")
            claimed = detail.get("exit_code")
            if claimed is not None and str(claimed) != str(run["exit_code"]):
                raise EvidenceError(f"evidence #{i + 1}: you claimed exit code {claimed}, but {run['command']!r} really "
                                    f"{'timed out' if run['timed_out'] else f'exited {run['exit_code']}'}")
            quote = str(detail.get("quote") or "").strip()
            if quote and re.sub(r"\s+", " ", quote) not in re.sub(r"\s+", " ", run["output"]):
                raise EvidenceError(f"evidence #{i + 1}: the quoted output is not in what {run['command']!r} really printed")
            detail.update(exit_code=run["exit_code"], verified=True, ran=run["command"],
                          output_tail=run["output"][-300:], **({"timed_out": True} if run["timed_out"] else {}))
        if kind in ("command", "test") and "exit_code" not in detail:
            raise EvidenceError(f"evidence #{i + 1}: a {kind} needs its 'exit_code'")
        if kind == "url_quote" and not str(detail.get("quote") or "").strip():
            raise EvidenceError(f"evidence #{i + 1}: a url_quote needs the 'quote' it relies on")
        out.append({"kind": kind, "ref": ref, "detail": detail})
    return out


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", htmllib.unescape(str(text or ""))).strip().casefold()


async def fetch_page(url: str) -> str:
    """The text a url_quote cites, fetched the way web_fetch does (public addresses; loopback only for a
    dev server on this PC). Raises on an error page or a refused address."""
    from urllib.parse import urlparse

    from omnibots.runtime.web_tools import _client, html_to_text, safe_get
    host = (urlparse(url).hostname or "").lower()
    async with _client() as c:
        res = await safe_get(c, url, allow_loopback=host in ("localhost", "127.0.0.1", "::1"))
    if res.status_code >= 400:
        raise ValueError(f"HTTP {res.status_code}")
    return html_to_text(res.text) if "html" in res.headers.get("content-type", "") else res.text


class Ledger:
    def __init__(self, db, bus: MessageBus, boss_id: str = "omi", fetch: Callable[[str], Awaitable[str]] | None = None):
        self.db, self.bus, self.boss_id = db, bus, boss_id
        self.fetch = fetch or fetch_page                  # A15.c.01: a url_quote is checked against the real page

    async def verify_pages(self, items: list[dict[str, Any]]) -> None:
        """A15.c.01: open every cited page; the quote must really be on it."""
        for i, e in enumerate(items):
            if e["kind"] != "url_quote":
                continue
            try:
                text = await asyncio.wait_for(self.fetch(e["ref"]), 30)
            except Exception as exc:
                raise EvidenceError(f"evidence #{i + 1}: couldn't open {e['ref']} to check the quote ({exc})")
            if _norm(e["detail"]["quote"]) not in _norm(text):
                seen = re.sub(r"\s+", " ", text).strip()[:160]
                raise EvidenceError(f"evidence #{i + 1}: the quote isn't on {e['ref']} (the page starts: {seen!r})")
            e["detail"].update(verified=True, checked_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))

    async def submit(self, *, bot_id: str, text: str, evidence: list[dict[str, Any]], job_id: str | None = None,
                     project_id: str | None = None, workspace: Path | None = None,
                     runs: list[dict[str, Any]] | None = None) -> int:
        items = check_evidence(evidence, workspace, runs)
        await self.verify_pages(items)
        cid = await self.db.write("INSERT INTO claims (job_id, project_id, bot_id, text) VALUES (?,?,?,?)", (job_id, project_id, bot_id, text))
        await self.db.write_many("INSERT INTO evidence (claim_id, kind, ref, detail_json) VALUES (?,?,?,?)",
                                 [(cid, e["kind"], e["ref"], json.dumps(e["detail"])) for e in items])
        await self.bus.publish(topic_job(job_id) if job_id else topic_bot(bot_id), "CLAIM_SUBMITTED",
                               {"claim_id": cid, "text": text, "evidence": items},
                               sender_type="bot", sender_id=bot_id, recipient_id=self.boss_id, job_id=job_id, project_id=project_id)
        return cid

    async def decide(self, claim_id: int, accepted: bool, reason: str, decided_by: str) -> None:
        row = await self.db.read_one("SELECT * FROM claims WHERE id=?", (claim_id,))
        if not row:
            raise KeyError(f"no claim {claim_id}")
        if row["status"] != "submitted":
            raise ValueError(f"claim {claim_id} is already {row['status']}")
        await self.db.write("UPDATE claims SET status=?, decided_by=?, reason=?, decided_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?",
                            ("accepted" if accepted else "rejected", decided_by, reason, claim_id))
        payload = {"claim_id": claim_id, "reason": reason, "text": row["text"]}
        await self.bus.publish(topic_job(row["job_id"]) if row["job_id"] else topic_bot(row["bot_id"]),
                               "CLAIM_ACCEPTED" if accepted else "CLAIM_REJECTED", payload,
                               sender_type="bot", sender_id=decided_by, recipient_id=row["bot_id"],
                               job_id=row["job_id"], project_id=row["project_id"])

    async def claims(self, *, job_id: str | None = None, project_id: str | None = None, bot_id: str | None = None) -> list[dict[str, Any]]:
        where, args = [], []
        for col, val in (("job_id", job_id), ("project_id", project_id), ("bot_id", bot_id)):
            if val:
                where.append(f"{col}=?")
                args.append(val)
        rows = await self.db.read("SELECT * FROM claims" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY id", args)
        out = []
        for r in rows:
            ev = await self.db.read("SELECT kind, ref, detail_json FROM evidence WHERE claim_id=? ORDER BY id", (r["id"],))
            out.append({**dict(r), "evidence": [{"kind": e["kind"], "ref": e["ref"], "detail": json.loads(e["detail_json"] or "{}")} for e in ev]})
        return out


def claim_tool(ledger: Ledger, *, project_id: str | None = None) -> Tool:
    async def submit_claim(args: dict[str, Any], ctx: ToolContext) -> str:
        try:
            cid = await ledger.submit(bot_id=ctx.bot_id, text=str(args.get("text", "")), evidence=list(args.get("evidence") or []),
                                      job_id=ctx.job_id, project_id=project_id, workspace=ctx.workspace, runs=ctx.runs)
        except EvidenceError as exc:
            return f"ERROR: claim not recorded: {exc}"
        return f"claim #{cid} submitted to Omi for checking"

    return Tool(
        "submit_claim",
        "Report a result WITH proof. `text`: what you claim is done. `evidence`: list of {kind, ref, ...}. "
        "kind is one of file|diff|test|url_quote|screenshot|command. ref is a workspace path, URL or the command. "
        "command/test must be a command YOU ran in this job with run_shell/run_python (its real exit code and output are "
        "attached automatically; you can add a `quote` from its output); a test is run again when Omi accepts. url_quote "
        "needs quote, and the page is opened: the quote must really be on it. Add live=true to the url_quote of a deployed "
        "result (the address where it's live). Omi accepts or rejects it.",
        {"type": "object", "properties": {
            "text": {"type": "string"},
            "evidence": {"type": "array", "items": {"type": "object", "properties": {
                "kind": {"type": "string"}, "ref": {"type": "string"}, "exit_code": {"type": "integer"}, "quote": {"type": "string"},
                "live": {"type": "boolean"}},
                "required": ["kind", "ref"]}}},
         "required": ["text", "evidence"]},
        "R0", submit_claim, path_arg=None, summary=lambda a: f"submit_claim: {str(a.get('text', ''))[:60]}")
