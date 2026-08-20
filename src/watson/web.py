from __future__ import annotations

import asyncio
import hmac
import json
import math
import os
import secrets
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape
from pydantic import ValidationError
from starlette.background import BackgroundTask
from starlette.datastructures import UploadFile

from watson.config import ConfigStore, CredentialStoreError, system_credential_store_name
from watson.deviation_check import DEVIATION_RUN_FILENAME
from watson.file_support import supported_file_types_label
from watson.gemini_client import DEFAULT_MODEL
from watson.jobs import ACTIVE_JOB_STATES, JobConflictError, JobManager, JobNotFoundError
from watson.projects import (
    MAX_FILE_BYTES,
    MAX_REQUEST_BYTES,
    ProjectError,
    ProjectNotFoundError,
    ProjectSettings,
    ProjectStore,
    SettingsInvalidationRequired,
    UploadTooLargeError,
)
from watson.resources import assert_runtime_assets, rebuild_reports_script, static_dir, templates_dir
from watson.runner import RunnerSettings


def create_app(
    *,
    token: str | None = None,
    data_dir: Path | None = None,
    store: ProjectStore | None = None,
    jobs: JobManager | None = None,
) -> FastAPI:
    assert_runtime_assets()
    session_token = token or secrets.token_urlsafe(32)
    project_store = store or ProjectStore(data_dir)
    job_manager = jobs or JobManager(project_store)
    config_store = ConfigStore(project_store.data_dir)
    templates = Environment(
        loader=FileSystemLoader(str(templates_dir())),
        autoescape=select_autoescape(("html", "xml")),
    )
    app = FastAPI(
        title="Watson",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.session_token = session_token
    app.state.project_store = project_store
    app.state.jobs = job_manager
    app.state.config = config_store

    @app.middleware("http")
    async def local_security(request: Request, call_next):
        host = request.headers.get("host", "")
        hostname = _host_name(host)
        if hostname not in {"127.0.0.1", "localhost", "::1"}:
            return JSONResponse({"detail": "Invalid host."}, status_code=400)
        path_token = request.url.path.lstrip("/").split("/", 1)[0]
        if not hmac.compare_digest(path_token, session_token):
            return JSONResponse({"detail": "Not found."}, status_code=404)
        fetch_site = request.headers.get("sec-fetch-site", "").lower()
        origin = request.headers.get("origin")
        if fetch_site == "cross-site" and (not origin or not _trusted_browser_origin(origin, host, fetch_site)):
            return JSONResponse({"detail": "Cross-site request rejected."}, status_code=403)
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            content_length = request.headers.get("content-length")
            if content_length:
                try:
                    if int(content_length) > MAX_REQUEST_BYTES:
                        return JSONResponse({"detail": "Request is too large."}, status_code=413)
                except ValueError:
                    return JSONResponse({"detail": "Invalid Content-Length."}, status_code=400)
            if origin and not _trusted_browser_origin(origin, host, fetch_site):
                return JSONResponse({"detail": "Cross-origin request rejected."}, status_code=403)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; style-src 'self'; script-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        )
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        return response

    def url(path: str = "") -> str:
        return f"/{session_token}/{path.lstrip('/')}"

    def render(name: str, request: Request, status_code: int = 200, **context) -> HTMLResponse:
        context.update(request=request, token=session_token, url=url)
        return HTMLResponse(templates.get_template(name).render(**context), status_code=status_code)

    @app.get(f"/{session_token}/", response_class=HTMLResponse)
    async def home(request: Request, q: str = "", page: int = 1):
        per_page = 20
        projects, total = project_store.list(q, page, per_page)
        return render(
            "home.html",
            request,
            projects=projects,
            query=q,
            page=max(1, page),
            pages=max(1, math.ceil(total / per_page)),
            total=total,
            credential_state=config_store.get_credential_state(),
        )

    @app.post(f"/{session_token}/projects")
    async def create_project(request: Request):
        form = await request.form(max_fields=10)
        try:
            project = project_store.create(str(form.get("name", "")))
        except ProjectError as exc:
            projects, total = project_store.list()
            return render(
                "home.html",
                request,
                status_code=422,
                projects=projects,
                query="",
                page=1,
                pages=max(1, math.ceil(total / 20)),
                total=total,
                credential_state=config_store.get_credential_state(),
                error=str(exc),
            )
        return RedirectResponse(url(f"projects/{project.id}"), status_code=303)

    @app.get(f"/{session_token}/projects/{{project_id}}", response_class=HTMLResponse)
    async def project_page(request: Request, project_id: str, notice: str = "", error: str = "", item_page: int = 1):
        try:
            project = project_store.get(project_id)
            settings = project_store.get_settings(project_id)
            inputs = project_store.list_inputs(project_id)
            paths = project_store.paths(project_id)
        except ProjectNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        jobs_list = job_manager.list(project_id)
        active_job = next((job for job in jobs_list if job.state in ACTIVE_JOB_STATES), None)
        summary = _load_project_summary(paths)
        per_page = 25
        item_page = max(1, item_page)
        start = (item_page - 1) * per_page
        return render(
            "project.html",
            request,
            project=project,
            settings=settings,
            inputs=inputs,
            items=summary["items"][start : start + per_page],
            item_page=item_page,
            item_pages=max(1, math.ceil(len(summary["items"]) / per_page)),
            summary=summary,
            jobs=jobs_list,
            active_job=active_job,
            notice=notice,
            error=error,
            supported_types=supported_file_types_label(),
            max_file_mb=MAX_FILE_BYTES // (1024 * 1024),
            credential_loaded=config_store.get_credential_state().session_loaded,
            model=config_store.get_default_model(DEFAULT_MODEL),
        )

    @app.get(f"/{session_token}/projects/{{project_id}}/reports/{{kind}}", response_class=HTMLResponse)
    async def report_page(request: Request, project_id: str, kind: str):
        try:
            project = project_store.get(project_id)
            paths = project_store.paths(project_id)
            report = _load_browser_report(paths, kind)
        except ProjectNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if report is None:
            raise HTTPException(status_code=404, detail="Report not found.")
        return render(
            "report.html",
            request,
            project=project,
            report=report,
            available_reports=_load_project_summary(paths)["reports"],
        )

    @app.post(f"/{session_token}/projects/{{project_id}}/inputs")
    async def upload_inputs(request: Request, project_id: str):
        records = []
        try:
            project_store.get(project_id)
            if job_manager.active_for_project(project_id):
                raise JobConflictError("Inputs cannot change while a run is active.")
            form = await request.form(max_files=100, max_fields=20, max_part_size=MAX_FILE_BYTES)
            uploads = [value for _, value in form.multi_items() if isinstance(value, UploadFile)]
            if not uploads:
                raise ProjectError("Choose at least one input file.")
            total_size = sum(upload.size or 0 for upload in uploads)
            if total_size > MAX_REQUEST_BYTES:
                raise UploadTooLargeError(
                    f"Upload exceeds the {MAX_REQUEST_BYTES // (1024 * 1024)} MB request limit."
                )
            for upload in uploads:
                records.append(project_store.add_stream(project_id, upload.filename or "", upload.file))
            if sum(record.size_bytes for record in records) > MAX_REQUEST_BYTES:
                raise UploadTooLargeError(
                    f"Upload exceeds the {MAX_REQUEST_BYTES // (1024 * 1024)} MB request limit."
                )
        except (ProjectError, JobConflictError, UploadTooLargeError) as exc:
            for record in records:
                try:
                    project_store.remove_input(project_id, record.name)
                except ProjectError:
                    pass
            return RedirectResponse(url(f"projects/{project_id}?error={_query_value(str(exc))}"), status_code=303)
        return RedirectResponse(
            url(f"projects/{project_id}?notice={_query_value(f'Added {len(records)} input file(s).')}"),
            status_code=303,
        )

    @app.post(f"/{session_token}/projects/{{project_id}}/inputs/delete")
    async def delete_input(request: Request, project_id: str):
        form = await request.form(max_fields=10)
        try:
            if job_manager.active_for_project(project_id):
                raise JobConflictError("Inputs cannot change while a run is active.")
            project_store.remove_input(project_id, str(form.get("filename", "")))
        except (ProjectError, JobConflictError) as exc:
            return RedirectResponse(url(f"projects/{project_id}?error={_query_value(str(exc))}"), status_code=303)
        return RedirectResponse(url(f"projects/{project_id}?notice=Input+removed."), status_code=303)

    @app.get(f"/{session_token}/projects/{{project_id}}/settings", response_class=HTMLResponse)
    async def project_settings_page(request: Request, project_id: str, notice: str = "", error: str = ""):
        try:
            project = project_store.get(project_id)
            settings = project_store.get_settings(project_id)
            has_outputs = _has_outputs(project_store.paths(project_id))
        except ProjectNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return render(
            "settings.html",
            request,
            project=project,
            settings=settings,
            has_outputs=has_outputs,
            can_clear_output=has_outputs,
            notice=notice,
            error=error,
        )

    @app.post(f"/{session_token}/projects/{{project_id}}/settings")
    async def save_project_settings(request: Request, project_id: str):
        form = await request.form(max_fields=20)
        try:
            if job_manager.active_for_project(project_id):
                raise JobConflictError("Settings cannot change while a run is active.")
            settings = ProjectSettings(file_context=str(form.get("file_context", "")))
            invalidated = project_store.set_settings(
                project_id,
                settings,
                confirm_invalidation=str(form.get("confirm_invalidation", "")) == "yes",
            )
        except (ValidationError, ProjectError, JobConflictError, SettingsInvalidationRequired) as exc:
            return RedirectResponse(url(f"projects/{project_id}/settings?error={_query_value(_validation_message(exc))}"), status_code=303)
        notice = "Settings saved; generated results were cleared." if invalidated else "Settings saved."
        return RedirectResponse(url(f"projects/{project_id}/settings?notice={_query_value(notice)}"), status_code=303)

    @app.post(f"/{session_token}/projects/{{project_id}}/rename")
    async def rename_project(request: Request, project_id: str):
        form = await request.form(max_fields=10)
        try:
            if job_manager.active_for_project(project_id):
                raise JobConflictError("The project cannot be renamed while a run is active.")
            project_store.rename(project_id, str(form.get("name", "")))
        except (ProjectError, JobConflictError) as exc:
            return RedirectResponse(url(f"projects/{project_id}/settings?error={_query_value(str(exc))}"), status_code=303)
        return RedirectResponse(url(f"projects/{project_id}/settings?notice=Project+renamed."), status_code=303)

    @app.post(f"/{session_token}/projects/{{project_id}}/clear-output")
    async def clear_project_output(project_id: str):
        try:
            project_store.get(project_id)
            if job_manager.active_for_project(project_id):
                raise JobConflictError("Results cannot be cleared while a run is active.")
            project_store.invalidate_outputs(project_id)
        except (ProjectError, JobConflictError) as exc:
            return RedirectResponse(url(f"projects/{project_id}/settings?error={_query_value(str(exc))}"), status_code=303)
        return RedirectResponse(url(f"projects/{project_id}/settings?notice=Generated+results+cleared%3B+source+inputs+were+kept."), status_code=303)

    @app.post(f"/{session_token}/projects/{{project_id}}/delete")
    async def delete_project(project_id: str):
        try:
            project_store.get(project_id)
            if job_manager.active_for_project(project_id):
                raise JobConflictError("The project cannot be deleted while a run is active.")
            project_store.delete(project_id)
        except (ProjectError, JobConflictError) as exc:
            return RedirectResponse(url(f"projects/{project_id}/settings?error={_query_value(str(exc))}"), status_code=303)
        return RedirectResponse(url(f"?notice={_query_value('Project deleted.')}"), status_code=303)

    @app.get(f"/{session_token}/settings", response_class=HTMLResponse)
    async def global_settings_page(request: Request, notice: str = "", error: str = ""):
        return render(
            "global_settings.html",
            request,
            model=config_store.get_default_model(DEFAULT_MODEL),
            thinking_level=config_store.get_thinking_level(),
            credential_state=config_store.get_credential_state(),
            credential_store_name=system_credential_store_name(),
            notice=notice,
            error=error,
        )

    @app.post(f"/{session_token}/settings")
    async def save_global_settings(request: Request):
        form = await request.form(max_fields=10)
        try:
            config_store.set_default_model(str(form.get("model", "")))
            config_store.set_thinking_level(str(form.get("thinking_level", "")))
        except ValueError as exc:
            return RedirectResponse(url(f"settings?error={_query_value(str(exc))}"), status_code=303)
        return RedirectResponse(url("settings?notice=Settings+saved."), status_code=303)

    @app.post(f"/{session_token}/credentials")
    async def save_credentials(request: Request):
        form = await request.form(max_fields=10)
        api_key = str(form.get("api_key", "")).strip()
        try:
            if not api_key:
                raise ProjectError("Enter an API key.")
            config_store.save_api_key_to_keychain(api_key)
            notice = (
                f"API key saved in {system_credential_store_name()} and loaded for this Watson session."
            )
        except (ProjectError, ValueError, CredentialStoreError) as exc:
            return RedirectResponse(url(f"settings?error={_query_value(str(exc))}"), status_code=303)
        return RedirectResponse(url(f"settings?notice={_query_value(notice)}"), status_code=303)

    @app.post(f"/{session_token}/credentials/load")
    async def load_credentials():
        try:
            config_store.load_api_key_from_keychain()
        except CredentialStoreError as exc:
            return RedirectResponse(url(f"settings?error={_query_value(str(exc))}"), status_code=303)
        notice = f"API key loaded from {system_credential_store_name()} for this Watson session."
        return RedirectResponse(url(f"settings?notice={_query_value(notice)}"), status_code=303)

    @app.post(f"/{session_token}/credentials/forget")
    async def forget_credentials():
        config_store.forget_session_api_key()
        return RedirectResponse(
            url("settings?notice=API+key+removed+from+this+Watson+session."),
            status_code=303,
        )

    @app.post(f"/{session_token}/credentials/migrate")
    async def migrate_credentials():
        try:
            config_store.migrate_legacy_api_key_to_keychain()
        except CredentialStoreError as exc:
            return RedirectResponse(url(f"settings?error={_query_value(str(exc))}"), status_code=303)
        notice = (
            f"Legacy API key moved to {system_credential_store_name()}, loaded for this session, "
            "and removed from Watson's old file vault."
        )
        return RedirectResponse(url(f"settings?notice={_query_value(notice)}"), status_code=303)

    @app.post(f"/{session_token}/credentials/delete")
    async def delete_credentials():
        try:
            config_store.delete_api_key_from_keychain()
        except CredentialStoreError as exc:
            return RedirectResponse(url(f"settings?error={_query_value(str(exc))}"), status_code=303)
        return RedirectResponse(
            url("settings?notice=API+key+deleted+from+the+system+credential+store."),
            status_code=303,
        )

    @app.post(f"/{session_token}/credentials/delete-legacy")
    async def delete_legacy_credentials():
        config_store.delete_legacy_api_key()
        return RedirectResponse(url("settings?notice=Legacy+file-vault+API+key+deleted."), status_code=303)

    @app.post(f"/{session_token}/projects/{{project_id}}/runs")
    async def start_run(request: Request, project_id: str):
        form = await request.form(max_fields=10)
        try:
            inputs = project_store.list_inputs(project_id)
            if not inputs:
                raise ProjectError("Add at least one input file before processing.")
            api_key = config_store.get_session_api_key()
            if not api_key:
                raise ProjectError(
                    "Explicitly load the Gemini API key from the system credential store in Settings before processing."
                )
            saved = project_store.get_settings(project_id)
            action = str(form.get("action", "all"))
            retry_mode = str(form.get("retry_mode", "failed"))
            settings = RunnerSettings(
                action=action,
                model=config_store.get_default_model(DEFAULT_MODEL),
                thinking_level=config_store.get_thinking_level(),
                api_key=api_key,
                retry_mode=retry_mode,
                file_context=saved.file_context,
            )
            job = job_manager.start(project_id, settings)
        except (ProjectError, JobConflictError, ValidationError) as exc:
            return RedirectResponse(url(f"projects/{project_id}?error={_query_value(_validation_message(exc))}"), status_code=303)
        return RedirectResponse(url(f"projects/{project_id}?notice=Run+started.&job={job.id}"), status_code=303)

    @app.post(f"/{session_token}/projects/{{project_id}}/runs/{{job_id}}/cancel")
    async def cancel_run(project_id: str, job_id: str):
        try:
            job_manager.cancel(project_id, job_id)
        except (ProjectNotFoundError, JobNotFoundError) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return RedirectResponse(url(f"projects/{project_id}?notice=Cancellation+requested."), status_code=303)

    @app.get(f"/{session_token}/projects/{{project_id}}/runs/{{job_id}}/events")
    async def job_events(project_id: str, job_id: str, request: Request):
        try:
            job_manager.get(project_id, job_id)
        except (ProjectNotFoundError, JobNotFoundError) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        last_event_header = request.headers.get("last-event-id", "0")
        try:
            last_event = max(0, int(last_event_header))
        except ValueError:
            last_event = 0

        async def stream():
            cursor = last_event
            while True:
                if await request.is_disconnected():
                    return
                for event in job_manager.events(project_id, job_id, cursor):
                    cursor = event.id
                    payload = event.model_dump_json()
                    yield f"id: {event.id}\nevent: progress\ndata: {payload}\n\n"
                job = job_manager.get(project_id, job_id)
                if job.state not in ACTIVE_JOB_STATES:
                    yield f"event: terminal\ndata: {job.model_dump_json()}\n\n"
                    return
                yield ": keep-alive\n\n"
                await asyncio.sleep(0.75)

        return StreamingResponse(stream(), media_type="text/event-stream", headers={"X-Accel-Buffering": "no"})

    @app.get(f"/{session_token}/projects/{{project_id}}/downloads/{{filename}}")
    async def download_output(project_id: str, filename: str):
        paths = project_store.paths(project_id)
        if filename != Path(filename).name:
            raise HTTPException(status_code=400, detail="Unsafe path.")
        allowed = {
            "watson-inventory-report.md": paths.outputs / "watson-inventory-report.md",
            "watson-prereg-adherence-report.md": paths.outputs / "watson-prereg-adherence-report.md",
            "reproducibility.json": paths.outputs / "reproducibility.json",
            "inventory.json": paths.state / "inventory.json",
            "study-map.json": paths.state / "study-map.json",
            DEVIATION_RUN_FILENAME: paths.state / DEVIATION_RUN_FILENAME,
        }
        target = allowed.get(filename)
        if target is None or not target.is_file():
            raise HTTPException(status_code=404, detail="Download not found.")
        return FileResponse(target, filename=filename, media_type="application/octet-stream")

    @app.get(f"/{session_token}/projects/{{project_id}}/download.zip")
    async def download_archive(project_id: str):
        project = project_store.get(project_id)
        paths = project_store.paths(project_id)
        handle, archive_name = tempfile.mkstemp(prefix="watson-export-", suffix=".zip")
        os.close(handle)
        archive = Path(archive_name)
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
            for folder, prefix in ((paths.inputs, "sources"), (paths.state, "data"), (paths.outputs, "reports")):
                for path in folder.rglob("*"):
                    if path.is_file() and not path.name.startswith(".") and "gemini-files.json" not in path.name:
                        bundle.write(path, f"{prefix}/{path.relative_to(folder)}")
            bundle.writestr("README.txt", "This archive contains source inputs, machine-readable results, reports, and reproducibility metadata.\n")
            with rebuild_reports_script() as rebuild_script:
                bundle.write(rebuild_script, "rebuild_reports.py")
        filename = f"{_download_name(project.name)}-watson-results.zip"
        return FileResponse(
            archive,
            filename=filename,
            media_type="application/zip",
            background=BackgroundTask(archive.unlink, missing_ok=True),
        )

    @app.post(f"/{session_token}/projects/{{project_id}}/open-folder")
    async def open_project_folder(project_id: str):
        paths = project_store.paths(project_id)
        try:
            _open_folder(paths.root)
        except OSError as exc:
            return RedirectResponse(url(f"projects/{project_id}?error={_query_value(str(exc))}"), status_code=303)
        return RedirectResponse(url(f"projects/{project_id}?notice=Project+folder+opened."), status_code=303)

    @app.get(f"/{session_token}/static/{{filename}}")
    async def static_asset(filename: str):
        if filename not in {"basecoat.css", "theme.css", "watson.css", "settings.css", "watson.js", "theme-init.js"}:
            raise HTTPException(status_code=404)
        return FileResponse(static_dir() / filename)

    @app.exception_handler(ProjectNotFoundError)
    async def project_not_found(_request: Request, exc: ProjectNotFoundError):
        return JSONResponse({"detail": str(exc)}, status_code=404)

    return app


def _host_name(host: str) -> str:
    if host.startswith("["):
        return host.split("]", 1)[0][1:].lower()
    return host.rsplit(":", 1)[0].lower() if ":" in host else host.lower()


def _same_origin(origin: str, host: str) -> bool:
    parsed = urlsplit(origin)
    target = urlsplit(f"//{host}")
    if parsed.scheme != "http":
        return False
    if (parsed.hostname or "").lower() not in {"127.0.0.1", "localhost", "::1"}:
        return False
    if (target.hostname or "").lower() not in {"127.0.0.1", "localhost", "::1"}:
        return False
    try:
        origin_port = parsed.port or 80
        target_port = target.port or 80
    except ValueError:
        return False
    return origin_port == target_port


def _trusted_browser_origin(origin: str, host: str, fetch_site: str = "") -> bool:
    if _same_origin(origin, host):
        return True
    if fetch_site == "cross-site":
        return False
    if origin == "null":
        # Sandboxed and privacy-focused browser views use an opaque origin.
        # The unguessable URL token remains the request credential.
        return True
    parsed = urlsplit(origin)
    if parsed.scheme == "http" and (parsed.hostname or "").lower() in {
        "127.0.0.1",
        "localhost",
        "::1",
    }:
        # Some local proxies omit or rewrite the port in Origin. They cannot
        # learn Watson's random URL token from another origin.
        return True
    return parsed.scheme in {
        "chrome-extension",
        "moz-extension",
        "safari-web-extension",
        "vscode-webview",
    }


def _query_value(value: str) -> str:
    from urllib.parse import quote_plus

    return quote_plus(value[:1000])


def _validation_message(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        return "; ".join(error["msg"] for error in exc.errors())
    return str(exc)


def _has_outputs(paths) -> bool:
    return any(paths.outputs.iterdir()) or (paths.state / "inventory.json").exists()


def _load_project_summary(paths) -> dict:
    result = {
        "has_result": False,
        "files": 0,
        "studies": 0,
        "ready": 0,
        "checked": 0,
        "failed": 0,
        "deviations": 0,
        "missing": 0,
        "unregistered": 0,
        "degrees_of_freedom": 0,
        "warnings": [],
        "items": [],
        "downloads": [],
        "reports": [],
        "usage": {},
    }
    inventory_path = paths.state / "inventory.json"
    if inventory_path.exists():
        try:
            inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
            result["has_result"] = True
            result["files"] = len(inventory.get("files", []))
            result["studies"] = len(inventory.get("studies", []))
            result["warnings"].extend(inventory.get("review_notes", []))
            matches = {
                match.get("study_id"): match
                for match in inventory.get("preregistration_matches", [])
            }
            result["items"] = [
                _inventory_study_item(study, matches.get(study.get("study_id")), index)
                for index, study in enumerate(inventory.get("studies", []), start=1)
            ]
            result["downloads"].extend(["inventory.json", "study-map.json", "watson-inventory-report.md"])
            result["reports"].append({"kind": "inventory", "label": "Read inventory report"})
        except (OSError, json.JSONDecodeError):
            result["warnings"].append("The saved inventory could not be read.")
    map_path = paths.state / "study-map.json"
    if map_path.exists():
        try:
            study_map = json.loads(map_path.read_text(encoding="utf-8"))
            result["ready"] = sum(bool(study.get("ready_for_deviation_check")) for study in study_map.get("studies", []))
        except (OSError, json.JSONDecodeError):
            pass
    deviation_path = paths.state / DEVIATION_RUN_FILENAME
    if deviation_path.exists():
        try:
            run = json.loads(deviation_path.read_text(encoding="utf-8"))
            reports = run.get("reports", [])
            result["checked"] = len(reports)
            result["failed"] = sum(report.get("status") == "failed" for report in reports)
            result["warnings"].extend(run.get("review_notes", []))
            result["items"] = [
                _deviation_study_item(report, index)
                for index, report in enumerate(reports, start=1)
            ]
            result["missing"] = sum(len(item["missing_items"]) for item in result["items"])
            result["unregistered"] = sum(len(item["unregistered_items"]) for item in result["items"])
            result["degrees_of_freedom"] = sum(
                len(item["degrees_of_freedom"]) for item in result["items"]
            )
            result["deviations"] = sum(len(item["findings"]) for item in result["items"])
            result["downloads"].extend([DEVIATION_RUN_FILENAME, "watson-prereg-adherence-report.md"])
            result["reports"].append(
                {"kind": "preregistration", "label": "Read preregistration report"}
            )
        except (OSError, json.JSONDecodeError):
            result["warnings"].append("The saved preregistration results could not be read.")
    if (paths.outputs / "reproducibility.json").exists():
        result["downloads"].append("reproducibility.json")
        try:
            reproducibility = json.loads(
                (paths.outputs / "reproducibility.json").read_text(encoding="utf-8")
            )
            result["usage"] = reproducibility.get("resource_usage", {})
        except (OSError, json.JSONDecodeError):
            pass
    result["warnings"] = list(dict.fromkeys(result["warnings"]))
    result["downloads"] = [name for name in dict.fromkeys(result["downloads"]) if (paths.outputs / name).exists() or (paths.state / name).exists()]
    return result


def _inventory_study_item(study: dict, match: dict | None, index: int) -> dict:
    match = match or {}
    preregistered = study.get("article_says_preregistered")
    return {
        "anchor": f"study-{index}",
        "label": study.get("label", "Untitled study"),
        "status": match.get("match_status", "inventoried"),
        "detail": study.get("description", ""),
        "deviations": None,
        "article_file_path": study.get("article_file_path", ""),
        "article_location": study.get("article_location", ""),
        "preregistered": "yes" if preregistered is True else "no" if preregistered is False else "unclear",
        "preregistration_file_path": match.get("matched_file_path", ""),
        "match_confidence": match.get("confidence", 0),
        "rationale": match.get("rationale", ""),
        "findings": [],
        "missing_items": [],
        "unregistered_items": [],
        "degrees_of_freedom": [],
        "review_notes": [],
        "stage_errors": [],
        "supplemental_file_paths": [],
        "coverage": {},
    }


def _deviation_study_item(report: dict, index: int) -> dict:
    missing = report.get("missing_preregistered_items", [])
    unregistered = report.get("unregistered_article_items", [])
    deviations = report.get("deviations", [])
    degrees_of_freedom = report.get("degrees_of_freedom", [])
    prereg_inventory = report.get("preregistration_inventory") or {}
    article_inventory = report.get("article_inventory") or {}
    prereg_items = prereg_inventory.get("items", [])
    return {
        "anchor": f"study-{index}",
        "label": report.get("study_label", "Untitled study"),
        "status": report.get("status", "unknown"),
        "detail": report.get("overall_assessment") or report.get("summary", ""),
        "summary": report.get("summary", ""),
        "deviations": len(missing) + len(unregistered) + len(deviations) + len(degrees_of_freedom),
        "article_file_path": report.get("article_file_path", ""),
        "preregistration_file_path": report.get("preregistration_file_path", ""),
        "supplemental_file_paths": report.get("supplemental_file_paths", []),
        "apa_citation": report.get("apa_citation", ""),
        "error": report.get("error", ""),
        "review_notes": report.get("review_notes", []),
        "stage_errors": report.get("stage_errors", []),
        "coverage": {
            "prereg_items": len(prereg_items),
            "article_items": len(article_inventory.get("items", [])),
            "underspecified": sum(
                item.get("specificity") in {"partially_specified", "unspecified"}
                for item in prereg_items
            ),
        },
        "missing_items": [
            {
                "number": number,
                "prereg_item_id": item.get("prereg_item_id", ""),
                "category": item.get("category", "uncategorised"),
                "preregistered_plan": item.get("preregistered_plan", ""),
                "searched_for": item.get("searched_for", ""),
                "evidence": item.get("evidence", ""),
                "disclosed": item.get("disclosed", ""),
                "confidence": item.get("confidence", ""),
            }
            for number, item in enumerate(missing, start=1)
        ],
        "unregistered_items": [
            {
                "number": number,
                "article_item_id": item.get("article_item_id", ""),
                "category": item.get("category", "uncategorised"),
                "article_report": item.get("article_report", ""),
                "framing": item.get("framing", ""),
                "evidence": item.get("evidence", ""),
                "disclosed": item.get("disclosed", ""),
                "confidence": item.get("confidence", ""),
            }
            for number, item in enumerate(unregistered, start=1)
        ],
        "findings": [
            {
                "number": number,
                "deviation_type": finding.get("deviation_type", "Unclassified finding"),
                "summary": finding.get("summary", ""),
                "preregistered_plan": finding.get("preregistered_plan", ""),
                "article_report": finding.get("article_report", ""),
                "evidence": finding.get("evidence", ""),
                "confidence": finding.get("confidence", ""),
                "disclosed": finding.get("disclosed", ""),
                "explanation_given": finding.get("explanation_given", ""),
                "robustness_check": finding.get("robustness_check", ""),
            }
            for number, finding in enumerate(deviations, start=1)
        ],
        "degrees_of_freedom": [
            {
                "number": number,
                "prereg_item_id": finding.get("prereg_item_id", ""),
                "category": finding.get("category", "uncategorised"),
                "preregistered_plan": finding.get("preregistered_plan", ""),
                "underspecification": finding.get("underspecification", ""),
                "plausible_alternatives": finding.get("plausible_alternatives", ""),
                "article_choice": finding.get("article_choice", ""),
                "potential_impact": finding.get("potential_impact", ""),
                "evidence": finding.get("evidence", ""),
                "severity": finding.get("severity", ""),
            }
            for number, finding in enumerate(degrees_of_freedom, start=1)
        ],
    }


def _load_browser_report(paths, kind: str) -> dict | None:
    if kind == "inventory":
        path = paths.state / "inventory.json"
        if not path.is_file():
            return None
        try:
            inventory = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        matches = {
            match.get("study_id"): match
            for match in inventory.get("preregistration_matches", [])
        }
        studies = [
            _inventory_study_item(study, matches.get(study.get("study_id")), index)
            for index, study in enumerate(inventory.get("studies", []), start=1)
        ]
        preregistered = sum(
            study.get("article_says_preregistered") is True
            for study in inventory.get("studies", [])
        )
        matched = sum(bool(match.get("matched_file_path")) for match in matches.values())
        return {
            "kind": "inventory",
            "title": "Inventory report",
            "generated_at": inventory.get("generated_at", ""),
            "model": inventory.get("model", ""),
            "summary": (
                f"Watson found {len(inventory.get('files', []))} supported documents and "
                f"{len(studies)} studies. {preregistered} studies were described as preregistered; "
                f"{matched} have a matched preregistration file."
            ),
            "warnings": inventory.get("review_notes", []),
            "documents": inventory.get("documents", []),
            "studies": studies,
            "download": "watson-inventory-report.md",
        }
    if kind == "preregistration":
        path = paths.state / DEVIATION_RUN_FILENAME
        if not path.is_file():
            return None
        try:
            run = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        studies = [
            _deviation_study_item(report, index)
            for index, report in enumerate(run.get("reports", []), start=1)
        ]
        findings = sum(study["deviations"] for study in studies)
        failed = sum(study["status"] == "failed" for study in studies)
        return {
            "kind": "preregistration",
            "title": "Preregistration adherence report",
            "generated_at": run.get("generated_at", ""),
            "model": run.get("model", ""),
            "summary": (
                f"Watson checked {len(studies)} studies and returned {findings} findings across "
                "missing preregistered items, unregistered reported items, deviations, and "
                "preregistration degrees of freedom. "
                f"{failed} studies failed and {len(run.get('skipped_studies', []))} were skipped."
            ),
            "warnings": run.get("review_notes", []),
            "studies": studies,
            "skipped_studies": run.get("skipped_studies", []),
            "download": "watson-prereg-adherence-report.md",
        }
    return None


def _download_name(value: str) -> str:
    cleaned = "-".join(part for part in value.lower().split() if part)
    cleaned = "".join(character for character in cleaned if character.isalnum() or character == "-")
    return cleaned[:80] or "project"


def _open_folder(path: Path) -> None:
    if sys.platform == "darwin":
        subprocess.Popen(["open", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    elif os.name == "nt":
        os.startfile(str(path))  # type: ignore[attr-defined]
    else:
        subprocess.Popen(["xdg-open", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
