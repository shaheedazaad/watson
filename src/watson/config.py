from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from rich.console import Console
from typer import prompt


SERVICE_NAME = "watson"
KEYRING_USERNAME = "gemini-api-key"
CONFIG_FILENAME = "config.json"
APP_CONFIG_DIR_ENV = "WATSON_CONFIG_DIR"
DEFAULT_THINKING_LEVEL = "high"
THINKING_LEVEL_OPTIONS = ("minimal", "low", "medium", "high")


def get_app_state_dir() -> Path:
    override = os.environ.get(APP_CONFIG_DIR_ENV)
    if override:
        return Path(override).expanduser().resolve()
    if sys.platform == "darwin":
        return (Path.home() / "Library" / "Application Support" / SERVICE_NAME).resolve()
    xdg_config_home = os.environ.get("XDG_CONFIG_HOME")
    if xdg_config_home:
        return (Path(xdg_config_home).expanduser() / SERVICE_NAME).resolve()
    return (Path.home() / ".config" / SERVICE_NAME).resolve()


class ConfigStore:
    def __init__(self, state_dir: Path, console: Console | None = None) -> None:
        self.state_dir = state_dir
        self.config_path = state_dir / CONFIG_FILENAME
        self.console = console or Console()

    def ensure_state_dir(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)

    def read_config(self) -> dict[str, Any]:
        if not self.config_path.exists():
            return {}
        return json.loads(self.config_path.read_text(encoding="utf-8"))

    def write_config(self, config: dict[str, Any]) -> None:
        self.ensure_state_dir()
        self.config_path.write_text(
            json.dumps(config, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        try:
            os.chmod(self.config_path, 0o600)
        except OSError:
            pass

    def get_api_key(self) -> str | None:
        key = self._get_keyring_key()
        if key:
            return key
        config = self.read_config()
        value = config.get("gemini_api_key")
        return value if isinstance(value, str) and value else None

    def has_api_key(self) -> bool:
        return self.get_api_key() is not None

    def get_api_key_storage(self) -> str:
        if self._get_keyring_key():
            return "keyring"
        config = self.read_config()
        if config.get("gemini_api_key"):
            return "config_file"
        return "none"

    def prompt_for_api_key(self) -> str:
        return prompt("Enter Gemini API key", hide_input=True).strip()

    def save_api_key(self, api_key: str) -> str:
        config = self.read_config()
        if self._set_keyring_key(api_key):
            config.pop("gemini_api_key", None)
            config["gemini_api_key_storage"] = "keyring"
            self.write_config(config)
            return "keyring"

        config["gemini_api_key"] = api_key
        config["gemini_api_key_storage"] = "config_file"
        self.write_config(config)
        return "config_file"

    def delete_api_key(self) -> None:
        self._delete_keyring_key()
        config = self.read_config()
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

    def _get_keyring_key(self) -> str | None:
        try:
            import keyring

            return keyring.get_password(SERVICE_NAME, KEYRING_USERNAME)
        except Exception:
            return None

    def _set_keyring_key(self, api_key: str) -> bool:
        try:
            import keyring

            keyring.set_password(SERVICE_NAME, KEYRING_USERNAME, api_key)
            return True
        except Exception:
            return False

    def _delete_keyring_key(self) -> None:
        try:
            import keyring

            keyring.delete_password(SERVICE_NAME, KEYRING_USERNAME)
        except Exception:
            pass
