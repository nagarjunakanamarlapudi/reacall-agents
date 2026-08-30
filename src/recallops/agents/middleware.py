"""Composable middleware for bounded and recoverable calls."""

from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import Callable
from functools import wraps
from threading import Lock
from typing import Any, Literal, cast

from recallops.agents.telemetry import TraceBoundary, TraceRecorder


class TransientCallError(RuntimeError):
    """A retryable dependency failure such as a timeout or rate limit."""


class PermanentCallError(RuntimeError):
    """A non-retryable failure such as invalid input or denied permission."""


class CircuitOpenError(RuntimeError):
    """Raised before a dependency call while its circuit is open."""


class CallBudgetExceeded(RuntimeError):
    """Raised before a call that would exceed its configured budget."""


class CallBudget:
    """Count actual call attempts and fail closed at a fixed limit."""

    def __init__(self, limit: int) -> None:
        if limit <= 0:
            raise ValueError("call budget limit must be positive")
        self.limit = limit
        self.used = 0

    @property
    def remaining(self) -> int:
        return self.limit - self.used

    def consume(self) -> int:
        if self.used >= self.limit:
            raise CallBudgetExceeded(f"call budget exhausted after {self.limit} calls")
        self.used += 1
        return self.used

    def call(self, operation: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
        self.consume()
        return operation(*args, **kwargs)

    def reset(self) -> None:
        self.used = 0


class CircuitBreaker:
    """Three-state breaker that counts only configured dependency failures."""

    def __init__(
        self,
        failure_threshold: int,
        reset_timeout_seconds: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        counts_failure: Callable[[BaseException], bool] | None = None,
    ) -> None:
        if failure_threshold <= 0:
            raise ValueError("failure_threshold must be positive")
        if reset_timeout_seconds < 0:
            raise ValueError("reset_timeout_seconds must be nonnegative")
        self.failure_threshold = failure_threshold
        self.reset_timeout_seconds = reset_timeout_seconds
        self._clock = clock
        self._counts_failure = counts_failure or (
            lambda error: isinstance(error, TransientCallError)
        )
        self._state: Literal["closed", "open", "half_open"] = "closed"
        self._failure_count = 0
        self._opened_at: float | None = None
        self._probe_in_progress = False
        self._lock = Lock()

    @property
    def state(self) -> Literal["closed", "open", "half_open"]:
        with self._lock:
            return self._state

    @property
    def failure_count(self) -> int:
        with self._lock:
            return self._failure_count

    def before_call(self) -> None:
        with self._lock:
            if self._state == "open":
                assert self._opened_at is not None
                elapsed = self._clock() - self._opened_at
                if elapsed < self.reset_timeout_seconds:
                    remaining = self.reset_timeout_seconds - elapsed
                    raise CircuitOpenError(f"circuit is open for {remaining:.3f}s more")
                self._state = "half_open"
            if self._state == "half_open":
                if self._probe_in_progress:
                    raise CircuitOpenError("circuit is half-open; probe already in progress")
                self._probe_in_progress = True

    def record_success(self) -> None:
        with self._lock:
            self._state = "closed"
            self._failure_count = 0
            self._opened_at = None
            self._probe_in_progress = False

    def record_failure(self, error: BaseException) -> None:
        if not self._counts_failure(error):
            self.record_success()
            return
        with self._lock:
            self._failure_count += 1
            if self._state == "half_open" or self._failure_count >= self.failure_threshold:
                self._state = "open"
                self._opened_at = self._clock()
            self._probe_in_progress = False

    def call[**P, R](self, operation: Callable[P, R], /, *args: P.args, **kwargs: P.kwargs) -> R:
        self.before_call()
        try:
            result = operation(*args, **kwargs)
        except Exception as error:
            self.record_failure(error)
            raise
        self.record_success()
        return result

    async def async_call[**P](
        self, operation: Callable[P, Any], /, *args: P.args, **kwargs: P.kwargs
    ) -> Any:
        self.before_call()
        try:
            result = await operation(*args, **kwargs)
        except Exception as error:
            self.record_failure(error)
            raise
        self.record_success()
        return result

    def reset(self) -> None:
        self.record_success()


def with_retry[**P, R](
    operation: Callable[P, R],
    *,
    max_attempts: int = 3,
    base_delay_seconds: float = 0.1,
    backoff_multiplier: float = 2.0,
    sleep: Callable[[float], Any] | None = None,
    retry_if: Callable[[BaseException], bool] | None = None,
    budget: CallBudget | None = None,
    breaker: CircuitBreaker | None = None,
    recorder: TraceRecorder | None = None,
    boundary: TraceBoundary = "tool",
    operation_name: str | None = None,
    operation_kind: Literal["read", "model", "write"] = "read",
) -> Callable[P, R]:
    """Wrap a sync or async callable with bounded transient-only retry."""

    if max_attempts <= 0:
        raise ValueError("max_attempts must be positive")
    if base_delay_seconds < 0:
        raise ValueError("base_delay_seconds must be nonnegative")
    if backoff_multiplier <= 0:
        raise ValueError("backoff_multiplier must be positive")

    should_retry = retry_if or (lambda error: isinstance(error, TransientCallError))
    trace_operation = operation_name or getattr(operation, "__qualname__", "call")

    def record_attempt(
        *,
        status: Literal["success", "retry", "error", "blocked", "circuit_open"],
        started_at: float,
        attempt: int,
        attributes: dict[str, Any] | None = None,
    ) -> None:
        if recorder is not None:
            recorder.record(
                boundary=boundary,
                operation=trace_operation,
                status=status,
                duration_ms=max(0.0, (time.monotonic() - started_at) * 1000),
                attempt=attempt,
                attributes=attributes,
            )

    if inspect.iscoroutinefunction(operation):

        @wraps(operation)
        async def async_wrapper(*args: P.args, **kwargs: P.kwargs) -> Any:
            for attempt in range(1, max_attempts + 1):
                started_at = time.monotonic()
                try:
                    if breaker is not None:
                        breaker.before_call()
                    if budget is not None:
                        budget.consume()
                    result = await operation(*args, **kwargs)
                except CircuitOpenError as error:
                    record_attempt(
                        status="circuit_open",
                        started_at=started_at,
                        attempt=attempt,
                        attributes={"error_type": type(error).__name__},
                    )
                    raise
                except CallBudgetExceeded as error:
                    record_attempt(
                        status="blocked",
                        started_at=started_at,
                        attempt=attempt,
                        attributes={"error_type": type(error).__name__},
                    )
                    raise
                except Exception as error:
                    if breaker is not None:
                        breaker.record_failure(error)
                    retryable = operation_kind != "write" and should_retry(error)
                    if not retryable or attempt == max_attempts:
                        record_attempt(
                            status="error",
                            started_at=started_at,
                            attempt=attempt,
                            attributes={"error_type": type(error).__name__},
                        )
                        raise
                    delay = base_delay_seconds * backoff_multiplier ** (attempt - 1)
                    record_attempt(
                        status="retry",
                        started_at=started_at,
                        attempt=attempt,
                        attributes={
                            "delay_seconds": delay,
                            "error_type": type(error).__name__,
                        },
                    )
                    if sleep is None:
                        await asyncio.sleep(delay)
                    else:
                        sleep_result = sleep(delay)
                        if inspect.isawaitable(sleep_result):
                            await sleep_result
                else:
                    if breaker is not None:
                        breaker.record_success()
                    record_attempt(status="success", started_at=started_at, attempt=attempt)
                    return result
            raise RuntimeError("unreachable retry state")

        return cast(Callable[P, R], async_wrapper)

    @wraps(operation)
    def sync_wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        for attempt in range(1, max_attempts + 1):
            started_at = time.monotonic()
            try:
                if breaker is not None:
                    breaker.before_call()
                if budget is not None:
                    budget.consume()
                result = operation(*args, **kwargs)
            except CircuitOpenError as error:
                record_attempt(
                    status="circuit_open",
                    started_at=started_at,
                    attempt=attempt,
                    attributes={"error_type": type(error).__name__},
                )
                raise
            except CallBudgetExceeded as error:
                record_attempt(
                    status="blocked",
                    started_at=started_at,
                    attempt=attempt,
                    attributes={"error_type": type(error).__name__},
                )
                raise
            except Exception as error:
                if breaker is not None:
                    breaker.record_failure(error)
                retryable = operation_kind != "write" and should_retry(error)
                if not retryable or attempt == max_attempts:
                    record_attempt(
                        status="error",
                        started_at=started_at,
                        attempt=attempt,
                        attributes={"error_type": type(error).__name__},
                    )
                    raise
                delay = base_delay_seconds * backoff_multiplier ** (attempt - 1)
                record_attempt(
                    status="retry",
                    started_at=started_at,
                    attempt=attempt,
                    attributes={
                        "delay_seconds": delay,
                        "error_type": type(error).__name__,
                    },
                )
                if sleep is None:
                    time.sleep(delay)
                else:
                    sleep(delay)
            else:
                if breaker is not None:
                    breaker.record_success()
                record_attempt(status="success", started_at=started_at, attempt=attempt)
                return result
        raise RuntimeError("unreachable retry state")

    return sync_wrapper
