from __future__ import annotations

from datetime import UTC, datetime

import pytest

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
    mask_sensitive,
    require_provenance,
)
from recallops.agents.telemetry import TraceEvent, TraceRecorder
from recallops.models import ApprovalDecision, Product


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


@pytest.mark.parametrize("limit", [0, -1])
def test_call_budget_rejects_nonpositive_limits(limit: int) -> None:
    with pytest.raises(ValueError, match="positive"):
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


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"max_attempts": 0}, "max_attempts"),
        ({"base_delay_seconds": -0.1}, "base_delay_seconds"),
        ({"backoff_multiplier": 0}, "backoff_multiplier"),
    ],
)
def test_with_retry_rejects_invalid_configuration(
    kwargs: dict[str, int | float], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        with_retry(lambda: None, **kwargs)


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
            {"failure_threshold": 1, "reset_timeout_seconds": -1},
            "reset_timeout_seconds",
        ),
    ],
)
def test_circuit_breaker_rejects_invalid_configuration(
    kwargs: dict[str, int], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        CircuitBreaker(**kwargs)


def _approval(
    *, decision: str = "approve", version: int = 4, action_ids: list[str] | None = None
) -> ApprovalDecision:
    return ApprovalDecision(
        decision=decision,
        actor="food-safety-manager",
        justification="Evidence supports the simulated containment action.",
        approved_at=datetime(2026, 8, 30, tzinfo=UTC),
        approved_case_version=version,
        action_ids=action_ids if action_ids is not None else ["action-hold"],
    )


def test_approval_guard_accepts_only_matching_approved_action_and_version() -> None:
    approval = _approval()

    assert (
        ApprovalGuard().validate(
            approval,
            action_id="action-hold",
            expected_case_version=4,
        )
        is approval
    )


@pytest.mark.parametrize("decision", ["edit", "reject", "escalate"])
def test_approval_guard_rejects_nonapproval_decisions(decision: str) -> None:
    with pytest.raises(ApprovalDeniedError, match="explicit approve"):
        ApprovalGuard().validate(
            _approval(decision=decision),
            action_id="action-hold",
            expected_case_version=4,
        )


def test_approval_guard_rejects_missing_decision() -> None:
    with pytest.raises(ApprovalDeniedError, match="required"):
        ApprovalGuard().validate(
            None,
            action_id="action-hold",
            expected_case_version=4,
        )


def test_approval_guard_rejects_stale_case_version() -> None:
    with pytest.raises(StaleApprovalError, match="version 3.*current version 4"):
        ApprovalGuard().validate(
            _approval(version=3),
            action_id="action-hold",
            expected_case_version=4,
        )


def test_approval_guard_rejects_action_outside_approved_scope() -> None:
    with pytest.raises(ApprovalScopeError, match="action-close"):
        ApprovalGuard().validate(
            _approval(action_ids=["action-hold"]),
            action_id="action-close",
            expected_case_version=4,
        )


def test_approval_guard_rejects_blank_action_and_invalid_expected_version() -> None:
    guard = ApprovalGuard()

    with pytest.raises(ApprovalScopeError, match="nonblank"):
        guard.validate(_approval(action_ids=[" "]), action_id=" ", expected_case_version=4)
    with pytest.raises(ValueError, match="nonnegative"):
        guard.validate(_approval(), action_id="action-hold", expected_case_version=-1)


def test_approval_guard_rejects_blank_actor_or_justification() -> None:
    approval = _approval()
    approval.actor = " "

    with pytest.raises(ApprovalDeniedError, match="nonblank actor"):
        ApprovalGuard().validate(
            approval,
            action_id="action-hold",
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
                ("unchanged", {"street address": "[MASKED]"}),
            ],
        },
    }
    assert observation["customer_name"] == "Ada Lovelace"
    assert observation["details"]["Email-Address"] == "ada@example.test"


def test_mask_sensitive_uses_explicit_extra_sensitive_keys() -> None:
    assert mask_sensitive(
        {"reviewer_alias": "ops-42", "lot": "LOT-1"},
        sensitive_keys={"reviewer_alias"},
    ) == {"reviewer_alias": "[MASKED]", "lot": "LOT-1"}


def test_require_provenance_accepts_public_and_synthetic_records() -> None:
    public = {"recall_number": "H-1230-2026", "provenance": "LIVE_OPENFDA"}
    product = Product(
        product_id="PROD-1",
        name="Demo product",
        origin="SYNTHETIC_RETAILER_DIGITAL_TWIN",
    )

    assert require_provenance(public) is public
    assert require_provenance([public, product]) == [public, product]


@pytest.mark.parametrize(
    "observation",
    [
        {"recall_number": "H-1230-2026"},
        {"origin": "UNKNOWN"},
        [],
        [{"origin": "SYNTHETIC_RETAILER_DIGITAL_TWIN"}, {"lot_id": "unlabelled"}],
    ],
)
def test_require_provenance_rejects_unlabelled_or_unknown_observations(
    observation: object,
) -> None:
    with pytest.raises(ProvenanceRequiredError, match="provenance"):
        require_provenance(observation)


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


@pytest.mark.parametrize("max_repeats", [0, -1])
def test_progress_watchdog_rejects_nonpositive_limits(max_repeats: int) -> None:
    with pytest.raises(ValueError, match="positive"):
        ProgressWatchdog(max_repeats=max_repeats)


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
