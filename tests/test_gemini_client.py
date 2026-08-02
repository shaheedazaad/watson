from __future__ import annotations

from pathlib import Path

from watson.deviation_guide import load_deviation_guide
from watson.gemini_client import build_deviation_checklist_prompt, build_deviation_prompt
from watson.schemas import StudyMapEntry


def test_deviation_prompt_keeps_wrapper_minimal() -> None:
    guide = load_deviation_guide(Path("watson-deviation-guide.yaml"))
    study = StudyMapEntry(
        study_id="study-1",
        label="Study 1",
        article_file_path="article.pdf",
        matched_preregistration_file_path="prereg.pdf",
    )

    prompt = build_deviation_prompt(guide, study, "Check outlier rules and thresholds.")

    assert "Target study metadata:" in prompt
    assert "Internal checklist prepared from the guide:" in prompt
    assert "Check outlier rules and thresholds." in prompt
    assert "Every issue you mention" not in prompt
    assert "If there are no deviations" not in prompt
    assert "explanation_given" in prompt


def test_deviation_checklist_prompt_primes_from_guide_without_documents() -> None:
    guide = load_deviation_guide(Path("watson-deviation-guide.yaml"))

    prompt = build_deviation_checklist_prompt(guide)

    assert "convert this guide into a concise audit" in prompt
    assert "Do not evaluate a study yet." in prompt
    assert "outliers" in prompt
