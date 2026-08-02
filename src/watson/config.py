from __future__ import annotations

import json
import os
import secrets
import sys
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from typer import prompt


SERVICE_NAME = "watson"
CONFIG_FILENAME = "config.json"
VAULT_FILENAME = "credentials.vault"
VAULT_KEY_FILENAME = ".credential-key"
APP_CONFIG_DIR_ENV = "WATSON_CONFIG_DIR"
DEFAULT_THINKING_LEVEL = "high"
THINKING_LEVEL_OPTIONS = ("minimal", "low", "medium", "high")
_SESSION_SECRETS: dict[str, str] = {}


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
        if not self.config_path.exists():
            return {}
        try:
            config = json.loads(self.config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        legacy_secret = config.pop("gemini_api_key", None)
        if isinstance(legacy_secret, str) and legacy_secret:
            storage = self._save_to_vault_or_session(legacy_secret)
            config["gemini_api_key_storage"] = storage
            self.write_config(config)
        return config

    def write_config(self, config: dict[str, Any]) -> None:
        self.ensure_state_dir()
        _write_private(
            self.config_path,
            (json.dumps(config, indent=2, sort_keys=True) + "\n").encode("utf-8"),
        )

    def get_api_key(self) -> str | None:
        key = self._read_vault()
        if key:
            return key
        self.read_config()  # Migrates plaintext credentials from early development builds.
        return self._read_vault() or _SESSION_SECRETS.get(self._session_key())

    def has_api_key(self) -> bool:
        return self.get_api_key() is not None

    def get_api_key_storage(self) -> str:
        if self._read_vault():
            return "app_vault"
        self.read_config()
        if self._read_vault():
            return "app_vault"
        if _SESSION_SECRETS.get(self._session_key()):
            return "session"
        return "none"

    def prompt_for_api_key(self) -> str:
        return prompt("Enter Gemini API key", hide_input=True).strip()

    def save_api_key(self, api_key: str) -> str:
        api_key = api_key.strip()
        if not api_key:
            raise ValueError("API key cannot be empty.")
        config = self.read_config()
        config.pop("gemini_api_key", None)
        storage = self._save_to_vault_or_session(api_key)
        config["gemini_api_key_storage"] = storage
        self.write_config(config)
        return storage

    def delete_api_key(self) -> None:
        config = self.read_config()
        _SESSION_SECRETS.pop(self._session_key(), None)
        for path in (self.vault_path, self.vault_key_path):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        config.pop("gemini_api_key", None)
        config["gemini_api_key_storage"] = "none"
        self.write_config(config)

    def get_default_model(self, fallback: str) -> str:
        config = self.read_config()
        value = config.get("model")
        return value if isinstance(value, str) and value else fallback

    def set_default_model(self, model: str) -> None:
        config = self.read_config()
        config["model"] = model
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

    def _save_to_vault_or_session(self, api_key: str) -> str:
        try:
            fernet = self._fernet(create=True)
            encrypted = fernet.encrypt(api_key.encode("utf-8")).decode("ascii")
            value = {"version": 1, "gemini_api_key": encrypted}
            _write_private(
                self.vault_path,
                (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8"),
            )
        except (OSError, ValueError):
            _SESSION_SECRETS[self._session_key()] = api_key
            return "session"
        _SESSION_SECRETS.pop(self._session_key(), None)
        return "app_vault"

    def _read_vault(self) -> str | None:
        if not self.vault_path.is_file() or self.vault_path.is_symlink():
            return None
        try:
            raw = json.loads(self.vault_path.read_text(encoding="utf-8"))
            encrypted = raw.get("gemini_api_key")
            if not isinstance(encrypted, str) or not encrypted:
                return None
            return self._fernet(create=False).decrypt(encrypted.encode("ascii")).decode("utf-8")
        except (OSError, ValueError, KeyError, json.JSONDecodeError, InvalidToken):
            return None

    def _fernet(self, *, create: bool) -> Fernet:
        if self.vault_key_path.is_file() and not self.vault_key_path.is_symlink():
            try:
                return Fernet(self.vault_key_path.read_bytes())
            except (OSError, ValueError):
                if not create:
                    raise
        if not create:
            raise ValueError("Credential vault key is unavailable.")
        self.ensure_state_dir()
        key = Fernet.generate_key()
        _write_private(self.vault_key_path, key)
        return Fernet(key)

    def _session_key(self) -> str:
        return str(self.state_dir)


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
