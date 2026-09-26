"""Live limits probe (PLAN.md A2.b.05): real, small requests to each provider.

  python -m omnibots.providers.probe [--parallel 4] [--only minimax.io,nvidia]

For each lineup provider: (1) a tool-call test: ask the model to call
`add(a=2, b=3)` and check the parsed call (native or text protocol), then
(2) N parallel tiny requests, to see whether the key handles that many at
once. Records latency, time to first token, usage, 429s and the rate-limit
headers. Saves a JSON report to ~/.omnibots/logs/ and each provider's result
into providers.verified_limits_json. Replies are capped to keep cost tiny.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import sys
import time
from datetime import datetime, timezone
from typing import Any

import httpx

from omnibots.db import Database
from omnibots.lineup import LINEUP_MODELS, TARGET_PROVIDERS
from omnibots.omni import load_omni_config, locate_omni
from omnibots.paths import get_paths
from omnibots.providers.client import ProviderError, chat_stream, resolve_model
from omnibots.providers.toolcalls import build_param_registry, parse_text_tool_calls

ADD_TOOL = [{"type": "function", "function": {
    "name": "add", "description": "Add two integers and return the sum.",
    "parameters": {"type": "object", "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}}, "required": ["a", "b"]}}}]
TOOL_MESSAGES = [
    {"role": "system", "content": "You are a test harness assistant. When asked to use a tool, call it immediately with no commentary."},
    {"role": "user", "content": "Use the add tool to add 2 and 3."},
]
PING = [{"role": "user", "content": "Reply with the single word OK."}]


async def _one(model, messages, tools, client) -> dict[str, Any]:
    t0 = time.perf_counter()
    try:
        res = await chat_stream(model, messages, tools, client=client)
        return {"ok": True, "status": 200, "latency_ms": res.latency_ms, "first_token_ms": res.first_token_ms,
                "usage": res.usage, "headers": res.headers, "res": res}
    except ProviderError as e:
        return {"ok": False, "status": e.status, "latency_ms": int((time.perf_counter() - t0) * 1000),
                "error": e.detail.split("\n")[0][:240], "headers": e.headers, "rate_limited": e.rate_limited}


def _tool_call_check(model, r) -> dict[str, Any]:
    if not r["ok"]:
        return {"tool_call_ok": False}
    res = r.pop("res")
    calls = res.message.get("tool_calls") or []
    mode = "native"
    if not calls and not model.native_tools:
        calls = parse_text_tool_calls(res.message.get("content") or "", build_param_registry(ADD_TOOL))
        mode = "text"
    elif not calls:
        # Some native providers still answer in the text protocol; accept it but note it.
        calls = parse_text_tool_calls(res.message.get("content") or "", build_param_registry(ADD_TOOL))
        mode = "text-fallback" if calls else "none"
    args = {}
    if calls:
        try:
            args = json.loads(calls[0]["function"]["arguments"] or "{}")
        except ValueError:
            args = {}
    ok = bool(calls) and calls[0]["function"]["name"] == "add" and {int(args.get("a", -1)), int(args.get("b", -1))} == {2, 3}
    return {"tool_call_ok": ok, "tool_mode": mode, "tool_args": args,
            "answer_preview": (res.answer or "")[:120], "thinking_chars": len(res.thinking)}


async def probe_provider(cfg, name: str, parallel: int, client: httpx.AsyncClient) -> dict[str, Any]:
    key = LINEUP_MODELS.get(name)
    prov = cfg.providers.get(name)
    if not key or not prov:
        return {"provider": name, "skipped": "not configured"}
    if not prov.has_key:
        return {"provider": name, "skipped": "no key in Omni"}
    base = resolve_model(cfg, key)
    tool_model = dataclasses.replace(base, max_tokens=min(base.max_tokens, 1024))
    ping_model = dataclasses.replace(base, max_tokens=min(base.max_tokens, 256))

    single = await _one(tool_model, TOOL_MESSAGES, ADD_TOOL, client)
    single.update(_tool_call_check(tool_model, single))
    burst = await asyncio.gather(*(_one(ping_model, PING, None, client) for _ in range(parallel)))
    for b in burst:
        b.pop("res", None)
    return {
        "provider": name, "model": key, "model_id": base.id, "native_tools": base.native_tools,
        "single": single,
        "parallel": {"n": parallel, "ok": sum(b["ok"] for b in burst), "rate_limited": sum(bool(b.get("rate_limited")) for b in burst),
                     "statuses": [b["status"] for b in burst], "latency_ms": [b["latency_ms"] for b in burst],
                     "errors": sorted({b["error"] for b in burst if not b["ok"]})},
        "rate_headers": single.get("headers") or next((b["headers"] for b in burst if b.get("headers")), {}),
    }


async def run_probe(only: list[str] | None, parallel: int) -> dict[str, Any]:
    cfg = load_omni_config(locate_omni())
    names = only or TARGET_PROVIDERS
    async with httpx.AsyncClient(timeout=httpx.Timeout(connect=15, read=180, write=30, pool=60),
                                 limits=httpx.Limits(max_connections=50)) as client:
        results = await asyncio.gather(*(probe_provider(cfg, n, parallel, client) for n in names))
    report = {"at": datetime.now(timezone.utc).isoformat(), "parallel": parallel, "results": results}

    paths = get_paths()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out = paths.logs / f"probe-{stamp}.json"
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    report["file"] = str(out)
    db = Database(paths.db_file)
    await db.open()
    for r in results:
        if "skipped" in r:
            continue
        await db.write(
            "INSERT INTO providers (name, verified_limits_json, last_tested_at) VALUES (?,?,?) "
            "ON CONFLICT(name) DO UPDATE SET verified_limits_json=excluded.verified_limits_json, last_tested_at=excluded.last_tested_at",
            (r["provider"], json.dumps({k: r[k] for k in ("model", "parallel", "rate_headers")} | {"tool_call_ok": r["single"].get("tool_call_ok")}),
             report["at"]))
    await db.audit("system", None, "provider_probe", json.dumps({"file": str(out)}))
    await db.close()
    return report


def summarize(report: dict[str, Any]) -> str:
    lines = [f"{'provider':<11} {'model':<34} {'tool call':<16} {'1st tok':>8} {'total':>7}   parallel x{report['parallel']}"]
    for r in report["results"]:
        if "skipped" in r:
            lines.append(f"{r['provider']:<11} skipped: {r['skipped']}")
            continue
        s, p = r["single"], r["parallel"]
        tool = (f"OK ({s.get('tool_mode')})" if s.get("tool_call_ok") else ("FAIL" if s["ok"] else f"ERR {s['status']}"))
        lines.append(f"{r['provider']:<11} {r['model']:<34} {tool:<16} {str(s.get('first_token_ms') or '-'):>8} {s['latency_ms']:>7}   "
                     f"{p['ok']}/{p['n']} ok, {p['rate_limited']} x 429  {p['statuses']}")
        if not s["ok"]:
            lines.append(f"{'':<11} error: {s.get('error')}")
        for e in p["errors"]:
            lines.append(f"{'':<11} parallel error: {e}")
    lines.append(f"report: {report['file']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m omnibots.providers.probe")
    ap.add_argument("--parallel", type=int, default=4)
    ap.add_argument("--only", default="", help="comma-separated provider names")
    a = ap.parse_args(argv)
    report = asyncio.run(run_probe([x for x in a.only.split(",") if x] or None, a.parallel))
    print(summarize(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
