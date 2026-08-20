from __future__ import annotations

import json
import os
import secrets
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from cryptography.fernet import Fernet, InvalidToken
from typer import prompt


SERVICE_NAME = "watson"
# This becomes the human-visible item name in macOS permission dialogs.
KEYCHAIN_SERVICE_NAME = "Watson Gemini API"
KEYCHAIN_ACCOUNT_NAME = "API key"
CONFIG_FILENAME = "config.json"
VAULT_FILENAME = "credentials.vault"
VAULT_KEY_FILENAME = ".credential-key"
APP_CONFIG_DIR_ENV = "WATSON_CONFIG_DIR"
DEFAULT_THINKING_LEVEL = "high"
THINKING_LEVEL_OPTIONS = ("minimal", "low", "medium", "high")
_SESSION_SECRETS: dict[str, str] = {}


class CredentialStoreError(RuntimeError):
    """A user-initiated system credential-store operation failed."""


class _CredentialBackend(Protocol):
    def get_password(self, service: str, username: str) -> str | None: ...

    def set_password(self, service: str, username: str, password: str) -> None: ...

    def delete_password(self, service: str, username: str) -> None: ...


@dataclass(frozen=True)
class CredentialState:
    persistent: str
    session_loaded: bool

    @property
    def has_saved_key(self) -> bool:
        return self.persistent == "keychain"

    @property
    def has_legacy_key(self) -> bool:
        return self.persistent == "legacy_vault"


def get_app_state_dir() -> Path:
    override = os.environ.get(APP_CONFIG_DIR_ENV)
    if override:
        return Path(override).expanduser().resolve()
    if sys.platform == "darwin":
        return (Path.home() / "Library" / "Application Support" / SERVICE_NAME).resolve()
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        return (base / "Watson").resolve()
    xdg_config_home = os.environ.get("XDG_CONFIG_HOME")
    if xdg_config_home:
        return (Path(xdg_config_home).expanduser() / SERVICE_NAME).resolve()
    return (Path.home() / ".config" / SERVICE_NAME).resolve()


def system_credential_store_name() -> str:
    if sys.platform == "darwin":
        return "macOS Keychain"
    if os.name == "nt":
        return "Windows Credential Manager"
    return "Secret Service keyring"


class ConfigStore:
    def __init__(self, state_dir: Path, console: object | None = None) -> None:
        self.state_dir = state_dir.expanduser().resolve()
        self.config_path = self.state_dir / CONFIG_FILENAME
        self.vault_path = self.state_dir / VAULT_FILENAME
        self.vault_key_path = self.state_dir / VAULT_KEY_FILENAME
        self.console = console

    def ensure_state_dir(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.state_dir, 0o700)
        except OSError:
            pass

    def read_config(self) -> dict[str, Any]:
        """Read non-secret metadata without accessing the system credential store."""
        if not self.config_path.exists():
            return {}
        try:
            return json.loads(self.config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def write_config(self, config: dict[str, Any]) -> None:
        self.ensure_state_dir()
        _write_private(
            self.config_path,
            (json.dumps(config, indent=2, sort_keys=True) + "\n").encode("utf-8"),
        )

    def get_credential_state(self) -> CredentialState:
        """Return local metadata only; this method never queries the Keychain."""
        config = self.read_config()
        storage = config.get("gemini_api_key_storage")
        if storage == "keychain":
            persistent = "keychain"
        elif self._legacy_credential_available(config):
            persistent = "legacy_vault"
        else:
            persistent = "none"
        return CredentialState(
            persistent=persistent,
            session_loaded=self.get_session_api_key() is not None,
        )

    def get_session_api_key(self) -> str | None:
        """Read only the process-memory copy; never access the Keychain."""
        return _SESSION_SECRETS.get(self._session_key())

    def save_api_key_to_keychain(self, api_key: str) -> None:
        """Persist a key after an explicit user save action and cache it for this run."""
        api_key = api_key.strip()
        if not api_key:
            raise ValueError("API key cannot be empty.")
        try:
            _credential_backend().set_password(
                KEYCHAIN_SERVICE_NAME,
                KEYCHAIN_ACCOUNT_NAME,
                api_key,
            )
        except Exception as exc:
            raise CredentialStoreError(
                f"{system_credential_store_name()} could not save the API key: {exc}"
            ) from exc
        config = self.read_config()
        config.pop("gemini_api_key", None)
        config["gemini_api_key_storage"] = "keychain"
        self.write_config(config)
        self._remove_legacy_vault()
        _SESSION_SECRETS[self._session_key()] = api_key

    def load_api_key_from_keychain(self) -> str:
        """Load a key only after an explicit user load action."""
        try:
            api_key = _credential_backend().get_password(
                KEYCHAIN_SERVICE_NAME,
                KEYCHAIN_ACCOUNT_NAME,
            )
        except Exception as exc:
            raise CredentialStoreError(
                f"{system_credential_store_name()} could not load the API key: {exc}"
            ) from exc
        if not api_key:
            raise CredentialStoreError(
                f"No Watson API key was found in {system_credential_store_name()}. "
                "Save it again to reconnect this installation."
            )
        _SESSION_SECRETS[self._session_key()] = api_key
        return api_key

    def forget_session_api_key(self) -> None:
        """Discard the in-memory key without accessing the Keychain."""
        _SESSION_SECRETS.pop(self._session_key(), None)

    def delete_api_key_from_keychain(self) -> None:
        """Delete the persisted key after an explicit user delete action."""
        try:
            _credential_backend().delete_password(
                KEYCHAIN_SERVICE_NAME,
                KEYCHAIN_ACCOUNT_NAME,
            )
        except Exception as exc:
            # Keyring uses PasswordDeleteError when the item is already absent.
            if exc.__class__.__name__ != "PasswordDeleteError":
                raise CredentialStoreError(
                    f"{system_credential_store_name()} could not delete the API key: {exc}"
                ) from exc
        self.forget_session_api_key()
        self._remove_credential_metadata_and_legacy_files()

    def migrate_legacy_api_key_to_keychain(self) -> None:
        """Move an old file-vault key after an explicit user migration action."""
        config = self.read_config()
        api_key = config.get("gemini_api_key")
        if not isinstance(api_key, str) or not api_key:
            api_key = self._read_legacy_vault()
        if not api_key:
            raise CredentialStoreError("The legacy credential could not be read. Save the API key again instead.")
        self.save_api_key_to_keychain(api_key)

    def delete_legacy_api_key(self) -> None:
        """Remove old file-based credentials without accessing the system store."""
        config = self.read_config()
        config.pop("gemini_api_key", None)
        config["gemini_api_key_storage"] = "none"
        self.write_config(config)
        self._remove_legacy_vault()

    def prompt_for_api_key(self) -> str:
        return prompt("Enter Gemini API key", hide_input=True).strip()

    def get_default_model(self, fallback: str) -> str:
        config = self.read_config()
        value = config.get("model")
        return value if isinstance(value, str) and value else fallback

    def set_default_model(self, model: str) -> None:
        normalized = model.strip()
        if not normalized or len(normalized) > 200 or any(ord(character) < 32 for character in normalized):
            raise ValueError("Model must be a non-empty identifier of at most 200 characters.")
        config = self.read_config()
        config["model"] = normalized
        self.write_config(config)

    def get_thinking_level(self, fallback: str = DEFAULT_THINKING_LEVEL) -> str:
        config = self.read_config()
        value = config.get("thinking_level")
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in THINKING_LEVEL_OPTIONS:
                return normalized
        return fallback

    def set_thinking_level(self, thinking_level: str) -> None:
        normalized = thinking_level.strip().lower()
        if normalized not in THINKING_LEVEL_OPTIONS:
            raise ValueError(
                f"Thinking level must be one of: {', '.join(THINKING_LEVEL_OPTIONS)}"
            )
        config = self.read_config()
        config["thinking_level"] = normalized
        self.write_config(config)

    def get_last_root(self, fallback: Path) -> Path:
        config = self.read_config()
        value = config.get("last_root")
        if isinstance(value, str) and value:
            path = Path(value).expanduser().resolve()
            if path.exists() and path.is_dir():
                return path
        return fallback.resolve()

    def set_last_root(self, root: Path) -> None:
        config = self.read_config()
        config["last_root"] = str(root.resolve())
        self.write_config(config)

    def _legacy_credential_available(self, config: dict[str, Any]) -> bool:
        plaintext = config.get("gemini_api_key")
        return bool(isinstance(plaintext, str) and plaintext) or (
            self.vault_path.is_file() and self.vault_key_path.is_file()
        )

    def _read_legacy_vault(self) -> str | None:
        if (
            not self.vault_path.is_file()
            or self.vault_path.is_symlink()
            or not self.vault_key_path.is_file()
            or self.vault_key_path.is_symlink()
        ):
            return None
        try:
            raw = json.loads(self.vault_path.read_text(encoding="utf-8"))
            encrypted = raw.get("gemini_api_key")
            if not isinstance(encrypted, str) or not encrypted:
                return None
            fernet = Fernet(self.vault_key_path.read_bytes())
            return fernet.decrypt(encrypted.encode("ascii")).decode("utf-8")
        except (OSError, ValueError, KeyError, json.JSONDecodeError, InvalidToken):
            return None

    def _remove_legacy_vault(self) -> None:
        for path in (self.vault_path, self.vault_key_path):
            try:
                path.unlink()
            except FileNotFoundError:
                pass

    def _remove_credential_metadata_and_legacy_files(self) -> None:
        config = self.read_config()
        config.pop("gemini_api_key", None)
        config["gemini_api_key_storage"] = "none"
        self.write_config(config)
        self._remove_legacy_vault()

    def _session_key(self) -> str:
        return str(self.state_dir)


def _credential_backend() -> _CredentialBackend:
    # Importing lazily makes it impossible for page rendering or app startup to
    # initialize a credential backend. Only explicit credential actions call here.
    # Selecting the native backend directly also prevents environment or config
    # overrides from silently redirecting credentials to an insecure file backend.
    if sys.platform == "darwin":
        from keyring.backends.macOS import Keyring

        return Keyring()
    if os.name == "nt":
        from keyring.backends.Windows import WinVaultKeyring

        return WinVaultKeyring()
    from keyring.backends.SecretService import Keyring

    return Keyring()


def _write_private(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.{secrets.token_hex(8)}.tmp"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
