"""The actual provider schemas must advertise the raw artifact guard's contract."""

from copy import deepcopy

import pytest
from jsonschema import Draft202012Validator

from recallops.llm import LLMSettings
from recallops.llm.live_reasoning import LiveReasoningService


@pytest.mark.parametrize(
    "name",
    [
        "RecallIntelligence",
        "ProductLotAssessment",
        "TraceabilityAssessment",
        "ContainmentProposal",
        "SupervisorResponse",
    ],
)
async def test_actual_provider_response_schemas_require_explicit_bounded_fields(
    raw_openai_script, live_request, live_case, name
):
    schemas = {}
    raw_openai_script(response_schemas=schemas)
    result = await LiveReasoningService(LLMSettings(mode="openai", model="test-model")).run(
        live_request, transport="direct"
    )
    assert result.status == "success"
    assert len(schemas) == 5
    schema = schemas[name]

    def check(node):
        if isinstance(node, list):
            for item in node:
                check(item)
        if not isinstance(node, dict):
            return
        if node.get("type") == "object":
            if "properties" in node:
                assert set(node.get("required", [])) == set(node["properties"])
                assert node.get("additionalProperties") is False
            assert node.get("maxProperties", float("inf")) <= 512
        if node.get("type") == "array":
            assert node.get("maxItems", float("inf")) <= 512
        if node.get("type") == "string":
            assert node.get("maxLength", float("inf")) <= 8192
        for value in node.values():
            check(value)

    check(schema)
    role = {
        "RecallIntelligence": "recall-intelligence",
        "ProductLotAssessment": "product-lot-matching",
        "TraceabilityAssessment": "traceability-reconciliation",
        "ContainmentProposal": "containment-communications",
    }.get(name)
    raw = (
        live_case.raw[role]
        if role
        else {
            "outcome": "human_review",
            "evidence_count": 0,
            "confirmed_lot_count": 2,
            "ambiguous_lot_count": 1,
            "proposed_action_count": 1,
            "executed": False,
        }
    )
    validator = Draft202012Validator(schema)
    assert validator.is_valid(raw)
    for key in raw:
        omitted = deepcopy(raw)
        del omitted[key]
        assert not validator.is_valid(omitted), f"provider incorrectly permits omission of {key}"
    with_extra = dict(raw, unexpected="not a contract field")
    assert not validator.is_valid(with_extra)
