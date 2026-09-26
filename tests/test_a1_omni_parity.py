"""A1: the Omni bridge reads exactly what Omni reads (parity with Omni's own
loader, run through Node), for synthetic edge cases and for the real ~/.omni."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from omni_helpers import NODE, REAL_OMNI, clean_env, make_fake_omni, node_dump, node_parse_frontmatter
from omnibots.lineup import TARGET_PROVIDERS
from omnibots.omni.config import key_fingerprint, load_omni_config
from omnibots.omni.frontmatter import parse_frontmatter
from omnibots.omni.locate import OmniLocation, locate_omni
from omnibots.omni.parity import parity_dump

needs_node = pytest.mark.skipif(not NODE or not (REAL_OMNI / "src").is_dir(), reason="needs node + Omni source")
needs_real_omni = pytest.mark.skipif(not (REAL_OMNI / "agent" / "settings.json").is_file(), reason="needs a real ~/.omni")

FRONTMATTER_CASES = [
    "---\nname: a\ndescription: plain\n---\nbody",
    "---\r\nname: crlf\r\ndescription: windows line endings\r\n---\r\nbody\r\n",
    "---\nname: block\ndescription: |\n  line one\n  line two\ncommand: /blk\n---\nB",
    "---\nname: folded\ndescription: >-\n  folded\n\n  text\n---\n",
    "---\nname: cont\ndescription:\n  continued on\n  next lines\n---\nx",
    "---\nname: 'it''s'\ndescription: \"say \\\"hi\\\"\\nnew\"\n---\n",
    "no frontmatter at all",
    "---\nname: only\n---",
    "---\r\r\nname: stray\r\r\ndescription: doubled CR\r\r\n---\r\r\nbody",
    "---\nname: tail\ndescription: ends with newline\n---\nbody\n",
]


@needs_node
def test_frontmatter_matches_omni():
    ours = [dict(zip(("meta", "body"), parse_frontmatter(t))) for t in FRONTMATTER_CASES]
    assert ours == node_parse_frontmatter(FRONTMATTER_CASES)


# ── synthetic Omni install: every key-precedence rule ─────────────────────
SETTINGS = {
    "defaultProvider": "p_disk",
    "defaultModel": "minimax/M3",
    "providers": {
        "p_disk":     {"baseUrl": "https://disk.example/v1", "apiKey": "disk-key-1111111111"},
        "p_env":      {"baseUrl": "https://env.example/v1", "apiKey": ""},
        "p_realenv":  {"baseUrl": "https://realenv.example/v1", "apiKey": ""},
        "atria":      {"baseUrl": "https://api.atria-asi.ai/v1", "apiKey": "", "reasoningParam": "none"},
        "minimax.io": {"baseUrl": "https://api.minimax.io/v1", "apiKey": ""},
        "minimax":    {"baseUrl": "https://api.minimax.io/v1", "apiKey": "legacy-provider-key-5"},
        "nvidia":     {"baseUrl": "https://integrate.api.nvidia.com/v1", "apiKey": "api-key-nv-7777777",
                       "accounts": {"nvidia1": "", "nvidia2": "acct-two-6666666"}, "activeAccount": "nvidia2",
                       "reasoningParam": "effort"},
        "agnes":      {"baseUrl": "https://apihub.agnes-ai.com/v1", "apiKey": "",
                       "accounts": {"agnes1": "", "agnes2": ""}, "activeAccount": "agnes2"},
        "keyless":    {"baseUrl": "http://localhost:11434/v1", "apiKey": "not-needed"},
        "broken":     {"baseUrl": "sk-this-is-a-key-not-a-url", "apiKey": ""},
        "groq":       {"baseUrl": "gsk_pasted-into-the-url-slot-9999", "apiKey": ""},   # Omni repairs from its defaults
    },
    "models": {
        "minimax/M3": {"provider": "minimax", "id": "MiniMax-M3", "maxTokens": 977000},
        "openai/gpt-4o": {"provider": "p_disk", "id": "gpt-4o", "maxTokens": 4096},
        "p_env/small": {"provider": "p_env", "id": "small-1", "maxTokens": 2048, "free": True},
    },
}
DOTENV = "\n".join([
    "# comment",
    "OMNI_P_DISK_KEY=env-should-lose-to-settings",
    "OMNI_P_ENV_KEY=from-dotenv-2222222",
    'OMNI_P_REALENV_KEY="dotenv-loses-to-real-env"',
    "OMNI_AGNES_KEY2=agnes-two-8888888",
    "OMNI_HOME=C:/should/be/ignored",
    "",
])
REAL_ENV = {"OMNI_P_REALENV_KEY": "real-env-wins-3333333", "ATRIA_API_KEY": "atria-alias-4444444",
            "OMNI_MINIMAX_KEY": "legacy-env-alias-loses"}


@pytest.fixture
def fake_omni(tmp_path) -> Path:
    root = make_fake_omni(tmp_path) if NODE else tmp_path / "fake-omni"
    (root / "agent").mkdir(parents=True, exist_ok=True)
    (root / "agent" / "settings.json").write_text(
        "// Omni tolerates whole-line comments\n" + json.dumps(SETTINGS, indent=2), encoding="utf-8")
    (root / ".env").write_text(DOTENV, encoding="utf-8")
    skills = root / "skills"
    for name, cmd in (("alpha", None), ("beta", "/beta-cmd")):
        (skills / name).mkdir(parents=True)
        head = f"---\r\nname: {name}\r\n" + (f"command: {cmd}\r\n" if cmd else "") + f"description: the {name} skill\r\n---\r\n"
        (skills / name / "SKILL.md").write_bytes((head + "Body\r\n").encode("utf-8"))  # exact bytes
    ext = tmp_path / "ext-skills"
    for name in ("gamma", "alpha"):              # "alpha" collides with bundled: bundled wins
        (ext / name).mkdir(parents=True)
        (ext / name / "SKILL.md").write_text(f"---\nname: {name}\ndescription: external {name}\n---\nx", encoding="utf-8")
    index = {"entries": [{"name": "gamma", "path": str(ext / "gamma")}, {"name": "alpha", "path": str(ext / "alpha")}],
             "nested": [{"name": "gamma", "path": str(ext / "gamma"), "parentPack": "p"}]}
    (tmp_path / "skills.json").write_text(json.dumps(index), encoding="utf-8")
    (root / "omni.config.json").write_text(json.dumps({
        "autoDiscoverSkills": True, "skillIndex": str(tmp_path / "skills.json"),
        "mcpServers": {"okf": {"command": "node", "args": ["{{INSTALL_ROOT}}/packages/okf/server.mjs"]}},
    }), encoding="utf-8")
    return root


def load_fake(root: Path):
    return load_omni_config(OmniLocation(root.resolve(), (root / "agent").resolve()), real_env=clean_env(**REAL_ENV))


def test_key_precedence_rules(fake_omni):
    cfg = load_fake(fake_omni)
    p = cfg.providers
    assert p["p_disk"].api_key == "disk-key-1111111111" and p["p_disk"].key_source == "settings"
    assert p["p_env"].api_key == "from-dotenv-2222222" and p["p_env"].key_source == "env"
    assert p["p_realenv"].api_key == "real-env-wins-3333333"          # real env beats .env
    assert p["atria"].api_key == "atria-alias-4444444"                # vendor alias
    assert p["minimax.io"].api_key == "legacy-provider-key-5"         # legacy provider folded in, beats env alias
    assert "minimax" not in p
    assert p["nvidia"].api_key == "acct-two-6666666" and p["nvidia"].key_source == "account:nvidia2"
    assert p["nvidia"].native_tools is False and p["nvidia"].reasoning_param is None
    assert p["agnes"].api_key == "agnes-two-8888888"                  # OMNI_AGNES_KEY2 alias -> agnes2
    assert p["keyless"].key_source == "keyless" and p["keyless"].has_key
    assert p["broken"].error and "invalid baseUrl" in p["broken"].error
    assert p["groq"].base_url == "https://api.groq.com/openai/v1" and p["groq"].api_key == "gsk_pasted-into-the-url-slot-9999"
    assert "xkiro" in p and p["xkiro"].base_url.startswith("https://")   # only in Omni's defaults
    m = cfg.models
    assert "minimax/M3" not in m and m["minimax.io/M3"]["provider"] == "minimax.io"
    assert m["minimax.io/M3"]["maxTokens"] == 128000                  # MiniMax output cap repaired
    assert m["openai/gpt-4o"]["vision"] is True
    cmds = {s.command: s for s in cfg.skills}
    assert set(cmds) == {"/alpha", "/beta-cmd", "/gamma"}
    assert cmds["/alpha"].source == "bundled" and cmds["/gamma"].source == "external"
    assert cmds["/alpha"].description == "the alpha skill"           # CRLF frontmatter parsed
    assert cfg.mcp_servers["okf"]["args"] == [f"{fake_omni.resolve()}/packages/okf/server.mjs"]


@needs_node
def test_synthetic_install_matches_omnis_own_loader(fake_omni):
    ours = parity_dump(load_fake(fake_omni))
    theirs = node_dump(fake_omni, clean_env(**REAL_ENV))
    for section in ("providers", "models", "skills", "mcp_servers"):
        diff = {k: (theirs[section].get(k), ours[section].get(k))
                for k in set(ours[section]) | set(theirs[section]) if ours[section].get(k) != theirs[section].get(k)}
        assert not diff, f"{section} differ (omni, ours): {diff}"
    assert ours["default_model"] == theirs["default_model"]


@needs_node
def test_defaults_snapshot_matches_installed_omni():
    """omni_defaults.json must match Omni's DEFAULT_SETTINGS; regenerate it after an Omni update."""
    import subprocess
    from omni_helpers import ROOT
    r = subprocess.run([NODE, str(ROOT / "tools" / "omni_defaults_snapshot.mjs"), str(REAL_OMNI), "--check"],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr


# ── the real ~/.omni ──────────────────────────────────────────────────────
def _snapshot(root: Path) -> dict[str, str]:
    files = [root / "agent" / "settings.json", root / "omni.config.json", root / ".env", root / "agent" / ".env"]
    return {str(f): hashlib.sha256(f.read_bytes()).hexdigest() for f in files if f.exists()}


@needs_real_omni
@needs_node
def test_real_omni_full_parity_and_untouched():
    before = _snapshot(REAL_OMNI)
    loc = locate_omni()
    ours = parity_dump(load_omni_config(loc, real_env=clean_env()))
    theirs = node_dump(loc.install_root, clean_env())
    assert ours == theirs
    assert _snapshot(REAL_OMNI) == before, "Omni files changed!"


@needs_real_omni
def test_real_omni_has_the_lineup_with_keys():
    cfg = load_omni_config(locate_omni(), real_env=clean_env())
    for name in TARGET_PROVIDERS:
        assert name in cfg.providers, f"{name} missing from Omni"
        assert cfg.providers[name].has_key, f"{name} has no key in Omni"
        assert cfg.providers[name].error is None
    assert cfg.providers["nvidia"].native_tools is False
    assert not cfg.errors
