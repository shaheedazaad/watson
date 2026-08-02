from __future__ import annotations

import hashlib
import mimetypes
from datetime import datetime, timezone
from pathlib import Path

from watson.file_context import FILE_CONTEXT_FILENAME
from watson.schemas import FileRecord


IGNORED_FILES = {
    ".DS_Store",
    FILE_CONTEXT_FILENAME,
    "watson-inventory-report.md",
    "watson-prereg-adherence-report.md",
}


def scan_files(root: Path) -> list[FileRecord]:
    root = root.resolve()
    if not root.is_dir():
        return []

    records: list[FileRecord] = []

    try:
        entries = list(root.iterdir())
    except OSError:
        return []

    for file_path in entries:
        filename = file_path.name
        if filename in IGNORED_FILES or filename.startswith("."):
            continue
        if not file_path.is_file():
            continue

        try:
            stat = file_path.stat()
        except OSError:
            continue

        mime_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
        records.append(
            FileRecord(
                path=file_path.name,
                extension=file_path.suffix.lower(),
                mime_type=mime_type,
                size_bytes=stat.st_size,
                modified_at=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
                sha256=hash_file(file_path),
            )
        )

    return sorted(records, key=lambda record: record.path.lower())


def hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
