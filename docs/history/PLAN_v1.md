> **📦 ARCHIVED 2026-09-25: history only, do not edit or work from this file.** The master plan is now `PLAN.md` at the project root. §3 below is still the user's original vision, word for word.

# Phased Plan Master — OmniBots Python App

> **Master development plan + live progress tracker for the OmniBots desktop multi-agent system.**
> This file is the source of truth for WHAT OmniBots is, WHICH phases exist, and WHAT each agent changed while implementing it.
>
> **All agents (Claude, GPT, local models, etc.) MUST follow the rules in `AGENTS.md` while working on this project.**

---

## Table of Contents

1. [Status Legend](#1-status-legend)
2. [How to Use This File](#2-how-to-use-this-file)
3. [Original Plan (verbatim)](#3-original-plan-verbatim)
4. [Phase Breakdown](#4-phase-breakdown)
   - [Phase 0 — Project Foundation & Tooling](#phase-0--project-foundation--tooling)
   - [Phase 1 — Database & Schema](#phase-1--database--schema)
   - [Phase 2 — Local API Service](#phase-2--local-api-service-fastapi)
   - [Phase 3 — Authentication & Email Verification](#phase-3--authentication--email-verification)
   - [Phase 4 — Desktop Shell](#phase-4--desktop-shell-pyside6)
   - [Phase 5 — Main Configuration Panels](#phase-5--main-configuration-panels)
   - [Phase 6 — Bot Profiles & memory.md](#phase-6--bot-profiles--memorymd)
   - [Phase 7 — Message Board](#phase-7--message-board-coordination-layer)
   - [Phase 8 — Jobs & Task Tracking](#phase-8--jobs--task-tracking)
   - [Phase 9 — Orchestrator](#phase-9--orchestrator-boss-agent)
   - [Phase 10 — Bot Factory](#phase-10--bot-factory-dynamic-worker-creation)
   - [Phase 11 — Skill & Tool Pools](#phase-11--skill--tool-pools)
   - [Phase 12 — Provider Management & Quota System](#phase-12--provider-management--multi-provider-quota-system)
   - [Phase 13 — Maestro / CEO Bot](#phase-13--maestro--ceo-bot-orchestrator-as-bot)
   - [Phase 14 — Reset & System Controls](#phase-14--reset--system-controls)
   - [Phase 15 — Observability & Extras](#phase-15--observability--extras)
   - [Phase 16 — Bot Window (ID Card) & Live Faces](#phase-16--bot-window-id-card--live-faces)
5. [Notes Log](#5-notes-log)

---

## 1. Status Legend

| Symbol | Status | Meaning |
|--------|--------|---------|
| ⏳ | Not Started | Phase defined, no work yet |
| 🟡 | In Progress | An agent is actively implementing this |
| ✅ | Passed | Implementation complete, tested, verified |
| ❌ | Failed | Attempted but did not pass acceptance |
| ⚠️ | Blocked | Cannot proceed; needs user input or external fix |
| ⛔ | Dropped | Cancelled by the user; kept struck-through for history — never implement |

---

## 2. How to Use This File

1. **Read** this file before starting any work. Find the phase + sub-phase that matches your task.
2. **Update status** from `⏳ Not Started` → `🟡 In Progress` when you begin.
3. **Add notes** to the phase's Notes section as you work:
   - What you did
   - Files created or modified (paths)
   - Other parts of the codebase that were affected
   - Test results (commands run + outcome)
4. **Mark complete** when done: `🟡 → ✅ Passed` (with evidence), `❌ Failed` (with reason), or `⚠️ Blocked` (with what's blocking).
5. **Add new sub-phases** if needed; never delete existing ones without explicit user confirmation.
6. **Cross-phase changes**: if your work affects another phase, add a dated note at the top of that phase too, AND an entry in Section 5 Notes Log.

---

## 3. Original Plan (verbatim)

> The following plan was provided by the user as the source-of-truth vision for OmniBots. It is preserved here EXACTLY as written, unchanged. All phase breakdowns in Section 4 map back to sections of this document.

---

This is a very strong foundation for an **OmniBots / Grok-style multi-agent automation system**. What you're describing is basically:

- a **bot network**
- with a **boss/orchestrator**
- a **shared message board**
- **per-bot profiles + memory.md**
- a **dynamic worker factory**
- **skill/tool pools**
- a **user account system with email verification**
- and a **Python desktop/tray interface** for controlling everything

Below is a full planning blueprint you can use as the architecture and product spec.

---

# 1. Core Product Concept

## System name idea
**OmniBots**

## Main idea
A local or hosted automated agent system where:

- one **main orchestrator bot** receives goals/tasks
- creates specialized **worker bots** as needed
- every bot has:
  - a profile
  - a job history
  - a `memory.md`
  - assigned skills
  - assigned tools
- all bots coordinate through a **shared message board**
- the user controls everything from a **Python GUI + tray icon**

---

# 2. High-Level Architecture

## Major layers

### A. Desktop / Tray App
This is the user-facing Python interface.

Responsibilities:
- register/login
- open main configuration window
- minimize to tray
- tray context menu controls
- show:
  - bot list
  - running jobs
  - message board
  - config
  - providers
  - parameters
  - logs

Suggested tech:
- **PySide6 / Qt** for:
  - main window
  - tray icon
  - context menus
  - panels
- alternative:
  - `pystray` + `webview` / Flask/FastAPI UI

Best recommendation:
> **PySide6 + local FastAPI backend**

Why:
- Python native
- powerful desktop UI
- native tray support
- easy to connect to a local API/server

---

### B. Local Backend / Control Service
A local Python service running the system.

Responsibilities:
- auth/register/verify
- bot management
- job management
- message board
- orchestrator runtime
- provider config
- parameter config
- reset/start/stop controls

Suggested tech:
- **FastAPI**
- **SQLAlchemy**
- **SQLite** for MVP
- later: **PostgreSQL**

---

### C. Bot Runtime Engine
This is where bots actually live and work.

Components:
- **Orchestrator / Boss Agent**
- **Bot Factory**
- **Worker Agents**
- **Skill Registry**
- **Tool Registry**
- **Supervisor / Watchdog**

---

### D. Persistence Layer
Stores:
- users
- verification codes
- bots
- jobs
- messages
- skills
- tools
- providers
- parameters
- logs

Storage:
- SQL database for structured state
- file storage for:
  - `memory.md`
  - artifacts
  - logs
  - bot workspaces

---

# 3. Main System Components

## 3.1 User / Auth Service
Handles:
- registration
- email verification
- login
- session management

---

## 3.2 Orchestrator / Boss Agent
The "brain" of the system.

Responsibilities:
- receives user objectives
- breaks them into tasks
- decides which worker bots are needed
- creates workers dynamically
- assigns tasks
- monitors progress on the message board
- resolves blocked tasks
- reports final results

Important:
The orchestrator should **not do everything itself**.
It should delegate.

---

## 3.3 Bot Factory
Creates new worker bots on demand.

When creating a bot, it decides:
- bot name
- role
- skills
- tools
- model/provider
- memory template
- permissions
- task assignment

Example worker types:
- researcher
- coder
- writer
- tester
- data analyst
- scraper
- file organizer
- scheduler
- reviewer

---

## 3.4 Worker Bots
Each worker bot has:
- unique ID
- profile
- assigned skills
- assigned tools
- current job
- past job history
- `memory.md`
- message board access

Worker responsibilities:
- read assigned tasks
- post status updates
- use tools/skills
- write outputs/artifacts
- report completion/failure

---

## 3.5 Message Board
This is the shared coordination hub.

Every bot can:
- read relevant messages
- post updates
- request help
- report progress
- hand off artifacts

This is one of the most important parts of your design.

---

## 3.6 Skill Pool
Skills are **capabilities**.

Examples:
- coding
- summarization
- web research
- email drafting
- data extraction
- planning
- testing
- file conversion
- SQL generation
- image prompting

A skill is not necessarily a direct executable tool.
It is more like:
- a role capability
- a prompt package
- a permission set
- a compatible tool bundle

---

## 3.7 Tool Pool
Tools are **executable functions**.

Examples:
- `read_file`
- `write_file`
- `run_python`
- `search_web`
- `send_email`
- `http_request`
- `sql_query`
- `scrape_url`
- `create_task`
- `spawn_bot`
- `stop_bot`

Important distinction:
- **Skill = ability/role**
- **Tool = action executable**

---

## 3.8 Provider Manager
Stores and manages external services.

Examples:
- LLM providers:
  - OpenAI
  - Anthropic
  - OpenRouter
  - local model
- email providers:
  - SMTP
  - SendGrid
  - Resend
- search providers:
  - Brave
  - Tavily
  - SerpAPI
- storage providers:
  - local disk
  - S3

Each provider can store:
- API key
- base URL
- model name
- rate limits
- timeout
- enabled/disabled state

---

## 3.9 Parameter Manager
Global and per-bot settings.

Examples:
- max bots
- max concurrent jobs
- model temperature
- max tokens
- polling interval
- memory limit
- auto-create workers on/off
- require user approval before creating new bots
- budget cap
- log verbosity

---

# 4. Registration + Email Verification Flow

You described this very clearly. Here is the clean version.

## Registration flow

### Step 1: User enters email
User submits:
- email
- password (recommended even if token-based login)

### Step 2: System creates user in SQL
Default:
- `verified = false`

### Step 3: Generate 6-digit token
Example:
- random 6-digit code
- store hashed code in DB
- expiry time: 5–10 minutes
- max attempts: 3–5

### Step 4: Send code to email
Use:
- SMTP
- email provider API

### Step 5: User enters code in app
App submits:
- email
- code

### Step 6: Validate code
If:
- code exists
- not expired
- not consumed
- matches

Then:
- mark user verified in SQL

Example SQL logic:

```sql
UPDATE users
SET verified = 1,
    verified_at = CURRENT_TIMESTAMP
WHERE id = :user_id;
```

Also:
- mark verification token as used

---

## Login flow
After verification:
- user logs in with email + password
- or passwordless email code if you prefer

Then:
- open the main **Python configuration window**

This window becomes the **OmniBots control interface**

---

# 5. Desktop UI / Python Window Behavior

## Main App Behavior

### After login:
- open main OmniBots configuration panel
- this is the primary GUI

### When minimized:
- minimize to tray instead of closing

### Tray icon:
- single click or right click opens context menu
- double click opens main window

---

# 6. Tray Context Menu Design

You already listed many important controls. Here is a full structured menu.

## Tray Menu Example

### Top Section
- Open OmniBots
- Status: Running / Stopped
- Start System
- Stop System

---

### Bots
- Bots List
  - Bot 1
    - Start
    - Stop
    - Restart
    - Open Profile
    - Open Memory
    - View Jobs
  - Bot 2
  - Bot 3

---

### Running
- Running List
  - Running Job 1
  - Running Job 2
  - View Active Bots
  - View Current Tasks

---

### Message Board
- Open Message Board
- Pause Board Updates
- Filter by Bot
- Filter by Job

---

### Controls
- Start All Bots
- Stop All Bots
- Pause All Bots
- Resume All Bots

---

### Reset
- Reset...
  - on click:
    - show warning:
      - **"Are you sure? Yes / No"**
  - if Yes:
    - perform reset action

Reset could have two modes:
1. **Soft Reset**
   - stop active jobs
   - clear running state
   - keep memory/history
2. **Hard Reset**
   - clear jobs/board/state
   - optionally wipe bot memory

For your first version:
> use **Soft Reset** with confirmation

---

### Configuration
- Open Config
- Open Providers
- Open Parameters
- Open Skill Pool
- Open Tool Pool
- Open Orchestrator Settings

---

### Logs / Monitoring
- Open Logs
- Open Audit Trail
- Open Errors
- Open Bot Memory Viewer

---

### Session
- Lock
- Logout
- Exit

---

# 7. Main Configuration Panels

Once logged in, the main Python window can have a sidebar or tab layout.

## Suggested panels

### 1. Dashboard
Shows:
- system status
- orchestrator status
- active bots
- running jobs
- recent messages
- errors/warnings

---

### 2. Bots List
Shows all bots.

Columns:
- name
- role
- status
- current job
- skills
- tools
- last active
- memory path
- actions

Actions:
- start
- stop
- restart
- edit
- open memory
- view history

---

### 3. Running List
Shows live activity.

Columns:
- job ID
- task title
- assigned bot
- state
- progress
- started at
- last update
- actions

Actions:
- cancel
- retry
- reassign
- view logs

---

### 4. Message Board
A shared timeline/board.

Features:
- view all messages
- filter by bot
- filter by job
- filter by event type
- post manual message
- pause auto-scroll
- export conversation

Message types:
- task created
- task assigned
- progress update
- blocked
- completed
- failed
- bot created
- bot stopped

---

### 5. Config
General system settings:
- workspace path
- database path
- auto-start bots
- auto-create workers
- default orchestrator behavior
- reset behavior
- theme/UI settings

---

### 6. Providers
Manage external integrations:
- LLM providers
- email providers
- search providers
- storage providers

Fields:
- provider name
- type
- API key
- base URL
- default model
- enabled/disabled
- test connection

---

### 7. Parameters
Tunable settings.

Global parameters:
- max bots
- max concurrent jobs
- message polling interval
- default model
- max retries
- timeout
- budget cap

Per-bot parameters:
- temperature
- max tokens
- top_p
- tool_timeout
- allowed_skills
- allowed_tools
- memory_limit
- workspace_limit

---

### 8. Skills / Tools Manager
Skill pool:
- enable/disable skills
- assign to bots
- view compatible tools

Tool pool:
- enable/disable tools
- set permissions
- set timeouts
- sandbox rules

---

### 9. Memory Viewer
View a bot's `memory.md`
- read-only or editable
- show recent summaries
- show job history

---

### 10. Logs / Audit
- debug logs
- errors
- bot actions
- tool invocations
- login history
- reset events

---

# 8. Bot Profile Design

Each bot should have a structured profile.

## Bot profile fields
- bot_id
- name
- role
- description
- owner_id
- created_by_bot_id
- status
- assigned skills
- assigned tools
- provider_id
- model
- memory_path
- workspace_path
- created_at
- last_active_at
- max_concurrent_tasks
- permissions

## Example bot profile
```json
{
  "bot_id": "bot_001",
  "name": "Coder-01",
  "role": "software_engineer",
  "description": "Writes and debugs Python code",
  "status": "running",
  "skills": ["coding", "debugging", "testing"],
  "tools": ["read_file", "write_file", "run_python"],
  "provider": "openai",
  "model": "gpt-5",
  "memory_path": "/workspace/bots/bot_001/memory.md",
  "workspace_path": "/workspace/bots/bot_001",
  "created_by": "orchestrator"
}
```

---

# 9. Memory System

You said:
> each bot keeps their own memory.md

That is a very good approach.

## Recommended structure for `memory.md`

```md
---
bot_id: bot_001
name: Coder-01
role: software_engineer
status: active
created_at: 2026-09-25T10:00:00Z
---

# Long-Term Notes
- Prefers small modular functions
- Always writes tests when possible

# Lessons Learned
- 2026-09-25: File paths should be validated before writing

# Current Task
- task_id: task_123
- objective: Build login validator
- status: in_progress

# Job History
- task_120: created registration endpoint [completed]
- task_118: fixed tray icon bug [completed]
```

## Memory best practices
- keep `memory.md` human-readable
- append important completed jobs
- summarize old history periodically
- avoid unlimited growth
- separate:
  - long-term notes
  - lessons learned
  - current task
  - job history

---

# 10. Job / Task Tracking

Every bot should track past jobs in SQL too, not only in memory.

## Job lifecycle states
- `pending`
- `assigned`
- `running`
- `blocked`
- `review`
- `completed`
- `failed`
- `cancelled`

## Job record fields
- job_id
- title
- description
- status
- priority
- created_by
- assigned_bot_id
- parent_job_id
- result_summary
- artifact_path
- started_at
- finished_at
- error_message

This gives you:
- reliable SQL history
- searchable records
- bot performance history

---

# 11. Message Board Design

This is the coordination backbone.

## Recommended implementation
For MVP:
- SQL table for messages
- bots poll every 1–2 seconds

For later:
- WebSocket / Redis pubsub for real-time updates

---

## Message Board Table Fields
- message_id
- topic
- sender_type
- sender_id
- job_id
- message_type
- payload_json
- created_at

---

## Example topics
- `#general`
- `#tasks`
- `#bot/bot_001`
- `#job/task_123`
- `#orchestrator`

---

## Example message types
- `TASK_RECEIVED`
- `TASK_PLANNED`
- `BOT_CREATED`
- `TASK_ASSIGNED`
- `WORK_STARTED`
- `PROGRESS_UPDATE`
- `BLOCKED`
- `ARTIFACT_READY`
- `REVIEW_REQUESTED`
- `TASK_COMPLETED`
- `TASK_FAILED`
- `SYSTEM_RESET`

---

## Example message
```json
{
  "message_id": 981,
  "topic": "#job/task_123",
  "sender_type": "bot",
  "sender_id": "bot_001",
  "job_id": "task_123",
  "message_type": "PROGRESS_UPDATE",
  "payload": {
    "progress": 60,
    "note": "Login validation logic complete, testing next"
  },
  "created_at": "2026-09-25T12:10:00Z"
}
```

---

# 12. Coordination Protocol

To prevent chaos, define a clean task lifecycle.

## Recommended flow

### 1. User creates objective
Example:
> "Build an email verification system"

### 2. Orchestrator receives task
Posts:
- `TASK_RECEIVED`

### 3. Orchestrator plans task
Breaks into subtasks:
- create DB schema
- implement code generator
- implement email sending
- implement verification endpoint
- test flow

Posts:
- `TASK_PLANNED`

### 4. Orchestrator checks existing bots
Looks for bots with matching:
- skills
- availability
- tools

If none:
- creates a worker

Posts:
- `BOT_CREATED`

### 5. Orchestrator assigns tasks
Posts:
- `TASK_ASSIGNED`

### 6. Worker starts work
Posts:
- `WORK_STARTED`

### 7. Worker posts progress
Posts:
- `PROGRESS_UPDATE`

### 8. Worker completes task
Posts:
- `TASK_COMPLETED`

### 9. Orchestrator verifies result
Then:
- marks job complete
- updates job history
- updates bot memory
- notifies user

---

# 13. Dynamic Worker Creation System

This is one of the most important features you asked for.

## Main agent must be able to create any new worker it needs

### Bot Factory inputs
- objective
- required skills
- required tools
- role name
- model/provider preference
- permissions
- memory template

### Bot Factory outputs
- new bot record in SQL
- new bot folder
- new `memory.md`
- loaded skill set
- loaded tool set
- running process or agent task

---

## Worker creation example
User task:
> "Research competitors and write a summary"

Orchestrator decides:
- need a researcher bot
- need a writer bot

It creates:
- `Researcher-01`
  - skills: research, summarization
  - tools: web_search, read_url
- `Writer-01`
  - skills: writing, synthesis
  - tools: write_file, markdown_export

Then assigns jobs.

---

# 14. Skill Pool Design

## Skill object fields
- skill_id
- name
- description
- category
- compatible_roles
- compatible_tools
- prompt_template
- enabled

## Example skills
- research
- coding
- debugging
- testing
- copywriting
- planning
- data cleaning
- sql analysis
- ui design advice
- email composition

---

# 15. Tool Pool Design

## Tool object fields
- tool_id
- name
- description
- type
- input_schema
- output_schema
- permissions
- timeout
- enabled
- sandbox_level

## Tool types
- python_function
- shell_command
- http_api
- file_operation
- database_query
- internal_system_action

## Example tools
- `read_file`
- `write_file`
- `list_dir`
- `search_web`
- `fetch_url`
- `run_python`
- `run_shell`
- `send_email`
- `query_sql`
- `create_bot`
- `stop_bot`

---

# 16. Permissions and Safety

Very important if bots can run tools.

## Recommended permission rules
- file tools limited to workspace folder
- shell disabled by default
- network tools allowlist only
- bot creation requires budget/limit checks
- dangerous tools require user approval

## Safety limits
- max bots
- max concurrent tasks
- max retries
- max execution time
- max daily API cost
- forced human approval for:
  - deleting data
  - sending emails
  - running shell
  - creating many bots

---

# 17. Database Schema Recommendation

Here is a solid starting schema.

## users
```sql
CREATE TABLE users (
    id INTEGER PRIMARY KEY,
    email TEXT UNIQUE NOT NULL,
    password_hash TEXT,
    verified INTEGER DEFAULT 0,
    verified_at DATETIME,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
```

## email_verification_codes
```sql
CREATE TABLE email_verification_codes (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL,
    code_hash TEXT NOT NULL,
    expires_at DATETIME NOT NULL,
    consumed_at DATETIME,
    attempts INTEGER DEFAULT 0,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
```

## bots
```sql
CREATE TABLE bots (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    role TEXT,
    description TEXT,
    status TEXT DEFAULT 'idle',
    profile_json TEXT,
    memory_path TEXT,
    workspace_path TEXT,
    created_by TEXT,
    owner_id INTEGER,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    last_active_at DATETIME
);
```

## skills
```sql
CREATE TABLE skills (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT,
    manifest_json TEXT,
    enabled INTEGER DEFAULT 1
);
```

## tools
```sql
CREATE TABLE tools (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT,
    tool_type TEXT,
    manifest_json TEXT,
    enabled INTEGER DEFAULT 1
);
```

## bot_skills
```sql
CREATE TABLE bot_skills (
    bot_id TEXT,
    skill_id TEXT,
    PRIMARY KEY (bot_id, skill_id)
);
```

## bot_tools
```sql
CREATE TABLE bot_tools (
    bot_id TEXT,
    tool_id TEXT,
    PRIMARY KEY (bot_id, tool_id)
);
```

## jobs
```sql
CREATE TABLE jobs (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    description TEXT,
    status TEXT DEFAULT 'pending',
    priority INTEGER DEFAULT 0,
    assigned_bot_id TEXT,
    created_by TEXT,
    parent_job_id TEXT,
    result_summary TEXT,
    artifact_path TEXT,
    error_message TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    started_at DATETIME,
    finished_at DATETIME
);
```

## messages
```sql
CREATE TABLE messages (
    id INTEGER PRIMARY KEY,
    topic TEXT NOT NULL,
    sender_type TEXT NOT NULL,
    sender_id TEXT,
    job_id TEXT,
    message_type TEXT NOT NULL,
    payload_json TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
```

## providers
```sql
CREATE TABLE providers (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    provider_type TEXT NOT NULL,
    config_json TEXT,
    enabled INTEGER DEFAULT 1
);
```

## parameters
```sql
CREATE TABLE parameters (
    id INTEGER PRIMARY KEY,
    scope TEXT NOT NULL, -- global / bot / orchestrator
    bot_id TEXT,
    key TEXT NOT NULL,
    value TEXT
);
```

## audit_logs
```sql
CREATE TABLE audit_logs (
    id INTEGER PRIMARY KEY,
    actor_type TEXT,
    actor_id TEXT,
    action TEXT,
    details_json TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
```

---

# 18. Suggested Folder Structure

```text
omnibots/
│
├── app/
│   ├── main.py
│   ├── auth/
│   ├── api/
│   ├── core/
│   ├── orchestrator/
│   ├── bots/
│   ├── board/
│   ├── skills/
│   ├── tools/
│   ├── providers/
│   ├── ui/
│   └── tray/
│
├── workspace/
│   ├── bots/
│   │   ├── bot_001/
│   │   │   ├── memory.md
│   │   │   ├── artifacts/
│   │   │   └── logs/
│   │   └── bot_002/
│   ├── jobs/
│   └── exports/
│
├── config/
│   ├── settings.yaml
│   ├── providers.yaml
│   └── parameters.yaml
│
├── db/
│   └── omnibots.sqlite
│
└── logs/
    ├── app.log
    └── error.log
```

---

# 19. Recommended Tech Stack

## Best practical stack for your requirements
- **Python 3.12+**
- **PySide6** for GUI + tray
- **FastAPI** for local control API
- **SQLAlchemy** for ORM
- **SQLite** for MVP
- **PostgreSQL** later
- **APScheduler** or asyncio loops for bot polling
- **SMTP / email API** for verification codes
- **Pydantic** for data validation
- **Keyring** or encrypted config for secrets

---

# 20. API Design

Even if the UI is desktop, a local API is very useful.

## Auth endpoints
- `POST /auth/register`
- `POST /auth/verify`
- `POST /auth/login`
- `POST /auth/logout`

## Bot endpoints
- `GET /bots`
- `GET /bots/{id}`
- `POST /bots`
- `POST /bots/{id}/start`
- `POST /bots/{id}/stop`
- `POST /bots/{id}/restart`

## Job endpoints
- `GET /jobs`
- `GET /jobs/{id}`
- `POST /jobs`
- `POST /jobs/{id}/cancel`
- `POST /jobs/{id}/retry`

## Message board endpoints
- `GET /board/messages`
- `POST /board/messages`
- `GET /board/topics`

## Config endpoints
- `GET /config`
- `PUT /config`
- `GET /providers`
- `POST /providers`
- `PUT /providers/{id}`
- `GET /parameters`
- `PUT /parameters`

## System endpoints
- `POST /system/start`
- `POST /system/stop`
- `POST /system/reset`
- `GET /system/status`

---

# 21. Orchestrator Logic Design

The orchestrator should run a continuous loop.

## Orchestrator loop
1. read new tasks
2. read new board messages
3. update task states
4. decide actions:
   - assign existing bot
   - create new bot
   - retry failed task
   - escalate to user
5. post updates to board
6. save state to SQL
7. sleep/poll

---

## Orchestrator decision rules
- if task requires skill not currently available → create worker
- if bot idle and qualified → assign task
- if task blocked > X minutes → notify user or reassign
- if job fails too many times → cancel or ask user
- if max bots reached → queue task instead of spawning

---

# 22. Worker Bot Runtime Design

Each worker should also run its own loop.

## Worker loop
1. check message board for assigned tasks
2. load task details
3. load relevant memory
4. execute task using tools
5. save artifacts
6. post progress updates
7. mark completed/failed
8. append to memory.md
9. wait for next assignment

---

# 23. Reset Behavior

You specifically mentioned:

> reset [on click]=<warning message are you sure -yes or no>

## Recommended UX
When user clicks **Reset**:
- show modal:
  - **"Are you sure?"**
  - buttons:
    - Yes
    - No

If **Yes**:
- stop all running bots
- stop active jobs
- clear active task assignments
- optionally clear board
- optionally reset bot states
- write audit log entry

## Best reset modes
### Soft Reset
- stops running work
- clears current active state
- keeps history/memory

### Hard Reset
- clears jobs/messages/state
- optionally wipes memory
- more dangerous

For MVP:
> use **Soft Reset only**

---

# 24. Message Board UI Ideas

This can become one of the most useful screens.

## Board layout
### Left panel
- topics
- bots
- jobs

### Center panel
- live message feed

### Right panel
- selected message details
- payload viewer
- related job
- related bot

## Filters
- by bot
- by job
- by message type
- by time range
- errors only

## Actions
- pause feed
- export JSON
- export Markdown
- pin message
- jump to job

---

# 25. Parameters You Should Brainstorm

Here are good ones to include early.

## Global Parameters
- `max_bots`
- `max_concurrent_jobs`
- `default_model`
- `default_provider`
- `poll_interval_seconds`
- `auto_create_workers`
- `require_approval_for_new_bots`
- `max_retries_per_job`
- `task_timeout_seconds`
- `memory_summary_enabled`
- `log_level`

## Bot Parameters
- `temperature`
- `max_tokens`
- `top_p`
- `tool_timeout`
- `allowed_skills`
- `allowed_tools`
- `memory_limit`
- `workspace_limit`

## Orchestrator Parameters
- `planning_depth`
- `delegation_style`
- `review_required`
- `escalation_threshold`
- `max_spawn_per_cycle`

---

# 26. Provider Panel Ideas

## Provider categories
### LLM providers
- OpenAI
- Anthropic
- OpenRouter
- Ollama
- custom endpoint

### Email providers
- SMTP
- SendGrid
- Mailgun
- Resend

### Search providers
- Brave
- Tavily
- Serper
- Bing

### Storage providers
- local
- S3
- Google Drive

---

## Provider fields
- name
- type
- api_key
- base_url
- default_model
- timeout
- max_rpm
- enabled
- test button

---

# 27. Security Design

Since you have login + bots + tools, security matters.

## Must-have security features
- password hashing:
  - bcrypt or argon2
- 6-digit code:
  - hashed in DB
  - expiry
  - attempt limit
- local API binds only to:
  - `127.0.0.1`
- encrypt secrets:
  - API keys
  - provider credentials
- audit logs:
  - login
  - reset
  - bot creation
  - tool usage

## Recommended extra
- session timeout
- logout
- role-based permissions later
- two-factor authentication later

---

# 28. MVP Roadmap

Here is the smartest way to build it in phases.

---

## Phase 1 — Foundation
Build:
- project structure
- config system
- SQLite database
- local FastAPI service
- basic logging

Deliver:
- app starts locally

---

## Phase 2 — Authentication
Build:
- register
- email code generation
- verify code
- mark verified in SQL
- login
- session handling

Deliver:
- user can register and log in

---

## Phase 3 — Desktop Shell
Build:
- PySide6 login window
- main config window
- minimize to tray
- tray icon
- basic tray menu

Deliver:
- Python desktop UI with tray controls

---

## Phase 4 — Bot Profiles
Build:
- bots table
- bot creation
- bot folders
- `memory.md`
- bot list panel

Deliver:
- static bots can exist and be viewed

---

## Phase 5 — Message Board
Build:
- messages table
- board viewer
- posting API
- filters

Deliver:
- bots and system can coordinate via board

---

## Phase 6 — Jobs
Build:
- jobs table
- task assignment
- status updates
- running list
- history tracking

Deliver:
- system can track work

---

## Phase 7 — Orchestrator
Build:
- planner
- task decomposition
- assignment engine
- simple worker creation logic

Deliver:
- boss agent can delegate work

---

## Phase 8 — Bot Factory
Build:
- dynamic worker creation
- skill/tool assignment
- worker runtime loop

Deliver:
- main agent can spawn new bots on demand

---

## Phase 9 — Skills / Tools
Build:
- skill registry
- tool registry
- permission model
- tool execution logs

Deliver:
- bots can use controlled capabilities

---

## Phase 10 — Full Tray Controls
Build:
- start/stop
- reset confirm
- open board
- open config/providers/parameters
- running list submenu

Deliver:
- full control from tray

---

# 29. Extra Ideas Worth Brainstorming

Since you said "others once we brainstormed them all", here are more controls/features you can add.

## Bot controls
- clone bot
- archive bot
- export bot profile
- import bot profile
- bot templates
- bot tags
- favorite bots

## Job controls
- duplicate job
- schedule job
- repeat job
- priority boost
- assign manually
- convert job into template

## Board controls
- mute bot
- follow job thread
- bookmark message
- annotate message
- search board

## System controls
- safe mode
- read-only mode
- maintenance mode
- export full backup
- restore backup
- system diagnostics

## Observability
- bot health dashboard
- cost tracking
- token usage
- error rate
- average completion time
- bottleneck detection

---

# 30. Important Design Rules

To keep the system stable, follow these rules.

## Rule 1: SQL is truth for system state
Use SQL for:
- users
- bots
- jobs
- messages
- providers

## Rule 2: `memory.md` is bot identity / learned context
Use it for:
- notes
- lessons
- summaries
- history highlights

## Rule 3: Message board is coordination layer
Use it for:
- events
- collaboration
- handoffs
- status updates

## Rule 4: Orchestrator controls spawning
Do not let workers freely spawn unlimited bots unless explicitly allowed.

## Rule 5: Tools must be permissioned
Never give all bots all tools by default.

---

# 31. Best First Version to Build

If you want the fastest useful MVP, build this first:

## MVP Feature Set
- user registration with 6-digit email verification
- login opens Python main window
- minimize to tray
- tray menu with:
  - Open OmniBots
  - Bots List
  - Running List
  - Open Message Board
  - Reset with Yes/No warning
  - Start/Stop
  - Open Config
  - Open Providers
  - Open Parameters
- SQLite backend
- one orchestrator bot
- manual worker creation
- shared message board
- each bot has:
  - profile
  - `memory.md`
  - job history

That alone gives you a strong working prototype.

---

# 32. Recommended Final Architecture Summary

## In one sentence
**OmniBots is a Python desktop-controlled multi-agent system where an orchestrator creates worker bots from skill/tool pools, coordinates them through a shared message board, and stores persistent bot profiles/job history in SQL plus per-bot memory files.**

---

# 33. Suggested Next Step

If you want, I can help you with the next step in one of these ways:

1. **Turn this into a full product requirements document (PRD)**
2. **Design the exact database schema + API routes**
3. **Create the folder structure and starter code**
4. **Design the PySide6 tray + login/main window architecture**
5. **Write the orchestrator + bot factory logic in detail**

If you want, my next reply can be:

> **"OmniBots MVP technical spec + starter architecture"**

and I can lay it out as a build-ready blueprint.

---

## 4. Phase Breakdown

> The phases below decompose the original plan into trackable units. Each phase has sub-phases (e.g., `1.a.01`) that can be marked as ✅ Passed, 🟡 In Progress, ⏳ Not Started, ❌ Failed, or ⚠️ Blocked.
>
> Phases 12.b, 12.c, 13.a, and 13.b are **NEW** (added from follow-up user input about multi-provider quotas and the Boss/Maestro persona).

> **⚠ SCOPE CHANGE — 2026-09-24 (user decision, recorded by Claude)**
> 1. **No login, no registration, no passwords, no email verification.** OmniBots is for the single local user on this PC, same as the Omni agent. Everything auth-related (1.a.01, 1.a.02, 2.b, Phase 3, 4.a, Lock/Logout in 4.d.09) is **DROPPED** — struck through below, kept for history.
> 2. **Providers come from Omni's config, read-only.** OmniBots uses the same providers + keys as Omni (`~/.omni/agent/settings.json` + `~/.omni/.env`). OmniBots must **never write** to Omni files. See new sub-phase **0.c**.
> 3. **One account per provider** — no nim1/nim2 style doubling. Provider set: `minimax.io`, `nvidia`, `agnes`, `xkiro`, `atria`, `openrouter`, `groq`.
> 4. **Build standalone first** in `C:\omnibots` (`python -m omnibots`), test it, **then** plug it into Omni as a plugin with a `/omnibots` command (0.c.04 — blocked until standalone passes). Omni files are not touched until then.
> 5. **Test lineup**: 4 MiniMax agents running concurrently + a 5th multi-provider agent. See new sub-phase **13.c**.

---

### Phase 0 — Project Foundation & Tooling

> Goal: project skeleton exists, dependencies pinned, config + logging wired.

#### 0.a — Repository & Project Layout
- **0.a.01** ⏳ Initialize project root + folder structure — maps to original §18
- **0.a.02** ⏳ Python version pin + dependency lockfile — §19
- **0.a.03** ⏳ `.gitignore`, `LICENSE`, `README.md` scaffolding

**Notes:**
_(empty — agents append dated entries here)_

#### 0.b — Configuration & Logging
- **0.b.01** ⏳ YAML config loader (settings, providers, parameters) — §18, §19
- **0.b.02** ⏳ Logging system (`app.log`, `error.log`) — §18, §27
- **0.b.03** ⏳ Secret encryption (keyring) — §27

**Notes:**
- 2026-09-24 Claude: 0.b.03 scope shrinks — provider API keys are owned by Omni (see 0.c), so OmniBots stores no provider secrets of its own. Keyring only needed if OmniBots later gets secrets Omni doesn't have.

#### 0.c — Omni Config Bridge & Launch (NEW 2026-09-24, Claude)
- **0.c.01** ⏳ Read-only Omni config reader, mirroring Omni's own loader (`~/.omni/src/core/config.mjs`):
  - Omni home = `%OMNI_HOME%` if set, else `<omni install>/agent` (today: `C:\Users\ThePa\.omni\agent`).
  - Read `<home>/settings.json` — strip whole-line `//` comments, then parse JSON. Use `providers` (`baseUrl`, `apiKey`, `reasoningParam`, `accounts`/`activeAccount`) and `models` (`provider`, `id`, `maxTokens`, `free`, `vision`).
  - Read `.env` from `<install>/.env` then `<home>/.env`; a real shell env var is never overridden by a file.
  - Key precedence (Omni's rule): **non-empty key in settings.json wins**; `OMNI_<PROVIDER>_KEY` only fills empty slots. Name rule: uppercase, non-alphanumerics → `_` (`minimax.io` → `OMNI_MINIMAX_IO_KEY`). Aliases: `OMNI_MINIMAX_KEY` → minimax.io, `ATRIA_API_KEY` → atria.
  - Providers with `accounts`: use the `activeAccount` key only (one account per provider — see scope change).
  - **Never write** to any Omni file. Never log or display keys (mask as `set`/`empty`).
- **0.c.02** ⏳ Standalone launch: `python -m omnibots` from `C:\omnibots` opens the main window directly (no login).
- **0.c.03** ⏳ Single-instance guard: a second launch focuses the existing window instead of starting a new copy.
- **0.c.04** ⚠️ Blocked — Omni plugin: `/omnibots` command in Omni launches OmniBots. **Blocked until the standalone app passes** (user: "build it here, test standalone, then plug into Omni").

**Notes:**
- 2026-09-24 Claude: sub-phase added after scope change. Verified live: all 7 target providers exist in Omni settings (`minimax.io`, `nvidia`, `agnes`, `xkiro`, `atria`, `openrouter`, `groq`), all OpenAI-compatible `/chat/completions`. Agnes key is empty in settings.json (comes from `.env`), so the `.env` fallback is required, not optional.
- 2026-09-24 Claude: FYI (Omni file, not changed): `~/.omni/.env.example` says ".env values override settings.json" — Omni's actual code does the opposite (settings.json wins). Follow the code. Logged as Bug 1 in `~/.omni/NEWBUGS_OMNI.md`.
- 2026-09-24 Claude: **0.c.04 patch PREPARED, NOT APPLIED** (stays ⚠️ Blocked until standalone passes + user go).
  - Files: `omni_patch/omni-plugins.patch`, `omni_patch/README.md` (apply/undo steps), `omni_patch/EXTENDING-plugins-section.md` (docs text — Omni's `docs/` is git-ignored so it can't be in the patch), `omni-plugin.json` (OmniBots' own plugin manifest: `/omnibots` → `pythonw -m omnibots`).
  - Side effects: none — built and tested on clones of `~/.omni` in the session scratchpad; real `~/.omni` untouched except the new bug report `~/.omni/NEWBUGS_OMNI.md` (user asked for it).
  - Tests: Omni baseline `node tests/run-all.mjs` → 59 suites 0 failed; patched → 60 suites 0 failed; `git apply --check` on fresh clone → OK; live `omni /plugdemo hello world` launched the program with `OMNI_HOME` + args; `/plugins` listed it.
  - Notes: patch adds a new Omni extension point "plugin" = folder with `omni-plugin.json` whose commands launch a program. Plugin commands never shadow built-ins or skills. Enable later with `"plugins": ["C:/omnibots"]` in `~/.omni/omni.config.json`.
  - 6 Omni bugs found and written up (problem + fix + test) in `~/.omni/NEWBUGS_OMNI.md` for a separate agent. Bug 5 (HIGH) matters to OmniBots: keys set via `/provider apikey` for `nvidia`/`agnes` are lost on Omni restart — if a key "doesn't work" in OmniBots, check that first.

---

### Phase 1 — Database & Schema

> Goal: SQLite schema covers every table in the original §17 plus the new quota tables.

#### 1.a — Core Schema (maps to §17)
- ~~**1.a.01** ⛔ `users` table~~ — ⛔ Dropped 2026-09-24 (no login; single local user)
- ~~**1.a.02** ⛔ `email_verification_codes` table~~ — ⛔ Dropped 2026-09-24 (no registration)
- **1.a.03** ⏳ `bots` table
- **1.a.04** ⏳ `skills` table
- **1.a.05** ⏳ `tools` table
- **1.a.06** ⏳ `bot_skills` + `bot_tools` join tables
- **1.a.07** ⏳ `jobs` table
- **1.a.08** ⏳ `messages` table
- **1.a.09** ⏳ `providers` table — 2026-09-24: holds OmniBots-only state per provider (enabled-for-bots flag, quota numbers, 429 state). Connection info (baseUrl, key, models) is read live from Omni (0.c.01), never copied into SQLite.
- **1.a.10** ⏳ `parameters` table
- **1.a.11** ⏳ `audit_logs` table

**Notes:**
_(empty)_

#### 1.b — Provider Quota Tracking Schema (NEW from user)
- **1.b.01** ⏳ `provider_quotas` table (credit limits, time windows, current usage %)
- **1.b.02** ⏳ `provider_usage_events` table (per-request log for stats)
- **1.b.03** ⏳ `bot_provider_state` table (`multi_provider` flag + failover chain per bot)

**Notes:**
_(empty)_

---

### Phase 2 — Local API Service (FastAPI)

> Goal: `python -m app.main` starts the FastAPI service bound to localhost only.

#### 2.a — Skeleton & Security
- **2.a.01** ⏳ App boots, `/health` endpoint — §20
- **2.a.02** ⏳ Bind to `127.0.0.1` only — §27

**Notes:**
_(empty)_

#### ~~2.b — Auth Endpoints (§20)~~ — ⛔ Dropped 2026-09-24 (no login)
- ~~**2.b.01** ⛔ `POST /auth/register`~~
- ~~**2.b.02** ⛔ `POST /auth/verify`~~
- ~~**2.b.03** ⛔ `POST /auth/login`~~
- ~~**2.b.04** ⛔ `POST /auth/logout`~~

**Notes:**
_(empty)_

#### 2.c — System Endpoints (§20, §23)
- **2.c.01** ⏳ `POST /system/start`, `/system/stop`, `/system/reset`
- **2.c.02** ⏳ `GET /system/status`

**Notes:**
_(empty)_

---

### ~~Phase 3 — Authentication & Email Verification~~ — ⛔ Dropped 2026-09-24

> **⛔ Dropped by user 2026-09-24:** no register, no login, no passwords, no email. OmniBots runs for the local PC user only, like Omni. Sub-phases below are kept for history only — do not implement.
>
> ~~Goal: a fresh user can register, receive a 6-digit code, verify, and log in.~~

#### 3.a — Registration Flow (§4)
- **3.a.01** ⛔ User submits email + password
- **3.a.02** ⛔ Generate 6-digit code, hash, store with expiry + attempt cap
- **3.a.03** ⛔ Send via SMTP / email provider
- **3.a.04** ⛔ User submits code
- **3.a.05** ⛔ Validate + mark verified in SQL

**Notes:**
_(empty)_

#### 3.b — Login Flow (§4)
- **3.b.01** ⛔ Email + password login
- **3.b.02** ⛔ Session token + expiry

**Notes:**
_(empty)_

#### 3.c — Security (§27)
- **3.c.01** ⛔ Password hashing (bcrypt/argon2)
- **3.c.02** ⛔ Code hashing + attempt cap
- **3.c.03** ⛔ Audit log entries on register/verify/login/reset

**Notes:**
_(empty)_

---

### Phase 4 — Desktop Shell (PySide6)

> Goal: ~~a login window opens,~~ the main config window opens directly on launch, and the app minimizes to a working tray icon with a context menu. (Login dropped 2026-09-24.)

#### ~~4.a — Login Window~~ — ⛔ Dropped 2026-09-24
- ~~**4.a.01** ⛔ Login form UI~~

**Notes:**
_(empty)_

#### 4.b — Main Configuration Window (§7)
- **4.b.01** ⏳ Sidebar / tab layout
- **4.b.02** ⏳ All 10 main panels scaffolded (empty)

**Notes:**
_(empty)_

#### 4.c — Tray Icon (§5)
- **4.c.01** ⏳ Tray icon with click handlers
- **4.c.02** ⏳ Minimize-to-tray behavior

**Notes:**
_(empty)_

#### 4.d — Tray Context Menu (§6)
- **4.d.01** ⏳ Top section (Open, Status, Start/Stop)
- **4.d.02** ⏳ Bots submenu
- **4.d.03** ⏳ Running submenu
- **4.d.04** ⏳ Message Board submenu
- **4.d.05** ⏳ Controls (Start/Stop/Pause All)
- **4.d.06** ⏳ Reset (Yes/No confirmation modal) — §23
- **4.d.07** ⏳ Configuration submenu
- **4.d.08** ⏳ Logs submenu
- **4.d.09** ⏳ Exit (stops bots cleanly, then quits). ~~Lock, Logout~~ dropped 2026-09-24 (no login).

**Notes:**
_(empty)_

---

### Phase 5 — Main Configuration Panels (§7)

> Goal: every panel in the original §7 is reachable and renders data from the API.

#### 5.a — Dashboard
- **5.a.01** ⏳ System + orchestrator status, active bots, recent messages, errors

**Notes:**
_(empty)_

#### 5.b — Bots List
> 2026-09-24 cross-ref: each row gets a mini face (16.b.04) and double-click opens that bot's ID-card window (16.a.05).
- **5.b.01** ⏳ Table + start/stop/restart/edit/open-memory/view-history actions

**Notes:**
_(empty)_

#### 5.c — Running List
- **5.c.01** ⏳ Live activity table + cancel/retry/reassign/view-logs

**Notes:**
_(empty)_

#### 5.d — Message Board Viewer (§24)
- **5.d.01** ⏳ 3-pane layout (topics, feed, details)
- **5.d.02** ⏳ Filters (bot, job, message type, time, errors-only)
- **5.d.03** ⏳ Actions (pause, export JSON/MD, pin, jump-to-job)

**Notes:**
_(empty)_

#### 5.e — Config / Providers / Parameters Panels
- **5.e.01** ⏳ Config panel — §7.5
- **5.e.02** ⏳ Providers panel — §26
- **5.e.03** ⏳ Parameters panel — §25

**Notes:**
_(empty)_

#### 5.f — Skills / Tools Manager (§7.8, §14, §15)
- **5.f.01** ⏳ Skill pool management
- **5.f.02** ⏳ Tool pool management

**Notes:**
_(empty)_

#### 5.g — Memory Viewer (§7.9)
- **5.g.01** ⏳ Read/edit memory.md, show summaries + history

**Notes:**
_(empty)_

#### 5.h — Logs / Audit (§7.10)
- **5.h.01** ⏳ Logs, errors, bot actions, tool invocations, login history

**Notes:**
_(empty)_

---

### Phase 6 — Bot Profiles & memory.md

> Goal: a bot profile can be created, edited, and has a folder with memory.md.

#### 6.a — Bot CRUD (§8)
- **6.a.01** ⏳ Create bot profile with all fields
- **6.a.02** ⏳ Bot folder + memory.md template
- **6.a.03** ⏳ View/edit bot profiles

**Notes:**
_(empty)_

#### 6.b — Memory System (§9)
- **6.b.01** ⏳ memory.md template sections (long-term notes, lessons, current task, history)
- **6.b.02** ⏳ Append lessons from completed jobs

**Notes:**
_(empty)_

---

### Phase 7 — Message Board (Coordination Layer)

> Goal: bots can read/write the board, the UI shows a live feed, and topics work.

#### 7.a — Board API (§11, §20)
- **7.a.01** ⏳ `GET` / `POST /board/messages`
- **7.a.02** ⏳ Topics (`#general`, `#tasks`, `#bot/<id>`, `#job/<id>`, `#orchestrator`)
- **7.a.03** ⏳ (2026-09-24: + `USER_STEER` for 16.a.04) Message types (TASK_RECEIVED, TASK_PLANNED, BOT_CREATED, TASK_ASSIGNED, WORK_STARTED, PROGRESS_UPDATE, BLOCKED, ARTIFACT_READY, REVIEW_REQUESTED, TASK_COMPLETED, TASK_FAILED, SYSTEM_RESET)

**Notes:**
_(empty)_

#### 7.b — Polling
- **7.b.01** ⏳ Bots poll every 1-2s (MVP)
- **7.b.02** ⏳ WebSocket / Redis pubsub (post-MVP)

**Notes:**
_(empty)_

---

### Phase 8 — Jobs & Task Tracking (§10)

> Goal: jobs have full lifecycle in SQL and the Running List shows live state.

#### 8.a — Job Lifecycle
- **8.a.01** ⏳ Job states (pending/assigned/running/blocked/review/completed/failed/cancelled)
- **8.a.02** ⏳ Create/assign/cancel/retry endpoints

**Notes:**
_(empty)_

#### 8.b — Job Data Model
- **8.b.01** ⏳ All job fields with indexes

**Notes:**
_(empty)_

---

### Phase 9 — Orchestrator (Boss Agent) (§21, §12)

> Goal: the orchestrator loop runs, decomposes objectives, and dispatches to workers.

#### 9.a — Runtime Loop
- **9.a.01** ⏳ Continuous loop (read tasks → plan → assign → verify)
- **9.a.02** ⏳ Decision rules (spawn vs assign vs escalate)

**Notes:**
_(empty)_

#### 9.b — Planning
- **9.b.01** ⏳ Objective decomposition
- **9.b.02** ⏳ Match existing bot or spawn new

**Notes:**
_(empty)_

#### 9.c — Verification
- **9.c.01** ⏳ Verify worker output before marking complete

**Notes:**
_(empty)_

---

### Phase 10 — Bot Factory (Dynamic Worker Creation) (§13, §22)

> Goal: the orchestrator can spawn a new bot on demand with skills/tools/credentials.

#### 10.a — Creation Flow
- **10.a.01** ⏳ Inputs (objective, skills, tools, provider, permissions, memory template)
- **10.a.02** ⏳ Create bot record + folder + memory.md
- **10.a.03** ⏳ Load skill + tool sets
- **10.a.04** ⏳ Issue bot credentials (bot logs in as itself, posts to message board) — 2026-09-24: no user login exists, so "credentials" = an internal per-bot identity token so the board knows which bot posted. Not a password.

**Notes:**
_(empty)_

#### 10.b — Worker Runtime Loop (§22)
> 2026-09-24 cross-ref: the loop must (a) emit live `console` / `thinking` / `terminal` / `state` events for the bot window (16.a.03, 16.b.05) and (b) check the steer queue before every step (16.a.04).
- **10.b.01** ⏳ Poll board for assigned tasks
- **10.b.02** ⏳ Execute + post progress
- **10.b.03** ⏳ Append to memory.md

**Notes:**
_(empty)_

#### 10.c — Spawn Governor (CRITICAL — safety)
- **10.c.01** ⏳ Hard `max_bots` limit
- **10.c.02** ⏳ `max_spawn_per_cycle` limit
- **10.c.03** ⏳ Cost-per-spawn estimation
- **10.c.04** ⏳ `require_approval_for_new_bots` toggle

**Notes:**
_(empty)_

---

### Phase 11 — Skill & Tool Pools

> Goal: skills and tools are registered, assigned to bots, and tools execute under permission rules.

#### 11.a — Skill Registry (§14)
- **11.a.01** ⏳ Skill object schema
- **11.a.02** ⏳ Assign skills to bots

**Notes:**
_(empty)_

#### 11.b — Tool Registry (§15)
- **11.b.01** ⏳ Tool object schema
- **11.b.02** ⏳ Permission model
- **11.b.03** ⏳ Sandbox rules — §16
- **11.b.04** ⏳ Tool execution logs

**Notes:**
_(empty)_

#### 11.c — Permission Rules (§16)
- **11.c.01** ⏳ File tools limited to workspace
- **11.c.02** ⏳ Shell disabled by default
- **11.c.03** ⏳ Network allowlist
- **11.c.04** ⏳ Force approval for dangerous tools

**Notes:**
_(empty)_

---

### Phase 12 — Provider Management & Multi-Provider Quota System (§26 + NEW)

> Goal: providers are configurable, quota is tracked live in SQL, 429s auto-recover.

#### 12.a — Provider Registry
- **12.a.01** ⏳ LLM / Email / Search / Storage provider types — 2026-09-24: LLM providers only for now, loaded from Omni (0.c.01). Email provider no longer needed (auth dropped).
- **12.a.02** ⏳ Provider CRUD + test-connection — 2026-09-24: Omni owns provider config, so OmniBots **views** providers (masked key status, models) + **test-connection** + enable/disable for bots only. Adding/changing keys is done in Omni (`/apikey`, `/addprovider`) until the plugin phase (0.c.04).

**Notes:**
_(empty)_

#### 12.b — Quota Tracking (NEW from user)
- **12.b.01** ⏳ Track per-provider credit limits + time windows. Initial values to encode:

  | Provider | Credits | Window | Tool Calls | Models |
  |----------|---------|--------|------------|--------|
  | **NVIDIA** | 1500 | 5 hr | 35 | Nemotron 330b or 3.5 Lightning |
  | **Agnes AI** | 1500 | — | 30 | (TBD) |
  | **MiniMax M3** | — | 1 hr | 200 | vision, text, TTS arc1.0, H3 video (full stack) — **NEEDS LIVE VERIFICATION** of 20k+ TPM |
  | **Other small APIs** | — | — | — | xkiro, groq, atria dawn, open router, etc. |

  **2026-09-24 update — one account per provider (Omni provider names):**

  | Omni provider | Base URL | Notes |
  |---------------|----------|-------|
  | `minimax.io` | https://api.minimax.io/v1 | Main — 4 concurrent agents (13.c) |
  | `nvidia` | https://integrate.api.nvidia.com/v1 | Omni has `accounts`; use `activeAccount` only |
  | `agnes` | https://apihub.agnes-ai.com/v1 | Key currently comes from `.env` |
  | `xkiro` | https://api.xkiro.com/v1 | https://xkiro.com/ — free tier 5M tokens/day (per Omni settings comment) |
  | `atria` | https://api.atria-asi.ai/v1 | https://api.atria-asi.ai/console — model `Atria-Dawn-Preview` |
  | `openrouter` | https://openrouter.ai/api/v1 | `:free` models |
  | `groq` | https://api.groq.com/openai/v1 | |

  Quota numbers not in the first table still need to be read from each provider console and entered.

- **12.b.02** ⏳ Per-provider live usage % counter
- **12.b.03** ⏳ Persist every usage event in `provider_usage_events` for stats
- **12.b.04** ⏳ Detect 429 responses, mark provider as exhausted, stop routing work to it
- **12.b.05** ⏳ Auto-recovery: poll until API-key reset window expires + usage returns to 0%, then re-enable

**Notes:**
_(empty)_

#### 12.c — Bot Card Live Provider % (NEW from user)
- **12.c.01** ⏳ UI card on each bot showing live per-provider usage %
- **12.c.02** ⏳ Historical chart from `provider_usage_events`

**Notes:**
_(empty)_

---

### Phase 13 — Maestro / CEO Bot (Orchestrator-as-Bot)

> Goal: the boss persona can orchestrate the dance, spawn workers, route across providers.

#### 13.a — Boss Persona (NEW from user)
- **13.a.01** ⏳ CEO / Boss / Maestro / Orchestrator / Master persona
- **13.a.02** ⏳ Reads user config to determine orchestration style
- **13.a.03** ⏳ Generates bot-creation script based on user input
- **13.a.04** ⏳ MiniMax M3 = main agent + 3 worker sub-agents (vision, text, TTS arc1.0, H3 video)

**Notes:**
_(empty)_

#### 13.b — Multi-Provider Bot (NEW from user)
- **13.b.01** ⏳ `multi_provider = ON` flag on bot profile
- **13.b.02** ⏳ Quota-aware task routing (small jobs → cheap/free APIs, big jobs → MiniMax M3)
- **13.b.03** ⏳ Failover chain when primary 429s
- **13.b.04** ⏳ When ALL small providers exhausted → stop until all reset (per 12.b.05)

**Notes:**
_(empty)_

#### 13.c — Test Agent Lineup (NEW 2026-09-24, Claude)
- **13.c.01** ⏳ 4 MiniMax (`minimax.io`) agents running at the same time: main/orchestrator + 3 workers. Verify live that the account really allows 4 concurrent sessions (ties to 12.b "NEEDS LIVE VERIFICATION").
- **13.c.02** ⏳ 5th agent = multi-provider bot (`multi_provider = ON`) routing small jobs across `nvidia`, `agnes`, `xkiro`, `atria`, `openrouter`, `groq`, with 429 failover (13.b.03).
- **13.c.03** ⏳ End-to-end test: one real objective → orchestrator plans → 4 workers + multi-provider bot coordinate on the board → result + memory.md updates + usage % visible.

**Notes:**
- 2026-09-24 Claude: added from user request ("minimax.io will be used to run the tests cause we can use 4 agents at same time… add a 5th agent using multi-provider").

---

### Phase 14 — Reset & System Controls (§23)

> Goal: Reset always shows Yes/No confirmation; Soft Reset preserves history.

#### 14.a — Reset UX
- **14.a.01** ⏳ Yes/No confirmation modal
- **14.a.02** ⏳ Soft Reset (stops work, keeps history/memory)
- **14.a.03** ⏳ Hard Reset (locked behind extra confirmation)

**Notes:**
_(empty)_

---

### Phase 15 — Observability & Extras (§29)

> Goal: at-a-glance view of cost, health, bottlenecks.

#### 15.a — Cost Tracking
- **15.a.01** ⏳ Per-bot token usage
- **15.a.02** ⏳ Per-provider cost dashboard

**Notes:**
_(empty)_

#### 15.b — Health
- **15.b.01** ⏳ Bot health dashboard
- **15.b.02** ⏳ Bottleneck detection

**Notes:**
_(empty)_

---

### Phase 16 — Bot Window (ID Card) & Live Faces

> NEW 2026-09-24 (user request, recorded by Claude). Goal: every bot feels alive. You can open any working bot from the list and watch it work: its face shows what it's doing and feeling, its console and its thinking stream live in two clean side-by-side sections, and you can type into it to steer it mid-task.
>
> **Layout — "driver's licence" style:**
> ```
> ┌───────────────────────────────────────────────────────────────┐
> │ ┌────────┐  CODER-01                       ● RUNNING          │
> │ │  ◉  ◉  │  ID  bot_001      Role  software_engineer          │
> │ │   ▽    │  Model minimax.io / MiniMax-M3   Usage ▓▓▓░░ 62%   │
> │ │ (@#%$) │  Skills coding, testing     Tools read_file, …     │
> │ └────────┘  Jobs done 14   Since 2026-09-24   Job: task_123    │
> ├───────────────────────────────┬───────────────────────────────┤
> │ CONSOLE                       │ THINKING                      │
> │ $ pytest tests/test_login.py  │ The validator fails on empty  │
> │ 3 passed in 0.41s             │ emails — check the regex…     │
> │ ▸ write_file app/auth.py      │                               │
> ├───────────────────────────────┴───────────────────────────────┤
> │ Steer ▸ [ type to steer the bot while it works…     ] [Send]  │
> └───────────────────────────────────────────────────────────────┘
> ```

#### 16.a — Bot Window
- **16.a.01** ⏳ ID-card header: the bot face in the **top-left corner like a photo ID**. Beside it, aligned fields: name, bot ID, role, status light, provider/model, live usage % (12.c.01), skills, tools, jobs completed, created date, current job.
- **16.a.02** ⏳ Two separate, aligned, equal-width sections under the header, each monospaced with its own scroll (auto-scroll with a pause toggle, copy, clear view):
  - **Console**: output, tool calls (`▸ tool_name args`), and terminal commands the bot runs (`$ cmd` plus output), all in one timeline.
  - **Thinking**: chain-of-thought / reasoning stream, kept separate from console output.
- **16.a.03** ⏳ Live feed plumbing: the worker runtime streams `console`, `terminal`, `thinking` and `state` events into the window. Events are also persisted so you can reopen a window and see recent history.
- **16.a.04** ⏳ Steer input at the bottom: the text goes to the bot as a `USER_STEER` message on `#bot/<id>` (7.a.03), and the bot reads it before its next step, like Omni's `/btw`. It's shown in the Console as `▸ you: …`. It's logged to the audit trail.
- **16.a.05** ⏳ Open any bot's window from Bots List (5.b), Running List (5.c), the tray Bots submenu (4.d.02) or its face on the dashboard. Several bot windows can be open at once. Closing a window never stops the bot.

**Notes:**
- 2026-09-24 Claude: added from user requests. Layout: "structure the bot windows like a driver's licence, bot face in the left top corner like a photo ID". Sections: "console and thinking text outputs in 2 different sections… well aligned and clean". Also: console output, terminal cmd, chain of thoughts, and a steer input.
- Open question: models that don't expose reasoning will show nothing in Thinking. Show "(this model does not share its thinking)" instead of an empty box.

#### 16.b — Live Bot Faces & Emotes
- **16.b.01** ⏳ Little robot face per bot, drawn in code with Qt `QPainter` (no image files). Each bot gets its own look (color, antenna, eye shape), derived from its bot ID so it stays the same forever.
- **16.b.02** ⏳ Expressions per state: idle, thinking, working/typing, running a tool, waiting, blocked (confused), error (dizzy), completed (happy), stopped (sleeping, "z z"), rate-limited/429 (tired + small clock).
- **16.b.03** ⏳ **Thinking bubble**: a small bubble beside the face showing a series of **4 symbols** that cycles while the bot thinks, e.g. `@#%$` → `@$#%` → `^%&$` → `$%#%`. It stays exactly 4 characters so it fits the small bubble.
- **16.b.04** ⏳ Animation loop on a `QTimer` (blink, bobbing, bubble cycling). It must stay cheap: animate only visible faces, about 10 fps. The same face appears full-size in the ID card and as a mini version in Bots List rows and the dashboard.
- **16.b.05** ⏳ Face state is driven by real events: runtime state and board messages (`WORK_STARTED`, `BLOCKED`, `TASK_COMPLETED`, `TASK_FAILED`, 429s from 12.b.04). It never just guesses.

**Notes:**
- 2026-09-24 Claude: added from user request ("each bot will have its own little animation face… emoticons to show what he's doing and feeling… thinking bubble with symbols").

---

## 5. Notes Log

> Append dated entries here ONLY for cross-phase changes (e.g., "Phase 3 added a `password_hash` column that broke Phase 6"). For per-phase implementation notes, write them inside that phase's Notes section.

- 2026-09-24 Claude: **Scope change (user decision)** — auth removed entirely; single local user. Dropped: 1.a.01, 1.a.02, 2.b.*, Phase 3, 4.a, Lock/Logout in 4.d.09. Affected: 0.b.03 (keyring shrinks), 1.a.09 (providers table = OmniBots-only state), 10.a.04 (bot token, not password), 12.a (LLM providers from Omni, view + test only).
- 2026-09-24 Claude: **Omni config bridge** — providers/keys/models read-only from `~/.omni/agent/settings.json` + `.env` (new 0.c). One account per provider. Omni plugin (`/omnibots`) deferred until standalone passes (0.c.04 ⚠️ Blocked).
- 2026-09-24 Claude: **Test lineup** — 4 concurrent MiniMax agents + 1 multi-provider agent (new 13.c).
- 2026-09-24 Claude: **Legend** — added `⛔ Dropped` status to `AGENTS.md` §3 and this file §1 (user approved); auth items re-marked ⛔.
- 2026-09-24 Claude: **Omni plugin patch prepared (not applied)** — `omni_patch/`; see 0.c Notes. Omni bugs → `~/.omni/NEWBUGS_OMNI.md`.
- 2026-09-24 Claude: **New Phase 16 (bot window + faces)** — affects 5.b (open window from list), 7.a.03 (new `USER_STEER` message type), 10.b (worker loop streams console/thinking events + reads steer queue).
