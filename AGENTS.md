# AGENTS.md — OmniBots Python App (DEVELOPMENT STAGE)

> **Read `PLAN.md` before starting any work on this project.** It is the only plan you work from.
> This file defines the rules that ALL agents (Claude, GPT, local models, etc.) MUST follow while implementing OmniBots.

---

## 1. Project Context

OmniBots is a Python desktop multi-agent automation system:

- One **orchestrator / boss / maestro / master agent** receives user goals.
- It creates and coordinates **worker bots** through a shared **message board**.
- Each bot has a profile, a `memory.md`, assigned skills, and assigned tools.
- The user controls everything from a **PySide6 desktop app** with a **system tray icon**.
- **Multiple LLM providers** run side-by-side (MiniMax M3 as main + small/cheap APIs for small jobs). Quota, 429 recovery, and per-bot usage % are tracked live in SQL.

### Project files — where everything is

| File | What it is | Who edits it |
|------|-----------|--------------|
| `AGENTS.md` | These rules. Start here. | Only with the user's OK (see §7) |
| `PLAN.md` | **The master plan.** Goal (§1), architecture decisions / ADRs (§2), safety model (§3), coordination design (§4), **Phase A** (standalone Python app) and **Phase B** (Omni plugin) work items, and the Notes Log (§9). | Every agent: status + Notes, per §2 below |
| `docs/history/PLAN_v1.md` | The old master plan. Its §3 is the user's original vision, kept word for word, so read it for product intent. | **Nobody.** History only; never update it. |
| `docs/history/AUDIT_*.md` | Past audits of the plan. Their fixes are already applied in `PLAN.md`. | Nobody (history) |
| `omni_patch/`, `omni-plugin.json` | Phase B material for the Omni plugin. **Do not apply the patch to Omni** before `PLAN.md` item A14.99 is ✅ and the user says go. | Phase B work only |

Omni (the agent harness at `~/.omni`) is a **separate project**. OmniBots reads Omni's config files but **never writes to them**.

---

## 2. Phase Tracking Rule (MANDATORY)

Every agent working on OmniBots MUST follow these steps:

### 2.1 Before starting work
1. Read `PLAN.md` (at least §2 ADRs, §3 Safety, and the phase you work on).
2. Find the work item that matches your task (e.g., `A0.a.01`, `A9.b.01`, `B1.a.02`). Phase B items stay closed until A14.99 is ✅.
3. If no matching sub-phase exists, **ADD a new one** (with today's date and your agent name) before starting. Don't silently invent work.
4. Confirm the phase status is `⏳ Not Started` or `⚠️ Blocked`. If it's already `🟡 In Progress` or `✅ Passed`, coordinate with whoever is on it.

### 2.2 When you start work
1. Update the phase status from `⏳` to `🟡 In Progress`.
2. Add a dated line to the phase's **Notes** section:

   ```
   - YYYY-MM-DD <agent-name>: starting work
     - Goal: <one-line summary>
   ```

### 2.3 While you work
1. As you create or modify files, append them to the phase's Notes section.
2. If your change affects **another phase** (e.g., you added a column to the `bots` table while working on the Bot Factory), add a cross-reference note at the top of that affected phase AND an entry in `PLAN.md` §9 (Notes Log).
3. Changing an architecture decision (ADR in `PLAN.md` §2) or the safety model (§3) needs the user's OK first.
4. If you discover a phase needs to be split or merged, do it; explain in the Notes.
5. If you find an unrelated bug, file it as a new sub-phase under the right phase — do not silently fix it.

### 2.4 When you finish
1. Verify the phase acceptance criteria are met.
2. Update the status:
   - `✅ Passed` — works + tests/verification pass (attach evidence: test output, screenshot, command result)
   - `❌ Failed` — tests fail or acceptance not met (attach the failure reason)
   - `⚠️ Blocked` — you cannot proceed (explain what's blocking; the user decides next)
3. Add a final dated line summarizing what shipped:

   ```
   - YYYY-MM-DD <agent>: PASSED
     - Files: `app/auth/login.py`, `tests/test_login.py`
     - Side effects: changed `users.password_hash` column (nullable=false now)
     - Tests: `pytest tests/test_login.py::test_valid_login` → PASSED
     - Notes: <anything the next agent should know>
   ```

### 2.5 Notes Format (full)

```
- YYYY-MM-DD <agent>: <short summary>
  - Files: `path/to/file.py`, `path/to/other.py`
  - Side effects: <what else in the codebase changed>
  - Tests: `<command>` → PASSED / FAILED / SKIPPED
  - Notes: <handoff info for the next agent>
```

---

## 3. Status Legend

| Symbol | Status |
|--------|--------|
| ⏳ | Not Started |
| 🟡 | In Progress |
| ✅ | Passed |
| ❌ | Failed |
| ⚠️ | Blocked |
| ⛔ | Dropped (cancelled by the user — kept struck-through for history, never implement) |

---

## 4. Hard Rules for All Agents

These are non-negotiable, drawn from the user's standing operating preferences:

1. **Never delete or overwrite existing files without explicit user confirmation.** Phrases like "rebuild", "rewrite", or "redo" mean **add-on-top-of**, not delete-and-start-over.
2. **Always explain before acting.** No silent file writes, edits, or installs. Tell the user what you're about to do and why.
3. **Push back on bad ideas.** If a request seems wrong, surface smaller alternatives instead of treating the request as a foregone conclusion.
4. **Test in the real environment**, not mocked unit tests. Success = the user can run it and confirm it works end-to-end.
5. **If unsure, ask.** Don't guess when direction is unclear.

---

## 5. Agent Transition Note (IMPORTANT — read before changing this file)

**This `AGENTS.md` is for the DEVELOPMENT STAGE only.**

Once OmniBots is built and the bots themselves begin using the system, this file will be **REPLACED** with a different one — one that turns the bot into a CEO / Boss / Maestro / Orchestrator / Master agent. The new persona:

- Reads user config and orchestrates the dance.
- Generates a bot-creation script to spawn the exact worker he needs based on the job.
- The created bot receives **credentials**, logs in to its own profile, and joins the message board.
- **Multi-provider routing**:
  - **MiniMax M3** = main agent + 3 worker sub-agents (vision, text, TTS arc1.0, H3 video outputs). Full stack. Up to 200 tool calls/hr and 20k+ TPM — **needs live verification**.
  - **NVIDIA** = 1500 credits / 5 hr / 35 tool calls — Nemotron 330b or 3.5 Lightning.
  - **Agnes AI** = 1500 credits / 30 tool calls.
  - **Other small APIs** = xkiro, groq, atria dawn, open router, etc. — for small jobs.
- When `multi_provider = ON`, a bot can use multiple providers; usage % is shown live on its card.
- When all small providers return **HTTP 429**, they stop until the API key reset window expires and usage returns to 0%. Quota state is persisted in SQL for stats.

When that transition happens, **replace this file wholesale** with the boss-stage AGENTS.md. Do not mix dev-stage and boss-stage instructions in one file.

---

## 6. Quick Reference

- **Master plan**: `PLAN.md` (the only one; `docs/history/` is read-only history)
- **Work item ID format**: `<major><group>.<sub>.<item>` — examples `A0.a.01`, `A11.c.01`, `B2.a.01`. Every group ends with `.99` = acceptance test; a group is ✅ only when its `.99` passes for real.
- **Status updates**: edit the item's status emoji inline
- **Notes**: append under the group's Notes section, dated, with your agent name
- **Cross-phase changes**: also append to `PLAN.md` §9 (Notes Log)
- **New work item**: add it to `PLAN.md` first, then start work
- **Order**: Phase A top to bottom (A0 → A14). Phase B only after A14.99 ✅ + user go.

---

## 7. If You Discover a Problem with This File

If `PLAN.md` or this `AGENTS.md` has a wrong rule, a missing phase, or an out-of-date assumption: do **not** silently fix it. Flag it to the user with what you found and your proposed change. The user decides.
