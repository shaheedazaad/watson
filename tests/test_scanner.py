from __future__ import annotations

from pathlib import Path

from watson.scanner import scan_files


def test_scan_files_only_reads_top_level_visible_files(tmp_path: Path) -> None:
    (tmp_path / "article.txt").write_text("article", encoding="utf-8")
    (tmp_path / ".DS_Store").write_text("ignored", encoding="utf-8")
    (tmp_path / "watson-inventory-report.md").write_text("ignored", encoding="utf-8")
    (tmp_path / "watson-prereg-adherence-report.md").write_text("ignored", encoding="utf-8")
    (tmp_path / "watson_file_context.txt").write_text("ignored", encoding="utf-8")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("ignored", encoding="utf-8")
    (tmp_path / ".watson").mkdir()
    (tmp_path / ".watson" / "inventory.json").write_text("ignored", encoding="utf-8")
    (tmp_path / "notes").mkdir()
    (tmp_path / "notes" / "draft.txt").write_text("ignored", encoding="utf-8")

    records = scan_files(tmp_path)

    assert [record.path for record in records] == ["article.txt"]
    assert records[0].sha256
