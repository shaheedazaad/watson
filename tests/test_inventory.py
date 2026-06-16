from __future__ import annotations

from pathlib import Path
from datetime import datetime, timezone

from watson.inventory import (
    build_study_map,
    save_inventory,
    save_study_map,
    save_upload_cache,
    run_inventory,
)
from watson.schemas import (
    DocumentClassification,
    DocumentType,
    InventoryResult,
    PreregistrationMatch,
    StudyExtractionResult,
    StudyRecord,
)


class FakeClient:
    def __init__(self) -> None:
        self.upload_cache = {}

    def classify_document(self, root: Path, file_record):
        document_type = DocumentType.UNKNOWN
        if file_record.path == "article.txt":
            document_type = DocumentType.ARTICLE
        elif file_record.path == "prereg.txt":
            document_type = DocumentType.PREREGISTRATION
        return DocumentClassification(
            file_path=file_record.path,
            document_type=document_type,
            confidence=0.9,
            rationale="fixture",
        )

    def extract_studies(self, root: Path, article: DocumentClassification):
        return StudyExtractionResult(
            article_file_path=article.file_path,
            studies=[
                StudyRecord(
                    study_id="study-1",
                    label="Study 1",
                    article_file_path=article.file_path,
                    article_says_preregistered=True,
                    confidence=0.9,
                )
            ],
            confidence=0.9,
        )

    def match_preregistrations(self, root: Path, study, preregistrations):
        return PreregistrationMatch(
            study_id=study.study_id,
            study_label=study.label,
            matched_file_path=preregistrations[0].file_path,
            match_status="matched",
            confidence=0.9,
        )


def test_run_inventory_classifies_extracts_and_matches(tmp_path: Path) -> None:
    (tmp_path / "article.txt").write_text("Study 1 was preregistered.", encoding="utf-8")
    (tmp_path / "prereg.txt").write_text("Preregistration for Study 1.", encoding="utf-8")
    (tmp_path / "image.png").write_bytes(b"not supported")
    state_dir = tmp_path / ".watson"
    state_dir.mkdir()

    inventory = run_inventory(tmp_path, state_dir, FakeClient())

    assert inventory.article_file_path == "article.txt"
    assert [file_record.path for file_record in inventory.files] == ["article.txt", "prereg.txt"]
    assert len(inventory.studies) == 1
    assert inventory.preregistration_matches[0].matched_file_path == "prereg.txt"
    assert "Skipped `image.png`" in inventory.review_notes[0]


def test_build_study_map_records_ready_preregistered_studies(tmp_path: Path) -> None:
    (tmp_path / "article.txt").write_text("Study 1 was preregistered.", encoding="utf-8")
    (tmp_path / "prereg.txt").write_text("Preregistration for Study 1.", encoding="utf-8")
    state_dir = tmp_path / ".watson"
    state_dir.mkdir()

    inventory = run_inventory(tmp_path, state_dir, FakeClient())
    study_map = build_study_map(inventory)

    assert study_map.article_file_path == "article.txt"
    assert study_map.preregistration_file_paths == ["prereg.txt"]
    assert len(study_map.studies) == 1
    assert study_map.studies[0].matched_preregistration_file_path == "prereg.txt"
    assert study_map.studies[0].ready_for_deviation_check is True


def test_save_inventory_artifacts_create_missing_state_dir(tmp_path: Path) -> None:
    state_dir = tmp_path / ".watson"
    inventory = InventoryResult(
        generated_at=datetime.now(tz=timezone.utc),
        root=str(tmp_path),
        model="gemini-3.1-pro-preview",
        files=[],
        documents=[],
    )
    study_map = build_study_map(inventory)

    save_inventory(state_dir / "inventory.json", inventory)
    save_study_map(state_dir / "study-map.json", study_map)
    save_upload_cache(state_dir / "gemini-files.json", {})

    assert (state_dir / "inventory.json").exists()
    assert (state_dir / "study-map.json").exists()
    assert (state_dir / "gemini-files.json").exists()
