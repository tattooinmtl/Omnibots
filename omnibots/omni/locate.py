"""Find Omni's install root and home folder.

Mirrors Omni (src/core/config.mjs, 3.5.5+):
  home = %OMNI_HOME% (a real environment variable only; Omni ignores it in
         .env files) or <install root>/agent
Install root, in order:
  1. OMNI_INSTALL_ROOT (set by Omni when it launches a plugin; Phase B)
  2. settings.toml [omni] install_root
  3. ~/.omni

When none of those is an Omni install, OmniBots runs on its own config
(PLAN.md A1.s.01): `<omnibots home>/config`, the same shape as Omni's home
(settings.json, .env, omni.config.json), with the keys encrypted in OmniBots'
database. `resolve_location()` picks one or the other; the doctor creates the
standalone folder when it's needed.
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
    standalone: bool = False            # True: OmniBots' own config, not an Omni install
    db_file: Path | None = None         # standalone only: where the encrypted keys are

    @property
    def keys_in_db(self) -> bool:
        return self.standalone and self.db_file is not None

    @property
    def settings_file(self) -> Path:
        return self.home / "settings.json"

    @property
    def project_config_file(self) -> Path:
        return self.install_root / "omni.config.json"

    @property
    def env_files(self) -> list[Path]:
        # Same order as Omni's loadDotEnv: install root first, then home.
        files = [self.install_root / ".env", self.home / ".env"]
        return files[:1] if files[0] == files[1] else files


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


STANDALONE_DIR = "config"


def standalone_location(omnibots_home: Path) -> OmniLocation:
    folder = (Path(omnibots_home) / STANDALONE_DIR).resolve()
    return OmniLocation(install_root=folder, home=folder, standalone=True,
                        db_file=(Path(omnibots_home) / "db" / "omnibots.sqlite").resolve())


def resolve_location(configured_root: str = "", omnibots_home: Path | None = None,
                     *, create: bool = True) -> OmniLocation:
    """Omni when it's installed, otherwise OmniBots' own Omni-shaped config.

    With `create`, a missing standalone folder is made (settings.json, .env,
    omni.config.json; no keys). Omni's folders are never created or changed here.
    """
    try:
        return locate_omni(configured_root)
    except OmniNotFound:
        if (configured_root or "").strip() or os.environ.get("OMNI_INSTALL_ROOT", "").strip():
            raise                         # an Omni folder was named on purpose: a wrong one is an error, not "no Omni"
        if omnibots_home is None:
            from omnibots.paths import resolve_home
            omnibots_home = resolve_home()
        loc = standalone_location(omnibots_home)
        if create:
            from omnibots.omni.standalone import ensure_standalone
            ensure_standalone(loc)
        return loc
