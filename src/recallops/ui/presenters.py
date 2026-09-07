"""Pure, deterministic presenters for the Streamlit and CLI surfaces.

This module intentionally knows nothing about Streamlit, MCP, environment
variables, or durable workflow mutation.  It accepts a normalized runtime
snapshot and returns safe, semantic view models.
"""

from __future__ import annotations

import copy
import html
import re
from collections.abc import Mapping, MutableMapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from recallops.ui.evaluation_reports import EvaluationProjection


def build_retrieval_chart_spec(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Compare distinct unit-scale metrics side by side, in artifact order."""
    return {
        "mark": "bar",
        "transform": [{"fold": ["Recall@5", "nDCG@5"], "as": ["Metric", "Score"]}],
        "encoding": {
            "x": {
                "field": "Configuration",
                "type": "nominal",
                "sort": [row["Configuration"] for row in rows],
            },
            "xOffset": {"field": "Metric", "type": "nominal", "sort": ["Recall@5", "nDCG@5"]},
            "y": {
                "field": "Score",
                "type": "quantitative",
                "stack": None,
                "scale": {"domain": [0, 1]},
            },
            "color": {
                "field": "Metric",
                "type": "nominal",
                "scale": {"domain": ["Recall@5", "nDCG@5"]},
            },
            "tooltip": [
                {"field": "Configuration"},
                {"field": "Metric"},
                {"field": "Score", "type": "quantitative"},
            ],
        },
    }


def build_live_summary_rows(projection: EvaluationProjection) -> list[dict[str, str]]:
    if (
        projection.verification_status != "verified"
        or projection.optional_live_summary.status != "completed"
    ):
        return []
    live = projection.optional_live_summary
    values = (
        ("Provider", live.provider),
        ("Model SHA-256", live.model_sha256),
        ("Prompt SHA-256", live.prompt_sha256),
        ("Repetitions", live.repetitions),
        ("Executed cases", live.executed_case_count),
        ("Task success rate", live.task_success_rate),
        ("Evidence fact coverage", live.evidence_fact_coverage),
        ("Budget compliance", live.budget_compliance),
        ("Total tool calls", live.total_tool_calls),
        ("Duplicate tool call ratio", live.duplicate_tool_call_ratio),
        ("Prohibited tool calls", live.prohibited_tool_call_count),
        ("Duration (ms)", live.duration_ms),
        ("Tokens available", live.tokens_available),
        ("Tokens", live.tokens if live.tokens_available else None),
        ("Cost available", live.cost_available),
        ("Estimated cost", live.estimated_cost if live.cost_available else None),
    )
    return [
        {
            "Live metric": label,
            "Value": "Unavailable" if value is None else mask_display_value(value),
        }
        for label, value in values
    ]


def build_retrieval_ablation_rows(
    projection: EvaluationProjection,
    family: str | None = None,
) -> list[dict[str, Any]]:
    """Keep report numeric values verbatim, including counts and zero-valued measurements."""
    if projection.verification_status != "verified":
        return []
    labels = {"case_count": "Cases", "recall_at_5": "Recall@5", "ndcg_at_5": "nDCG@5"}
    rows = []
    for config in projection.retrieval.configurations:
        metrics = config["family_metrics"].get(family) if family else config["metrics"]
        if metrics is None:
            continue
        rows.append(
            {
                "Configuration": mask_display_value(config["name"]),
                **{
                    labels.get(name, mask_display_value(name)): value
                    for name, value in metrics.items()
                    if type(value) in (int, float)
                },
            }
        )
    return rows


def build_critic_rows(
    projection: EvaluationProjection, family: str | None = None
) -> list[dict[str, Any]]:
    if projection.verification_status != "verified":
        return []
    return [
        {
            "Configuration": mask_display_value(config["name"]),
            "Critic stop": mask_display_value(reason),
            "Cases": count,
        }
        for config in projection.retrieval.configurations
        for reason, count in (
            config["family_critic_stops"].get(family, {}) if family else config["critic_stops"]
        ).items()
    ]


def build_retrieval_delta_rows(projection: EvaluationProjection) -> list[dict[str, Any]]:
    if projection.verification_status != "verified":
        return []
    return [
        {"Comparison": label, "Delta": projection.retrieval.gates[name]}
        for name, label in (
            ("fusion_recall_delta", "RRF fusion − best sparse/dense · Recall@5"),
            ("rerank_ndcg_delta", "RRF + rerank − RRF fusion · nDCG@5"),
        )
        if name in projection.retrieval.gates
    ]


def build_orchestration_delta_rows(projection: EvaluationProjection) -> list[dict[str, Any]]:
    if projection.verification_status != "verified":
        return []
    return [
        {"Metric": mask_display_value(name), "Delta": value}
        for name, value in projection.orchestration.deltas.items()
    ]


PINNED_RECALL = "H-1230-2026"
DEFAULT_ACTOR = "Food-safety manager"
APPROVAL_JUSTIFICATION = (
    "Authorize simulated containment for confirmed scope; retain ambiguous lot for review."
)
ESCALATION_JUSTIFICATION = (
    "Do not close while acknowledgement, ambiguity, or reconciliation gaps remain."
)
EQUATION = "received = on_hand + quarantined + sold + returned + disposed + unaccounted"
VIEWS = (
    "Command Center",
    "Investigation",
    "Reconciliation",
    "Human Review",
    "Audit & Evaluation",
)
DECISIONS = ("approve", "edit", "reject", "escalate")

_SENSITIVE_FIELDS = (
    "customer",
    "email",
    "phone",
    "address",
    "account",
    "password",
    "secret",
    "token",
    "api_key",
    "authorization",
)


@dataclass(frozen=True, slots=True)
class SourceBadge:
    label: str
    tone: str


@dataclass(frozen=True, slots=True)
class CasePresentation:
    raw: dict[str, Any]
    recall_number: str
    case_id: str | None
    thread_id: str | None
    case_version: int | None
    status: str
    source_mode: str | None
    model_mode: str
    current_node: str | None


@dataclass(frozen=True, slots=True)
class HeaderPresentation:
    recall_number: str
    case_id: str
    thread_id: str
    case_version: str
    status: str
    source_mode: str
    model_mode: str
    current_node: str


@dataclass(frozen=True, slots=True)
class LabelValueRow:
    label: str
    value: str


@dataclass(frozen=True, slots=True)
class MatchRow:
    product: str
    upc: str
    lot_id: str
    plant_code: str
    julian_date: str
    classification: str
    rationale: str
    evidence_count: int
    affected_facilities: str
    source: str
    review_flag: str


@dataclass(frozen=True, slots=True)
class LineageRow:
    occurred_at: str
    event_type: str
    from_facility: str
    to_facility: str
    lot_id: str
    quantity: str
    event_id: str
    source: str


@dataclass(frozen=True, slots=True)
class EvidenceRow:
    citation_id: str
    source: str
    observation: str
    url: str


@dataclass(frozen=True, slots=True)
class GapRow:
    gap_type: str
    impact: str
    evidence_id: str
    closure_implication: str


@dataclass(frozen=True, slots=True)
class ReconciliationPresentation:
    equation: str
    totals: dict[str, str]
    balanced: bool | None
    rows: list[dict[str, str]]
    gaps: list[GapRow]


@dataclass(frozen=True, slots=True)
class TimelineRow:
    order: int
    state: str
    route: str
    actor: str
    specialist: str
    tool: str
    server: str
    classification: str
    status: str
    duration: str
    warning: str
    case_version: str
    receipt_or_correlation_id: str
    source: str


@dataclass(frozen=True, slots=True)
class ReviewPacketPresentation:
    scope: str
    case_version: int
    predicate: list[LabelValueRow]
    matches: list[MatchRow]
    facilities: list[dict[str, str]]
    reconciliation: ReconciliationPresentation
    verification: dict[str, str]
    proposed_actions: list[dict[str, str]]
    remaining_actions: list[str]
    citations: list[EvidenceRow]
    timeline: list[TimelineRow]


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    field: str
    message: str


@dataclass(frozen=True, slots=True)
class ReceiptRow:
    status: str
    receipt_id: str
    action: str
    case_id: str
    case_version: str
    idempotency_result: str
    actor: str
    justification: str
    timestamp: str
    source: str


@dataclass(frozen=True, slots=True)
class EvaluationRow:
    scenario: str
    expected: str
    safety_critical: str
    result: str
    observed: str
    exception: str


@dataclass(frozen=True, slots=True)
class EvaluationMetricRow:
    metric: str
    value: str
    kind: str
    status: str


@dataclass(frozen=True, slots=True)
class ClosureGateRow:
    gate: str
    state: str
    detail: str
    next_step: str


@dataclass(frozen=True, slots=True)
class RetrievalRow:
    hop: int
    query: str
    sparse: str
    dense: str
    fusion: str
    rerank: str
    critic: str


_UI_DEFAULTS: dict[str, Any] = {
    "ui_active_view": "Command Center",
    "ui_recall_number": PINNED_RECALL,
    "ui_case": None,
    "ui_case_id": None,
    "ui_thread_id": None,
    "ui_case_version": None,
    "ui_source_mode": None,
    "ui_model_mode": "deterministic",
    "ui_investigation_state": "not_started",
    "ui_pending_interrupt": None,
    "ui_review_packet": None,
    "ui_decision": "approve",
    "ui_actor": DEFAULT_ACTOR,
    "ui_justification": APPROVAL_JUSTIFICATION,
    "ui_edited_action": "",
    "ui_human_decision": None,
    "ui_approval": None,
    "ui_write_receipts": [],
    "ui_node_trace": [],
    "ui_tool_trace": [],
    "ui_reconciliation": None,
    "ui_verification": None,
    "ui_evaluation_report": None,
    "ui_failure_scenario": None,
    "ui_last_error": None,
    "ui_loading_action": None,
}


def initialize_ui_state(state: MutableMapping[str, Any]) -> None:
    """Initialize every documented UI key without overwriting a rerun state."""

    for key, value in _UI_DEFAULTS.items():
        state.setdefault(key, copy.deepcopy(value))


def reduce_case_snapshot(raw_case: Mapping[str, Any]) -> CasePresentation:
    """Copy a JSON-like runtime snapshot into the single UI boundary object."""

    raw = copy.deepcopy(dict(raw_case))
    version = raw.get("case_version")
    if isinstance(version, bool) or (version is not None and not isinstance(version, int)):
        version = None
    return CasePresentation(
        raw=raw,
        recall_number=str(raw.get("recall_number") or PINNED_RECALL),
        case_id=_strict_optional_text(raw.get("case_id")),
        thread_id=_strict_optional_text(raw.get("thread_id")),
        case_version=version,
        status=str(raw.get("status") or "not_started"),
        source_mode=_optional_text(raw.get("source_mode")),
        model_mode=str(raw.get("model_mode") or "deterministic"),
        current_node=_optional_text(raw.get("current_node")),
    )


def source_badge(source: Mapping[str, Any] | str | None) -> SourceBadge:
    value: str
    if isinstance(source, Mapping):
        value = " ".join(str(source.get(key, "")) for key in ("provenance", "kind", "source"))
    else:
        value = str(source or "")
    normalized = value.upper().replace("-", "_").replace(" ", "_")
    if "LIVE_OPENFDA" in normalized or normalized in {"LIVE", "OPENFDA_LIVE"}:
        return SourceBadge("OFFICIAL — openFDA", "official")
    if (
        "OFFICIAL_OPENFDA_SNAPSHOT" in normalized
        or "OPENFDA_SNAPSHOT" in normalized
        or normalized == "SNAPSHOT"
    ):
        return SourceBadge("OFFICIAL — openFDA snapshot", "official_snapshot")
    if "SYNTHETIC" in normalized or "DIGITAL_TWIN" in normalized:
        return SourceBadge("SYNTHETIC — ACADEMIC DEMO", "synthetic")
    if "OFFICIAL_GUIDANCE" in normalized or "POLICY" in normalized:
        return SourceBadge("OFFICIAL — guidance/reference", "official_reference")
    return SourceBadge("SOURCE — UNKNOWN", "unknown")


def build_case_header(case: CasePresentation | None) -> HeaderPresentation:
    if case is None:
        return HeaderPresentation(
            PINNED_RECALL, "—", "—", "—", "not_started", "—", "Deterministic offline planner", "—"
        )
    source = source_badge(case.source_mode)
    model = (
        "Deterministic offline planner"
        if case.model_mode.casefold() == "deterministic"
        else mask_display_value(case.model_mode)
    )
    return HeaderPresentation(
        recall_number=mask_display_value(case.recall_number),
        case_id=mask_display_value(case.case_id or "—"),
        thread_id=mask_display_value(case.thread_id or "—"),
        case_version="—" if case.case_version is None else str(case.case_version),
        status=mask_display_value(case.status),
        source_mode=source.label if source.tone != "unknown" else "—",
        model_mode=model,
        current_node=mask_display_value(case.current_node or "—"),
    )


def build_predicate_rows(case: CasePresentation) -> list[LabelValueRow]:
    recall = _mapping(case.raw.get("recall"))
    predicate = _mapping(recall.get("predicate"))
    rows: list[LabelValueRow] = []
    scalar_fields = (
        ("product", "Product"),
        ("upc", "UPC"),
        ("plant", "Plant"),
        ("geography_text", "Geography"),
        ("hazard", "Hazard"),
    )
    for key, label in scalar_fields:
        if predicate.get(key) not in (None, "", []):
            rows.append(LabelValueRow(label, _display(predicate[key])))
    list_fields = (
        ("product_terms", "Product terms"),
        ("upcs", "UPCs"),
        ("plant_codes", "Plant codes"),
        ("geography", "Geography"),
    )
    existing_labels = {row.label for row in rows}
    for key, label in list_fields:
        values = predicate.get(key)
        if label not in existing_labels and isinstance(values, list) and values:
            rows.append(LabelValueRow(label, ", ".join(_display(item) for item in values)))
    start, end = predicate.get("julian_start"), predicate.get("julian_end")
    if start is not None or end is not None:
        rows.append(LabelValueRow("Julian date", f"{_display(start)}–{_display(end)}"))
    return rows


def build_match_rows(case: CasePresentation) -> list[MatchRow]:
    rows: list[MatchRow] = []
    for item in _list_of_mappings(case.raw.get("matches")):
        classification = str(item.get("classification") or "unknown")
        evidence = item.get("evidence_ids") if isinstance(item.get("evidence_ids"), list) else []
        facilities = item.get("facility_ids") if isinstance(item.get("facility_ids"), list) else []
        rows.append(
            MatchRow(
                product=_display(item.get("product") or item.get("product_id")),
                upc=_display(item.get("upc")),
                lot_id=_display(item.get("lot_id")),
                plant_code=_display(item.get("plant_code")),
                julian_date=_display(item.get("julian_date")),
                classification=mask_display_value(classification),
                rationale=_display(item.get("rationale")),
                evidence_count=len(evidence),
                affected_facilities=", ".join(_display(value) for value in facilities) or "—",
                source=source_badge(item.get("source") or item.get("origin")).label,
                review_flag=("Human review required" if classification == "ambiguous" else "—"),
            )
        )
    return rows


def build_lineage_rows(case: CasePresentation, selected_lot: str | None) -> list[LineageRow]:
    events = _list_of_mappings(case.raw.get("lineage"))
    if selected_lot:
        events = [item for item in events if item.get("lot_id") == selected_lot]
    events.sort(key=lambda item: str(item.get("occurred_at") or ""))
    return [
        LineageRow(
            occurred_at=_display(item.get("occurred_at")),
            event_type=_display(item.get("event_type")),
            from_facility=_display(item.get("from_facility")),
            to_facility=_display(item.get("to_facility")),
            lot_id=_display(item.get("lot_id")),
            quantity=f"{_display(item.get('quantity'))} {_display(item.get('unit') or 'units')}",
            event_id=_display(item.get("event_id")),
            source=source_badge(item.get("source") or item.get("origin")).label,
        )
        for item in events
    ]


def build_evidence_rows(case: CasePresentation, scope: str) -> list[EvidenceRow]:
    raw_rows = [
        *_list_of_mappings(_mapping(case.raw.get("recall")).get("citations")),
        *_list_of_mappings(case.raw.get("evidence")),
    ]
    rows: list[EvidenceRow] = []
    for item in raw_rows:
        row_scope = str(item.get("scope") or "")
        if scope and row_scope and row_scope != scope:
            continue
        rows.append(
            EvidenceRow(
                citation_id=_display(item.get("citation_id") or item.get("source_id")),
                source=source_badge(item.get("source") or item.get("provenance")).label,
                observation=_display(item.get("observation") or item.get("error")),
                url=_safe_url(item.get("url")),
            )
        )
    if scope:
        rows.sort(
            key=lambda row: row.citation_id != scope and not row.citation_id.startswith("EVT-")
        )
    return rows


def build_reconciliation_presentation(case: CasePresentation) -> ReconciliationPresentation:
    reconciliation = _mapping(case.raw.get("reconciliation"))
    raw_totals = _mapping(reconciliation.get("totals"))
    unit = str(reconciliation.get("unit") or "units")
    components = (
        "received",
        "on_hand",
        "quarantined",
        "sold",
        "returned",
        "disposed",
        "unaccounted",
    )
    totals = {
        key: "Unknown"
        if raw_totals.get(key) is None
        else f"{_display(raw_totals.get(key))} {mask_display_value(unit)}"
        for key in components
    }
    numeric = [raw_totals.get(key) for key in components]
    balanced: bool | None
    if not numeric or any(
        isinstance(value, bool) or not isinstance(value, (int, float)) for value in numeric
    ):
        balanced = None
    else:
        balanced = numeric[0] == sum(numeric[1:])
    rows: list[dict[str, str]] = []
    for item in _list_of_mappings(reconciliation.get("rows")):
        rows.append(
            {
                "Product": _display(item.get("product")),
                "Lot": _display(item.get("lot_id")),
                "Facility": _display(item.get("facility")),
                "Received": _quantity(item.get("received"), unit),
                "On hand": _quantity(item.get("on_hand"), unit),
                "Quarantined": _quantity(item.get("quarantined"), unit),
                "Sold": _quantity(item.get("sold"), unit),
                "Returned": _quantity(item.get("returned"), unit),
                "Disposed": _quantity(item.get("disposed"), unit),
                "Unaccounted": _quantity(item.get("unaccounted"), unit),
                "Status": _reconciliation_status(item),
                "Evidence": ", ".join(_display(value) for value in item.get("evidence_ids", []))
                or "—",
                "Source": source_badge(item.get("source") or item.get("origin")).label,
            }
        )
    gaps = [
        GapRow(
            gap_type=_display(item.get("gap_type") or item.get("type")),
            impact=_display(item.get("impact")),
            evidence_id=_display(item.get("evidence_id")),
            closure_implication=_display(item.get("closure_implication")),
        )
        for item in _list_of_mappings(reconciliation.get("gaps"))
    ]
    return ReconciliationPresentation(EQUATION, totals, balanced, rows, gaps)


def build_review_packet(case: CasePresentation) -> ReviewPacketPresentation | None:
    interrupt = _mapping(case.raw.get("pending_interrupt"))
    if interrupt.get("kind") not in {"action_review", "closure_review"}:
        return None
    version = interrupt.get("expected_version")
    if isinstance(version, bool) or not isinstance(version, int):
        version = case.case_version if case.case_version is not None else -1
    facilities = [
        {
            "Facility": _display(item.get("facility_id")),
            "Acknowledged": "Yes"
            if item.get("acknowledged") is True
            else "No"
            if item.get("acknowledged") is False
            else "Unknown",
            "Source": source_badge(item.get("source") or item.get("origin")).label,
        }
        for item in _list_of_mappings(case.raw.get("facilities"))
    ]
    actions = [
        {
            "Action ID": _display(item.get("action_id")),
            "Action type": _display(item.get("action_type")),
            "Draft scope": _display(item.get("summary")),
            "Targets": ", ".join(_display(value) for value in item.get("target_ids", [])) or "—",
            "Expected version": _display(item.get("expected_version")),
            "Source": source_badge(item.get("source") or item.get("origin")).label,
        }
        for item in _list_of_mappings(case.raw.get("proposed_actions"))
    ]
    verification = {
        str(key).replace("_", " ").title(): _display(value)
        for key, value in _mapping(case.raw.get("verification")).items()
    }
    return ReviewPacketPresentation(
        scope=_display(interrupt.get("scope") or interrupt.get("action_type")),
        case_version=version,
        predicate=build_predicate_rows(case),
        matches=build_match_rows(case),
        facilities=facilities,
        reconciliation=build_reconciliation_presentation(case),
        verification=verification,
        proposed_actions=actions,
        remaining_actions=[
            mask_display_value(value)
            for value in interrupt.get("remaining_action_types", [])
            if isinstance(value, str) and value.strip()
        ],
        citations=build_evidence_rows(case, ""),
        timeline=build_timeline_rows(case),
    )


def validate_review_submission(
    decision: str,
    actor: str,
    justification: str,
    case: CasePresentation | None,
    edited_action: str = "",
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    if decision not in DECISIONS:
        issues.append(
            ValidationIssue("decision", "Decision must be approve, edit, reject, or escalate.")
        )
    if not actor.strip():
        issues.append(ValidationIssue("actor", "Actor is required."))
    if not justification.strip():
        issues.append(ValidationIssue("justification", "Justification is required."))
    if decision == "edit" and not edited_action.strip():
        issues.append(
            ValidationIssue("edited_action", "Edit requires revised proposed-action text.")
        )
    if case is None or not case.case_id or not case.thread_id or case.case_version is None:
        issues.append(
            ValidationIssue("case", "Refresh and review a current case before submitting.")
        )
        return issues
    interrupt = _mapping(case.raw.get("pending_interrupt"))
    if interrupt.get("kind") not in {"action_review", "closure_review"}:
        issues.append(ValidationIssue("interrupt", "No human-review interrupt is pending."))
    bindings = (
        ("case_id", case.case_id),
        ("thread_id", case.thread_id),
        ("expected_version", case.case_version),
    )
    if any(
        type(interrupt.get(key)) is not type(expected) or interrupt.get(key) != expected
        for key, expected in bindings
    ):
        issues.append(
            ValidationIssue(
                "case_version", "The review is stale; refresh and review the current version."
            )
        )
    return issues


def can_simulate(case: CasePresentation | None) -> tuple[bool, str]:
    if case is None:
        return False, "Open and investigate a case before simulation."
    approval = _mapping(case.raw.get("approval"))
    interrupt = _mapping(case.raw.get("pending_interrupt"))
    actions = _list_of_mappings(case.raw.get("proposed_actions"))
    action = actions[0] if actions else {}
    if not approval or approval.get("decision") != "approve":
        return False, "A matching approval is required."
    interrupt_kind = interrupt.get("kind")
    if interrupt_kind not in {"execution_confirmation", "write_outcome_recovery"}:
        return False, "Execution confirmation or exact-key recovery is not pending."
    bindings = (
        _strict_equal_text(approval.get("case_id"), case.case_id, interrupt.get("case_id")),
        _strict_equal_text(approval.get("thread_id"), case.thread_id, interrupt.get("thread_id")),
        all(
            type(value) is int
            for value in (
                approval.get("expected_version"),
                case.case_version,
                interrupt.get("expected_version"),
            )
        )
        and approval.get("expected_version")
        == case.case_version
        == interrupt.get("expected_version"),
        _strict_equal_text(
            approval.get("action_digest"),
            action.get("digest"),
            interrupt.get("action_digest"),
        ),
        _strict_equal_text(approval.get("execution_id"), interrupt.get("execution_id")),
        _strict_equal_text(approval.get("idempotency_key"), interrupt.get("idempotency_key")),
        _strict_nonblank_text(approval.get("actor")),
        _strict_nonblank_text(approval.get("justification")),
    )
    if not all(bindings):
        return (
            False,
            "Approval does not match the current action, case version, and idempotency key.",
        )
    if interrupt_kind == "write_outcome_recovery":
        return True, "Recover the recorded outcome using the exact original idempotency key."
    return True, "Approval matches the current action and case version."


def build_receipt_rows(case: CasePresentation) -> list[ReceiptRow]:
    rows: list[ReceiptRow] = []
    for item in _list_of_mappings(case.raw.get("receipts")):
        rows.append(
            ReceiptRow(
                status="Simulated action recorded",
                receipt_id=_display(item.get("receipt_id")),
                action=_display(item.get("action") or item.get("action_type")),
                case_id=_display(item.get("case_id")),
                case_version=_display(item.get("case_version")),
                idempotency_result=_display(item.get("idempotency_result")),
                actor=_display(item.get("actor"), "actor"),
                justification=_display(item.get("justification")),
                timestamp=_display(
                    item.get("timestamp") or item.get("recorded_at") or item.get("created_at")
                ),
                source=source_badge(item.get("source") or item.get("origin")).label,
            )
        )
    return rows


def build_timeline_rows(case: CasePresentation) -> list[TimelineRow]:
    items = [
        *_list_of_mappings(case.raw.get("node_trace")),
        *_list_of_mappings(case.raw.get("tool_trace")),
    ]
    items.sort(key=lambda item: int(item.get("order", 0)))
    return [
        TimelineRow(
            order=int(item.get("order", index)),
            state=_display(item.get("node") or item.get("state")),
            route=_display(item.get("route") or item.get("transition")),
            actor=_display(item.get("actor") or "system", "actor"),
            specialist=_display(item.get("specialist")),
            tool=_display(item.get("tool")),
            server=_display(item.get("server")),
            classification=_display(item.get("classification")),
            status=_display(item.get("status") or item.get("outcome")),
            duration=_display(item.get("duration")),
            warning=_display(item.get("warning") or item.get("error")),
            case_version=_display(item.get("case_version")),
            receipt_or_correlation_id=_display(
                item.get("receipt_id") or item.get("correlation_id")
            ),
            source=source_badge(item.get("source") or item.get("provenance")).label,
        )
        for index, item in enumerate(items, start=1)
    ]


def build_evaluation_rows(report: Mapping[str, Any] | None) -> list[EvaluationRow]:
    if not report:
        return []
    return [
        EvaluationRow(
            scenario=_display(item.get("scenario")),
            expected=_display(item.get("expected") or item.get("expected_assertion")),
            safety_critical="Yes" if item.get("safety_critical") is True else "No",
            result="PASS" if item.get("passed") is True else "FAIL",
            observed=_display(item.get("observed") or item.get("outcome")),
            exception=_display(item.get("exception")),
        )
        for item in _list_of_mappings(report.get("scenarios"))
    ]


def build_evaluation_metric_rows(
    report: Mapping[str, Any] | None,
) -> list[EvaluationMetricRow]:
    if not report or report.get("status") != "verified":
        return []
    rows: list[EvaluationMetricRow] = []
    metrics = report.get("metrics")
    if isinstance(metrics, Mapping):
        for name, value in metrics.items():
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            rows.append(
                EvaluationMetricRow(
                    metric=str(name).replace("_", " ").capitalize(),
                    value=f"{float(value):.1%}",
                    kind="rate",
                    status="PASS" if value == 1 else "FAIL",
                )
            )
    counters = report.get("unsafe_counters")
    if isinstance(counters, Mapping):
        for name, value in counters.items():
            if isinstance(value, bool) or not isinstance(value, int):
                continue
            rows.append(
                EvaluationMetricRow(
                    metric=str(name).replace("_", " ").capitalize(),
                    value=str(value),
                    kind="unsafe counter",
                    status="PASS" if value == 0 else "FAIL",
                )
            )
    return rows


def build_closure_gate_rows(case: CasePresentation) -> list[ClosureGateRow]:
    closure = _mapping(case.raw.get("closure"))
    gates = _list_of_mappings(closure.get("gates"))
    all_pass = bool(gates) and all(item.get("state") == "pass" for item in gates)
    return [
        ClosureGateRow(
            gate=_display(item.get("gate")),
            state=_display(item.get("state") or "unknown"),
            detail=_display(item.get("detail")),
            next_step=(
                "Complete the version-bound human closure review; this is not closed."
                if item.get("state") == "review"
                else "Final human closure review is required; this is not closed."
                if all_pass
                else "Resolve the returned blocker before requesting closure again."
            ),
        )
        for item in gates
    ]


def build_retrieval_rows(case: CasePresentation) -> list[RetrievalRow]:
    retrieval = _mapping(case.raw.get("retrieval"))
    return [
        RetrievalRow(
            hop=int(item.get("hop", index)),
            query=_display(item.get("query")),
            sparse=_retrieval_stage("BM25", item.get("sparse_hits")),
            dense=_retrieval_stage("LSA dense", item.get("dense_hits")),
            fusion=_retrieval_stage("RRF", item.get("fused_hits")),
            rerank=_retrieval_stage("Deterministic rerank", item.get("reranked_hits")),
            critic=_display(item.get("critic")),
        )
        for index, item in enumerate(_list_of_mappings(retrieval.get("queries")), start=1)
    ]


def mask_display_value(value: Any, field_name: str = "") -> str:
    """Return escaped display text while masking customer-like and secret fields."""

    normalized_field = field_name.casefold()
    if any(part in normalized_field for part in _SENSITIVE_FIELDS):
        return "[MASKED]"
    if value is None or value == "":
        return "—"
    text = str(value)
    if re.search(r"\bsk-[A-Za-z0-9_-]{8,}\b", text):
        return "[MASKED]"
    if re.search(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b", text):
        return "[MASKED]"
    return html.escape(text, quote=True)


def _display(value: Any, field_name: str = "") -> str:
    if isinstance(value, list):
        return ", ".join(_display(item, field_name) for item in value) or "—"
    if isinstance(value, Mapping):
        return (
            ", ".join(f"{_display(key)}: {_display(item, str(key))}" for key, item in value.items())
            or "—"
        )
    return mask_display_value(value, field_name)


def _quantity(value: Any, unit: str) -> str:
    return "Unknown" if value is None else f"{_display(value)} {_display(unit)}"


def _reconciliation_status(item: Mapping[str, Any]) -> str:
    value = item.get("unaccounted")
    if value is None:
        return "Unknown — evidence gap"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return "Blocked — unaccounted units" if value > 0 else "Balanced"
    return "Unknown — evidence gap"


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _list_of_mappings(value: Any) -> list[dict[str, Any]]:
    return (
        [dict(item) for item in value]
        if isinstance(value, list) and all(isinstance(item, Mapping) for item in value)
        else []
    )


def _optional_text(value: Any) -> str | None:
    return str(value) if value not in (None, "") else None


def _strict_optional_text(value: Any) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _strict_nonblank_text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _strict_equal_text(*values: Any) -> bool:
    return all(_strict_nonblank_text(value) for value in values) and len(set(values)) == 1


def _safe_url(value: Any) -> str:
    text = str(value or "")
    return html.escape(text, quote=True) if text.startswith(("https://", "http://")) else ""


def _retrieval_stage(label: str, count: Any) -> str:
    if isinstance(count, int) and not isinstance(count, bool):
        return f"{label} · {count} hits"
    return f"{label} · executed (count not exposed)"
