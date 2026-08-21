from __future__ import annotations

from pathlib import Path

import pytest
from click import unstyle
from typer.testing import CliRunner

import watson.cli as cli_module
import watson.config as config_module
from watson.cli import app, resolve_guide_path
from watson.projects import ProjectStore


def test_resolve_guide_path_prefers_workspace_file(tmp_path: Path) -> None:
    guide_path = tmp_path / "watson-deviation-guide.yaml"
    guide_path.write_text("version: 1\n", encoding="utf-8")

    resolved = resolve_guide_path(Path("watson-deviation-guide.yaml"), tmp_path)

    assert resolved == guide_path


def test_resolve_guide_path_falls_back_to_repo_file(tmp_path: Path) -> None:
    resolved = resolve_guide_path(Path("watson-deviation-guide.yaml"), tmp_path)

    assert resolved.exists()
    assert resolved.name == "watson-deviation-guide.yaml"


def test_headless_run_requires_a_saved_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = ProjectStore(tmp_path).create("No implicit CLI access")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setattr(
        config_module,
        "_credential_backend",
        lambda: (_ for _ in ()).throw(AssertionError("unexpected Keychain access")),
    )

    result = CliRunner().invoke(app, ["run", project.id, "--data-dir", str(tmp_path)])

    assert result.exit_code != 0
    assert "Save a Gemini API key" in unstyle(result.output)


def test_headless_run_reads_keychain_when_the_run_starts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = ProjectStore(tmp_path).create("Explicit CLI access")
    calls: list[str] = []

    class Backend:
        def get_password(self, service: str, username: str) -> str:
            calls.append("get")
            return "cli-secret"

    class Result:
        def model_dump_json(self, indent: int = 2) -> str:
            return "{}"

    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setattr(config_module, "_credential_backend", lambda: Backend())
    monkeypatch.setattr(cli_module, "run_project", lambda *args, **kwargs: Result())
    config_module.ConfigStore(tmp_path).write_config({"gemini_api_key_storage": "keychain"})

    result = CliRunner().invoke(
        app,
        ["run", project.id, "--data-dir", str(tmp_path)],
    )

    assert result.exit_code == 0
    assert calls == ["get"]
