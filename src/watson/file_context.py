from __future__ import annotations

from pathlib import Path


FILE_CONTEXT_FILENAME = "watson_file_context.txt"


def get_file_context_path(root: Path) -> Path:
    return root.resolve() / FILE_CONTEXT_FILENAME


def load_file_context(root: Path) -> str:
    path = get_file_context_path(root)
    if not path.exists() or not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def save_file_context(root: Path, text: str) -> None:
    normalized = text.strip()
    get_file_context_path(root).write_text(normalized + "\n", encoding="utf-8")


def build_file_context_prompt(root: Path) -> str:
    context = load_file_context(root)
    if not context:
        return ""
    return f"""
User-provided directory context:
{context}

Use this only as supporting context for ambiguous filenames or folder contents.
If it conflicts with the file contents, rely on the file contents.
"""
