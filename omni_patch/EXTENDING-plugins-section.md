## Plugins (commands that launch programs)

Skills only inject instructions and extensions only add model-callable tools.
A **plugin** adds a slash command that starts a separate program — for
example `/omnibots` opening a Python desktop app. A plugin is a folder with an
`omni-plugin.json` manifest:

```json
{
  "name": "omnibots",
  "version": "0.1.0",
  "description": "Multi-agent bot board (PySide6 desktop app).",
  "commands": [{
    "name": "omnibots",
    "usage": "/omnibots",
    "summary": "open the OmniBots control window",
    "run": {
      "command": "pythonw",
      "args": ["-m", "omnibots"],
      "cwd": "{{PLUGIN_DIR}}",
      "env": { "OMNIBOTS_LAUNCHED_BY": "omni" },
      "wait": false
    }
  }]
}
```

- **Where plugins load from**: every `plugins/<name>/omni-plugin.json` under
  the install root (auto-discovered), plus every folder listed in
  `omni.config.json` -> `"plugins": ["C:/omnibots"]` (absolute, or relative to
  the install root). The second form lets a plugin stay in its own project.
- **`run`**: `command` is required; `args`, `cwd` (default: the plugin
  folder) and `env` are optional. Words typed after the command are appended
  to `args`. `wait: false` (default) starts the program detached and returns
  at once — right for GUI apps. `wait: true` runs it in the terminal and
  reports the exit code.
- **Placeholders** in `command`/`args`/`cwd`/`env`: `{{INSTALL_ROOT}}`,
  `{{OMNI_HOME}}`, `{{PLUGIN_DIR}}`, `{{CWD}}`. The program always receives
  `OMNI_HOME`, `OMNI_INSTALL_ROOT` and `OMNI_PLUGIN_DIR` in its environment, so
  it can read Omni's `settings.json` / `.env` for providers and keys.
- **No shadowing**: a plugin command whose name is already a built-in command
  or a skill is skipped and reported in the startup warnings and `/plugins`.
- `/plugins` lists loaded plugins, their commands, and any load errors.
  `omni /omnibots` works one-shot too (no model key needed).
- **Trust**: a plugin runs a program with your permissions — the same trust
  level as an extension. Only add folders you trust.

Also add this row to the extension-point table at the top of the file, and
change "three extension points" to "four":

| **Plugin** | A `/slash-command` that launches an external program (e.g. a desktop app) | `plugins/<name>/omni-plugin.json`, or a folder listed under `plugins` in `omni.config.json` | yes |
