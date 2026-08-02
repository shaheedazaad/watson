from __future__ import annotations

from pathlib import Path

from bs4 import BeautifulSoup

from watson.file_support import TEXT_LIKE_EXTENSIONS


def extract_text(path: Path, max_chars: int = 80_000) -> str:
    suffix = path.suffix.lower()
    try:
        if suffix in TEXT_LIKE_EXTENSIONS - {".html", ".htm"}:
            return path.read_text(encoding="utf-8", errors="replace")[:max_chars]
        if suffix == ".html" or suffix == ".htm":
            soup = BeautifulSoup(path.read_text(encoding="utf-8", errors="replace"), "html.parser")
            return soup.get_text("\n", strip=True)[:max_chars]
        if suffix == ".pdf":
            return _extract_pdf(path, max_chars)
    except Exception as exc:
        return f"[Text extraction failed for {path.name}: {exc}]"
    return ""


def _extract_pdf(path: Path, max_chars: int) -> str:
    from pypdf import PdfReader

    chunks: list[str] = []
    reader = PdfReader(str(path))
    for index, page in enumerate(reader.pages, start=1):
        text = page.extract_text() or ""
        chunks.append(f"\n[Page {index}]\n{text}")
        if sum(len(chunk) for chunk in chunks) >= max_chars:
            break
    return "\n".join(chunks)[:max_chars]
