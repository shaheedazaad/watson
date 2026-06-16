from __future__ import annotations

from pathlib import Path

import pytest

from watson.deviation_guide import (
    DeviationGuideError,
    build_deviation_system_prompt,
    load_deviation_guide,
)


def test_load_deviation_guide_and_build_prompt() -> None:
    guide = load_deviation_guide(Path("watson-deviation-guide.yaml"))
    prompt = build_deviation_system_prompt(guide)

    assert "outcome_switching" in guide.allowed_deviation_type_ids
    assert "degree_of_freedom" in guide.allowed_deviation_type_ids
    assert "Allowed deviation_type values" in prompt
    assert "Deviation output fields" in prompt


def test_deviation_guide_rejects_duplicate_type_ids(tmp_path: Path) -> None:
    guide_path = tmp_path / "guide.yaml"
    guide_path.write_text(
        """
version: 1
system_instruction: Test instruction.
deviation_fields:
  - name: deviation_type
    description: Type id.
deviation_types:
  - id: duplicate
    label: A
    description: First.
  - id: duplicate
    label: B
    description: Second.
""",
        encoding="utf-8",
    )

    with pytest.raises(DeviationGuideError, match="Deviation type ids must be unique"):
        load_deviation_guide(guide_path)
