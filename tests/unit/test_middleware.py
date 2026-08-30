from __future__ import annotations

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Barrier

import pytest
from pydantic import BaseModel, ValidationError

from recallops.agents.middleware import (
    CallBudget,
    CallBudgetExceeded,
    CircuitBreaker,
    CircuitOpenError,
    PermanentCallError,
    TransientCallError,
    with_retry,
)
from recallops.agents.policies import (
    ApprovalDeniedError,
    ApprovalGuard,
    ApprovalScopeError,
    ProgressStalledError,
    ProgressWatchdog,
    ProvenanceRequiredError,
    StaleApprovalError,
    ToolObservation,
    mask_sensitive,
    require_provenance,
    wrap_tool_observation,
)
from recallops.agents.telemetry import TraceEvent, TraceRecorder
from recallops.models import (
    ApprovalBinding,
    ApprovalDecision,
    Product,
    ProposedAction,
    proposed_action_digest,
)


def test_call_budget_blocks_the_call_beyond_its_limit_and_can_reset() -> None:
    calls: list[str] = []
    budget = CallBudget(limit=2)

    assert budget.call(lambda value: calls.append(value) or value, "first") == "first"
    assert budget.call(lambda value: calls.append(value) or value, "second") == "second"

    with pytest.raises(CallBudgetExceeded, match="2 calls"):
        budget.call(lambda: calls.append("forbidden"))

    assert calls == ["first", "second"]
    assert budget.used == 2
    assert budget.remaining == 0

    budget.reset()
    assert budget.call(lambda: "after-reset") == "after-reset"
    assert budget.used == 1


@pytest.mark.parametrize("limit", [0, -1, True, 1.0, float("nan"), float("inf")])
def test_call_budget_rejects_nonintegral_or_nonpositive_limits(limit: object) -> None:
    with pytest.raises((TypeError, ValueError), match="positive integer"):
        CallBudget(limit=limit)


def test_with_retry_retries_transient_calls_with_exponential_delays() -> None:
    attempts = 0
    delays: list[float] = []
    budget = CallBudget(limit=3)

    def flaky_read() -> str:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise TransientCallError("temporary timeout")
        return "evidence"

    result = with_retry(
        flaky_read,
        max_attempts=3,
        base_delay_seconds=0.25,
        backoff_multiplier=2,
        sleep=delays.append,
        budget=budget,
    )()

    assert result == "evidence"
    assert attempts == 3
    assert delays == [0.25, 0.5]
    assert budget.used == 3


def test_with_retry_does_not_retry_permanent_failures() -> None:
    attempts = 0
    delays: list[float] = []

    def invalid_request() -> None:
        nonlocal attempts
        attempts += 1
        raise PermanentCallError("invalid schema")

    wrapped = with_retry(invalid_request, max_attempts=4, sleep=delays.append)

    with pytest.raises(PermanentCallError, match="invalid schema"):
        wrapped()

    assert attempts == 1
    assert delays == []


def test_with_retry_fails_closed_for_write_calls_even_on_transient_failure() -> None:
    attempts = 0
    recorder = TraceRecorder()

    def simulated_write() -> None:
        nonlocal attempts
        attempts += 1
        raise TransientCallError("lost write response")

    wrapped = with_retry(
        simulated_write,
        max_attempts=3,
        operation_kind="write",
        sleep=lambda _: None,
        recorder=recorder,
        operation_name="apply_inventory_hold",
    )

    with pytest.raises(TransientCallError, match="lost write response"):
        wrapped()

    assert attempts == 1
    assert [(event.status, event.attempt) for event in recorder.events] == [("error", 1)]


@pytest.mark.asyncio
async def test_with_retry_supports_async_callables() -> None:
    attempts = 0
    delays: list[float] = []

    async def flaky_model() -> str:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise TransientCallError("rate limited")
        return "structured output"

    async def record_delay(delay: float) -> None:
        delays.append(delay)

    wrapped = with_retry(
        flaky_model,
        max_attempts=2,
        base_delay_seconds=0.1,
        sleep=record_delay,
    )

    assert await wrapped() == "structured output"
    assert attempts == 2
    assert delays == [0.1]


@pytest.mark.asyncio
async def test_with_retry_awaits_async_callable_objects_before_recording_outcome() -> None:
    recorder = TraceRecorder()

    class AsyncCallable:
        def __init__(self) -> None:
            self.attempts = 0

        async def __call__(self) -> str:
            self.attempts += 1
            if self.attempts == 1:
                raise TransientCallError("retry me")
            return "awaited"

    operation = AsyncCallable()
    wrapped = with_retry(
        operation,
        max_attempts=2,
        base_delay_seconds=0,
        recorder=recorder,
    )

    assert await wrapped() == "awaited"
    assert operation.attempts == 2
    assert [(event.status, event.attempt) for event in recorder.events] == [
        ("retry", 1),
        ("success", 2),
    ]


@pytest.mark.asyncio
async def test_with_retry_awaits_sync_callable_awaitable_result_before_success() -> None:
    recorder = TraceRecorder()
    attempts = 0

    def returns_awaitable():
        async def result() -> str:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise TransientCallError("retry awaitable")
            return "done"

        return result()

    wrapped = with_retry(
        returns_awaitable,
        max_attempts=2,
        base_delay_seconds=0,
        recorder=recorder,
    )

    assert await wrapped() == "done"
    assert attempts == 2
    assert [(event.status, event.attempt) for event in recorder.events] == [
        ("retry", 1),
        ("success", 2),
    ]


@pytest.mark.asyncio
async def test_async_callable_write_fails_closed_without_retry() -> None:
    class AsyncWrite:
        def __init__(self) -> None:
            self.attempts = 0

        async def __call__(self) -> None:
            self.attempts += 1
            raise TransientCallError("unknown write result")

    operation = AsyncWrite()
    wrapped = with_retry(operation, max_attempts=3, operation_kind="write")

    with pytest.raises(TransientCallError):
        await wrapped()
    assert operation.attempts == 1


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"max_attempts": 0}, "max_attempts"),
        ({"max_attempts": True}, "max_attempts"),
        ({"max_attempts": 2.0}, "max_attempts"),
        ({"base_delay_seconds": -0.1}, "base_delay_seconds"),
        ({"base_delay_seconds": True}, "base_delay_seconds"),
        ({"base_delay_seconds": float("nan")}, "base_delay_seconds"),
        ({"base_delay_seconds": float("inf")}, "base_delay_seconds"),
        ({"backoff_multiplier": 0}, "backoff_multiplier"),
        ({"backoff_multiplier": True}, "backoff_multiplier"),
        ({"backoff_multiplier": float("nan")}, "backoff_multiplier"),
        ({"backoff_multiplier": float("inf")}, "backoff_multiplier"),
    ],
)
def test_with_retry_rejects_invalid_configuration(
    kwargs: dict[str, int | float], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        with_retry(lambda: None, **kwargs)


def test_with_retry_rejects_unknown_operation_kind_at_construction() -> None:
    with pytest.raises(ValueError, match="operation_kind"):
        with_retry(lambda: None, operation_kind="delete")


def test_circuit_breaker_opens_then_allows_one_probe_after_timeout() -> None:
    now = [100.0]
    attempts = 0
    breaker = CircuitBreaker(
        failure_threshold=2,
        reset_timeout_seconds=10,
        clock=lambda: now[0],
    )

    def unavailable() -> None:
        nonlocal attempts
        attempts += 1
        raise TransientCallError("service unavailable")

    for _ in range(2):
        with pytest.raises(TransientCallError):
            breaker.call(unavailable)

    assert breaker.state == "open"
    with pytest.raises(CircuitOpenError, match="open"):
        breaker.call(lambda: "must not run")
    assert attempts == 2

    now[0] += 10
    assert breaker.call(lambda: "healthy") == "healthy"
    assert breaker.state == "closed"
    assert breaker.failure_count == 0


def test_circuit_breaker_reopens_when_half_open_probe_fails() -> None:
    now = [20.0]
    breaker = CircuitBreaker(
        failure_threshold=1,
        reset_timeout_seconds=5,
        clock=lambda: now[0],
    )

    with pytest.raises(TransientCallError):
        breaker.call(lambda: (_ for _ in ()).throw(TransientCallError("first")))
    now[0] += 5
    with pytest.raises(TransientCallError):
        breaker.call(lambda: (_ for _ in ()).throw(TransientCallError("probe")))

    now[0] += 4.9
    with pytest.raises(CircuitOpenError):
        breaker.call(lambda: "too early")
    now[0] += 0.1
    assert breaker.call(lambda: "recovered") == "recovered"


def test_permanent_failure_does_not_trip_circuit() -> None:
    breaker = CircuitBreaker(failure_threshold=1, reset_timeout_seconds=10)

    with pytest.raises(PermanentCallError):
        breaker.call(lambda: (_ for _ in ()).throw(PermanentCallError("bad input")))

    assert breaker.state == "closed"
    assert breaker.failure_count == 0


def test_noncounted_permanent_failure_preserves_prior_transient_history() -> None:
    breaker = CircuitBreaker(failure_threshold=2, reset_timeout_seconds=10)

    with pytest.raises(TransientCallError):
        breaker.call(lambda: (_ for _ in ()).throw(TransientCallError("first")))
    with pytest.raises(PermanentCallError):
        breaker.call(lambda: (_ for _ in ()).throw(PermanentCallError("invalid")))

    assert breaker.failure_count == 1
    with pytest.raises(TransientCallError):
        breaker.call(lambda: (_ for _ in ()).throw(TransientCallError("second")))
    assert breaker.state == "open"


@pytest.mark.asyncio
async def test_cancelled_half_open_probe_reopens_and_releases_probe_lease() -> None:
    now = [10.0]
    breaker = CircuitBreaker(
        failure_threshold=1,
        reset_timeout_seconds=5,
        clock=lambda: now[0],
    )
    with pytest.raises(TransientCallError):
        breaker.call(lambda: (_ for _ in ()).throw(TransientCallError("open")))

    now[0] += 5

    async def cancelled_probe() -> None:
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await with_retry(cancelled_probe, breaker=breaker)()

    assert breaker.state == "open"
    with pytest.raises(CircuitOpenError, match="open"):
        breaker.call(lambda: "probe lease was not released")

    now[0] += 5
    assert breaker.call(lambda: "recovered") == "recovered"


@pytest.mark.asyncio
async def test_circuit_call_does_not_record_awaitable_success_before_awaiting() -> None:
    breaker = CircuitBreaker(failure_threshold=1, reset_timeout_seconds=10)

    async def failed_after_await() -> None:
        await asyncio.sleep(0)
        raise TransientCallError("async dependency failed")

    pending = breaker.call(failed_after_await)
    assert breaker.failure_count == 0
    with pytest.raises(TransientCallError):
        await pending

    assert breaker.state == "open"
    assert breaker.failure_count == 1


def test_base_exception_during_sync_half_open_retry_releases_probe_lease() -> None:
    now = [10.0]
    breaker = CircuitBreaker(
        failure_threshold=1,
        reset_timeout_seconds=5,
        clock=lambda: now[0],
    )
    with pytest.raises(TransientCallError):
        breaker.call(lambda: (_ for _ in ()).throw(TransientCallError("open")))
    now[0] += 5

    with pytest.raises(KeyboardInterrupt):
        with_retry(lambda: (_ for _ in ()).throw(KeyboardInterrupt()), breaker=breaker)()

    assert breaker.state == "open"


def test_budget_block_during_half_open_probe_releases_probe_lease() -> None:
    now = [10.0]
    breaker = CircuitBreaker(
        failure_threshold=1,
        reset_timeout_seconds=5,
        clock=lambda: now[0],
    )
    with pytest.raises(TransientCallError):
        breaker.call(lambda: (_ for _ in ()).throw(TransientCallError("open")))
    now[0] += 5
    budget = CallBudget(1)
    budget.consume()

    with pytest.raises(CallBudgetExceeded):
        with_retry(lambda: "blocked", budget=budget, breaker=breaker)()

    assert breaker.state == "open"


def test_with_retry_stops_before_calling_an_open_circuit() -> None:
    attempts = 0
    breaker = CircuitBreaker(failure_threshold=1, reset_timeout_seconds=60)

    def unavailable() -> None:
        nonlocal attempts
        attempts += 1
        raise TransientCallError("timeout")

    wrapped = with_retry(unavailable, max_attempts=3, sleep=lambda _: None, breaker=breaker)

    with pytest.raises(CircuitOpenError):
        wrapped()
    assert attempts == 1


def test_circuit_breaker_manual_reset_clears_open_state() -> None:
    breaker = CircuitBreaker(failure_threshold=1, reset_timeout_seconds=60)
    with pytest.raises(TransientCallError):
        breaker.call(lambda: (_ for _ in ()).throw(TransientCallError("timeout")))

    breaker.reset()

    assert breaker.state == "closed"
    assert breaker.failure_count == 0
    assert breaker.call(lambda: "ready") == "ready"


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        (
            {"failure_threshold": 0, "reset_timeout_seconds": 1},
            "failure_threshold",
        ),
        (
            {"failure_threshold": True, "reset_timeout_seconds": 1},
            "failure_threshold",
        ),
        (
            {"failure_threshold": 1.0, "reset_timeout_seconds": 1},
            "failure_threshold",
        ),
        (
            {"failure_threshold": 1, "reset_timeout_seconds": -1},
            "reset_timeout_seconds",
        ),
        (
            {"failure_threshold": 1, "reset_timeout_seconds": True},
            "reset_timeout_seconds",
        ),
        (
            {"failure_threshold": 1, "reset_timeout_seconds": float("nan")},
            "reset_timeout_seconds",
        ),
        (
            {"failure_threshold": 1, "reset_timeout_seconds": float("inf")},
            "reset_timeout_seconds",
        ),
    ],
)
def test_circuit_breaker_rejects_invalid_configuration(
    kwargs: dict[str, object], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        CircuitBreaker(**kwargs)


def _action(
    *,
    action_id: str = "action-hold",
    action_type: str = "apply_inventory_hold",
    case_id: str = "case-001",
    target_ids: list[str] | None = None,
    rationale: str = "Contain the evidence-backed affected lot.",
    evidence_ids: list[str] | None = None,
    version: int = 4,
) -> ProposedAction:
    return ProposedAction(
        action_id=action_id,
        action_type=action_type,
        case_id=case_id,
        target_ids=target_ids if target_ids is not None else ["LOT-1"],
        rationale=rationale,
        evidence_ids=evidence_ids if evidence_ids is not None else ["EV-1"],
        expected_case_version=version,
    )


def _approval(
    *,
    action: ProposedAction | None = None,
    decision: str = "approve",
    version: int = 4,
) -> ApprovalDecision:
    reviewed = action or _action(version=version)
    return ApprovalDecision(
        decision=decision,
        actor="food-safety-manager",
        justification="Evidence supports the simulated containment action.",
        approved_at=datetime(2026, 8, 30, tzinfo=UTC),
        approved_case_version=version,
        approved_case_id=reviewed.case_id,
        action_ids=[reviewed.action_id],
        action_bindings=[
            ApprovalBinding(
                action_id=reviewed.action_id,
                action_digest=proposed_action_digest(reviewed),
            )
        ],
    )


def test_approval_guard_accepts_only_matching_approved_action_and_version() -> None:
    action = _action()
    approval = _approval(action=action)

    assert (
        ApprovalGuard().validate(
            approval,
            proposed_action=action,
            expected_case_version=4,
        )
        is approval
    )


@pytest.mark.parametrize("decision", ["edit", "reject", "escalate"])
def test_approval_guard_rejects_nonapproval_decisions(decision: str) -> None:
    with pytest.raises(ApprovalDeniedError, match="explicit approve"):
        action = _action()
        ApprovalGuard().validate(
            _approval(action=action, decision=decision),
            proposed_action=action,
            expected_case_version=4,
        )


def test_approval_guard_rejects_missing_decision() -> None:
    with pytest.raises(ApprovalDeniedError, match="required"):
        ApprovalGuard().validate(
            None,
            proposed_action=_action(),
            expected_case_version=4,
        )


def test_approval_guard_rejects_stale_case_version() -> None:
    action = _action(version=3)
    with pytest.raises(StaleApprovalError, match="version 3.*current version 4"):
        ApprovalGuard().validate(
            _approval(action=action, version=3),
            proposed_action=action,
            expected_case_version=4,
        )


def test_approval_guard_rejects_action_outside_approved_scope() -> None:
    reviewed = _action()
    different = _action(action_id="action-close", action_type="close_case", target_ids=[])
    with pytest.raises(ApprovalScopeError, match="action-close"):
        ApprovalGuard().validate(
            _approval(action=reviewed),
            proposed_action=different,
            expected_case_version=4,
        )


def test_approval_guard_rejects_blank_action_and_invalid_expected_version() -> None:
    guard = ApprovalGuard()

    with pytest.raises(ValueError, match="nonnegative"):
        guard.validate(_approval(), proposed_action=_action(), expected_case_version=-1)


def test_approval_contract_rejects_blank_actor_or_justification() -> None:
    payload = _approval().model_dump(mode="json")
    payload["actor"] = " "

    with pytest.raises(ValidationError, match="nonblank"):
        ApprovalDecision.model_validate(payload)


@pytest.mark.parametrize(
    "changed_action",
    [
        _action(case_id="case-002"),
        _action(action_type="create_facility_tasks"),
        _action(target_ids=["LOT-2"]),
        _action(rationale="A different rationale after review."),
        _action(evidence_ids=["EV-2"]),
        _action(version=5),
    ],
)
def test_approval_guard_rejects_cross_case_or_post_review_action_mutation(
    changed_action: ProposedAction,
) -> None:
    reviewed = _action()

    with pytest.raises((ApprovalScopeError, StaleApprovalError), match="approval|binding"):
        ApprovalGuard().validate(
            _approval(action=reviewed),
            proposed_action=changed_action,
            expected_case_version=4,
        )


def test_mask_sensitive_recursively_masks_values_without_mutating_input() -> None:
    observation = {
        "customer_name": "Ada Lovelace",
        "details": {
            "Email-Address": "ada@example.test",
            "Customer Email": "ada.secondary@example.test",
            "safe": "LOT-PROBABLE-160",
            "contacts": [
                {"phone_number": "+1-555-0100", "facility": "STORE-004"},
                ("unchanged", {"street address": "1 Main St"}),
            ],
        },
    }

    masked = mask_sensitive(observation)

    assert masked == {
        "customer_name": "[MASKED]",
        "details": {
            "Email-Address": "[MASKED]",
            "Customer Email": "[MASKED]",
            "safe": "LOT-PROBABLE-160",
            "contacts": [
                {"phone_number": "[MASKED]", "facility": "STORE-004"},
                ["unchanged", {"street address": "[MASKED]"}],
            ],
        },
    }
    assert observation["customer_name"] == "Ada Lovelace"
    assert observation["details"]["Email-Address"] == "ada@example.test"


def test_mask_sensitive_recurses_into_models_and_canonicalizes_unordered_collections() -> None:
    class Contact(BaseModel):
        customer_email: str
        safe_tags: frozenset[str]

    contact = Contact(customer_email="ada@example.test", safe_tags=frozenset({"z", "a"}))
    value = {
        "contact": contact,
        "numbers": {3, 1, 2},
        "tuple": ("keep", "order"),
    }

    masked = mask_sensitive(value)

    assert masked == {
        "contact": {"customer_email": "[MASKED]", "safe_tags": ["a", "z"]},
        "numbers": [1, 2, 3],
        "tuple": ["keep", "order"],
    }
    assert contact.customer_email == "ada@example.test"
    assert json.dumps(masked, allow_nan=False)


def test_mask_sensitive_uses_explicit_extra_sensitive_keys() -> None:
    assert mask_sensitive(
        {"reviewer_alias": "ops-42", "lot": "LOT-1"},
        sensitive_keys={"reviewer_alias"},
    ) == {"reviewer_alias": "[MASKED]", "lot": "LOT-1"}


def test_tool_observation_wraps_each_gateway_record_with_explicit_provenance() -> None:
    public = {"recall_number": "H-1230-2026", "provenance": "LIVE_OPENFDA"}
    product = Product(
        product_id="PROD-1",
        name="Demo product",
        origin="SYNTHETIC_RETAILER_DIGITAL_TWIN",
    )

    public_observation = wrap_tool_observation(
        "search_recalls", [public], provenance="LIVE_OPENFDA"
    )
    synthetic_observation = wrap_tool_observation(
        "find_candidate_products",
        [product],
        provenance="SYNTHETIC_RETAILER_DIGITAL_TWIN",
    )

    assert isinstance(require_provenance(public_observation), ToolObservation)
    assert public_observation.records[0].value == public
    assert synthetic_observation.records[0].value["product_id"] == "PROD-1"
    assert all(
        record.provenance == "SYNTHETIC_RETAILER_DIGITAL_TWIN"
        for record in synthetic_observation.records
    )


@pytest.mark.parametrize(
    ("observation", "message"),
    [
        ({"recall_number": "H-1230-2026"}, "ToolObservation"),
        (
            {
                "provenance": "OFFICIAL_OPENFDA_SNAPSHOT",
                "payload": [{"lot_id": "unlabelled"}],
            },
            "ToolObservation",
        ),
        ([], "ToolObservation"),
    ],
)
def test_require_provenance_rejects_raw_or_labelled_wrapper_bypasses(
    observation: object, message: str
) -> None:
    with pytest.raises(ProvenanceRequiredError, match=message):
        require_provenance(observation)


@pytest.mark.parametrize(
    ("provenance", "payload"),
    [
        (
            "SYNTHETIC_RETAILER_DIGITAL_TWIN",
            {"lot_id": "LOT-1", "origin": "UNKNOWN"},
        ),
        (
            "OFFICIAL_OPENFDA_SNAPSHOT",
            {
                "recall_number": "H-1230-2026",
                "provenance": "OFFICIAL_OPENFDA_SNAPSHOT",
                "origin": "SYNTHETIC_RETAILER_DIGITAL_TWIN",
            },
        ),
        (
            "OFFICIAL_OPENFDA_SNAPSHOT",
            {"recall_number": "H-1230-2026", "provenance": "LIVE_OPENFDA"},
        ),
    ],
)
def test_tool_observation_rejects_unknown_dual_or_contradictory_embedded_labels(
    provenance: str, payload: dict[str, str]
) -> None:
    with pytest.raises(ProvenanceRequiredError, match="label|contradict"):
        tool_name = (
            "trace_forward" if provenance == "SYNTHETIC_RETAILER_DIGITAL_TWIN" else "get_recall"
        )
        wrap_tool_observation(tool_name, payload, provenance=provenance)


def test_tool_observation_rejects_false_source_assignment_for_unlabelled_gateway_output() -> None:
    with pytest.raises(ProvenanceRequiredError, match="tool provenance"):
        wrap_tool_observation(
            "reconcile_units",
            {"lot_id": "LOT-1", "unaccounted": 0},
            provenance="OFFICIAL_OPENFDA_SNAPSHOT",
        )
    with pytest.raises(ProvenanceRequiredError, match="unknown gateway tool"):
        wrap_tool_observation(
            "arbitrary_tool",
            {"value": "untrusted"},
            provenance="LIVE_OPENFDA",
        )
    forged = ToolObservation.model_validate(
        {
            "tool_name": "reconcile_units",
            "records": [
                {
                    "provenance": "OFFICIAL_OPENFDA_SNAPSHOT",
                    "value": {"lot_id": "LOT-1", "unaccounted": 0},
                }
            ],
        }
    )
    with pytest.raises(ProvenanceRequiredError, match="tool provenance"):
        require_provenance(forged)


@pytest.mark.parametrize(
    ("tool_name", "payload", "provenance"),
    [
        (
            "search_recalls",
            [{"recall_number": "H-1230-2026", "provenance": "OFFICIAL_OPENFDA_SNAPSHOT"}],
            "OFFICIAL_OPENFDA_SNAPSHOT",
        ),
        (
            "get_recall",
            {"recall_number": "H-1230-2026", "provenance": "LIVE_OPENFDA"},
            "LIVE_OPENFDA",
        ),
        (
            "get_product_metadata",
            {"upc": "011110609038", "source": "openFDA recall product description"},
            "OFFICIAL_OPENFDA_SNAPSHOT",
        ),
        (
            "find_candidate_products",
            [{"product_id": "P-1", "origin": "SYNTHETIC_RETAILER_DIGITAL_TWIN"}],
            "SYNTHETIC_RETAILER_DIGITAL_TWIN",
        ),
        (
            "match_lots",
            [{"lot_id": "LOT-1", "origin": "SYNTHETIC_RETAILER_DIGITAL_TWIN"}],
            "SYNTHETIC_RETAILER_DIGITAL_TWIN",
        ),
        (
            "trace_forward",
            [{"event_id": "EV-1", "origin": "SYNTHETIC_RETAILER_DIGITAL_TWIN"}],
            "SYNTHETIC_RETAILER_DIGITAL_TWIN",
        ),
        (
            "trace_backward",
            [{"event_id": "EV-1", "origin": "SYNTHETIC_RETAILER_DIGITAL_TWIN"}],
            "SYNTHETIC_RETAILER_DIGITAL_TWIN",
        ),
        (
            "get_inventory",
            [{"position_id": "INV-1", "origin": "SYNTHETIC_RETAILER_DIGITAL_TWIN"}],
            "SYNTHETIC_RETAILER_DIGITAL_TWIN",
        ),
        (
            "get_sales",
            [{"event_id": "EV-S-1", "origin": "SYNTHETIC_RETAILER_DIGITAL_TWIN"}],
            "SYNTHETIC_RETAILER_DIGITAL_TWIN",
        ),
        (
            "reconcile_units",
            {"lot_id": "LOT-1", "received": 10, "unaccounted": 0},
            "SYNTHETIC_RETAILER_DIGITAL_TWIN",
        ),
        (
            "apply_inventory_hold",
            {"receipt_id": "R-1", "status": "simulated"},
            "SIMULATED_RECALL_OPERATIONS",
        ),
    ],
)
def test_observation_envelope_supports_every_gateway_output_category(
    tool_name: str, payload: object, provenance: str
) -> None:
    observation = wrap_tool_observation(tool_name, payload, provenance=provenance)

    assert require_provenance(observation) is observation
    assert all(record.provenance == provenance for record in observation.records)


def test_progress_watchdog_escalates_consecutive_equivalent_signatures() -> None:
    watchdog = ProgressWatchdog(max_repeats=2)

    assert watchdog.observe({"node": "trace", "lots": ["LOT-1"]}) == 0
    assert watchdog.observe({"lots": ["LOT-1"], "node": "trace"}) == 1
    with pytest.raises(ProgressStalledError, match="repeated 2 times"):
        watchdog.observe({"node": "trace", "lots": ["LOT-1"]})


def test_progress_watchdog_resets_on_new_progress_and_explicit_reset() -> None:
    watchdog = ProgressWatchdog(max_repeats=1)

    watchdog.observe("plan")
    watchdog.observe("trace")
    assert watchdog.repeat_count == 0
    watchdog.reset()
    assert watchdog.current_signature is None
    assert watchdog.observe("plan") == 0


@pytest.mark.parametrize("max_repeats", [0, -1, True, 1.0])
def test_progress_watchdog_rejects_nonintegral_or_nonpositive_limits(
    max_repeats: object,
) -> None:
    with pytest.raises(ValueError, match="positive integer"):
        ProgressWatchdog(max_repeats=max_repeats)


@pytest.mark.parametrize(
    ("signature", "error", "message"),
    [
        ({1: "integer key"}, TypeError, "string"),
        ({"value": float("nan")}, ValueError, "finite"),
        ({"value": float("inf")}, ValueError, "finite"),
        ({"value": object()}, TypeError, "unsupported"),
        ({"value": (1, 2)}, TypeError, "unsupported"),
        ({"value": {1, 2}}, TypeError, "unsupported"),
    ],
)
def test_progress_watchdog_rejects_ambiguous_or_non_json_signatures(
    signature: object, error: type[Exception], message: str
) -> None:
    watchdog = ProgressWatchdog(max_repeats=1)
    with pytest.raises(error, match=message):
        watchdog.observe(signature)


@pytest.mark.parametrize("first", [1, 1.0, True, None, "1"])
def test_progress_watchdog_preserves_distinct_json_value_types(first: object) -> None:
    distinct_values = [1, 1.0, True, None, "1"]
    second = next(value for value in distinct_values if type(value) is not type(first))
    watchdog = ProgressWatchdog(max_repeats=1)

    assert watchdog.observe({"value": first}) == 0
    assert watchdog.observe({"value": second}) == 0
    with pytest.raises(ProgressStalledError):
        watchdog.observe({"value": second})


def test_trace_recorder_emits_structured_events_for_every_boundary_and_masks_attributes() -> None:
    recorder = TraceRecorder(
        case_id="case-001",
        thread_id="thread-001",
        wall_clock=lambda: datetime(2026, 8, 30, 12, 0, tzinfo=UTC),
    )

    for boundary in ("agent", "tool", "model", "middleware"):
        event = recorder.record(
            boundary=boundary,
            operation=f"{boundary}-operation",
            status="success",
            duration_ms=1.25,
            attributes={"customer_name": "Ada", "lot_id": "LOT-1"},
        )
        assert isinstance(event, TraceEvent)

    payloads = recorder.to_dicts()
    assert [event["event_id"] for event in payloads] == [
        "trace-000001",
        "trace-000002",
        "trace-000003",
        "trace-000004",
    ]
    assert [event["boundary"] for event in payloads] == [
        "agent",
        "tool",
        "model",
        "middleware",
    ]
    assert all(event["case_id"] == "case-001" for event in payloads)
    assert all(event["thread_id"] == "thread-001" for event in payloads)
    assert all(event["attributes"]["customer_name"] == "[MASKED]" for event in payloads)
    assert all(event["attributes"]["lot_id"] == "LOT-1" for event in payloads)


def test_trace_span_records_success_and_error_durations_without_swallowing_failure() -> None:
    ticks = iter([10.0, 10.125, 20.0, 20.5])
    recorder = TraceRecorder(monotonic_clock=lambda: next(ticks))

    with recorder.span(boundary="agent", operation="intake"):
        pass

    with pytest.raises(PermanentCallError, match="invalid"):
        with recorder.span(boundary="model", operation="extract"):
            raise PermanentCallError("invalid")

    assert [(event.status, event.duration_ms) for event in recorder.events] == [
        ("success", 125.0),
        ("error", 500.0),
    ]
    assert recorder.events[1].attributes == {"error_type": "PermanentCallError"}


def test_with_retry_records_each_retry_and_success_as_structured_trace() -> None:
    recorder = TraceRecorder(case_id="case-001", thread_id="thread-001")
    attempts = 0

    def flaky_tool() -> str:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise TransientCallError("customer email should not enter trace")
        return "ok"

    result = with_retry(
        flaky_tool,
        max_attempts=2,
        base_delay_seconds=0,
        sleep=lambda _: None,
        recorder=recorder,
        boundary="tool",
        operation_name="search_recalls",
    )()

    assert result == "ok"
    assert [(event.status, event.attempt) for event in recorder.events] == [
        ("retry", 1),
        ("success", 2),
    ]
    assert recorder.events[0].attributes == {
        "delay_seconds": 0,
        "error_type": "TransientCallError",
    }
    assert all(event.operation == "search_recalls" for event in recorder.events)


def test_with_retry_records_permanent_failure_without_retrying() -> None:
    recorder = TraceRecorder()

    wrapped = with_retry(
        lambda: (_ for _ in ()).throw(PermanentCallError("bad request")),
        recorder=recorder,
        boundary="model",
        operation_name="extract_predicate",
    )

    with pytest.raises(PermanentCallError):
        wrapped()

    assert [(event.status, event.attempt) for event in recorder.events] == [("error", 1)]
    assert recorder.events[0].attributes == {"error_type": "PermanentCallError"}


def test_trace_recorder_rejects_non_json_or_nonfinite_attributes() -> None:
    recorder = TraceRecorder()

    with pytest.raises(TypeError, match="unsupported JSON value"):
        recorder.record(
            boundary="tool",
            operation="bad-object",
            status="error",
            duration_ms=0,
            attributes={"value": object()},
        )
    with pytest.raises(ValueError, match="finite"):
        recorder.record(
            boundary="tool",
            operation="bad-number",
            status="error",
            duration_ms=0,
            attributes={"value": float("nan")},
        )


def test_trace_recorder_canonicalizes_mapping_and_set_attributes() -> None:
    recorder = TraceRecorder()
    recorder.record(
        boundary="middleware",
        operation="canonicalize",
        status="success",
        duration_ms=0,
        attributes={"z": {"b", "a"}, "a": {"y": 2, "x": 1}},
    )

    attributes = recorder.to_dicts()[0]["attributes"]
    assert list(attributes) == ["a", "z"]
    assert list(attributes["a"]) == ["x", "y"]
    assert attributes["z"] == ["a", "b"]


def test_trace_recorder_returns_detached_events_and_payloads() -> None:
    recorder = TraceRecorder()
    returned = recorder.record(
        boundary="tool",
        operation="trace_forward",
        status="success",
        duration_ms=1,
        attributes={"nested": {"lot_id": "LOT-1"}},
    )

    returned.attributes["nested"]["lot_id"] = "MUTATED"
    payload = recorder.to_dicts()
    payload[0]["attributes"]["nested"]["lot_id"] = "ALSO-MUTATED"

    assert recorder.events[0].attributes == {"nested": {"lot_id": "LOT-1"}}


def test_trace_recorder_assigns_ids_and_appends_atomically_under_concurrency() -> None:
    worker_count = 12
    barrier = Barrier(worker_count)
    recorder = TraceRecorder(wall_clock=lambda: datetime(2026, 8, 30, 12, 0, tzinfo=UTC))

    def record(worker: int) -> None:
        barrier.wait()
        recorder.record(
            boundary="agent",
            operation=f"worker-{worker:02d}",
            status="success",
            duration_ms=0,
        )

    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        list(executor.map(record, range(worker_count)))

    events = recorder.events
    assert [event.event_id for event in events] == [
        f"trace-{index:06d}" for index in range(1, worker_count + 1)
    ]
    assert len({event.operation for event in events}) == worker_count
    assert [item["event_id"] for item in recorder.to_dicts()] == [
        event.event_id for event in events
    ]
