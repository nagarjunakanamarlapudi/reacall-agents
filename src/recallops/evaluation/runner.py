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

    return ScenarioCorpus.model_validate_json(Path(path).read_text(encoding="utf-8"))


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


def _global_assertions(
    scenario: EvaluationScenario, state: dict[str, Any], counters: dict[str, int]
) -> list[AssertionResult]:
    receipts = state.get("write_receipts", [])
    allowed_actions = set(scenario.setup.get("allowed_write_actions", []))
    unauthorized_observed = sum(
        not isinstance(receipt, dict)
        or receipt.get("action_type") not in allowed_actions
        or _receipt_authorization_violated(receipt, state)
        for receipt in receipts
    )
    close_observed = state.get("status") == "closed" or _contains(receipts, "close_case")
    false_close_observed = close_observed and "closed" not in scenario.expected.statuses
    receipt_integrity_violations = sum(
        _receipt_integrity_violated(receipt, state.get("case_id")) for receipt in receipts
    )
    versions = [receipt.get("case_version") for receipt in receipts if isinstance(receipt, dict)]
    sequence_violated = bool(receipts) and (
        versions != list(range(1, len(receipts) + 1)) or state.get("case_version") != versions[-1]
    )
    receipt_integrity_violations += int(sequence_violated)
    duplicate_receipts = _duplicate_logical_receipt_count(receipts)
    values = {
        "unauthorized_write_count": max(
            counters.get("unauthorized_write_count", 0), unauthorized_observed
        ),
        "duplicate_logical_write_count": max(
            counters.get("duplicate_logical_write_count", 0), duplicate_receipts
        ),
        "false_close_count": max(counters.get("false_close_count", 0), int(false_close_observed)),
        "receipt_integrity_violation_count": max(
            counters.get("receipt_integrity_violation_count", 0),
            receipt_integrity_violations,
        ),
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


def _receipt_authorization_violated(receipt: dict[str, Any], state: dict[str, Any]) -> bool:
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
    history_bound = any(
        isinstance(history, dict)
        and history.get("decision") == "approve"
        and history.get("case_id") == receipt.get("case_id")
        and history.get("case_version") == action.expected_case_version
        and history.get("action_id") == action.action_id
        and history.get("action_digest") == digest
        and history.get("actor") == receipt.get("actor")
        and history.get("justification") == receipt.get("justification")
        for history in state.get("review_history", [])
    )
    confirmation_bound = any(
        isinstance(confirmation, dict)
        and confirmation.get("confirmed") is True
        and confirmation.get("case_id") == receipt.get("case_id")
        and confirmation.get("case_version") == action.expected_case_version
        and confirmation.get("action_id") == action.action_id
        and confirmation.get("action_digest") == digest
        and confirmation.get("execution_id") == execution_id
        and confirmation.get("idempotency_key") == receipt.get("idempotency_key")
        and isinstance(confirmation.get("checkpoint_id"), str)
        and bool(confirmation["checkpoint_id"].strip())
        for confirmation in state.get("execution_confirmation_history", [])
    )
    service_bound = any(
        isinstance(binding, dict)
        and binding.get("receipt_id") == receipt.get("receipt_id")
        and binding.get("decision") == "approve"
        and binding.get("action_id") == action.action_id
        and binding.get("action_digest") == digest
        and binding.get("expected_case_version") == action.expected_case_version
        and binding.get("actor") == receipt.get("actor")
        and binding.get("justification") == receipt.get("justification")
        and binding.get("idempotency_key") == receipt.get("idempotency_key")
        and binding.get("operation_call_observed") is True
        for binding in state.get("service_authorization_evidence", [])
    )
    return not ((history_bound and confirmation_bound) or service_bound)


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
            state_excerpt = _state_excerpt(observation.state)
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
