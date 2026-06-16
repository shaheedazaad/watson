from __future__ import annotations

from pathlib import Path

from watson.cli import resolve_guide_path


def test_resolve_guide_path_prefers_workspace_file(tmp_path: Path) -> None:
    guide_path = tmp_path / "watson-deviation-guide.yaml"
    guide_path.write_text("version: 1\n", encoding="utf-8")

    resolved = resolve_guide_path(Path("watson-deviation-guide.yaml"), tmp_path)

    assert resolved == guide_path


def test_resolve_guide_path_falls_back_to_repo_file(tmp_path: Path) -> None:
    resolved = resolve_guide_path(Path("watson-deviation-guide.yaml"), tmp_path)

    assert resolved.exists()
    assert resolved.name == "watson-deviation-guide.yaml"
