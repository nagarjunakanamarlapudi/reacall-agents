"""Fail-closed policy checks used across agent and tool boundaries."""

from __future__ import annotations

import json
import math
from collections.abc import Collection, Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic_core import PydanticSerializationError, to_jsonable_python

from recallops.models import (
    ApprovalDecision,
    ProposedAction,
    proposed_action_digest,
    validate_case_version,
)

MASKED_VALUE = "[MASKED]"
PUBLIC_PROVENANCE = frozenset({"OFFICIAL_OPENFDA_SNAPSHOT", "LIVE_OPENFDA"})
SYNTHETIC_ORIGINS = frozenset({"SYNTHETIC_RETAILER_DIGITAL_TWIN"})
ProvenanceLabel = Literal[
    "OFFICIAL_OPENFDA_SNAPSHOT",
    "LIVE_OPENFDA",
    "SYNTHETIC_RETAILER_DIGITAL_TWIN",
    "SIMULATED_RECALL_OPERATIONS",
]
TOOL_PROVENANCE: dict[str, frozenset[str]] = {
    "search_recalls": PUBLIC_PROVENANCE,
    "get_recall": PUBLIC_PROVENANCE,
    "get_product_metadata": PUBLIC_PROVENANCE,
    "find_candidate_products": SYNTHETIC_ORIGINS,
    "match_lots": SYNTHETIC_ORIGINS,
    "trace_forward": SYNTHETIC_ORIGINS,
    "trace_backward": SYNTHETIC_ORIGINS,
    "get_inventory": SYNTHETIC_ORIGINS,
    "get_sales": SYNTHETIC_ORIGINS,
    "reconcile_units": SYNTHETIC_ORIGINS,
    "create_case": frozenset({"SIMULATED_RECALL_OPERATIONS"}),
    "apply_inventory_hold": frozenset({"SIMULATED_RECALL_OPERATIONS"}),
    "create_facility_tasks": frozenset({"SIMULATED_RECALL_OPERATIONS"}),
    "record_acknowledgment": frozenset({"SIMULATED_RECALL_OPERATIONS"}),
    "record_disposition": frozenset({"SIMULATED_RECALL_OPERATIONS"}),
    "close_case": frozenset({"SIMULATED_RECALL_OPERATIONS"}),
}
DEFAULT_SENSITIVE_KEYS = frozenset(
    {
        "address",
        "creditcard",
        "customer",
        "customerid",
        "customername",
        "email",
        "emailaddress",
        "phone",
        "phonenumber",
        "postaladdress",
        "ssn",
        "streetaddress",
    }
)


class ApprovalDeniedError(PermissionError):
    """Raised when a side effect has no explicit human approval."""


class StaleApprovalError(ApprovalDeniedError):
    """Raised when approval is bound to an older or different case version."""


class ApprovalScopeError(ApprovalDeniedError):
    """Raised when an action is absent from the approved action set."""


class ProvenanceRequiredError(ValueError):
    """Raised when evidence lacks a recognized public or synthetic source label."""


class ProgressStalledError(RuntimeError):
    """Raised when graph state repeats without material progress."""


def _validate_embedded_provenance(value: Any, expected: str) -> None:
    if not isinstance(value, Mapping):
        return
    present = [field for field in ("provenance", "origin") if field in value]
    if len(present) > 1:
        raise ProvenanceRequiredError("record has dual provenance and origin labels")
    if not present:
        return
    field = present[0]
    label = value[field]
    allowed = PUBLIC_PROVENANCE if field == "provenance" else SYNTHETIC_ORIGINS
    if label not in allowed:
        raise ProvenanceRequiredError(f"record has unknown {field} label: {label!r}")
    if label != expected:
        raise ProvenanceRequiredError(
            f"record {field} label {label!r} contradicts envelope {expected!r}"
        )


class ObservationRecord(BaseModel):
    """One gateway result item with boundary-assigned provenance."""

    model_config = ConfigDict(frozen=True)

    provenance: ProvenanceLabel
    value: Any

    @field_validator("value", mode="before")
    @classmethod
    def validate_value(cls, value: Any) -> Any:
        normalized = strict_json_value(value)
        if isinstance(normalized, list):
            raise ValueError("observation records cannot contain an unwrapped result list")
        return normalized


class ToolObservation(BaseModel):
    """Typed provenance envelope for one complete gateway observation."""

    model_config = ConfigDict(frozen=True)

    tool_name: str = Field(min_length=1)
    records: tuple[ObservationRecord, ...]

    @field_validator("tool_name")
    @classmethod
    def nonblank_tool_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("tool_name must be nonblank")
        return value


class ApprovalGuard:
    """Require an explicit, scoped, version-bound decision before every write."""

    def validate(
        self,
        approval: ApprovalDecision | None,
        *,
        proposed_action: ProposedAction,
        expected_case_version: int,
    ) -> ApprovalDecision:
        validate_case_version(expected_case_version, "expected_case_version")
        if approval is None:
            raise ApprovalDeniedError("explicit human approval is required")
        validate_case_version(
            approval.approved_case_version,
            "approved_case_version",
        )
        validate_case_version(
            proposed_action.expected_case_version,
            "proposed_action.expected_case_version",
        )
        if approval.decision != "approve":
            raise ApprovalDeniedError("side effects require an explicit approve decision")
        if not approval.actor.strip() or not approval.justification.strip():
            raise ApprovalDeniedError("approval requires a nonblank actor and justification")
        if approval.approved_case_version != expected_case_version:
            raise StaleApprovalError(
                f"approval version {approval.approved_case_version} does not match "
                f"current version {expected_case_version}"
            )
        if proposed_action.expected_case_version != expected_case_version:
            raise StaleApprovalError(
                "approval binding action version "
                f"{proposed_action.expected_case_version} does not match current version "
                f"{expected_case_version}"
            )
        if proposed_action.case_id != approval.approved_case_id:
            raise ApprovalScopeError(
                "approval binding case does not match the proposed action case"
            )
        binding = next(
            (
                item
                for item in approval.action_bindings
                if item.action_id == proposed_action.action_id
            ),
            None,
        )
        if binding is None:
            raise ApprovalScopeError(
                f"action {proposed_action.action_id!r} is outside the approval binding scope"
            )
        if binding.action_digest != proposed_action_digest(proposed_action):
            raise ApprovalScopeError(
                f"approval binding does not match reviewed action {proposed_action.action_id!r}"
            )
        return approval


def _normalize_key(key: object) -> str:
    return "".join(character for character in str(key).lower() if character.isalnum())


def mask_sensitive(
    value: Any,
    *,
    sensitive_keys: Collection[str] | None = None,
) -> Any:
    """Return a recursively masked copy while preserving collection shapes."""

    keys = DEFAULT_SENSITIVE_KEYS | {_normalize_key(key) for key in (sensitive_keys or ())}

    def is_sensitive(key: object) -> bool:
        normalized = _normalize_key(key)
        return normalized in keys or normalized.startswith("customer")

    return _json_copy(value, is_sensitive=is_sensitive)


def _json_copy(value: Any, *, is_sensitive: Any) -> Any:
    def normalize(item: Any) -> Any:
        if isinstance(item, BaseModel):
            return normalize(item.model_dump(mode="python"))
        if isinstance(item, Mapping):
            if any(not isinstance(key, str) for key in item):
                raise TypeError("JSON object keys must be strings")
            return {
                key: MASKED_VALUE if is_sensitive(key) else normalize(child)
                for key, child in sorted(item.items())
            }
        if isinstance(item, (list, tuple)):
            return [normalize(child) for child in item]
        if isinstance(item, (set, frozenset)):
            normalized = [normalize(child) for child in item]
            return sorted(normalized, key=_canonical_json)
        if item is None or isinstance(item, (bool, int, str)):
            return item
        if isinstance(item, float):
            if not math.isfinite(item):
                raise ValueError("JSON numbers must be finite")
            return item
        try:
            converted = to_jsonable_python(item)
        except (PydanticSerializationError, TypeError, ValueError) as error:
            raise TypeError(f"unsupported JSON value: {type(item).__name__}") from error
        if converted is item:
            raise TypeError(f"unsupported JSON value: {type(item).__name__}")
        return normalize(converted)

    return normalize(value)


def strict_json_value(value: Any) -> Any:
    """Return a detached, deterministic JSON value or reject unsupported input."""

    return _json_copy(value, is_sensitive=lambda _: False)


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )


def wrap_tool_observation(
    tool_name: str,
    output: Any,
    *,
    provenance: ProvenanceLabel,
) -> ToolObservation:
    """Assign provenance at the tool boundary without changing domain payloads."""

    _require_tool_provenance(tool_name, provenance)
    normalized = strict_json_value(output)
    values = normalized if isinstance(normalized, list) else [normalized]
    records = []
    for value in values:
        _validate_embedded_provenance(value, provenance)
        records.append(ObservationRecord(provenance=provenance, value=value))
    return ToolObservation(tool_name=tool_name, records=records)


def _require_tool_provenance(tool_name: str, provenance: str) -> None:
    allowed = TOOL_PROVENANCE.get(tool_name)
    if allowed is None:
        raise ProvenanceRequiredError(f"unknown gateway tool {tool_name!r}")
    if provenance not in allowed:
        raise ProvenanceRequiredError(
            f"tool provenance {provenance!r} is invalid for {tool_name!r}"
        )


def require_provenance(observation: Any) -> ToolObservation:
    """Accept only typed per-record observation envelopes and revalidate their contents."""

    if not isinstance(observation, ToolObservation):
        raise ProvenanceRequiredError("gateway output must be a typed ToolObservation envelope")
    for record in observation.records:
        _require_tool_provenance(observation.tool_name, record.provenance)
        normalized = strict_json_value(record.value)
        if isinstance(normalized, list):
            raise ProvenanceRequiredError("observation record contains unwrapped child records")
        _validate_embedded_provenance(normalized, record.provenance)
    return observation


def _signature_json(value: Any) -> str:
    def validate(item: Any) -> Any:
        if isinstance(item, Mapping):
            if any(not isinstance(key, str) for key in item):
                raise TypeError("progress signature mapping keys must be strings")
            return {key: validate(child) for key, child in item.items()}
        if isinstance(item, list):
            return [validate(child) for child in item]
        if item is None or isinstance(item, (bool, int, str)):
            return item
        if isinstance(item, float):
            if not math.isfinite(item):
                raise ValueError("progress signature numbers must be finite")
            return item
        raise TypeError(f"unsupported progress signature value: {type(item).__name__}")

    return _canonical_json(validate(value))


class ProgressWatchdog:
    """Escalate after a bounded number of consecutive equivalent signatures."""

    def __init__(self, max_repeats: int) -> None:
        if isinstance(max_repeats, bool) or not isinstance(max_repeats, int) or max_repeats <= 0:
            raise ValueError("max_repeats must be a positive integer")
        self.max_repeats = max_repeats
        self.current_signature: str | None = None
        self.repeat_count = 0

    def observe(self, signature: Any) -> int:
        canonical = _signature_json(signature)
        if canonical == self.current_signature:
            self.repeat_count += 1
            if self.repeat_count >= self.max_repeats:
                raise ProgressStalledError(f"progress signature repeated {self.repeat_count} times")
        else:
            self.current_signature = canonical
            self.repeat_count = 0
        return self.repeat_count

    def reset(self) -> None:
        self.current_signature = None
        self.repeat_count = 0
