from __future__ import annotations

from pathlib import Path

from watson.deviation_check import (
    DEVIATION_RESULTS_DIRNAME,
    load_study_report,
    render_deviation_markdown,
    render_report_warning,
    render_study_report,
    run_deviation_checks,
    save_deviation_run,
    study_result_path,
    validate_report_consistency,
    validate_report_deviation_types,
)
from watson.deviation_guide import load_deviation_guide
from watson.schemas import DeviationCheckRun, DeviationFinding, StudyDeviationReport, StudyMap, StudyMapEntry


class FakeDeviationClient:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def check_preregistration_adherence(self, root: Path, study: StudyMapEntry, guide):
        self.calls.append(study.study_id)
        return StudyDeviationReport(
            study_id=study.study_id,
            study_label=study.label,
            article_file_path=study.article_file_path,
            preregistration_file_path=study.matched_preregistration_file_path,
            summary="One potential deviation was found.",
            overall_assessment="The preregistration is partially adhered to.",
            deviations=[
                DeviationFinding(
                    deviation_type="degree_of_freedom",
                    summary="The analysis model was underspecified.",
                    preregistered_plan="Run a regression.",
                    article_report="Reports a regression with covariates.",
                    evidence="prereg.pdf p. 2; article.pdf p. 8",
                    confidence="medium",
                    disclosed="no",
                    explanation_given="no",
                    robustness_check="not reported",
                )
            ],
        )


def test_run_deviation_checks_saves_per_study_and_markdown(tmp_path: Path) -> None:
    state_dir = tmp_path / ".watson"
    state_dir.mkdir()
    study_map = make_study_map(tmp_path)
    guide = load_deviation_guide(Path("watson-deviation-guide.yaml"))
    client = FakeDeviationClient()

    run = run_deviation_checks(
        root=tmp_path,
        state_dir=state_dir,
        study_map=study_map,
        guide=guide,
        guide_path=Path("watson-deviation-guide.yaml"),
        client=client,
        model="gemini-3.1-pro-preview",
    )

    result_path = study_result_path(state_dir / DEVIATION_RESULTS_DIRNAME, study_map.studies[0])
    saved_report = load_study_report(result_path)
    markdown = render_deviation_markdown(run)

    assert client.calls == ["study-1"]
    assert saved_report.deviations[0].deviation_type == "degree_of_freedom"
    assert "Watson Preregistration Adherence Report" in markdown
    assert "Study 1" in markdown
    assert "degree\\_of\\_freedom" in markdown
    assert "Guide:" not in markdown
    assert "Interpret these results with caution" in markdown


def test_run_deviation_checks_skips_completed_results_without_force(tmp_path: Path) -> None:
    state_dir = tmp_path / ".watson"
    state_dir.mkdir()
    study_map = make_study_map(tmp_path)
    guide = load_deviation_guide(Path("watson-deviation-guide.yaml"))
    result_path = study_result_path(state_dir / DEVIATION_RESULTS_DIRNAME, study_map.studies[0])
    result_path.parent.mkdir()
    result_path.write_text(
        StudyDeviationReport(
            study_id="study-1",
            study_label="Study 1",
            article_file_path="article.pdf",
            preregistration_file_path="prereg.pdf",
            status="completed",
            summary="Existing result.",
        ).model_dump_json(indent=2)
        + "\n",
        encoding="utf-8",
    )
    client = FakeDeviationClient()

    run = run_deviation_checks(
        root=tmp_path,
        state_dir=state_dir,
        study_map=study_map,
        guide=guide,
        guide_path=Path("watson-deviation-guide.yaml"),
        client=client,
        model="gemini-3.1-pro-preview",
    )

    assert client.calls == []
    assert run.reports[0].summary == "Existing result."


def test_validate_report_deviation_types_adds_review_note() -> None:
    guide = load_deviation_guide(Path("watson-deviation-guide.yaml"))
    report = StudyDeviationReport(
        study_id="study-1",
        study_label="Study 1",
        article_file_path="article.pdf",
        preregistration_file_path="prereg.pdf",
        deviations=[
            DeviationFinding(
                deviation_type="not_in_guide",
                summary="Unknown type.",
                preregistered_plan="Plan.",
                article_report="Report.",
                evidence="Evidence.",
                confidence="low",
            )
        ],
    )

    validate_report_deviation_types(report, guide)

    assert "undefined deviation_type" in report.review_notes[0]


def test_save_deviation_run_creates_missing_state_dir(tmp_path: Path) -> None:
    path = tmp_path / ".watson" / "deviation-checks.json"
    run = DeviationCheckRun(
        generated_at="2026-01-01T00:00:00Z",
        root=str(tmp_path),
        model="gemini-3.1-pro-preview",
        guide_path="watson-deviation-guide.yaml",
        study_map_path=".watson/study-map.json",
    )

    save_deviation_run(path, run)

    assert path.exists()


def test_null_like_error_is_not_rendered_as_failure() -> None:
    report = StudyDeviationReport(
        study_id="study-1",
        study_label="Meta-analysis",
        article_file_path="article.pdf",
        preregistration_file_path="prereg.pdf",
        status="completed",
        error="null",
    )

    lines = render_study_report(report)

    assert "Error: null" not in "\n".join(lines)
    assert "No deviations were reported by the model." in "\n".join(lines)


def test_empty_deviations_with_deviation_prose_gets_review_note() -> None:
    report = StudyDeviationReport(
        study_id="study-1",
        study_label="Meta-analysis",
        article_file_path="article.pdf",
        preregistration_file_path="prereg.pdf",
        status="completed",
        overall_assessment="The article largely adheres but features a few deviations and omitted sensitivity analyses.",
        deviations=[],
    )

    validate_report_consistency(report)
    markdown = "\n".join(render_study_report(report))

    assert "empty structured deviations list" in report.review_notes[0]
    assert "No deviations were reported by the model." not in markdown
    assert "No structured deviations were returned" in markdown


def test_render_study_report_prefers_single_overall_assessment_block() -> None:
    report = StudyDeviationReport(
        study_id="study-1",
        study_label="Meta-analysis",
        article_file_path="article.pdf",
        preregistration_file_path="prereg.pdf",
        status="completed",
        summary="Short summary that should not render separately.",
        overall_assessment="Longer assessment that should be the only narrative block.",
    )

    markdown = "\n".join(render_study_report(report))

    assert markdown.count("#### Overall Assessment") == 1
    assert "Longer assessment that should be the only narrative block." in markdown
    assert "Short summary that should not render separately." not in markdown


def test_render_report_warning_uses_user_facing_caution_text() -> None:
    warning = render_report_warning()

    assert "color: orange" in warning
    assert "checked manually by the user" in warning


def test_render_study_report_omits_severity_even_if_present() -> None:
    report = StudyDeviationReport(
        study_id="study-1",
        study_label="Meta-analysis",
        article_file_path="article.pdf",
        preregistration_file_path="prereg.pdf",
        deviations=[
            DeviationFinding.model_validate(
                {
                    "deviation_type": "degree_of_freedom",
                    "severity": "high",
                    "summary": "Analytic flexibility was present.",
                    "preregistered_plan": "Run a fixed model.",
                    "article_report": "Runs an expanded model.",
                    "evidence": "article.pdf p. 8",
                    "confidence": "medium",
                }
            )
        ],
    )

    markdown = "\n".join(render_study_report(report))

    assert "| Type | Confidence | Summary |" in markdown
    assert "Severity" not in markdown
    assert "- Severity:" not in markdown


def test_render_study_report_uses_explanation_given() -> None:
    report = StudyDeviationReport(
        study_id="study-1",
        study_label="Meta-analysis",
        article_file_path="article.pdf",
        preregistration_file_path="prereg.pdf",
        deviations=[
            DeviationFinding(
                deviation_type="degree_of_freedom",
                summary="Analytic flexibility was present.",
                preregistered_plan="Run a fixed model.",
                article_report="Runs an expanded model.",
                evidence="article.pdf p. 8",
                confidence="medium",
                explanation_given="yes",
            )
        ],
    )

    markdown = "\n".join(render_study_report(report))

    assert "- Explanation given: yes" in markdown
    assert "- Justified:" not in markdown


def make_study_map(root: Path) -> StudyMap:
    return StudyMap(
        generated_at="2026-01-01T00:00:00Z",
        root=str(root),
        model="gemini-3.1-pro-preview",
        article_file_path="article.pdf",
        studies=[
            StudyMapEntry(
                study_id="study-1",
                label="Study 1",
                article_file_path="article.pdf",
                article_says_preregistered=True,
                matched_preregistration_file_path="prereg.pdf",
                match_status="matched",
                match_confidence=0.9,
                ready_for_deviation_check=True,
            )
        ],
        preregistration_file_paths=["prereg.pdf"],
    )
