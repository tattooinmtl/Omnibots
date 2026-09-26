# Mini Audit — OmniBots Phase Plan

**Auditor:** coder-ai-senior-developer
**Date:** 2026-09-25
**Scope:** `Phased_Plan_Master.md` + `omni_patch/` + `omni-plugin.json`
**Verdict:** **Plan is directionally sound, but contains real correctness bugs, hidden dependencies, sequencing issues, and underspecified pieces that will bite during implementation. Several assumptions are wrong or inconsistent.**

Status: every phase is `⏳ Not Started` except `0.c.04` (`⚠️ Blocked`) and the auth phase (`⛔ Dropped`). Most findings below are preventable problems to fix **before** coding starts, not after.

---

## TL;DR — Top 8 things that are wrong

1. **Phase ordering is broken.** Quota tracking (12.b) is needed *before* bot factory (10), not after. Phase 16 introduces a new persistence requirement (`bot_events`) that doesn't exist in Phase 1's schema.
2. **MiniMax quota math contradicts the test plan.** 5 concurrent MiniMax agents vs. "200 tool calls/hr" per account is not survivable. Either the quota is per-account and you can't run the test, or it's per-session and the wording is wrong.
3. **Phase 13.b.04 has a self-destructive rule.** "When ALL small providers are exhausted → stop until all reset" ignores MiniMax M3, the most expensive fallback. This will freeze the system whenever cheap APIs hit 429.
4. **The HTTP API is gratuitous for a single-process desktop app.** All traffic is loopback. The plan adds FastAPI without justifying it over `qasync` + in-process calls or QLocalSocket.
5. **Polling at 1–2 s for the message board is the wrong MVP.** `asyncio.Queue` + a tiny pub/sub bus inside the same process is strictly better and removes a whole class of bugs. Polling only makes sense once bots run in separate processes — which is not what 13.c tests.
6. **`providers` table and `provider_quotas` table overlap and the boundary isn't drawn.** Both hold per-provider state. Either merge or define the relationship.
7. **"Bot logs in as itself" (10.a.04) is dead text from the auth era.** With auth dropped, "credentials" reduces to a UUID; the wording still reads as if a bot needs to authenticate.
8. **No testing strategy anywhere except Phase 13.c.** Phases 0–12 have no acceptance-test sub-phase. AGENTS.md says "test in real environment" but no phase has a test step.

---

## 1. Phase ordering — three real bugs

### 1.1 Quota (12.b) must come before Bot Factory (10)

Phase 10 lets the orchestrator spawn bots. Phase 12.b adds quota tracking, 429 detection, and recovery. You cannot safely run multi-bot tests (13.c) without quota. Building 10 first and retrofitting quota is how you end up writing quota logic twice and missing every `httpx` call site on the second pass.

**Fix:** Renumber. Move 12.b → between 0.c and 1, or make 10.c ("Spawn Governor") explicitly depend on 12.b.

### 1.2 Phase 16 introduces a hidden Phase 1 dependency

16.a.03 says: *"Live feed plumbing: the worker runtime streams console, terminal, thinking and state events … events are also persisted so you can reopen a window and see recent history."*

There is no `bot_events` / `console_log` / `live_stream` table in Phase 1. Phase 16 *requires* a new schema that nobody planned for.

**Fix:** Add `1.a.12 ⏳ bot_events table (bot_id, kind, payload, ts, idx)` and link it in cross-refs. Or merge with `messages` if you're willing to overload the topic space — I'd recommend a separate table because board events ≠ live console/thinking stream.

### 1.3 Phase 13 retroactively edits Phase 7

7.a.03 is marked done in Phase 7 (build order), but the `USER_STEER` message type is added in 16.a.04. So Phase 7's deliverable will be incomplete unless the implementer reads 16 first.

**Fix:** Move the `USER_STEER` row to 7.a.03 from the start, with a forward reference to 16.a.04 for the *consumer* (the bot window / bot loop).

### 1.4 Phase 5.b depends on Phase 16

5.b is "Bots List with start/stop/.../double-click opens bot's window". The window doesn't exist until Phase 16. Same kind of forward dep — fine if you ship 5.b with a stub, just call it out.

---

## 2. Quota / test plan inconsistency

12.b.01 says MiniMax M3 has **200 tool calls/hr and 20k+ TPM** (per account) — flagged for live verification.

13.c.01 wants **4 concurrent MiniMax agents + 1 multi-provider agent** for end-to-end testing.

If "200 tool calls/hr" is per-account, **a single end-to-end test run of 4 agents × any meaningful task will exhaust the account inside an hour.** If it's per-agent, the wording is wrong.

**Fix before coding:**
- Decide: per-account or per-session? Per-session is the only realistic answer for "4 concurrent agents".
- Update the 12.b.01 table with the verified number once you've tested.
- Pick a realistic test goal for 13.c that fits the actual budget — e.g., "each agent performs 10 short tasks in parallel and we measure total elapsed time and per-agent token cost".

---

## 3. Phase 13.b.04 will freeze the system on the first 429 storm

> "When ALL small providers are exhausted → stop until all reset"

This rule means: as soon as NVIDIA, Agnes, xkiro, atria, openrouter, and groq all 429 simultaneously (very plausible during a burst), **the entire system halts** even though MiniMax M3 is right there and presumably still has budget.

**Fix:** Replace the rule with:

> "When a bot's primary provider 429s, route the next request to the next provider in its failover chain. If the chain is exhausted, fall back to MiniMax M3 (the system-capable provider) and only escalate to user / pause when MiniMax is also at quota."

13.b.04 should be: *"global pause if every provider in Omni's settings — including MiniMax M3 — is exhausted."*

---

## 4. The HTTP API is unjustified

You have one user, one PC, one PySide6 process. FastAPI gives you:
- Loopback HTTP
- A second process to crash
- Two serialization hops (UI → JSON → in-memory model → JSON → bot runtime)
- A second config surface (uvicorn port, CORS, etc.)

What you actually need:
- In-process calls between the desktop UI and the orchestrator/bot runtime (signals/slots or `asyncio.Queue`)
- IPC only if a separate process is needed (e.g., headless bot workers)

**Recommendation:** Drop FastAPI from the MVP. Use:
- `qasync` to run asyncio under the Qt event loop, **or**
- Plain Qt signals for UI updates + a single asyncio loop running bots in the same process.

If you keep FastAPI for "later we want a remote CLI", at minimum move it to Phase 15 as a future enhancement and don't make Phases 2/5/9 depend on it.

If you *must* keep FastAPI:
- Bind to a Unix domain socket on Windows (`\\.\pipe\omnibots`) — avoids the port, avoids firewall prompts, and is faster than TCP loopback.
- Add health checks the UI can poll so a FastAPI crash surfaces in the tray.

---

## 5. Polling-based message board is the wrong MVP

7.b.01 says bots poll every 1–2 s. This is fine only if bots are in separate processes and you genuinely can't share memory. Since the plan implies a single desktop process (`python -m omnibots` → PySide6 + everything else), use:

- An in-process pub/sub bus (one `asyncio.Queue` per subscriber + a broadcast helper).
- Bots subscribe to `#tasks`, `#bot/<id>`, `#orchestrator`.
- The UI subscribes to the same bus and renders directly from the event stream.
- SQL is still the durable backing store — bus events are written-through.

**Why this matters:**
- 50 bots polling at 1 Hz = 50 SQL reads/sec just for board ticks, plus each bot's writes. SQLite can handle it but it's wasted load.
- 2-second UI lag on a desktop control panel feels like a bug, not a feature.
- The WebSocket upgrade (7.b.02) becomes a *cosmetic* layer, not an architecture change.

If you're worried about horizontal scaling later, keep SQL as the durable log and run an in-memory fan-out for live subscribers. You don't lose the durability.

---

## 6. Schema overlap — `providers` vs `provider_quotas`

1.a.09 says `providers` holds *"enabled-for-bots flag, quota numbers, 429 state"*.
1.b.01 adds `provider_quotas` with *"credit limits, time windows, current usage %"*.

Two tables for the same concept, owned by different phases. Pick one:

**Option A (recommended):** merge. `providers` table = (id, name, display_name, enabled_for_bots, current_usage_pct, last_429_at, reset_at). Drop `provider_quotas`. Keep `provider_usage_events` separate (it's per-event history, not state).

**Option B:** split cleanly. `providers` = OmniBots-side state (enabled flag, last tested at, default model override). `provider_quotas` = budget/429/recovery state. Add an explicit cross-phase note that these are not redundant.

Right now, the same concept lives in two places and no agent will know which to update when 12.b.04 detects a 429.

---

## 7. Dead text from the auth era

`10.a.04`: *"Issue bot credentials (bot logs in as itself, posts to message board)"*.

With auth dropped, "credentials" is reduced to *"an internal per-bot identity token so the board knows which bot posted. Not a password."*

This still reads as if a bot authenticates somewhere. Rewrite as:

> **10.a.04** Assign each bot a stable opaque ID (`bot_<uuid>`) at creation. The message board stamps every message with `sender_id = bot_id`. No authentication — the loopback API and the in-process bus both trust the ID.

If you later need to prevent a misbehaving bot from impersonating another, that's a separate concern (sandboxing / process isolation), not "credentials".

---

## 8. No testing strategy outside Phase 13.c

AGENTS.md says *"test in real environment, not mocked unit tests"*. The phased plan has no acceptance-test sub-phases for Phases 0–12. There's no agreed definition of "Phase X passes".

**Fix:** For every phase, add a `X.z.99 ⏳ Acceptance verification` sub-phase with explicit pass criteria, e.g.:

- `0.c.01`: Reading `~/.omni/agent/settings.json` + `.env` returns exactly the 7 expected providers with non-empty keys for the 6 that have keys, Agnes key coming from `.env`.
- `0.c.03`: Launching the app twice focuses the existing window.
- `2.a.01`: `/health` returns 200 on `127.0.0.1` only, no listener on external interfaces (`netstat -an` test).
- `7.a.02`: Posting to `#bot/bot_xxx` delivers to only that subscriber.

Phase 13.c tests the whole system end-to-end but won't catch which sub-phase introduced a regression.

---

## 9. Underspecified — list of items to write down before coding

These are not bugs, they're decisions that aren't made yet but will block implementation:

### 9.1 Concurrency model
PySide6 owns the main thread. Where do asyncio bots run?
- Same process under `qasync`? (Recommended; kills the FastAPI need.)
- Separate worker process pool? (Adds IPC, justifies FastAPI.)

### 9.2 Bot runtime — process vs coroutine
A "worker bot" is described as a loop. Is it:
- A `asyncio.Task` per bot in the same loop?
- A `subprocess.Popen` per bot?
- A thread?

Pick one. The plan currently implies the first but never says so.

### 9.3 Tool sandbox for `python_function`
`subprocess` with `resource` limits? RestrictedPython? Docker? Each has very different perf and capability. Don't leave this to "Phase 11.b.03".

### 9.4 Memory summarization
`memory_summary_enabled` parameter exists; nothing implements it. Is summarization manual (user clicks "summarize") or automatic (triggered at N entries)? If automatic, who runs the LLM and which provider?

### 9.5 `memory_limit` units
MB? Lines? Tokens? Phase 25 leaves this undefined.

### 9.6 Hard Reset scope
Phase 14.a.03 says "Hard Reset — clears jobs/messages/state, optionally wipes memory". What counts as "state"? `bot_provider_state`? `provider_usage_events`? `audit_logs`? Pick.

### 9.7 SQLite migration strategy
Alembic? Plain SQL files? `pragma user_version`? Phase 16 will need a schema change; nothing plans for it.

### 9.8 `max_spawn_per_cycle` semantics
Phase 10.c.02. What is a "cycle"? Orchestrator main loop iteration? If so, the limit resets every iteration, so it's effectively `∞`. Spell it out.

### 9.9 Provider `accounts` handling
0.c.01 says "use the `activeAccount` key only (one account per provider — see scope change)". OK. But what happens at boot if `activeAccount` points to an account with an empty key? Does OmniBots fall back to other accounts? No — and that's correct given the scope — but the error path needs an explicit message in the UI.

### 9.10 `top_p` parameter (§25)
OpenAI has deprecated `top_p` on most modern models. Phase 25 still has it. Either drop or document as "legacy, sent only to providers that accept it".

### 9.11 `provider_usage_events` token counts
Many providers (free tiers in particular) don't return token usage. How is `total_tokens` populated? Optional column? Estimated?

### 9.12 Bot face palette
16.b.01: "each bot gets its own look (color, antenna, eye shape), derived from its bot ID so it stays the same forever". A naive hash of a UUID → RGB gives ugly colors. Use a fixed palette indexed by `hash(bot_id) % N`, not full RGB.

### 9.13 "Sub-agents" wording
13.a.04: *"MiniMax M3 = main agent + 3 worker sub-agents (vision, text, TTS arc1.0, H3 video outputs)"*. These are modalities, not agents. Calling them sub-agents is confusing. Either rename or be explicit that a single MiniMax M3 session is given three specialized prompts/skills.

### 9.14 Single-instance guard implementation
0.c.03 says "a second launch focuses the existing window". On Windows, `QLockFile` + a `QLocalSocket` for "show window" messages, or a named pipe, or a QtSingleApplication pattern. Pick one and write it down.

### 9.15 Event persistence schema
Same as §1.2 — Phase 16.a.03 needs `bot_events`. Not in Phase 1.

---

## 10. Smaller issues

| Where | Issue |
|------|------|
| §9 sample | Hardcoded date `2026-09-25T10:00:00Z` in the memory.md example. Templates should generate. |
| 4.d.09 | "Exit (stops bots cleanly, then quits)" — define "cleanly". Cancel running jobs? Flush pending messages to SQL? Write a "session end" audit log entry? |
| 5.b cross-ref | "double-click opens that bot's ID-card window (16.a.05)" — implementation order means 5.b ships a stub handler. Add a `// TODO 16.a.05` marker policy. |
| 6.a.02 | "Bot folder + memory.md template" — where is "the folder"? Under `workspace/bots/<bot_id>/`? Or under `~/.omnibots/bots/<bot_id>/`? Not specified. |
| 7.a.01 | `GET /board/messages` — pagination? Filtering by topic must be supported server-side, otherwise large boards will kill the UI. |
| 9.a.01 | Orchestrator "continuous loop" — needs a way to stop (graceful shutdown). Phase 2.c.01 has `/system/stop`, but no link from 9.a.01. |
| 10.b | "Worker runtime loop" — needs a back-off strategy when the board is quiet (don't tight-loop, don't burn CPU on 100 ms idle). |
| 13.a.02 | "Reads user config to determine orchestration style" — which config? `parameters` table? A separate orchestrator profile? |
| 16.b.04 | "Animate only visible faces, about 10 fps" — what counts as visible? Tabs? Minimized windows? Tray mini-preview? |
| AGENTS.md | "Once OmniBots is built … this file will be REPLACED with a different one" — `AGENTS.md` replacement needs a sub-phase. Not in any phase. |

---

## 11. What's actually good

- Phase 0.c (config bridge) is added up front instead of as a retrofit. Smart.
- Soft vs Hard Reset with explicit confirmation. Right call.
- Per-bot memory files (`memory.md`) as readable text, not just SQL blobs. Correct.
- Multi-provider failover is designed in, not bolted on.
- Test lineup (13.c) is concrete and exercises real accounts.
- AGENTS.md is explicit about hard rules (no silent writes, no deletes without confirmation, real-environment testing).
- The scope change on 2026-09-24 (drop auth, single user, providers owned by Omni) is the right call and is consistently applied across phases.

---

## 12. Recommended actions, in priority order

Before any phase goes `🟡 In Progress`:

1. **Decide concurrency model** (9.1) and write it into `AGENTS.md` + Phase 0.a.01 notes. Without this, every phase guess-wrong.
2. **Fix Phase ordering**: add `1.a.12 bot_events`; move 12.b to depend on 0.c but precede 10; merge or cleanly split `providers` vs `provider_quotas`.
3. **Rewrite 13.b.04** to fall back to MiniMax M3 before pausing.
4. **Verify MiniMax quota numbers live** before locking 12.b.01 numbers — otherwise 13.c can't pass.
5. **Add `X.z.99` acceptance test** sub-phase to every phase.
6. **Strip dead auth-era text** from 10.a.04.
7. **Decide on FastAPI** — keep and justify, or drop and use `qasync`.
8. **Replace polling MVP** with an in-process bus, or document why polling is required.

After those, the plan is implementable as written.