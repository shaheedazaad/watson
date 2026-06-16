from __future__ import annotations

import asyncio
from pathlib import Path

from rich.text import Text
from textual.widgets import DirectoryTree

from watson.config import ConfigStore, DEFAULT_THINKING_LEVEL, get_app_state_dir
from watson.file_context import load_file_context, save_file_context
from watson.tui import FileExplorerScreen, Watson, build_logo, inventory_followup_message


def test_textual_app_mounts_and_exposes_settings() -> None:
    async def run() -> None:
        app = Watson(initial_root=Path("."))
        async with app.run_test() as pilot:
            assert app.title == "Watson"
            assert app.theme == "gruvbox"
            logo = app.query_one("#logo").renderable
            assert isinstance(logo, Text)
            assert "████" in logo.plain
            assert "automated pre-registration auditing" not in logo.plain
            assert app.query_one("#inventory-status").renderable
            assert app.query_one("#study-status").renderable
            assert app.query_one("#config-status").renderable
            assert app.query_one("#overwrite-results").label.plain == "Overwrite previous results"
            assert "Supported file types:" in app.query_one("#support-note").renderable
            assert app.should_overwrite() is False
            await pilot.press("s")
            assert app.query_one("#model-input").value
            assert app.query_one("#thinking-input").value == DEFAULT_THINKING_LEVEL
            assert app.query_one("#api-key-input").password is True
            await pilot.press("h")

    asyncio.run(run())


def test_build_logo_uses_rich_color_spans() -> None:
    logo = build_logo()

    assert isinstance(logo, Text)
    assert "████" in logo.plain
    assert "automated pre-registration auditing" not in logo.plain


def test_file_explorer_starts_at_current_directory(tmp_path: Path) -> None:
    async def run() -> None:
        screen = FileExplorerScreen(tmp_path)
        app = Watson(initial_root=tmp_path)
        async with app.run_test():
            await app.push_screen(screen, wait_for_dismiss=False)
            tree = screen.query_one("#explorer-tree", DirectoryTree)
            assert screen.current_path == tmp_path.resolve()
            assert Path(tree.path).resolve() == tmp_path.resolve()

    asyncio.run(run())


def test_file_explorer_can_move_to_parent(tmp_path: Path) -> None:
    child = tmp_path / "child"
    child.mkdir()

    async def run() -> None:
        screen = FileExplorerScreen(child)
        app = Watson(initial_root=child)
        async with app.run_test():
            await app.push_screen(screen, wait_for_dismiss=False)
            screen.go_up()
            assert screen.current_path == tmp_path.resolve()
            assert screen.selected_path == tmp_path.resolve()

    asyncio.run(run())


def test_watson_uses_last_directory_when_initial_root_is_missing(
    tmp_path: Path, monkeypatch
) -> None:
    config_dir = tmp_path / "config"
    saved_root = tmp_path / "saved"
    saved_root.mkdir()
    monkeypatch.setenv("WATSON_CONFIG_DIR", str(config_dir))

    ConfigStore(get_app_state_dir()).set_last_root(saved_root)

    app = Watson(initial_root=None)

    assert app.root_path == saved_root.resolve()


def test_folder_selection_persists_last_directory(tmp_path: Path, monkeypatch) -> None:
    config_dir = tmp_path / "config"
    target = tmp_path / "selected"
    target.mkdir()
    monkeypatch.setenv("WATSON_CONFIG_DIR", str(config_dir))

    async def run() -> None:
        app = Watson(initial_root=tmp_path)
        async with app.run_test():
            app.on_folder_picked(target)
            saved_root = ConfigStore(get_app_state_dir()).get_last_root(Path("/"))
            assert saved_root == target.resolve()

    asyncio.run(run())


def test_inventory_context_prompt_only_shows_until_context_exists(tmp_path: Path) -> None:
    app = Watson(initial_root=tmp_path)

    assert app.should_prompt_for_inventory_context() is True

    save_file_context(tmp_path, "Main article and prereg.")

    assert app.should_prompt_for_inventory_context() is False
    assert load_file_context(tmp_path) == "Main article and prereg."


def test_inventory_followup_message_mentions_context_and_overwrite() -> None:
    message = inventory_followup_message()

    assert "watson-inventory-report.md" in message
    assert "watson_file_context.txt" in message
    assert "Overwrite previous results" in message


def test_save_settings_persists_thinking_level(tmp_path: Path) -> None:
    async def run() -> None:
        app = Watson(initial_root=tmp_path)
        async with app.run_test():
            app.show_settings()
            app.query_one("#thinking-input", Input).value = "medium"
            app.save_settings()
            config = ConfigStore(tmp_path / ".watson")
            assert config.get_thinking_level() == "medium"

    from textual.widgets import Input

    asyncio.run(run())
