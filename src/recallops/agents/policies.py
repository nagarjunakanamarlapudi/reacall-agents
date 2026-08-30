"""Fail-closed policy checks used across agent and tool boundaries."""

from __future__ import annotations

import json
from collections.abc import Collection, Mapping, Sequence
from typing import Any

from pydantic import BaseModel

from recallops.models import ApprovalDecision

MASKED_VALUE = "[MASKED]"
PUBLIC_PROVENANCE = frozenset({"OFFICIAL_OPENFDA_SNAPSHOT", "LIVE_OPENFDA"})
SYNTHETIC_ORIGINS = frozenset({"SYNTHETIC_RETAILER_DIGITAL_TWIN"})
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


class ApprovalGuard:
    """Require an explicit, scoped, version-bound decision before every write."""

    def validate(
        self,
        approval: ApprovalDecision | None,
        *,
        action_id: str,
        expected_case_version: int,
    ) -> ApprovalDecision:
        if not action_id.strip():
            raise ApprovalScopeError("approved action ID must be nonblank")
        if expected_case_version < 0:
            raise ValueError("expected_case_version must be nonnegative")
        if approval is None:
            raise ApprovalDeniedError("explicit human approval is required")
        if approval.decision != "approve":
            raise ApprovalDeniedError("side effects require an explicit approve decision")
        if not approval.actor.strip() or not approval.justification.strip():
            raise ApprovalDeniedError("approval requires a nonblank actor and justification")
        if approval.approved_case_version != expected_case_version:
            raise StaleApprovalError(
                f"approval version {approval.approved_case_version} does not match "
                f"current version {expected_case_version}"
            )
        if action_id not in approval.action_ids:
            raise ApprovalScopeError(f"action {action_id!r} is outside the approved scope")
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

    def mask(item: Any) -> Any:
        if isinstance(item, Mapping):
            return {
                key: MASKED_VALUE if is_sensitive(key) else mask(child)
                for key, child in item.items()
            }
        if isinstance(item, list):
            return [mask(child) for child in item]
        if isinstance(item, tuple):
            return tuple(mask(child) for child in item)
        if isinstance(item, set):
            return {mask(child) for child in item}
        return item

    if isinstance(value, BaseModel):
        return mask(value.model_dump(mode="json"))
    return mask(value)


def require_provenance(observation: Any) -> Any:
    """Validate record-level provenance without inspecting an official payload's internals."""

    if isinstance(observation, BaseModel):
        record: Any = observation.model_dump(mode="json")
    else:
        record = observation

    if isinstance(record, Mapping):
        labels: list[tuple[str, Any, frozenset[str]]] = []
        if "provenance" in record:
            labels.append(("provenance", record["provenance"], PUBLIC_PROVENANCE))
        if "origin" in record:
            labels.append(("origin", record["origin"], SYNTHETIC_ORIGINS))
        if not labels:
            raise ProvenanceRequiredError("observation is missing provenance or origin")
        for field, label, allowed in labels:
            if label not in allowed:
                raise ProvenanceRequiredError(
                    f"observation has unknown provenance label in {field}: {label!r}"
                )
        return observation

    if isinstance(record, Sequence) and not isinstance(record, (str, bytes, bytearray)):
        if not record:
            raise ProvenanceRequiredError(
                "observation collection has no provenance-labelled records"
            )
        for item in record:
            require_provenance(item)
        return observation

    raise ProvenanceRequiredError("observation must be a provenance-labelled record or collection")


def _signature_json(value: Any) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    if isinstance(value, Mapping):
        normalized = {str(key): json.loads(_signature_json(child)) for key, child in value.items()}
        return json.dumps(normalized, sort_keys=True, separators=(",", ":"), default=str)
    if isinstance(value, (list, tuple)):
        return json.dumps(
            [json.loads(_signature_json(child)) for child in value],
            separators=(",", ":"),
            default=str,
        )
    if isinstance(value, (set, frozenset)):
        normalized_items = sorted(_signature_json(child) for child in value)
        return json.dumps(
            [json.loads(child) for child in normalized_items],
            separators=(",", ":"),
            default=str,
        )
    return json.dumps(value, separators=(",", ":"), default=str)


class ProgressWatchdog:
    """Escalate after a bounded number of consecutive equivalent signatures."""

    def __init__(self, max_repeats: int) -> None:
        if max_repeats <= 0:
            raise ValueError("max_repeats must be positive")
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
