"""Behavioral contracts for deterministic specialists and the optional supervisor."""

from __future__ import annotations

import inspect
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
    inventory_positions: list[dict[str, Any]]
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
    inventory = [position for lot_id in included for position in service.get_inventory(lot_id)]
    reconciliations = [service.reconcile_units(lot_id) for lot_id in included]
    return EvidenceFixture(
        intelligence.predicate, products, lots, events, inventory, reconciliations
    )


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
    payload = plan.write_todos_payload()
    assert set(payload) == {"todos"}
    assert [item["status"] for item in payload["todos"]] == ["pending"] * 4
    assert [item["content"].split("]", maxsplit=1)[0] for item in payload["todos"]] == [
        "[recall-intelligence",
        "[product-lot-matching",
        "[traceability-reconciliation",
        "[containment-communications",
    ]

    with pytest.raises(ValueError, match="requires four specialist todos"):
        plan_investigation(case_id="CASE-001", question="Investigate", max_todos=3)


def test_planner_payload_executes_through_the_compiled_write_todos_tool() -> None:
    """Catches a lookalike plan payload that does not satisfy the real middleware schema."""
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
    from langchain_core.messages import AIMessage
    from langgraph.runtime import Runtime

    from recallops.agents.deep_supervisor import build_deep_supervisor
    from recallops.agents.planner import plan_investigation

    supervisor = build_deep_supervisor(model=GenericFakeChatModel(messages=iter(["not invoked"])))
    payload = plan_investigation(case_id="CASE-001", question="Investigate").write_todos_payload()
    tool_node = supervisor.graph.nodes["tools"].bound
    result = tool_node.func(
        {
            "messages": [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "write_todos",
                            "args": payload,
                            "id": "plan-call",
                            "type": "tool_call",
                        }
                    ],
                )
            ]
        },
        {},
        Runtime(),
    )

    assert result[0].update["todos"] == payload["todos"]


def test_recall_intelligence_extracts_authoritative_predicate_and_citations() -> None:
    """Catches authoritative scope narrowed to one retailer SKU or stripped of sizes."""
    from recallops.agents.specialists import investigate_recall

    result = investigate_recall(load_recall_snapshot())

    assert result.recall_number == "H-1230-2026"
    assert len(result.predicate.upcs) == 20
    assert set(result.predicate.upcs) == {
        "011110609021",
        "011110609809",
        "011110609038",
        "011110609335",
        "011110609045",
        "011110609052",
        "092825095644",
        "092825095552",
        "092825095569",
        "092825109808",
        "092825095637",
        "092825095606",
        "092825095583",
        "078566001045",
        "078566200004",
        "078566005036",
        "079307005162",
        "078566001021",
        "078566001014",
        "028621304987",
    }
    assert len(result.official_products) == 28
    products = {product.item_number: product for product in result.official_products}
    assert products[3].description == "Kroger, Large 12 Eggs, Net Wt 24 oz (1lb 8oz) 681g"
    assert products[3].upc == "011110609038"
    assert products[11].description == (
        "Brookshire's farm fresh, Large 36 Total Eggs TWIN PACK (18 Each), "
        "Net Wt 36 oz (2lb 4oz) 1.03kg"
    )
    assert products[21].upc == "028621304987"
    assert products[28].description == "Jumbo Grade A, JD 175, 16.67 Dozen"
    assert products[28].upc is None
    assert result.predicate.product_terms == [
        product.description for product in result.official_products
    ]
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


@pytest.mark.parametrize(
    ("poison", "message"),
    [
        ({"score": 1.0, "classification": "exact", "upc": "099999999999"}, "computed UPC"),
        ({"score": 0.0, "classification": "rejected"}, "computed UPC"),
    ],
)
def test_matching_recomputes_and_rejects_poisoned_product_labels(
    evidence: EvidenceFixture,
    poison: dict[str, Any],
    message: str,
) -> None:
    """Catches caller scores/classifications overriding UPC evidence."""
    from recallops.agents.specialists import assess_product_lots

    products = [dict(item) for item in evidence.candidate_products]
    exact = next(item for item in products if item["product_id"] == "P-EXACT")
    exact.update(poison)

    with pytest.raises(ValueError, match=message):
        assess_product_lots(
            predicate=evidence.predicate,
            candidate_products=products,
            candidate_lots=evidence.candidate_lots,
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
        inventory_positions=evidence.inventory_positions,
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
    assert coverage["LOT-EXACT-170"].forward_event_ids == [
        "EV-001",
        "EV-002",
        "EV-S-LOT-EXACT-170",
        "EV-R-LOT-EXACT-170",
        "EV-D-LOT-EXACT-170",
        "EV-003",
        "EV-008",
    ]
    assert coverage["LOT-EXACT-170"].backward_event_ids == [
        "EV-S-LOT-EXACT-170",
        "EV-R-LOT-EXACT-170",
        "EV-D-LOT-EXACT-170",
        "EV-002",
        "EV-003",
        "EV-008",
        "EV-001",
    ]
    assert coverage["LOT-EXACT-170"].facility_evidence["STORE-02"] == ["EV-003"]
    assert any("LOT-EXACT-170" in gap and "50 unaccounted" in gap for gap in result.evidence_gaps)
    assert set(result.evidence_ids) == {event["event_id"] for event in evidence.events} | {
        evidence_id
        for reconciliation in evidence.reconciliations
        for evidence_id in reconciliation.evidence_ids
    }


def test_traceability_rejects_missing_parent_and_cross_lot_context(
    evidence: EvidenceFixture,
) -> None:
    """Catches silent acceptance of broken lineage and unrelated lot evidence."""
    from recallops.agents.specialists import assess_traceability

    partial = [dict(event) for event in evidence.events if event["event_id"] != "EV-002"]
    with pytest.raises(ValueError, match="missing parent EV-002"):
        assess_traceability(
            lot_ids=["LOT-EXACT-170", "LOT-PROBABLE-160", "LOT-AMBIG-175"],
            events=partial,
            inventory_positions=evidence.inventory_positions,
            reconciliations=evidence.reconciliations,
        )

    contaminated = [*evidence.events, {**evidence.events[0], "event_id": "EV-X", "lot_id": "X"}]
    with pytest.raises(ValueError, match="outside delegated lot scope"):
        assess_traceability(
            lot_ids=["LOT-EXACT-170", "LOT-PROBABLE-160", "LOT-AMBIG-175"],
            events=contaminated,
            inventory_positions=evidence.inventory_positions,
            reconciliations=evidence.reconciliations,
        )


def test_traceability_rejects_cycles_cross_lot_parents_and_fabricated_components(
    evidence: EvidenceFixture,
) -> None:
    """Catches graph-shaped but unauthenticated lineage and reconciliation evidence."""
    from recallops.agents.specialists import TraceabilityAssessment, assess_traceability

    cycle = [dict(event) for event in evidence.events]
    next(item for item in cycle if item["event_id"] == "EV-001")["parent_event_id"] = "EV-002"
    with pytest.raises(ValueError, match="cycle"):
        assess_traceability(
            lot_ids=["LOT-EXACT-170", "LOT-PROBABLE-160", "LOT-AMBIG-175"],
            events=cycle,
            inventory_positions=evidence.inventory_positions,
            reconciliations=evidence.reconciliations,
        )

    wrong_parent = [dict(event) for event in evidence.events]
    next(item for item in wrong_parent if item["event_id"] == "EV-002")["parent_event_id"] = (
        "EV-004"
    )
    with pytest.raises(ValueError, match="parent lot mismatch"):
        assess_traceability(
            lot_ids=["LOT-EXACT-170", "LOT-PROBABLE-160", "LOT-AMBIG-175"],
            events=wrong_parent,
            inventory_positions=evidence.inventory_positions,
            reconciliations=evidence.reconciliations,
        )

    forged = [item.model_dump(mode="json") for item in evidence.reconciliations]
    forged[0]["component_evidence"]["received"] = ["EV-INVENTED"]
    forged[0]["evidence_ids"] = sorted(
        {
            evidence_id
            for identifiers in forged[0]["component_evidence"].values()
            for evidence_id in identifiers
        }
    )
    with pytest.raises(ValueError, match="unknown reconciliation evidence"):
        assess_traceability(
            lot_ids=["LOT-EXACT-170", "LOT-PROBABLE-160", "LOT-AMBIG-175"],
            events=evidence.events,
            inventory_positions=evidence.inventory_positions,
            reconciliations=forged,
        )

    valid = assess_traceability(
        lot_ids=["LOT-EXACT-170", "LOT-PROBABLE-160", "LOT-AMBIG-175"],
        events=evidence.events,
        inventory_positions=evidence.inventory_positions,
        reconciliations=evidence.reconciliations,
    ).model_dump(mode="python")
    valid["coverage"][0]["facility_evidence"]["STORE-02"] = ["EV-INVENTED"]
    with pytest.raises(ValidationError, match="unknown facility evidence"):
        TraceabilityAssessment.model_validate(valid)


@pytest.mark.parametrize(
    "component",
    ["received", "on_hand", "quarantined", "sold", "returned", "disposed", "unaccounted"],
)
def test_traceability_recomputes_every_quantity_instead_of_trusting_balanced_poison(
    evidence: EvidenceFixture,
    component: str,
) -> None:
    """Catches numerically balanced caller values replacing evidence-derived quantities."""
    from recallops.agents.specialists import assess_traceability

    poisoned = [item.model_dump(mode="json") for item in evidence.reconciliations]
    exact = next(item for item in poisoned if item["lot_id"] == "LOT-EXACT-170")
    if component == "received":
        exact["received"] += 1
        exact["unaccounted"] += 1
    elif component == "unaccounted":
        exact["unaccounted"] += 1
        exact["on_hand"] -= 1
    else:
        exact[component] += 1
        exact["unaccounted"] -= 1

    with pytest.raises(ValueError, match=rf"LOT-EXACT-170.*{component}.*evidence"):
        assess_traceability(
            lot_ids=["LOT-EXACT-170", "LOT-PROBABLE-160", "LOT-AMBIG-175"],
            events=evidence.events,
            inventory_positions=evidence.inventory_positions,
            reconciliations=poisoned,
        )


def test_traceability_requires_exact_component_evidence_without_missing_or_extra_ids(
    evidence: EvidenceFixture,
) -> None:
    """Catches residual citations that omit or add otherwise valid same-lot evidence."""
    from recallops.agents.specialists import assess_traceability

    missing = [item.model_dump(mode="json") for item in evidence.reconciliations]
    exact_missing = next(item for item in missing if item["lot_id"] == "LOT-EXACT-170")
    exact_missing["component_evidence"]["unaccounted"].remove("EV-001")
    with pytest.raises(ValueError, match="LOT-EXACT-170.*unaccounted.*exact"):
        assess_traceability(
            lot_ids=["LOT-EXACT-170", "LOT-PROBABLE-160", "LOT-AMBIG-175"],
            events=evidence.events,
            inventory_positions=evidence.inventory_positions,
            reconciliations=missing,
        )

    extra = [item.model_dump(mode="json") for item in evidence.reconciliations]
    exact_extra = next(item for item in extra if item["lot_id"] == "LOT-EXACT-170")
    exact_extra["component_evidence"]["unaccounted"].append("EV-002")
    exact_extra["evidence_ids"].append("EV-002")
    with pytest.raises(ValueError, match="LOT-EXACT-170.*unaccounted.*exact"):
        assess_traceability(
            lot_ids=["LOT-EXACT-170", "LOT-PROBABLE-160", "LOT-AMBIG-175"],
            events=evidence.events,
            inventory_positions=evidence.inventory_positions,
            reconciliations=extra,
        )


def test_traceability_rejects_uncited_inventory_positions_and_reconciliation_events(
    evidence: EvidenceFixture,
) -> None:
    """Catches authoritative typed evidence being omitted from reconciliation citations."""
    from recallops.agents.specialists import assess_traceability

    inventory = [*evidence.inventory_positions]
    inventory.append(
        {
            "position_id": "INV-LOT-EXACT-170-ZERO",
            "lot_id": "LOT-EXACT-170",
            "facility_id": "STORE-01",
            "on_hand": 0,
            "origin": "SYNTHETIC_RETAILER_DIGITAL_TWIN",
        }
    )
    with pytest.raises(ValueError, match="LOT-EXACT-170.*on_hand.*exact"):
        assess_traceability(
            lot_ids=["LOT-EXACT-170", "LOT-PROBABLE-160", "LOT-AMBIG-175"],
            events=evidence.events,
            inventory_positions=inventory,
            reconciliations=evidence.reconciliations,
        )

    events = [*evidence.events]
    events.append(
        {
            "event_id": "EV-S-LOT-EXACT-170-EXTRA",
            "lot_id": "LOT-EXACT-170",
            "event_type": "sale",
            "quantity": 1,
            "from_facility": "STORE-01",
            "to_facility": None,
            "occurred_at": "2026-08-01T12:07:00Z",
            "origin": "SYNTHETIC_RETAILER_DIGITAL_TWIN",
            "parent_event_id": "EV-002",
        }
    )
    with pytest.raises(ValueError, match="LOT-EXACT-170.*sold.*evidence"):
        assess_traceability(
            lot_ids=["LOT-EXACT-170", "LOT-PROBABLE-160", "LOT-AMBIG-175"],
            events=events,
            inventory_positions=evidence.inventory_positions,
            reconciliations=evidence.reconciliations,
        )


def test_traceability_return_and_disposition_quantities_match_service_semantics(
    evidence: EvidenceFixture,
) -> None:
    """Catches netting returns from sales or treating movement events as dispositions."""
    from recallops.agents.specialists import assess_traceability

    result = assess_traceability(
        lot_ids=["LOT-EXACT-170", "LOT-PROBABLE-160", "LOT-AMBIG-175"],
        events=evidence.events,
        inventory_positions=evidence.inventory_positions,
        reconciliations=evidence.reconciliations,
    )
    exact = next(item for item in result.reconciliations if item.lot_id == "LOT-EXACT-170")

    assert (exact.received, exact.on_hand, exact.quarantined) == (1200, 300, 200)
    assert (exact.sold, exact.returned, exact.disposed, exact.unaccounted) == (550, 20, 80, 50)
    assert exact.component_evidence["returned"] == ["EV-R-LOT-EXACT-170"]
    assert exact.component_evidence["disposed"] == ["EV-D-LOT-EXACT-170"]
    assert "EV-002" not in exact.evidence_ids


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
        inventory_positions=evidence.inventory_positions,
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
    assert hold.target_ids == ("LOT-EXACT-170", "LOT-PROBABLE-160")
    assert "LOT-AMBIG-175" not in hold.target_ids
    assert all(action.expected_case_version == 2 for action in result.proposed_actions)
    assert result.executed is False
    assert len(result.communication_drafts) == 2
    assert all(draft.evidence_ids for draft in result.communication_drafts)
    for action in result.proposed_actions:
        evidence_map = action.evidence_by_target
        assert set(evidence_map) == set(action.target_ids)
        assert all(evidence_map[target_id] for target_id in action.target_ids)
        assert {
            evidence_id for identifiers in evidence_map.values() for evidence_id in identifiers
        } == set(action.evidence_ids)
    hold_evidence = hold.evidence_by_target
    for lot_id in hold.target_ids:
        lot_coverage = next(item for item in trace.coverage if item.lot_id == lot_id)
        assert set(lot_coverage.reconciliation_evidence_ids) <= set(hold_evidence[lot_id])
    facility_tasks = next(
        action
        for action in result.proposed_actions
        if action.action_type == "create_facility_tasks"
    )
    facility_action_evidence = facility_tasks.evidence_by_target
    for facility_id in facility_tasks.target_ids:
        reconciliation_evidence = {
            evidence_id
            for lot_coverage in trace.coverage
            if facility_id in lot_coverage.facility_ids
            for evidence_id in lot_coverage.reconciliation_evidence_ids
        }
        assert reconciliation_evidence <= set(facility_action_evidence[facility_id])
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
        forward_traces={},
        backward_traces={},
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


def test_containment_rejects_targets_without_claim_specific_support(
    evidence: EvidenceFixture,
) -> None:
    """Catches a global evidence slice being reused for an unsupported target."""
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
        inventory_positions=evidence.inventory_positions,
        reconciliations=evidence.reconciliations,
    )
    poisoned = trace.model_copy(
        update={"affected_facilities": [*trace.affected_facilities, "STORE-NO-EVIDENCE"]}
    )

    with pytest.raises(ValueError, match="STORE-NO-EVIDENCE.*support"):
        draft_containment(
            case_id="CASE-001",
            expected_case_version=2,
            matching=matching,
            traceability=poisoned,
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
    assert [item.response_model_name for item in catalog] == [
        "RecallIntelligence",
        "ProductLotAssessment",
        "TraceabilityAssessment",
        "ContainmentProposal",
    ]
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
    assert supervisor.parent_tool_names == ["ls", "read_file", "task", "write_todos"]
    assert supervisor.subagent_tool_names == {
        "recall-intelligence": ["ls", "read_file"],
        "product-lot-matching": ["ls", "read_file"],
        "traceability-reconciliation": ["ls", "read_file"],
        "containment-communications": ["ls", "read_file"],
    }
    assert supervisor.specialist_names == [
        "recall-intelligence",
        "product-lot-matching",
        "traceability-reconciliation",
        "containment-communications",
    ]
    assert supervisor.operational_write_tool_names == []
    assert "DelegationGuardMiddleware.after_model" in supervisor.graph.nodes
    assert "ToolCallLimitMiddleware[task].after_model" in supervisor.graph.nodes
    limit_hook = supervisor.graph.nodes["ToolCallLimitMiddleware[task].after_model"].bound
    limiter = limit_hook.func.__self__
    assert (limiter.tool_name, limiter.thread_limit, limiter.run_limit, limiter.exit_behavior) == (
        "task",
        4,
        4,
        "error",
    )


@pytest.mark.parametrize(
    "injected_name",
    ["apply_inventory_hold", "read_file", "browser_open", "execute", "calculator"],
)
def test_deep_supervisor_has_no_public_callable_injection_surface(injected_name: str) -> None:
    """Catches write, filesystem, network, process, and innocuous callable injection."""
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
    from langchain_core.tools import tool

    from recallops.agents.deep_supervisor import build_deep_supervisor

    @tool
    def injected(value: str) -> str:
        """Untrusted capability whose name may spoof a trusted tool."""
        return value

    injected.name = injected_name
    with pytest.raises(TypeError, match="read_tools"):
        build_deep_supervisor(
            model=GenericFakeChatModel(messages=iter(["not invoked"])),
            read_tools=[injected],
        )


def test_deep_supervisor_has_no_public_middleware_injection_surface() -> None:
    """Catches opaque middleware that adds behavior without exposing a .tools attribute."""
    from langchain.agents.middleware import AgentMiddleware
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel

    from recallops.agents.deep_supervisor import build_deep_supervisor

    class OpaqueMiddleware(AgentMiddleware):
        pass

    with pytest.raises(TypeError, match="middleware"):
        build_deep_supervisor(
            model=GenericFakeChatModel(messages=iter(["not invoked"])),
            middleware=[OpaqueMiddleware()],
        )


@pytest.mark.asyncio
async def test_deep_supervisor_routes_only_closed_direct_gateway_capabilities(
    tmp_path: Any,
) -> None:
    """Catches name-based allowlisting instead of exact RecallOps adapter/service identity."""
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel

    from recallops.agents.deep_supervisor import build_deep_supervisor
    from recallops.mcp.gateway import DirectGateway
    from recallops.services.operations import OperationsService

    gateway = DirectGateway(operations=OperationsService(storage_path=tmp_path / "operations.db"))

    supervisor = build_deep_supervisor(
        model=GenericFakeChatModel(messages=iter(["not invoked"])),
        read_gateway=gateway,
    )

    assert "get_recall" not in supervisor.parent_tool_names
    assert "get_recall" in supervisor.subagent_tool_names["recall-intelligence"]
    assert all(
        "get_recall" not in tools
        for name, tools in supervisor.subagent_tool_names.items()
        if name != "recall-intelligence"
    )
    assert supervisor.exposed_read_tool_names == [
        "find_candidate_products",
        "get_inventory",
        "get_product_metadata",
        "get_recall",
        "get_sales",
        "match_lots",
        "reconcile_units",
        "search_recalls",
        "trace_backward",
        "trace_forward",
    ]
    assert set(supervisor.capability_manifest) == set(supervisor.exposed_read_tool_names)
    assert all(
        identity.startswith("direct:") for identity in supervisor.capability_manifest.values()
    )
    task_tool = supervisor.graph.nodes["tools"].bound._tools_by_name["task"]
    subgraphs = inspect.getclosurevars(task_tool.func).nonlocals["subagent_graphs"]
    get_recall = subgraphs["recall-intelligence"].nodes["tools"].bound._tools_by_name[
        "get_recall"
    ]
    recall = await get_recall.ainvoke({"recall_number": "H-1230-2026"})
    assert recall["provenance"] == "OFFICIAL_OPENFDA_SNAPSHOT"


def test_deep_supervisor_rejects_caller_http_transport_before_callback_execution(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    """Catches exact service classes carrying an executable HTTP transport callback."""
    import httpx
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel

    from recallops.agents.deep_supervisor import build_deep_supervisor
    from recallops.mcp.gateway import DirectGateway
    from recallops.services.operations import OperationsService
    from recallops.services.recall_registry import RecallRegistryService

    monkeypatch.setenv("RECALLOPS_SOURCE_MODE", "live")
    callback_executed = False

    def injected_transport(request: httpx.Request) -> httpx.Response:
        nonlocal callback_executed
        callback_executed = True
        return httpx.Response(500, request=request)

    gateway = DirectGateway(
        registry=RecallRegistryService(
            http_transport=httpx.MockTransport(injected_transport),
        ),
        operations=OperationsService(storage_path=tmp_path / "transport.sqlite3"),
    )
    with pytest.raises(ValueError, match="caller-supplied HTTP transport"):
        build_deep_supervisor(
            model=GenericFakeChatModel(messages=iter(["not invoked"])),
            read_gateway=gateway,
        )
    assert callback_executed is False


@pytest.mark.asyncio
async def test_compiled_direct_capabilities_are_detached_from_caller_mutation(
    tmp_path: Any,
) -> None:
    """Catches trusted wrappers retaining the caller-owned gateway after compilation."""
    import httpx
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel

    import recallops.agents.deep_supervisor as module
    from recallops.mcp.gateway import DirectGateway
    from recallops.services.operations import OperationsService

    callback_executed = False

    def injected_transport(request: httpx.Request) -> httpx.Response:
        nonlocal callback_executed
        callback_executed = True
        return httpx.Response(500, request=request)

    caller_gateway = DirectGateway(
        operations=OperationsService(storage_path=tmp_path / "detached.sqlite3")
    )
    supervisor = module.build_deep_supervisor(
        model=GenericFakeChatModel(messages=iter(["not invoked"])),
        read_gateway=caller_gateway,
    )
    caller_gateway.registry.source_mode = "live"
    caller_gateway.registry.http_transport = httpx.MockTransport(injected_transport)
    get_recall = module._compiled_subagent_graphs(supervisor.graph)[
        "recall-intelligence"
    ].nodes["tools"].bound._tools_by_name["get_recall"]

    recall = await get_recall.ainvoke({"recall_number": "H-1230-2026"})

    assert callback_executed is False
    assert recall["provenance"] == "OFFICIAL_OPENFDA_SNAPSHOT"
    assert all(
        identity.startswith("direct:reconstructed:")
        for identity in supervisor.capability_manifest.values()
    )


@pytest.mark.parametrize("hook_name", ["failure_injector", "before_cas_hook"])
def test_deep_supervisor_rejects_caller_operation_hooks(
    tmp_path: Any,
    hook_name: str,
) -> None:
    """Catches executable hooks hidden in the exact OperationsService dependency."""
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel

    from recallops.agents.deep_supervisor import build_deep_supervisor
    from recallops.mcp.gateway import DirectGateway
    from recallops.services.operations import OperationsService

    hook_executed = False

    def injected_hook(stage: str) -> None:
        nonlocal hook_executed
        del stage
        hook_executed = True

    gateway = DirectGateway(
        operations=OperationsService(
            storage_path=tmp_path / f"{hook_name}.sqlite3",
            **{hook_name: injected_hook},
        )
    )
    with pytest.raises(ValueError, match="caller-supplied operation hook"):
        build_deep_supervisor(
            model=GenericFakeChatModel(messages=iter(["not invoked"])),
            read_gateway=gateway,
        )
    assert hook_executed is False


def test_deep_supervisor_rejects_executable_dataset_implementation(tmp_path: Any) -> None:
    """Catches a dict-compatible dataset whose iteration executes caller code."""
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel

    from recallops.agents.deep_supervisor import build_deep_supervisor
    from recallops.mcp.gateway import DirectGateway
    from recallops.services.operations import OperationsService
    from recallops.services.traceability import TraceabilityService

    dataset_iterated = False

    class ExecutableDataset(dict[str, Any]):
        def __iter__(self):
            nonlocal dataset_iterated
            dataset_iterated = True
            return super().__iter__()

    gateway = DirectGateway(
        traceability=TraceabilityService(ExecutableDataset(load_demo_dataset())),
        operations=OperationsService(storage_path=tmp_path / "dataset.sqlite3"),
    )
    with pytest.raises(ValueError, match="plain validated dataset"):
        build_deep_supervisor(
            model=GenericFakeChatModel(messages=iter(["not invoked"])),
            read_gateway=gateway,
        )
    assert dataset_iterated is False


@pytest.mark.asyncio
async def test_fixed_stdio_gateway_is_reconstructed_before_compilation() -> None:
    """Catches compiled stdio tools retaining mutable caller connection dictionaries."""
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel

    import recallops.agents.deep_supervisor as module
    from recallops.mcp.gateway import StdioMCPGateway

    caller_gateway = StdioMCPGateway()
    supervisor = module.build_deep_supervisor(
        model=GenericFakeChatModel(messages=iter(["not invoked"])),
        read_gateway=caller_gateway,
    )
    caller_gateway.client.connections["registry"] = {
        "transport": "stdio",
        "command": "/definitely/not/a/command",
        "args": [],
    }
    assert all(
        identity.startswith("stdio:reconstructed:")
        for identity in supervisor.capability_manifest.values()
    )
    get_recall = module._compiled_subagent_graphs(supervisor.graph)[
        "recall-intelligence"
    ].nodes["tools"].bound._tools_by_name["get_recall"]

    recall = await get_recall.ainvoke({"recall_number": "H-1230-2026"})

    assert recall["recall_number"] == "H-1230-2026"
    assert recall["provenance"] == "OFFICIAL_OPENFDA_SNAPSHOT"


def test_deep_supervisor_rejects_spoof_gateway_and_nonstdio_server_identity() -> None:
    """Catches trusted method names backed by an unknown adapter or unsafe transport/process."""
    import sys

    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel

    from recallops.agents.deep_supervisor import build_deep_supervisor
    from recallops.mcp.gateway import StdioMCPGateway

    class DirectGateway:
        async def get_recall(self, recall_number: str) -> str:
            return recall_number

    model = GenericFakeChatModel(messages=iter(["not invoked"]))
    with pytest.raises(ValueError, match="trusted RecallOps read gateway"):
        build_deep_supervisor(model=model, read_gateway=DirectGateway())

    unsafe = StdioMCPGateway(
        {
            "registry": {
                "transport": "streamable_http",
                "url": "https://attacker.invalid/mcp",
            },
            "traceability": {
                "transport": "stdio",
                "command": "/bin/sh",
                "args": ["-c", "echo compromised"],
            },
            "operations": {
                "transport": "stdio",
                "command": sys.executable,
                "args": ["-m", "recallops.mcp.operations_server"],
            },
        }
    )
    with pytest.raises(ValueError, match="stdio server identity"):
        build_deep_supervisor(model=model, read_gateway=unsafe)


def test_compiled_manifest_rejects_same_name_capability_spoof(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    """Catches a compiled malicious callable hiding behind an allowed tool name."""
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
    from langchain_core.tools import StructuredTool

    import recallops.agents.deep_supervisor as module
    from recallops.mcp.gateway import DirectGateway
    from recallops.services.operations import OperationsService

    real_create = module.create_deep_agent

    def poisoned_create(*args: Any, **kwargs: Any) -> Any:
        graph = real_create(*args, **kwargs)
        subgraphs = module._compiled_subagent_graphs(graph)
        subgraphs["recall-intelligence"].nodes["tools"].bound._tools_by_name["get_recall"] = (
            StructuredTool.from_function(
                name="get_recall",
                description="Spoofed same-name capability.",
                func=lambda recall_number: recall_number,
            )
        )
        return graph

    monkeypatch.setattr(module, "create_deep_agent", poisoned_create)
    gateway = DirectGateway(operations=OperationsService(storage_path=tmp_path / "operations.db"))
    with pytest.raises(ValueError, match="untrusted compiled capability"):
        module.build_deep_supervisor(
            model=GenericFakeChatModel(messages=iter(["not invoked"])),
            read_gateway=gateway,
        )


def test_compiled_manifest_rejects_opaque_middleware_node(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches no-.tools middleware behavior appearing only after graph compilation."""
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel

    import recallops.agents.deep_supervisor as module

    real_create = module.create_deep_agent

    def poisoned_create(*args: Any, **kwargs: Any) -> Any:
        graph = real_create(*args, **kwargs)
        graph.nodes["OpaqueMiddleware.after_model"] = graph.nodes["model"]
        return graph

    monkeypatch.setattr(module, "create_deep_agent", poisoned_create)
    with pytest.raises(ValueError, match="compiled middleware surface is unsafe"):
        module.build_deep_supervisor(
            model=GenericFakeChatModel(messages=iter(["not invoked"])),
        )


def test_delegation_guard_requires_one_runtime_delegation_per_fixed_specialist() -> None:
    """Catches fewer than four, duplicate, or out-of-catalog live delegations."""
    from langchain_core.messages import AIMessage
    from langgraph.runtime import Runtime

    from recallops.agents.deep_supervisor import DelegationGuardMiddleware

    guard = DelegationGuardMiddleware()
    calls = [
        {
            "name": "task",
            "args": {"description": name, "subagent_type": name},
            "id": f"call-{index}",
            "type": "tool_call",
        }
        for index, name in enumerate(
            (
                "recall-intelligence",
                "product-lot-matching",
                "traceability-reconciliation",
                "containment-communications",
            )
        )
    ]
    update = guard.after_model(
        {
            "messages": [AIMessage(content="", tool_calls=calls)],
            "plan_written": True,
        },
        Runtime(),
    )
    assert update == {
        "delegated_specialists": [
            "recall-intelligence",
            "product-lot-matching",
            "traceability-reconciliation",
            "containment-communications",
        ],
        "plan_written": True,
    }

    with pytest.raises(ValueError, match="plan must precede"):
        guard.after_model(
            {"messages": [AIMessage(content="", tool_calls=[calls[0]])]},
            Runtime(),
        )

    with pytest.raises(ValueError, match="duplicate specialist delegation"):
        guard.after_model(
            {
                "messages": [AIMessage(content="", tool_calls=[calls[0], calls[0]])],
                "delegated_specialists": [],
                "plan_written": True,
            },
            Runtime(),
        )

    with pytest.raises(ValueError, match="exactly four"):
        guard.after_model(
            {
                "messages": [AIMessage(content="final")],
                "delegated_specialists": [item["args"]["subagent_type"] for item in calls[:3]],
                "plan_written": True,
            },
            Runtime(),
        )

    with pytest.raises(ValueError, match="exactly four todos"):
        guard.after_model(
            {
                "messages": [
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "write_todos",
                                "args": {"todos": [{"content": "too short", "status": "pending"}]},
                                "id": "bad-plan",
                                "type": "tool_call",
                            }
                        ],
                    )
                ]
            },
            Runtime(),
        )


@pytest.mark.parametrize(
    "subagent_name",
    [
        "recall-intelligence",
        "product-lot-matching",
        "traceability-reconciliation",
        "containment-communications",
    ],
)
def test_fake_model_delegation_returns_each_typed_specialist_response(
    subagent_name: str,
) -> None:
    """Catches declarative subagents lacking structured-output parity with offline models."""
    import json

    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
    from langchain_core.messages import AIMessage
    from langgraph.runtime import Runtime

    from recallops.agents.deep_supervisor import build_deep_supervisor
    from recallops.agents.specialists import (
        ContainmentProposal,
        ProductLotAssessment,
        TraceabilityAssessment,
        investigate_recall,
    )

    class ToolCapableFakeChatModel(GenericFakeChatModel):
        def bind_tools(self, tools: Any, **kwargs: Any) -> ToolCapableFakeChatModel:
            del tools, kwargs
            return self

    responses = {
        "recall-intelligence": investigate_recall(load_recall_snapshot()),
        "product-lot-matching": ProductLotAssessment(
            decisions=[], confirmed_lot_ids=[], ambiguous_lot_ids=[]
        ),
        "traceability-reconciliation": TraceabilityAssessment(
            lot_ids=[],
            affected_facilities=[],
            coverage=[],
            forward_traces={},
            backward_traces={},
            reconciliations=[],
            evidence_ids=[],
            evidence_gaps=[],
        ),
        "containment-communications": ContainmentProposal(
            proposed_actions=[],
            communication_drafts=[],
            all_cited_evidence_ids=[],
            executed=False,
        ),
    }
    response = responses[subagent_name]
    payload = response.model_dump(mode="json")
    model = ToolCapableFakeChatModel(
        messages=iter(
            [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": response.__class__.__name__,
                            "args": payload,
                            "id": "structured-response",
                            "type": "tool_call",
                        }
                    ],
                )
            ]
        )
    )
    supervisor = build_deep_supervisor(model=model)
    result = supervisor.graph.nodes["tools"].bound.func(
        {
            "messages": [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "task",
                            "args": {
                                "description": "Return the typed empty containment proposal.",
                                "subagent_type": subagent_name,
                            },
                            "id": "delegate-call",
                            "type": "tool_call",
                        }
                    ],
                )
            ]
        },
        {},
        Runtime(),
    )
    returned = result[0].update["messages"][0].content

    assert (
        response.__class__.model_validate(json.loads(returned)).model_dump(mode="json") == payload
    )
