"""Bounded, non-executing source audit helpers."""
from __future__ import annotations

from pathlib import Path

from watson.schemas import (
    ArticleInventory,
    CodeAuditAnalysis,
    CodeAuditCheck,
    CodeAuditResult,
    CodeCitation,
)

MAX_SOURCE_FILES = 12
MAX_SOURCE_LINES = 1_200
MAX_EXCERPT_CHARS = 40_000

def manifest(code_dir: Path) -> list[dict[str, object]]:
    result = []
    for path in sorted(code_dir.rglob("*")):
        if path.is_file() and not path.name.startswith("."):
            data = path.read_bytes()
            import hashlib
            result.append({"path": path.relative_to(code_dir).as_posix(), "extension": path.suffix.lower(), "size_bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})
    return result

def excerpts(code_dir: Path, requests: list[CodeCitation]) -> tuple[list[CodeCitation], str]:
    """Return only exact requested source ranges, under fixed reading limits."""
    access: list[CodeCitation] = []; chunks: list[str] = []; chars = lines = 0; files: set[str] = set()
    for request in requests:
        if request.path not in files and len(files) >= MAX_SOURCE_FILES: continue
        target = code_dir / request.path
        if not target.is_file() or code_dir not in target.resolve().parents: continue
        source = target.read_text(encoding="utf-8", errors="strict").splitlines()
        start, end = max(1, request.start_line), min(len(source), request.end_line)
        if start > end: continue
        quote = "\n".join(source[start - 1:end])
        if lines + (end-start+1) > MAX_SOURCE_LINES or chars + len(quote) > MAX_EXCERPT_CHARS: continue
        citation = CodeCitation(path=request.path, start_line=start, end_line=end, quote=quote)
        numbered_quote = "\n".join(
            f"{line_number}: {line}"
            for line_number, line in enumerate(source[start - 1:end], start=start)
        )
        access.append(citation); chunks.append(f"{request.path}:{start}-{end}\n{numbered_quote}")
        files.add(request.path); lines += end-start+1; chars += len(quote)
    return access, "\n\n".join(chunks)


def constrain_analysis_scope(
    analyses: list[CodeAuditAnalysis],
    article_inventory: ArticleInventory,
) -> list[CodeAuditAnalysis]:
    """Reject planned analyses that are not anchored to a paper-inventory item."""
    valid_item_ids = {item.item_id for item in article_inventory.items if item.item_id}
    scoped = []
    for analysis in analyses:
        article_item_ids = list(
            dict.fromkeys(
                item_id
                for item_id in analysis.article_item_ids
                if item_id in valid_item_ids
            )
        )
        if not article_item_ids:
            continue
        scoped.append(
            analysis.model_copy(
                update={
                    "analysis_id": f"C{len(scoped) + 1}",
                    "article_item_ids": article_item_ids,
                },
                deep=True,
            )
        )
    return scoped

def align_findings(
    result: CodeAuditResult,
    analyses: list[CodeAuditAnalysis],
) -> CodeAuditResult:
    """Keep exactly one two-check finding for every analysis in the paper inventory."""
    returned = {
        finding.analysis.analysis_id: finding
        for finding in result.findings
        if finding.analysis.analysis_id
    }
    findings = []
    for analysis in analyses:
        finding = returned.get(analysis.analysis_id)
        if finding is None:
            note = "The model did not return a check for this reported analysis."
            finding = _unclear_finding(analysis, note)
        else:
            # The paper-derived scope is authoritative; model output cannot rename,
            # omit, or expand it based on code that happens to be present.
            finding.analysis = analysis.model_copy(deep=True)
        findings.append(finding)
    result.findings = findings
    return result


def verify(result: CodeAuditResult, code_dir: Path) -> CodeAuditResult:
    """Resolve authorized line references and downgrade only unsupported checks."""
    for finding in result.findings:
        for check in (finding.manuscript_check, finding.preregistration_check):
            valid = []
            for citation in check.citations:
                authorized = any(
                    access.path == citation.path
                    and access.start_line <= citation.start_line
                    and citation.end_line <= access.end_line
                    for access in result.access_log
                )
                if not authorized or citation.start_line > citation.end_line:
                    continue
                target = code_dir / citation.path
                if not target.is_file() or code_dir not in target.resolve().parents: continue
                lines = target.read_text(encoding="utf-8", errors="strict").splitlines()
                if citation.end_line > len(lines):
                    continue
                actual = "\n".join(lines[citation.start_line - 1:citation.end_line])
                if citation.quote and actual != citation.quote:
                    continue
                citation.quote = actual
                valid.append(citation)
            check.citations = valid
            if not valid:
                check.status = "unclear"
                check.note = "No verifiable source citation was returned."
    return result


def _unclear_finding(analysis: CodeAuditAnalysis, note: str):
    from watson.schemas import CodeAuditFinding

    return CodeAuditFinding(
        analysis=analysis.model_copy(deep=True),
        manuscript_check=CodeAuditCheck(note=note),
        preregistration_check=CodeAuditCheck(note=note),
    )


def _render_check(label: str, check: CodeAuditCheck) -> list[str]:
    lines = [f"#### {label}: {check.status}", "", check.rationale or "No rationale returned.", ""]
    for cite in check.citations:
        lines.append(f"- `{cite.path}:{cite.start_line}-{cite.end_line}` — `{cite.quote}`")
    if check.note:
        lines += [f"Note: {check.note}", ""]
    return lines


def failure_message(error: str) -> str:
    normalized = error.lower()
    if "completed paper and preregistration inventories" in normalized:
        return (
            "This audit used an older result. Re-run it and Watson will rebuild "
            "the information it needs automatically."
        )
    if "no matched preregistration" in normalized:
        return "Watson could not identify the preregistration for this study. Review the document match, then re-run the audit."
    if "invalid structured output" in normalized:
        return "Gemini returned an incomplete audit. Re-run it to request a fresh response."
    return "Watson could not complete this code audit. Re-run it to try again."


def render(results: list[CodeAuditResult]) -> str:
    lines = [
        "# Watson Code Audit",
        "",
        "This optional audit checks only analyses reported in the article or supplemental materials. It never executes uploaded code or reads raw data.",
        "",
    ]
    for result in results:
        lines += [f"## {result.study_label}", ""]
        if result.status == "failed":
            lines += ["### Code audit needs a rerun", "", failure_message(result.error), ""]
            if result.error:
                lines += ["<details><summary>Technical details</summary>", "", result.error, "", "</details>", ""]
            continue
        for finding in result.findings:
            analysis = finding.analysis
            lines += [f"### {analysis.analysis_id}: {analysis.reported_analysis}", ""]
            if analysis.article_item_ids:
                lines += [f"Paper inventory items: {', '.join(analysis.article_item_ids)}", ""]
            if analysis.article_evidence:
                lines += [f"Paper evidence: {analysis.article_evidence}", ""]
            lines += _render_check("Matches the manuscript", finding.manuscript_check)
            lines += _render_check("Matches the preregistration", finding.preregistration_check)
    return "\n".join(lines) + "\n"
