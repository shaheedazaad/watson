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
    load_study_map,
    ready_studies,
    run_deviation_checks,
    save_deviation_markdown,
)
from watson.deviation_guide import load_deviation_guide
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
from watson.schemas import InventoryResult


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

    action: Literal["inventory", "deviation", "all"] = "all"
    model: str = DEFAULT_MODEL
    thinking_level: str = DEFAULT_THINKING_LEVEL
    api_key: SecretStr = Field(exclude=True)
    retry_mode: Literal["failed", "all"] = "failed"
    file_context: str = ""
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
            _release_context_caches(client)
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
