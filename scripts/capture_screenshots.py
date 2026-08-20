"""Generate the app screenshots used in the docs site.

Spins up a real Watson instance against a temporary project directory,
seeds one demo project with sample input files and pre-baked results (no
Gemini API calls are made), then uses Playwright to capture PNGs of the
key screens into docs/assets/screenshots/.

Dev-only tool, not shipped with the package. Run with:

    .venv311/bin/python scripts/capture_screenshots.py
"""

from __future__ import annotations

import io
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import uvicorn
from playwright.sync_api import sync_playwright

from watson.schemas import (
    DegreeOfFreedomFinding,
    DegreesOfFreedomResult,
    DeviationCheckRun,
    DeviationFinding,
    DocumentClassification,
    DocumentType,
    FileRecord,
    InventoryDiff,
    InventoryResult,
    MissingPreregisteredItem,
    PreregistrationMatch,
    StudyDeviationReport,
    StudyMap,
    StudyMapEntry,
    StudyRecord,
    UnregisteredArticleItem,
)
from watson.web import create_app

TOKEN = "docs-screenshot-token"
PORT = 8731
BASE = f"http://127.0.0.1:{PORT}/{TOKEN}"
OUT_DIR = Path(__file__).resolve().parents[1] / "docs" / "assets" / "screenshots"
VIEWPORT = {"width": 1280, "height": 900}

NOW = datetime(2026, 3, 4, 15, 30, tzinfo=timezone.utc)
ARTICLE = "smith-2026-memory-article.pdf"
PREREG = "smith-2026-preregistration.pdf"
SUPPLEMENT = "smith-2026-supplemental-analyses.pdf"


def build_inventory() -> InventoryResult:
    return InventoryResult(
        generated_at=NOW,
        root="/demo/project",
        model="gemini-3.1-pro-preview",
        files=[
            FileRecord(
                path=ARTICLE,
                extension=".pdf",
                mime_type="application/pdf",
                size_bytes=812_400,
                modified_at=NOW,
                sha256="a" * 64,
            ),
            FileRecord(
                path=PREREG,
                extension=".pdf",
                mime_type="application/pdf",
                size_bytes=143_200,
                modified_at=NOW,
                sha256="b" * 64,
            ),
            FileRecord(
                path=SUPPLEMENT,
                extension=".pdf",
                mime_type="application/pdf",
                size_bytes=94_800,
                modified_at=NOW,
                sha256="c" * 64,
            ),
        ],
        documents=[
            DocumentClassification(
                file_path=ARTICLE,
                document_type=DocumentType.ARTICLE,
                title="Working Memory Load and Recall Accuracy",
                authors=["A. Smith", "B. Chen"],
                confidence=0.96,
                rationale="Published manuscript with methods, results, and discussion sections.",
            ),
            DocumentClassification(
                file_path=PREREG,
                document_type=DocumentType.PREREGISTRATION,
                confidence=0.94,
                rationale="OSF preregistration document with hypotheses and analysis plan.",
            ),
            DocumentClassification(
                file_path=SUPPLEMENT,
                document_type=DocumentType.SUPPLEMENTAL_MATERIAL,
                confidence=0.88,
                rationale="Supplemental robustness checks referenced in the article.",
            ),
        ],
        article_file_path=ARTICLE,
        studies=[
            StudyRecord(
                study_id="study-1",
                label="Study 1",
                description="Working memory load manipulation and free recall accuracy.",
                article_file_path=ARTICLE,
                article_says_preregistered=True,
                confidence=0.92,
            ),
            StudyRecord(
                study_id="study-2",
                label="Study 2",
                description="Replication with an extended retention interval.",
                article_file_path=ARTICLE,
                article_says_preregistered=True,
                confidence=0.9,
            ),
        ],
        preregistration_matches=[
            PreregistrationMatch(
                study_id="study-1",
                study_label="Study 1",
                matched_file_path=PREREG,
                match_status="matched",
                confidence=0.93,
            ),
            PreregistrationMatch(
                study_id="study-2",
                study_label="Study 2",
                matched_file_path=PREREG,
                match_status="matched",
                confidence=0.85,
            ),
        ],
    )


def build_study_map() -> StudyMap:
    return StudyMap(
        generated_at=NOW,
        root="/demo/project",
        model="gemini-3.1-pro-preview",
        article_file_path=ARTICLE,
        studies=[
            StudyMapEntry(
                study_id="study-1",
                label="Study 1",
                article_file_path=ARTICLE,
                article_says_preregistered=True,
                matched_preregistration_file_path=PREREG,
                supplemental_material_file_paths=[SUPPLEMENT],
                match_status="matched",
                match_confidence=0.93,
                ready_for_deviation_check=True,
            ),
            StudyMapEntry(
                study_id="study-2",
                label="Study 2",
                article_file_path=ARTICLE,
                article_says_preregistered=True,
                matched_preregistration_file_path=PREREG,
                supplemental_material_file_paths=[SUPPLEMENT],
                match_status="matched",
                match_confidence=0.85,
                ready_for_deviation_check=True,
            ),
        ],
        preregistration_file_paths=[PREREG],
        supplemental_material_file_paths=[SUPPLEMENT],
    )


def build_deviation_run() -> DeviationCheckRun:
    report_1 = StudyDeviationReport(
        study_id="study-1",
        study_label="Study 1",
        article_file_path=ARTICLE,
        preregistration_file_path=PREREG,
        supplemental_file_paths=[SUPPLEMENT],
        apa_citation="Smith, A., & Chen, B. (2026). Working memory load and recall accuracy. Journal of Cognition.",
        status="completed",
        generated_at=NOW,
        model="gemini-3.1-pro-preview",
        summary="One undisclosed exclusion rule and one preregistered analysis left unreported.",
        missing_preregistered_items=[
            MissingPreregisteredItem(
                prereg_item_id="prereg-3",
                category="analysis",
                preregistered_plan="Planned a linear mixed-effects model on trial-level recall accuracy.",
                searched_for="Mixed-effects model results in the results section and supplement.",
                evidence="Only a repeated-measures ANOVA on aggregated accuracy is reported.",
                disclosed="no",
                confidence="high",
            )
        ],
        unregistered_article_items=[
            UnregisteredArticleItem(
                article_item_id="art-5",
                category="exclusion",
                article_report="Three participants excluded for 'inattentiveness' during the task.",
                framing="unclear",
                evidence="Methods section, paragraph 2.",
                disclosed="no",
                confidence="medium",
            )
        ],
        deviations=[
            DeviationFinding(
                deviation_type="analysis_change",
                summary="Reported analysis differs from the preregistered mixed-effects model.",
                preregistered_plan="Linear mixed-effects model on trial-level accuracy.",
                article_report="Repeated-measures ANOVA on participant-level mean accuracy.",
                evidence="Results, Table 2.",
                confidence="high",
                disclosed="no",
            )
        ],
        degrees_of_freedom=[
            DegreeOfFreedomFinding(
                prereg_item_id="prereg-1",
                category="exclusion",
                preregistered_plan="Exclude participants with 'poor task engagement'.",
                underspecification="No operational definition of engagement was given.",
                plausible_alternatives="Accuracy floor, RT-based, or experimenter judgment could each apply.",
                article_choice="Experimenter judgment, applied post hoc.",
                potential_impact="Exclusion criteria chosen after seeing the data could inflate the effect.",
                evidence="Preregistration, Section 4.",
                severity="high",
            )
        ],
        overall_assessment="One undisclosed analysis change and one high-severity degree of freedom.",
    )
    report_2 = StudyDeviationReport(
        study_id="study-2",
        study_label="Study 2",
        article_file_path=ARTICLE,
        preregistration_file_path=PREREG,
        supplemental_file_paths=[SUPPLEMENT],
        apa_citation="Smith, A., & Chen, B. (2026). Working memory load and recall accuracy. Journal of Cognition.",
        status="completed",
        generated_at=NOW,
        model="gemini-3.1-pro-preview",
        summary="No deviations detected; the replication followed the preregistered plan.",
        overall_assessment="Fully consistent with the preregistration.",
    )
    return DeviationCheckRun(
        generated_at=NOW,
        root="/demo/project",
        model="gemini-3.1-pro-preview",
        guide_path="watson-deviation-guide.yaml",
        study_map_path="study-map.json",
        reports=[report_1, report_2],
    )


def seed_project(app) -> str:
    store = app.state.project_store
    project = store.create("Working Memory Replication (Demo)")
    store.add_stream(project.id, ARTICLE, io.BytesIO(b"%PDF-1.4 demo article content\n" + b"0" * 800_000))
    store.add_stream(project.id, PREREG, io.BytesIO(b"%PDF-1.4 demo preregistration content\n" + b"0" * 140_000))
    store.add_stream(project.id, SUPPLEMENT, io.BytesIO(b"%PDF-1.4 demo supplemental content\n" + b"0" * 90_000))

    paths = store.paths(project.id)
    (paths.state / "inventory.json").write_text(build_inventory().model_dump_json(), encoding="utf-8")
    (paths.state / "study-map.json").write_text(build_study_map().model_dump_json(), encoding="utf-8")
    (paths.state / "deviation-checks.json").write_text(build_deviation_run().model_dump_json(), encoding="utf-8")

    empty = store.create("New Project (Demo)")
    return project.id


def run_server(app) -> uvicorn.Server:
    config = uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    return server


def capture(project_id: str) -> None:
    # Every file written here must be referenced from a docs/*.md page — see the
    # README-style comment at the top of this script. Add a page reference before
    # adding a new screenshot, and delete the file if you stop using one.
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=VIEWPORT)

        page.goto(f"{BASE}/")
        page.screenshot(path=OUT_DIR / "home.png")

        page.goto(f"{BASE}/projects/{project_id}")
        page.screenshot(path=OUT_DIR / "project-overview.png", full_page=True)

        page.goto(f"{BASE}/projects/{project_id}/settings")
        page.screenshot(path=OUT_DIR / "project-settings.png", full_page=True)

        page.goto(f"{BASE}/settings")
        page.screenshot(path=OUT_DIR / "global-settings.png", full_page=True)

        page.goto(f"{BASE}/projects/{project_id}/results")
        page.screenshot(path=OUT_DIR / "results.png", full_page=True)

        page.goto(f"{BASE}/projects/{project_id}/reports/preregistration")
        page.screenshot(path=OUT_DIR / "report-preregistration.png", full_page=True)
        page.click("text=Study 1")
        page.wait_for_timeout(150)
        page.screenshot(path=OUT_DIR / "report-study-expanded.png", full_page=True)

        browser.close()
    print(f"Screenshots written to {OUT_DIR}")


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="watson-docs-screenshots-") as tmp:
        app = create_app(token=TOKEN, data_dir=Path(tmp))
        project_id = seed_project(app)
        server = run_server(app)
        try:
            capture(project_id)
        finally:
            server.should_exit = True
            time.sleep(0.3)


if __name__ == "__main__":
    main()
