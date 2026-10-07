# OmniBots

A local team of AI bots, **the Guild**. A boss bot plans your goal, lends out MiniMax seats, relays work to specialist bots, checks each result against its evidence, and asks you before anything risky. You watch it all from a desktop app with a tray icon.

**Status:** early development.

## Install

**The Windows installer:** `OmniBots-Setup-1.0-<version>.exe` from https://omnibots.globalwarningnetworks.com. It carries
the code itself (a git bundle), runs the same steps as the line below, and shows them as it goes. Silent install:
`OmniBots-Setup.exe --dir D:\OmniBots --auto` (also `--no-shortcut`, `--no-browser`; the log is
`%TEMP%\OmniBots-Setup.log`). Build it with `python installer/build.py`.

Or in **PowerShell** (Windows 10/11):

```powershell
irm https://raw.githubusercontent.com/tattooinmtl/Omnibots/master/install.ps1 | iex
```

It checks for Python 3.12+ and Git (offering to install them with winget), downloads OmniBots into
`%USERPROFILE%\.omnibots` (next to your bots' data, which git ignores, the same way Omni lives in `~\.omni`) with its
own Python environment, installs the bots' browser, and adds **OmniBots** to the Start menu. The folder is a full git
clone, so you can also work on OmniBots from it. Run the same line again to update. Options: `-InstallDir`, `-NoShortcut`, `-NoBrowser`, `-Yes`
(see the top of `install.ps1`). The website: https://omnibots.globalwarningnetworks.com

## Run (from a clone)

```bash
pip install -r requirements.lock
python -m omnibots
```

| Command | What it does |
|---------|--------------|
| `python -m omnibots` | Start the app, or bring the running one to the front |
| `python -m omnibots --send status` | Ask the running app for its status (JSON) |
| `python -m omnibots --send stop` | Close the running app cleanly |
| `python -m omnibots --no-window --exit-after 5` | Headless start and clean exit (used by tests) |

## What the bots can do

Besides planning, coding, testing and deploying as a team: read PDF/Word/Excel/PowerPoint; watch videos; take a camera
picture (`[camera]` in settings.toml, every picture asks); make pictures, picture edits, slides, documents and PDFs;
research with cited sources; personalities and moods (Layout menu), keep-on-top windows and the team's mind (3D);
Omi's own goal ideas (they start only when you accept) and a daily journal; a live preview beside the editor; MCP
servers local or over HTTP (`~/.omnibots/mcp.json`, plus Omni's and OmniOne's), including Blender; GitHub repos,
issues and pull requests (`github_token` in the vault); read-only PC checks with fixes you approve; ESP32, Pico and
Arduino boards; and the team in Telegram or Discord (`telegram_bot_token` / `discord_bot_token` in the vault, then pair
your chat; nothing listens on your PC). The feature-by-feature comparison is on the website (`compare.html`).

## Where data lives

Everything is in `~/.omnibots` (override with `OMNIBOTS_HOME`): the database (`db/omnibots.sqlite`), bot folders, projects, sandbox, browser profiles, logs and `settings.toml`. Besides that, only `%LOCALAPPDATA%\OmniBots` (Qt's window cache) and the projects folder you pick on first start.

## Providers and keys

- **With [Omni](https://omni.globalwarningnetworks.com)** (install it first): both apps share one provider list, Omni's `~/.omni/agent/settings.json`. Add or edit providers in Omni, or in OmniBots under **Settings → Providers**, which changes only the provider entries in that file.
- **Without Omni**: OmniBots keeps its own list in `~/.omnibots/config` (Omni's `settings.json`, `.env` and `omni.config.json` formats, with no keys in them). Keys are stored in OmniBots' database, encrypted with Windows DPAPI for your Windows user on this PC, never in a text file.
- Keys never go in `settings.toml`, the logs, the board or a bot's memory.

## The doctor

The doctor checks everything OmniBots and Omni need, sets up what's missing and keeps it that way. Its reference is `omnibots/doctor/layout.json`: folders, settings, the database, Python packages, the provider config and keys, Omni's files (read-only), environment variables, and old copies left in other places. It creates and upgrades (the database after a backup), never deletes, never writes to `~/.omni`, and never shows a key.

| How | |
|---|---|
| Every start | The quick part, quietly (folders, settings, the provider config) |
| **Settings → Doctor → Run doctor** | Everything; tick *Test provider keys online* to try each key |
| Ask Omi: "call the doctor" | Omi's `call_doctor` tool |
| `python -m omnibots.doctor` | `--no-fix`, `--online`, `--install-deps`, `--only <group>`, `--json`, `--all` |
| The installer | Once, at the end |

The last report is saved to `~/.omnibots/logs/doctor.json`.

## Tests

```bash
python -m pytest
```

Tests run the real app (real processes, the named pipe, crashes) against a throwaway home folder and a private pipe name, so they never touch your data or a running OmniBots.
