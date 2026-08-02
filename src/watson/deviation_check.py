from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from watson.deviation_guide import DeviationGuide, build_deviation_system_prompt
from watson.schemas import DeviationCheckRun, StudyDeviationReport, StudyMap, StudyMapEntry


DEVIATION_RESULTS_DIRNAME = "deviation-results"
DEVIATION_RUN_FILENAME = "deviation-checks.json"
DEVIATION_REPORT_FILENAME = "watson-prereg-adherence-report.md"


class DeviationClient(Protocol):
    def check_preregistration_adherence(
        self,
        root: Path,
        study: StudyMapEntry,
        guide: DeviationGuide,
    ) -> StudyDeviationReport: ...


def load_study_map(path: Path) -> StudyMap:
    return StudyMap.model_validate_json(path.read_text(encoding="utf-8"))


def study_result_path(results_dir: Path, study: StudyMapEntry) -> Path:
    return results_dir / f"{slugify(study.study_id)}.json"


def load_study_report(path: Path) -> StudyDeviationReport:
    return StudyDeviationReport.model_validate_json(path.read_text(encoding="utf-8"))


def save_study_report(path: Path, report: StudyDeviationReport) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(report.model_dump_json(indent=2) + "\n", encoding="utf-8")


def ready_studies(study_map: StudyMap) -> list[StudyMapEntry]:
    return [
        study
        for study in study_map.studies
        if study.ready_for_deviation_check and study.matched_preregistration_file_path
    ]


def skipped_studies(study_map: StudyMap) -> list[StudyMapEntry]:
    return [
        study
        for study in study_map.studies
        if not (study.ready_for_deviation_check and study.matched_preregistration_file_path)
    ]


def run_deviation_checks(
    root: Path,
    state_dir: Path,
    study_map: StudyMap,
    guide: DeviationGuide,
    guide_path: Path,
    client: DeviationClient,
    model: str,
    force: bool = False,
    progress=None,
    cancelled=None,
) -> DeviationCheckRun:
    results_dir = state_dir / DEVIATION_RESULTS_DIRNAME
    reports: list[StudyDeviationReport] = []
    studies_to_check = ready_studies(study_map)

    for index, study in enumerate(studies_to_check, start=1):
        if cancelled and cancelled():
            raise InterruptedError("Processing was cancelled.")
        result_path = study_result_path(results_dir, study)
        if result_path.exists() and not force:
            report = load_study_report(result_path)
            if report.status == "completed":
                validate_report_consistency(report)
                if progress:
                    progress(f"Skipping {study.label} ({index}/{len(studies_to_check)}) - existing result")
                reports.append(report)
                continue

        if progress:
            progress(f"Checking {study.label} ({index}/{len(studies_to_check)})")

        try:
            report = client.check_preregistration_adherence(root, study, guide)
            report.status = "completed"
            report.generated_at = report.generated_at or datetime.now(tz=timezone.utc)
            report.model = model
            validate_report_deviation_types(report, guide)
            validate_report_consistency(report)
        except Exception as exc:
            report = StudyDeviationReport(
                study_id=study.study_id,
                study_label=study.label,
                article_file_path=study.article_file_path,
                preregistration_file_path=study.matched_preregistration_file_path,
                status="failed",
                generated_at=datetime.now(tz=timezone.utc),
                model=model,
                error=str(exc),
                review_notes=["This study failed during preregistration adherence checking."],
            )

        save_study_report(result_path, report)
        reports.append(report)

    run = DeviationCheckRun(
        generated_at=datetime.now(tz=timezone.utc),
        root=str(root.resolve()),
        model=model,
        guide_path=str(guide_path),
        study_map_path=str(state_dir / "study-map.json"),
        reports=reports,
        skipped_studies=skipped_studies(study_map),
        review_notes=build_run_review_notes(study_map, reports),
    )
    save_deviation_run(state_dir / DEVIATION_RUN_FILENAME, run)
    return run


def save_deviation_run(path: Path, run: DeviationCheckRun) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(run.model_dump_json(indent=2) + "\n", encoding="utf-8")


def validate_report_deviation_types(
    report: StudyDeviationReport,
    guide: DeviationGuide,
) -> None:
    allowed = set(guide.allowed_deviation_type_ids)
    for finding in report.deviations:
        if finding.deviation_type not in allowed:
            report.review_notes.append(
                f"Finding uses undefined deviation_type `{finding.deviation_type}`."
            )


def validate_report_consistency(report: StudyDeviationReport) -> None:
    if report.deviations:
        return
    prose = " ".join([report.summary, report.overall_assessment])
    if mentions_potential_deviations(prose):
        note = (
            "The model mentioned possible deviations in prose but returned an empty "
            "structured deviations list. Re-run this study with --force so Watson can "
            "ask for structured findings."
        )
        if note not in report.review_notes:
            report.review_notes.append(note)


def render_deviation_markdown(run: DeviationCheckRun) -> str:
    lines = [
        "# Watson Preregistration Adherence Report",
        "",
        render_report_warning(),
        "",
        f"Generated at: `{run.generated_at.isoformat()}`",
        f"Model: `{run.model}`",
        "",
        "## Summary",
        "",
        (
            f"Checked {len(run.reports)} preregistered study/studies. "
            f"{sum(1 for report in run.reports if report.status == 'failed')} failed. "
            f"{len(run.skipped_studies)} study/studies were skipped because they were not ready for deviation checking."
        ),
        "",
    ]

    if run.review_notes:
        lines.extend(["## Review Notes", ""])
        lines.extend(f"- {note}" for note in run.review_notes)
        lines.append("")

    lines.extend(["## Study Reports", ""])
    for report in run.reports:
        lines.extend(render_study_report(report))

    if run.skipped_studies:
        lines.extend(["## Skipped Studies", ""])
        for study in run.skipped_studies:
            reason = "; ".join(study.review_notes) if study.review_notes else "Not ready for deviation checking."
            lines.append(f"- {study.label}: {reason}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def save_deviation_markdown(path: Path, run: DeviationCheckRun) -> None:
    path.write_text(render_deviation_markdown(run), encoding="utf-8")


def render_report_warning() -> str:
    return (
        '<p><strong><span style="color: orange;">Warning:</span></strong> '
        "Interpret these results with caution. Watson's output should be checked "
        "manually by the user before drawing conclusions.</p>"
    )


def render_study_report(report: StudyDeviationReport) -> list[str]:
    lines = [f"### {report.study_label}", ""]
    if report.apa_citation:
        lines.extend([f"> {report.apa_citation}", ""])
    lines.extend([
        f"- Article: `{report.article_file_path}`",
        f"- Preregistration: `{report.preregistration_file_path or ''}`",
        f"- Status: `{report.status}`",
        "",
    ])

    if has_real_error(report.error):
        lines.extend([f"Error: {report.error}", ""])
        return lines

    assessment = report.overall_assessment or report.summary
    if assessment:
        lines.extend(["#### Overall Assessment", "", assessment, ""])
    if report.review_notes:
        lines.extend(["Review notes:", ""])
        lines.extend(f"- {note}" for note in report.review_notes)
        lines.append("")

    if not report.deviations:
        if mentions_potential_deviations(" ".join([report.summary, report.overall_assessment])):
            lines.extend(
                [
                    "No structured deviations were returned by the model, but the prose assessment appears to mention potential deviations.",
                    "Re-run this study with `--force` to request structured findings.",
                    "",
                ]
            )
        else:
            lines.extend(["No deviations were reported by the model.", ""])
        return lines

    lines.extend(
        [
            "<!-- Legacy column set: | Type | Confidence | Summary | -->",
            "| Type | Confidence | Disclosed | Summary |",
            "| --- | --- | --- | --- |",
        ]
    )
    for finding in report.deviations:
        lines.append(
            f"| {escape_table(markdown_escape(finding.deviation_type))} | "
            f"{escape_table(finding.confidence)} | "
            f"{escape_table(finding.disclosed)} | {escape_table(finding.summary)} |"
        )
    lines.append("")

    for number, finding in enumerate(report.deviations, start=1):
        lines.extend([f"#### Finding {number}: {markdown_escape(finding.deviation_type)}", ""])
        lines.extend(
            [
                f"- Confidence: {finding.confidence}",
                f"- Disclosed: {finding.disclosed}",
                f"- Explanation given: {finding.explanation_given}",
                f"- Robustness check: {finding.robustness_check}",
                "",
                f"Preregistered plan: {finding.preregistered_plan}",
                "",
                f"Article report: {finding.article_report}",
                "",
                f"Evidence: {finding.evidence}",
                "",
            ]
        )

    return lines


def build_run_review_notes(
    study_map: StudyMap,
    reports: list[StudyDeviationReport],
) -> list[str]:
    notes: list[str] = []
    if not ready_studies(study_map):
        notes.append("No studies were ready for deviation checking.")
    for report in reports:
        if report.status == "failed":
            notes.append(f"{report.study_label} failed during deviation checking.")
    for study in skipped_studies(study_map):
        notes.append(f"{study.label} was skipped because it is not ready for deviation checking.")
    return dedupe(notes)


def has_real_error(value: str | None) -> bool:
    if value is None:
        return False
    return value.strip().lower() not in {"", "null", "none", "n/a"}


def mentions_potential_deviations(value: str) -> bool:
    normalized = " ".join(value.lower().split())
    if not normalized:
        return False
    no_issue_phrases = [
        "no deviations",
        "no deviation",
        "no meaningful deviations",
        "no meaningful deviation",
        "no issues",
        "without deviations",
    ]
    if any(phrase in normalized for phrase in no_issue_phrases):
        return False
    issue_terms = [
        "deviation",
        "deviations",
        "omission",
        "omitted",
        "not reported",
        "not included",
        "exploratory",
        "degree of freedom",
        "degrees of freedom",
        "sensitivity analyses",
        "sensitivity analysis",
    ]
    return any(term in normalized for term in issue_terms)


def slugify(value: str) -> str:
    slug = "".join(character if character.isalnum() else "-" for character in value.lower())
    return "-".join(part for part in slug.split("-") if part) or "study"


def escape_table(value: str) -> str:
    return (value or "").replace("|", "\\|").replace("\n", " ")


def markdown_escape(value: str) -> str:
    return (value or "").replace("_", "\\_")


def dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value not in seen:
            result.append(value)
            seen.add(value)
    return result
