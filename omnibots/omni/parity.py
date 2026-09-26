"""The Python side of the parity check: the same shape tools/omni_parity_dump.mjs
prints from Omni's own loader. Keys appear only as fingerprints.

  python -m omnibots.omni.parity        prints the dump as JSON
"""

from __future__ import annotations

import json
import sys
from typing import Any

from omnibots.omni.config import OmniConfig, key_fingerprint, load_omni_config
from omnibots.omni.locate import locate_omni


def parity_dump(cfg: OmniConfig) -> dict[str, Any]:
    providers = {}
    for name, p in cfg.providers.items():
        base = p.raw.get("baseUrl", p.base_url)
        providers[name] = {
            "base_url": str(base or ""),
            "key_hash": "keyless" if p.api_key == "not-needed" else key_fingerprint(p.api_key),
            "native_tools": p.native_tools,
            "reasoning_param": p.reasoning_param,
            "active_account": p.active_account,
        }
    models = {
        k: {"provider": m.get("provider"), "id": m.get("id"), "maxTokens": m.get("maxTokens"),
            "vision": m.get("vision") is True, "free": m.get("free") is True}
        for k, m in cfg.models.items()
    }
    skills = {s.command: {"source": s.source, "description": s.description} for s in cfg.skills}
    mcp = {n: {"command": c.get("command"), "url": c.get("url")} for n, c in cfg.mcp_servers.items()}
    return {"providers": providers, "models": models, "skills": skills, "mcp_servers": mcp, "default_model": cfg.default_model}


if __name__ == "__main__":
    loc = locate_omni(sys.argv[1] if len(sys.argv) > 1 else "")
    json.dump(parity_dump(load_omni_config(loc)), sys.stdout)
