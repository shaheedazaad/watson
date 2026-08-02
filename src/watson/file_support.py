from __future__ import annotations

from watson.schemas import FileRecord


SUPPORTED_FILE_MIME_TYPES: dict[str, tuple[str, ...]] = {
    ".pdf": ("application/pdf",),
    ".txt": ("text/plain",),
    ".csv": ("text/csv", "application/csv", "text/plain"),
    ".html": ("text/html", "application/xhtml+xml"),
    ".htm": ("text/html", "application/xhtml+xml"),
    ".xml": ("text/xml", "application/xml"),
    ".json": ("application/json", "text/json", "text/plain"),
    ".rtf": ("text/rtf", "application/rtf", "text/plain"),
}

TEXT_LIKE_EXTENSIONS = {".txt", ".csv", ".html", ".htm", ".xml", ".json", ".rtf"}


def supported_extensions() -> set[str]:
    return set(SUPPORTED_FILE_MIME_TYPES)


def supported_file_types_label() -> str:
    return "Supported file types: PDF, TXT, CSV, HTML/HTM, XML, JSON, RTF."


def is_supported_file(record: FileRecord) -> bool:
    extension = record.extension.lower()
    if extension not in SUPPORTED_FILE_MIME_TYPES:
        return False

    mime_type = record.mime_type.lower()
    if not mime_type or mime_type == "application/octet-stream":
        return True
    if mime_type in SUPPORTED_FILE_MIME_TYPES[extension]:
        return True
    if extension in TEXT_LIKE_EXTENSIONS and mime_type.startswith("text/"):
        return True
    return False


def unsupported_reason(record: FileRecord) -> str:
    extension = record.extension.lower()
    if extension not in SUPPORTED_FILE_MIME_TYPES:
        return f"unsupported extension `{extension or '[no extension]'}`"
    return f"unsupported MIME type `{record.mime_type}` for `{extension}`"
