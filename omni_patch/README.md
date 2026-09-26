# Omni plugin-support patch (NOT APPLIED)

Prepared 2026-09-24 by Claude. Now tracked as **`PLAN.md` B0/B1**. **Do not apply until `PLAN.md` A14.99 is ✅** and the user says go.

> ⚠️ 2026-09-25: this patch **no longer applies** to Omni 3.5.6. It conflicts in `src/cli/commands.mjs` because `printHelp` now renders every category. Rebase it first (`PLAN.md` B0.a.01). `docs/EXTENDING.md` is tracked in Omni now, so the docs section can go into the patch itself.

## What it adds to Omni

A fourth extension point: **plugins** — a folder with an `omni-plugin.json` whose commands launch an external program. This is what lets `/omnibots` open the OmniBots desktop app. Skills (instructions only) and extensions (model tools only) can't do this.

| File | Change |
|------|--------|
| `src/integrations/plugins.mjs` | **new** — discover, validate, build launch spec, spawn (detached or wait) |
| `src/cli/commands.mjs` | `registerCommand()` for runtime commands; `/plugins` command; "Plugins" section in `/help` |
| `src/cli/main.mjs` | load plugins at startup, register their commands (never shadowing built-ins or skills), one-shot `omni /omnibots` works with no model key |
| `schema/omni.config.schema.json` | new `"plugins": [ ... ]` key |
| `tests/plugins.test.mjs` | **new** — 12 tests |
| `docs/EXTENDING.md` | **not in the patch** — `docs/` is git-ignored in Omni. Paste `EXTENDING-plugins-section.md` above `## MCP servers` by hand. |

The manifest format is documented at the top of `plugins.mjs` and in `EXTENDING-plugins-section.md`. OmniBots' own manifest is `C:\omnibots\omni-plugin.json`.

## Verified (2026-09-24, on clones, never on the real `~/.omni`)

- Baseline Omni at `ae47212`: `node tests/run-all.mjs` → 59 suites, 0 failed.
- With the patch: 60 suites, 0 failed (`plugins.test.mjs` 12/12, `all-commands.test.mjs` 30/30).
- `git apply --check` passes on a fresh clone of `~/.omni`.
- Live: `omni /plugdemo hello world` launched the demo program with `OMNI_HOME` set and the args appended; `/plugins` in the REPL listed it.

## How to apply (when ready)

```bash
cd ~/.omni
git switch -c feat/plugins
git apply --check C:/omnibots/omni_patch/omni-plugins.patch
git apply C:/omnibots/omni_patch/omni-plugins.patch
node tests/run-all.mjs
```

Then tell Omni where OmniBots lives — add to `~/.omni/omni.config.json`:

```json
"plugins": ["C:/omnibots"]
```

Restart Omni, run `/plugins` (should list `omnibots`), then `/omnibots`.

Note for Git Bash users: to test one-shot mode from Git Bash, prefix with `MSYS_NO_PATHCONV=1`, otherwise Git Bash rewrites `/omnibots` into a file path before Omni sees it. PowerShell/cmd and the Omni REPL are unaffected.

## How to undo

```bash
cd ~/.omni
git apply -R C:/omnibots/omni_patch/omni-plugins.patch
```

and remove the `"plugins"` key from `omni.config.json`.

## If Omni changed since `ae47212`

If `git apply --check` fails, the patch is small enough to redo by hand: the new file `plugins.mjs` is standalone; the edits to `commands.mjs` / `main.mjs` are about 50 lines total and are easiest to read from the `.patch` file.
