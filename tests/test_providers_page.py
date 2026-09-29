"""Settings → Providers writes Omni's settings.json and nothing else (A11.p.01)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from PySide6.QtWidgets import QLabel, QLineEdit

from qt_helpers import qapp

from omnibots.omni.providers_store import (
    ProviderDocumentError, apply_provider, clear_provider_key, delete_provider, list_providers,
)
from omnibots.ui.providers_page import ProvidersPage
from omnibots.ui.settings_window import SettingsWindow

qapp()

SECRET = "sk-test-provider-key-1234567890"
OTHER = "sk-other-provider-key-0987654321"
ENV_LEAK = "sk-env-value-must-not-be-written"


def _write(path: Path, doc: dict) -> None:
    path.write_text(json.dumps(doc, indent=2, ensure_ascii=False), encoding="utf-8", newline="\n")


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _sample(path: Path) -> None:
    _write(path, {
        "theme": "dark",
        "defaultModel": "openai/gpt",
        "models": {"openai/gpt": {"id": "gpt", "provider": "openai", "maxTokens": 100}},
        "providers": {
            "examplebox": {
                "baseUrl": "https://api.example.test/v1",
                "apiKey": SECRET,
                "label": "Example Box",
                "nativeTools": True,
                "maxToolIterations": 40,
            },
            "nvidia": {
                "baseUrl": "https://integrate.api.nvidia.com/v1",
                "apiKey": "nk-account-one-zzzzzzzz",
                "label": "NVIDIA NIM",
                "nativeTools": False,
                "activeAccount": "nvidia1",
                "accounts": {
                    "nvidia1": "nk-account-one-zzzzzzzz",
                    "nvidia2": "nk-account-two-yyyyyyyy",
                    "nvidia3": "",
                },
            },
            "openai": {"baseUrl": "https://api.openai.com/v1", "apiKey": OTHER, "label": "OpenAI"},
        },
    })


def _rows(path: Path):
    return {row.name: row for row in list_providers(path, env_files=[], real_env={})}


def test_edit_keeps_the_key_and_every_other_setting(tmp_path):
    path = tmp_path / "settings.json"
    _sample(path)
    apply_provider(path, name="examplebox", base_url="https://api.example.test/v2", label="Example Box",
                   native_tools=True, key_action="keep", api_key=ENV_LEAK)
    doc = _read(path)
    box = doc["providers"]["examplebox"]
    assert box["apiKey"] == SECRET and box["baseUrl"] == "https://api.example.test/v2"
    assert box["maxToolIterations"] == 40
    assert doc["theme"] == "dark" and doc["models"]["openai/gpt"]["maxTokens"] == 100
    assert doc["providers"]["openai"]["apiKey"] == OTHER
    assert ENV_LEAK not in path.read_text(encoding="utf-8")
    assert not (tmp_path / "settings.json.omnibots-tmp").exists()


def test_accounts_follow_omnis_rules(tmp_path):
    path = tmp_path / "settings.json"
    _sample(path)
    apply_provider(path, name="nvidia", base_url="https://integrate.api.nvidia.com/v1", label="NVIDIA NIM",
                   native_tools=False, key_action="set", api_key="nk-new-key-123456789", active_account="nvidia1")
    nv = _read(path)["providers"]["nvidia"]
    assert nv["apiKey"] == "nk-new-key-123456789"
    assert nv["accounts"]["nvidia1"] == "nk-new-key-123456789"
    assert nv["accounts"]["nvidia2"] == "nk-account-two-yyyyyyyy"

    apply_provider(path, name="nvidia", base_url=nv["baseUrl"], label="NVIDIA NIM", native_tools=False,
                   key_action="keep", active_account="nvidia2")
    nv = _read(path)["providers"]["nvidia"]
    assert nv["activeAccount"] == "nvidia2" and nv["apiKey"] == "nk-account-two-yyyyyyyy"
    assert nv["accounts"]["nvidia1"] == "nk-new-key-123456789"

    apply_provider(path, name="nvidia", base_url=nv["baseUrl"], label="NVIDIA NIM", native_tools=False,
                   key_action="keep", active_account="nvidia3")
    nv = _read(path)["providers"]["nvidia"]
    assert nv["activeAccount"] == "nvidia3"
    assert nv["accounts"]["nvidia3"] == "nk-account-two-yyyyyyyy"   # empty account adopts the provider key
    assert nv["accounts"]["nvidia2"] == "nk-account-two-yyyyyyyy"

    clear_provider_key(path, "nvidia")
    nv = _read(path)["providers"]["nvidia"]
    assert nv["apiKey"] == "" and nv["accounts"]["nvidia3"] == ""
    assert nv["accounts"]["nvidia1"] == "nk-new-key-123456789"
    assert nv["nativeTools"] is False


def test_add_remove_and_reset_builtin(tmp_path):
    path = tmp_path / "settings.json"
    _sample(path)
    apply_provider(path, name="mybox", base_url="http://127.0.0.1:11434/v1", label="My Box",
                   native_tools=True, key_action="keyless")
    doc = _read(path)
    assert doc["providers"]["mybox"]["apiKey"] == "not-needed"
    assert set(doc["providers"]) == {"examplebox", "nvidia", "openai", "mybox"}

    assert delete_provider(path, "mybox") == "removed"
    assert "mybox" not in _read(path)["providers"]

    assert delete_provider(path, "openai") == "reset"
    assert "openai" not in _read(path)["providers"]
    rows = _rows(path)
    assert "openai" in rows and rows["openai"].disk_has_key is False and rows["openai"].builtin

    bare = tmp_path / "bare.json"
    _write(bare, {"theme": "dark"})
    before = bare.read_bytes()
    assert delete_provider(bare, "openai") == "reset"
    assert bare.read_bytes() == before


def test_a_key_pasted_into_the_url_is_kept_when_the_url_is_fixed(tmp_path):
    path = tmp_path / "settings.json"
    _write(path, {"providers": {"examplebox": {"baseUrl": SECRET, "apiKey": ""}}})
    apply_provider(path, name="examplebox", base_url="https://api.example.test/v1", key_action="keep")
    box = _read(path)["providers"]["examplebox"]
    assert box["baseUrl"] == "https://api.example.test/v1" and box["apiKey"] == SECRET


def test_bad_json_and_bad_url_do_not_touch_the_file(tmp_path):
    path = tmp_path / "settings.json"
    raw = '{\n  "providers": { "examplebox": { "apiKey": "' + SECRET + '" }\n'
    path.write_text(raw, encoding="utf-8")
    with pytest.raises(ProviderDocumentError) as bad:
        apply_provider(path, name="examplebox", base_url="https://api.example.test/v1", key_action="set", api_key="new")
    assert SECRET not in str(bad.value)
    assert path.read_text(encoding="utf-8") == raw

    _sample(path)
    before = path.read_bytes()
    with pytest.raises(ProviderDocumentError):
        apply_provider(path, name="examplebox", base_url="not a url", key_action="set", api_key="nk-should-not-land")
    assert path.read_bytes() == before
    with pytest.raises(ProviderDocumentError):
        apply_provider(path, name="has space", base_url="https://api.example.test/v1", key_action="keyless")
    assert path.read_bytes() == before


def test_the_page_edits_the_shared_file_and_hides_the_key(tmp_path):
    path = tmp_path / "settings.json"
    _sample(path)

    class Engine:
        def __init__(self):
            self.reloaded = 0

        async def reload_omni(self):
            self.reloaded += 1

        def submit(self, coro):
            import asyncio
            asyncio.run(coro)

    engine = Engine()
    page = ProvidersPage(path, engine, autoload=True)
    page.confirm = lambda title, body: True
    texts = " ".join(label.text() for label in page.findChildren(QLabel))
    typed = " ".join(box.text() for box in page.findChildren(QLineEdit))
    listed = " ".join(page.list.item(i).text() for i in range(page.list.count()))
    assert SECRET not in texts and SECRET not in typed and SECRET not in listed
    assert page.key.text() == ""

    page.begin_add()
    page.name.setText("localbox")
    page.label_edit.setText("Local")
    page.url.setText("http://127.0.0.1:11434/v1")
    page.key.setText("sk-localbox-key-123456789")
    page.save()
    assert engine.reloaded == 1
    saved = _read(path)["providers"]["localbox"]
    assert saved["baseUrl"] == "http://127.0.0.1:11434/v1"
    assert saved["apiKey"] == "sk-localbox-key-123456789"
    assert page.key.text() == ""

    page.filter.setText("examplebox")
    page._select("examplebox")
    assert SECRET not in page.source.text() and "7890" in page.source.text()
    page.url.setText("https://api.example.test/v9")
    page.key.clear()
    page.save()
    box = _read(path)["providers"]["examplebox"]
    assert box["baseUrl"] == "https://api.example.test/v9" and box["apiKey"] == SECRET

    page.remove_current()
    assert "examplebox" not in _read(path)["providers"]
    assert _read(path)["theme"] == "dark"

    window = SettingsWindow(omni_settings=path)
    assert window.tabs.count() == 4                      # Folders, Bots && icons, Providers, Doctor (A16.d.02)
    assert window.tabs.tabText(2) == "Providers"
    plain = SettingsWindow()
    assert plain.tabs.tabText(2) == "Providers"
    assert plain.providers._loaded is False
