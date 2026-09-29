# The OmniBots doctor

The doctor checks everything OmniBots and Omni need to work together, sets up what's missing, and keeps it that way.
Its reference is one file, [`omnibots/doctor/layout.json`](../omnibots/doctor/layout.json): every folder, file,
dependency, environment variable and old location it knows about. To change what "a good install" means, change
that file (and `tests/test_standalone_doctor.py::test_the_layout_file_is_complete`).

## Ways to run it

| How | What it does |
|---|---|
| Every app start | The quick part, quietly: folders, `settings.toml`, the provider config. Problems go to `logs/app.log` |
| **Settings → Doctor → Run doctor** | Everything. Tick *Test provider keys online* to try each key with its server |
| Ask Omi ("call the doctor", "check my providers") | Omi's `call_doctor` tool runs the same full check and tells you what it found |
| `python -m omnibots.doctor` | From a terminal. `--no-fix` only checks, `--online` tests keys, `--install-deps` pip-installs missing packages, `--only providers omni`, `--json`, `--all` |
| The installer (`install.ps1`) | Runs it once at the end of every install or update |

The last report is saved to `%USERPROFILE%\.omnibots\logs\doctor.json`. The CLI exits with 1 when a check fails.

## Where things live (the only places)

| Folder | What's there |
|---|---|
| `%USERPROFILE%\.omnibots` | OmniBots: code, `.venv`, `settings.toml`, `db\omnibots.sqlite`, `bots\`, `projects\`, `sandbox\`, `profiles\`, `logs\`, `skills\`, `sessions\`, `backups\`, and `config\` when Omni isn't installed |
| `%USERPROFILE%\.omni` | Omni. Its `agent\settings.json` (and `.env`) hold the providers and keys **both** apps use |
| `%LOCALAPPDATA%\OmniBots` | Only Qt's cache (`cache\qtpipelinecache-…`). The window starts slower without it |
| The projects folder (`[output] folder`, e.g. `C:\omnibots_output`) | The bots' projects, one folder per goal |

Anything that looks like OmniBots' or Omni's config somewhere else (an old `C:\omnibots` copy with settings or a
database, an old `%LOCALAPPDATA%\OmniBots` install, a loose `%USERPROFILE%\.env`) is **reported, never deleted**.
Move what you need into the folders above, then delete the old copy yourself.

## Providers and keys

**Omni installed** (the usual setup): Omni's `settings.json` is the shared provider list. Settings → Providers edits
it (add, edit, remove), and Omni sees the change the next time it loads. The doctor only *reads* Omni's files: it
checks they exist and parse, and that Omni loads the OmniBots launcher (`/omnibots`). Omni's own `/doctor` looks
after Omni.

**Omni not installed**: OmniBots makes its own copy of Omni's layout in `%USERPROFILE%\.omnibots\config`:

| File | Format | Holds |
|---|---|---|
| `settings.json` | Omni's `settings.json` | `defaultProvider`, `defaultModel`, `providers` (URLs, labels, accounts), `models`. **No keys** |
| `.env` | Omni's `.env` | Nothing by default. A key typed here is moved into the store by the doctor and its line commented out |
| `omni.config.json` | Omni's project config | `skills`, `mcpServers` |

The keys live in the `provider_keys` table of `db\omnibots.sqlite`, each one encrypted with Windows DPAPI for your
Windows user on this PC. A copy of the files, or of the database on another PC or account, can't be read. Keys you
type in Settings → Providers go straight there. A real environment variable (`OMNI_<NAME>_KEY`) still works.
Install Omni later and OmniBots goes back to Omni's settings; `config\` is kept but not used (add your keys in Omni).

If `[omni] install_root` in `settings.toml` (or `OMNI_INSTALL_ROOT`) names a folder that isn't an Omni install,
that's an error to fix, not "no Omni": OmniBots won't quietly switch to its own config.

## What each group checks

| Group | Checks | Repairs |
|---|---|---|
| `python` | Python 3.11+, PySide6, keyring, httpx, pydantic, playwright, git on PATH | pip-installs missing packages with `--install-deps` |
| `folders` | Every folder in the table above; the projects folder can be written to | Creates missing folders |
| `settings` | `settings.toml` reads; no key-like values in it | Creates it with the defaults if missing (never rewrites yours) |
| `database` | Exists, schema is current, `PRAGMA quick_check` | Creates it; upgrades an old one after a backup to `backups\` |
| `code` | `omnibots\__main__.py`, `.venv\Scripts\python.exe` | — (run the install line) |
| `omni` | Omni found → its files, JSON, the launcher listed. No Omni → the `config\` files, no keys as text, stored keys readable | Creates the `config\` files; moves text keys into the encrypted store |
| `providers` | At least one provider has a key, the default one does, no provider errors. `--online`: `GET <baseUrl>/models` with the key | — (Settings → Providers) |
| `env` | `OMNIBOTS_HOME`, `OMNIBOTS_DIR`, `OMNI_HOME` point where they should | — |
| `legacy` | Old copies outside the folders above | — (reported only) |

## Rules

- It creates and upgrades; it never deletes a file or folder.
- It never writes to Omni's folder.
- A key is never printed, logged, saved in the report or passed to Omi. Only "has a key" and where it comes from.
