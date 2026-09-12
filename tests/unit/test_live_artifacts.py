"""Safe projections preserve factual claims without persisting model prose."""

import copy
import importlib.util

import pytest


def module():
    assert importlib.util.find_spec("recallops.llm.artifacts"), "safe live claims are missing"
    from recallops.llm import artifacts

    return artifacts


def test_claim_contract_exists_before_authority_is_enabled():
    assert module().SpecialistClaims


async def test_projection_preserves_classifications_quantities_and_removes_prose(
    live_case, live_request
):
    mod = module()
    raw = copy.deepcopy(live_case.raw)
    raw["product-lot-matching"]["decisions"][0]["rationale"] = "sk-proj-PRIVATE_CREDENTIAL_CANARY"
    for action in raw["containment-communications"]["proposed_actions"]:
        action["rationale"] = "PRIVATE_MODEL_REASONING_CANARY"
    for draft in raw["containment-communications"]["communication_drafts"]:
        draft["body"] += " PRIVATE_MODEL_REASONING_CANARY"
    claims = mod.project_specialist_claims(live_request, raw)
    assert claims.matching.decisions[0].classification == "exact"
    assert claims.traceability.reconciliations[0].received == 1200
    assert claims.traceability.reconciliations[0].unaccounted == 50
    assert "CANARY" not in claims.model_dump_json()
    assert claims == mod.SpecialistClaims.model_validate_json(claims.model_dump_json())
    with pytest.raises((AttributeError, TypeError, ValueError)):
        claims.matching.decisions[0].classification = "rejected"


async def test_model_draft_action_identifiers_are_not_durable_claims(live_case, live_request):
    raw = copy.deepcopy(live_case.raw)
    for action in raw["containment-communications"]["proposed_actions"]:
        action["action_id"] = "PRIVATE_DRAFT_ACTION_CANARY"
    claims = module().project_specialist_claims(live_request, raw)
    assert "PRIVATE_DRAFT_ACTION_CANARY" not in claims.model_dump_json()
    assert all(
        "action_id" not in action
        for action in claims.containment.model_dump(mode="json")["proposed_actions"]
    )


@pytest.mark.parametrize(
    "path",
    [
        "recall-intelligence.recall_number",
        "recall-intelligence.predicate.upcs.0",
        "recall-intelligence.predicate.plant_codes.0",
        "recall-intelligence.official_products.0.upc",
        "recall-intelligence.citations.0",
        "product-lot-matching.decisions.0.product_id",
        "product-lot-matching.decisions.0.lot_id",
        "product-lot-matching.decisions.0.evidence_ids.0",
        "product-lot-matching.confirmed_lot_ids.0",
        "product-lot-matching.ambiguous_lot_ids.0",
        "traceability-reconciliation.lot_ids.0",
        "traceability-reconciliation.affected_facilities.0",
        "traceability-reconciliation.coverage.0.lot_id",
        "traceability-reconciliation.coverage.0.facility_ids.0",
        "traceability-reconciliation.coverage.0.event_ids.0",
        "traceability-reconciliation.coverage.0.forward_event_ids.0",
        "traceability-reconciliation.coverage.0.backward_event_ids.0",
        "traceability-reconciliation.coverage.0.inventory_evidence_ids.0",
        "traceability-reconciliation.coverage.0.reconciliation_evidence_ids.0",
        "traceability-reconciliation.coverage.0.facility_evidence.@key",
        "traceability-reconciliation.coverage.0.facility_evidence.@value.0",
        "traceability-reconciliation.forward_traces.@key",
        "traceability-reconciliation.forward_traces.@value.0",
        "traceability-reconciliation.backward_traces.@key",
        "traceability-reconciliation.backward_traces.@value.0",
        "traceability-reconciliation.reconciliations.0.lot_id",
        "traceability-reconciliation.reconciliations.0.evidence_ids.0",
        "traceability-reconciliation.reconciliations.0.component_evidence.received.0",
        "traceability-reconciliation.evidence_ids.0",
        "containment-communications.proposed_actions.0.target_ids.0",
        "containment-communications.proposed_actions.0.evidence_ids.0",
        "containment-communications.proposed_actions.0.evidence_by_target.@key",
        "containment-communications.proposed_actions.0.evidence_by_target.@value.0",
        "containment-communications.communication_drafts.0.target_ids.0",
        "containment-communications.communication_drafts.0.evidence_ids.0",
        "containment-communications.communication_drafts.0.evidence_by_target.@key",
        "containment-communications.communication_drafts.0.evidence_by_target.@value.0",
        "containment-communications.all_cited_evidence_ids.0",
    ],
)
async def test_every_model_owned_identifier_is_source_bound_or_hashed(
    live_case, live_request, path
):
    raw = copy.deepcopy(live_case.raw)
    canary = "PRIVATE_IDENTIFIER_CANARY"
    parent = raw
    parts = path.split(".")
    for part in parts[:-1]:
        key = next(iter(parent)) if part == "@value" else int(part) if part.isdigit() else part
        parent = parent[key]
    last = parts[-1]
    if last == "@key":
        parent[canary] = parent.pop(next(iter(parent)))
    else:
        parent[int(last) if last.isdigit() else last] = canary
    claims = module().project_specialist_claims(live_request, raw)
    assert canary not in claims.model_dump_json(), path
    assert "unresolved:" in claims.model_dump_json(), path


async def test_parsed_gap_identifier_cannot_smuggle_private_text(live_case, live_request):
    raw = copy.deepcopy(live_case.raw)
    raw["traceability-reconciliation"]["evidence_gaps"].append(
        "LOT-PRIVATE-CANARY: 50 unaccounted units"
    )
    claims = module().project_specialist_claims(live_request, raw)
    assert "LOT-PRIVATE-CANARY" not in claims.model_dump_json()
    assert claims.traceability.evidence_gaps[-1].lot_id.startswith("unresolved:")


async def test_nested_claim_evidence_maps_are_immutable(live_case, live_request):
    claims = module().project_specialist_claims(live_request, live_case.raw)
    with pytest.raises(TypeError):
        claims.traceability.coverage[0].facility_evidence["FORGED"] = ("EV-FORGED",)


@pytest.mark.parametrize(
    "mutation",
    [
        "extra",
        "coercion",
        "nonfinite",
        "duplicate",
        "oversize",
        "credential_id",
        "executed",
        "foreign_case",
        "predicate_extra",
    ],
)
async def test_unsafe_or_wrongly_bound_claims_are_rejected(live_case, live_request, mutation):
    raw = copy.deepcopy(live_case.raw)
    decision = raw["product-lot-matching"]["decisions"][0]
    if mutation == "extra":
        decision["secret"] = "PRIVATE"
    elif mutation == "coercion":
        decision["product_score"] = "1.0"
    elif mutation == "nonfinite":
        decision["product_score"] = float("nan")
    elif mutation == "duplicate":
        decision["evidence_ids"] *= 2
    elif mutation == "oversize":
        decision["lot_id"] = "X" * 1000
    elif mutation == "credential_id":
        decision["lot_id"] = "sk-proj-secretcredential1234567890"
    elif mutation == "executed":
        raw["containment-communications"]["executed"] = 0
    elif mutation == "foreign_case":
        raw["containment-communications"]["proposed_actions"][0]["case_id"] = "OTHER"
    else:
        raw["recall-intelligence"]["predicate"]["prompt"] = "PRIVATE"
    with pytest.raises(ValueError):
        module().project_specialist_claims(live_request, raw)


async def test_unknown_gap_is_hashed_and_never_silently_discarded(live_case, live_request):
    raw = copy.deepcopy(live_case.raw)
    raw["traceability-reconciliation"]["evidence_gaps"].append("PRIVATE_GAP_CANARY")
    claims = module().project_specialist_claims(live_request, raw)
    assert claims.traceability.evidence_gaps[-1].code == "unclassified"
    assert "PRIVATE_GAP_CANARY" not in claims.model_dump_json()


async def test_context_and_claim_digest_rebinding_is_rejected(live_case, live_request):
    mod = module()
    raw = live_request.model_dump(mode="json")
    raw["context"]["digest"] = "0" * 64
    with pytest.raises(ValueError):
        mod.LiveInvestigationRequest.model_validate(raw)
    claims = mod.project_specialist_claims(live_request, live_case.raw)
    raw = claims.model_dump(mode="json")
    raw["body"] = "PRIVATE"
    with pytest.raises(ValueError):
        mod.SpecialistClaims.model_validate(raw)


@pytest.mark.parametrize(
    "mutation", ["context_text", "context_origin", "duplicate_ids", "extra_prose"]
)
async def test_context_projection_rejects_unbound_or_untrusted_documents(live_request, mutation):
    mod = module()
    from recallops.retrieval.agentic import AgenticRetriever, ClosedRetrievalGateway

    retriever = AgenticRetriever(ClosedRetrievalGateway.direct())
    rag = (await retriever.resume(retriever.start("H-1230-2026 eggs recall"))).model_dump(
        mode="json"
    )
    doc = rag["evidence"][0]["document"]
    if mutation == "context_text":
        doc["text"] = "PRIVATE_CONTEXT_CANARY"
    elif mutation == "context_origin":
        doc["origin"] = "SYNTHETIC_RETAILER_DIGITAL_TWIN"
    elif mutation == "duplicate_ids":
        rag["evidence"].append(copy.deepcopy(rag["evidence"][0]))
    else:
        doc["raw_prompt"] = "PRIVATE_CONTEXT_CANARY"
    with pytest.raises(ValueError):
        mod.build_live_request(
            case_id="CASE-LIVE",
            thread_id="THREAD-LIVE",
            case_version=0,
            recall_number="H-1230-2026",
            question="case",
            scope_lot_ids=[],
            rag_result=rag,
        )


async def test_live_execution_result_is_recursively_immutable(monkeypatch, live_case, live_request):
    from recallops.llm import LLMSettings
    from recallops.llm import live_reasoning as live

    monkeypatch.setattr(live, "build_chat_model", lambda settings: live_case.script())
    result = await live.LiveReasoningService(LLMSettings(mode="openai", model="test-model")).run(
        live_request, transport="direct"
    )
    with pytest.raises((AttributeError, TypeError)):
        result.summary.plan.append("recall-intelligence")
    with pytest.raises((AttributeError, TypeError)):
        result.summary.events.clear()


@pytest.mark.parametrize("field,value", [("evidence_count", "1"), ("executed", 0)])
def test_advisory_synthesis_rejects_coerced_counts_and_execution(field, value):
    from recallops.agents.deep_supervisor import SupervisorResponse

    raw = {
        "outcome": "human_review",
        "evidence_count": 1,
        "confirmed_lot_count": 1,
        "ambiguous_lot_count": 0,
        "proposed_action_count": 2,
        "executed": False,
    }
    raw[field] = value
    with pytest.raises(ValueError):
        SupervisorResponse.model_validate(raw)


@pytest.mark.parametrize("field", ["schema_version", "executed"])
async def test_durable_claim_literals_cannot_be_coerced(live_case, live_request, field):
    mod = module()
    claims = mod.project_specialist_claims(live_request, live_case.raw).model_dump(mode="json")
    if field == "schema_version":
        claims["schema_version"] = True
    else:
        claims["containment"]["executed"] = 0
    import json

    with pytest.raises(ValueError):
        mod.SpecialistClaims.model_validate_json(json.dumps(claims))


async def test_duplicate_candidate_records_are_rejected_at_claim_boundary(live_case, live_request):
    raw = copy.deepcopy(live_case.raw)
    raw["product-lot-matching"]["decisions"].append(raw["product-lot-matching"]["decisions"][0])
    with pytest.raises(ValueError):
        module().project_specialist_claims(live_request, raw)


def test_execution_failure_cannot_claim_completed_model_status():
    from recallops.llm.artifacts import LiveInvestigationResult, LiveReasoningSummary

    with pytest.raises(ValueError):
        LiveInvestigationResult(
            status="execution_failure",
            summary=LiveReasoningSummary(model="test-model", status="completed", duration_ms=1.0),
            request_digest="0" * 64,
            context_digest="0" * 64,
            source_digest="0" * 64,
            run_id="0" * 64,
            failure_category="provider_execution",
        )
