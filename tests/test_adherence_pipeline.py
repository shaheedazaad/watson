from __future__ import annotations

import json
from pathlib import Path

import pytest

from watson.deviation_check import render_study_report, run_deviation_checks
from watson.deviation_guide import load_deviation_guide
from watson.gemini_client import GeminiResearchClient
from watson.schemas import (
    ArticleInventory,
    DegreeOfFreedomFinding,
    DegreesOfFreedomResult,
    DeviationFinding,
    ExecutedItem,
    InventoryDiff,
    MissingPreregisteredItem,
    PlannedItem,
    PreregistrationInventory,
    StudyDeviationReport,
    StudyMap,
    StudyMapEntry,
    UnregisteredArticleItem,
)


class FakeResponse:
    def __init__(self, payload: str) -> None:
        self.text = payload
        self.usage_metadata = None


class FakeModels:
    def __init__(self, payloads: list[str]) -> None:
        self.payloads = list(payloads)
        self.calls: list[dict] = []

    def generate_content(self, *, model, contents, config):
        self.calls.append({"contents": contents, "config": config})
        return FakeResponse(self.payloads.pop(0))


class FakeCaches:
    def __init__(self, name: str | None = "cachedContents/abc") -> None:
        self.name = name
        self.created: list[dict] = []
        self.deleted: list[str] = []

    def create(self, *, model, config):
        self.created.append({"model": model, "config": config})
        if self.name is None:
            raise RuntimeError("Cached content is too small to cache.")
        return type("Cache", (), {"name": self.name})()

    def delete(self, *, name):
        self.deleted.append(name)


class FakeFiles:
    def __init__(self) -> None:
        self.uploads: list[str] = []

    def upload(self, *, file):
        self.uploads.append(str(file))
        return type("File", (), {"name": f"files/{Path(file).stem}", "uri": "", "mime_type": "application/pdf", "state": "ACTIVE"})()

    def get(self, *, name):
        return type("File", (), {"name": name, "uri": "", "mime_type": "application/pdf", "state": "ACTIVE"})()


class FakeGenaiClient:
    def __init__(self, payloads: list[str], cache_name: str | None = "cachedContents/abc") -> None:
        self.models = FakeModels(payloads)
        self.caches = FakeCaches(cache_name)
        self.files = FakeFiles()


def stage_payloads() -> list[str]:
    return [
        PreregistrationInventory(
            items=[
                PlannedItem(
                    item_id="P1",
                    category="exclusion_criteria",
                    statement="Outliers will be removed.",
                    specification="none given",
                    specificity="unspecified",
                ),
                PlannedItem(
                    item_id="P2",
                    category="analysis_model",
                    statement="A mediation analysis will be run.",
                    specification="none given",
                    specificity="partially_specified",
                ),
            ]
        ).model_dump_json(),
        ArticleInventory(
            items=[
                ExecutedItem(
                    item_id="A1",
                    category="exclusion_criteria",
                    statement="Removed responses beyond 3 SD.",
                    framing="confirmatory",
                )
            ]
        ).model_dump_json(),
        InventoryDiff(
            summary="One promised analysis is missing and one exclusion rule differs.",
            overall_assessment="The reported analyses depart from the plan in two places.",
            missing_preregistered_items=[
                MissingPreregisteredItem(
                    prereg_item_id="P2",
                    category="analysis_model",
                    preregistered_plan="A mediation analysis will be run.",
                    searched_for="Results section, supplement, and all tables.",
                    evidence="prereg.pdf p. 3",
                    disclosed="no",
                    confidence="high",
                )
            ],
            unregistered_article_items=[
                UnregisteredArticleItem(
                    article_item_id="A1",
                    category="exclusion_criteria",
                    article_report="Removed responses beyond 3 SD.",
                    framing="confirmatory",
                    evidence="article.pdf p. 6",
                    disclosed="no",
                    confidence="medium",
                )
            ],
            deviations=[
                DeviationFinding(
                    deviation_type="exclusion_criteria",
                    summary="The applied outlier cutoff was never preregistered.",
                    preregistered_plan="Outliers will be removed.",
                    article_report="Removed responses beyond 3 SD.",
                    evidence="prereg.pdf p. 2; article.pdf p. 6",
                    confidence="medium",
                    disclosed="no",
                    prereg_item_id="P1",
                    article_item_id="A1",
                )
            ],
        ).model_dump_json(),
        DegreesOfFreedomResult(
            summary="One preregistered rule leaves the cutoff open.",
            findings=[
                DegreeOfFreedomFinding(
                    prereg_item_id="P1",
                    category="exclusion_criteria",
                    preregistered_plan="Outliers will be removed.",
                    underspecification="No definition or cutoff for an outlier is given.",
                    plausible_alternatives="2 SD, 2.5 SD, 3 SD, IQR-based, or no removal.",
                    article_choice="3 SD",
                    potential_impact="Different cutoffs change which participants are analysed.",
                    evidence="prereg.pdf p. 2",
                    severity="high",
                )
            ],
        ).model_dump_json(),
        "Author, A. (2026). A title. Journal, 1(1), 1-10.",
    ]


def make_client(tmp_path: Path, cache_name: str | None = "cachedContents/abc") -> tuple[GeminiResearchClient, FakeGenaiClient]:
    client = GeminiResearchClient(api_key="test-key")
    fake = FakeGenaiClient(stage_payloads(), cache_name)
    client._client = fake
    for name in ("article.pdf", "prereg.pdf", "supplement.pdf"):
        (tmp_path / name).write_bytes(b"%PDF-1.4 test document")
    return client, fake


def make_study() -> StudyMapEntry:
    return StudyMapEntry(
        study_id="study-1",
        label="Study 1",
        article_file_path="article.pdf",
        article_says_preregistered=True,
        matched_preregistration_file_path="prereg.pdf",
        supplemental_material_file_paths=["supplement.pdf"],
        match_status="matched",
        match_confidence=0.9,
        ready_for_deviation_check=True,
    )


def test_pipeline_runs_one_request_per_stage_over_a_shared_cache(tmp_path: Path) -> None:
    client, fake = make_client(tmp_path)
    guide = load_deviation_guide(Path("watson-deviation-guide.yaml"))
    stages: list[str] = []

    report = client.check_preregistration_adherence(
        tmp_path, make_study(), guide, stages.append
    )

    # Four structured stages plus the citation request.
    assert len(fake.models.calls) == 5
    assert len(fake.caches.created) == 1
    # Every document is uploaded once and then referenced through the cache.
    assert sorted(Path(path).name for path in fake.files.uploads) == [
        "article.pdf",
        "prereg.pdf",
        "supplement.pdf",
    ]
    assert all(call["config"].cached_content == "cachedContents/abc" for call in fake.models.calls)
    # With the documents cached, each request carries only its short stage prompt.
    assert all(len(call["contents"]) == 1 for call in fake.models.calls)
    assert stages[0] == "inventorying the preregistration"
    assert "degrees of freedom" in stages[-2]

    assert report.preregistration_inventory is not None
    assert [item.item_id for item in report.preregistration_inventory.items] == ["P1", "P2"]
    assert [item.prereg_item_id for item in report.missing_preregistered_items] == ["P2"]
    assert [item.article_item_id for item in report.unregistered_article_items] == ["A1"]
    assert [item.deviation_type for item in report.deviations] == ["exclusion_criteria"]
    assert [item.prereg_item_id for item in report.degrees_of_freedom] == ["P1"]
    assert report.supplemental_file_paths == ["supplement.pdf"]
    assert report.apa_citation.startswith("Author, A.")
    assert report.finding_count == 4


def test_pipeline_attaches_documents_when_caching_is_refused(tmp_path: Path) -> None:
    client, fake = make_client(tmp_path, cache_name=None)
    guide = load_deviation_guide(Path("watson-deviation-guide.yaml"))

    report = client.check_preregistration_adherence(tmp_path, make_study(), guide)

    # The refusal is remembered, so the remaining stages do not retry the cache.
    assert len(fake.caches.created) == 1
    assert all(call["config"].cached_content is None for call in fake.models.calls)
    # Stage 1 gets the preregistration; stage 2 gets the article plus its supplement.
    assert len(fake.models.calls[0]["contents"]) == 2
    assert len(fake.models.calls[1]["contents"]) == 3
    # Without a cache the guide has to travel inline with every stage prompt.
    assert "Allowed deviation_type values:" in fake.models.calls[0]["contents"][-1]
    assert report.finding_count == 4


def test_release_caches_deletes_what_the_run_created(tmp_path: Path) -> None:
    client, fake = make_client(tmp_path)
    guide = load_deviation_guide(Path("watson-deviation-guide.yaml"))
    client.check_preregistration_adherence(tmp_path, make_study(), guide)

    client.release_caches()

    assert fake.caches.deleted == ["cachedContents/abc"]
    assert client._context_caches == {}


def test_stage_four_failure_keeps_the_first_three_sections(tmp_path: Path) -> None:
    client, fake = make_client(tmp_path)
    fake.models.payloads[3] = "not json at all"
    guide = load_deviation_guide(Path("watson-deviation-guide.yaml"))

    report = client.check_preregistration_adherence(tmp_path, make_study(), guide)

    assert report.degrees_of_freedom == []
    assert report.stage_errors and "Degrees-of-freedom stage failed" in report.stage_errors[0]
    assert report.missing_preregistered_items
    assert report.deviations


def test_report_markdown_carries_all_four_sections(tmp_path: Path) -> None:
    client, _ = make_client(tmp_path)
    guide = load_deviation_guide(Path("watson-deviation-guide.yaml"))
    report = client.check_preregistration_adherence(tmp_path, make_study(), guide)

    markdown = "\n".join(render_study_report(report))

    assert "#### 1. Missing Preregistered Items" in markdown
    assert "#### 2. Reported But Not Preregistered" in markdown
    assert "#### 3. Deviations" in markdown
    assert "#### 4. Preregistration Degrees Of Freedom" in markdown
    assert "A mediation analysis will be run." in markdown
    assert "2 SD, 2.5 SD, 3 SD, IQR-based, or no removal." in markdown
    assert "<summary>Preregistration inventory</summary>" in markdown
    assert "Preregistered commitments inventoried: 2 (2 not fully specified)" in markdown


def test_run_deviation_checks_forwards_stage_progress(tmp_path: Path) -> None:
    state_dir = tmp_path / ".watson"
    state_dir.mkdir()
    client, _ = make_client(tmp_path)
    guide = load_deviation_guide(Path("watson-deviation-guide.yaml"))
    study_map = StudyMap(
        generated_at="2026-01-01T00:00:00Z",
        root=str(tmp_path),
        model="gemini-3.1-pro-preview",
        article_file_path="article.pdf",
        studies=[make_study()],
        preregistration_file_paths=["prereg.pdf"],
    )
    messages: list[str] = []

    run = run_deviation_checks(
        root=tmp_path,
        state_dir=state_dir,
        study_map=study_map,
        guide=guide,
        guide_path=Path("watson-deviation-guide.yaml"),
        client=client,
        model="gemini-3.1-pro-preview",
        progress=messages.append,
    )

    assert run.reports[0].status == "completed"
    assert "Study 1 (1/1): inventorying the preregistration" in messages
    assert "Study 1 (1/1): diffing the two inventories" in messages
