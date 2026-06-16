from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.prompt import Confirm, Prompt
from rich.table import Table

from watson.config import ConfigStore, DEFAULT_THINKING_LEVEL
from watson.deviation_check import (
    DEVIATION_REPORT_FILENAME,
    DEVIATION_RUN_FILENAME,
    load_study_map,
    ready_studies,
    run_deviation_checks,
    save_deviation_markdown,
)
from watson.deviation_guide import (
    DEFAULT_DEVIATION_GUIDE_PATH,
    DeviationGuideError,
    build_deviation_system_prompt,
    load_deviation_guide,
)
from watson.gemini_client import DEFAULT_MODEL, GeminiError, GeminiResearchClient
from watson.inventory import (
    build_study_map,
    load_upload_cache,
    run_inventory,
    save_inventory,
    save_study_map,
    save_upload_cache,
)
from watson.report import write_inventory_report
from watson.scanner import scan_files
from watson.schemas import InventoryResult


app = typer.Typer(help="Inventory psychology research materials.", invoke_without_command=True)
config_app = typer.Typer(help="Manage Watson configuration.")
deviation_guide_app = typer.Typer(help="Inspect and validate the deviation guide.")
app.add_typer(config_app, name="config")
app.add_typer(deviation_guide_app, name="deviation-guide")
console = Console()


@app.callback()
def main(ctx: typer.Context) -> None:
    """Launch the guided Watson flow when no command is provided."""
    if ctx.invoked_subcommand is None:
        run_guide()


@app.command()
def guide(
    root: Path = typer.Option(Path("."), "--root", help="Initial directory to show in the guide."),
    model: str = typer.Option(DEFAULT_MODEL, "--model", help="Gemini model to use."),
) -> None:
    """Open the Textual app."""
    run_guide(initial_root=root, model=model)


@app.command()
def tui(
    root: Path = typer.Option(Path("."), "--root", help="Initial directory to show in the app."),
    model: str = typer.Option(DEFAULT_MODEL, "--model", help="Gemini model to use."),
) -> None:
    """Open the Textual app."""
    run_guide(initial_root=root, model=model)


@app.command()
def init(
    root: Path = typer.Option(Path("."), "--root", help="Directory to inventory."),
    model: str = typer.Option(DEFAULT_MODEL, "--model", help="Gemini model to use."),
    force: bool = typer.Option(False, "--force", help="Rerun even if inventory artifacts exist."),
) -> None:
    """Initialize Watson state and run the first inventory."""
    perform_inventory(root=root.resolve(), model=model, force=force)


@app.command()
def check(
    root: Path = typer.Option(Path("."), "--root", help="Watson workspace directory."),
    guide_path: Path = typer.Option(
        DEFAULT_DEVIATION_GUIDE_PATH,
        "--guide",
        help="Deviation guide YAML file.",
    ),
    model: str = typer.Option(DEFAULT_MODEL, "--model", help="Gemini model to use."),
    force: bool = typer.Option(False, "--force", help="Recheck studies even when completed JSON exists."),
) -> None:
    """Check preregistration adherence study by study."""
    perform_deviation_check(
        root=root.resolve(),
        guide_path=guide_path,
        model=model,
        force=force,
    )


def run_guide(initial_root: Path = Path("."), model: str = DEFAULT_MODEL) -> None:
    from watson.tui import run_textual_app

    run_textual_app(initial_root=initial_root, model=model)


def run_prompt_guide(initial_root: Path = Path("."), model: str = DEFAULT_MODEL) -> None:
    console.print(
        Panel(
            "[bold]Watson guide[/bold]\n\n"
            "1. Choose the folder with the article, preregistration, and supplements.\n"
            "2. Preview the files Watson knows about.\n"
            "3. Run inventory or preregistration adherence checks.\n"
            "4. Review the generated reports.",
            title="Watson",
            expand=False,
        )
    )

    root = choose_root(initial_root.resolve())
    files = scan_files(root)
    show_file_preview(root, files)

    if not files:
        console.print("[yellow]No inventoryable files were found in that folder.[/yellow]")
        raise typer.Exit(code=1)

    project_warning = looks_like_watson_project(root, files)
    if project_warning:
        console.print(
            "[yellow]This folder looks like the Watson project, not a research-materials folder.[/yellow]"
        )
        if not Confirm.ask("Continue with this folder anyway?", default=False):
            raise typer.Exit()

    action = choose_guide_action(root)
    if action == "exit":
        raise typer.Exit()
    if action == "check":
        perform_deviation_check(
            root=root,
            guide_path=resolve_guide_path(DEFAULT_DEVIATION_GUIDE_PATH, root),
            model=model,
            force=False,
        )
        return

    force = False
    if (root / ".watson" / "inventory.json").exists():
        force = Confirm.ask("An inventory already exists. Rerun and overwrite it?", default=False)
        if not force:
            raise typer.Exit()

    perform_inventory(root=root, model=model, force=force)


def choose_guide_action(root: Path) -> str:
    study_map_path = root / ".watson" / "study-map.json"
    inventory_path = root / ".watson" / "inventory.json"
    if not inventory_path.exists():
        if Confirm.ask("Run Gemini inventory on this folder?", default=True):
            return "inventory"
        return "exit"

    ready_count = 0
    if study_map_path.exists():
        try:
            ready_count = len(ready_studies(load_study_map(study_map_path)))
        except Exception:
            ready_count = 0

    if study_map_path.exists() and ready_count > 0:
        console.print(
            f"[green]Existing inventory found.[/green] {ready_count} study/studies are ready for preregistration adherence checking."
        )
        return Prompt.ask(
            "Next step",
            choices=["check", "inventory", "exit"],
            default="check",
        )

    if study_map_path.exists():
        console.print("[yellow]Existing inventory found, but no studies are ready for deviation checking.[/yellow]")
    else:
        console.print("[yellow]Existing inventory found, but `.watson/study-map.json` is missing.[/yellow]")
    return Prompt.ask(
        "Next step",
        choices=["inventory", "exit"],
        default="inventory",
    )


def choose_root(default_root: Path) -> Path:
    while True:
        answer = Prompt.ask("Research folder", default=str(default_root))
        root = Path(answer).expanduser().resolve()
        if root.exists() and root.is_dir():
            return root
        console.print(f"[red]Not a directory:[/red] {root}")


def show_file_preview(root: Path, files) -> None:
    table = Table(title=f"Files Watson will inspect in {root}", show_lines=False)
    table.add_column("File")
    table.add_column("Type", justify="right")
    table.add_column("Size", justify="right")

    for file_record in files[:15]:
        table.add_row(file_record.path, file_record.extension or "-", format_size(file_record.size_bytes))

    if len(files) > 15:
        table.add_row(f"... and {len(files) - 15} more", "", "")

    console.print(table)


def looks_like_watson_project(root: Path, files) -> bool:
    paths = {file_record.path for file_record in files}
    return (
        "pyproject.toml" in paths
        and "README.md" in paths
        and any(path.startswith("src/watson/") for path in paths)
    )


def format_size(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


def perform_inventory(root: Path, model: str, force: bool) -> InventoryResult:
    root = root.resolve()
    state_dir = root / ".watson"
    inventory_path = state_dir / "inventory.json"
    study_map_path = state_dir / "study-map.json"
    upload_cache_path = state_dir / "gemini-files.json"
    report_path = root / "watson-inventory-report.md"

    if inventory_path.exists() and not force:
        raise typer.BadParameter(
            "Inventory already exists. Use --force to rerun and overwrite Watson artifacts."
        )

    config = ConfigStore(state_dir, console)
    config.ensure_state_dir()
    api_key = config.get_api_key()
    if not api_key:
        api_key = config.prompt_for_api_key()
        if not api_key:
            raise typer.BadParameter("A Gemini API key is required.")

    upload_cache = load_upload_cache(upload_cache_path)
    thinking_level = config.get_thinking_level(DEFAULT_THINKING_LEVEL)
    client = GeminiResearchClient(
        api_key=api_key,
        model=model,
        thinking_level=thinking_level,
        upload_cache=upload_cache,
    )

    if not config.get_api_key():
        with console.status("Validating Gemini API key..."):
            try:
                client.validate_key()
            except GeminiError as exc:
                raise typer.BadParameter(str(exc)) from exc
        storage = config.save_api_key(api_key)
        if storage == "config_file":
            console.print(
                "[yellow]System keyring was unavailable; saved the API key in .watson/config.json.[/yellow]"
            )

    config.set_default_model(model)

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
        transient=True,
    ) as progress:
        progress.add_task("Inventorying files with Gemini...", total=None)
        inventory = run_inventory(root=root, state_dir=state_dir, client=client, model=model)

    save_inventory(inventory_path, inventory)
    save_study_map(study_map_path, build_study_map(inventory))
    save_upload_cache(upload_cache_path, client.upload_cache)
    write_inventory_report(report_path, inventory)

    console.print(f"[green]Inventory complete.[/green] Wrote `{inventory_path}`.")
    console.print(f"[green]Study map:[/green] `{study_map_path}`")
    console.print(f"[green]Review report:[/green] `{report_path}`")
    return inventory


def perform_deviation_check(
    root: Path,
    guide_path: Path,
    model: str,
    force: bool,
) -> None:
    root = root.resolve()
    state_dir = root / ".watson"
    study_map_path = state_dir / "study-map.json"
    upload_cache_path = state_dir / "gemini-files.json"
    run_path = state_dir / DEVIATION_RUN_FILENAME
    markdown_path = root / DEVIATION_REPORT_FILENAME
    guide_path = resolve_guide_path(guide_path, root)

    if not study_map_path.exists():
        raise typer.BadParameter("No `.watson/study-map.json` found. Run `watson init` first.")

    try:
        guide = load_deviation_guide(guide_path)
    except DeviationGuideError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc

    study_map = load_study_map(study_map_path)
    studies = ready_studies(study_map)
    if not studies:
        console.print("[yellow]No studies are ready for deviation checking.[/yellow]")
        console.print("Review `.watson/study-map.json` and `watson-inventory-report.md` first.")
        raise typer.Exit(code=1)

    config = ConfigStore(state_dir, console)
    config.ensure_state_dir()
    api_key = config.get_api_key()
    if not api_key:
        api_key = config.prompt_for_api_key()
        if not api_key:
            raise typer.BadParameter("A Gemini API key is required.")

    upload_cache = load_upload_cache(upload_cache_path)
    thinking_level = config.get_thinking_level(DEFAULT_THINKING_LEVEL)
    client = GeminiResearchClient(
        api_key=api_key,
        model=model,
        thinking_level=thinking_level,
        upload_cache=upload_cache,
    )

    if not config.get_api_key():
        with console.status("Validating Gemini API key..."):
            try:
                client.validate_key()
            except GeminiError as exc:
                raise typer.BadParameter(str(exc)) from exc
        storage = config.save_api_key(api_key)
        if storage == "config_file":
            console.print(
                "[yellow]System keyring was unavailable; saved the API key in .watson/config.json.[/yellow]"
            )

    config.set_default_model(model)

    console.print(f"Found {len(studies)} study/studies ready for preregistration adherence checking.")

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
        transient=False,
    ) as progress:
        task_id = progress.add_task("Starting preregistration checks...", total=None)

        def update(message: str) -> None:
            progress.update(task_id, description=message)

        run = run_deviation_checks(
            root=root,
            state_dir=state_dir,
            study_map=study_map,
            guide=guide,
            guide_path=guide_path,
            client=client,
            model=model,
            force=force,
            progress=update,
        )
        progress.update(task_id, description="Preregistration checks complete")

    save_upload_cache(upload_cache_path, client.upload_cache)
    save_deviation_markdown(markdown_path, run)

    console.print(f"[green]Deviation JSON:[/green] `{run_path}`")
    console.print(f"[green]Deviation report:[/green] `{markdown_path}`")


def resolve_guide_path(guide_path: Path, root: Path) -> Path:
    if guide_path.is_absolute() and guide_path.exists():
        return guide_path
    candidates = [
        root / guide_path,
        guide_path,
        Path(__file__).resolve().parents[2] / guide_path,
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return guide_path


@config_app.command("set-key")
def config_set_key(
    root: Path = typer.Option(Path("."), "--root", help="Watson workspace directory."),
    model: str = typer.Option(DEFAULT_MODEL, "--model", help="Model used to validate the key."),
) -> None:
    """Prompt for and save a Gemini API key."""
    root = root.resolve()
    state_dir = root / ".watson"
    config = ConfigStore(state_dir, console)
    config.ensure_state_dir()
    api_key = config.prompt_for_api_key()
    if not api_key:
        raise typer.BadParameter("A Gemini API key is required.")
    client = GeminiResearchClient(
        api_key=api_key,
        model=model,
        thinking_level=DEFAULT_THINKING_LEVEL,
    )
    with console.status("Validating Gemini API key..."):
        try:
            client.validate_key()
        except GeminiError as exc:
            raise typer.BadParameter(str(exc)) from exc
    storage = config.save_api_key(api_key)
    config.set_default_model(model)
    console.print(f"[green]Gemini API key saved via {storage}.[/green]")


@deviation_guide_app.command("validate")
def validate_deviation_guide(
    guide_path: Path = typer.Option(
        DEFAULT_DEVIATION_GUIDE_PATH,
        "--guide",
        help="Deviation guide YAML file.",
    ),
    show_prompt: bool = typer.Option(
        False,
        "--show-prompt",
        help="Print the rendered system prompt Watson will send to the model.",
    ),
) -> None:
    """Validate the human-editable deviation guide."""
    try:
        guide = load_deviation_guide(guide_path)
    except DeviationGuideError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc

    console.print(f"[green]Deviation guide is valid:[/green] `{guide_path}`")
    console.print(
        f"Loaded {len(guide.deviation_types)} deviation type(s): "
        + ", ".join(guide.allowed_deviation_type_ids)
    )
    if show_prompt:
        console.print(Panel(build_deviation_system_prompt(guide), title="Rendered System Prompt"))
