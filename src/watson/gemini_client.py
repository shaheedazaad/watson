from __future__ import annotations

import hashlib
import json
import mimetypes
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, TypeVar

from pydantic import BaseModel, Field, ValidationError

from watson.deviation_guide import DeviationGuide, build_deviation_system_prompt
from watson.file_context import build_file_context_prompt
from watson.file_support import supported_extensions
from watson.schemas import (
    ArticleInventory,
    DegreesOfFreedomResult,
    DocumentClassification,
    DocumentType,
    FileRecord,
    GeminiFileRecord,
    InventoryDiff,
    PreregistrationInventory,
    PreregistrationMatch,
    StudyDeviationReport,
    StudyMapEntry,
    StudyExtractionResult,
    StudyRecord,
    CodeAuditAnalysis,
    CodeAuditCheck,
    CodeAuditFinding,
    CodeAuditResult,
    CodeCitation,
)
from watson.code_audit import (
    align_findings,
    constrain_analysis_scope,
    excerpts,
    manifest,
    verify,
)
from watson.scanner import hash_file
from watson.text_extractors import extract_text


DEFAULT_MODEL = "gemini-3.1-pro-preview"
UPLOAD_EXTENSIONS = supported_extensions()
DEFAULT_TEMPERATURE = 0.7
THINKING_LEVEL_OPTIONS = ("minimal", "low", "medium", "high")
DEFAULT_THINKING_LEVEL = "high"
CONTEXT_CACHE_TTL_SECONDS = 3600
FILE_ACTIVATION_TIMEOUT_SECONDS = 60
FILE_POLL_SECONDS = 2

INVENTORY_CATEGORIES = (
    "hypothesis",
    "sample_size",
    "stopping_rule",
    "exclusion_criteria",
    "measure",
    "outcome",
    "analysis_model",
    "transformation",
    "multiple_comparison_correction",
    "procedure",
    "other",
)

T = TypeVar("T", bound=BaseModel)


class GeminiError(RuntimeError):
    pass


class ContextCacheRecord(BaseModel):
    name: str
    expires_at: datetime

class CodeExcerptRequest(BaseModel):
    requests: list[CodeCitation] = Field(default_factory=list)


class CodeAuditPlan(BaseModel):
    analyses: list[CodeAuditAnalysis] = Field(default_factory=list)
    requests: list[CodeCitation] = Field(default_factory=list)


class CitedCodeAuditCheck(CodeAuditCheck):
    citations: list[CodeCitation] = Field(min_length=1)


class CitedCodeAuditFinding(CodeAuditFinding):
    manuscript_check: CitedCodeAuditCheck
    preregistration_check: CitedCodeAuditCheck


class CitedCodeAuditResult(CodeAuditResult):
    findings: list[CitedCodeAuditFinding] = Field(default_factory=list)


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
        self._context_caches: dict[str, ContextCacheRecord] = {}
        self.usage = {
            "requests": 0,
            "prompt_tokens": 0,
            "output_tokens": 0,
            "cached_tokens": 0,
            "total_tokens": 0,
        }

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

    # ------------------------------------------------------------------
    # Preregistration adherence pipeline
    # ------------------------------------------------------------------

    def check_preregistration_adherence(
        self,
        root: Path,
        study: StudyMapEntry,
        guide: DeviationGuide,
        progress: Callable[[str], None] | None = None,
    ) -> StudyDeviationReport:
        """Run the four-stage adherence pipeline for one study.

        Stage 1 inventories what the preregistration promised, stage 2 inventories
        what the article and supplements actually report, stage 3 diffs the two,
        and stage 4 audits the preregistration inventory for researcher degrees of
        freedom. Each stage is its own request; the documents are uploaded once and
        shared across all four through an explicit context cache when the API
        allows one.
        """
        if not study.matched_preregistration_file_path:
            raise GeminiError(f"{study.label} has no matched preregistration file.")

        def announce(message: str) -> None:
            if progress:
                progress(message)

        prereg_part, article_parts, supplement_paths = self._study_documents(root, study)
        cache_name = self._ensure_study_cache(study, guide, [prereg_part, *article_parts])

        stage_errors: list[str] = []

        announce("inventorying the preregistration")
        prereg_inventory = self._inventory_preregistration(
            study, guide, prereg_part, cache_name
        )

        announce("inventorying the article and supplements")
        article_inventory = self._inventory_article(
            study, guide, article_parts, supplement_paths, cache_name
        )

        announce("diffing the two inventories")
        diff = self._diff_inventories(
            study,
            guide,
            prereg_inventory,
            article_inventory,
            [prereg_part, *article_parts],
            cache_name,
        )

        announce("auditing the preregistration for degrees of freedom")
        try:
            degrees_of_freedom = self._assess_degrees_of_freedom(
                study, guide, prereg_inventory, article_inventory, prereg_part, cache_name
            )
        except GeminiError as exc:
            # The first three stages already carry the report; do not lose them.
            stage_errors.append(f"Degrees-of-freedom stage failed: {exc}")
            degrees_of_freedom = DegreesOfFreedomResult(study_id=study.study_id)

        announce("collecting the citation")
        apa_citation = self._article_citation(study, article_parts, cache_name)

        review_notes = dedupe_notes(
            [
                *prereg_inventory.notes,
                *article_inventory.notes,
                *diff.notes,
                *degrees_of_freedom.notes,
            ]
        )

        return StudyDeviationReport(
            study_id=study.study_id,
            study_label=study.label,
            article_file_path=study.article_file_path,
            preregistration_file_path=study.matched_preregistration_file_path,
            supplemental_file_paths=supplement_paths,
            apa_citation=apa_citation,
            summary=diff.summary,
            preregistration_inventory=prereg_inventory,
            article_inventory=article_inventory,
            missing_preregistered_items=diff.missing_preregistered_items,
            unregistered_article_items=diff.unregistered_article_items,
            deviations=diff.deviations,
            degrees_of_freedom=degrees_of_freedom.findings,
            overall_assessment=diff.overall_assessment or diff.summary,
            review_notes=review_notes,
            stage_errors=stage_errors,
        )

    def audit_code(
        self,
        root: Path,
        code_dir: Path,
        study: StudyMapEntry,
        guide: DeviationGuide,
        preregistration_inventory: PreregistrationInventory | None,
        article_inventory: ArticleInventory | None,
    ) -> CodeAuditResult:
        """Run three bounded audit requests; source is never executed."""
        if not study.matched_preregistration_file_path:
            raise GeminiError("Study has no matched preregistration.")
        prereg, article, supplement_paths = self._study_documents(root, study)
        cache = self._ensure_study_cache(study, guide, [prereg, *article])
        inventory_requests = 0
        if preregistration_inventory is None:
            preregistration_inventory = self._inventory_preregistration(
                study, guide, prereg, cache
            )
            inventory_requests += 1
        if article_inventory is None:
            article_inventory = self._inventory_article(
                study, guide, article, supplement_paths, cache
            )
            inventory_requests += 1
        source_manifest = manifest(code_dir)
        first_prompt = build_code_audit_plan_prompt(
            study, article_inventory, source_manifest
        )
        first = self._generate_json(first_prompt, CodeAuditPlan, cache)
        first.analyses = constrain_analysis_scope(first.analyses, article_inventory)
        access1, text1 = excerpts(code_dir, first.requests)
        second_prompt = build_code_audit_followup_prompt(first.analyses, text1)
        second = self._generate_json(second_prompt, CodeExcerptRequest, cache)
        access2, text2 = excerpts(code_dir, second.requests)
        final_prompt = build_code_audit_final_prompt(
            first.analyses,
            preregistration_inventory,
            article_inventory,
            text1,
            text2,
        )
        cited_result = self._generate_json(final_prompt, CitedCodeAuditResult, cache)
        result = CodeAuditResult.model_validate(cited_result.model_dump())
        result.study_id = study.study_id; result.study_label = study.label; result.access_log = access1 + access2
        result.resource_usage.requests = 3 + inventory_requests
        result.resource_usage.files_read = len({citation.path for citation in result.access_log})
        result.resource_usage.lines_read = sum(citation.end_line - citation.start_line + 1 for citation in result.access_log)
        result.resource_usage.excerpt_characters = len(text1) + len(text2)
        return verify(align_findings(result, first.analyses), code_dir)

    def _study_documents(
        self,
        root: Path,
        study: StudyMapEntry,
    ) -> tuple[Any, list[Any], list[str]]:
        """Upload the preregistration, article, and supplements once for this study."""
        prereg_record = self._record_for_existing_path(root, study.matched_preregistration_file_path)
        prereg_part = self._upload_file(root / study.matched_preregistration_file_path, prereg_record)

        article_record = self._record_for_existing_path(root, study.article_file_path)
        article_parts = [self._upload_file(root / study.article_file_path, article_record)]

        supplement_paths: list[str] = []
        for relative_path in study.supplemental_material_file_paths:
            path = root / relative_path
            if not path.is_file() or path.suffix.lower() not in UPLOAD_EXTENSIONS:
                continue
            try:
                record = self._record_for_existing_path(root, relative_path)
                article_parts.append(self._upload_file(path, record))
            except Exception:
                continue
            supplement_paths.append(relative_path)

        return prereg_part, article_parts, supplement_paths

    def _inventory_preregistration(
        self,
        study: StudyMapEntry,
        guide: DeviationGuide,
        prereg_part: Any,
        cache_name: str | None,
    ) -> PreregistrationInventory:
        prompt = build_preregistration_inventory_prompt(guide, study, include_guide=not cache_name)
        contents = [prompt] if cache_name else [prereg_part, prompt]
        inventory = self._generate_json(contents, PreregistrationInventory, cache_name)
        inventory.study_id = study.study_id
        inventory.study_label = study.label
        inventory.preregistration_file_path = study.matched_preregistration_file_path or ""
        return inventory

    def _inventory_article(
        self,
        study: StudyMapEntry,
        guide: DeviationGuide,
        article_parts: list[Any],
        supplement_paths: list[str],
        cache_name: str | None,
    ) -> ArticleInventory:
        prompt = build_article_inventory_prompt(
            guide, study, supplement_paths, include_guide=not cache_name
        )
        contents = [prompt] if cache_name else [*article_parts, prompt]
        inventory = self._generate_json(contents, ArticleInventory, cache_name)
        inventory.study_id = study.study_id
        inventory.study_label = study.label
        inventory.article_file_path = study.article_file_path
        inventory.supplemental_file_paths = supplement_paths
        return inventory

    def _diff_inventories(
        self,
        study: StudyMapEntry,
        guide: DeviationGuide,
        prereg_inventory: PreregistrationInventory,
        article_inventory: ArticleInventory,
        parts: list[Any],
        cache_name: str | None,
    ) -> InventoryDiff:
        prompt = build_inventory_diff_prompt(
            guide,
            study,
            prereg_inventory,
            article_inventory,
            include_guide=not cache_name,
        )
        contents = [prompt] if cache_name else [*parts, prompt]
        diff = self._generate_json(contents, InventoryDiff, cache_name)
        diff.study_id = study.study_id
        return diff

    def _assess_degrees_of_freedom(
        self,
        study: StudyMapEntry,
        guide: DeviationGuide,
        prereg_inventory: PreregistrationInventory,
        article_inventory: ArticleInventory,
        prereg_part: Any,
        cache_name: str | None,
    ) -> DegreesOfFreedomResult:
        prompt = build_degrees_of_freedom_prompt(
            guide,
            study,
            prereg_inventory,
            article_inventory,
            include_guide=not cache_name,
        )
        contents = [prompt] if cache_name else [prereg_part, prompt]
        result = self._generate_json(contents, DegreesOfFreedomResult, cache_name)
        result.study_id = study.study_id
        return result

    def _article_citation(
        self,
        study: StudyMapEntry,
        article_parts: list[Any],
        cache_name: str | None,
    ) -> str:
        prompt = (
            "Return the full APA 7th edition reference-list citation for the attached "
            f"article ({study.article_file_path}). Return only the citation text."
        )
        contents = [prompt] if cache_name else [article_parts[0], prompt]
        try:
            return self._generate_text(contents, cache_name).strip()
        except GeminiError:
            return ""

    # ------------------------------------------------------------------
    # Context caching
    # ------------------------------------------------------------------

    def _ensure_study_cache(
        self,
        study: StudyMapEntry,
        guide: DeviationGuide,
        parts: list[Any],
    ) -> str | None:
        """Cache the study's documents and the guide so the four stages share them.

        Returns the cache name, or None when the API refuses to cache this content
        (too few tokens, unsupported model) so callers fall back to attaching the
        files to every request.
        """
        key = self._cache_key(guide, parts)
        record = self._context_caches.get(key)
        now = datetime.now(tz=timezone.utc)
        if record is not None:
            if not record.name:
                return None
            if record.expires_at > now:
                return record.name

        try:
            cache = self._client.caches.create(
                model=self.model,
                config=self._types.CreateCachedContentConfig(
                    contents=list(parts),
                    systemInstruction=build_deviation_system_prompt(guide),
                    ttl=f"{CONTEXT_CACHE_TTL_SECONDS}s",
                    displayName=f"watson-{study.study_id}"[:120],
                ),
            )
        except Exception:
            # Caching is an optimisation, never a requirement. Remember the refusal
            # so the remaining stages of this study do not retry it.
            self._context_caches[key] = ContextCacheRecord(name="", expires_at=now)
            return None

        name = getattr(cache, "name", "") or ""
        expires_at = now + timedelta(seconds=CONTEXT_CACHE_TTL_SECONDS - 120)
        self._context_caches[key] = ContextCacheRecord(name=name, expires_at=expires_at)
        return name or None

    def _cache_key(self, guide: DeviationGuide, parts: list[Any]) -> str:
        digest = hashlib.sha256()
        digest.update(self.model.encode("utf-8"))
        digest.update(guide.model_dump_json().encode("utf-8"))
        for part in parts:
            digest.update(str(getattr(part, "name", part)).encode("utf-8"))
        return digest.hexdigest()

    def release_caches(self) -> None:
        """Delete the explicit context caches this client created."""
        for record in self._context_caches.values():
            if not record.name:
                continue
            try:
                self._client.caches.delete(name=record.name)
            except Exception:
                continue
        self._context_caches.clear()

    # ------------------------------------------------------------------
    # Request plumbing
    # ------------------------------------------------------------------

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

        uploaded = self._wait_until_active(self._client.files.upload(file=path))
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

    def _wait_until_active(self, uploaded: Any) -> Any:
        """Give the Files API time to finish processing a freshly uploaded document."""
        name = getattr(uploaded, "name", "")
        if not name:
            return uploaded
        deadline = time.monotonic() + FILE_ACTIVATION_TIMEOUT_SECONDS
        current = uploaded
        while _file_state(current) == "PROCESSING" and time.monotonic() < deadline:
            time.sleep(FILE_POLL_SECONDS)
            try:
                current = self._client.files.get(name=name)
            except Exception:
                return current
        return current

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

    def _generate_json(
        self,
        contents: Any,
        schema: type[T],
        cached_content: str | None = None,
    ) -> T:
        try:
            config = self._types.GenerateContentConfig(
                responseMimeType="application/json",
                responseSchema=schema,
                temperature=DEFAULT_TEMPERATURE,
                thinkingConfig=self._thinking_config(),
                cachedContent=cached_content,
            )
            response = self._client.models.generate_content(
                model=self.model,
                contents=contents,
                config=config,
            )
        except TypeError:
            config = {
                "response_mime_type": "application/json",
                "response_schema": schema.model_json_schema(),
                "temperature": DEFAULT_TEMPERATURE,
                "thinking_config": {"thinking_level": self.thinking_level},
            }
            if cached_content:
                config["cached_content"] = cached_content
            response = self._client.models.generate_content(
                model=self.model,
                contents=contents,
                config=config,
            )
        except Exception as exc:
            raise GeminiError(f"Gemini request failed: {exc}") from exc

        text = getattr(response, "text", "") or ""
        self._record_usage(response)
        try:
            return schema.model_validate_json(text)
        except ValidationError as exc:
            try:
                return schema.model_validate(json.loads(text))
            except Exception as json_exc:
                raise GeminiError(f"Gemini returned invalid structured output: {json_exc}") from exc

    def _generate_text(self, contents: Any, cached_content: str | None = None) -> str:
        try:
            config = self._types.GenerateContentConfig(
                temperature=DEFAULT_TEMPERATURE,
                thinkingConfig=self._thinking_config(),
                cachedContent=cached_content,
            )
            response = self._client.models.generate_content(
                model=self.model,
                contents=contents,
                config=config,
            )
        except TypeError:
            config = {
                "temperature": DEFAULT_TEMPERATURE,
                "thinking_config": {"thinking_level": self.thinking_level},
            }
            if cached_content:
                config["cached_content"] = cached_content
            response = self._client.models.generate_content(
                model=self.model,
                contents=contents,
                config=config,
            )
        except Exception as exc:
            raise GeminiError(f"Gemini request failed: {exc}") from exc

        text = getattr(response, "text", "") or ""
        self._record_usage(response)
        if not text.strip():
            raise GeminiError("Gemini returned an empty response.")
        return text

    def _thinking_config(self):
        return self._types.ThinkingConfig(
            thinkingLevel=getattr(self._types.ThinkingLevel, self.thinking_level.upper()),
        )

    def _record_usage(self, response: Any) -> None:
        metadata = getattr(response, "usage_metadata", None)
        self.usage["requests"] += 1
        if metadata is None:
            return
        for target, source in (
            ("prompt_tokens", "prompt_token_count"),
            ("output_tokens", "candidates_token_count"),
            ("cached_tokens", "cached_content_token_count"),
            ("total_tokens", "total_token_count"),
        ):
            value = getattr(metadata, source, 0) or 0
            if isinstance(value, int) and value >= 0:
                self.usage[target] += value


def _file_state(uploaded: Any) -> str:
    state = getattr(uploaded, "state", None)
    return str(getattr(state, "name", state) or "").upper()


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


def dedupe_notes(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        cleaned = value.strip()
        if cleaned and cleaned not in seen:
            result.append(cleaned)
            seen.add(cleaned)
    return result


def build_code_audit_plan_prompt(
    study: StudyMapEntry,
    article_inventory: ArticleInventory,
    source_manifest: list[dict[str, object]],
) -> str:
    """Build the paper-scoped analysis inventory and initial source request."""
    return f"""
Plan a source-only code audit for this study.

Study metadata:
{study.model_dump_json(indent=2)}

Authoritative article and supplemental-material inventory:
{article_inventory.model_dump_json(indent=2)}

Available source files (metadata only):
{json.dumps(source_manifest, indent=2)}

First derive the inventory of analyses reported in the article or supplemental
materials from the supplied article inventory. An analysis is a distinct
statistical/model/result-bearing computation. Group atomic inventory items that
describe the same analysis, including its outcome, exclusions, transformations,
predictors, covariates, corrections, and robustness framing. Include analyses
reported only in supplemental materials. Assign stable analysis_id values C1,
C2, and so on; copy every supporting A-item ID into article_item_ids and capture
the documentary description and evidence.

This paper-derived list is the complete scope of the audit. Do not add an
analysis merely because it appears in the code, and do not add an analysis that
appears only in the preregistration. Missing preregistered analyses and standalone
unreported code analyses are handled by the preregistration check elsewhere.

Then request precise code paths and line ranges needed to audit every listed
analysis. Return JSON matching the requested schema. Do not make findings yet.
"""


def build_code_audit_followup_prompt(
    analyses: list[CodeAuditAnalysis],
    first_excerpts: str,
) -> str:
    return f"""
The reported-analysis inventory is fixed:
{json.dumps([item.model_dump() for item in analyses], indent=2)}

Source excerpts supplied so far:
{first_excerpts or "none"}

Request only precise additional source paths and line ranges essential to answer
both implementation-fidelity questions for these analyses. Do not expand the
analysis inventory. Return JSON matching the requested schema.
"""


def build_code_audit_final_prompt(
    analyses: list[CodeAuditAnalysis],
    preregistration_inventory: PreregistrationInventory,
    article_inventory: ArticleInventory,
    first_excerpts: str,
    second_excerpts: str,
) -> str:
    return f"""
Return the final source-only code audit.

Fixed inventory of reported analyses:
{json.dumps([item.model_dump() for item in analyses], indent=2)}

Article and supplemental-material inventory:
{article_inventory.model_dump_json(indent=2)}

Preregistration inventory:
{preregistration_inventory.model_dump_json(indent=2)}

Source excerpts:
{first_excerpts or "none"}

{second_excerpts or "none"}

Return exactly one finding for every analysis in the fixed inventory, in the same
order, and no other findings. Copy its analysis object exactly. Each finding has
exactly these two independent checks:

1. manuscript_check: Did the code implement this analysis exactly as reported in
   the article or supplemental materials? Mark deviates for a conflicting or
   undisclosed implementation step that affects this reported analysis; matches
   only when the supplied source supports the reported account.
2. preregistration_check: Did the code implement this analysis exactly as
   preregistered? Mark deviates for any implemented choice or step not specified
   by, or conflicting with, the preregistration; matches only when the supplied
   source supports the preregistered account.

For each check, status must be matches, deviates, or unclear. Give a specific
rationale and at least one concise source citation. Every citation must use a
path and exact numbered line range supplied in the source excerpts. Keep ranges
focused (ideally 12 lines or fewer) and set quote to an empty string; Watson will
retrieve and verify the exact quote. A matches or deviates judgment without a
source citation is invalid and will be downgraded to unclear. If the source
excerpts are insufficient, use unclear; never infer unseen code. Do not turn a
standalone code-only analysis or a missing preregistered analysis into a finding.
Those inventory-gap checks belong to the separate preregistration check.

Return JSON matching the requested schema.
"""


# ----------------------------------------------------------------------
# Stage prompts
# ----------------------------------------------------------------------


def _guide_header(guide: DeviationGuide, include_guide: bool) -> str:
    """The guide is in the context cache when one exists, so only inline it otherwise."""
    return build_deviation_system_prompt(guide) if include_guide else ""


def _category_line() -> str:
    return ", ".join(INVENTORY_CATEGORIES)


def build_preregistration_inventory_prompt(
    guide: DeviationGuide,
    study: StudyMapEntry,
    include_guide: bool = True,
) -> str:
    return f"""
{_guide_header(guide, include_guide)}

Stage 1 of 4: inventory the preregistration.

Target study metadata:
{study.model_dump_json(indent=2)}

Read the attached preregistration ({study.matched_preregistration_file_path}) and
list every commitment the researchers made about what they would do and how they
would do it. Cover hypotheses, sample size and stopping rules, exclusion and
outlier rules, measures and how they will be scored, outcome definitions,
analysis models and their predictors and covariates, transformations, correction
procedures, and any other decision rule.

For each item:
- item_id: a stable identifier of the form P1, P2, P3, in document order.
- category: one of {_category_line()}.
- statement: what the researchers said they would do, in one sentence.
- specification: the concrete detail given — exact thresholds, model terms,
  cutoffs, instruments, numbers. Write "none given" when the preregistration
  states the intent without the detail.
- specificity: fully_specified when a reader could execute it exactly one way,
  partially_specified when some detail is given but choices remain open, and
  unspecified when only the intent is stated.
- location: the section or page of the preregistration.
- quote: a short verbatim quote carrying the commitment.

Inventory only. Do not look for problems, do not compare against any article, and
do not evaluate whether the plan was followed. Split compound sentences into
separate items when they commit to separate things. Use notes for anything that
was unreadable or ambiguous in the document itself.

Return JSON matching the requested schema.
"""


def build_article_inventory_prompt(
    guide: DeviationGuide,
    study: StudyMapEntry,
    supplement_paths: list[str],
    include_guide: bool = True,
) -> str:
    supplements = "\n".join(f"- {path}" for path in supplement_paths) or "- none"
    return f"""
{_guide_header(guide, include_guide)}

Stage 2 of 4: inventory what the researchers actually did.

Target study metadata:
{study.model_dump_json(indent=2)}

Attached documents:
- Article: {study.article_file_path}
- Supplemental materials:
{supplements}

Read the article and every supplemental document, and list everything the
researchers report actually doing for this study only. Cover the hypotheses they
say they tested, the sample they collected and analysed, every exclusion they
applied, every measure and how it was scored, every outcome they report, every
statistical model they ran including its predictors and covariates, every
transformation, and every correction procedure. Include analyses that appear only
in a supplement, a footnote, a table, or a figure note.

For each item:
- item_id: a stable identifier of the form A1, A2, A3, in reading order.
- category: one of {_category_line()}.
- statement: what the researchers did, in one sentence.
- specification: the concrete detail as executed — realised sample sizes, exact
  cutoffs applied, model terms, software or package, numbers reported.
- framing: confirmatory when the article presents it as planned, preregistered,
  primary, or hypothesis-testing; exploratory when the article labels it
  exploratory, post-hoc, or unplanned; robustness when it is presented as a
  sensitivity or robustness check; unclear when the article does not say.
- source_file_path: the file the item came from.
- location: the section, table, or page.
- quote: a short verbatim quote.

Inventory only. Do not compare against the preregistration and do not judge
anything. Where the article reports a sample size in an analysis that differs
from the sample size in the methods section, record both as separate items.

Return JSON matching the requested schema.
"""


def build_inventory_diff_prompt(
    guide: DeviationGuide,
    study: StudyMapEntry,
    prereg_inventory: PreregistrationInventory,
    article_inventory: ArticleInventory,
    include_guide: bool = True,
) -> str:
    allowed_types = ", ".join(guide.allowed_deviation_type_ids)
    return f"""
{_guide_header(guide, include_guide)}

Stage 3 of 4: diff the two inventories.

Target study metadata:
{study.model_dump_json(indent=2)}

Preregistration inventory (stage 1):
{prereg_inventory.model_dump_json(indent=2)}

Article and supplement inventory (stage 2):
{article_inventory.model_dump_json(indent=2)}

Align the two inventories item by item and return three separate lists. The
attached documents are available; consult them to confirm evidence before you
report an item, and drop any item the documents contradict.

1. missing_preregistered_items — preregistered items with no counterpart anywhere
   in the article or supplements. For each, set prereg_item_id, category, the
   preregistered_plan, searched_for describing where you looked for it, evidence,
   disclosed for whether the article acknowledges dropping it, and confidence.

2. unregistered_article_items — reported items with no counterpart in the
   preregistration. For each, set article_item_id, category, the article_report,
   framing copied from the stage 2 item, evidence, disclosed for whether the
   article labels it exploratory or unplanned, and confidence.

3. deviations — items present in both inventories but executed differently from
   the plan. For each, set prereg_item_id and article_item_id, a deviation_type
   from {allowed_types}, summary, preregistered_plan, article_report, evidence
   citing both documents, confidence, disclosed, explanation_given, and
   robustness_check.

An item counts as matched when it is the same commitment, even if the wording
differs. A matched pair executed exactly as planned belongs in none of the three
lists. Report every instance you find; there may be several of the same
deviation_type. Do not report an item in more than one list.

Set summary to a factual one-paragraph statement of what the diff found, and
overall_assessment to a short evidence-based assessment. State only what the
documents show; do not infer intent and do not judge whether the researchers did
a good or bad job.

Return JSON matching the requested schema.
"""


def build_degrees_of_freedom_prompt(
    guide: DeviationGuide,
    study: StudyMapEntry,
    prereg_inventory: PreregistrationInventory,
    article_inventory: ArticleInventory,
    include_guide: bool = True,
) -> str:
    return f"""
{_guide_header(guide, include_guide)}

Stage 4 of 4: audit the preregistration for researcher degrees of freedom.

Target study metadata:
{study.model_dump_json(indent=2)}

Preregistration inventory (stage 1):
{prereg_inventory.model_dump_json(indent=2)}

Article and supplement inventory (stage 2), for context only:
{article_inventory.model_dump_json(indent=2)}

Work through the preregistration inventory. Report every item that is
underspecified in a way that leaves the researchers a choice which could
plausibly change the results. Start with the items marked partially_specified or
unspecified, then check the fully_specified items for detail that only looks
precise.

Typical cases: an outlier rule with no definition or cutoff; an analysis named
without its predictors, covariates, or interaction terms; a measure without its
scoring or aggregation rule; a target sample size with no stopping rule; a
correction procedure named without the family it applies to; a procedure named
only by citation without the metric or threshold it implies; a numeric choice
stated without justification, so a different number would have been equally
defensible.

For each finding:
- prereg_item_id and category: copied from the stage 1 item.
- preregistered_plan: what the preregistration actually says.
- underspecification: precisely which decision is left open.
- article_choice: which one the article took, or "not reported".
- evidence: the preregistration location and quote.
- severity: high when the open choice could plausibly flip a reported
  conclusion, medium when it could move an estimate materially, low otherwise.

Judge the preregistration as written, not the researchers. An item is a finding
because the wording permits more than one defensible result, whether or not the
article exploits it. Do not report items that are fully pinned down.

Return JSON matching the requested schema.
"""
