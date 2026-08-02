from __future__ import annotations

from datetime import datetime, timezone

from watson.file_support import is_supported_file, supported_file_types_label
from watson.schemas import FileRecord


def test_supported_file_requires_supported_extension_and_mime() -> None:
    record = FileRecord(
        path="article.pdf",
        extension=".pdf",
        mime_type="image/png",
        size_bytes=1,
        modified_at=datetime.now(tz=timezone.utc),
        sha256="abc",
    )

    assert is_supported_file(record) is False


def test_supported_file_types_label_lists_supported_formats() -> None:
    label = supported_file_types_label()

    assert "PDF" in label
    assert "HTML/HTM" in label
    assert "XML" in label
    assert "JSON" in label
    assert "RTF" in label
    assert "DOCX" not in label


def test_docx_is_not_supported() -> None:
    record = FileRecord(
        path="article.docx",
        extension=".docx",
        mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        size_bytes=1,
        modified_at=datetime.now(tz=timezone.utc),
        sha256="abc",
    )

    assert is_supported_file(record) is False
