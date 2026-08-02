from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from watson.projects import (
    ProjectError,
    ProjectSettings,
    ProjectStore,
    SettingsInvalidationRequired,
    UploadTooLargeError,
    sanitize_filename,
)


def test_project_creation_reopening_and_search(tmp_path: Path) -> None:
    store = ProjectStore(tmp_path / "application data")
    created = store.create("  Replication   α  ")

    reopened = ProjectStore(tmp_path / "application data").get(created.id)
    projects, total = store.list("α")

    assert reopened.name == "Replication α"
    assert reopened.id == created.id
    assert total == 1
    assert projects[0].metadata.id == created.id


def test_duplicate_inputs_are_numbered_and_paths_are_sanitized(tmp_path: Path) -> None:
    store = ProjectStore(tmp_path)
    project = store.create("Duplicates")

    first = store.add_stream(project.id, "../paper.txt", io.BytesIO(b"first"))
    second = store.add_stream(project.id, "paper.txt", io.BytesIO(b"second"))

    assert first.name == "paper.txt"
    assert second.name == "paper (2).txt"
    assert [item.name for item in store.list_inputs(project.id)] == ["paper (2).txt", "paper.txt"]
    assert not (store.paths(project.id).root.parent / "paper.txt").exists()


def test_upload_validates_signature_and_size(tmp_path: Path, monkeypatch) -> None:
    store = ProjectStore(tmp_path)
    project = store.create("Limits")

    with pytest.raises(ProjectError, match="valid PDF signature"):
        store.add_stream(project.id, "fake.pdf", io.BytesIO(b"not a pdf"))

    monkeypatch.setattr("watson.projects.MAX_FILE_BYTES", 3)
    with pytest.raises(UploadTooLargeError):
        store.add_stream(project.id, "large.txt", io.BytesIO(b"four"))
    assert store.list_inputs(project.id) == []


def test_unicode_long_and_windows_reserved_filenames_are_safe() -> None:
    assert sanitize_filename("résumé 数据.txt") == "résumé 数据.txt"
    assert len(sanitize_filename("a" * 300 + ".txt")) <= 180
    assert sanitize_filename("CON.txt") == "_CON.txt"


def test_settings_require_confirmation_before_outputs_are_invalidated(tmp_path: Path) -> None:
    store = ProjectStore(tmp_path)
    project = store.create("Settings")
    paths = store.paths(project.id)
    (paths.inputs / "source.txt").write_text("source", encoding="utf-8")
    (paths.state / "inventory.json").write_text("{}", encoding="utf-8")
    (paths.outputs / "report.md").write_text("result", encoding="utf-8")
    changed = ProjectSettings(model="new-model", thinking_level="medium")

    with pytest.raises(SettingsInvalidationRequired):
        store.set_settings(project.id, changed)

    assert (paths.outputs / "report.md").exists()
    store.set_settings(project.id, changed, confirm_invalidation=True)

    assert not (paths.outputs / "report.md").exists()
    assert not (paths.state / "inventory.json").exists()
    assert (paths.inputs / "source.txt").read_text(encoding="utf-8") == "source"


def test_invalid_settings_never_delete_outputs(tmp_path: Path) -> None:
    store = ProjectStore(tmp_path)
    project = store.create("Invalid settings")
    output = store.paths(project.id).outputs / "result.json"
    output.write_text(json.dumps({"ok": True}), encoding="utf-8")

    with pytest.raises(ValueError):
        ProjectSettings(model="", thinking_level="high")

    assert output.exists()
