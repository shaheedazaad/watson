from __future__ import annotations

import threading
from pathlib import Path

import pytest

from watson.jobs import JobConflictError, JobManager
from watson.projects import ProjectStore
from watson.runner import RunnerResult, RunnerSettings


def test_job_persists_progress_and_redacts_credentials(tmp_path: Path) -> None:
    store = ProjectStore(tmp_path)
    project = store.create("Jobs")
    finished = threading.Event()

    def fake_runner(_project_dir, _settings, progress, _cancellation):
        progress.emit("test", "key=secret-value")
        finished.set()
        return RunnerResult(status="complete", summary={"checked": 1})

    manager = JobManager(store, runner=fake_runner)
    job = manager.start(project.id, RunnerSettings(api_key="secret-value"))
    assert finished.wait(2)
    thread = manager._threads.get(job.id)
    if thread:
        thread.join(2)

    saved = manager.get(project.id, job.id)
    events = manager.events(project.id, job.id)
    all_files = "".join(path.read_text(encoding="utf-8") for path in store.paths(project.id).jobs.iterdir())

    assert saved.state == "complete"
    assert events[0].message == "key=[REDACTED]"
    assert "secret-value" not in all_files


def test_simultaneous_project_runs_are_rejected(tmp_path: Path) -> None:
    store = ProjectStore(tmp_path)
    project = store.create("Lock")
    release = threading.Event()

    def waiting_runner(_project_dir, _settings, _progress, cancellation):
        release.wait(2)
        if cancellation.is_cancelled():
            from watson.runner import ProcessingCancelled
            raise ProcessingCancelled("cancelled")
        return RunnerResult(status="complete")

    manager = JobManager(store, runner=waiting_runner)
    first = manager.start(project.id, RunnerSettings(api_key="key"))

    with pytest.raises(JobConflictError):
        manager.start(project.id, RunnerSettings(api_key="key"))

    manager.cancel(project.id, first.id)
    release.set()
