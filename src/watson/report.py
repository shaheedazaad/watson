from __future__ import annotations

from pathlib import Path

from watson.schemas import DocumentClassification, InventoryResult, PreregistrationMatch, StudyRecord


def render_inventory_report(inventory: InventoryResult) -> str:
    lines = [
        "# Watson Inventory Report",
        "",
        render_summary(inventory),
        "",
        "## Documents",
        "",
        "| File | Type | Confidence | Rationale |",
        "| --- | --- | ---: | --- |",
    ]

    for document in inventory.documents:
        lines.append(
            f"| `{document.file_path}` | {document.document_type.value} | "
            f"{document.confidence:.2f} | {escape_table(document.rationale)} |"
        )

    lines.extend(
        [
            "",
            "## Studies And Preregistration Matches",
            "",
            "| Study | Article location | Article says preregistered | Matched preregistration | Match status | Confidence |",
            "| --- | --- | --- | --- | --- | ---: |",
        ]
    )

    matches = {match.study_id: match for match in inventory.preregistration_matches}
    for study in inventory.studies:
        match = matches.get(study.study_id)
        lines.append(render_study_row(study, match))

    if not inventory.studies:
        lines.append("| No studies detected |  |  |  | needs_review | 0.00 |")

    lines.extend(["", "## Needs User Review", ""])
    if inventory.review_notes:
        for note in inventory.review_notes:
            lines.append(f"- {note}")
    else:
        lines.append("- No review notes were generated.")

    lines.extend(
        [
            "",
            "## Run Metadata",
            "",
            f"- Generated at: `{inventory.generated_at.isoformat()}`",
            f"- Root: `{inventory.root}`",
            f"- Model: `{inventory.model}`",
        ]
    )
    return "\n".join(lines) + "\n"


def write_inventory_report(path: Path, inventory: InventoryResult) -> None:
    path.write_text(render_inventory_report(inventory), encoding="utf-8")


def render_summary(inventory: InventoryResult) -> str:
    study_count = len(inventory.studies)
    preregistered = [
        study for study in inventory.studies if study.article_says_preregistered is True
    ]
    matches = [
        match
        for match in inventory.preregistration_matches
        if match.match_status == "matched" and match.matched_file_path
    ]
    matched_labels = ", ".join(
        f"{match.study_label} (`{match.matched_file_path}`)" for match in matches
    )
    if not matched_labels:
        matched_labels = "none"

    return (
        f"Watson detected {study_count} study/experiment record(s). "
        f"{len(preregistered)} are described in the article as preregistered. "
        f"{len(matches)} have an associated preregistration file: {matched_labels}."
    )


def render_study_row(study: StudyRecord, match: PreregistrationMatch | None) -> str:
    if match:
        matched_file = f"`{match.matched_file_path}`" if match.matched_file_path else ""
        status = match.match_status
        confidence = match.confidence
    else:
        matched_file = ""
        status = "none"
        confidence = 0

    preregistered = (
        "yes"
        if study.article_says_preregistered is True
        else "no"
        if study.article_says_preregistered is False
        else "unclear"
    )
    return (
        f"| {escape_table(study.label)} | {escape_table(study.article_location)} | "
        f"{preregistered} | {matched_file} | {status} | {confidence:.2f} |"
    )


def escape_table(value: str) -> str:
    return (value or "").replace("|", "\\|").replace("\n", " ")
