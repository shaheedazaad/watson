from __future__ import annotations

from pathlib import Path

from watson.deviation_guide import load_deviation_guide
from watson.gemini_client import (
    build_article_inventory_prompt,
    build_degrees_of_freedom_prompt,
    build_inventory_diff_prompt,
    build_preregistration_inventory_prompt,
)
from watson.schemas import (
    ArticleInventory,
    ExecutedItem,
    PlannedItem,
    PreregistrationInventory,
    StudyMapEntry,
)


def make_guide():
    return load_deviation_guide(Path("watson-deviation-guide.yaml"))


def make_study() -> StudyMapEntry:
    return StudyMapEntry(
        study_id="study-1",
        label="Study 1",
        article_file_path="article.pdf",
        matched_preregistration_file_path="prereg.pdf",
        supplemental_material_file_paths=["supplement.pdf"],
    )


def make_prereg_inventory() -> PreregistrationInventory:
    return PreregistrationInventory(
        study_id="study-1",
        items=[
            PlannedItem(
                item_id="P1",
                category="exclusion_criteria",
                statement="Outliers will be removed.",
                specification="none given",
                specificity="unspecified",
            )
        ],
    )


def make_article_inventory() -> ArticleInventory:
    return ArticleInventory(
        study_id="study-1",
        items=[
            ExecutedItem(
                item_id="A1",
                category="exclusion_criteria",
                statement="Removed responses beyond 3 SD.",
                framing="confirmatory",
            )
        ],
    )


def test_preregistration_inventory_prompt_only_inventories() -> None:
    prompt = build_preregistration_inventory_prompt(make_guide(), make_study())

    assert "Stage 1 of 4" in prompt
    assert "prereg.pdf" in prompt
    assert "fully_specified" in prompt
    assert "Do not look for problems" in prompt
    assert "Stage 3" not in prompt


def test_article_inventory_prompt_lists_supplements() -> None:
    prompt = build_article_inventory_prompt(make_guide(), make_study(), ["supplement.pdf"])

    assert "Stage 2 of 4" in prompt
    assert "- supplement.pdf" in prompt
    assert "Do not compare against the preregistration" in prompt


def test_diff_prompt_carries_both_inventories_and_three_lists() -> None:
    prompt = build_inventory_diff_prompt(
        make_guide(), make_study(), make_prereg_inventory(), make_article_inventory()
    )

    assert "Stage 3 of 4" in prompt
    assert "missing_preregistered_items" in prompt
    assert "unregistered_article_items" in prompt
    assert "deviations" in prompt
    assert "degree_of_freedom" in prompt  # allowed deviation type ids are inlined
    assert '"item_id": "P1"' in prompt
    assert '"item_id": "A1"' in prompt


def test_degrees_of_freedom_prompt_audits_the_preregistration_inventory() -> None:
    prompt = build_degrees_of_freedom_prompt(
        make_guide(), make_study(), make_prereg_inventory(), make_article_inventory()
    )

    assert "Stage 4 of 4" in prompt
    assert "partially_specified" in prompt
    assert "severity" in prompt
    assert '"item_id": "P1"' in prompt


def test_prompts_drop_the_guide_when_it_is_already_cached() -> None:
    guide = make_guide()
    inlined = build_preregistration_inventory_prompt(guide, make_study(), include_guide=True)
    cached = build_preregistration_inventory_prompt(guide, make_study(), include_guide=False)

    assert "Allowed deviation_type values:" in inlined
    assert "Allowed deviation_type values:" not in cached
    assert len(cached) < len(inlined)
    assert "Stage 1 of 4" in cached
