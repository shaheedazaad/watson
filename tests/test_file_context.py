from __future__ import annotations

from pathlib import Path

from watson.file_context import build_file_context_prompt, load_file_context, save_file_context


def test_file_context_round_trips_and_builds_prompt(tmp_path: Path) -> None:
    save_file_context(tmp_path, "Main article PDF and OSF preregistration.")

    assert load_file_context(tmp_path) == "Main article PDF and OSF preregistration."
    prompt = build_file_context_prompt(tmp_path)
    assert "User-provided directory context:" in prompt
    assert "Main article PDF and OSF preregistration." in prompt
