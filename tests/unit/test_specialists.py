"""Behavioral contracts for deterministic specialists and the optional supervisor."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest
from pydantic import ValidationError

from recallops.data.loaders import load_demo_dataset, load_recall_snapshot
from recallops.services.traceability import TraceabilityService


@dataclass(frozen=True)
class EvidenceFixture:
    predicate: Any
    candidate_products: list[dict[str, Any]]
    candidate_lots: list[dict[str, Any]]
    events: list[dict[str, Any]]
    reconciliations: list[Any]


@pytest.fixture
def evidence() -> EvidenceFixture:
    from recallops.agents.specialists import investigate_recall

    intelligence = investigate_recall(load_recall_snapshot())
    service = TraceabilityService(load_demo_dataset())
    products = service.find_candidate_products(intelligence.predicate)
    lots = service.match_lots(intelligence.predicate)
    included = [
        lot["lot_id"] for lot in lots if lot["classification"] in {"exact", "probable", "ambiguous"}
    ]
    events = [event for lot_id in included for event in service.trace_forward(lot_id)]
    reconciliations = [service.reconcile_units(lot_id) for lot_id in included]
    return EvidenceFixture(intelligence.predicate, products, lots, events, reconciliations)


def test_planner_returns_four_bounded_todos_with_completion_criteria() -> None:
    """Catches an unbounded/free-form planner that cannot drive deterministic routing."""
    from recallops.agents.planner import SpecialistName, plan_investigation

    plan = plan_investigation(
        case_id="CASE-001",
        question="Which lots and facilities require containment?",
        max_todos=4,
    )

    assert len(plan.todos) == 4
    assert [todo.specialist for todo in plan.todos] == list(SpecialistName)
    assert [todo.todo_id for todo in plan.todos] == ["todo-1", "todo-2", "todo-3", "todo-4"]
    assert all(todo.status == "pending" for todo in plan.todos)
    assert all(todo.completion_criteria for todo in plan.todos)
    assert plan.write_todos_payload() == [todo.model_dump(mode="json") for todo in plan.todos]

    with pytest.raises(ValueError, match="requires four specialist todos"):
        plan_investigation(case_id="CASE-001", question="Investigate", max_todos=3)


def test_recall_intelligence_extracts_authoritative_predicate_and_citations() -> None:
    """Catches a specialist that invents scope instead of grounding it in the recall."""
    from recallops.agents.specialists import investigate_recall

    result = investigate_recall(load_recall_snapshot())

    assert result.recall_number == "H-1230-2026"
    assert result.predicate.upcs == ["011110609038"]
    assert result.predicate.plant_codes == ["P-1950", "0840962"]
    assert (result.predicate.julian_start, result.predicate.julian_end) == (157, 184)
    assert "Salmonella Enteritidis" in result.predicate.hazard
    assert result.citations == ["openfda:H-1230-2026"]
    assert result.source_provenance == "OFFICIAL_OPENFDA_SNAPSHOT"


def test_matching_explains_exact_probable_ambiguous_and_rejected(evidence: EvidenceFixture) -> None:
    """Catches classification without field-level rationale or an ambiguity review gate."""
    from recallops.agents.specialists import assess_product_lots

    result = assess_product_lots(
        predicate=evidence.predicate,
        candidate_products=evidence.candidate_products,
        candidate_lots=evidence.candidate_lots,
    )
    decisions = {decision.lot_id: decision for decision in result.decisions}

    assert decisions["LOT-EXACT-170"].classification == "exact"
    assert {"upc", "plant_code", "julian_date"} <= set(decisions["LOT-EXACT-170"].matched_fields)
    assert decisions["LOT-PROBABLE-160"].classification == "probable"
    assert "one-digit" in decisions["LOT-PROBABLE-160"].rationale
    assert decisions["LOT-AMBIG-175"].classification == "ambiguous"
    assert decisions["LOT-AMBIG-175"].requires_human_review is True
    assert "uncertain" in decisions["LOT-AMBIG-175"].rationale
    assert decisions["LOT-REJECT-190"].classification == "rejected"
    assert "outside" in decisions["LOT-REJECT-190"].rationale
    assert result.confirmed_lot_ids == ["LOT-EXACT-170", "LOT-PROBABLE-160"]
    assert result.ambiguous_lot_ids == ["LOT-AMBIG-175"]


def test_matching_rejects_lot_evidence_for_unknown_product(evidence: EvidenceFixture) -> None:
    """Catches cross-context evidence injection into the matching specialist."""
    from recallops.agents.specialists import assess_product_lots

    contaminated = [dict(item) for item in evidence.candidate_lots]
    contaminated[0]["product_id"] = "P-NOT-IN-CANDIDATES"

    with pytest.raises(ValueError, match="unknown candidate product"):
        assess_product_lots(
            predicate=evidence.predicate,
            candidate_products=evidence.candidate_products,
            candidate_lots=contaminated,
        )


def test_traceability_reports_facility_coverage_reconciliation_and_gaps(
    evidence: EvidenceFixture,
) -> None:
    """Catches a trace specialist that drops facility coverage or quantity gaps."""
    from recallops.agents.specialists import assess_product_lots, assess_traceability

    matching = assess_product_lots(
        predicate=evidence.predicate,
        candidate_products=evidence.candidate_products,
        candidate_lots=evidence.candidate_lots,
    )
    result = assess_traceability(
        lot_ids=matching.confirmed_lot_ids + matching.ambiguous_lot_ids,
        events=evidence.events,
        reconciliations=evidence.reconciliations,
    )

    assert result.affected_facilities == [
        "DC-NORTH",
        "DC-SOUTH",
        "STORE-01",
        "STORE-02",
        "STORE-03",
        "STORE-08",
    ]
    coverage = {item.lot_id: item for item in result.coverage}
    assert coverage["LOT-PROBABLE-160"].unaccounted_units == 0
    assert coverage["LOT-PROBABLE-160"].complete is True
    assert coverage["LOT-EXACT-170"].unaccounted_units == 50
    assert coverage["LOT-EXACT-170"].complete is False
    assert any("LOT-EXACT-170" in gap and "50 unaccounted" in gap for gap in result.evidence_gaps)
    assert set(result.evidence_ids) == {event["event_id"] for event in evidence.events} | {
        evidence_id
        for reconciliation in evidence.reconciliations
        for evidence_id in reconciliation.evidence_ids
    }


def test_traceability_marks_missing_parent_as_gap_but_rejects_cross_lot_context(
    evidence: EvidenceFixture,
) -> None:
    """Catches silent acceptance of broken lineage and unrelated lot evidence."""
    from recallops.agents.specialists import assess_traceability

    partial = [dict(event) for event in evidence.events if event["event_id"] != "EV-002"]
    result = assess_traceability(
        lot_ids=["LOT-EXACT-170", "LOT-PROBABLE-160", "LOT-AMBIG-175"],
        events=partial,
        reconciliations=evidence.reconciliations,
    )
    assert any("missing parent EV-002" in gap for gap in result.evidence_gaps)

    contaminated = [*evidence.events, {**evidence.events[0], "event_id": "EV-X", "lot_id": "X"}]
    with pytest.raises(ValueError, match="outside delegated lot scope"):
        assess_traceability(
            lot_ids=["LOT-EXACT-170", "LOT-PROBABLE-160", "LOT-AMBIG-175"],
            events=contaminated,
            reconciliations=evidence.reconciliations,
        )


def test_containment_returns_cited_drafts_and_never_executes_writes(
    evidence: EvidenceFixture,
) -> None:
    """Catches uncited or directly executed containment recommendations."""
    from recallops.agents.specialists import (
        assess_product_lots,
        assess_traceability,
        draft_containment,
    )

    matching = assess_product_lots(
        predicate=evidence.predicate,
        candidate_products=evidence.candidate_products,
        candidate_lots=evidence.candidate_lots,
    )
    trace = assess_traceability(
        lot_ids=matching.confirmed_lot_ids + matching.ambiguous_lot_ids,
        events=evidence.events,
        reconciliations=evidence.reconciliations,
    )
    result = draft_containment(
        case_id="CASE-001",
        expected_case_version=2,
        matching=matching,
        traceability=trace,
    )

    assert {action.action_type for action in result.proposed_actions} == {
        "apply_inventory_hold",
        "create_facility_tasks",
    }
    hold = next(
        action for action in result.proposed_actions if action.action_type == "apply_inventory_hold"
    )
    assert hold.target_ids == ["LOT-EXACT-170", "LOT-PROBABLE-160"]
    assert "LOT-AMBIG-175" not in hold.target_ids
    assert all(action.expected_case_version == 2 for action in result.proposed_actions)
    assert result.executed is False
    assert len(result.communication_drafts) == 2
    assert all(draft.evidence_ids for draft in result.communication_drafts)
    assert all("SYNTHETIC — ACADEMIC DEMO" in draft.body for draft in result.communication_drafts)
    assert set(result.all_cited_evidence_ids) <= set(trace.evidence_ids)


def test_containment_fails_closed_when_no_trace_evidence_is_available(
    evidence: EvidenceFixture,
) -> None:
    """Catches plausible-looking containment text with fabricated or missing citations."""
    from recallops.agents.specialists import (
        ProductLotAssessment,
        TraceabilityAssessment,
        draft_containment,
    )

    matching = ProductLotAssessment(decisions=[], confirmed_lot_ids=[], ambiguous_lot_ids=[])
    empty_trace = TraceabilityAssessment(
        lot_ids=[],
        affected_facilities=[],
        coverage=[],
        reconciliations=[],
        evidence_ids=[],
        evidence_gaps=["No trace evidence"],
    )

    with pytest.raises(ValueError, match="trace evidence"):
        draft_containment(
            case_id="CASE-001",
            expected_case_version=0,
            matching=matching,
            traceability=empty_trace,
        )


def test_containment_contract_rejects_unknown_citations() -> None:
    """Catches a draft that cites evidence outside its quarantined context."""
    from recallops.agents.specialists import CommunicationDraft, ContainmentProposal

    with pytest.raises(ValidationError, match="unknown evidence"):
        ContainmentProposal(
            proposed_actions=[],
            communication_drafts=[
                CommunicationDraft(
                    audience="facility",
                    subject="Review",
                    body="SYNTHETIC — ACADEMIC DEMO review",
                    evidence_ids=["EV-INVENTED"],
                )
            ],
            all_cited_evidence_ids=["EV-REAL"],
        )


def test_specialist_catalog_has_four_separate_least_privilege_roles() -> None:
    """Catches collapsed 'multi-agent' roles or write-capable specialist tool scopes."""
    from recallops.agents.deep_supervisor import specialist_catalog

    catalog = specialist_catalog()

    assert [item.name for item in catalog] == [
        "recall-intelligence",
        "product-lot-matching",
        "traceability-reconciliation",
        "containment-communications",
    ]
    assert len({item.system_prompt for item in catalog}) == 4
    assert catalog[0].allowed_tool_names == [
        "search_recalls",
        "get_recall",
        "get_product_metadata",
    ]
    assert catalog[1].allowed_tool_names == ["find_candidate_products", "match_lots"]
    assert catalog[2].allowed_tool_names == [
        "trace_forward",
        "trace_backward",
        "get_inventory",
        "get_sales",
        "reconcile_units",
    ]
    assert catalog[3].allowed_tool_names == []
    assert not any(
        "write" in name or "hold" in name for item in catalog for name in item.allowed_tool_names
    )


def test_deep_supervisor_factory_uses_real_deep_agent_without_provider_credentials() -> None:
    """Catches a slide-only adapter or a factory that resolves a live provider at import time."""
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel

    from recallops.agents.deep_supervisor import build_deep_supervisor

    model = GenericFakeChatModel(messages=iter(["not invoked"]))
    supervisor = build_deep_supervisor(model=model)

    assert supervisor.graph is not None
    tool_node = supervisor.graph.nodes["tools"].bound
    assert set(tool_node._tools_by_name) == {"ls", "read_file", "task", "write_todos"}
    task_description = tool_node._tools_by_name["task"].description
    available_agents = task_description.split("Specify subagent_type", maxsplit=1)[0]
    assert "- general-purpose:" not in available_agents
    assert all(f"- {name}:" in available_agents for name in supervisor.specialist_names)
    assert supervisor.planning_tool_name == "write_todos"
    assert supervisor.delegation_tool_name == "task"
    assert supervisor.specialist_names == [
        "recall-intelligence",
        "product-lot-matching",
        "traceability-reconciliation",
        "containment-communications",
    ]
    assert supervisor.operational_write_tool_names == []


def test_deep_supervisor_rejects_operational_write_tools() -> None:
    """Catches accidental exposure of side-effecting Operations MCP tools to the supervisor."""
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
    from langchain_core.tools import tool

    from recallops.agents.deep_supervisor import build_deep_supervisor

    @tool
    def apply_inventory_hold(case_id: str) -> str:
        """Unsafe test-only simulated write."""
        return case_id

    with pytest.raises(ValueError, match="operational write tool"):
        build_deep_supervisor(
            model=GenericFakeChatModel(messages=iter(["not invoked"])),
            read_tools=[apply_inventory_hold],
        )
