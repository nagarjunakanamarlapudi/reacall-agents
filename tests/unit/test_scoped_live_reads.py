"""The sealed live view and source verifier agree on request-scoped bytes."""

import json

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel

from recallops.agents import deep_supervisor as deep
from recallops.agents.verification import resolve_trusted_evidence, verify_live_investigation
from recallops.config import Settings
from recallops.llm import LLMSettings
from recallops.llm.artifacts import (
    LiveInvestigationRequest,
    canonical_digest,
    project_specialist_claims,
)
from recallops.models import LotMatch
from recallops.services.traceability import TraceabilityService


def scoped_request(request, scope):
    raw = request.model_dump(mode="json", exclude={"run_id", "request_digest"})
    raw["scope_lot_ids"] = list(scope)
    digest = canonical_digest(raw)
    return LiveInvestigationRequest.model_validate_json(
        json.dumps(
            {
                **raw,
                "request_digest": digest,
                "run_id": canonical_digest({"request": digest, "schema": 1}),
            }
        )
    )


@pytest.mark.parametrize("transport", ["direct", "stdio"])
@pytest.mark.parametrize("bounded", [True, False])
async def test_model_facing_match_lots_and_verifier_receipt_share_scoped_bytes(
    live_request, live_case, transport, bounded
):
    request = scoped_request(live_request, live_request.scope_lot_ids if bounded else ())
    source = deep._make_read_config(transport, Settings(), scope_lot_ids=request.scope_lot_ids)
    supervisor = deep.build_deep_supervisor(
        model=GenericFakeChatModel(messages=iter([])), request=request, _read_source=source
    )
    child = deep._compiled_subagent_graphs(supervisor.graph)["product-lot-matching"]
    tool = deep._compiled_tools(child)["match_lots"]
    observations = []
    token = deep.READ_TOOL_OBSERVATIONS.set(observations)
    try:
        rows = await tool.ainvoke({"predicate": live_case.intelligence.predicate})
    finally:
        deep.READ_TOOL_OBSERVATIONS.reset(token)
    full = [
        LotMatch.model_validate(row).model_dump(mode="json")
        for row in TraceabilityService().match_lots(live_case.intelligence.predicate)
    ]
    assert len(full) == 144
    assert len(rows) == (4 if bounded else 144)
    expected = [row for row in full if not bounded or row["lot_id"] in request.scope_lot_ids]
    assert rows == expected
    trusted = await resolve_trusted_evidence(request)
    receipt = next(row for row in trusted.required_receipts if row.name == "match_lots")
    assert observations[0].result_digest == receipt.result_digest == canonical_digest(rows)
    assert observations[0].input_digest == receipt.input_digest
    claims = project_specialist_claims(request, trusted.artifacts)
    assert verify_live_investigation(
        request, claims, trusted, receipts=trusted.required_receipts
    ).result.passed
    if bounded:
        changed = tuple(
            row.model_copy(update={"result_digest": canonical_digest(full)})
            if row.name == "match_lots"
            else row
            for row in trusted.required_receipts
        )
        rejected = verify_live_investigation(request, claims, trusted, receipts=changed)
        assert not rejected.result.passed
        assert "receipt_mismatch" in {item.code for item in rejected.result.violations}
        with pytest.raises(ValueError, match="unexpected"):
            await tool.ainvoke({"predicate": live_case.intelligence.predicate, "scope_lot_ids": []})
    assert set(tool.args_schema.model_fields) == {"predicate"}
    assert set(deep._compiled_tools(child)) == {
        "ls",
        "read_file",
        "find_candidate_products",
        "match_lots",
    }


def test_ordered_scope_is_bound_to_config_and_exact_request(live_request):
    scope = live_request.scope_lot_ids
    source = deep._make_read_config("direct", Settings(), scope_lot_ids=scope)
    assert source.scope_lot_ids == scope
    reversed_request = scoped_request(live_request, tuple(reversed(scope)))
    reversed_source = deep._make_read_config(
        "direct", Settings(), scope_lot_ids=reversed_request.scope_lot_ids
    )
    assert source.digest != reversed_source.digest
    with pytest.raises(ValueError, match="scope"):
        deep.build_deep_supervisor(
            model=GenericFakeChatModel(messages=iter([])),
            request=reversed_request,
            _read_source=source,
        )
    with pytest.raises(ValueError, match="digest"):
        deep._validate_read_config(
            source._replace(scope_lot_ids=()), expected_config_digest=source.digest
        )
    with pytest.raises(ValueError, match="build-time"):
        deep._validate_read_config(reversed_source, expected_config_digest=source.digest)


def test_scope_injection_is_rejected_before_serialization(live_request):
    touched = []

    class HostileTuple(tuple):
        def __iter__(self):
            touched.append(True)
            raise AssertionError("scope hook executed")

    with pytest.raises(ValueError, match="scope"):
        deep._make_read_config("direct", Settings(), scope_lot_ids=HostileTuple())
    object.__setattr__(live_request, "scope_lot_ids", HostileTuple())
    with pytest.raises(ValueError, match="scope"):
        deep.build_deep_supervisor(
            model=GenericFakeChatModel(messages=iter([])), request=live_request
        )
    assert touched == []


def test_tampered_request_scope_cannot_be_rebound(live_request):
    object.__setattr__(live_request, "scope_lot_ids", ())
    with pytest.raises(ValueError, match="binding"):
        deep.build_deep_supervisor(
            model=GenericFakeChatModel(messages=iter([])), request=live_request
        )


async def test_missing_requested_lot_fails_closed_in_view_and_verifier(live_request, live_case):
    request = scoped_request(live_request, ("LOT-NOT-IN-SOURCE",))
    supervisor = deep.build_deep_supervisor(
        model=GenericFakeChatModel(messages=iter([])),
        request=request,
        _read_source=deep._make_read_config(
            "direct", Settings(), scope_lot_ids=request.scope_lot_ids
        ),
    )
    graph = deep._compiled_subagent_graphs(supervisor.graph)["product-lot-matching"]
    with pytest.raises(ValueError, match="scope"):
        await deep._compiled_tools(graph)["match_lots"].ainvoke(
            {"predicate": live_case.intelligence.predicate}
        )
    with pytest.raises(ValueError, match="scope"):
        await resolve_trusted_evidence(request)


async def test_scope_is_detached_from_request_mutation_after_compilation(live_request, live_case):
    source = deep._make_read_config("direct", Settings(), scope_lot_ids=live_request.scope_lot_ids)
    supervisor = deep.build_deep_supervisor(
        model=GenericFakeChatModel(messages=iter([])), request=live_request, _read_source=source
    )
    object.__setattr__(live_request, "scope_lot_ids", ())
    child = deep._compiled_subagent_graphs(supervisor.graph)["product-lot-matching"]
    rows = await deep._compiled_tools(child)["match_lots"].ainvoke(
        {"predicate": live_case.intelligence.predicate}
    )
    assert len(rows) == 4


async def test_actual_compiled_model_receives_four_rows_not_full_source(
    live_request, live_case, monkeypatch
):
    import recallops.llm.live_reasoning as live

    observations = []
    script = live_case.script()

    class InspectingModel(type(script)):
        def _generate(self, messages, *args, **kwargs):
            for message in messages:
                if message.type == "tool" and message.name == "match_lots":
                    rows = json.loads(message.content)
                    observations.append((len(rows), canonical_digest(rows)))
            return super()._generate(messages, *args, **kwargs)

    monkeypatch.setattr(
        live, "build_chat_model", lambda settings: InspectingModel(messages=script.messages)
    )
    result = await live.LiveReasoningService(LLMSettings(mode="openai", model="test-model")).run(
        live_request, transport="direct"
    )
    assert result.status == "success"
    receipt = next(row for row in result.receipts if row.name == "match_lots")
    assert observations and set(observations) == {(4, receipt.result_digest)}
