from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

import watson.config as config_module
from watson.config import ConfigStore, CredentialStoreError


class FakeCredentialBackend:
    def __init__(self) -> None:
        self.passwords: dict[tuple[str, str], str] = {}
        self.calls: list[tuple[str, str, str]] = []

    def get_password(self, service: str, username: str) -> str | None:
        self.calls.append(("get", service, username))
        return self.passwords.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.calls.append(("set", service, username))
        self.passwords[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        self.calls.append(("delete", service, username))
        self.passwords.pop((service, username), None)


@pytest.fixture
def credential_backend(monkeypatch: pytest.MonkeyPatch) -> FakeCredentialBackend:
    backend = FakeCredentialBackend()
    monkeypatch.setattr(config_module, "_credential_backend", lambda: backend)
    return backend


def test_native_backend_cannot_be_redirected_to_plaintext(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PYTHON_KEYRING_BACKEND", "keyring.backends.null.Keyring")

    backend = config_module._credential_backend()

    if sys.platform == "darwin":
        assert backend.__class__.__module__ == "keyring.backends.macOS"
    elif os.name == "nt":
        assert backend.__class__.__name__ == "WinVaultKeyring"
    else:
        assert backend.__class__.__module__ == "keyring.backends.SecretService"


def test_status_checks_never_access_credential_backend(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = ConfigStore(tmp_path)
    store.write_config({"gemini_api_key_storage": "keychain"})
    monkeypatch.setattr(
        config_module,
        "_credential_backend",
        lambda: (_ for _ in ()).throw(AssertionError("unexpected Keychain access")),
    )

    state = store.get_credential_state()

    assert state.has_saved_key
    assert not state.session_loaded


def test_explicit_save_uses_keychain_and_keeps_secret_off_disk(
    tmp_path: Path,
    credential_backend: FakeCredentialBackend,
) -> None:
    store = ConfigStore(tmp_path / "application data")

    store.save_api_key_to_keychain("super-secret")

    assert [call[0] for call in credential_backend.calls] == ["set"]
    assert store.get_session_api_key() == "super-secret"
    assert store.get_credential_state().has_saved_key
    assert "super-secret" not in store.config_path.read_text(encoding="utf-8")
    if os.name != "nt":
        assert store.config_path.stat().st_mode & 0o077 == 0


def test_saved_key_requires_explicit_load_in_a_new_session(
    tmp_path: Path,
    credential_backend: FakeCredentialBackend,
) -> None:
    store = ConfigStore(tmp_path)
    store.save_api_key_to_keychain("load-me")
    store.forget_session_api_key()
    credential_backend.calls.clear()

    assert store.get_session_api_key() is None
    assert store.get_credential_state().has_saved_key
    assert credential_backend.calls == []

    assert store.load_api_key_from_keychain() == "load-me"
    assert [call[0] for call in credential_backend.calls] == ["get"]
    assert store.get_session_api_key() == "load-me"


def test_explicit_delete_removes_keychain_item_and_session_copy(
    tmp_path: Path,
    credential_backend: FakeCredentialBackend,
) -> None:
    store = ConfigStore(tmp_path)
    store.save_api_key_to_keychain("delete-me")
    credential_backend.calls.clear()

    store.delete_api_key_from_keychain()

    assert [call[0] for call in credential_backend.calls] == ["delete"]
    assert store.get_session_api_key() is None
    assert store.get_credential_state().persistent == "none"


def test_legacy_vault_is_only_migrated_after_explicit_action(
    tmp_path: Path,
    credential_backend: FakeCredentialBackend,
) -> None:
    store = ConfigStore(tmp_path)
    store.ensure_state_dir()
    key = Fernet.generate_key()
    encrypted = Fernet(key).encrypt(b"legacy-secret").decode("ascii")
    store.vault_key_path.write_bytes(key)
    store.vault_path.write_text(
        json.dumps({"version": 1, "gemini_api_key": encrypted}),
        encoding="utf-8",
    )
    store.write_config({"gemini_api_key_storage": "app_vault", "model": "test"})

    state = store.get_credential_state()

    assert state.has_legacy_key
    assert credential_backend.calls == []
    assert store.get_session_api_key() is None

    store.migrate_legacy_api_key_to_keychain()

    assert [call[0] for call in credential_backend.calls] == ["set"]
    assert store.get_session_api_key() == "legacy-secret"
    assert not store.vault_path.exists()
    assert not store.vault_key_path.exists()
    assert "legacy-secret" not in store.config_path.read_text(encoding="utf-8")


def test_legacy_plaintext_key_is_not_read_until_explicit_migration(
    tmp_path: Path,
    credential_backend: FakeCredentialBackend,
) -> None:
    store = ConfigStore(tmp_path)
    store.write_config({"gemini_api_key": "legacy-secret", "model": "test"})

    assert store.get_credential_state().has_legacy_key
    assert store.get_session_api_key() is None
    assert credential_backend.calls == []

    store.migrate_legacy_api_key_to_keychain()

    assert [call[0] for call in credential_backend.calls] == ["set"]
    assert "legacy-secret" not in store.config_path.read_text(encoding="utf-8")


def test_missing_saved_key_reports_reconnect_error(
    tmp_path: Path,
    credential_backend: FakeCredentialBackend,
) -> None:
    store = ConfigStore(tmp_path)
    store.write_config({"gemini_api_key_storage": "keychain"})

    with pytest.raises(CredentialStoreError, match="reconnect"):
        store.load_api_key_from_keychain()
