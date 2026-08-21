from __future__ import annotations

from pathlib import Path

from watson.code_audit import (
    align_findings,
    constrain_analysis_scope,
    excerpts,
    render,
    verify,
)
from watson.schemas import (
    ArticleInventory,
    CodeAuditAnalysis,
    CodeAuditCheck,
    CodeAuditFinding,
    CodeAuditResult,
    CodeCitation,
    ExecutedItem,
)


def analysis(analysis_id: str) -> CodeAuditAnalysis:
    return CodeAuditAnalysis(
        analysis_id=analysis_id,
        article_item_ids=[f"A{analysis_id[1:]}"],
        reported_analysis=f"Reported analysis {analysis_id}",
        article_evidence="article.pdf, Results",
    )


def finding(analysis_id: str, citation: CodeCitation | None = None) -> CodeAuditFinding:
    citations = [citation] if citation else []
    return CodeAuditFinding(
        analysis=analysis(analysis_id),
        manuscript_check=CodeAuditCheck(
            status="matches", rationale="Matches the paper.", citations=citations
        ),
        preregistration_check=CodeAuditCheck(
            status="matches", rationale="Matches the plan.", citations=citations
        ),
    )


def test_alignment_uses_only_the_paper_inventory_and_fills_omissions() -> None:
    result = CodeAuditResult(
        study_id="study-1",
        study_label="Study 1",
        findings=[finding("C1"), finding("CODE-ONLY")],
    )

    aligned = align_findings(result, [analysis("C1"), analysis("C2")])

    assert [item.analysis.analysis_id for item in aligned.findings] == ["C1", "C2"]
    assert aligned.findings[1].manuscript_check.status == "unclear"
    assert aligned.findings[1].preregistration_check.status == "unclear"
    assert "did not return" in aligned.findings[1].manuscript_check.note


def test_analysis_scope_requires_a_paper_inventory_anchor() -> None:
    inventory = ArticleInventory(
        items=[ExecutedItem(item_id="A1", category="analysis_model")]
    )
    anchored = analysis("C8")
    anchored.article_item_ids = ["A1", "NOT-IN-PAPER"]
    code_only = analysis("CODE-ONLY")
    code_only.article_item_ids = ["NOT-IN-PAPER"]

    scoped = constrain_analysis_scope([anchored, code_only], inventory)

    assert [item.analysis_id for item in scoped] == ["C1"]
    assert scoped[0].article_item_ids == ["A1"]


def test_verification_downgrades_each_check_independently(tmp_path: Path) -> None:
    source = tmp_path / "analysis.R"
    source.write_text("model <- lm(y ~ x)\nsummary(model)\n", encoding="utf-8")
    valid = CodeCitation(
        path="analysis.R", start_line=1, end_line=1
    )
    invalid = CodeCitation(
        path="analysis.R", start_line=2, end_line=2, quote="not the source"
    )
    item = finding("C1")
    item.manuscript_check.citations = [valid]
    item.preregistration_check.citations = [invalid]
    result = CodeAuditResult(
        study_id="study-1",
        study_label="Study 1",
        findings=[item],
        access_log=[
            CodeCitation(
                path="analysis.R",
                start_line=1,
                end_line=1,
                quote="model <- lm(y ~ x)",
            )
        ],
    )

    verified = verify(result, tmp_path)

    assert verified.findings[0].manuscript_check.status == "matches"
    assert verified.findings[0].manuscript_check.citations[0].quote == "model <- lm(y ~ x)"
    assert verified.findings[0].preregistration_check.status == "unclear"
    assert verified.findings[0].preregistration_check.citations == []


def test_excerpt_limit_counts_distinct_files(tmp_path: Path) -> None:
    requests = []
    for index in range(13):
        path = tmp_path / f"analysis-{index}.R"
        path.write_text("result <- 1\n", encoding="utf-8")
        requests.append(
            CodeCitation(path=path.name, start_line=1, end_line=1, quote="")
        )

    access, text = excerpts(tmp_path, requests)

    assert len(access) == 12
    assert "1: result <- 1" in text


def test_markdown_reports_both_checks_for_each_analysis(tmp_path: Path) -> None:
    source = tmp_path / "analysis.R"
    source.write_text("result <- 1\n", encoding="utf-8")
    citation = CodeCitation(
        path="analysis.R", start_line=1, end_line=1, quote="result <- 1"
    )
    result = CodeAuditResult(
        study_id="study-1",
        study_label="Study 1",
        findings=[finding("C1", citation)],
        access_log=[citation],
    )

    markdown = render([verify(result, tmp_path)])

    assert "Reported analysis C1" in markdown
    assert "Matches the manuscript: matches" in markdown
    assert "Matches the preregistration: matches" in markdown
    assert "checks only analyses reported" in markdown
