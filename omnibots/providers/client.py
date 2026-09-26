"""OpenAI-compatible streaming chat client: a port of Omni's src/core/provider.mjs.

Same request shape as Omni (system-message hoisting, tool-message flattening
on the text path, reasoning effort param, stream_options.include_usage,
temperature 0.2), so a provider behaves the same under Omni and OmniBots.

Additions over Omni, all needed by OmniBots:
  - `reasoning_content` / `reasoning` stream deltas are captured (Thinking panel);
  - <think> blocks inside content are split live (ThinkSplitter);
  - usage is taken from whichever chunk carries it;
  - HTTP errors keep status + rate-limit headers (Retry-After, x-ratelimit-*)
    so the quota layer can compute when a provider is usable again.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

import httpx

from omnibots.omni.config import OmniConfig, ProviderInfo
from omnibots.providers.toolcalls import ThinkSplitter, text_tool_instructions

TokenCallback = Callable[[str], Awaitable[None] | None]

# Omni's classifiers (src/core/agent.mjs).
RETRYABLE_RE = re.compile(r"ResourceExhausted|workers are busy|Service Unavailable|too many requests|rate.?limit", re.I)
RATE_LIMITED_RE = re.compile(r"ResourceExhausted|too many requests|rate.?limit", re.I)

DEFAULT_TIMEOUT = httpx.Timeout(connect=15.0, read=180.0, write=30.0, pool=30.0)


@dataclass
class ModelSpec:
    key: str
    id: str
    provider_name: str
    provider: ProviderInfo
    max_tokens: int = 8192
    context_window: int | None = None
    reasoning: str = "medium"
    native_tools: bool = True
    vision: bool = False
    max_tool_iterations: int = 30

    @property
    def label(self) -> str:
        return f"{self.provider_name}:{self.id}"


def resolve_model(cfg: OmniConfig, key: str) -> ModelSpec:
    """Port of Omni's resolveModel (the fields OmniBots uses).

    `provider::model-id` names a model directly on one of Omni's providers,
    for models the provider serves but Omni's saved catalog doesn't list
    (OmniBots never writes Omni's settings, so it can't add them there).
    """
    if "::" in key and key not in cfg.models:
        pname, mid = key.split("::", 1)
        if pname not in cfg.providers:
            raise KeyError(f'Provider "{pname}" not configured')
        return resolve_model(_with_model(cfg, key, {"provider": pname, "id": mid, "maxTokens": 8192}), key)
    m = cfg.models.get(key)
    if not m:
        raise KeyError(f'Unknown model "{key}"')
    pname = m.get("provider")
    prov = cfg.providers.get(pname)
    if prov is None:
        raise KeyError(f'Provider "{pname}" not configured')
    raw = prov.raw
    return ModelSpec(
        key=key,
        id=str(m.get("id")),
        provider_name=pname,
        provider=prov,
        max_tokens=int(m.get("maxTokens") or 8192),
        context_window=m.get("contextWindow"),
        reasoning="off" if m.get("reasoning") is False else cfg.reasoning,
        native_tools=m.get("nativeTools") is not False and prov.native_tools,
        vision=m.get("vision") is True or (m.get("vision") is not False and raw.get("vision") is True),
        max_tool_iterations=int(m.get("maxToolIterations") or raw.get("maxToolIterations") or (200 if pname == "minimax.io" else 30)),
    )


def _with_model(cfg: OmniConfig, key: str, entry: dict[str, Any]) -> OmniConfig:
    import copy

    clone = copy.copy(cfg)
    clone.models = {**cfg.models, key: entry}
    return clone


# ── request body ──────────────────────────────────────────────────────────
def auth_headers(p: ProviderInfo) -> dict[str, str]:
    h = {"Content-Type": "application/json"}
    key = (p.api_key or "").strip()
    if key and key != "not-needed":
        h["Authorization"] = f"Bearer {key}"
    extra = p.raw.get("extraHeaders")
    if isinstance(extra, dict):
        h.update({str(k): str(v) for k, v in extra.items()})
    return h


def apply_reasoning(body: dict[str, Any], model: ModelSpec) -> None:
    tier = (model.reasoning or "").lower()
    if not tier or tier == "off":
        return
    effort = "high" if tier in ("extra", "xhigh") else tier
    param = model.provider.raw["reasoningParam"] if "reasoningParam" in model.provider.raw else "reasoning_effort"
    if not param or param == "none":
        return
    body[param] = effort


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(p if isinstance(p, str) else (p or {}).get("text", "") for p in content)
    return "" if content is None else str(content)


def hoist_system_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    parts, rest = [], []
    for m in messages:
        if m and m.get("role") == "system":
            t = _text_of(m.get("content"))
            if t:
                parts.append(t)
        else:
            rest.append(m)
    if len(parts) <= 1 and ((messages and messages[0].get("role") == "system") or not parts):
        return messages
    return [{"role": "system", "content": "\n\n".join(parts)}, *rest]


def _flatten_tool_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for m in messages:
        if m.get("role") == "tool":
            name = f" ({m['name']})" if m.get("name") else ""
            out.append({"role": "user", "content": f"Tool result{name}:\n{m.get('content') or ''}"})
        elif m.get("role") == "assistant" and m.get("tool_calls"):
            calls = "\n\n".join(
                f"Tool call requested: {(c.get('function') or {}).get('name', '')}\nArguments: {(c.get('function') or {}).get('arguments') or '{}'}"
                for c in m["tool_calls"])
            out.append({"role": "assistant", "content": "\n\n".join(x for x in (m.get("content"), calls) if x)})
        else:
            out.append(m)
    return out


def _sanitize_native(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{k: v for k, v in m.items() if k != "name"} if m.get("role") == "tool" and "name" in m else m for m in messages]


def build_chat_body(model: ModelSpec, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    wire = hoist_system_messages(messages)
    body: dict[str, Any] = {
        "model": model.id,
        "messages": _sanitize_native(wire) if model.native_tools else _flatten_tool_messages(wire),
        "max_tokens": model.max_tokens,
        "temperature": 0.2,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    apply_reasoning(body, model)
    if model.native_tools and tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"
    return body


def serialize_tool_call(call: dict[str, Any]) -> str:
    """Port of Omni's serializeToolCall (canonical text form for history)."""
    fn = call.get("function") or {}
    try:
        args = json.loads(fn.get("arguments") or "{}")
    except ValueError:
        args = {}
    params = "\n".join(
        f"<parameter={k}>{v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, separators=(',', ':'))}</parameter>"
        for k, v in (args.items() if isinstance(args, dict) else []))
    return f"<tool_call>\n<function={fn.get('name', '')}>\n{params}\n</function>\n</tool_call>"


def messages_with_text_tools(messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Port of Omni's messagesWithTextTools: history in the text protocol +
    protocol instructions appended to the system prompt."""
    norm = []
    for m in messages:
        if m.get("role") == "tool":
            name = f" ({m['name']})" if m.get("name") else ""
            norm.append({"role": "user", "content": f"Tool result{name}:\n{m.get('content') or ''}"})
        elif m.get("role") == "assistant" and m.get("tool_calls"):
            calls = "\n".join(serialize_tool_call(c) for c in m["tool_calls"])
            norm.append({"role": "assistant", "content": "\n".join(x for x in (m.get("content"), calls) if x)})
        else:
            norm.append(m)
    instructions = text_tool_instructions(tools)
    if not norm or norm[0].get("role") != "system":
        return [{"role": "system", "content": instructions}, *norm]
    return [{**norm[0], "content": _text_of(norm[0].get("content")) + "\n" + instructions}, *norm[1:]]


# ── errors ────────────────────────────────────────────────────────────────
def _duration_seconds(v: str | None) -> float | None:
    """'30', '1.5', '6m0s', '2h3m', '250ms' -> seconds."""
    if not v:
        return None
    v = v.strip()
    try:
        return float(v)
    except ValueError:
        pass
    total, found = 0.0, False
    for num, unit in re.findall(r"(\d+(?:\.\d+)?)(ms|h|m|s)", v):
        found = True
        total += float(num) * {"ms": 0.001, "s": 1, "m": 60, "h": 3600}[unit]
    return total if found else None


RATE_HEADERS = ("retry-after", "x-ratelimit-reset-requests", "x-ratelimit-reset-tokens", "ratelimit-reset",
                "x-ratelimit-reset", "x-ratelimit-remaining-requests", "x-ratelimit-remaining-tokens",
                "x-ratelimit-limit-requests", "x-ratelimit-limit-tokens")


def rate_headers(headers: httpx.Headers | dict[str, str]) -> dict[str, str]:
    return {k: headers[k] for k in RATE_HEADERS if k in headers}


class ProviderError(Exception):
    def __init__(self, provider: str, status: int | None, message: str, headers: dict[str, str] | None = None, *, fatal: bool = False):
        self.provider = provider
        self.fatal = fatal                   # config problem (bad URL): never retry
        self.status = status
        self.headers = headers or {}
        super().__init__(f"Provider {status} {provider}: {message}" if status else f"{provider}: {message}")
        self.detail = message

    @property
    def rate_limited(self) -> bool:
        return self.status == 429 if self.status is not None else bool(RATE_LIMITED_RE.search(self.detail))

    @property
    def retryable(self) -> bool:
        if self.status is not None:
            return self.status == 429 or 502 <= self.status <= 504
        return bool(RETRYABLE_RE.search(self.detail))

    @property
    def auth_failed(self) -> bool:
        return self.status in (401, 403)

    def retry_after_seconds(self) -> float | None:
        """When the provider says it will accept requests again."""
        h = self.headers
        for name in ("retry-after", "x-ratelimit-reset-requests", "x-ratelimit-reset-tokens", "ratelimit-reset", "x-ratelimit-reset"):
            secs = _duration_seconds(h.get(name))
            if secs is not None:
                # x-ratelimit-reset is sometimes an epoch timestamp.
                if secs > 10_000_000:
                    secs = max(0.0, secs - time.time())
                return secs
        return None


def extract_provider_message(text: str) -> str:
    try:
        data = json.loads(text)
        err = data.get("error")
        return str(data.get("detail") or (err.get("message") if isinstance(err, dict) else err) or data.get("message") or text)
    except (ValueError, AttributeError):
        return str(text or "")


# ── streaming ─────────────────────────────────────────────────────────────
@dataclass
class ChatResult:
    message: dict[str, Any]              # {"role":"assistant","content":raw,"tool_calls":[...]}
    answer: str                          # content with <think> removed
    thinking: str                        # <think> text + reasoning_content deltas
    finish_reason: str | None
    usage: dict[str, Any] | None
    latency_ms: int
    first_token_ms: int | None
    headers: dict[str, str] = field(default_factory=dict)


async def _emit(cb: TokenCallback | None, text: str) -> None:
    if cb and text:
        r = cb(text)
        if r is not None and hasattr(r, "__await__"):
            await r


def _base_url(p: ProviderInfo) -> str:
    if not re.match(r"^https?://", p.base_url or "", re.I):
        raise ProviderError(p.name, None, f'invalid baseUrl "{p.base_url}"; fix it in Omni', fatal=True)
    return p.base_url.rstrip("/")


async def chat_stream(
    model: ModelSpec,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    *,
    on_token: TokenCallback | None = None,
    on_think: TokenCallback | None = None,
    client: httpx.AsyncClient | None = None,
    timeout: httpx.Timeout = DEFAULT_TIMEOUT,
) -> ChatResult:
    url = _base_url(model.provider) + "/chat/completions"
    wire_messages = messages if model.native_tools or not tools else messages_with_text_tools(messages, tools)
    body = build_chat_body(model, wire_messages, tools)
    own = client is None
    client = client or httpx.AsyncClient(timeout=timeout)
    t0 = time.perf_counter()
    first: int | None = None
    try:
        async with client.stream("POST", url, headers=auth_headers(model.provider), json=body, timeout=timeout) as res:
            if res.status_code >= 400:
                text = (await res.aread()).decode("utf-8", "replace")
                raise ProviderError(model.provider_name, res.status_code,
                                    _format_error(res.status_code, extract_provider_message(text)[:500], model),
                                    rate_headers(res.headers))
            headers = rate_headers(res.headers)
            content, thinking = [], []
            answer = []
            splitter = ThinkSplitter()
            calls: dict[int, dict[str, Any]] = {}
            finish, usage = None, None
            async for line in res.aiter_lines():
                t = line.strip()
                if not t or t.startswith(":") or not t.startswith("data:"):
                    continue
                data = t[5:].strip()
                if data == "[DONE]":
                    continue
                try:
                    chunk = json.loads(data)
                except ValueError:
                    continue
                if isinstance(chunk.get("usage"), dict):
                    usage = chunk["usage"]
                if chunk.get("error"):
                    msg = chunk["error"].get("message") if isinstance(chunk["error"], dict) else str(chunk["error"])
                    raise ProviderError(model.provider_name, None, f"stream error: {msg}", headers)
                choices = chunk.get("choices") or []
                if not choices:
                    continue
                choice = choices[0]
                delta = choice.get("delta") or {}
                reasoning = delta.get("reasoning_content") or delta.get("reasoning")
                if isinstance(reasoning, str) and reasoning:
                    first = first if first is not None else int((time.perf_counter() - t0) * 1000)
                    thinking.append(reasoning)
                    await _emit(on_think, reasoning)
                piece = delta.get("content")
                if isinstance(piece, str) and piece:
                    first = first if first is not None else int((time.perf_counter() - t0) * 1000)
                    content.append(piece)
                    part = splitter.feed(piece)
                    thinking.append(part["think"])
                    answer.append(part["answer"])
                    await _emit(on_think, part["think"])
                    await _emit(on_token, part["answer"])
                for tc in delta.get("tool_calls") or []:
                    idx = tc.get("index", 0) or 0
                    entry = calls.setdefault(idx, {"id": tc.get("id") or "", "type": "function", "function": {"name": "", "arguments": ""}})
                    if tc.get("id"):
                        entry["id"] = tc["id"]
                    fn = tc.get("function") or {}
                    if fn.get("name") and not entry["function"]["name"]:
                        entry["function"]["name"] = fn["name"]
                    if fn.get("arguments"):
                        entry["function"]["arguments"] += fn["arguments"]
                if choice.get("finish_reason"):
                    finish = choice["finish_reason"]
            tail = splitter.flush()
            thinking.append(tail["think"])
            answer.append(tail["answer"])
            await _emit(on_think, tail["think"])
            await _emit(on_token, tail["answer"])
    except httpx.TimeoutException as exc:
        raise ProviderError(model.provider_name, None, f"timed out ({type(exc).__name__})") from exc
    except httpx.TransportError as exc:
        raise ProviderError(model.provider_name, None, f"network error: {exc}") from exc
    finally:
        if own:
            await client.aclose()
    message: dict[str, Any] = {"role": "assistant", "content": "".join(content)}
    if calls:
        message["tool_calls"] = [calls[i] for i in sorted(calls)]
    return ChatResult(
        message=message, answer="".join(answer), thinking="".join(thinking), finish_reason=finish, usage=usage,
        latency_ms=int((time.perf_counter() - t0) * 1000), first_token_ms=first, headers=headers,
    )


def _format_error(status: int, msg: str, model: ModelSpec) -> str:
    if status in (401, 403):
        acct = model.provider.active_account
        where = f"{model.provider_name}:{acct}" if acct else model.provider_name
        has_key = bool((model.provider.api_key or "").strip())
        return (f"{msg}\n" + (f"The API key for {where} was rejected (wrong, expired, or for a different endpoint: {model.provider.base_url})."
                             if has_key else f"No API key is configured for {where}.")
                + f"\nFix it in Omni: /apikey {acct or model.provider_name} <your-key>")
    if re.search(r"DEGRADED function cannot be invoked", msg, re.I):
        return f"{msg}\nNVIDIA reports model \"{model.id}\" is degraded (a provider-side problem)."
    if re.search(r"end of life|no longer available", msg, re.I):
        return f"{msg}\nModel \"{model.id}\" is no longer available from this provider."
    return msg


async def list_models(p: ProviderInfo, client: httpx.AsyncClient | None = None) -> list[str]:
    own = client is None
    client = client or httpx.AsyncClient(timeout=DEFAULT_TIMEOUT)
    try:
        res = await client.get(_base_url(p) + "/models", headers=auth_headers(p))
        if res.status_code >= 400:
            raise ProviderError(p.name, res.status_code, res.text[:300], rate_headers(res.headers))
        data = res.json()
        raw = data.get("data") if isinstance(data.get("data"), list) else data.get("models") or []
        return sorted(m if isinstance(m, str) else (m.get("id") or m.get("name") or m.get("model")) for m in raw if m)
    finally:
        if own:
            await client.aclose()
