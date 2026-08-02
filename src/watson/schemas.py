from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, field_validator


class DocumentType(str, Enum):
    ARTICLE = "article"
    PREREGISTRATION = "preregistration"
    SUPPLEMENTAL_MATERIAL = "supplemental_material"
    DATA_OR_CODE = "data_or_code"
    OTHER = "other"
    UNKNOWN = "unknown"


class FileRecord(BaseModel):
    path: str
    extension: str
    mime_type: str
    size_bytes: int
    modified_at: datetime
    sha256: str


class EvidenceHint(BaseModel):
    quote_or_detail: str = ""
    location: str = ""


class DocumentClassification(BaseModel):
    file_path: str
    document_type: DocumentType
    title: str = ""
    authors: list[str] = Field(default_factory=list)
    study_labels: list[str] = Field(default_factory=list)
    preregistration_indicators: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1)
    rationale: str = ""
    evidence: list[EvidenceHint] = Field(default_factory=list)


class StudyRecord(BaseModel):
    study_id: str
    label: str
    description: str = ""
    article_file_path: str
    article_location: str = ""
    article_says_preregistered: bool | None = None
    preregistration_references: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1)
    rationale: str = ""


class StudyExtractionResult(BaseModel):
    article_file_path: str
    studies: list[StudyRecord] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1)
    rationale: str = ""


class PreregistrationMatch(BaseModel):
    study_id: str
    study_label: str
    matched_file_path: str | None = None
    match_status: str = Field(
        default="none",
        description="One of matched, ambiguous, none, or needs_review.",
    )
    confidence: float = Field(ge=0, le=1)
    rationale: str = ""
    competing_file_paths: list[str] = Field(default_factory=list)


class GeminiFileRecord(BaseModel):
    local_path: str
    sha256: str
    name: str = ""
    uri: str = ""
    mime_type: str = ""
    uploaded_at: datetime
    expires_at: datetime


class InventoryResult(BaseModel):
    generated_at: datetime
    root: str
    model: str
    files: list[FileRecord]
    documents: list[DocumentClassification]
    article_file_path: str | None = None
    studies: list[StudyRecord] = Field(default_factory=list)
    preregistration_matches: list[PreregistrationMatch] = Field(default_factory=list)
    review_notes: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class StudyMapEntry(BaseModel):
    study_id: str
    label: str
    description: str = ""
    article_file_path: str
    article_location: str = ""
    article_says_preregistered: bool | None = None
    preregistration_references: list[str] = Field(default_factory=list)
    matched_preregistration_file_path: str | None = None
    match_status: str = "none"
    match_confidence: float = Field(default=0, ge=0, le=1)
    competing_preregistration_file_paths: list[str] = Field(default_factory=list)
    ready_for_deviation_check: bool = False
    review_notes: list[str] = Field(default_factory=list)


class StudyMap(BaseModel):
    generated_at: datetime
    root: str
    model: str
    article_file_path: str | None = None
    studies: list[StudyMapEntry] = Field(default_factory=list)
    preregistration_file_paths: list[str] = Field(default_factory=list)
    supplemental_material_file_paths: list[str] = Field(default_factory=list)
    review_notes: list[str] = Field(default_factory=list)


class DeviationFinding(BaseModel):
    deviation_type: str
    summary: str
    preregistered_plan: str
    article_report: str
    evidence: str
    confidence: str
    disclosed: str = ""
    explanation_given: str = ""
    robustness_check: str = ""


class StudyDeviationReport(BaseModel):
    study_id: str
    study_label: str
    article_file_path: str
    preregistration_file_path: str | None = None
    apa_citation: str = ""
    status: str = "completed"
    generated_at: datetime | None = None
    model: str = ""
    summary: str = ""
    deviations: list[DeviationFinding] = Field(default_factory=list)
    overall_assessment: str = ""
    review_notes: list[str] = Field(default_factory=list)
    error: str = ""

    @field_validator("error", mode="before")
    @classmethod
    def normalize_error(cls, value) -> str:
        if value is None:
            return ""
        if isinstance(value, str) and value.strip().lower() in {"", "null", "none", "n/a"}:
            return ""
        return str(value)


class DeviationCheckRun(BaseModel):
    generated_at: datetime
    root: str
    model: str
    guide_path: str
    study_map_path: str
    reports: list[StudyDeviationReport] = Field(default_factory=list)
    skipped_studies: list[StudyMapEntry] = Field(default_factory=list)
    review_notes: list[str] = Field(default_factory=list)
