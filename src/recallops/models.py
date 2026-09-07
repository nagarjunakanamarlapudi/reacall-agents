"""JSON-serializable contracts shared by RecallOps services and adapters."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    PlainSerializer,
    field_validator,
    model_validator,
)

PUBLIC_PROVENANCE = "OFFICIAL_OPENFDA_SNAPSHOT"
SYNTHETIC_ORIGIN = "SYNTHETIC_RETAILER_DIGITAL_TWIN"
Provenance = Literal["OFFICIAL_OPENFDA_SNAPSHOT", "LIVE_OPENFDA"]
ActionDecision = Literal["approve", "edit", "reject", "escalate"]
MatchClassification = Literal["exact", "probable", "ambiguous", "rejected"]
Disposition = Literal["dispose_unaccounted", "quarantined", "returned"]


def validate_case_version(value: object, field_name: str = "case_version") -> int:
    """Reject coercible values at every safety-critical case-version boundary."""

    if type(value) is not int or value < 0:
        raise ValueError(f"{field_name} must be a strict nonnegative integer")
    return value


def _validate_case_version_field(value: object) -> int:
    return validate_case_version(value)


CaseVersion = Annotated[
    int,
    BeforeValidator(_validate_case_version_field),
    Field(ge=0),
]


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
    cached: bool = True


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


class CandidateProduct(Product):
    score: float = Field(ge=0, le=1)
    classification: Literal["exact", "probable", "rejected"]


class ProductMetadata(BaseModel):
    upc: str
    source: str


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


class LotMatch(Lot):
    classification: MatchClassification
    on_hand: int = Field(ge=0)
    quarantined: int = Field(ge=0)
    sold: int = Field(ge=0)
    returned: int = Field(ge=0)
    disposed: int = Field(ge=0)
    unaccounted: int = Field(ge=0)


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


class InventoryPosition(BaseModel):
    position_id: str
    lot_id: str
    facility_id: str
    on_hand: int = Field(ge=0)
    origin: Literal["SYNTHETIC_RETAILER_DIGITAL_TWIN"]


class Reconciliation(BaseModel):
    lot_id: str
    received: int = Field(ge=0)
    on_hand: int = Field(ge=0)
    quarantined: int = Field(ge=0)
    sold: int = Field(ge=0)
    returned: int = Field(ge=0)
    disposed: int = Field(ge=0)
    unaccounted: int = Field(ge=0)
    evidence_ids: list[str] = Field(default_factory=list)
    component_evidence: dict[str, list[str]] = Field(default_factory=dict)
    verified: bool = False

    @model_validator(mode="after")
    def balanced_and_evidence_backed(self) -> Reconciliation:
        accounted = (
            self.on_hand
            + self.quarantined
            + self.sold
            + self.returned
            + self.disposed
            + self.unaccounted
        )
        if self.received != accounted:
            raise ValueError("reconciliation equation does not balance")
        if self.verified:
            components = {
                "received",
                "on_hand",
                "quarantined",
                "sold",
                "returned",
                "disposed",
                "unaccounted",
            }
            if set(self.component_evidence) != components:
                raise ValueError("verified reconciliation requires exact component evidence")
            quantities = {
                "received": self.received,
                "on_hand": self.on_hand,
                "quarantined": self.quarantined,
                "sold": self.sold,
                "returned": self.returned,
                "disposed": self.disposed,
            }
            for component, quantity in quantities.items():
                if quantity and not self.component_evidence[component]:
                    raise ValueError(f"verified reconciliation lacks {component} evidence")
            if not self.component_evidence["unaccounted"]:
                raise ValueError("verified reconciliation lacks residual evidence")
            component_ids = {
                evidence_id
                for identifiers in self.component_evidence.values()
                for evidence_id in identifiers
            }
            if component_ids != set(self.evidence_ids):
                raise ValueError("evidence_ids must equal the component evidence union")
        return self

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


class FrozenEvidenceMap(Mapping[str, tuple[str, ...]]):
    """Small immutable mapping used in approval-bound action contracts."""

    __slots__ = ("_items",)

    def __init__(self, values: Mapping[str, tuple[str, ...]]) -> None:
        object.__setattr__(self, "_items", tuple(sorted(values.items())))

    def __setattr__(self, name: str, value: object) -> None:
        del name, value
        raise TypeError("FrozenEvidenceMap is immutable")

    def __getitem__(self, key: str) -> tuple[str, ...]:
        for target_id, identifiers in self._items:
            if target_id == key:
                return identifiers
        raise KeyError(key)

    def __iter__(self) -> Iterator[str]:
        return (target_id for target_id, _ in self._items)

    def __len__(self) -> int:
        return len(self._items)

    def __deepcopy__(self, memo: dict[int, Any]) -> FrozenEvidenceMap:
        del memo
        return self


EvidenceByTarget = Annotated[
    Mapping[str, tuple[str, ...]],
    PlainSerializer(
        lambda value: dict(value),
        return_type=dict[str, tuple[str, ...]],
    ),
]


class ProposedAction(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    action_id: str = Field(min_length=1)
    action_type: Literal[
        "create_case",
        "apply_inventory_hold",
        "create_facility_tasks",
        "record_acknowledgment",
        "record_disposition",
        "close_case",
    ]
    case_id: str = Field(min_length=1)
    target_ids: tuple[str, ...] = Field(default_factory=tuple)
    rationale: str = Field(min_length=1)
    evidence_ids: tuple[str, ...] = Field(default_factory=tuple)
    evidence_by_target: EvidenceByTarget
    expected_case_version: CaseVersion

    @field_validator("action_id", "case_id", "rationale")
    @classmethod
    def nonblank_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("value must be nonblank")
        return value

    @field_validator("target_ids", "evidence_ids")
    @classmethod
    def unique_nonblank_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item.strip() for item in value):
            raise ValueError("identifiers must be nonblank")
        if len(value) != len(set(value)):
            raise ValueError("identifiers must be unique")
        return value

    @field_validator("evidence_by_target")
    @classmethod
    def freeze_target_evidence(cls, value: Mapping[str, tuple[str, ...]]) -> FrozenEvidenceMap:
        normalized: dict[str, tuple[str, ...]] = {}
        for target_id, identifiers in value.items():
            if not target_id.strip():
                raise ValueError("target evidence identifiers must be unique nonblank values")
            if any(not evidence_id.strip() for evidence_id in identifiers):
                raise ValueError("target evidence identifiers must be unique nonblank values")
            if len(identifiers) != len(set(identifiers)):
                raise ValueError("target evidence identifiers must be unique nonblank values")
            normalized[target_id] = tuple(sorted(identifiers))
        return FrozenEvidenceMap(normalized)

    @model_validator(mode="after")
    def target_evidence_is_complete(self) -> ProposedAction:
        if set(self.evidence_by_target) != set(self.target_ids):
            raise ValueError("evidence_by_target must cover every target exactly")
        if any(not identifiers for identifiers in self.evidence_by_target.values()):
            raise ValueError("evidence_by_target contains an unsupported target")
        union = {
            evidence_id
            for identifiers in self.evidence_by_target.values()
            for evidence_id in identifiers
        }
        if union != set(self.evidence_ids):
            raise ValueError("evidence_ids must equal the target evidence union")
        return self


def proposed_action_digest(action: ProposedAction) -> str:
    """Return the canonical SHA-256 binding for the complete reviewed action."""

    canonical = json.dumps(
        action.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class ApprovalBinding(BaseModel):
    model_config = ConfigDict(frozen=True)

    action_id: str = Field(min_length=1)
    action_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("action_id")
    @classmethod
    def nonblank_action_id(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("action_id must be nonblank")
        return value


class ApprovalDecision(BaseModel):
    model_config = ConfigDict(frozen=True)

    decision: ActionDecision
    actor: str = Field(min_length=1)
    justification: str = Field(min_length=1)
    approved_at: datetime
    approved_case_version: CaseVersion
    approved_case_id: str = Field(min_length=1)
    action_ids: tuple[str, ...] = Field(min_length=1)
    action_bindings: tuple[ApprovalBinding, ...] = Field(min_length=1)

    @field_validator("actor", "justification", "approved_case_id")
    @classmethod
    def nonblank_approval_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("approval fields must be nonblank")
        return value

    @model_validator(mode="after")
    def exact_binding_set(self) -> ApprovalDecision:
        if len(self.action_ids) != len(set(self.action_ids)):
            raise ValueError("action_ids must be unique")
        binding_ids = [binding.action_id for binding in self.action_bindings]
        if len(binding_ids) != len(set(binding_ids)):
            raise ValueError("action_bindings must have unique action IDs")
        if set(binding_ids) != set(self.action_ids):
            raise ValueError("action_ids must exactly match action_bindings")
        return self


class AuditReceipt(BaseModel):
    receipt_id: str
    case_id: str
    action_type: str
    actor: str
    justification: str
    idempotency_key: str
    case_version: CaseVersion
    status: Literal["simulated", "rejected"]
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    details: dict[str, Any] = Field(default_factory=dict)


class DispositionEvent(BaseModel):
    """Append-only, workflow-approved quantity evidence created by Operations."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_id: str = Field(min_length=1)
    lot_id: str = Field(min_length=1)
    disposition: Disposition
    quantity: int = Field(gt=0)
    occurred_at: datetime
    provenance: Literal["WORKFLOW_APPROVED_SYNTHETIC_DISPOSITION"]
    source_evidence_ids: tuple[str, ...] = Field(min_length=1)

    @field_validator("event_id", "lot_id")
    @classmethod
    def nonblank_disposition_identifier(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("disposition identifiers must be nonblank")
        return value

    @field_validator("source_evidence_ids")
    @classmethod
    def unique_source_evidence(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item.strip() for item in value) or len(value) != len(set(value)):
            raise ValueError("source evidence identifiers must be unique and nonblank")
        return value


class RecallCaseState(BaseModel):
    case_id: str
    thread_id: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    status: Literal["open", "closed"] = "open"
    case_version: CaseVersion = 0
    recall_number: str
    question: str = ""
    source_mode: str = "snapshot"
    recall: RecallRecord | None = None
    recall_predicate: RecallPredicate | None = None
    candidate_products: list[dict[str, Any]] = Field(default_factory=list)
    candidate_lots: list[dict[str, Any]] = Field(default_factory=list)
    trace_events: list[TraceEvent] = Field(default_factory=list)
    confirmed_lot_ids: list[str] = Field(default_factory=list)
    trace_event_ids: list[str] = Field(default_factory=list)
    required_facilities: list[str] = Field(default_factory=list)
    reconciliation: list[Reconciliation] = Field(default_factory=list)
    disposition_events: list[DispositionEvent] = Field(default_factory=list)
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
