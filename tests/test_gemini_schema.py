from __future__ import annotations

from watson.schemas import (
    DocumentClassification,
    CodeAuditResult,
    PreregistrationMatch,
    StudyDeviationReport,
    StudyExtractionResult,
)


def test_gemini_response_schemas_do_not_use_additional_properties() -> None:
    for schema_model in [
        DocumentClassification,
        CodeAuditResult,
        PreregistrationMatch,
        StudyDeviationReport,
        StudyExtractionResult,
    ]:
        schema = schema_model.model_json_schema()
        assert_not_present(schema, "additionalProperties")


def assert_not_present(value, forbidden_key: str) -> None:
    if isinstance(value, dict):
        assert forbidden_key not in value
        for child in value.values():
            assert_not_present(child, forbidden_key)
    elif isinstance(value, list):
        for child in value:
            assert_not_present(child, forbidden_key)
