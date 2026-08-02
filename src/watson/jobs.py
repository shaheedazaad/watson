from __future__ import annotations

import json
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Literal

from pydantic import BaseModel, ConfigDict, Field

from watson.projects import ProjectPaths, ProjectStore
from watson.runner import EventCancellationSignal, ProcessingCancelled, RunnerResult, RunnerSettings, run_project


ACTIVE_JOB_STATES = {"queued", "running", "cancelling"}
TERMINAL_JOB_STATES = {"complete", "failed", "cancelled"}


class JobRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    project_id: str
    state: Literal["queued", "running", "complete", "failed", "cancelling", "cancelled"]
    action: str
    created_at: datetime
    updated_at: datetime
    message: str = ""
    error: str = ""
    result: dict = Field(default_factory=dict)


class ProgressEvent(BaseModel):
    id: int
    timestamp: datetime
    stage: str
    message: str
    current: int | None = None
    total: int | None = None
    state: str


class JobConflictError(RuntimeError):
    pass


class JobNotFoundError(RuntimeError):
    pass


class JobManager:
    def __init__(
        self,
        store: ProjectStore,
        runner: Callable[..., RunnerResult] = run_project,
    ) -> None:
        self.store = store
        self.runner = runner
        self._signals: dict[str, EventCancellationSignal] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._lock = threading.RLock()
        self._mark_interrupted_jobs()

    def start(self, project_id: str, settings: RunnerSettings) -> JobRecord:
        paths = self.store.paths(project_id)
        with self._lock:
            active = self.active_for_project(project_id)
            if active:
                raise JobConflictError("This project already has an active run.")
            now = _now()
            job = JobRecord(
                id=uuid.uuid4().hex,
                project_id=project_id,
                state="queued",
                action=settings.action,
                created_at=now,
                updated_at=now,
                message="Run queued",
            )
            self._save(paths, job)
            signal = EventCancellationSignal()
            self._signals[job.id] = signal
            thread = threading.Thread(
                target=self._execute,
                name=f"watson-job-{job.id[:8]}",
                args=(paths, job.id, settings, signal),
                daemon=True,
            )
            self._threads[job.id] = thread
            thread.start()
            return job

    def get(self, project_id: str, job_id: str) -> JobRecord:
        if not re.fullmatch(r"[0-9a-f]{32}", job_id):
            raise JobNotFoundError("Run not found.")
        path = self.store.paths(project_id).jobs / f"{job_id}.json"
        if not path.is_file():
            raise JobNotFoundError("Run not found.")
        return JobRecord.model_validate_json(path.read_text(encoding="utf-8"))

    def list(self, project_id: str, limit: int = 20) -> list[JobRecord]:
        jobs = []
        for path in self.store.paths(project_id).jobs.glob("*.json"):
            try:
                jobs.append(JobRecord.model_validate_json(path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue
        return sorted(jobs, key=lambda job: job.created_at, reverse=True)[:limit]

    def active_for_project(self, project_id: str) -> JobRecord | None:
        return next((job for job in self.list(project_id) if job.state in ACTIVE_JOB_STATES), None)

    def cancel(self, project_id: str, job_id: str) -> JobRecord:
        with self._lock:
            job = self.get(project_id, job_id)
            if job.state not in {"queued", "running"}:
                return job
            job.state = "cancelling"
            job.message = "Cancellation requested"
            job.updated_at = _now()
            self._save(self.store.paths(project_id), job)
            signal = self._signals.get(job_id)
            if signal:
                signal.cancel()
            return job

    def events(self, project_id: str, job_id: str, after: int = 0) -> list[ProgressEvent]:
        self.get(project_id, job_id)
        path = self.store.paths(project_id).jobs / f"{job_id}.events.jsonl"
        if not path.exists():
            return []
        events = []
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                event = ProgressEvent.model_validate_json(line)
                if event.id > after:
                    events.append(event)
            except ValueError:
                continue
        return events

    def _execute(
        self,
        paths: ProjectPaths,
        job_id: str,
        settings: RunnerSettings,
        signal: EventCancellationSignal,
    ) -> None:
        secret = settings.api_key.get_secret_value()
        self._update(paths, job_id, state="running", message="Run started")
        adapter = _JobProgress(self, paths, job_id, secret)
        try:
            result = self.runner(paths.root, settings, adapter, signal)
        except ProcessingCancelled:
            self._update(paths, job_id, state="cancelled", message="Run cancelled")
            adapter.emit("cancelled", "Run cancelled")
        except Exception as exc:
            error = redact(str(exc), secret)
            self._update(paths, job_id, state="failed", message="Run failed", error=error)
            adapter.emit("failed", error)
        else:
            self._update(
                paths,
                job_id,
                state="complete",
                message="Run complete",
                result=result.model_dump(mode="json"),
            )
        finally:
            with self._lock:
                self._signals.pop(job_id, None)
                self._threads.pop(job_id, None)
            try:
                self.store.touch(paths.root.name)
            except Exception:
                pass

    def _update(self, paths: ProjectPaths, job_id: str, **changes) -> JobRecord:
        with self._lock:
            job = JobRecord.model_validate_json((paths.jobs / f"{job_id}.json").read_text(encoding="utf-8"))
            for key, value in changes.items():
                setattr(job, key, value)
            job.updated_at = _now()
            self._save(paths, job)
            return job

    def _save(self, paths: ProjectPaths, job: JobRecord) -> None:
        path = paths.jobs / f"{job.id}.json"
        temporary = path.with_suffix(".tmp")
        temporary.write_text(job.model_dump_json(indent=2) + "\n", encoding="utf-8")
        temporary.replace(path)

    def _append_event(
        self,
        paths: ProjectPaths,
        job_id: str,
        stage: str,
        message: str,
        current: int | None,
        total: int | None,
    ) -> None:
        with self._lock:
            job = self.get(paths.root.name, job_id)
            path = paths.jobs / f"{job_id}.events.jsonl"
            existing_count = 0
            if path.exists():
                with path.open("rb") as handle:
                    existing_count = sum(1 for _ in handle)
            event = ProgressEvent(
                id=existing_count + 1,
                timestamp=_now(),
                stage=stage,
                message=message,
                current=current,
                total=total,
                state=job.state,
            )
            with path.open("a", encoding="utf-8") as handle:
                handle.write(event.model_dump_json() + "\n")
            self._update(paths, job_id, message=message)

    def _mark_interrupted_jobs(self) -> None:
        if not self.store.projects_dir.exists():
            return
        for metadata in self.store.projects_dir.glob("*/project.json"):
            project_id = metadata.parent.name
            try:
                for job in self.list(project_id):
                    if job.state in ACTIVE_JOB_STATES:
                        self._update(
                            ProjectPaths(metadata.parent),
                            job.id,
                            state="failed",
                            message="Run interrupted by application restart",
                            error="The application stopped before this run finished. Retry to continue missing work.",
                        )
            except Exception:
                continue


class _JobProgress:
    def __init__(self, manager: JobManager, paths: ProjectPaths, job_id: str, secret: str) -> None:
        self.manager = manager
        self.paths = paths
        self.job_id = job_id
        self.secret = secret

    def emit(self, stage: str, message: str, current: int | None = None, total: int | None = None) -> None:
        self.manager._append_event(
            self.paths,
            self.job_id,
            stage,
            redact(message, self.secret),
            current,
            total,
        )


def redact(value: str, secret: str = "") -> str:
    redacted = value
    if secret:
        redacted = redacted.replace(secret, "[REDACTED]")
    redacted = re.sub(r"(?i)(api[_ -]?key\s*[:=]\s*)\S+", r"\1[REDACTED]", redacted)
    redacted = re.sub(r"AIza[0-9A-Za-z_-]{20,}", "[REDACTED]", redacted)
    return redacted[:4000]


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)
