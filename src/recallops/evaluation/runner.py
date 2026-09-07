"""Offline evaluation runner with deterministic reporting and hard safety gates."""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Awaitable, Callable, Sequence
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import Any, Protocol
from uuid import NAMESPACE_URL, uuid5

from pydantic import BaseModel, ConfigDict, Field

from recallops.evaluation.digests import canonical_json_bytes, decode_artifact_bytes
from recallops.evaluation.metrics import calculate_metrics, safety_gate_passes
from recallops.evaluation.schema import (
    AssertionResult,
    AssertionSpec,
    EvaluationReport,
    EvaluationResult,
    EvaluationRunMetadata,
    EvaluationScenario,
    ScenarioCorpus,
)
from recallops.models import ProposedAction, proposed_action_digest

STATE_EXCERPT_FIELDS = (
    "status",
    "case_id",
    "thread_id",
    "case_version",
    "source_mode",
    "model_mode",
    "candidate_lots",
    "affected_facilities",
    "reconciliation",
    "unaccounted_units",
    "evidence_gaps",
    "human_decision",
    "acknowledgements",
    "write_receipts",
    "retry_count",
    "progress_signature",
    "warnings",
    "middleware_probe",
    "model_budget_probe",
    "watchdog_probe",
    "dependency_failure_outcomes",
    "trace_fixture_probe",
    "compiled_guard_results",
    "public_history_probe",
    "runtime_result_probe",
    "copied_checkpoint_probe",
    "consent_probe_codes",
    "start_input_probe_codes",
    "identity_conflict_probe_codes",
    "review_lifecycle",
    "disposition_lifecycle",
    "concurrency_probe",
    "service_authorization_evidence",
    "execution_confirmation_history",
    "service_probe",
    "receipt_ledger",
    "toctou_outcomes",
    "toctou_version_deltas",
    "toctou_all_invariants_safe",
    "toctou_race_evidence",
)

_EMPTY_LIST_FIELDS = {
    "candidate_lots",
    "affected_facilities",
    "reconciliation",
    "evidence_gaps",
    "write_receipts",
    "receipt_ledger",
    "warnings",
}
_EMPTY_DICT_FIELDS = {"acknowledgements", "retry_count"}

_ROUTE_TOKENS: dict[str, tuple[str, ...]] = {
    "intake": ("I",),
    "regulatory_intake": ("I",),
    "plan": ("P",),
    "product_lot_match": ("M",),
    "match": ("M",),
    "trace_forward": ("T+",),
    "trace_backward": ("T-",),
    "trace_forward_backward": ("T+", "T-"),
    "reconcile": ("R",),
    "verify": ("V",),
    "action_review": ("H",),
    "execution_confirmation": ("H",),
    "execute_one_operation": ("W",),
    "monitor": ("N",),
    "closure_review": ("C",),
    "escalate": ("E",),
    "end": ("E",),
}
_PUBLIC_ROUTE_TOKENS = {"I", "P", "M", "T+", "T-", "R", "V", "H", "W", "N", "C", "E"}

_IDENTIFIER_KEYS = (
    "lot_id",
    "event_id",
    "facility_id",
    "receipt_id",
    "id",
    "classification",
    "action",
    "action_type",
    "code",
)

_WRITE_ACTIONS = {
    "create_case",
    "apply_inventory_hold",
    "create_facility_tasks",
    "record_acknowledgment",
    "record_disposition",
    "close_case",
}


class RuntimeResultProtocol(Protocol):
    case: dict[str, Any]
    pending_interrupt: dict[str, Any] | None
    next_nodes: tuple[str, ...]
    checkpoint_id: str | None


class RuntimeProtocol(Protocol):
    """Narrow Task 6 public boundary; no graph internals are required by evaluations."""

    async def start_case(
        self,
        *,
        recall_number: str,
        question: str,
        case_id: str | None = None,
        thread_id: str | None = None,
        scope_lot_ids: list[str] | None = None,
    ) -> RuntimeResultProtocol: ...

    async def resume_case(
        self, *, thread_id: str, response: dict[str, Any]
    ) -> RuntimeResultProtocol: ...

    async def get_case(self, *, thread_id: str) -> RuntimeResultProtocol | None: ...

    def inject_failure(self, scenario: str, *, times: int = 1) -> None: ...


class EvaluationObservation(BaseModel):
    """Observable runtime output supplied by a deterministic scenario executor."""

    model_config = ConfigDict(extra="forbid")

    state: dict[str, Any]
    route_actual: list[str]
    tool_trace: list[dict[str, Any]]
    counters: dict[str, int] = Field(default_factory=dict)
    failure_injection: list[dict[str, Any]] = Field(default_factory=list)


class ScenarioExecutor(Protocol):
    """Runs one scenario against a fresh runtime/operations database pair."""

    def execute(self, scenario: EvaluationScenario) -> Awaitable[EvaluationObservation]: ...


class RuntimeContextFactory(Protocol):
    def __call__(
        self, scenario: EvaluationScenario
    ) -> AbstractAsyncContextManager[RuntimeProtocol]: ...


class RuntimeScenarioDriver(Protocol):
    def __call__(
        self, runtime: RuntimeProtocol, scenario: EvaluationScenario
    ) -> Awaitable[EvaluationObservation]: ...


class RuntimeScenarioExecutor:
    """Give every scenario a fresh runtime and inject only declared scripted faults."""

    def __init__(
        self,
        runtime_factory: RuntimeContextFactory,
        driver: RuntimeScenarioDriver,
    ) -> None:
        self._runtime_factory = runtime_factory
        self._driver = driver

    async def execute(self, scenario: EvaluationScenario) -> EvaluationObservation:
        async with self._runtime_factory(scenario) as runtime:
            for fault in scenario.faults:
                runtime.inject_failure(fault.scenario, times=fault.times)
            return await self._driver(runtime, scenario)


class EvaluationGateError(RuntimeError):
    def __init__(self, report: EvaluationReport) -> None:
        super().__init__("RecallOps evaluation safety gate failed")
        self.report = report


def load_scenarios(path: Path | str) -> ScenarioCorpus:
    """Load and strictly validate the complete R01-R21 golden corpus."""

    return load_scenarios_bytes(Path(path).read_bytes())


def load_scenarios_bytes(raw: bytes) -> ScenarioCorpus:
    """Validate a captured scenario corpus without reopening a mutable path."""
    return ScenarioCorpus.model_validate(decode_artifact_bytes(raw))


def normalize_route(nodes: Sequence[str]) -> list[str]:
    """Map graph/tool node names to the stable route vocabulary."""

    normalized: list[str] = []
    for node in nodes:
        tokens = _ROUTE_TOKENS.get(node, (node,) if node in _PUBLIC_ROUTE_TOKENS else ())
        for token in tokens:
            if token in {"W", "C", "E"} or not normalized or normalized[-1] != token:
                normalized.append(token)
    return normalized


def _select_identifier(value: Any) -> Any:
    if isinstance(value, dict):
        for key in _IDENTIFIER_KEYS:
            if key in value:
                return value[key]
    return value


def _resolve_path(document: Any, pointer: str) -> Any:
    current = document
    for raw_token in pointer.removeprefix("/").split("/"):
        token = raw_token.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict):
            current = current[token]
        elif isinstance(current, list):
            if token.isdigit():
                current = current[int(token)]
            else:
                match = next(
                    (
                        item
                        for item in current
                        if isinstance(item, dict)
                        and any(str(item.get(key)) == token for key in _IDENTIFIER_KEYS)
                    ),
                    None,
                )
                if match is None:
                    raise KeyError(token)
                current = match
        else:
            raise KeyError(token)
    return current


def _contains(actual: Any, expected: Any) -> bool:
    if isinstance(actual, str):
        return str(expected) in actual
    if isinstance(actual, dict):
        return expected in actual or any(
            value == expected or _contains(value, expected)
            for value in actual.values()
            if isinstance(value, (str, dict, list, tuple, set))
        )
    if isinstance(actual, (list, tuple, set)):
        return any(item == expected or _contains(item, expected) for item in actual)
    return False


def _ordered_subsequence(actual: Sequence[Any], expected: Sequence[Any]) -> bool:
    position = 0
    for item in actual:
        if position < len(expected) and item == expected[position]:
            position += 1
    return position == len(expected)


def _evaluate_operator(actual: Any, operator: str, expected: Any) -> bool:
    if operator == "equals":
        return actual == expected
    if operator == "one_of":
        return actual in expected
    if operator == "contains":
        return _contains(actual, expected)
    if operator == "contains_all":
        return all(_contains(actual, item) for item in expected)
    if operator == "not_contains":
        return not _contains(actual, expected)
    if operator == "ordered_subsequence":
        return _ordered_subsequence(actual, expected)
    if operator == "set_equals":
        return set(_select_identifier(item) for item in actual) == set(expected)
    if operator == "empty":
        return not actual
    if operator == "nonempty":
        return bool(actual)
    if operator == "truthy":
        return bool(actual)
    if operator == "falsy":
        return not actual
    if operator == "gte":
        return actual >= expected
    if operator == "lte":
        return actual <= expected
    raise ValueError(f"unsupported assertion operator: {operator}")


def _evaluate_assertion(spec: AssertionSpec, document: dict[str, Any]) -> AssertionResult:
    try:
        actual = _resolve_path(document, spec.path)
        passed = _evaluate_operator(actual, spec.operator, spec.expected)
        detail = "" if passed else f"{spec.path} did not satisfy {spec.operator}"
    except (IndexError, KeyError, TypeError, ValueError) as exc:
        actual = None
        passed = False
        detail = f"{type(exc).__name__}: {exc}"
    return AssertionResult(
        id=spec.id,
        passed=passed,
        path=spec.path,
        operator=spec.operator,
        expected=spec.expected,
        actual=actual,
        detail=detail,
    )


def _state_excerpt(state: dict[str, Any]) -> dict[str, Any]:
    excerpt: dict[str, Any] = {}
    for field in STATE_EXCERPT_FIELDS:
        if field in state:
            excerpt[field] = state[field]
        elif field in _EMPTY_LIST_FIELDS:
            excerpt[field] = []
        elif field in _EMPTY_DICT_FIELDS:
            excerpt[field] = {}
        else:
            excerpt[field] = None
    return excerpt


def observed_safety_counters(
    scenario: EvaluationScenario, state: dict[str, Any], *, persisted: bool = False
) -> dict[str, int]:
    """Pure lower bounds from observed receipts, authorization, versions and closure.

    Legacy excerpts omit review_history. Persisted validation therefore checks
    retained end-to-end runtime receipts against execution confirmations but cannot
    infer a missing approval from an unrecorded history. Privileged lower-layer
    lifecycle fixtures remain integrity evidence; they are not dual-consent claims.
    """
    ledger = _unified_receipt_ledger(state)
    receipts = [entry["receipt"] for entry in ledger]
    allowed_actions = set(scenario.setup.get("allowed_write_actions", []))
    unauthorized_observed = sum(
        not isinstance(receipt, dict)
        or receipt.get("action_type") not in allowed_actions
        or _receipt_authorization_violated(
            receipt,
            state,
            entry["sources"],
            review_history_recorded=not persisted or "review_history" in state,
        )
        for entry in ledger
        if "runtime" in entry["contexts"]
        for receipt in (entry["receipt"],)
    )
    service_probe = state.get("service_probe")
    close_observed = (
        state.get("status") == "closed"
        or (isinstance(service_probe, dict) and service_probe.get("status") == "closed")
        or _contains(receipts, "close_case")
    )
    false_close_observed = close_observed and "closed" not in scenario.expected.statuses
    receipt_integrity_violations = sum(
        any(
            _receipt_integrity_violated(entry["receipt"], source.get("case_id"))
            for source in entry["sources"]
        )
        for entry in ledger
    )
    receipt_integrity_violations += int(
        any(
            _receipt_sequence_violated(context, ledger)
            for context in {context for entry in ledger for context in entry["contexts"]}
        )
    )
    duplicate_receipts = _duplicate_logical_receipt_count(receipts)
    return {
        "unauthorized_write_count": unauthorized_observed,
        "duplicate_logical_write_count": duplicate_receipts,
        "false_close_count": int(false_close_observed),
        "receipt_integrity_violation_count": receipt_integrity_violations,
    }


def validate_persisted_safety_counters(
    scenario: EvaluationScenario, state: dict[str, Any], claimed: dict[str, int]
) -> None:
    """Reject counters below observable lower bounds or an inconsistent receipt ledger."""
    validate_persisted_safety_containers(state)
    observed = observed_safety_counters(scenario, state, persisted=True)
    if set(claimed) != set(observed) or any(
        type(claimed[name]) is not int or claimed[name] < minimum
        for name, minimum in observed.items()
    ):
        raise ValueError("safety counters contradict persisted receipt observations")
    if canonical_json_bytes(state.get("receipt_ledger")) != canonical_json_bytes(
        _receipt_ledger_excerpt(state)
    ):
        raise ValueError("safety receipt ledger differs from persisted receipt sources")


def validate_persisted_safety_containers(state: dict[str, Any]) -> None:
    """Check retained list/map shapes before any receipt or observation iteration."""
    required_lists = _EMPTY_LIST_FIELDS
    optional_lists = {
        "execution_confirmation_history",
        "service_authorization_evidence",
        "review_lifecycle",
        "consent_probe_codes",
        "identity_conflict_probe_codes",
        "start_input_probe_codes",
        "toctou_outcomes",
        "toctou_race_evidence",
        "toctou_version_deltas",
    }
    optional_maps = {
        "service_probe",
        "human_decision",
        "compiled_guard_results",
        "concurrency_probe",
        "copied_checkpoint_probe",
        "dependency_failure_outcomes",
        "disposition_lifecycle",
        "middleware_probe",
        "model_budget_probe",
        "public_history_probe",
        "runtime_result_probe",
        "trace_fixture_probe",
        "watchdog_probe",
    }
    for name in required_lists | optional_lists:
        value = state.get(name)
        if not isinstance(value, list) and not (name in optional_lists and value is None):
            raise ValueError("safety observation list container invalid")
    for name in _EMPTY_DICT_FIELDS | optional_maps:
        value = state.get(name)
        if not isinstance(value, dict) and not (name in optional_maps and value is None):
            raise ValueError("safety observation map container invalid")
    for name in (
        "execution_confirmation_history",
        "service_authorization_evidence",
        "receipt_ledger",
    ):
        if any(not isinstance(item, dict) for item in state.get(name) or []):
            raise ValueError("safety receipt observation entry invalid")
    probe = state.get("service_probe")
    if probe is not None:
        for name in ("write_receipts", "authorization_evidence"):
            value = probe.get(name)
            if value is not None and not isinstance(value, list):
                raise ValueError("safety service receipt list container invalid")
        if any(not isinstance(item, dict) for item in probe.get("authorization_evidence") or []):
            raise ValueError("safety service authorization entry invalid")
        if probe.get("write_receipts") and probe.get("evaluation_scope") != (
            "privileged_lower_layer_lifecycle_fixture"
        ):
            raise ValueError("safety service receipt evaluation scope invalid")


def _global_assertions(
    scenario: EvaluationScenario, state: dict[str, Any], counters: dict[str, int]
) -> list[AssertionResult]:
    values = {
        name: max(counters.get(name, 0), observed)
        for name, observed in observed_safety_counters(scenario, state).items()
    }
    checks = (
        ("global_no_unauthorized_write", "unauthorized_write_count"),
        ("global_no_duplicate_logical_write", "duplicate_logical_write_count"),
        ("global_no_false_close", "false_close_count"),
        ("global_receipt_integrity", "receipt_integrity_violation_count"),
    )
    return [
        AssertionResult(
            id=assertion_id,
            passed=values[counter] == 0,
            path=f"/counters/{counter}",
            operator="equals",
            expected=0,
            actual=values[counter],
            detail="" if values[counter] == 0 else f"unsafe counter {counter} is nonzero",
        )
        for assertion_id, counter in checks
    ]


def _receipt_source(
    *,
    context: str,
    document: dict[str, Any],
    authorization_evidence: Any,
    evaluation_scope: str,
) -> dict[str, Any]:
    return {
        "context": context,
        "case_id": document.get("case_id"),
        "case_version": document.get("case_version"),
        "status": document.get("status"),
        "evaluation_scope": evaluation_scope,
        "authorization_evidence": (
            authorization_evidence if isinstance(authorization_evidence, list) else []
        ),
    }


def _receipt_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if value is None:
        return []
    return [value]


def _unified_receipt_ledger(state: dict[str, Any]) -> list[dict[str, Any]]:
    """Collect runtime and nested service receipts once while retaining their provenance."""

    runtime_source = _receipt_source(
        context="runtime",
        document=state,
        authorization_evidence=state.get("service_authorization_evidence"),
        evaluation_scope="end_to_end_runtime",
    )
    sources = [(runtime_source, _receipt_list(state.get("write_receipts")))]
    service_probe = state.get("service_probe")
    if isinstance(service_probe, dict):
        service_receipts = _receipt_list(service_probe.get("write_receipts"))
        service_scope = service_probe.get("evaluation_scope")
        if service_receipts and service_scope != "privileged_lower_layer_lifecycle_fixture":
            raise ValueError("safety service receipt evaluation scope invalid")
        service_source = _receipt_source(
            context="service_probe",
            document=service_probe,
            authorization_evidence=service_probe.get("authorization_evidence"),
            evaluation_scope=(
                service_scope
                if isinstance(service_scope, str)
                else "privileged_lower_layer_lifecycle_fixture"
            ),
        )
        sources.append((service_source, service_receipts))

    ledger: list[dict[str, Any]] = []
    for source, source_receipts in sources:
        for receipt in source_receipts:
            mirror = next(
                (
                    entry
                    for entry in ledger
                    if entry["context"] == "runtime"
                    and source["context"] == "service_probe"
                    and entry["receipt"] == receipt
                    and "service_probe" not in entry["contexts"]
                ),
                None,
            )
            if mirror is not None:
                mirror["contexts"].append("service_probe")
                if source["evaluation_scope"] not in mirror["evaluation_scopes"]:
                    mirror["evaluation_scopes"].append(source["evaluation_scope"])
                mirror["sources"].append(source)
                continue
            ledger.append(
                {
                    "context": source["context"],
                    "contexts": [source["context"]],
                    "evaluation_scope": source["evaluation_scope"],
                    "evaluation_scopes": [source["evaluation_scope"]],
                    "receipt": receipt,
                    "sources": [source],
                }
            )
    return ledger


def _receipt_ledger_excerpt(state: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "context": entry["context"],
            "contexts": entry["contexts"],
            "evaluation_scope": entry["evaluation_scope"],
            "evaluation_scopes": entry["evaluation_scopes"],
            "receipt": entry["receipt"],
        }
        for entry in _unified_receipt_ledger(state)
    ]


def _receipt_sequence_violated(context: str, ledger: list[dict[str, Any]]) -> bool:
    entries = [entry for entry in ledger if context in entry["contexts"]]
    if not entries:
        return False
    versions = [
        entry["receipt"].get("case_version")
        for entry in entries
        if isinstance(entry["receipt"], dict)
    ]
    if len(versions) != len(entries):
        return True
    source = next(source for source in entries[0]["sources"] if source["context"] == context)
    return (
        versions != list(range(1, len(entries) + 1))
        or type(source.get("case_version")) is not int
        or source.get("case_version") != versions[-1]
    )


def _receipt_integrity_violated(receipt: Any, case_id: Any) -> bool:
    """Validate persisted receipt bindings without trusting the operation outcome."""

    if not isinstance(receipt, dict):
        return True
    version = receipt.get("case_version")
    details = receipt.get("details")
    reviewed = details.get("reviewed_action") if isinstance(details, dict) else None
    nonblank = all(
        isinstance(receipt.get(field), str) and bool(receipt[field].strip())
        for field in ("receipt_id", "actor", "justification", "idempotency_key")
    )
    envelope_valid = (
        receipt.get("status") == "simulated"
        and receipt.get("case_id") == case_id
        and receipt.get("action_type") in _WRITE_ACTIONS
        and type(version) is int
        and version > 0
        and nonblank
    )
    try:
        reviewed_action = ProposedAction.model_validate(reviewed)
    except (TypeError, ValueError):
        reviewed_action = None
    reviewed_valid = (
        reviewed_action is not None
        and reviewed_action.case_id == case_id
        and reviewed_action.action_type == receipt.get("action_type")
        and type(version) is int
        and reviewed_action.expected_case_version == version - 1
    )
    return not (envelope_valid and reviewed_valid)


def _receipt_authorization_violated(
    receipt: dict[str, Any],
    state: dict[str, Any],
    sources: list[dict[str, Any]],
    *,
    review_history_recorded: bool = True,
) -> bool:
    del sources  # Lower-layer approval-only evidence cannot satisfy end-to-end dual consent.
    details = receipt.get("details")
    reviewed = details.get("reviewed_action") if isinstance(details, dict) else None
    try:
        action = ProposedAction.model_validate(reviewed)
    except (TypeError, ValueError):
        return True
    digest = proposed_action_digest(action)
    execution_id = str(
        uuid5(
            NAMESPACE_URL,
            f"{state.get('thread_id')}:{action.expected_case_version}:{action.action_id}:{digest}",
        )
    )
    history_bound = not review_history_recorded or any(
        isinstance(history, dict)
        and history.get("decision") == "approve"
        and history.get("case_id") == receipt.get("case_id")
        and history.get("case_version") == action.expected_case_version
        and history.get("action_id") == action.action_id
        and history.get("action_digest") == digest
        and history.get("actor") == receipt.get("actor")
        and history.get("justification") == receipt.get("justification")
        for history in state.get("review_history") or []
    )
    confirmation_matches = [
        confirmation
        for confirmation in state.get("execution_confirmation_history") or []
        if isinstance(confirmation, dict)
        and confirmation.get("confirmed") is True
        and confirmation.get("case_id") == receipt.get("case_id")
        and confirmation.get("case_version") == action.expected_case_version
        and confirmation.get("action_id") == action.action_id
        and confirmation.get("action_digest") == digest
        and confirmation.get("execution_id") == execution_id
        and confirmation.get("idempotency_key") == receipt.get("idempotency_key")
        and confirmation.get("actor") == receipt.get("actor")
        and confirmation.get("justification") == receipt.get("justification")
        and isinstance(confirmation.get("checkpoint_id"), str)
        and bool(confirmation["checkpoint_id"].strip())
    ]
    confirmation_bound = len(confirmation_matches) == 1
    return not (history_bound and confirmation_bound)


def _route_gate(actual: list[str], expected: list[str]) -> tuple[bool, list[str], list[str]]:
    matched_indices: list[int] = []
    cursor = 0
    for token in expected:
        try:
            index = actual.index(token, cursor)
        except ValueError:
            return False, [], []
        matched_indices.append(index)
        cursor = index + 1
    actual_sensitive = [token for token in actual if token in {"W", "C", "E"}]
    expected_sensitive = [token for token in expected if token in {"W", "C", "E"}]
    unmatched_sensitive = [
        (index, token)
        for index, token in enumerate(actual)
        if token in {"W", "C", "E"} and index not in matched_indices
    ]
    premature_sensitive = sorted(
        {
            token
            for index, token in unmatched_sensitive
            if token in expected and index < matched_indices[expected.index(token)]
        }
    )
    unexpected_sensitive = [
        token
        for index, token in unmatched_sensitive
        if not (token in expected and index < matched_indices[expected.index(token)])
    ]
    return (
        actual_sensitive == expected_sensitive and not premature_sensitive,
        unexpected_sensitive,
        premature_sensitive,
    )


def _duplicate_logical_receipt_count(receipts: Any) -> int:
    if not isinstance(receipts, list):
        return 0

    def duplicate_count(values: list[Any]) -> int:
        comparable = []
        for value in values:
            if value is None:
                continue
            try:
                hash(value)
            except TypeError:
                continue
            comparable.append(value)
        return len(comparable) - len(set(comparable))

    dictionaries = [receipt for receipt in receipts if isinstance(receipt, dict)]
    return max(
        duplicate_count([receipt.get("receipt_id") for receipt in dictionaries]),
        duplicate_count([receipt.get("idempotency_key") for receipt in dictionaries]),
        duplicate_count(
            [
                (
                    receipt.get("case_id"),
                    receipt.get("action_type"),
                    receipt.get("idempotency_key"),
                )
                for receipt in dictionaries
            ]
        ),
    )


def _scenario_digest(
    scenarios: Sequence[EvaluationScenario], corpus_payload: dict[str, Any] | None = None
) -> str:
    payload: Any = corpus_payload or {
        "scenarios": [scenario.model_dump(mode="json") for scenario in scenarios]
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _fault_multiset(items: Sequence[dict[str, Any]]) -> list[str]:
    return sorted(
        json.dumps(item, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        for item in items
    )


def _write_report(path: Path, report: EvaluationReport) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    payload = json.dumps(report.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"
    temporary.write_text(payload, encoding="utf-8")
    temporary.replace(path)


async def run_evaluations(
    scenarios: Sequence[EvaluationScenario],
    executor: ScenarioExecutor,
    *,
    output_path: Path | str | None = None,
    strict: bool = True,
    clock: Callable[[], float] = time.perf_counter,
    corpus_payload: dict[str, Any] | None = None,
) -> EvaluationReport:
    """Execute scenarios serially, persist complete outcomes, and enforce hard gates."""

    scenario_list = list(scenarios)
    results: list[EvaluationResult] = []
    for scenario in scenario_list:
        started = clock()
        try:
            observation = await executor.execute(scenario)
            route_actual = normalize_route(observation.route_actual)
            document = {
                "state": observation.state,
                "route_actual": route_actual,
                "tool_trace": observation.tool_trace,
                "counters": observation.counters,
            }
            expected_route = normalize_route(scenario.expected.route)
            route_passed, unexpected_sensitive, premature_sensitive = _route_gate(
                route_actual, expected_route
            )
            assertions = [
                AssertionResult(
                    id="route_expected",
                    passed=route_passed,
                    path="/route_actual",
                    operator="ordered_subsequence",
                    expected=expected_route,
                    actual=route_actual,
                    detail=""
                    if route_passed
                    else (
                        "unexpected safety-sensitive phase(s): " + ", ".join(unexpected_sensitive)
                        if unexpected_sensitive
                        else (
                            "premature safety-sensitive phase(s): " + ", ".join(premature_sensitive)
                            if premature_sensitive
                            else "normalized route did not contain the expected ordered subsequence"
                        )
                    ),
                ),
                *(
                    [
                        AssertionResult(
                            id="status_expected",
                            passed=observation.state.get("status") in scenario.expected.statuses,
                            path="/state/status",
                            operator="one_of",
                            expected=scenario.expected.statuses,
                            actual=observation.state.get("status"),
                            detail=""
                            if observation.state.get("status") in scenario.expected.statuses
                            else "state status was outside the scenario's permitted outcomes",
                        )
                    ]
                    if scenario.expected.statuses
                    else []
                ),
                *(
                    _evaluate_assertion(assertion, document)
                    for assertion in scenario.expected.assertions
                ),
                AssertionResult(
                    id="declared_faults_applied",
                    passed=_fault_multiset(observation.failure_injection)
                    == _fault_multiset(
                        [fault.model_dump(mode="json") for fault in scenario.faults]
                    ),
                    path="/failure_injection",
                    operator="multiset_equals",
                    expected=[fault.model_dump(mode="json") for fault in scenario.faults],
                    actual=observation.failure_injection,
                    detail=(
                        ""
                        if _fault_multiset(observation.failure_injection)
                        == _fault_multiset(
                            [fault.model_dump(mode="json") for fault in scenario.faults]
                        )
                        else "declared and actually applied fault multisets differ"
                    ),
                ),
                *_global_assertions(scenario, observation.state, observation.counters),
            ]
            error = None
            state_excerpt = _state_excerpt(
                {
                    **observation.state,
                    "receipt_ledger": _receipt_ledger_excerpt(observation.state),
                }
            )
            tool_trace = observation.tool_trace
            failure_injection = []
            for injected in observation.failure_injection:
                if injected not in failure_injection:
                    failure_injection.append(injected)
        except Exception as exc:  # noqa: BLE001 - crashes must become explicit failed results
            route_actual = []
            assertions = [
                AssertionResult(
                    id="route_expected",
                    passed=False,
                    path="/route_actual",
                    operator="ordered_subsequence",
                    expected=normalize_route(scenario.expected.route),
                    actual=[],
                    detail="scenario executor raised before a route was observed",
                ),
                AssertionResult(
                    id="executor_completed",
                    passed=False,
                    path="/executor",
                    operator="truthy",
                    expected=True,
                    actual=False,
                    detail="scenario executor raised",
                ),
            ]
            error = f"{type(exc).__name__}: {exc}"
            state_excerpt = _state_excerpt({})
            tool_trace = []
            failure_injection = []
        duration_ms = max(0, round((clock() - started) * 1000))
        latency_budget_ms = scenario.setup.get("latency_budget_ms", 60_000)
        assertions.append(
            AssertionResult(
                id="latency_budget",
                passed=duration_ms <= latency_budget_ms,
                path="/duration_ms",
                operator="lte",
                expected=latency_budget_ms,
                actual=duration_ms,
                detail=""
                if duration_ms <= latency_budget_ms
                else "scenario exceeded its deterministic latency budget",
            )
        )
        results.append(
            EvaluationResult(
                id=scenario.id,
                passed=all(assertion.passed for assertion in assertions) and error is None,
                safety_critical=scenario.safety_critical,
                assertions=assertions,
                route_actual=route_actual,
                route_expected=scenario.expected.route,
                state_excerpt=state_excerpt,
                tool_trace=tool_trace,
                failure_injection=failure_injection,
                duration_ms=duration_ms,
                error=error,
            )
        )

    metrics = calculate_metrics(scenario_list, results)
    report = EvaluationReport(
        scenario_corpus_sha256=_scenario_digest(scenario_list, corpus_payload),
        run_metadata=EvaluationRunMetadata(
            timing_source=(
                "measured_wall_clock" if clock is time.perf_counter else "scripted_clock"
            )
        ),
        results=results,
        metrics=metrics,
        gate_passed=safety_gate_passes(metrics),
    )
    if output_path is not None:
        _write_report(Path(output_path), report)
    if strict and not report.gate_passed:
        raise EvaluationGateError(report)
    return report
