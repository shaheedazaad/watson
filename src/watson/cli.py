from __future__ import annotations

import os
import secrets
import socket
import threading
import webbrowser
from pathlib import Path

import typer

from watson.config import ConfigStore, CredentialStoreError
from watson.deviation_guide import (
    DEFAULT_DEVIATION_GUIDE_PATH,
    DeviationGuideError,
    build_deviation_system_prompt,
    load_deviation_guide,
)
from watson.gemini_client import DEFAULT_MODEL
from watson.projects import ProjectError, ProjectStore
from watson.jobs import redact
from watson.runner import NullProgress, RunnerSettings, run_project


app = typer.Typer(
    help="Review research materials in a private localhost browser app.",
    invoke_without_command=True,
    no_args_is_help=False,
)
projects_app = typer.Typer(help="Create and inspect managed projects for automation.")
deviation_guide_app = typer.Typer(help="Inspect and validate the deviation guide.")
app.add_typer(projects_app, name="projects")
app.add_typer(deviation_guide_app, name="deviation-guide")


@app.callback()
def main(ctx: typer.Context) -> None:
    """Open Watson in the default browser when no subcommand is given."""
    if ctx.invoked_subcommand is None:
        serve_browser()


@app.command()
def web() -> None:
    """Open the private localhost browser app."""
    serve_browser()


@app.command("run")
def run_headless(
    project_id: str = typer.Argument(..., help="Opaque ID shown by `watson projects list`."),
    action: str = typer.Option("all", "--action", help="inventory, deviation, code_audit, or all."),
    retry_all: bool = typer.Option(False, "--retry-all", help="Rerun completed work too."),
    api_key_env: str = typer.Option("GEMINI_API_KEY", "--api-key-env", help="Environment variable containing the API key."),
    data_dir: Path | None = typer.Option(None, "--data-dir", help="Override Watson's application-data directory."),
) -> None:
    """Run the same processing pipeline noninteractively for a managed project."""
    store = ProjectStore(data_dir)
    paths = store.paths(project_id)
    saved = store.get_settings(project_id)
    config = ConfigStore(store.data_dir)
    api_key = os.environ.get(api_key_env)
    if not api_key:
        try:
            api_key = config.get_api_key_for_run()
        except CredentialStoreError as exc:
            raise typer.BadParameter(str(exc)) from exc
    settings = RunnerSettings(
        action=action,
        model=config.get_default_model(DEFAULT_MODEL),
        thinking_level=config.get_thinking_level(),
        api_key=api_key,
        retry_mode="all" if retry_all else "failed",
        file_context=saved.file_context,
    )
    try:
        result = run_project(paths.root, settings, _ConsoleProgress())
    except Exception as exc:
        raise typer.BadParameter(redact(str(exc), api_key)) from exc
    typer.echo(result.model_dump_json(indent=2))


@projects_app.command("create")
def create_project(
    name: str = typer.Argument(...),
    import_dir: Path | None = typer.Option(None, "--import-dir", help="Copy top-level supported files into the project."),
    data_dir: Path | None = typer.Option(None, "--data-dir"),
) -> None:
    """Create a managed project and optionally import a directory."""
    store = ProjectStore(data_dir)
    try:
        project = store.create(name)
        imported = store.import_directory(project.id, import_dir) if import_dir else []
    except ProjectError as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(f"{project.id}\t{project.name}\t{len(imported)} input(s)")


@projects_app.command("list")
def list_projects(data_dir: Path | None = typer.Option(None, "--data-dir")) -> None:
    """List managed projects."""
    projects, _ = ProjectStore(data_dir).list(per_page=100)
    for project in projects:
        typer.echo(f"{project.metadata.id}\t{project.metadata.name}\t{project.input_count} input(s)")


@deviation_guide_app.command("validate")
def validate_deviation_guide(
    guide_path: Path = typer.Option(DEFAULT_DEVIATION_GUIDE_PATH, "--guide"),
    show_prompt: bool = typer.Option(False, "--show-prompt"),
) -> None:
    """Validate the human-editable deviation guide."""
    try:
        guide = load_deviation_guide(guide_path)
    except DeviationGuideError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(f"Deviation guide is valid: {guide_path}")
    typer.echo(f"Loaded {len(guide.deviation_types)} deviation type(s).")
    if show_prompt:
        typer.echo(build_deviation_system_prompt(guide))


def serve_browser(*, open_browser: bool = True, data_dir: Path | None = None) -> None:
    import uvicorn

    from watson.web import create_app

    token = secrets.token_urlsafe(32)
    server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server_socket.bind(("127.0.0.1", 0))
    server_socket.listen(128)
    port = server_socket.getsockname()[1]
    application = create_app(token=token, data_dir=data_dir)
    url = f"http://127.0.0.1:{port}/{token}/"
    typer.echo(f"Watson is running at {url}")
    typer.echo("Press Ctrl+C to stop.")
    if open_browser:
        threading.Timer(0.35, lambda: webbrowser.open(url)).start()
    config = uvicorn.Config(application, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    try:
        server.run(sockets=[server_socket])
    except KeyboardInterrupt:
        pass
    finally:
        server_socket.close()


def resolve_guide_path(guide_path: Path, root: Path) -> Path:
    if guide_path.is_absolute() and guide_path.exists():
        return guide_path
    candidates = [root / guide_path, guide_path, Path(__file__).resolve().parents[2] / guide_path]
    return next((candidate for candidate in candidates if candidate.exists()), guide_path)


class _ConsoleProgress(NullProgress):
    def emit(self, stage: str, message: str, current: int | None = None, total: int | None = None) -> None:
        typer.echo(f"[{stage}] {message}")
