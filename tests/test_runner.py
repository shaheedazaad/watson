from __future__ import annotations

from pathlib import Path

import pytest

from watson.projects import ProjectStore
from watson.runner import EventCancellationSignal, ProcessingCancelled, RunnerSettings, run_project
from watson.schemas import DocumentClassification, DocumentType, StudyExtractionResult


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
