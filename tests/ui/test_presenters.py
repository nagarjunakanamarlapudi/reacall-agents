from __future__ import annotations

from dataclasses import asdict

import pytest

from recallops.ui.presenters import (
    APPROVAL_JUSTIFICATION,
    EQUATION,
    PINNED_RECALL,
    VIEWS,
    build_case_header,
    build_closure_gate_rows,
    build_evaluation_rows,
    build_evidence_rows,
    build_lineage_rows,
    build_match_rows,
    build_predicate_rows,
    build_receipt_rows,
    build_reconciliation_presentation,
    build_retrieval_rows,
    build_review_packet,
    build_timeline_rows,
    can_simulate,
    initialize_ui_state,
    mask_display_value,
    reduce_case_snapshot,
    source_badge,
    validate_review_submission,
)


def _raw_case() -> dict:
    return {
        "recall_number": PINNED_RECALL,
        "case_id": "CASE-H-1230-2026",
        "thread_id": "THREAD-H-1230-2026",
        "case_version": 0,
        "status": "review_required",
        "source_mode": "snapshot",
        "model_mode": "deterministic",
        "current_node": "action_review",
        "recall": {
            "predicate": {
                "product": "Grade A shell eggs",
                "upcs": ["011110609038"],
                "plant_codes": ["P-1950", "0840962"],
                "julian_start": 157,
                "julian_end": 184,
                "geography": ["Texas", "Oklahoma"],
                "hazard": "Possible Salmonella Enteritidis",
            },
            "citations": [
                {
                    "citation_id": "FDA-H-1230-2026",
                    "source": "OFFICIAL_OPENFDA_SNAPSHOT",
                    "url": "https://api.fda.gov/food/enforcement.json",
                    "observation": "Official notice predicate",
                }
            ],
        },
        "matches": [
            {
                "product": "Northstar Grade A Large Eggs 12 ct",
                "upc": "011110609038",
                "lot_id": "LOT-EXACT-170",
                "plant_code": "P-1950",
                "julian_date": 170,
                "classification": "exact",
                "rationale": "UPC, plant and date are in scope.",
                "evidence_ids": ["LOT-EXACT-170", "EVT-001"],
                "facility_ids": ["DC-NORTH"],
                "source": "SYNTHETIC_RETAILER_DIGITAL_TWIN",
            },
            {
                "product": "Northstar Cage Free Large Eggs 12 ct",
                "upc": "011110609038",
                "lot_id": "LOT-AMBIG-175",
                "plant_code": "P-1950?",
                "julian_date": 175,
                "classification": "ambiguous",
                "rationale": "Plant code contains an unresolved character.",
                "evidence_ids": ["LOT-AMBIG-175", "EVT-002"],
                "facility_ids": ["STORE-07"],
                "source": "SYNTHETIC_RETAILER_DIGITAL_TWIN",
            },
        ],
        "lineage": [
            {
                "event_id": "EVT-002",
                "lot_id": "LOT-EXACT-170",
                "event_type": "shipping",
                "occurred_at": "2026-06-20T10:00:00+00:00",
                "from_facility": "DC-NORTH",
                "to_facility": "STORE-01",
                "quantity": 100,
                "unit": "units",
                "customer_email": "buyer@example.com",
                "source": "SYNTHETIC_RETAILER_DIGITAL_TWIN",
            },
            {
                "event_id": "EVT-001",
                "lot_id": "LOT-EXACT-170",
                "event_type": "receiving",
                "occurred_at": "2026-06-19T10:00:00+00:00",
                "from_facility": "SUPPLIER",
                "to_facility": "DC-NORTH",
                "quantity": 1200,
                "unit": "units",
                "source": "SYNTHETIC_RETAILER_DIGITAL_TWIN",
            },
        ],
        "evidence": [
            {
                "citation_id": "EVT-001",
                "scope": "LOT-EXACT-170",
                "source": "SYNTHETIC_RETAILER_DIGITAL_TWIN",
                "observation": "Digital-twin receiving event",
            }
        ],
        "reconciliation": {
            "unit": "units",
            "totals": {
                "received": 1200,
                "on_hand": 300,
                "quarantined": 200,
                "sold": 550,
                "returned": 20,
                "disposed": 80,
                "unaccounted": 50,
            },
            "rows": [
                {
                    "product": "Northstar Grade A Large Eggs 12 ct",
                    "lot_id": "LOT-EXACT-170",
                    "facility": "All traced facilities",
                    "received": 1200,
                    "on_hand": 300,
                    "quarantined": 200,
                    "sold": 550,
                    "returned": 20,
                    "disposed": 80,
                    "unaccounted": 50,
                    "evidence_ids": ["EVT-001"],
                    "source": "SYNTHETIC_RETAILER_DIGITAL_TWIN",
                }
            ],
            "gaps": [
                {
                    "gap_type": "quantity discrepancy",
                    "impact": "50 units remain unaccounted",
                    "evidence_id": "EVT-001",
                    "closure_implication": "Blocks closure",
                }
            ],
        },
        "facilities": [
            {
                "facility_id": "STORE-07",
                "acknowledged": False,
                "source": "SYNTHETIC_RETAILER_DIGITAL_TWIN",
            }
        ],
        "proposed_actions": [
            {
                "action_id": "ACTION-CREATE-CASE",
                "action_type": "create_case",
                "summary": "Create the simulated operations case.",
                "target_ids": ["CASE-H-1230-2026"],
                "source": "SYNTHETIC_RETAILER_DIGITAL_TWIN",
                "digest": "digest-1",
                "expected_version": 0,
            }
        ],
        "pending_interrupt": {
            "kind": "action_review",
            "scope": "create_case",
            "case_id": "CASE-H-1230-2026",
            "thread_id": "THREAD-H-1230-2026",
            "expected_version": 0,
            "action_digest": "digest-1",
        },
        "verification": {
            "outcome": "review_required",
            "confidence": 0.87,
            "contradictions": ["Ambiguous plant code P-1950?"],
        },
        "retrieval": {
            "mode": "agentic_rag",
            "queries": [
                {
                    "hop": 1,
                    "query": "H-1230-2026 plant date scope",
                    "sparse_hits": 8,
                    "dense_hits": 8,
                    "fused_hits": 8,
                    "reranked_hits": 4,
                    "critic": "sufficient",
                }
            ],
            "citations": ["FDA-H-1230-2026", "policy-recall-closure"],
        },
        "node_trace": [
            {
                "order": 1,
                "node": "intake",
                "route": "START → intake",
                "actor": "system",
                "classification": "read",
                "status": "complete",
                "case_version": 0,
            }
        ],
        "tool_trace": [
            {
                "order": 2,
                "node": "retrieve_context",
                "route": "registry.hybrid_search",
                "actor": "system",
                "specialist": "Regulatory Intake",
                "tool": "recall_registry_hybrid_search",
                "server": "Recall Registry MCP",
                "classification": "read",
                "status": "complete",
                "case_version": 0,
            }
        ],
        "review_history": [],
        "receipts": [],
        "closure": {
            "status": "Open — closure blocked",
            "gates": [
                {"gate": "Reconciliation", "state": "block", "detail": "50 unaccounted"},
                {
                    "gate": "Ambiguous matches",
                    "state": "block",
                    "detail": "LOT-AMBIG-175 requires review",
                },
            ],
            "blockers": ["50 units unaccounted", "Ambiguous lot remains"],
        },
        "evaluation_report": {
            "scenarios": [
                {
                    "scenario": "approval guard",
                    "expected": "zero writes before approval",
                    "safety_critical": True,
                    "passed": True,
                    "observed": "blocked",
                    "exception": None,
                }
            ]
        },
    }


def test_initialize_ui_state_has_every_locked_default() -> None:
    state: dict = {}
    initialize_ui_state(state)

    assert state["ui_active_view"] == "Command Center"
    assert state["ui_recall_number"] == "H-1230-2026"
    assert state["ui_model_mode"] == "deterministic"
    assert state["ui_decision"] == "approve"
    assert state["ui_actor"] == "Food-safety manager"
    assert state["ui_justification"] == APPROVAL_JUSTIFICATION
    assert set(VIEWS) == {
        "Command Center",
        "Investigation",
        "Reconciliation",
        "Human Review",
        "Audit & Evaluation",
    }
    assert len(state) == 26


@pytest.mark.parametrize(
    ("source", "label"),
    [
        ("LIVE_OPENFDA", "OFFICIAL — openFDA"),
        ("OFFICIAL_OPENFDA_SNAPSHOT", "OFFICIAL — openFDA snapshot"),
        ("SYNTHETIC_RETAILER_DIGITAL_TWIN", "SYNTHETIC — ACADEMIC DEMO"),
        ({"kind": "official_guidance"}, "OFFICIAL — guidance/reference"),
        (None, "SOURCE — UNKNOWN"),
    ],
)
def test_source_badges_do_not_promote_unknown_sources(source: object, label: str) -> None:
    assert source_badge(source).label == label


def test_case_header_and_predicate_show_only_supplied_truth() -> None:
    case = reduce_case_snapshot(_raw_case())
    header = build_case_header(case)
    assert asdict(header) == {
        "recall_number": "H-1230-2026",
        "case_id": "CASE-H-1230-2026",
        "thread_id": "THREAD-H-1230-2026",
        "case_version": "0",
        "status": "review_required",
        "source_mode": "OFFICIAL — openFDA snapshot",
        "model_mode": "Deterministic offline planner",
        "current_node": "action_review",
    }
    rows = {row.label: row.value for row in build_predicate_rows(case)}
    assert rows["Plant codes"] == "P-1950, 0840962"
    assert rows["Julian date"] == "157–184"
    assert "Package" not in rows


def test_case_snapshot_rejects_coercive_safety_bindings() -> None:
    raw = _raw_case()
    raw.update(case_id=123, thread_id=["THREAD"], case_version=True)
    case = reduce_case_snapshot(raw)
    assert case.case_id is None
    assert case.thread_id is None
    assert case.case_version is None
    issues = validate_review_submission("approve", "actor", "why", case)
    assert any(issue.field == "case" for issue in issues)


def test_review_guard_rejects_bool_version_even_when_equal_to_zero() -> None:
    raw = _raw_case()
    raw["pending_interrupt"]["expected_version"] = False
    issues = validate_review_submission(
        "approve", "Food-safety manager", "Scoped approval.", reduce_case_snapshot(raw)
    )
    assert any(issue.field == "case_version" for issue in issues)


def test_closure_review_uses_the_same_strict_human_decision_contract() -> None:
    raw = _raw_case()
    raw["pending_interrupt"]["kind"] = "closure_review"
    issues = validate_review_submission(
        "edit",
        "Food-safety manager",
        "Keep this in closure review after the rationale edit.",
        reduce_case_snapshot(raw),
        "Clarify the closure rationale; do not change the action or scope.",
    )
    assert issues == []


def test_execution_guard_rejects_coercive_approval_metadata() -> None:
    raw = _raw_case()
    raw["pending_interrupt"] = {
        **raw["pending_interrupt"],
        "kind": "execution_confirmation",
        "idempotency_key": "idem-1",
    }
    raw["approval"] = {
        "decision": "approve",
        "case_id": raw["case_id"],
        "thread_id": raw["thread_id"],
        "expected_version": raw["case_version"],
        "action_digest": raw["proposed_actions"][0]["digest"],
        "idempotency_key": "idem-1",
        "actor": 123,
        "justification": "Scoped approval.",
    }
    allowed, reason = can_simulate(reduce_case_snapshot(raw))
    assert not allowed
    assert "does not match" in reason


def test_match_rows_keep_classification_rationale_and_review_flag() -> None:
    rows = build_match_rows(reduce_case_snapshot(_raw_case()))
    assert [row.classification for row in rows] == ["exact", "ambiguous"]
    assert rows[1].review_flag == "Human review required"
    assert rows[0].source == "SYNTHETIC — ACADEMIC DEMO"
    assert rows[0].evidence_count == 2
    assert build_match_rows(reduce_case_snapshot({"recall_number": PINNED_RECALL})) == []


def test_lineage_is_chronological_and_customer_values_are_masked() -> None:
    case = reduce_case_snapshot(_raw_case())
    rows = build_lineage_rows(case, "LOT-EXACT-170")
    assert [row.event_id for row in rows] == ["EVT-001", "EVT-002"]
    assert all(row.source == "SYNTHETIC — ACADEMIC DEMO" for row in rows)
    evidence = build_evidence_rows(case, "LOT-EXACT-170")
    assert evidence[0].citation_id == "EVT-001"
    assert "Northstar was involved" not in evidence[0].observation


def test_reconciliation_keeps_unknown_values_and_blocks_positive_gap() -> None:
    raw = _raw_case()
    raw["reconciliation"]["totals"]["disposed"] = None
    presentation = build_reconciliation_presentation(reduce_case_snapshot(raw))
    assert presentation.equation == EQUATION
    assert presentation.totals["disposed"] == "Unknown"
    assert presentation.totals["unaccounted"] == "50 units"
    assert presentation.balanced is None
    assert any("50 units remain unaccounted" in gap.impact for gap in presentation.gaps)


def test_review_packet_contains_scope_evidence_gaps_actions_and_trace() -> None:
    raw = _raw_case()
    raw["pending_interrupt"]["remaining_action_types"] = [
        "apply_inventory_hold",
        "record_acknowledgment",
        "record_disposition",
        "close_case",
    ]
    packet = build_review_packet(reduce_case_snapshot(raw))
    assert packet is not None
    assert packet.scope == "create_case"
    assert packet.case_version == 0
    assert packet.matches[1].review_flag == "Human review required"
    assert packet.reconciliation.gaps
    assert packet.proposed_actions[0]["Action type"] == "create_case"
    assert packet.remaining_actions == [
        "apply_inventory_hold",
        "record_acknowledgment",
        "record_disposition",
        "close_case",
    ]
    assert packet.citations[0].source == "OFFICIAL — openFDA snapshot"
    assert packet.timeline
    raw = _raw_case()
    raw["pending_interrupt"] = None
    assert build_review_packet(reduce_case_snapshot(raw)) is None
    raw["pending_interrupt"] = {"kind": "execution_confirmation"}
    assert build_review_packet(reduce_case_snapshot(raw)) is None


@pytest.mark.parametrize("decision", ["approve", "reject", "escalate"])
def test_valid_review_decisions_pass_without_side_effects(decision: str) -> None:
    assert (
        validate_review_submission(
            decision,
            "Food-safety manager",
            APPROVAL_JUSTIFICATION,
            reduce_case_snapshot(_raw_case()),
        )
        == []
    )


def test_review_validation_rejects_blank_invalid_stale_and_unedited_edit() -> None:
    case = reduce_case_snapshot(_raw_case())
    messages = {issue.message for issue in validate_review_submission("ship", " ", " ", case)}
    assert "Decision must be approve, edit, reject, or escalate." in messages
    assert "Actor is required." in messages
    assert "Justification is required." in messages
    assert validate_review_submission("edit", "actor", "why", case)[0].field == "edited_action"
    raw = _raw_case()
    raw["pending_interrupt"]["expected_version"] = 1
    stale = validate_review_submission("approve", "actor", "why", reduce_case_snapshot(raw))
    assert any(issue.field == "case_version" for issue in stale)


def test_simulation_guard_requires_exact_approval_binding_and_key() -> None:
    raw = _raw_case()
    case = reduce_case_snapshot(raw)
    allowed, reason = can_simulate(case)
    assert not allowed and "approval" in reason.lower()

    raw["approval"] = {
        "case_id": raw["case_id"],
        "thread_id": raw["thread_id"],
        "expected_version": 0,
        "action_digest": "digest-1",
        "actor": "Food-safety manager",
        "justification": APPROVAL_JUSTIFICATION,
        "idempotency_key": "demo-create-v0",
        "decision": "approve",
    }
    raw["pending_interrupt"] = {
        **raw["pending_interrupt"],
        "kind": "execution_confirmation",
        "idempotency_key": "demo-create-v0",
    }
    assert can_simulate(reduce_case_snapshot(raw)) == (
        True,
        "Approval matches the current action and case version.",
    )
    raw["approval"]["expected_version"] = 1
    assert can_simulate(reduce_case_snapshot(raw))[0] is False


def test_receipts_and_timeline_are_masked_and_ordered() -> None:
    raw = _raw_case()
    raw["receipts"] = [
        {
            "receipt_id": "R-1",
            "action": "create_case",
            "case_id": raw["case_id"],
            "case_version": 1,
            "idempotency_result": "new",
            "timestamp": "2026-08-30T12:00:00+00:00",
            "customer_email": "person@example.com",
            "source": "SYNTHETIC_RETAILER_DIGITAL_TWIN",
        }
    ]
    receipts = build_receipt_rows(reduce_case_snapshot(raw))
    assert receipts[0].status == "Simulated action recorded"
    assert receipts[0].source == "SYNTHETIC — ACADEMIC DEMO"
    assert "person@example.com" not in str(asdict(receipts[0]))
    timeline = build_timeline_rows(reduce_case_snapshot(raw))
    assert [row.order for row in timeline] == [1, 2]
    assert timeline[1].tool == "recall_registry_hybrid_search"
    assert timeline[1].source == "SOURCE — UNKNOWN"


def test_repeated_action_receipts_remain_visible_as_distinct_versions() -> None:
    raw = _raw_case()
    raw["receipts"] = [
        {
            "receipt_id": "R-ACK-1",
            "action_type": "record_acknowledgment",
            "case_id": raw["case_id"],
            "case_version": 4,
            "source": "SYNTHETIC_RETAILER_DIGITAL_TWIN",
        },
        {
            "receipt_id": "R-ACK-2",
            "action_type": "record_acknowledgment",
            "case_id": raw["case_id"],
            "case_version": 5,
            "source": "SYNTHETIC_RETAILER_DIGITAL_TWIN",
        },
    ]
    receipts = build_receipt_rows(reduce_case_snapshot(raw))
    assert [row.receipt_id for row in receipts] == ["R-ACK-1", "R-ACK-2"]
    assert [row.case_version for row in receipts] == ["4", "5"]


def test_retrieval_rows_prove_sparse_dense_fusion_rerank_and_critic() -> None:
    rows = build_retrieval_rows(reduce_case_snapshot(_raw_case()))
    assert asdict(rows[0]) == {
        "hop": 1,
        "query": "H-1230-2026 plant date scope",
        "sparse": "BM25 · 8 hits",
        "dense": "LSA dense · 8 hits",
        "fusion": "RRF · 8 hits",
        "rerank": "Deterministic rerank · 4 hits",
        "critic": "sufficient",
    }


def test_closure_rows_never_treat_all_pass_as_closed() -> None:
    case = reduce_case_snapshot(_raw_case())
    rows = build_closure_gate_rows(case)
    assert rows[0].state == "block"
    assert case.raw["closure"]["status"] == "Open — closure blocked"
    raw = _raw_case()
    raw["closure"] = {
        "status": "closure_review_required",
        "gates": [{"gate": "Reconciliation", "state": "pass", "detail": "balanced"}],
        "blockers": [],
    }
    all_pass = build_closure_gate_rows(reduce_case_snapshot(raw))
    assert all_pass[0].state == "pass"
    assert "human" in all_pass[0].next_step.lower()
    raw["closure"] = {
        "status": "closure_review_required",
        "gates": [
            {
                "gate": "Final closure action",
                "state": "review",
                "detail": "Version-bound human review is pending",
            }
        ],
        "blockers": [],
    }
    review = build_closure_gate_rows(reduce_case_snapshot(raw))
    assert review[0].state == "review"
    assert "human closure review" in review[0].next_step.lower()


def test_evaluation_missing_is_not_success() -> None:
    assert build_evaluation_rows(None) == []
    rows = build_evaluation_rows(_raw_case()["evaluation_report"])
    assert rows[0].safety_critical == "Yes"
    assert rows[0].result == "PASS"


def test_safe_display_masks_sensitive_and_escapes_untrusted_text() -> None:
    assert mask_display_value("person@example.com", "customer_email") == "[MASKED]"
    assert mask_display_value("sk-secret-value", "api_key") == "[MASKED]"
    escaped = mask_display_value("<script>alert('x')</script>")
    assert "<script>" not in escaped
    assert "&lt;script&gt;" in escaped
