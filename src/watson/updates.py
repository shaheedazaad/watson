"""Best-effort, cached release availability checks."""
from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from urllib.request import Request, urlopen

RELEASE_URL = "https://api.github.com/repos/shaheedazaad/watson/releases/latest"
STATUS_FILENAME = "update-status.json"
CHECK_INTERVAL = timedelta(hours=24)


def installed_version() -> str:
    try:
        return version("watson-research-cli")
    except PackageNotFoundError:
        return "0.0.0"


class UpdateChecker:
    def __init__(self, data_dir: Path) -> None:
        self.path = data_dir / STATUS_FILENAME
        self.status = self._read()
        if self._due():
            threading.Thread(target=self._check, daemon=True, name="watson-update-check").start()

    @property
    def available(self) -> bool:
        return bool(self.status.get("available"))

    def _due(self) -> bool:
        try:
            checked = datetime.fromisoformat(self.status["checked_at"])
            return checked + CHECK_INTERVAL <= datetime.now(tz=timezone.utc)
        except (KeyError, TypeError, ValueError):
            return True

    def _read(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            return {}

    def _check(self) -> None:
        try:
            request = Request(RELEASE_URL, headers={"Accept": "application/vnd.github+json", "User-Agent": "watson"})
            with urlopen(request, timeout=3) as response:
                latest = str(json.loads(response.read())['tag_name']).lstrip('v')
            status = {"checked_at": datetime.now(tz=timezone.utc).isoformat(), "available": _newer(latest, installed_version()), "latest_version": latest}
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(json.dumps(status), encoding="utf-8")
            temporary.replace(self.path)
            self.status = status
        except Exception:
            return


def _newer(latest: str, current: str) -> bool:
    def parts(value: str) -> tuple[int, ...]:
        return tuple(int(part) for part in value.split(".") if part.isdigit())
    return parts(latest) > parts(current)
