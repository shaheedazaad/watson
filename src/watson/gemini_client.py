from __future__ import annotations

import json
import mimetypes
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from watson.deviation_guide import DeviationGuide, build_deviation_system_prompt
from watson.file_context import build_file_context_prompt
from watson.file_support import supported_extensions
from watson.schemas import (
    DocumentClassification,
    DocumentType,
    FileRecord,
    GeminiFileRecord,
    PreregistrationMatch,
    StudyDeviationReport,
    StudyMapEntry,
    StudyExtractionResult,
    StudyRecord,
)
from watson.scanner import hash_file
from watson.text_extractors import extract_text


DEFAULT_MODEL = "gemini-3.1-pro-preview"
UPLOAD_EXTENSIONS = supported_extensions()
DEFAULT_TEMPERATURE = 0.0
THINKING_LEVEL_OPTIONS = ("minimal", "low", "medium", "high")
DEFAULT_THINKING_LEVEL = "high"

T = TypeVar("T", bound=BaseModel)


class GeminiError(RuntimeError):
    pass


class GeminiResearchClient:
    def __init__(
        self,
        api_key: str,
        model: str = DEFAULT_MODEL,
        thinking_level: str = DEFAULT_THINKING_LEVEL,
        upload_cache: dict[str, GeminiFileRecord] | None = None,
    ) -> None:
        try:
            from google import genai
            from google.genai import types
        except Exception as exc:
            raise GeminiError(
                "The google-genai package is not installed. Run `python -m pip install -e .`."
            ) from exc

        self.model = model
        self.thinking_level = normalize_thinking_level(thinking_level)
        self._types = types
        self._client = genai.Client(api_key=api_key)
        self.upload_cache = upload_cache or {}

    def validate_key(self) -> None:
        try:
            response = self._client.models.generate_content(
                model=self.model,
                contents="Return the single word ok.",
            )
        except Exception as exc:
            raise GeminiError(f"Gemini API key validation failed: {exc}") from exc
        if not getattr(response, "text", ""):
            raise GeminiError("Gemini API key validation returned an empty response.")

    def classify_document(self, root: Path, file_record: FileRecord) -> DocumentClassification:
        prompt = f"""
You are inventorying a psychology research directory.

Classify the attached document. Decide whether it is an article, preregistration,
supplemental material, data/code, other, or unknown. Be conservative when the
document is ambiguous.

Return JSON matching the requested schema. The file path is {file_record.path}.
{build_file_context_prompt(root)}
"""
        return self._generate_for_file(root, file_record, prompt, DocumentClassification)

    def extract_studies(self, root: Path, article: DocumentClassification) -> StudyExtractionResult:
        file_record = self._record_for_existing_path(root, article.file_path)
        prompt = f"""
Read the attached psychology article and identify each study or experiment.

For each study/experiment, determine whether the article says it was
preregistered. Capture any preregistration references, OSF/AsPredicted links,
dates, appendix references, and article section/page hints.

Use stable study IDs like study-1, study-2. The article file path is
{article.file_path}. Return JSON matching the requested schema.
{build_file_context_prompt(root)}
"""
        try:
            return self._generate_for_path(root, article.file_path, file_record, prompt, StudyExtractionResult)
        except GeminiError:
            result = StudyExtractionResult(
                article_file_path=article.file_path,
                studies=[],
                confidence=0,
                rationale="Study extraction failed.",
            )
            return result

    def match_preregistrations(
        self,
        root: Path,
        study: StudyRecord,
        preregistrations: list[DocumentClassification],
    ) -> PreregistrationMatch:
        candidates = [
            {
                "file_path": prereg.file_path,
                "title": prereg.title,
                "study_labels": prereg.study_labels,
                "indicators": prereg.preregistration_indicators,
                "rationale": prereg.rationale,
            }
            for prereg in preregistrations
        ]
        prompt = f"""
Match this article study to the most likely preregistration file.

Study:
{study.model_dump_json(indent=2)}

Candidate preregistration files:
{json.dumps(candidates, indent=2)}

Return one match. Use match_status="matched" only when there is a clear
correspondence. Use "ambiguous" when multiple candidates are plausible, "none"
when no candidate fits, and "needs_review" when evidence is too weak.
{build_file_context_prompt(root)}
"""
        contents: list[Any] = []
        for prereg in preregistrations:
            try:
                record = self._record_for_existing_path(root, prereg.file_path)
                contents.append(self._upload_file(root / prereg.file_path, record))
            except Exception:
                continue
        contents.append(prompt)
        return self._generate_json(contents if len(contents) > 1 else prompt, PreregistrationMatch)

    def check_preregistration_adherence(
        self,
        root: Path,
        study: StudyMapEntry,
        guide: DeviationGuide,
    ) -> StudyDeviationReport:
        if not study.matched_preregistration_file_path:
            raise GeminiError(f"{study.label} has no matched preregistration file.")

        article_record = self._record_for_existing_path(root, study.article_file_path)
        prereg_record = self._record_for_existing_path(root, study.matched_preregistration_file_path)
        prompt = build_deviation_prompt(guide, study)
        contents: list[Any] = [
            self._upload_file(root / study.article_file_path, article_record),
            self._upload_file(root / study.matched_preregistration_file_path, prereg_record),
            prompt,
        ]
        return self._generate_json(contents, StudyDeviationReport)

    def _generate_for_file(
        self,
        root: Path,
        file_record: FileRecord,
        prompt: str,
        schema: type[T],
    ) -> T:
        return self._generate_for_path(root, file_record.path, file_record, prompt, schema)

    def _generate_for_path(
        self,
        root: Path,
        relative_path: str,
        file_record: FileRecord,
        prompt: str,
        schema: type[T],
    ) -> T:
        path = root / relative_path
        if path.suffix.lower() in UPLOAD_EXTENSIONS and path.exists():
            try:
                uploaded_file = self._upload_file(path, file_record)
                return self._generate_json([uploaded_file, prompt], schema)
            except Exception:
                pass

        extracted = extract_text(path)
        if not extracted:
            raise GeminiError(f"Could not read or upload {relative_path}.")
        return self._generate_json(f"{prompt}\n\nExtracted text:\n{extracted}", schema)

    def _upload_file(self, path: Path, file_record: FileRecord) -> Any:
        cached = self.upload_cache.get(file_record.path)
        now = datetime.now(tz=timezone.utc)
        if cached and cached.sha256 == file_record.sha256 and cached.expires_at > now:
            return self._client.files.get(name=cached.name)

        uploaded = self._client.files.upload(file=path)
        uploaded_at = now
        expires_at = uploaded_at + timedelta(hours=47)
        self.upload_cache[file_record.path] = GeminiFileRecord(
            local_path=file_record.path,
            sha256=file_record.sha256,
            name=getattr(uploaded, "name", ""),
            uri=getattr(uploaded, "uri", ""),
            mime_type=getattr(uploaded, "mime_type", file_record.mime_type),
            uploaded_at=uploaded_at,
            expires_at=expires_at,
        )
        return uploaded

    def _record_for_existing_path(self, root: Path, relative_path: str) -> FileRecord:
        path = root / relative_path
        stat = path.stat()
        return FileRecord(
            path=relative_path,
            extension=path.suffix.lower(),
            mime_type=mimetypes.guess_type(path.name)[0] or "application/octet-stream",
            size_bytes=stat.st_size,
            modified_at=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
            sha256=hash_file(path),
        )

    def _generate_json(self, contents: Any, schema: type[T]) -> T:
        try:
            thinking_config = self._types.ThinkingConfig(
                thinkingLevel=getattr(self._types.ThinkingLevel, self.thinking_level.upper()),
            )
            config = self._types.GenerateContentConfig(
                responseMimeType="application/json",
                responseSchema=schema,
                temperature=DEFAULT_TEMPERATURE,
                thinkingConfig=thinking_config,
            )
            response = self._client.models.generate_content(
                model=self.model,
                contents=contents,
                config=config,
            )
        except TypeError:
            response = self._client.models.generate_content(
                model=self.model,
                contents=contents,
                config={
                    "response_mime_type": "application/json",
                    "response_schema": schema.model_json_schema(),
                    "temperature": DEFAULT_TEMPERATURE,
                    "thinking_config": {"thinking_level": self.thinking_level},
                },
            )
        except Exception as exc:
            raise GeminiError(f"Gemini request failed: {exc}") from exc

        text = getattr(response, "text", "") or ""
        try:
            return schema.model_validate_json(text)
        except ValidationError as exc:
            try:
                return schema.model_validate(json.loads(text))
            except Exception as json_exc:
                raise GeminiError(f"Gemini returned invalid structured output: {json_exc}") from exc


def fallback_classification(file_record: FileRecord, reason: str) -> DocumentClassification:
    return DocumentClassification(
        file_path=file_record.path,
        document_type=DocumentType.UNKNOWN,
        confidence=0,
        rationale=reason,
    )


def normalize_thinking_level(value: str) -> str:
    normalized = value.strip().lower()
    if normalized not in THINKING_LEVEL_OPTIONS:
        raise GeminiError(
            f"Thinking level must be one of: {', '.join(THINKING_LEVEL_OPTIONS)}"
        )
    return normalized


def build_deviation_prompt(guide: DeviationGuide, study: StudyMapEntry) -> str:
    return f"""
{build_deviation_system_prompt(guide)}

Target study metadata:
{study.model_dump_json(indent=2)}

Evaluate the attached article and preregistration for this target study only.
Return JSON matching the requested schema, using only deviation_type values from
the guide. Set study_id, study_label, article_file_path, and
preregistration_file_path exactly as provided in the target study metadata.
"""
