"""JSON-serializable contracts shared by RecallOps services and adapters."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

PUBLIC_PROVENANCE = "OFFICIAL_OPENFDA_SNAPSHOT"
SYNTHETIC_ORIGIN = "SYNTHETIC_RETAILER_DIGITAL_TWIN"
Provenance = Literal["OFFICIAL_OPENFDA_SNAPSHOT", "LIVE_OPENFDA"]
ActionDecision = Literal["approve", "edit", "reject", "escalate"]


def _compact(value: str) -> str:
    return "".join(character for character in value.upper() if character.isalnum())


class RecallRecord(BaseModel):
    recall_number: str
    source: str
    provenance: Provenance
    retrieved_at: datetime
    payload: dict[str, Any]
    sha256: str | None = None
    source_url: str | None = None


class RecallPredicate(BaseModel):
    product_terms: list[str] = Field(min_length=1)
    upcs: list[str] = Field(default_factory=list)
    plant_codes: list[str] = Field(default_factory=list)
    julian_start: int = Field(ge=1, le=366)
    julian_end: int = Field(ge=1, le=366)
    geography: list[str] = Field(default_factory=list)
    hazard: str = Field(min_length=1)

    @field_validator("upcs")
    @classmethod
    def normalize_upcs(cls, value: list[str]) -> list[str]:
        return [_compact(item) for item in value]

    @field_validator("plant_codes")
    @classmethod
    def normalize_plants(cls, value: list[str]) -> list[str]:
        return [item.strip().upper() for item in value]

    @model_validator(mode="after")
    def valid_julian_range(self) -> RecallPredicate:
        if self.julian_start > self.julian_end:
            raise ValueError("julian_start must not exceed julian_end")
        return self


class Product(BaseModel):
    product_id: str
    name: str
    upc: str | None = None
    brand: str | None = None
    package: str | None = None
    origin: Literal["SYNTHETIC_RETAILER_DIGITAL_TWIN"]

    @field_validator("upc")
    @classmethod
    def normalize_upc(cls, value: str | None) -> str | None:
        return _compact(value) if value else value


class Lot(BaseModel):
    lot_id: str
    product_id: str
    plant_code: str
    julian_date: int = Field(ge=1, le=366)
    received_units: int = Field(ge=0)
    origin: Literal["SYNTHETIC_RETAILER_DIGITAL_TWIN"]
    supplier_lot: str | None = None

    @field_validator("plant_code")
    @classmethod
    def normalize_plant(cls, value: str) -> str:
        return value.strip().upper()


class TraceEvent(BaseModel):
    event_id: str
    lot_id: str
    event_type: Literal[
        "receiving", "shipping", "transfer", "sale", "return", "quarantine", "disposal"
    ]
    quantity: int = Field(ge=0)
    from_facility: str | None = None
    to_facility: str | None = None
    occurred_at: datetime
    origin: Literal["SYNTHETIC_RETAILER_DIGITAL_TWIN"]
    parent_event_id: str | None = None


class Reconciliation(BaseModel):
    lot_id: str
    received: int = Field(ge=0)
    on_hand: int = Field(ge=0)
    quarantined: int = Field(ge=0)
    sold: int = Field(ge=0)
    returned: int = Field(ge=0)
    disposed: int = Field(ge=0)
    unaccounted: int
    evidence_ids: list[str] = Field(default_factory=list)
    component_evidence: dict[str, list[str]] = Field(default_factory=dict)

    @classmethod
    def from_quantities(
        cls,
        lot_id: str,
        *,
        received: int,
        on_hand: int = 0,
        quarantined: int = 0,
        sold: int = 0,
        returned: int = 0,
        disposed: int = 0,
    ) -> Reconciliation:
        unaccounted = received - on_hand - quarantined - sold - returned - disposed
        return cls(
            lot_id=lot_id,
            received=received,
            on_hand=on_hand,
            quarantined=quarantined,
            sold=sold,
            returned=returned,
            disposed=disposed,
            unaccounted=unaccounted,
        )


class ProposedAction(BaseModel):
    action_id: str
    action_type: Literal[
        "create_case",
        "apply_inventory_hold",
        "create_facility_tasks",
        "record_acknowledgment",
        "record_disposition",
        "close_case",
    ]
    case_id: str
    target_ids: list[str] = Field(default_factory=list)
    rationale: str = Field(min_length=1)
    evidence_ids: list[str] = Field(default_factory=list)
    expected_case_version: int = Field(ge=0)


class ApprovalDecision(BaseModel):
    decision: ActionDecision
    actor: str = Field(min_length=1)
    justification: str = Field(min_length=1)
    approved_at: datetime
    action_ids: list[str] = Field(default_factory=list)


class AuditReceipt(BaseModel):
    receipt_id: str
    case_id: str
    action_type: str
    actor: str
    justification: str
    idempotency_key: str
    case_version: int = Field(ge=0)
    status: Literal["simulated", "rejected"]
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    details: dict[str, Any] = Field(default_factory=dict)


class RecallCaseState(BaseModel):
    case_id: str
    thread_id: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    status: str = "open"
    case_version: int = Field(default=0, ge=0)
    recall_number: str
    question: str = ""
    source_mode: str = "snapshot"
    recall: RecallRecord | None = None
    recall_predicate: RecallPredicate | None = None
    candidate_products: list[dict[str, Any]] = Field(default_factory=list)
    candidate_lots: list[dict[str, Any]] = Field(default_factory=list)
    trace_events: list[TraceEvent] = Field(default_factory=list)
    reconciliation: list[Reconciliation] = Field(default_factory=list)
    proposed_actions: list[ProposedAction] = Field(default_factory=list)
    human_decision: ApprovalDecision | None = None
    write_receipts: list[AuditReceipt] = Field(default_factory=list)
    acknowledgements: dict[str, bool] = Field(default_factory=dict)
    tool_trace: list[dict[str, Any]] = Field(default_factory=list)
    node_trace: list[dict[str, Any]] = Field(default_factory=list)
    evidence_gaps: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    latency_ms: int = Field(default=0, ge=0)
    estimated_tokens: int = Field(default=0, ge=0)
    tool_call_count: int = Field(default=0, ge=0)
    model_mode: str = "deterministic"
