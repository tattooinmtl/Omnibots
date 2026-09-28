# Grok audit — do the bots actually live and keep the work working?

> **Update 2026-09-27 (listen):** `omnibots/bots/presence.py` (plan A7.a.15) keeps every bot reading the board, with no 30-minute cap on that loop. An idle bot picks up a job assigned to it. Omi checks a project when its files change, until the project is cancelled.
>
> **Update 2026-09-27 (fixes):** Section 7 is the build list that takes the remaining gaps to Grok Bot grade. It is the spec. It is not built yet.

**Date:** 2026-09-27
**Auditor:** Grok
**Asked:** Can every bot do the job it is given, think and act on its own, use whatever tools and search it needs, and — once created — keep working until the project is closed, so the result stays up and nothing stays broken? Does Omi evolve and have a mind of its own the same way?

**Answer:** Not yet. Listening is in. A bot still cannot reach for whatever tool the job needs, Omi is still told not to do the work, workers still cannot talk to each other, and "done" is still a sentence plus a file that exists. Section 7 is the fix list, measured against Grok Bot: an always-on agent with its own computer, that finishes the job in the real tool, checks the result, and keeps the routine.

The gap list below is the first audit. Where A7.a.15 already changed the code, the item says so. Section 7 is what to build next.

What already works, so the gaps are specific: while a job is running, `BotAgent` really does think, call tools, see the result, and try again (`omnibots/runtime/agent.py`). Command evidence is checked against commands the bot actually ran (`omnibots/board/ledger.py`). After a job, a few lessons can be written into `memory.md`. Playbooks can be updated at the end of a goal. Search, the browser, shell, git, and a per-bot computer exist as tools. They are not given to every bot, and nobody keeps using them after the goal is marked finished.

---

## 1. A bot's life starts and ends with one job

**[CRITICAL] Once created, a bot is an idle profile, not a running mind**

- Where: `omnibots/bots/profile.py` (a bot "is NOT a running model session; the job runner starts those"), `omnibots/bots/runner.py` `run()` (status `working` only for the job, then `idle`), `omnibots/runtime/agent.py` `run()`.
- What happens: creation writes a folder and a `memory.md`. No model is thinking. The loop starts only when `JobRunner.run` is called. The loop stops when the model answers without a tool call, hits 40 steps (workers) or 200 steps (Omi), repeats the same call 5 times, runs out of tokens, or the goal clock hits 30 minutes (`settings.py` `goal_minutes = 30`, `orchestrator/goal.py`).
- The worker prompt tells it to stop: "Do not call a tool after you are finished" (`runtime/agent.py` `WORKER_PROMPT`).
- Against the ask: a created bot does not begin a life. It waits, works one task, then sleeps until someone assigns another task.

**[CRITICAL] A finished worker stops listening** — partly fixed by A7.a.15

- Where: `omnibots/bots/presence.py` now keeps an inbox open and reads it every `listen_seconds` (default 5). `omnibots/ui/live.py` tells an idle worker the note will be read. The job inbox inside `runner.py` still closes when that job ends; the presence loop is a second subscription and stays up.
- Still open: a job `run_goal` cancels is marked `interrupted`, and presence only starts jobs whose status is `assigned`. After a goal ends, that work waits for Start. The listen loop polls. It does not sit and think between jobs.

**[CRITICAL] Workers are killed when the goal ends**

- Where: `omnibots/orchestrator/goal.py` `_stop_leftover_workers` ("no worker outlives its goal"), then `projects.set_status` to `done`, `failed`, or `open`.
- What happens: after Omi's turn, running workers get at most 5 minutes (`grace_seconds = 300`), then they are cancelled. The project is marked finished. There is no status meaning "this project stays alive until the user closes it," and no loop that keeps the team on that project.
- Against the ask: life is tied to the goal timer, not to the project staying open.

**[HIGH] Idle chat, crash, and app restart do not resume a life**

- Where: `omnibots/engine.py` `_recover_orphans` (anything still `working` becomes `idle`; live jobs become `interrupted`), `omnibots/orchestrator/team.py` `start()` (only if you press Start).
- What happens: a crash or quit does not wake the team. Interrupted work waits for you. Closing the window hides it; it does not keep a project maintained.

---

## 2. Omi does not have a mind of its own, and does not keep evolving

**[CRITICAL] Omi is written as a dispatcher who must not do the work**

- Where: `omnibots/bots/runner.py` `BOSS_PROMPT` ("You coordinate; you don't do the work yourself." "Never do the workers' jobs yourself."), `omnibots/orchestrator/boss_tools.py` `assign_job` ("you don't work jobs yourself"), `omnibots/bots/profile.py` `ensure_boss` (tools are only `read_file` and `list_dir`).
- What happens: Omi's own tools are plan, list, create a bot, assign, accept or reject a claim, review, council, ask you, submit. Omi cannot search, write the project, run code, browse, or use a computer unless those names are later added to the `omi` row. New goals call `start_goal`, which runs one boss job and then stops. Between goals Omi is idle. A chat to idle Omi starts a new goal; it is not an ongoing mind.
- Against the ask: Omi does not evolve by living in the project. Omi runs a shift and clocks out.

**[HIGH] "Evolution" is a short note after the goal, and sometimes it is skipped**

- Where: `omnibots/orchestrator/playbooks.py` `retrospective`, `omnibots/bots/memory.py`.
- What happens: at the end of a goal a cheap model may add up to 4 lesson bullets and maybe a playbook. If the goal succeeded and a playbook was already used, the function returns immediately and writes no new lessons. Lessons are capped (about 3 per job for workers, 15 kept, file trimmed at 32 KB). The "Long-Term Notes" section is never written by the system. There is no tool for a bot to record what it decided, what it believes, or what it will watch next.
- Personality (`omnibots/omni/personality.py`) is shared joke lines copied from Omni. Every bot is the same face and the same taunts. That is not a private, changing mind.

**[HIGH] Planner, reviewer, and council are one-shot calls, not living bots**

- Where: `omnibots/orchestrator/planner.py` (`make_plan` is one JSON call), `omnibots/runtime/review.py` (one critic call over the diff), `omnibots/orchestrator/council.py` (`hold_council`).
- What happens: they have no profile, no `memory.md`, no tool loop, and no life after the call returns. The "team" in the plan (planner, web agent, coder, reviewer) is not a set of agents that stay on the project.

**[MEDIUM] `multi_provider` is stored and never used**

- Where: `omnibots/bots/profile.py` writes the flag. Nothing in `omnibots/providers/` reads it.
- What happens: a bot cannot decide, per thought, to use another provider. It is stuck on the chain it was created with (`minimax` first, or the cheap lane). At 90% MiniMax use, new bots are forced onto the cheap lane (`orchestrator/factory.py` `lane_chain`).

---

## 3. Bots cannot use any tool they need

**[CRITICAL] Each bot only receives the tool names stored on its profile**

- Where: `omnibots/bots/runner.py` `tools_for` ("A bot only gets the tools it was assigned"), `omnibots/runtime/tools.py` `subset`, `omnibots/bots/profile.py` `DEFAULT_TOOLS`.
- Default list: `read_file`, `write_file`, `list_dir`, `run_python`, `grep`, `find_files`, `find_skill`, `invoke_skill`, `ask_help`.
- Not in the default list: `web_search`, `web_fetch`, the browser, `run_shell`, git, `create_tool`, and every `computer_*` tool. Those exist in the pool (`runtime/core_tools.py`, `runtime/browser.py`, `runtime/computer_tools.py`) and are stripped unless Omi named them in `create_bot`.
- Omi's create tool requires a `tools` array (`orchestrator/boss_tools.py`). The factory drops any name it does not already know (`orchestrator/factory.py` `KNOWN_TOOLS`). A bot cannot pick up a tool mid-job because it thought of it. There is no boss tool to add tools to a bot that already exists (`BotRegistry.update` exists; Omi is not given it).

**[HIGH] A tool the team forges is not actually given to the bots**

- Where: `omnibots/orchestrator/forge.py` says promoted tools are "loaded into every bot's pool." `runner.pool()` does add them. `tools_for()` then subsets to `prof.tools`.
- What happens: a new forged name is not on any existing profile, so no bot can call it. Forge also refuses risk above R2 and bans network imports, so a forged tool cannot search, deploy, or act on a live site.

**[HIGH] Workers cannot talk to each other**

- Where: `omnibots/board/a2a.py` — a worker `send_message` to anyone but `omi` returns an error.
- Against the ask: they are not independent agents coordinating. They are spokes. Only Omi routes, and only during the goal.

**[MEDIUM] Two hard stops even when the tools were assigned**

- A worker job is planned with `max_attempts: 2` (`orchestrator/boss_tools.py`). After two failures the job escalates and waits for a decision.
- A job marked R3 or higher cannot be assigned until some council verdict exists for that project (`assign_job`). One council on any question unlocks every later R3 job. That is a speed bump, not a check that this deploy is safe.
- Money (R4) and destructive commands (R5) still ask you. That matches the 2026-09-26 rule in `PLAN.md` §3. It is listed here so it is not confused with the tool lock above. It does not by itself keep a site up.

**[MEDIUM] The bot risk ceiling does nothing at runtime**

- Where: factory always stores `risk_ceiling="R2"` (`orchestrator/factory.py`). No code under `omnibots/runtime/` reads `risk_ceiling`.
- What happens: the label looks like a limit. The real limit is the tool list. A bot given `git_push` or `browser_click` can use them regardless of the R2 label (R4/R5 still ask).

---

## 4. Search is not "whatever they need"

**[HIGH] Search is off unless Omi remembered to assign it**

- Where: `web_search` / `web_fetch` are R2 tools in `omnibots/runtime/web_tools.py`. They are not in `DEFAULT_TOOLS`. Omi's own profile does not include them.
- A research job assigned to a default worker cannot search. The planner is forbidden from naming tools (`planner.py`), so the plan cannot say "this job needs search." Omi has to guess the tool list at `create_bot` time.

**[MEDIUM] There is one shared search pipe, with a weak fallback**

- Where: `web_tools.py` `gateway_search`. One key (`omnibots` / `search_gateway`), categories limited to general, it, science, news. The gateway allows 30 searches a minute per key (`deploy/search-gateway/gateway.py`). If the gateway is down or has no key, the fallback is DuckDuckGo's instant-answer API, which often returns nothing. `PLAN.md` says Google CSE was the only engine answering at one point.
- `url_quote` evidence checks that the bot typed a quote. It does not open the URL and check the quote is there (`ledger.py`).

**[MEDIUM] Private and local targets are refused**

- `web_fetch` and the browser refuse loopback, private, and link-local addresses (`web_tools.py` `assert_public_url`). That blocks cloud-metadata theft. It also blocks a bot from reading a dev server it started, unless it passes `allow_internal` and the host is localhost on the first hop. A result hosted on a LAN URL cannot be checked.

---

## 5. Nothing keeps the project working after it is "done"

**[CRITICAL] Finished means stop, not maintain** — file watch only, as of A7.a.15

- Where: `omnibots/bots/presence.py` `_watch_files` diffs the project folder and, when it is quiet, runs one Omi maintenance turn with no `goal_minutes` budget. `cancelled` projects are skipped.
- Still open: nothing opens the live URL, reruns tests, or restarts a service. `PLAN.md` A14.a.03 and A10.c.01 (hosting connectors) are not started. `git_push` exists; Netlify, Vercel, Cloudflare, DNS, and SFTP connectors do not. A maintenance turn can itself stop after 80 steps. It does not become a standing watch on the URL.

**[CRITICAL] The "always working" machinery is not connected to Omi or to a project**

- Where: `omnibots/projects/schedule.py`, started from `omnibots/engine.py` `_startup`.
- Routines (cron) and triggers (file change, board message, disk or MiniMax threshold) only run rows already in SQL. Nothing in the app, the UI, or Omi's tools calls `routines.add` or `triggers.add`. A Monday uptime check does not exist unless a person inserts it.
- Webhook and email or calendar triggers are not implemented (`Triggers.KINDS` is only `file`, `board`, `threshold`). `PLAN.md` A10.e.02 says the webhook needs your OK because it conflicts with "no HTTP server."
- Night shift (`NightShift.loop`) runs, but `enqueue` is only called from `tests/test_a6_projects.py`. The app never queues night work. The queue is also in memory, so a restart would drop it. The night worker is one cheap bot running a new goal, not a caretaker of an open project.

**[HIGH] "The result is active" is not something the team checks**

- A claim can pass because a file exists (`ledger.py` checks the path and the size) or because Omi's model says the evidence matches. `accept_claim` does not run the project, open the URL, or compare the claim to the done-criteria in code.
- `review_work` is one diff review, and only if Omi calls it before the clock runs out. A PASS does not start a keep-alive job. It cancels leftover planned jobs (`_close_unneeded_jobs`).
- Bot computers stop after 15 idle minutes (`deploy/bot-computers/gateway/app.py` `IDLE_MIN`). A computer is not a standing host for the project.

**[HIGH] The goal clock can abandon the work**

- Where: `orchestrator/goal.py` default 30 minutes, then `_resumable_if_out_of_time` posts a question: press Start to continue.
- The team does not keep going. You have to come back. Approval waits do not eat that clock (`agent.py` `waiting_on_user`). Thinking and tool time does.

**[MEDIUM] Stall watch only nudges a bot that is already inside a job**

- Where: `omnibots/orchestrator/team.py` `check_stalls`.
- After 5, 10, and 15 quiet minutes it steers the worker, tells Omi, then asks you. It does nothing for idle bots, finished projects, or a live site that went down.

---

## 6. Can they actually do the jobs?

| Job you would hand them | What the code can do today |
|---|---|
| Write files, run Python, grep, in one assigned job, if the model finishes inside the step and time caps | Yes, for a default worker. |
| Search and read the web while doing that job | Only if that bot's profile lists `web_search` and `web_fetch`. Omi does not have them. |
| Browse, click, deploy, use the bot computer | Only if Omi listed those tool names at creation, and only during the job. Click, type, and push are R3 (allowed without a prompt under the current `ask_from = R4` setting). The computer still idles out. |
| Talk, plan, and verify as a standing team | No. Only Omi routes, and workers cannot message each other. Planner and reviewer are single calls. The listen loop (A7.a.15) only picks up jobs already assigned. |
| Keep a shipped site up, notice it broke, and fix it until you close the project | Partly. A file change wakes one Omi check. A live URL, a cron, and a night queue still do not. |
| Omi grows a mind and keeps running the project | No. Omi coordinates one goal, may store a few lessons, then the listen loop waits. It does not write its own long-term notes. |

---

## 7. Fixes — Grok Bot grade

The bar is Grok Bot (xAI, August 2026), not a chat session that ends when the answer is typed.

- Each bot stays on, on its own computer, and keeps going after the person steps away.
- A chief of staff runs the roster. Specialists own a lane. They message each other, hand work over, and take ownership in one shared thread.
- The job is finished in the real tool (the site, the repo, the inbox), not in a report that says it is finished.
- The bot checks its own result: read the code, open the page, run the script. It is not done at 90%.
- Show the person only for a judgment call (money, a destructive act, a login the bot cannot complete).
- Learning is a routine: watch the job once, keep the steps, rerun them, and apply corrections.

OmniBots already has the pieces Grok Bot is made of: a per-bot computer on the VPS, a browser profile per bot, a vault, a board, playbooks, and now a listen loop. The fixes wire those pieces into that life. Money (R4) and destructive acts (R5) stay behind your click. That part matches Grok Bot's "ask only when a person must judge."

Build them in this order. Each one is usable before the later ones exist.

### F1. The life does not end when the goal function returns

Closes: section 1, and the hole left in A7.a.15.

- `run_goal` stops cancelling workers just because Omi submitted. A7.a.11 stays only for the CLI harness that is about to close the database. In the app, those jobs stay `assigned` and the presence loop carries them.
- User Stop and Panic still mark jobs `interrupted` and the listen loop does not restart those. Goal-end and user-stop must not share one status.
- Between jobs the bot is not only polling. On each quiet cycle it may take one short thought: read the newest board lines and its memory, and either stay quiet or start work. Cap that idle thought (one model call, cheap lane) so a silent project does not burn the MiniMax seats.
- Restart of the app resumes `assigned` work for projects that are not cancelled. It does not resume a user Stop.

Done when: quit the app in the middle of a job, reopen it, and the same bot continues without Start. Press Stop, and it stays stopped.

### F2. Every bot can use the tool the job needs

Closes: section 3 and section 4.

- `DEFAULT_TOOLS` becomes the full `KNOWN_TOOLS` list, including `web_search`, `web_fetch`, browser, shell, git, and `computer_*`. `ensure_boss` gets that same list plus the boss toolkit. New bots are not born blind.
- `tools_for` stops dropping tools that are not on the profile. The profile list is a preference ("start with these"), not a lock. A call to a known tool the bot did not list still runs. R4 and R5 still ask.
- Add `grant_tool` so a bot can pull a forged tool by name mid-job. When the forge promotes a tool, append that name onto every living bot, and do not reject network or R3 inside a forged tool that the reviewer passed. R4 and R5 forged tools still wait for you.
- `risk_ceiling` is enforced at `tools.run`. Default for a new worker is `R3`, so search, shell, and browser work. The factory's hardcoded `R2` is removed. A ceiling the user sets still blocks above it.
- `url_quote` evidence fetches the page and checks the quote is really there.

Done when: a brand-new coder with no tool list can search, edit, run, and open the page it just served, and a forged `check_links` tool is callable by the bot that did not exist when the tool was made.

### F3. Omi is a chief of staff who can also do the work

Closes: section 2, first item.

- Delete the hard ban "never do the workers' jobs yourself." Omi delegates when a specialist is the right owner, and does the small or stuck job itself.
- `assign_job` accepts `omi` as the worker.
- Omi's standing prompt matches Grok Bot's chief of staff: own the outcome, keep the roster, hand off, take work back when a specialist is stuck, and do not stop at a report.

Done when: a one-file fix is done by Omi alone, and a five-part site is split across specialists that Omi chose.

### F4. Bots talk to each other and take ownership

Closes: section 3, "workers cannot talk to each other."

- A worker may `send_message` any bot on the same project. The message is copied to Omi so the hub still sees it (ADR-10's visibility stays; the ban on peer messages goes).
- Add `claim_ownership(job_id)` so a free bot can take a ready job from the board without waiting to be named. Omi can take it back.
- Planner, reviewer, and each council seat are real bot profiles with `memory.md` and the listen loop, created once and reused. The one-shot functions in `planner.py`, `review.py`, and `council.py` become the first turn of those bots, not a substitute for them.

Done when: the coder messages the reviewer directly, the reviewer answers on the board, and Omi's window shows both messages.

### F5. A mind that writes itself

Closes: section 2, lessons and personality.

- Add `remember(note)` which appends to that bot's Long-Term Notes. The system is allowed to write that section. The user can still edit it.
- The retrospective always writes lessons, including when the playbook worked (what to repeat, not only what broke).
- Each memory file gains `# Watching`: the URLs, commands, and files this bot is responsible for. The listen loop reads it.
- Taunts stay shared. The private mind is `memory.md`, not the joke lines.

Done when: after a project, Omi's Long-Term Notes contain a decision it made, and the next goal's prompt includes that decision without anyone pasting it.

### F6. Done means checked, the way `/goal` checks

Closes: section 5, "the result is active," and section 6.

- `accept_claim` does not accept on Omi's say-so alone. It runs the done-criteria: a named command must have been run, a named test must pass now, a named URL must be fetched and contain the quote.
- A deploy claim stores the live URL on the project. The next maintenance cycle opens it. A failure opens a fix job by itself.
- The project is not `done` until that check has passed once. `done` does not mean "stop watching."

Done when: a claim that cites a test which fails is rejected with the real output, and a claim that cites a URL the page does not contain is rejected.

### F7. Stay on the result until the project is closed

Closes: section 5, routines and the night shift.

- Boss tools: `add_routine`, `add_trigger`, `enqueue_night`. The night queue moves from the in-memory list to SQL.
- When a live URL is stored, Omi adds a routine that fetches it on a schedule until the project is closed. The routine's goal is "if this is down or broken, fix it," not "start a fresh project."
- `close_project` is a real action (tray and a boss tool). It sets `cancelled`, stops that project's routines, and is the only thing that ends the watch. `done` keeps the watch.
- A project can pin its bot computer so the 15-minute idle reaper does not shut the host. Ordinary desktops still idle out.
- Webhook and mail triggers stay behind your OK (ADR-2). File, board, cron, and URL checks do not need a public port.

Done when: a site that returns 500 overnight produces a fix job by morning without anyone typing a goal, and Close on that project stops the checks.

### F8. The work clock does not abandon a live project

Closes: section 5, the 30-minute goal, and the 40-step worker cap.

- `goal_minutes` remains a budget for one burst of boss thinking. When it hits, presence re-queues the unfinished jobs as `assigned` instead of posting "press Start."
- The stuck-loop guard stays (five identical calls still stops that approach). The 40-step cap becomes "summarize and continue on the next burst," not "the job is blocked."
- Maintenance bursts use the same rule.

Done when: a goal that is still unfinished at 30 minutes is continued by the listen loop with no button press, and the board shows the handoff.

### F9. Search is a normal sense, not a privilege

Closes: section 4.

- F2 puts search on every bot, including Omi.
- One gateway key stays, with the 30-per-minute limit visible in the tool error so the bot switches to `web_fetch` on a known URL instead of retrying blindly.
- A bot may read the dev server it started (`allow_internal` on its own localhost port) without a special argument it has to remember. Other private addresses stay refused.

Done when: Omi, with a fresh profile, searches and then opens the page, and a bot reading its own `localhost` preview is not blocked.

### F10. The computer is the bot's desk

Closes: the gap with Grok Bot's "own computer."

- The VPS desktop (`computer_*`) is the place a bot goes when the work is in a real app: browser login, a GUI tool, a long-running process.
- The vault login and the handoff for a captcha or a password stay as they are (PLAN §3.3).
- F7's pin is what keeps that desk from vanishing at 15 idle minutes while the project is open.

Done when: a bot starts its computer, does the job there, you can watch it from the existing Computer button, and the computer is still running an hour later because the project is open.

### What this does not copy

Grok Bot's hosting, billing, and model stay at xAI. OmniBots stays on this PC plus the VPS computers you already run. The bar is the behavior: always on, own desk, talk to each other, finish in the real tool, check the result, learn the routine, ask you only for money, destruction, or a login you have to finish.

### Order

| Order | Fix | You can see it when |
|---|---|---|
| 1 | F1 life survives the goal | Reopen the app and the job continues |
| 2 | F2 and F9 tools and search | A new bot searches without being given a tool list |
| 3 | F6 checked done | A lying claim is rejected with the real output |
| 4 | F3 and F4 chief of staff and peer talk | Omi does a small job; two workers talk |
| 5 | F5 memory | The next goal quotes a decision from the last one |
| 6 | F7, F8, F10 keep it up | A broken URL fixes itself until you close the project |

---

## Evidence index

| Claim | File |
|---|---|
| Turn loop ends on a plain answer, 40 steps, or a repeated call | `omnibots/runtime/agent.py` |
| Boss must not do the work; worker stops reading when the job ends | `omnibots/bots/runner.py` |
| Profile tool list is the only tool list; defaults omit search | `omnibots/bots/profile.py`, `omnibots/bots/runner.py`, `omnibots/runtime/tools.py` |
| New workers are capped in the factory; forged tools cannot be R3 or use the network | `omnibots/orchestrator/factory.py`, `omnibots/orchestrator/forge.py` |
| Goal lasts 30 minutes, then workers are stopped | `omnibots/orchestrator/goal.py`, `omnibots/settings.py` |
| Workers may only message Omi | `omnibots/board/a2a.py` |
| Idle worker chat is not a new job | `omnibots/ui/live.py` |
| Search is one shared gateway plus a weak fallback | `omnibots/runtime/web_tools.py` |
| Routines, triggers, and night shift are not wired to Omi; night queue is never filled by the app | `omnibots/projects/schedule.py`, `omnibots/engine.py` |
| Memory does not grow a long-term mind; a successful playbook skips new lessons | `omnibots/bots/memory.py`, `omnibots/orchestrator/playbooks.py` |
| Hosting, adapters, webhooks, health, and end-to-end acceptance are not built | `PLAN.md` A10.c, A10.d, A10.e, A12, A13, A14 |
