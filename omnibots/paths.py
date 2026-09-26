"""Where OmniBots keeps its data (ADR-6).

Home = %OMNIBOTS_HOME% if set, otherwise ~/.omnibots. The app code can move
without losing data, and tests point OMNIBOTS_HOME at a temp folder.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

SUBDIRS = ("db", "bots", "projects", "sandbox", "profiles", "logs", "skills")


def resolve_home() -> Path:
    env = os.environ.get("OMNIBOTS_HOME", "").strip()
    return Path(env).expanduser().resolve() if env else (Path.home() / ".omnibots").resolve()


@dataclass(frozen=True)
class Paths:
    home: Path

    @property
    def db_file(self) -> Path:
        return self.home / "db" / "omnibots.sqlite"

    @property
    def settings_file(self) -> Path:
        return self.home / "settings.toml"

    @property
    def logs(self) -> Path:
        return self.home / "logs"

    @property
    def lock_file(self) -> Path:
        return self.home / "omnibots.lock"

    def dir(self, name: str) -> Path:
        return self.home / name

    def ensure(self) -> "Paths":
        for sub in SUBDIRS:
            (self.home / sub).mkdir(parents=True, exist_ok=True)
        return self


def get_paths() -> Paths:
    return Paths(resolve_home()).ensure()
