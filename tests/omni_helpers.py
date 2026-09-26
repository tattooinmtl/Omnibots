"""Helpers for A1 tests: run Omni's own code (Node) to compare against."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REAL_OMNI = Path.home() / ".omni"
NODE = shutil.which("node")


def clean_env(**extra: str) -> dict[str, str]:
    """The current environment minus every Omni key/home variable, plus extras."""
    env = {k: v for k, v in os.environ.items()
           if not (k.startswith("OMNI_") or k in ("ATRIA_API_KEY",))}
    env.update(extra)
    return env


def node_dump(install_root: Path, env: dict[str, str]) -> dict:
    r = subprocess.run(
        [NODE, str(ROOT / "tools" / "omni_parity_dump.mjs"), str(install_root)],
        env=env, capture_output=True, text=True, encoding="utf-8", timeout=120,
    )
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def node_parse_frontmatter(texts: list[str]) -> list[dict]:
    script = (
        "import { parseFrontmatter } from " + json.dumps((REAL_OMNI / "src/core/frontmatter.mjs").as_uri()) + ";"
        "let d='';process.stdin.on('data',c=>d+=c).on('end',()=>{"
        "const out=JSON.parse(d).map(t=>{const {meta,body}=parseFrontmatter(t);return {meta,body};});"
        "process.stdout.write(JSON.stringify(out));});"
    )
    r = subprocess.run([NODE, "--input-type=module", "-e", script], input=json.dumps(texts),
                       capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def make_fake_omni(tmp: Path) -> Path:
    """A throwaway Omni install: Omni's real source code + synthetic config.

    Copying src/ means Omni's own loader runs against our fixture files and
    never sees the real ~/.omni settings or .env.
    """
    root = tmp / "fake-omni"
    shutil.copytree(REAL_OMNI / "src", root / "src")
    shutil.copy2(REAL_OMNI / "package.json", root / "package.json")
    (root / "agent").mkdir()
    return root
