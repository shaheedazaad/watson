from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator


DEFAULT_DEVIATION_GUIDE_PATH = Path("watson-deviation-guide.yaml")


class DeviationField(BaseModel):
    name: str
    description: str
    required: bool = True


class DeviationExample(BaseModel):
    preregistered: str
    article: str
    explanation: str


class DeviationTypeDefinition(BaseModel):
    id: str
    label: str
    description: str
    examples: list[DeviationExample] = Field(default_factory=list)

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        if not value:
            raise ValueError("Deviation type id cannot be empty.")
        if not value.replace("_", "").replace("-", "").isalnum():
            raise ValueError("Deviation type id may only contain letters, numbers, hyphens, and underscores.")
        return value


class DeviationGuide(BaseModel):
    version: int = 1
    system_instruction: str
    deviation_fields: list[DeviationField]
    deviation_types: list[DeviationTypeDefinition]

    @model_validator(mode="after")
    def validate_guide(self) -> "DeviationGuide":
        type_ids = [item.id for item in self.deviation_types]
        if len(type_ids) != len(set(type_ids)):
            raise ValueError("Deviation type ids must be unique.")
        if not self.deviation_types:
            raise ValueError("At least one deviation type is required.")
        field_names = {field.name for field in self.deviation_fields}
        if "deviation_type" not in field_names:
            raise ValueError("deviation_fields must include a deviation_type field.")
        return self

    @property
    def allowed_deviation_type_ids(self) -> list[str]:
        return [item.id for item in self.deviation_types]


class DeviationGuideError(RuntimeError):
    pass


def load_deviation_guide(path: Path = DEFAULT_DEVIATION_GUIDE_PATH) -> DeviationGuide:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise DeviationGuideError(f"Deviation guide not found: {path}") from exc
    except yaml.YAMLError as exc:
        raise DeviationGuideError(f"Deviation guide YAML is invalid: {exc}") from exc

    if raw is None:
        raise DeviationGuideError(f"Deviation guide is empty: {path}")

    try:
        return DeviationGuide.model_validate(raw)
    except ValidationError as exc:
        raise DeviationGuideError(f"Deviation guide schema is invalid: {exc}") from exc


def build_deviation_system_prompt(guide: DeviationGuide) -> str:
    field_lines = "\n".join(
        f"- {field.name}: {field.description}" for field in guide.deviation_fields
    )
    type_lines = "\n\n".join(
        render_deviation_type(deviation_type) for deviation_type in guide.deviation_types
    )
    allowed_types = ", ".join(guide.allowed_deviation_type_ids)
    return f"""\
{guide.system_instruction.strip()}

Allowed deviation_type values:
{allowed_types}

Deviation output fields:
{field_lines}

Deviation type definitions and examples:
{type_lines}
"""


def render_deviation_type(deviation_type: DeviationTypeDefinition) -> str:
    examples = "\n".join(
        "- Preregistered: {preregistered}\n  Article: {article}\n  Explanation: {explanation}".format(
            preregistered=example.preregistered,
            article=example.article,
            explanation=example.explanation,
        )
        for example in deviation_type.examples
    )
    if not examples:
        examples = "- No examples provided."
    return (
        f"{deviation_type.id} ({deviation_type.label})\n"
        f"Description: {deviation_type.description.strip()}\n"
        f"Examples:\n{examples}"
    )
