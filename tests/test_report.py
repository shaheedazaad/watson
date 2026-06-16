from __future__ import annotations

from datetime import datetime, timezone

from watson.report import render_inventory_report
from watson.schemas import (
    DocumentClassification,
    DocumentType,
    InventoryResult,
    PreregistrationMatch,
    StudyRecord,
)


def test_report_contains_summary_and_match_table() -> None:
    inventory = InventoryResult(
        generated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        root="/tmp/project",
        model="gemini-3.1-pro-preview",
        files=[],
        documents=[
            DocumentClassification(
                file_path="article.pdf",
                document_type=DocumentType.ARTICLE,
                confidence=0.9,
                rationale="Published article.",
            )
        ],
        article_file_path="article.pdf",
        studies=[
            StudyRecord(
                study_id="study-1",
                label="Study 1",
                article_file_path="article.pdf",
                article_says_preregistered=True,
                confidence=0.8,
            )
        ],
        preregistration_matches=[
            PreregistrationMatch(
                study_id="study-1",
                study_label="Study 1",
                matched_file_path="prereg.pdf",
                match_status="matched",
                confidence=0.85,
            )
        ],
    )

    report = render_inventory_report(inventory)

    assert "Watson detected 1 study/experiment record(s)." in report
    assert "Study 1" in report
    assert "`prereg.pdf`" in report
