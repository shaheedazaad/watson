from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from watson.projects import ProjectStore
from watson.runner import EventCancellationSignal, ProcessingCancelled, RunnerSettings, run_project
from watson.scanner import scan_files
from watson.schemas import (
    ArticleInventory,
    CodeAuditResult,
    DeviationCheckRun,
    DocumentClassification,
    DocumentType,
    InventoryResult,
    PreregistrationInventory,
    StudyDeviationReport,
    StudyExtractionResult,
    StudyMap,
    StudyMapEntry,
)


class FakeInventoryClient:
    classifications = 0

    def __init__(self, **_kwargs) -> None:
        self.upload_cache = {}

    def classify_document(self, _root, file_record):
        type(self).classifications += 1
        return DocumentClassification(
            file_path=file_record.path,
            document_type=DocumentType.ARTICLE,
            confidence=0.9,
        )

    def extract_studies(self, _root, article):
        return StudyExtractionResult(article_file_path=article.file_path, confidence=0.9)


def test_runner_reuses_current_inventory_and_records_reproducibility(tmp_path: Path) -> None:
    store = ProjectStore(tmp_path)
    project = store.create("Runner")
    paths = store.paths(project.id)
    (paths.inputs / "article.txt").write_text("A paper", encoding="utf-8")
    settings = RunnerSettings(action="inventory", api_key="secret", model="test-model")
    FakeInventoryClient.classifications = 0

    first = run_project(paths.root, settings, client_factory=FakeInventoryClient)
    second = run_project(paths.root, settings, client_factory=FakeInventoryClient)

    assert first.summary["files"] == second.summary["files"] == 1
    assert FakeInventoryClient.classifications == 1
    metadata = (paths.outputs / "reproducibility.json").read_text(encoding="utf-8")
    assert "test-model" in metadata
    assert "secret" not in metadata


def test_runner_honors_cancellation_before_processing(tmp_path: Path) -> None:
    store = ProjectStore(tmp_path)
    project = store.create("Cancel")
    signal = EventCancellationSignal()
    signal.cancel()

    with pytest.raises(ProcessingCancelled):
        run_project(
            store.paths(project.id).root,
            RunnerSettings(action="inventory", api_key="secret"),
            cancellation=signal,
            client_factory=FakeInventoryClient,
        )


class FakeCodeAuditClient:
    calls = 0

    def __init__(self, **kwargs) -> None:
        self.upload_cache = kwargs.get("upload_cache", {})
        self.usage = {}

    def audit_code(
        self,
        _root,
        _code_dir,
        study,
        _guide,
        preregistration_inventory,
        article_inventory,
    ):
        type(self).calls += 1
        assert preregistration_inventory.study_id == study.study_id
        assert article_inventory.study_id == study.study_id
        return CodeAuditResult(study_id=study.study_id, study_label=study.label)


def test_runner_can_rerun_only_code_audit_from_saved_inventories(tmp_path: Path) -> None:
    store = ProjectStore(tmp_path)
    project = store.create("Code rerun")
    paths = store.paths(project.id)
    (paths.inputs / "article.txt").write_text("A paper", encoding="utf-8")
    (paths.code / "analysis.R").write_text("result <- 1", encoding="utf-8")
    now = datetime.now(tz=timezone.utc)
    inventory = InventoryResult(
        generated_at=now,
        root=str(paths.inputs),
        model="test-model",
        files=scan_files(paths.inputs),
        documents=[],
    )
    (paths.state / "inventory.json").write_text(
        inventory.model_dump_json(indent=2), encoding="utf-8"
    )
    study = StudyMapEntry(
        study_id="study-1",
        label="Study 1",
        article_file_path="article.txt",
        matched_preregistration_file_path="prereg.txt",
        ready_for_deviation_check=True,
    )
    study_map = StudyMap(
        generated_at=now,
        root=str(paths.inputs),
        model="test-model",
        studies=[study],
    )
    (paths.state / "study-map.json").write_text(
        study_map.model_dump_json(indent=2), encoding="utf-8"
    )
    report = StudyDeviationReport(
        study_id=study.study_id,
        study_label=study.label,
        article_file_path=study.article_file_path,
        preregistration_inventory=PreregistrationInventory(study_id=study.study_id),
        article_inventory=ArticleInventory(study_id=study.study_id),
    )
    deviation_run = DeviationCheckRun(
        generated_at=now,
        root=str(paths.inputs),
        model="test-model",
        guide_path="guide.yaml",
        study_map_path=str(paths.state / "study-map.json"),
        reports=[report],
    )
    (paths.state / "deviation-checks.json").write_text(
        deviation_run.model_dump_json(indent=2), encoding="utf-8"
    )
    FakeCodeAuditClient.calls = 0

    result = run_project(
        paths.root,
        RunnerSettings(action="code_audit", api_key="secret", model="test-model"),
        client_factory=FakeCodeAuditClient,
    )

    assert result.summary["code_audits"] == 1
    assert FakeCodeAuditClient.calls == 1
    assert (paths.state / "code-audit.json").is_file()
    assert (paths.state / "deviation-checks.json").is_file()

    run_project(
        paths.root,
        RunnerSettings(action="code_audit", api_key="secret", model="test-model"),
        client_factory=FakeCodeAuditClient,
    )
    assert FakeCodeAuditClient.calls == 1

    run_project(
        paths.root,
        RunnerSettings(
            action="code_audit",
            api_key="secret",
            model="test-model",
            retry_mode="all",
        ),
        client_factory=FakeCodeAuditClient,
    )
    assert FakeCodeAuditClient.calls == 2
