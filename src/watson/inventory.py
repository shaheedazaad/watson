from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from watson.file_support import is_supported_file, supported_file_types_label, unsupported_reason
from watson.gemini_client import DEFAULT_MODEL, fallback_classification
from watson.scanner import scan_files
from watson.schemas import (
    DocumentClassification,
    DocumentType,
    GeminiFileRecord,
    InventoryResult,
    PreregistrationMatch,
    StudyMap,
    StudyMapEntry,
    StudyExtractionResult,
)


class ResearchClient(Protocol):
    upload_cache: dict[str, GeminiFileRecord]

    def classify_document(self, root: Path, file_record): ...

    def extract_studies(self, root: Path, article: DocumentClassification) -> StudyExtractionResult: ...

    def match_preregistrations(
        self,
        root: Path,
        study,
        preregistrations: list[DocumentClassification],
    ) -> PreregistrationMatch: ...


def load_upload_cache(path: Path) -> dict[str, GeminiFileRecord]:
    if not path.exists():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    return {
        item["local_path"]: GeminiFileRecord.model_validate(item)
        for item in raw.get("files", [])
    }


def save_upload_cache(path: Path, cache: dict[str, GeminiFileRecord]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {"files": [record.model_dump(mode="json") for record in cache.values()]},
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def run_inventory(
    root: Path,
    state_dir: Path,
    client: ResearchClient,
    model: str = DEFAULT_MODEL,
    progress=None,
    cancelled=None,
) -> InventoryResult:
    files = scan_files(root)
    supported_files = [file_record for file_record in files if is_supported_file(file_record)]
    unsupported_files = [file_record for file_record in files if not is_supported_file(file_record)]
    classifications: list[DocumentClassification] = []
    notes: list[str] = []

    if unsupported_files:
        notes.extend(
            f"Skipped `{file_record.path}`: {unsupported_reason(file_record)}."
            for file_record in unsupported_files
        )
    if not supported_files:
        notes.append(f"No supported files were found. {supported_file_types_label()}")

    for index, file_record in enumerate(supported_files, start=1):
        if cancelled and cancelled():
            raise InterruptedError("Processing was cancelled.")
        if progress:
            progress(f"Classifying {file_record.path} ({index}/{len(supported_files)})")
        try:
            classification = client.classify_document(root, file_record)
        except Exception as exc:
            classification = fallback_classification(file_record, f"Classification failed: {exc}")
            notes.append(f"Could not classify `{file_record.path}`: {exc}")
        classifications.append(classification)

    article = choose_primary_article(classifications)
    studies = []
    matches = []
    if article:
        if cancelled and cancelled():
            raise InterruptedError("Processing was cancelled.")
        if progress:
            progress(f"Extracting studies from {article.file_path}")
        study_result = client.extract_studies(root, article)
        studies = study_result.studies
        if not studies:
            notes.append(f"No studies were extracted from `{article.file_path}`.")
    else:
        notes.append("No clear article was found.")

    preregistrations = [
        doc for doc in classifications if doc.document_type == DocumentType.PREREGISTRATION
    ]
    for study in studies:
        if study.article_says_preregistered is False:
            continue
        if cancelled and cancelled():
            raise InterruptedError("Processing was cancelled.")
        if progress:
            progress(f"Matching preregistration for {study.label}")
        try:
            matches.append(client.match_preregistrations(root, study, preregistrations))
        except Exception as exc:
            matches.append(
                PreregistrationMatch(
                    study_id=study.study_id,
                    study_label=study.label,
                    match_status="needs_review",
                    confidence=0,
                    rationale=f"Matching failed: {exc}",
                )
            )
            notes.append(f"Could not match preregistration for `{study.label}`: {exc}")

    notes.extend(build_review_notes(classifications, studies, matches))
    return InventoryResult(
        generated_at=datetime.now(tz=timezone.utc),
        root=str(root.resolve()),
        model=model,
        files=supported_files,
        documents=classifications,
        article_file_path=article.file_path if article else None,
        studies=studies,
        preregistration_matches=matches,
        review_notes=dedupe(notes),
    )


def save_inventory(path: Path, inventory: InventoryResult) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        inventory.model_dump_json(indent=2) + "\n",
        encoding="utf-8",
    )


def build_study_map(inventory: InventoryResult) -> StudyMap:
    matches_by_study = {
        match.study_id: match for match in inventory.preregistration_matches
    }
    documents_by_type = group_document_paths_by_type(inventory.documents)
    entries: list[StudyMapEntry] = []

    for study in inventory.studies:
        match = matches_by_study.get(study.study_id)
        review_notes: list[str] = []
        matched_path = match.matched_file_path if match else None
        match_status = match.match_status if match else "none"
        match_confidence = match.confidence if match else 0
        competing_paths = match.competing_file_paths if match else []
        ready = (
            study.article_says_preregistered is True
            and match_status == "matched"
            and bool(matched_path)
        )

        if study.article_says_preregistered is True and not ready:
            review_notes.append("Study appears preregistered, but no clear preregistration file is matched.")
        if study.article_says_preregistered is None:
            review_notes.append("Article preregistration status is unclear.")
        if match_status in {"ambiguous", "needs_review"}:
            review_notes.append("Preregistration match needs user review.")

        entries.append(
            StudyMapEntry(
                study_id=study.study_id,
                label=study.label,
                description=study.description,
                article_file_path=study.article_file_path,
                article_location=study.article_location,
                article_says_preregistered=study.article_says_preregistered,
                preregistration_references=study.preregistration_references,
                matched_preregistration_file_path=matched_path,
                supplemental_material_file_paths=documents_by_type[DocumentType.SUPPLEMENTAL_MATERIAL],
                match_status=match_status,
                match_confidence=match_confidence,
                competing_preregistration_file_paths=competing_paths,
                ready_for_deviation_check=ready,
                review_notes=review_notes,
            )
        )

    return StudyMap(
        generated_at=inventory.generated_at,
        root=inventory.root,
        model=inventory.model,
        article_file_path=inventory.article_file_path,
        studies=entries,
        preregistration_file_paths=documents_by_type[DocumentType.PREREGISTRATION],
        supplemental_material_file_paths=documents_by_type[DocumentType.SUPPLEMENTAL_MATERIAL],
        review_notes=inventory.review_notes,
    )


def save_study_map(path: Path, study_map: StudyMap) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(study_map.model_dump_json(indent=2) + "\n", encoding="utf-8")


def group_document_paths_by_type(
    documents: list[DocumentClassification],
) -> dict[DocumentType, list[str]]:
    grouped = {document_type: [] for document_type in DocumentType}
    for document in documents:
        grouped[document.document_type].append(document.file_path)
    return grouped


def choose_primary_article(documents: list[DocumentClassification]) -> DocumentClassification | None:
    articles = [doc for doc in documents if doc.document_type == DocumentType.ARTICLE]
    if not articles:
        return None
    return sorted(articles, key=lambda doc: doc.confidence, reverse=True)[0]


def build_review_notes(
    documents: list[DocumentClassification],
    studies,
    matches: list[PreregistrationMatch],
) -> list[str]:
    notes: list[str] = []
    article_count = sum(1 for doc in documents if doc.document_type == DocumentType.ARTICLE)
    if article_count > 1:
        notes.append("Multiple article candidates were found; confirm which article should drive the inventory.")

    for doc in documents:
        if doc.confidence < 0.65 or doc.document_type == DocumentType.UNKNOWN:
            notes.append(f"Review classification for `{doc.file_path}`.")

    matched_by_study = {match.study_id: match for match in matches}
    for study in studies:
        match = matched_by_study.get(study.study_id)
        if study.article_says_preregistered and (not match or match.match_status != "matched"):
            notes.append(f"Review preregistration match for `{study.label}`.")
    return notes


def dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value not in seen:
            result.append(value)
            seen.add(value)
    return result
