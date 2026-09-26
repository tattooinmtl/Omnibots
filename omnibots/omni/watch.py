"""Hot-reload Omni's config when its files change (PLAN.md A1.a.06).

Polls size + mtime every couple of seconds instead of using OS file events:
Omni writes settings.json atomically (write temp file, rename), which event
watchers often miss or report twice. Polling a handful of files costs nothing.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Awaitable, Callable

from omnibots.omni.config import OmniConfig, load_omni_config
from omnibots.omni.locate import OmniLocation

log = logging.getLogger(__name__)


def watched_files(loc: OmniLocation, cfg: OmniConfig | None = None) -> list[Path]:
    files = [loc.settings_file, loc.project_config_file, *loc.env_files]
    try:
        import json

        index = json.loads(loc.project_config_file.read_text(encoding="utf-8")).get("skillIndex")
        if isinstance(index, str) and index.strip():
            files.append(Path(index.strip()))
    except (OSError, ValueError):
        pass
    return files


def _stamp(files: list[Path]) -> tuple:
    out = []
    for f in files:
        try:
            st = f.stat()
            out.append((str(f), st.st_size, st.st_mtime_ns))
        except OSError:
            out.append((str(f), None, None))
    return tuple(out)


async def watch_omni(
    loc: OmniLocation,
    on_change: Callable[[OmniConfig], Awaitable[None] | None],
    *,
    interval: float = 2.0,
    debounce: float = 0.5,
) -> None:
    files = watched_files(loc)
    last = _stamp(files)
    while True:
        await asyncio.sleep(interval)
        now = _stamp(files)
        if now == last:
            continue
        await asyncio.sleep(debounce)          # let a multi-step save finish
        files = watched_files(loc)
        last = _stamp(files)
        try:
            cfg = await asyncio.to_thread(load_omni_config, loc)
        except Exception:
            log.exception("reloading Omni config failed; keeping the previous one")
            continue
        log.info("Omni config changed; reloaded (%d providers, %d skills)", len(cfg.providers), len(cfg.skills))
        result = on_change(cfg)
        if asyncio.iscoroutine(result):
            await result
