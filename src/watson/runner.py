from __future__ import annotations

import importlib.metadata
import json
import platform
import shutil
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from watson.config import DEFAULT_THINKING_LEVEL, THINKING_LEVEL_OPTIONS
from watson.deviation_check import (
    DEVIATION_REPORT_FILENAME,
    DEVIATION_RUN_FILENAME,
    load_deviation_run,
    load_study_map,
    ready_studies,
    run_deviation_checks,
    save_deviation_markdown,
)
from watson.deviation_guide import DeviationGuide, load_deviation_guide
from watson.file_context import save_file_context
from watson.gemini_client import DEFAULT_MODEL, GeminiResearchClient
from watson.inventory import (
    build_study_map,
    load_upload_cache,
    run_inventory,
    save_inventory,
    save_study_map,
    save_upload_cache,
)
from watson.projects import ProjectPaths
from watson.report import write_inventory_report
from watson.resources import default_deviation_guide
from watson.scanner import scan_files
from watson.schemas import CodeAuditResult, DeviationCheckRun, InventoryResult, StudyMap
from watson.code_audit import render as render_code_audit


class ProgressAdapter(Protocol):
    def emit(
        self,
        stage: str,
        message: str,
        current: int | None = None,
        total: int | None = None,
    ) -> None: ...


class CancellationSignal(Protocol):
    def is_cancelled(self) -> bool: ...


class NullProgress:
    def emit(self, stage: str, message: str, current: int | None = None, total: int | None = None) -> None:
        return None


class EventCancellationSignal:
    def __init__(self, event: threading.Event | None = None) -> None:
        self.event = event or threading.Event()

    def cancel(self) -> None:
        self.event.set()

    def is_cancelled(self) -> bool:
        return self.event.is_set()


class RunnerSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["inventory", "deviation", "code_audit", "all"] = "all"
    model: str = DEFAULT_MODEL
    thinking_level: str = DEFAULT_THINKING_LEVEL
    api_key: SecretStr = Field(exclude=True)
    retry_mode: Literal["failed", "all"] = "failed"
    file_context: str = ""
    code_audit_enabled: bool = False
    random_seed: int | None = None
    concurrency: int = Field(default=1, ge=1, le=16)

    @field_validator("model")
    @classmethod
    def validate_model(cls, value: str) -> str:
        value = value.strip()
        if not value or len(value) > 200:
            raise ValueError("Model must be a non-empty identifier of at most 200 characters.")
        return value

    @field_validator("thinking_level")
    @classmethod
    def validate_thinking(cls, value: str) -> str:
        value = value.strip().lower()
        if value not in THINKING_LEVEL_OPTIONS:
            raise ValueError(f"Thinking level must be one of: {', '.join(THINKING_LEVEL_OPTIONS)}")
        return value


class RunnerResult(BaseModel):
    status: Literal["complete", "cancelled"]
    inventory_path: str | None = None
    inventory_report_path: str | None = None
    deviation_path: str | None = None
    deviation_report_path: str | None = None
    summary: dict[str, int | str] = Field(default_factory=dict)


class ProcessingCancelled(RuntimeError):
    pass


def run_project(
    project_dir: Path,
    settings: RunnerSettings,
    progress: ProgressAdapter | None = None,
    cancellation: CancellationSignal | None = None,
    client_factory: Callable[..., object] = GeminiResearchClient,
) -> RunnerResult:
    paths = ProjectPaths(project_dir)
    paths.ensure()
    progress = progress or NullProgress()
    cancellation = cancellation or EventCancellationSignal()
    api_key = settings.api_key.get_secret_value().strip()
    if not api_key:
        raise ValueError("A Gemini API key is required.")

    if settings.file_context:
        save_file_context(paths.inputs, settings.file_context)

    upload_cache_path = paths.state / "gemini-files.json"
    client = client_factory(
        api_key=api_key,
        model=settings.model,
        thinking_level=settings.thinking_level,
        upload_cache=load_upload_cache(upload_cache_path),
    )
    result = RunnerResult(status="complete")

    try:
        _raise_if_cancelled(cancellation)
        inventory_path = paths.state / "inventory.json"
        inventory_current = _inventory_matches_inputs(inventory_path, paths.inputs)
        if settings.action in {"inventory", "all"}:
            if inventory_current and settings.retry_mode == "failed":
                inventory = InventoryResult.model_validate_json(inventory_path.read_text(encoding="utf-8"))
                study_map_path = paths.state / "study-map.json"
                inventory_report_path = paths.outputs / "watson-inventory-report.md"
                if not study_map_path.exists():
                    save_study_map(study_map_path, build_study_map(inventory))
                if not inventory_report_path.exists():
                    write_inventory_report(inventory_report_path, inventory)
                progress.emit("inventory", "Using the current completed inventory")
            else:
                if inventory_path.exists():
                    _clear_deviation_results(paths)
                progress.emit("inventory", "Starting document inventory")
                inventory = run_inventory(
                    root=paths.inputs,
                    state_dir=paths.state,
                    client=client,
                    model=settings.model,
                    progress=lambda message: progress.emit("inventory", message),
                    cancelled=cancellation.is_cancelled,
                )
                study_map_path = paths.state / "study-map.json"
                inventory_report_path = paths.outputs / "watson-inventory-report.md"
                save_inventory(inventory_path, inventory)
                save_study_map(study_map_path, build_study_map(inventory))
                save_upload_cache(upload_cache_path, client.upload_cache)
                write_inventory_report(inventory_report_path, inventory)
                progress.emit("inventory", "Document inventory complete")
            study_map_path = paths.state / "study-map.json"
            inventory_report_path = paths.outputs / "watson-inventory-report.md"
            result.inventory_path = str(inventory_path)
            result.inventory_report_path = str(inventory_report_path)
            result.summary.update(
                files=len(inventory.files),
                studies=len(inventory.studies),
                warnings=len(inventory.review_notes),
            )

        deviation_run: DeviationCheckRun | None = None
        study_map: StudyMap | None = None
        guide = None

        _raise_if_cancelled(cancellation)
        if settings.action in {"deviation", "all"}:
            study_map_path = paths.state / "study-map.json"
            if not study_map_path.exists():
                raise ValueError("No inventory exists. Run inventory first.")
            if settings.action == "deviation" and not inventory_current:
                raise ValueError("Inputs changed after the inventory. Run inventory before preregistration checks.")
            if settings.retry_mode == "all":
                _clear_deviation_results(paths)
            study_map = load_study_map(study_map_path)
            if not ready_studies(study_map):
                progress.emit("deviation", "No studies are ready for preregistration checking")
            with default_deviation_guide() as guide_path:
                guide = load_deviation_guide(guide_path)
                progress.emit("deviation", "Starting preregistration checks")
                deviation_run = run_deviation_checks(
                    root=paths.inputs,
                    state_dir=paths.state,
                    study_map=study_map,
                    guide=guide,
                    guide_path=guide_path,
                    client=client,
                    model=settings.model,
                    force=settings.retry_mode == "all",
                    progress=lambda message: progress.emit("deviation", message),
                    cancelled=cancellation.is_cancelled,
                )
            save_upload_cache(upload_cache_path, client.upload_cache)
            deviation_report_path = paths.outputs / DEVIATION_REPORT_FILENAME
            save_deviation_markdown(deviation_report_path, deviation_run)
            result.deviation_path = str(paths.state / DEVIATION_RUN_FILENAME)
            result.deviation_report_path = str(deviation_report_path)
            result.summary.update(
                checked=len(deviation_run.reports),
                failed=sum(report.status == "failed" for report in deviation_run.reports),
                skipped=len(deviation_run.skipped_studies),
            )
            progress.emit("deviation", "Preregistration checks complete")

        if settings.action == "code_audit":
            if not inventory_current:
                raise ValueError(
                    "Inputs changed after the inventory. Run inventory and preregistration checks before code audit."
                )
            study_map_path = paths.state / "study-map.json"
            deviation_run_path = paths.state / DEVIATION_RUN_FILENAME
            if not study_map_path.is_file() or not deviation_run_path.is_file():
                raise ValueError(
                    "Code audit requires completed inventory and preregistration checks."
                )
            study_map = load_study_map(study_map_path)
            deviation_run = load_deviation_run(deviation_run_path)
            with default_deviation_guide() as guide_path:
                guide = load_deviation_guide(guide_path)

        should_run_code_audit = settings.action == "code_audit" or (
            settings.code_audit_enabled and settings.action in {"deviation", "all"}
        )
        if should_run_code_audit:
            if study_map is None or deviation_run is None or guide is None:
                raise ValueError(
                    "Code audit requires completed paper and preregistration inventories."
                )
            code_audit_path = paths.state / "code-audit.json"
            existing_audits = (
                _load_code_audits(code_audit_path)
                if settings.retry_mode == "failed"
                else {}
            )
            audit_results = _run_code_audits(
                paths,
                study_map,
                deviation_run,
                guide,
                client,
                cancellation,
                progress,
                existing_audits,
                force=settings.retry_mode == "all",
            )
            code_audit_path.write_text(json.dumps([item.model_dump(mode="json") for item in audit_results], indent=2) + "\n", encoding="utf-8")
            (paths.outputs / "watson-code-audit-report.md").write_text(render_code_audit(audit_results), encoding="utf-8")
            save_upload_cache(upload_cache_path, client.upload_cache)
            result.summary["code_audits"] = len(audit_results)

        if settings.action in {"deviation", "code_audit", "all"}:
            _release_context_caches(client)
    except InterruptedError as exc:
        progress.emit("cancelled", "Processing cancelled")
        raise ProcessingCancelled(str(exc)) from exc

    usage = getattr(client, "usage", {})
    safe_usage: dict[str, int] = {}
    if isinstance(usage, dict):
        for key in ("requests", "prompt_tokens", "output_tokens", "cached_tokens", "total_tokens"):
            value = usage.get(key)
            if isinstance(value, int) and value >= 0:
                result.summary[key] = value
                safe_usage[key] = value
    _write_reproducibility(paths, settings, safe_usage)
    progress.emit("complete", "Run complete")
    return result


def _run_code_audits(
    paths: ProjectPaths,
    study_map: StudyMap,
    deviation_run: DeviationCheckRun,
    guide: DeviationGuide,
    client: object,
    cancellation: CancellationSignal,
    progress: ProgressAdapter,
    existing_audits: dict[str, CodeAuditResult] | None = None,
    *,
    force: bool = False,
) -> list[CodeAuditResult]:
    """Audit only the reported-analysis inventories from a completed reg check."""
    results: list[CodeAuditResult] = []
    existing_audits = existing_audits or {}
    deviation_reports = {
        report.study_id: report for report in deviation_run.reports
    }
    for study in ready_studies(study_map):
        _raise_if_cancelled(cancellation)
        existing = existing_audits.get(study.study_id)
        if (
            not force
            and existing is not None
            and existing.status in {"complete", "completed"}
        ):
            progress.emit("code_audit", f"Skipping {study.label} - existing result")
            results.append(existing)
            continue
        try:
            progress.emit("code_audit", f"Auditing code for {study.label}")
            deviation_report = deviation_reports.get(study.study_id)
            if deviation_report is None:
                raise ValueError(
                    "Code audit requires a completed preregistration check for this study."
                )
            audit = getattr(client, "audit_code")(
                paths.inputs,
                paths.code,
                study,
                guide,
                deviation_report.preregistration_inventory,
                deviation_report.article_inventory,
            )
        except Exception as exc:
            audit = CodeAuditResult(
                study_id=study.study_id,
                study_label=study.label,
                status="failed",
                error=str(exc),
            )
        results.append(audit)
    return results


def _load_code_audits(path: Path) -> dict[str, CodeAuditResult]:
    if not path.is_file():
        return {}
    try:
        values = json.loads(path.read_text(encoding="utf-8"))
        audits = [CodeAuditResult.model_validate(value) for value in values]
    except (OSError, ValueError, TypeError):
        return {}
    return {audit.study_id: audit for audit in audits}


def _release_context_caches(client: object) -> None:
    """Explicit Gemini caches bill per hour, so drop them once the run is done."""
    release = getattr(client, "release_caches", None)
    if callable(release):
        try:
            release()
        except Exception:
            pass


def _raise_if_cancelled(signal: CancellationSignal) -> None:
    if signal.is_cancelled():
        raise ProcessingCancelled("Processing was cancelled.")


def _write_reproducibility(paths: ProjectPaths, settings: RunnerSettings, usage: dict) -> None:
    packages = {}
    for name in ("watson-research-cli", "google-genai", "pydantic", "python"):
        if name == "python":
            packages[name] = platform.python_version()
            continue
        try:
            packages[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            packages[name] = "editable"
    value = {
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "model": settings.model,
        "thinking_level": settings.thinking_level,
        "random_seed": settings.random_seed,
        "concurrency": settings.concurrency,
        "resource_usage": usage,
        "packages": packages,
        "platform": platform.platform(),
    }
    path = paths.outputs / "reproducibility.json"
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def _inventory_matches_inputs(inventory_path: Path, inputs_dir: Path) -> bool:
    if not inventory_path.is_file():
        return False
    try:
        inventory = InventoryResult.model_validate_json(inventory_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    saved = {(record.path, record.sha256) for record in inventory.files}
    current = {(record.path, record.sha256) for record in scan_files(inputs_dir)}
    return saved == current


def _clear_deviation_results(paths: ProjectPaths) -> None:
    results = paths.state / "deviation-results"
    if results.exists():
        shutil.rmtree(results)
    for path in (
        paths.state / DEVIATION_RUN_FILENAME,
        paths.outputs / DEVIATION_REPORT_FILENAME,
    ):
        if path.exists():
            path.unlink()
    _clear_code_audit_results(paths)


def _clear_code_audit_results(paths: ProjectPaths) -> None:
    for path in (
        paths.state / "code-audit.json",
        paths.outputs / "watson-code-audit-report.md",
    ):
        if path.exists():
            path.unlink()
