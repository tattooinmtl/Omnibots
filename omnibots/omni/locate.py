"""Find Omni's install root and home folder.

Mirrors Omni (src/core/config.mjs, 3.5.5+):
  home = %OMNI_HOME% (a real environment variable only; Omni ignores it in
         .env files) or <install root>/agent
Install root, in order:
  1. OMNI_INSTALL_ROOT (set by Omni when it launches a plugin; Phase B)
  2. settings.toml [omni] install_root
  3. ~/.omni
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


class OmniNotFound(RuntimeError):
    pass


@dataclass(frozen=True)
class OmniLocation:
    install_root: Path
    home: Path

    @property
    def settings_file(self) -> Path:
        return self.home / "settings.json"

    @property
    def project_config_file(self) -> Path:
        return self.install_root / "omni.config.json"

    @property
    def env_files(self) -> list[Path]:
        # Same order as Omni's loadDotEnv: install root first, then home.
        return [self.install_root / ".env", self.home / ".env"]


def locate_omni(configured_root: str = "") -> OmniLocation:
    candidates = [
        os.environ.get("OMNI_INSTALL_ROOT", "").strip(),
        (configured_root or "").strip(),
        str(Path.home() / ".omni"),
    ]
    tried = []
    for c in candidates:
        if not c:
            continue
        root = Path(c).expanduser()
        tried.append(str(root))
        if (root / "omni.config.json").is_file() or (root / "bin" / "omni.mjs").is_file():
            env_home = os.environ.get("OMNI_HOME", "").strip()
            home = Path(env_home).expanduser() if env_home else root / "agent"
            return OmniLocation(install_root=root.resolve(), home=home.resolve())
    raise OmniNotFound(
        "Omni was not found (looked in: " + ", ".join(tried) + "). "
        "Install Omni, or set [omni] install_root in ~/.omnibots/settings.toml."
    )
