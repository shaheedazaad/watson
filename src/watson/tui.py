from __future__ import annotations

import asyncio
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from rich.text import Text
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, DataTable, DirectoryTree, Footer, Header, Input, Label, Static, TextArea
from textual.widgets._directory_tree import DirEntry

from watson.cli import DEFAULT_DEVIATION_GUIDE_PATH, resolve_guide_path
from watson.config import (
    ConfigStore,
    DEFAULT_THINKING_LEVEL,
    THINKING_LEVEL_OPTIONS,
    get_app_state_dir,
)
from watson.deviation_check import (
    DEVIATION_REPORT_FILENAME,
    DEVIATION_RUN_FILENAME,
    load_study_map,
    ready_studies,
    run_deviation_checks,
    save_deviation_markdown,
)
from watson.deviation_guide import load_deviation_guide
from watson.gemini_client import DEFAULT_MODEL
from watson.gemini_client import GeminiResearchClient
from watson.inventory import (
    build_study_map,
    load_upload_cache,
    run_inventory,
    save_inventory,
    save_study_map,
    save_upload_cache,
)
from watson.file_context import get_file_context_path, save_file_context
from watson.file_support import is_supported_file, supported_file_types_label
from watson.report import write_inventory_report
from watson.scanner import scan_files


LOGO_LINES = (
    "█   █  ███  █████  ████   ███  █   █",
    "█   █ █   █   █   █      █   █ ██  █",
    "█ █ █ █████   █    ███   █   █ █ █ █",
    "██ ██ █   █   █       █  █   █ █  ██",
    "█   █ █   █   █   ████    ███  █   █",
)


@dataclass
class FileContextSubmission:
    action: str
    text: str = ""


class Watson(App):
    TITLE = "Watson"
    SUB_TITLE = "Preregistration adherence"
    ENABLE_COMMAND_PALETTE = False

    CSS = """
    Screen {
        layout: vertical;
        overflow-y: auto;
    }

    #main {
        height: 1fr;
        padding: 0 1;
        overflow-y: auto;
    }

    #settings {
        height: 1fr;
        padding: 0 1;
        display: none;
        overflow-y: auto;
    }

    FileExplorerScreen {
        align: center middle;
    }

    FileContextScreen {
        align: center middle;
    }

    #file-explorer {
        width: 80%;
        height: 80%;
        border: thick $accent;
        background: $surface;
        padding: 1;
    }

    #file-context-modal {
        width: 84%;
        height: 75%;
        border: thick $accent;
        background: $surface;
        padding: 1;
    }

    #file-context-help,
    #file-context-status {
        height: auto;
    }

    #file-context-input {
        height: 1fr;
        margin: 1 0;
    }

    #explorer-tree {
        height: 1fr;
    }

    #explorer-nav {
        height: 3;
        margin-bottom: 1;
    }

    #explorer-current,
    #explorer-selected {
        height: 3;
    }

    .toolbar,
    .row {
        height: 3;
        margin-bottom: 0;
    }

    .panel,
    .metric {
        border: solid $surface;
        padding: 0 1;
        margin-bottom: 0;
    }

    .metric {
        height: 3;
        width: 1fr;
        margin-right: 1;
    }

    #config-status {
        margin-right: 0;
    }

    #logo {
        height: 6;
        margin: 1 0 0 0;
        padding: 0;
        color: ansi_yellow;
        text-style: bold;
        text-align: center;
    }

    #workspace-row {
        margin-top: 1;
    }

    #root-label,
    .field-label {
        width: 15;
        content-align: left middle;
    }

    #status-row {
        height: 3;
        margin-bottom: 0;
    }

    #actions-row {
        height: 3;
        margin-bottom: 0;
    }

    #files {
        height: 1fr;
        min-height: 8;
        border: solid $surface;
    }

    #overwrite-results {
        height: 3;
        width: auto;
        margin-bottom: 0;
        padding: 0;
    }

    #root-input {
        width: 1fr;
        max-width: 58%;
    }

    #choose-folder {
        min-width: 12;
    }

    #explorer-up {
        min-width: 22;
    }

    #settings-button,
    #refresh {
        min-width: 8;
    }

    #run-log {
        height: 3;
    }

    #support-note {
        height: auto;
    }

    #settings-title {
        height: 3;
        content-align: center middle;
        text-style: bold;
        color: $accent;
    }

    #settings-status {
        height: 3;
    }

    #model-input,
    #thinking-input,
    #api-key-input {
        width: 1fr;
    }

    Button {
        height: 3;
        margin: 0 1 0 0;
        min-width: 10;
    }

    Input {
        height: 3;
    }
    """

    BINDINGS = [
        ("q", "quit", "Quit"),
        ("s", "show_settings", "Settings"),
        ("h", "show_home", "Home"),
        ("r", "refresh", "Refresh"),
    ]

    def __init__(self, initial_root: Path | None = None, model: str = DEFAULT_MODEL) -> None:
        super().__init__()
        self.theme = "gruvbox"
        self.app_config = ConfigStore(get_app_state_dir())
        self.root_path = self.resolve_initial_root(initial_root)
        self.model = model
        self.thinking_level = DEFAULT_THINKING_LEVEL
        self.busy = False

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Vertical(id="main"):
            yield Static(build_logo(), id="logo")
            with Horizontal(id="workspace-row", classes="toolbar"):
                yield Label("Folder", id="root-label")
                yield Input(str(self.root_path), id="root-input")
                yield Button("Choose", id="choose-folder", variant="primary")
                yield Button("Refresh", id="refresh", variant="default")
                yield Button("Settings", id="settings-button", variant="primary")
            with Horizontal(id="status-row"):
                yield Static("", id="inventory-status", classes="metric")
                yield Static("", id="study-status", classes="metric")
                yield Static("", id="config-status", classes="metric")
            with Horizontal(id="actions-row", classes="toolbar"):
                yield Button("Run Inventory", id="run-inventory", variant="success")
                yield Button("Run Deviation Check", id="run-check", variant="warning")
                yield Checkbox("Overwrite previous results", id="overwrite-results")
            yield DataTable(id="files")
            yield Static("", id="run-log", classes="panel")
            yield Static("", id="support-note", classes="panel")
        with Vertical(id="settings"):
            yield Static("Settings", id="settings-title", classes="panel")
            yield Static("", id="settings-status", classes="panel")
            with Horizontal(classes="toolbar"):
                yield Label("Model", classes="field-label")
                yield Input(self.model, id="model-input")
            with Horizontal(classes="toolbar"):
                yield Label("Thinking", classes="field-label")
                yield Input(self.thinking_level, id="thinking-input")
            with Horizontal(classes="toolbar"):
                yield Label("API key", classes="field-label")
                yield Input("", id="api-key-input", password=True, placeholder="Leave blank to keep existing key")
            with Horizontal(classes="toolbar"):
                yield Button("Save Settings", id="save-settings", variant="success")
                yield Button("Delete API Key", id="delete-api-key", variant="error")
                yield Button("Back", id="back-home")
        yield Footer()

    def on_mount(self) -> None:
        self.refresh_workspace()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id
        if button_id == "choose-folder":
            self.open_folder_picker()
        elif button_id == "refresh":
            self.refresh_workspace()
        elif button_id == "settings-button":
            self.show_settings()
        elif button_id == "back-home":
            self.show_home()
        elif button_id == "save-settings":
            self.save_settings()
        elif button_id == "delete-api-key":
            self.delete_api_key()
        elif button_id == "run-inventory":
            self.run_inventory_from_tui()
        elif button_id == "run-check":
            self.run_check_from_tui()

    def action_show_settings(self) -> None:
        self.show_settings()

    def action_show_home(self) -> None:
        self.show_home()

    def action_refresh(self) -> None:
        self.refresh_workspace()

    def open_folder_picker(self) -> None:
        self.refresh_root_from_input()
        self.push_screen(FileExplorerScreen(self.root_path), self.on_folder_picked)

    def on_folder_picked(self, selected_path: Path | None) -> None:
        if selected_path is None:
            return
        self.set_root_path(selected_path)
        self.query_one("#root-input", Input).value = str(self.root_path)
        self.refresh_workspace()

    def show_settings(self) -> None:
        self.refresh_settings_fields()
        self.query_one("#main").display = False
        self.query_one("#settings").display = True

    def show_home(self) -> None:
        self.query_one("#settings").display = False
        self.query_one("#main").display = True
        self.refresh_workspace()

    def refresh_workspace(self) -> None:
        self.refresh_root_from_input()
        table = self.query_one("#files", DataTable)
        table.clear(columns=True)
        table.add_columns("File", "Type", "Size")
        inventory_status = self.query_one("#inventory-status", Static)
        study_status = self.query_one("#study-status", Static)
        config_status = self.query_one("#config-status", Static)

        if not self.root_path.exists() or not self.root_path.is_dir():
            inventory_status.update("Folder\nnot found")
            study_status.update("Ready studies\n-")
            config_status.update("Model/key\n-")
            return

        files = scan_files(self.root_path)
        supported_files = [file_record for file_record in files if is_supported_file(file_record)]
        unsupported_files = [file_record for file_record in files if not is_supported_file(file_record)]
        for file_record in files[:100]:
            table.add_row(file_record.path, file_record.extension or "-", format_size(file_record.size_bytes))

        state_dir = self.root_path / ".watson"
        inventory_exists = (state_dir / "inventory.json").exists()
        study_map_path = state_dir / "study-map.json"
        ready_count = 0
        if study_map_path.exists():
            try:
                ready_count = len(ready_studies(load_study_map(study_map_path)))
            except Exception:
                ready_count = 0

        config = ConfigStore(state_dir)
        self.model = config.get_default_model(DEFAULT_MODEL)
        self.thinking_level = config.get_thinking_level(DEFAULT_THINKING_LEVEL)
        inventory_status.update(
            f"Inventory: {'found' if inventory_exists else 'missing'}\n"
            f"Supported files: {len(supported_files)}/{len(files)}"
        )
        study_status.update(f"Ready studies: {ready_count}\nState: {state_dir.name}")
        key_status = "configured" if config.has_api_key() else "missing"
        config_status.update(f"Thinking: {self.thinking_level}\nAPI key: {key_status}")
        support_note = supported_file_types_label()
        if unsupported_files:
            support_note += f"\nSkipping {len(unsupported_files)} unsupported file(s) in this folder."
        self.query_one("#support-note", Static).update(support_note)

    def refresh_root_from_input(self) -> None:
        root_input = self.query_one("#root-input", Input)
        self.set_root_path(Path(root_input.value), persist=True)

    def context_file_path(self) -> Path:
        return get_file_context_path(self.root_path)

    def should_prompt_for_inventory_context(self) -> bool:
        return not self.context_file_path().exists()

    def resolve_initial_root(self, initial_root: Path | None) -> Path:
        if initial_root is not None:
            resolved = initial_root.expanduser().resolve()
            return resolved if resolved.is_dir() else resolved.parent
        return self.app_config.get_last_root(Path(".").resolve())

    def set_root_path(self, path: Path, persist: bool = True) -> None:
        resolved = path.expanduser().resolve()
        self.root_path = resolved
        if persist and resolved.exists() and resolved.is_dir():
            try:
                self.app_config.set_last_root(resolved)
            except OSError:
                pass

    def refresh_settings_fields(self) -> None:
        config = ConfigStore(self.root_path / ".watson")
        model = config.get_default_model(self.model or DEFAULT_MODEL)
        thinking_level = config.get_thinking_level(self.thinking_level or DEFAULT_THINKING_LEVEL)
        self.query_one("#model-input", Input).value = model
        self.query_one("#thinking-input", Input).value = thinking_level
        self.query_one("#api-key-input", Input).value = ""
        self.query_one("#settings-status", Static).update(
            f"API key storage: {config.get_api_key_storage()}\n"
            f"Temperature: 0. Thinking levels: {', '.join(THINKING_LEVEL_OPTIONS)}.\n"
            f"Settings folder: {config.state_dir}"
        )

    def save_settings(self) -> None:
        config = ConfigStore(self.root_path / ".watson")
        config.ensure_state_dir()
        model = self.query_one("#model-input", Input).value.strip() or DEFAULT_MODEL
        thinking_level = self.query_one("#thinking-input", Input).value.strip().lower() or DEFAULT_THINKING_LEVEL
        if thinking_level not in THINKING_LEVEL_OPTIONS:
            self.query_one("#settings-status", Static).update(
                f"Invalid thinking level: {thinking_level}. Use one of: {', '.join(THINKING_LEVEL_OPTIONS)}."
            )
            return
        api_key = self.query_one("#api-key-input", Input).value.strip()
        config.set_default_model(model)
        config.set_thinking_level(thinking_level)
        storage_message = "API key unchanged"
        if api_key:
            storage = config.save_api_key(api_key)
            storage_message = f"API key saved via {storage}"
        self.model = model
        self.thinking_level = thinking_level
        self.query_one("#settings-status", Static).update(
            f"Saved model: {model}\nSaved thinking: {thinking_level}\n{storage_message}"
        )

    def delete_api_key(self) -> None:
        config = ConfigStore(self.root_path / ".watson")
        config.ensure_state_dir()
        config.delete_api_key()
        self.query_one("#api-key-input", Input).value = ""
        self.query_one("#settings-status", Static).update("API key deleted.")

    def run_inventory_from_tui(self) -> None:
        if self.busy:
            return
        self.refresh_workspace()
        if not self.ensure_api_key_for_run():
            return
        if self.should_prompt_for_inventory_context():
            self.push_screen(
                FileContextScreen(),
                lambda result: self.on_file_context_submitted(result),
            )
            return
        self.start_inventory_run()

    def start_inventory_run(self) -> None:
        force = self.should_overwrite()
        label = "Inventory (overwrite)" if force else "Inventory"
        self.run_background(
            label,
            self._run_inventory_core,
            force,
            on_success=self.show_inventory_followup,
        )

    def on_file_context_submitted(self, result: FileContextSubmission | None) -> None:
        if result is None or result.action == "cancel":
            return
        if result.action == "save":
            save_file_context(self.root_path, result.text)
        self.start_inventory_run()

    def run_check_from_tui(self) -> None:
        if self.busy:
            return
        self.refresh_workspace()
        if not self.ensure_api_key_for_run():
            return
        guide_path = resolve_guide_path(DEFAULT_DEVIATION_GUIDE_PATH, self.root_path)
        force = self.should_overwrite()
        label = "Deviation check (overwrite)" if force else "Deviation check"
        self.run_background(label, self._run_deviation_check_core, guide_path, force)

    def should_overwrite(self) -> bool:
        return self.query_one("#overwrite-results", Checkbox).value

    def ensure_api_key_for_run(self) -> bool:
        config = ConfigStore(self.root_path / ".watson")
        if config.has_api_key():
            return True
        self.query_one("#run-log", Static).update("Configure a Gemini API key in Settings before running Watson.")
        self.show_settings()
        return False

    def run_background(self, label: str, func, *args, on_success=None) -> None:
        self.busy = True
        self.query_one("#run-log", Static).update(f"{label} running...")
        self.run_worker(self._run_background(label, func, *args, on_success=on_success), exclusive=True)

    async def _run_background(self, label: str, func, *args, on_success=None) -> None:
        try:
            await asyncio.to_thread(func, *args)
        except Exception as exc:
            self.query_one("#run-log", Static).update(f"{label} failed: {exc}")
        else:
            self.query_one("#run-log", Static).update(f"{label} complete.")
            self.refresh_workspace()
            if on_success is not None:
                on_success()
        finally:
            self.busy = False

    def show_inventory_followup(self) -> None:
        self.push_screen(InventoryFollowupScreen())

    def _run_inventory_core(self, force: bool) -> None:
        state_dir = self.root_path / ".watson"
        inventory_path = state_dir / "inventory.json"
        study_map_path = state_dir / "study-map.json"
        upload_cache_path = state_dir / "gemini-files.json"
        report_path = self.root_path / "watson-inventory-report.md"

        if inventory_path.exists() and not force:
            raise RuntimeError("Inventory already exists.")

        config = ConfigStore(state_dir)
        config.ensure_state_dir()
        api_key = config.get_api_key()
        if not api_key:
            raise RuntimeError("Configure a Gemini API key in Settings first.")

        upload_cache = load_upload_cache(upload_cache_path)
        client = GeminiResearchClient(
            api_key=api_key,
            model=self.model,
            thinking_level=self.thinking_level,
            upload_cache=upload_cache,
        )
        self.call_from_thread(self._set_run_log, "Inventorying files with Gemini...")
        inventory = run_inventory(root=self.root_path, state_dir=state_dir, client=client, model=self.model)

        save_inventory(inventory_path, inventory)
        save_study_map(study_map_path, build_study_map(inventory))
        save_upload_cache(upload_cache_path, client.upload_cache)
        write_inventory_report(report_path, inventory)

    def _run_deviation_check_core(self, guide_path: Path, force: bool) -> None:
        state_dir = self.root_path / ".watson"
        study_map_path = state_dir / "study-map.json"
        upload_cache_path = state_dir / "gemini-files.json"
        markdown_path = self.root_path / DEVIATION_REPORT_FILENAME

        if not study_map_path.exists():
            raise RuntimeError("No `.watson/study-map.json` found. Run inventory first.")

        config = ConfigStore(state_dir)
        api_key = config.get_api_key()
        if not api_key:
            raise RuntimeError("Configure a Gemini API key in Settings first.")

        guide = load_deviation_guide(guide_path)
        study_map = load_study_map(study_map_path)
        upload_cache = load_upload_cache(upload_cache_path)
        client = GeminiResearchClient(
            api_key=api_key,
            model=self.model,
            thinking_level=self.thinking_level,
            upload_cache=upload_cache,
        )

        def progress(message: str) -> None:
            self.call_from_thread(self._set_run_log, message)

        run = run_deviation_checks(
            root=self.root_path,
            state_dir=state_dir,
            study_map=study_map,
            guide=guide,
            guide_path=guide_path,
            client=client,
            model=self.model,
            force=force,
            progress=progress,
        )
        save_upload_cache(upload_cache_path, client.upload_cache)
        save_deviation_markdown(markdown_path, run)
        self.call_from_thread(
            self._set_run_log,
            f"Deviation check complete. JSON: {state_dir / DEVIATION_RUN_FILENAME}",
        )

    def _set_run_log(self, message: str) -> None:
        self.query_one("#run-log", Static).update(message)


def format_size(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


def build_logo() -> Text:
    width = max(len(line) for line in LOGO_LINES)
    logo = Text()
    for line in LOGO_LINES:
        logo.append(line.center(width))
        logo.append("\n")
    return logo


def inventory_followup_message() -> str:
    return (
        "Check `watson-inventory-report.md` to make sure the inventory is correct.\n\n"
        "If it is not, add more detail to `watson_file_context.txt` such as which "
        "filename corresponds to what, then run the inventory again with Overwrite "
        "previous results checked."
    )


def run_textual_app(initial_root: Path = Path("."), model: str = DEFAULT_MODEL) -> None:
    if sys.platform == "darwin" and not os.environ.get("TERM_PROGRAM"):
        os.environ["TERM_PROGRAM"] = "Apple_Terminal"
    Watson(initial_root=initial_root, model=model).run()


class ExplorerDirectoryTree(DirectoryTree):
    def set_root_path(self, path: Path) -> None:
        self.path = path
        self.reset(str(path), data=DirEntry(self.PATH(path)))
        self.reload()


class InventoryFollowupScreen(ModalScreen[None]):
    BINDINGS = [
        ("escape", "close", "Close"),
    ]

    def compose(self) -> ComposeResult:
        with Vertical(id="file-context-modal"):
            yield Static(inventory_followup_message(), classes="panel")
            with Horizontal(classes="row"):
                yield Button("OK", id="close-inventory-followup", variant="primary")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "close-inventory-followup":
            self.dismiss(None)

    def action_close(self) -> None:
        self.dismiss(None)


class FileContextScreen(ModalScreen[FileContextSubmission | None]):
    BINDINGS = [
        ("escape", "cancel", "Cancel"),
    ]

    def compose(self) -> ComposeResult:
        with Vertical(id="file-context-modal"):
            yield Static(
                "Watson will try to work out the files itself, but it helps if files are named "
                "descriptively and you add a few lines explaining what is in this directory.\n\n"
                "Example: main article PDF, preregistration PDF, supplement with appendices, "
                "and a codebook.",
                id="file-context-help",
                classes="panel",
            )
            yield TextArea(
                "",
                id="file-context-input",
                soft_wrap=True,
            )
            yield Static("", id="file-context-status", classes="panel")
            with Horizontal(classes="row"):
                yield Button("Save Context And Continue", id="save-file-context", variant="success")
                yield Button("Continue Without Context", id="skip-file-context", variant="primary")
                yield Button("Cancel", id="cancel-file-context")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id
        if button_id == "save-file-context":
            self.save_and_continue()
        elif button_id == "skip-file-context":
            self.dismiss(FileContextSubmission(action="skip"))
        elif button_id == "cancel-file-context":
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)

    def save_and_continue(self) -> None:
        text = self.query_one("#file-context-input", TextArea).text.strip()
        if not text:
            self.query_one("#file-context-status", Static).update(
                "Enter a few lines of context, or choose Continue Without Context."
            )
            return
        self.dismiss(FileContextSubmission(action="save", text=text))


class FileExplorerScreen(ModalScreen[Path | None]):
    BINDINGS = [
        ("escape", "cancel", "Cancel"),
        ("backspace", "go_up", "Up"),
    ]

    def __init__(self, start_path: Path) -> None:
        super().__init__()
        resolved = start_path.expanduser().resolve()
        self.current_path = resolved if resolved.is_dir() else resolved.parent
        self.selected_path = self.current_path

    def compose(self) -> ComposeResult:
        with Vertical(id="file-explorer"):
            yield Static("", id="explorer-current", classes="panel")
            with Horizontal(id="explorer-nav"):
                yield Button("Up to Parent Folder", id="explorer-up", variant="primary")
            yield ExplorerDirectoryTree(self.current_path, id="explorer-tree")
            yield Static("", id="explorer-selected", classes="panel")
            with Horizontal(classes="row"):
                yield Button("Use Selected Folder", id="choose-selected", variant="success")
                yield Button("Use Current Folder", id="choose-current", variant="primary")
                yield Button("Cancel", id="cancel-file-explorer")

    def on_mount(self) -> None:
        self.refresh_labels()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id
        if button_id == "explorer-up":
            self.go_up()
        elif button_id == "choose-selected":
            self.choose_selected()
        elif button_id == "choose-current":
            self.dismiss(self.current_path)
        elif button_id == "cancel-file-explorer":
            self.dismiss(None)

    def on_directory_tree_directory_selected(self, event: DirectoryTree.DirectorySelected) -> None:
        self.selected_path = event.path.resolve()
        self.refresh_labels()

    def on_directory_tree_file_selected(self, event: DirectoryTree.FileSelected) -> None:
        self.selected_path = event.path.resolve().parent
        self.refresh_labels()

    def action_choose_selected(self) -> None:
        self.choose_selected()

    def action_go_up(self) -> None:
        self.go_up()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def open_path(self, path: Path) -> None:
        if not path.exists() or not path.is_dir():
            return
        self.current_path = path.resolve()
        self.selected_path = self.current_path
        tree = self.query_one("#explorer-tree", ExplorerDirectoryTree)
        tree.set_root_path(self.current_path)
        self.refresh_labels()

    def go_up(self) -> None:
        parent = self.current_path.parent
        if parent != self.current_path:
            self.open_path(parent)

    def choose_selected(self) -> None:
        if self.selected_path.exists() and self.selected_path.is_dir():
            self.dismiss(self.selected_path)

    def refresh_labels(self) -> None:
        self.query_one("#explorer-current", Static).update(f"Browsing: {self.current_path}")
        self.query_one("#explorer-selected", Static).update(f"Selected folder: {self.selected_path}")
