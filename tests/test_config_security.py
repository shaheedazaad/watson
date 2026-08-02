from __future__ import annotations

import json
import os
from pathlib import Path

from watson.config import ConfigStore


def test_api_key_is_encrypted_in_app_vault_and_round_trips(tmp_path: Path) -> None:
    store = ConfigStore(tmp_path / "application data")

    storage = store.save_api_key("super-secret")
    reopened = ConfigStore(tmp_path / "application data")

    assert storage == "app_vault"
    assert reopened.get_api_key() == "super-secret"
    assert reopened.get_api_key_storage() == "app_vault"
    for path in (store.config_path, store.vault_path, store.vault_key_path):
        assert "super-secret" not in path.read_text(encoding="utf-8")
    if os.name != "nt":
        assert store.vault_path.stat().st_mode & 0o077 == 0
        assert store.vault_key_path.stat().st_mode & 0o077 == 0


def test_legacy_plaintext_key_is_migrated_to_encrypted_vault(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    tmp_path.mkdir(exist_ok=True)
    path.write_text(json.dumps({"gemini_api_key": "legacy-secret", "model": "test"}), encoding="utf-8")
    store = ConfigStore(tmp_path)

    assert store.get_api_key() == "legacy-secret"
    assert store.get_api_key_storage() == "app_vault"
    assert "legacy-secret" not in path.read_text(encoding="utf-8")
    assert "legacy-secret" not in store.vault_path.read_text(encoding="utf-8")


def test_deleting_api_key_removes_vault_and_key(tmp_path: Path) -> None:
    store = ConfigStore(tmp_path)
    store.save_api_key("delete-me")

    store.delete_api_key()

    assert store.get_api_key() is None
    assert not store.vault_path.exists()
    assert not store.vault_key_path.exists()
