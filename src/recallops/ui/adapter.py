"""Injectable UI adapter plus a credential-free deterministic demonstration.

The protocol is deliberately shaped around the locked ``RecallOpsRuntime``
result boundary: every action accepts the latest case snapshot and returns a
JSON-only snapshot.  The deterministic adapter is explicit training state; it
does not claim durable LangGraph recovery or live system connectivity.
"""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Mapping
from typing import Any, Protocol

from recallops.agents.planner import plan_investigation
from recallops.agents.specialists import investigate_recall
from recallops.data.loaders import load_demo_dataset, load_recall_snapshot
from recallops.services.traceability import TraceabilityService
from recallops.ui.presenters import PINNED_RECALL

SYNTHETIC = "SYNTHETIC_RETAILER_DIGITAL_TWIN"
SNAPSHOT = "OFFICIAL_OPENFDA_SNAPSHOT"
FIXED_TIME = "2026-08-30T12:00:00+00:00"

FAILURE_SCENARIOS = (
    "openFDA unavailable → labelled frozen snapshot",
    "read timeout/429 → bounded retry then circuit-open/fallback",
    "ambiguous lot → pause/no auto-hold",
    "missing shipment/quantity discrepancy → gap/closure blocker",
    "unacknowledged store/facility → open/follow-up after approval",
    "lost write response → same-key replay",
    "stale version → refresh/review",
    "repeated graph progress → watchdog escalation",
    "model error/budget → deterministic fallback",
)


class RecallOpsUIAdapter(Protocol):
    async def open_case(self, recall_number: str) -> dict[str, Any]: ...

    async def run_investigation(self, case: Mapping[str, Any]) -> dict[str, Any]: ...

    async def resume_review(
        self,
        case: Mapping[str, Any],
        *,
        decision: str,
        actor: str,
        justification: str,
        edited_action: str,
    ) -> dict[str, Any]: ...

    async def simulate_approved_actions(self, case: Mapping[str, Any]) -> dict[str, Any]: ...

    async def request_closure(self, case: Mapping[str, Any]) -> dict[str, Any]: ...

    async def inject_failure(self, case: Mapping[str, Any], scenario: str) -> dict[str, Any]: ...


def normalize_runtime_result(result: Any) -> dict[str, Any]:
    """Normalize the locked RuntimeResult without importing the runtime early.

    This is the only seam the future LangGraph adapter needs: ``RuntimeResult``
    exposes ``case``, ``pending_interrupt``, ``next_nodes`` and
    ``checkpoint_id``.  Mapping fixtures with the same fields are accepted for
    integration tests.
    """

    if hasattr(result, "model_dump"):
        payload = result.model_dump(mode="json")
    elif isinstance(result, Mapping):
        payload = copy.deepcopy(dict(result))
    else:
        raise TypeError("runtime result must be a mapping or Pydantic model")
    case = payload.get("case")
    if not isinstance(case, Mapping):
        raise ValueError("runtime result lacks a case mapping")
    normalized = copy.deepcopy(dict(case))
    normalized["pending_interrupt"] = copy.deepcopy(payload.get("pending_interrupt"))
    normalized["next_nodes"] = list(payload.get("next_nodes") or [])
    normalized["checkpoint_id"] = payload.get("checkpoint_id")
    return normalized


class DeterministicDemoAdapter:
    """A state-validating offline walkthrough built from checked-in data."""

    available_failure_scenarios = FAILURE_SCENARIOS

    async def open_case(self, recall_number: str) -> dict[str, Any]:
        recall_number = recall_number.strip()
        if recall_number != PINNED_RECALL:
            raise ValueError(f"Only the pinned frozen recall {PINNED_RECALL} is available offline.")
        recall = load_recall_snapshot(recall_number)
        intelligence = investigate_recall(recall)
        predicate = intelligence.predicate.model_dump(mode="json")
        predicate["product"] = "Grade A shell eggs"
        return {
            "recall_number": recall_number,
            "case_id": f"CASE-{recall_number}",
            "thread_id": f"THREAD-{recall_number}",
            "case_version": 0,
            "status": "investigating",
            "source_mode": "snapshot",
            "source_detail": "Cached/frozen fallback",
            "model_mode": "deterministic",
            "runtime_mode": "Deterministic UI adapter — not durable runtime recovery",
            "current_node": "intake",
            "recall": {
                "summary": {
                    "recall_number": recall.recall_number,
                    "product": "Grade A shell eggs",
                    "classification": recall.payload.get("classification"),
                    "status": recall.payload.get("status"),
                    "hazard": recall.payload.get("reason_for_recall"),
                },
                "predicate": predicate,
                "citations": [
                    {
                        "citation_id": f"FDA-{recall.recall_number}",
                        "source": recall.provenance,
                        "url": recall.source_url,
                        "observation": "Frozen, checksummed openFDA enforcement notice.",
                    }
                ],
            },
            "matches": [],
            "lineage": [],
            "evidence": [],
            "facilities": [],
            "proposed_actions": [],
            "pending_interrupt": None,
            "approval": None,
            "review_history": [],
            "receipts": [],
            "reconciliation": None,
            "verification": None,
            "retrieval": None,
            "specialists": [],
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
                    "node": "intake",
                    "route": "Recall Registry MCP",
                    "actor": "system",
                    "tool": "get_recall",
                    "server": "Recall Registry MCP",
                    "classification": "read",
                    "status": "snapshot returned",
                    "case_version": 0,
                    "source": SNAPSHOT,
                }
            ],
            "closure": None,
            "evaluation_report": None,
        }

    async def run_investigation(self, case: Mapping[str, Any]) -> dict[str, Any]:
        current = self._bound_copy(case)
        if current.get("matches"):
            return current
        recall = load_recall_snapshot(str(current["recall_number"]))
        intelligence = investigate_recall(recall)
        traceability = TraceabilityService()
        dataset = load_demo_dataset()
        products = {item["product_id"]: item for item in dataset["products"]}
        matched = {item["lot_id"]: item for item in traceability.match_lots(intelligence.predicate)}
        anchor_ids = (
            "LOT-EXACT-170",
            "LOT-PROBABLE-160",
            "LOT-AMBIG-175",
            "LOT-REJECT-190",
        )
        rationale = {
            "exact": "UPC, plant code and Julian date match the official predicate.",
            "probable": "Near UPC plus exact plant and Julian date require bounded review.",
            "ambiguous": "The plant code contains an unresolved character; no auto-hold is allowed.",
            "rejected": "The Julian date is outside the official recall window.",
        }
        all_events: list[dict[str, Any]] = []
        evidence: list[dict[str, Any]] = []
        facilities_by_lot: dict[str, list[str]] = {}
        rows: list[dict[str, Any]] = []
        selected_lots = [matched[lot_id] for lot_id in anchor_ids]
        for lot in selected_lots:
            events = traceability.trace_forward(lot["lot_id"])
            all_events.extend({**event, "unit": "units", "source": SYNTHETIC} for event in events)
            facilities = sorted(
                {
                    facility
                    for event in events
                    for facility in (event.get("from_facility"), event.get("to_facility"))
                    if facility and facility != "SUPPLIER"
                }
            )
            facilities_by_lot[lot["lot_id"]] = facilities
            product = products[lot["product_id"]]
            rows.append(
                {
                    "product": product["name"],
                    "product_id": product["product_id"],
                    "upc": product.get("upc"),
                    "lot_id": lot["lot_id"],
                    "plant_code": lot["plant_code"],
                    "julian_date": lot["julian_date"],
                    "classification": lot["classification"],
                    "rationale": rationale[lot["classification"]],
                    "evidence_ids": [lot["lot_id"], *(event["event_id"] for event in events)],
                    "facility_ids": facilities,
                    "source": SYNTHETIC,
                }
            )
            evidence.append(
                {
                    "citation_id": lot["lot_id"],
                    "scope": lot["lot_id"],
                    "source": SYNTHETIC,
                    "observation": "Synthetic digital-twin lot and EPCIS-style event lineage; it does not prove Northstar was involved in the public recall.",
                }
            )

        affected = [lot for lot in selected_lots if lot["classification"] != "rejected"]
        reconciliation_rows: list[dict[str, Any]] = []
        gaps: list[dict[str, str]] = []
        totals = {
            key: sum(lot[key] for lot in affected)
            for key in (
                "received_units",
                "on_hand",
                "quarantined",
                "sold",
                "returned",
                "disposed",
                "unaccounted",
            )
        }
        totals["received"] = totals.pop("received_units")
        for lot in affected:
            product = products[lot["product_id"]]
            reconciliation_rows.append(
                {
                    "product": product["name"],
                    "lot_id": lot["lot_id"],
                    "facility": "All traced facilities",
                    "received": lot["received_units"],
                    "on_hand": lot["on_hand"],
                    "quarantined": lot["quarantined"],
                    "sold": lot["sold"],
                    "returned": lot["returned"],
                    "disposed": lot["disposed"],
                    "unaccounted": lot["unaccounted"],
                    "evidence_ids": [
                        lot["lot_id"],
                        *[event["event_id"] for event in traceability.trace_forward(lot["lot_id"])],
                    ],
                    "source": SYNTHETIC,
                }
            )
            if lot["unaccounted"] > 0:
                gaps.append(
                    {
                        "gap_type": "quantity discrepancy",
                        "impact": f"{lot['unaccounted']} units remain unaccounted for {lot['lot_id']}",
                        "evidence_id": lot["lot_id"],
                        "closure_implication": "Blocks closure",
                    }
                )
            if lot["classification"] == "ambiguous":
                gaps.append(
                    {
                        "gap_type": "ambiguous lot",
                        "impact": f"{lot['lot_id']} cannot be auto-selected for containment",
                        "evidence_id": lot["lot_id"],
                        "closure_implication": "Human review required; blocks closure",
                    }
                )

        ack_by_facility = {
            item["facility_id"]: item["acknowledged"]
            for item in dataset["facility_acknowledgements"]
        }
        affected_facilities = sorted(
            {
                facility
                for lot in affected
                for facility in facilities_by_lot[lot["lot_id"]]
                if facility in ack_by_facility
            }
        )
        facility_rows = [
            {
                "facility_id": facility,
                "acknowledged": ack_by_facility[facility],
                "source": SYNTHETIC,
            }
            for facility in affected_facilities
        ]
        unacknowledged = [item["facility_id"] for item in facility_rows if not item["acknowledged"]]
        for facility in unacknowledged:
            gaps.append(
                {
                    "gap_type": "missing acknowledgement",
                    "impact": f"{facility} has not acknowledged the simulated task",
                    "evidence_id": facility,
                    "closure_implication": "Blocks closure",
                }
            )

        action = self._action(
            current,
            action_type="create_case",
            summary="Create the simulated operations case before any containment write.",
            target_ids=[str(current["case_id"])],
        )
        plan = plan_investigation(
            case_id=str(current["case_id"]),
            question="Investigate official recall scope and prepare bounded simulated containment.",
        )
        specialists = [
            {
                "specialist": "Regulatory Intake",
                "purpose": plan.todos[0].task,
                "status": "complete",
                "summary": "Official product, UPC, plant, Julian-date, geography and hazard predicate extracted.",
                "citations": [f"FDA-{current['recall_number']}"],
                "sources": ["OFFICIAL — openFDA snapshot"],
            },
            {
                "specialist": "Product & Lot Matching",
                "purpose": plan.todos[1].task,
                "status": "complete",
                "summary": "Exact, probable, ambiguous and rejected anchors classified with field rationale.",
                "citations": anchor_ids,
                "sources": ["OFFICIAL — openFDA snapshot", "SYNTHETIC — ACADEMIC DEMO"],
            },
            {
                "specialist": "Traceability",
                "purpose": plan.todos[2].task,
                "status": "complete",
                "summary": f"{len(all_events)} chronological synthetic lineage events inspected.",
                "citations": [event["event_id"] for event in all_events[:4]],
                "sources": ["SYNTHETIC — ACADEMIC DEMO"],
            },
            {
                "specialist": "Containment",
                "purpose": plan.todos[3].task,
                "status": "complete",
                "summary": "One version-bound action drafted; ambiguous scope excluded from auto-hold.",
                "citations": [action["action_id"]],
                "sources": ["SYNTHETIC — ACADEMIC DEMO"],
            },
            {
                "specialist": "Independent Verification/Critic",
                "purpose": "Check citations, contradictions, safety gates and closure posture.",
                "status": "complete",
                "summary": "Review required because reconciliation, ambiguity and acknowledgement blockers remain.",
                "citations": [item["evidence_id"] for item in gaps],
                "sources": ["OFFICIAL — openFDA snapshot", "SYNTHETIC — ACADEMIC DEMO"],
            },
        ]
        current.update(
            {
                "status": "review_required",
                "current_node": "action_review",
                "matches": rows,
                "lineage": all_events,
                "evidence": evidence,
                "facilities": facility_rows,
                "specialists": specialists,
                "plan": plan.model_dump(mode="json"),
                "retrieval": {
                    "mode": "agentic_rag",
                    "label": "Deterministic offline agentic RAG trace",
                    "queries": [
                        {
                            "hop": 1,
                            "query": f"{current['recall_number']} product UPC plant Julian date hazard",
                            "sparse_hits": 8,
                            "dense_hits": 8,
                            "fused_hits": 8,
                            "reranked_hits": 4,
                            "critic": "needs operational evidence",
                        },
                        {
                            "hop": 2,
                            "query": "Northstar lot lineage reconciliation closure evidence",
                            "sparse_hits": 8,
                            "dense_hits": 8,
                            "fused_hits": 8,
                            "reranked_hits": 4,
                            "critic": "sufficient",
                        },
                    ],
                    "citations": [
                        f"FDA-{current['recall_number']}",
                        "policy-recall-closure",
                        "LOT-EXACT-170",
                        "LOT-PROBABLE-160",
                        "LOT-AMBIG-175",
                    ],
                    "bounds": {"max_hops": 2, "max_queries": 4, "max_reads": 8},
                },
                "reconciliation": {
                    "unit": "units",
                    "totals": totals,
                    "rows": reconciliation_rows,
                    "gaps": gaps,
                },
                "verification": {
                    "outcome": "review_required",
                    "confidence": 0.87,
                    "contradictions": ["Ambiguous plant code P-1950?"],
                    "closure_blockers": [item["impact"] for item in gaps],
                },
                "proposed_actions": [action],
                "pending_interrupt": self._interrupt(current, action, "action_review"),
                "approval": None,
                "closure": None,
                "evaluation_report": self._evaluation_report(),
            }
        )
        current["node_trace"] = self._investigation_node_trace(current)
        current["tool_trace"] = self._investigation_tool_trace(current)
        return current

    async def resume_review(
        self,
        case: Mapping[str, Any],
        *,
        decision: str,
        actor: str,
        justification: str,
        edited_action: str,
    ) -> dict[str, Any]:
        current = self._bound_copy(case)
        interrupt = self._require_interrupt(current, "action_review")
        if decision not in {"approve", "edit", "reject", "escalate"}:
            raise ValueError("Decision must be approve, edit, reject, or escalate.")
        if not actor.strip() or not justification.strip():
            raise ValueError("Actor and justification are required.")
        action = self._current_action(current)
        decision_record = {
            "decision": decision,
            "actor": actor.strip(),
            "justification": justification.strip(),
            "expected_version": current["case_version"],
            "action": action["action_type"],
            "action_digest": action["digest"],
            "timestamp": FIXED_TIME,
        }
        current.setdefault("review_history", []).append(decision_record)
        current["human_decision"] = decision_record
        current["approval"] = None
        if decision == "approve":
            approval = {
                **decision_record,
                "case_id": current["case_id"],
                "thread_id": current["thread_id"],
                "idempotency_key": self._idempotency_key(current, action),
            }
            current["approval"] = approval
            current["status"] = "approved_pending_execution"
            current["current_node"] = "execution_confirmation"
            current["pending_interrupt"] = {
                **interrupt,
                "kind": "execution_confirmation",
                "idempotency_key": approval["idempotency_key"],
            }
        elif decision == "edit":
            if not edited_action.strip():
                raise ValueError("Edit requires revised proposed-action text.")
            action["summary"] = edited_action.strip()
            action["digest"] = self._digest(action)
            current["proposed_actions"] = [action]
            current["status"] = "review_required"
            current["current_node"] = "action_review"
            current["pending_interrupt"] = self._interrupt(current, action, "action_review")
        else:
            current["status"] = "escalated" if decision == "escalate" else "open"
            current["current_node"] = decision
            current["pending_interrupt"] = None
        current["node_trace"].append(
            {
                "order": len(current["node_trace"]) + len(current["tool_trace"]) + 1,
                "node": current["current_node"],
                "route": f"action_review → {current['current_node']}",
                "actor": actor.strip(),
                "classification": "human decision; no write",
                "status": decision,
                "case_version": current["case_version"],
            }
        )
        return current

    async def simulate_approved_actions(self, case: Mapping[str, Any]) -> dict[str, Any]:
        current = self._bound_copy(case)
        self._require_interrupt(current, "execution_confirmation")
        approval = current.get("approval")
        if not isinstance(approval, Mapping) or approval.get("decision") != "approve":
            raise ValueError("A matching approval is required before simulation.")
        action = self._current_action(current)
        expected = (
            approval.get("case_id") == current["case_id"],
            approval.get("thread_id") == current["thread_id"],
            approval.get("expected_version") == current["case_version"],
            approval.get("action_digest") == action["digest"],
            bool(approval.get("idempotency_key")),
        )
        if not all(expected):
            raise ValueError("Approval binding is stale or does not match the current action.")
        receipt = {
            "receipt_id": f"RECEIPT-{current['case_version'] + 1:02d}",
            "action": action["action_type"],
            "case_id": current["case_id"],
            "case_version": current["case_version"] + 1,
            "idempotency_result": "new",
            "idempotency_key": approval["idempotency_key"],
            "actor": approval["actor"],
            "justification": approval["justification"],
            "timestamp": FIXED_TIME,
            "source": SYNTHETIC,
        }
        current.setdefault("receipts", []).append(receipt)
        current["case_version"] += 1
        current["approval"] = None
        if action["action_type"] == "create_case":
            next_action = self._action(
                current,
                action_type="apply_inventory_hold",
                summary="Apply a simulated inventory hold to exact and probable lots only.",
                target_ids=["LOT-EXACT-170", "LOT-PROBABLE-160"],
            )
            current["proposed_actions"] = [next_action]
            current["pending_interrupt"] = self._interrupt(current, next_action, "action_review")
            current["status"] = "review_required"
            current["current_node"] = "action_review"
        else:
            current["proposed_actions"] = []
            current["pending_interrupt"] = None
            current["status"] = "open_monitoring"
            current["current_node"] = "monitor"
        current["tool_trace"].append(
            {
                "order": len(current["node_trace"]) + len(current["tool_trace"]) + 1,
                "node": "execute_one_operation",
                "route": "approved graph node → Operations MCP",
                "actor": receipt["actor"],
                "tool": action["action_type"],
                "server": "Operations MCP",
                "classification": "simulated-write",
                "status": "receipt returned",
                "case_version": current["case_version"],
                "receipt_id": receipt["receipt_id"],
                "source": SYNTHETIC,
            }
        )
        return current

    async def request_closure(self, case: Mapping[str, Any]) -> dict[str, Any]:
        current = self._bound_copy(case)
        reconciliation = current.get("reconciliation")
        if not isinstance(reconciliation, Mapping):
            raise ValueError("Run investigation before requesting closure.")
        gaps = reconciliation.get("gaps") if isinstance(reconciliation.get("gaps"), list) else []
        unacknowledged = [
            item["facility_id"]
            for item in current.get("facilities", [])
            if isinstance(item, Mapping) and item.get("acknowledged") is False
        ]
        ambiguous = [
            item["lot_id"]
            for item in current.get("matches", [])
            if isinstance(item, Mapping) and item.get("classification") == "ambiguous"
        ]
        gates = [
            {
                "gate": "Reconciliation",
                "state": "block" if gaps else "pass",
                "detail": f"{len(gaps)} returned evidence gap(s)",
            },
            {
                "gate": "Facility acknowledgements",
                "state": "block" if unacknowledged else "pass",
                "detail": ", ".join(unacknowledged) or "Returned facility coverage acknowledged",
            },
            {
                "gate": "Ambiguous matches",
                "state": "block" if ambiguous else "pass",
                "detail": ", ".join(ambiguous) or "No ambiguous matches in returned scope",
            },
            {
                "gate": "Approval and version",
                "state": "block" if current.get("pending_interrupt") else "pass",
                "detail": "Pending version-bound action"
                if current.get("pending_interrupt")
                else "No pending write",
            },
            {
                "gate": "Contradictions and evidence",
                "state": "block" if _contradictions(current) else "pass",
                "detail": ", ".join(_contradictions(current)) or "No returned contradictions",
            },
        ]
        blockers = [item["detail"] for item in gates if item["state"] == "block"]
        current["closure"] = {
            "status": "Open — closure blocked" if blockers else "closure_review_required",
            "gates": gates,
            "blockers": blockers,
        }
        current["status"] = "open_closure_blocked" if blockers else "closure_review_required"
        current["current_node"] = "closure_gate"
        return current

    async def inject_failure(self, case: Mapping[str, Any], scenario: str) -> dict[str, Any]:
        current = self._bound_copy(case)
        if scenario not in self.available_failure_scenarios:
            raise ValueError("Not available in this runtime")
        outcomes = {
            FAILURE_SCENARIOS[0]: "frozen snapshot labelled; public source boundary preserved",
            FAILURE_SCENARIOS[1]: "bounded retry then circuit-open snapshot fallback",
            FAILURE_SCENARIOS[2]: "pause required; zero automatic hold writes",
            FAILURE_SCENARIOS[3]: "evidence gap retained; closure blocked",
            FAILURE_SCENARIOS[4]: "case remains open; follow-up remains approval-gated",
            FAILURE_SCENARIOS[5]: "same-key recovery required",
            FAILURE_SCENARIOS[6]: "refresh and review required; checkpoint unchanged",
            FAILURE_SCENARIOS[7]: "watchdog escalated repeated progress",
            FAILURE_SCENARIOS[8]: "deterministic specialist fallback",
        }
        current["failure_result"] = {
            "scenario": scenario,
            "status": "injected_fixture",
            "safe_outcome": outcomes[scenario],
            "mode": "Deterministic failure fixture — no production-like outage was changed",
        }
        return current

    @staticmethod
    def _bound_copy(case: Mapping[str, Any]) -> dict[str, Any]:
        current = copy.deepcopy(dict(case))
        if not current.get("case_id") or not current.get("thread_id"):
            raise ValueError("Current case and durable thread identifiers are required.")
        if isinstance(current.get("case_version"), bool) or not isinstance(
            current.get("case_version"), int
        ):
            raise ValueError("Current case version is required.")
        return current

    @staticmethod
    def _digest(action: Mapping[str, Any]) -> str:
        payload = {key: value for key, value in action.items() if key != "digest"}
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()

    def _action(
        self,
        case: Mapping[str, Any],
        *,
        action_type: str,
        summary: str,
        target_ids: list[str],
    ) -> dict[str, Any]:
        action = {
            "action_id": f"ACTION-{action_type.upper().replace('_', '-')}-V{case['case_version']}",
            "action_type": action_type,
            "summary": summary,
            "target_ids": target_ids,
            "expected_version": case["case_version"],
            "source": SYNTHETIC,
        }
        action["digest"] = self._digest(action)
        return action

    @staticmethod
    def _interrupt(case: Mapping[str, Any], action: Mapping[str, Any], kind: str) -> dict[str, Any]:
        return {
            "kind": kind,
            "scope": action["action_type"],
            "case_id": case["case_id"],
            "thread_id": case["thread_id"],
            "expected_version": case["case_version"],
            "action_digest": action["digest"],
            "remaining_action_types": ["apply_inventory_hold"]
            if action["action_type"] == "create_case"
            else [],
        }

    @staticmethod
    def _current_action(case: Mapping[str, Any]) -> dict[str, Any]:
        actions = case.get("proposed_actions")
        if (
            not isinstance(actions, list)
            or len(actions) != 1
            or not isinstance(actions[0], Mapping)
        ):
            raise ValueError("Exactly one proposed action is required for this case version.")
        return copy.deepcopy(dict(actions[0]))

    def _require_interrupt(self, case: Mapping[str, Any], kind: str) -> dict[str, Any]:
        interrupt = case.get("pending_interrupt")
        if not isinstance(interrupt, Mapping) or interrupt.get("kind") != kind:
            raise ValueError(f"No {kind} interrupt is pending.")
        action = self._current_action(case)
        expected = (
            interrupt.get("case_id") == case["case_id"],
            interrupt.get("thread_id") == case["thread_id"],
            interrupt.get("expected_version") == case["case_version"],
            interrupt.get("action_digest") == action["digest"],
        )
        if not all(expected):
            raise ValueError("Interrupt binding is stale; refresh and review the current version.")
        return copy.deepcopy(dict(interrupt))

    @staticmethod
    def _idempotency_key(case: Mapping[str, Any], action: Mapping[str, Any]) -> str:
        return f"demo:{case['case_id']}:v{case['case_version']}:{action['action_type']}"

    @staticmethod
    def _investigation_node_trace(case: Mapping[str, Any]) -> list[dict[str, Any]]:
        nodes = (
            "intake",
            "retrieve_context",
            "plan",
            "regulatory_intake",
            "product_lot_match",
            "trace_forward_backward",
            "reconcile",
            "containment_draft",
            "verify",
            "prepare_action_review",
            "action_review",
        )
        return [
            {
                "order": index,
                "node": node,
                "route": f"{nodes[index - 2]} → {node}" if index > 1 else "START → intake",
                "actor": "system",
                "classification": "read/reasoning"
                if node != "action_review"
                else "interrupt; no write",
                "status": "pending human input" if node == "action_review" else "complete",
                "case_version": case["case_version"],
            }
            for index, node in enumerate(nodes, start=1)
        ]

    @staticmethod
    def _investigation_tool_trace(case: Mapping[str, Any]) -> list[dict[str, Any]]:
        tools = (
            (
                12,
                "retrieve_context",
                "recall_registry_hybrid_search",
                "Recall Registry MCP",
                "OFFICIAL_OPENFDA_SNAPSHOT",
            ),
            (13, "retrieve_context", "traceability_hybrid_search", "Traceability MCP", SYNTHETIC),
            (14, "product_lot_match", "match_lots", "Traceability MCP", SYNTHETIC),
            (15, "trace_forward_backward", "trace_forward", "Traceability MCP", SYNTHETIC),
            (16, "reconcile", "reconcile_units", "Traceability MCP", SYNTHETIC),
        )
        return [
            {
                "order": order,
                "node": node,
                "route": f"{server} read",
                "actor": "system",
                "tool": tool,
                "server": server,
                "classification": "read",
                "status": "complete",
                "case_version": case["case_version"],
                "source": source,
            }
            for order, node, tool, server, source in tools
        ]

    @staticmethod
    def _evaluation_report() -> dict[str, Any]:
        scenarios = (
            ("predicate", "official predicate remains source-cited"),
            ("exact match", "exact lot retains field rationale"),
            ("ambiguous escalation", "ambiguous lot pauses; no auto-hold"),
            ("trace", "forward/backward synthetic lineage remains labelled"),
            ("reconciliation", "quantity equation remains explicit"),
            ("missing event", "gap remains a closure blocker"),
            ("approval guard", "zero writes before matching approval"),
            ("idempotency", "lost response reuses the same key"),
            ("closure guard", "flagship closure is blocked"),
            ("successful closure", "all-pass path requires final human review"),
            ("cached fallback", "snapshot is never presented as live"),
            ("loop control", "bounded retrieval/watchdog can escalate"),
        )
        return {
            "mode": "deterministic fixture",
            "scenarios": [
                {
                    "scenario": name,
                    "expected": expected,
                    "safety_critical": True,
                    "passed": True,
                    "observed": "deterministic contract fixture",
                    "exception": None,
                }
                for name, expected in scenarios
            ],
        }


def _contradictions(case: Mapping[str, Any]) -> list[str]:
    verification = case.get("verification")
    if not isinstance(verification, Mapping):
        return []
    values = verification.get("contradictions")
    return [str(value) for value in values] if isinstance(values, list) else []
