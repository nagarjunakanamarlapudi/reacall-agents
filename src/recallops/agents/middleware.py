"""Composable middleware for bounded and recoverable calls."""

from __future__ import annotations

import asyncio
import inspect
import math
import time
from collections.abc import Callable, Coroutine
from functools import wraps
from numbers import Integral, Real
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


def _positive_integer(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, Integral) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return int(value)


def _finite_number(
    name: str,
    value: object,
    *,
    minimum: float,
    exclusive: bool = False,
) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"{name} must be a finite number")
    numeric = float(value)
    if not math.isfinite(numeric) or (numeric <= minimum if exclusive else numeric < minimum):
        comparator = "greater than" if exclusive else "at least"
        raise ValueError(f"{name} must be a finite number {comparator} {minimum}")
    return numeric


def _is_async_callable(operation: Callable[..., Any]) -> bool:
    return inspect.iscoroutinefunction(operation) or inspect.iscoroutinefunction(
        getattr(operation, "__call__", None)
    )


def _close_awaitable(awaitable: Any) -> None:
    close = getattr(awaitable, "close", None)
    if callable(close):
        close()
        return
    cancel = getattr(awaitable, "cancel", None)
    if callable(cancel):
        cancel()


class CallBudget:
    """Count actual call attempts and fail closed at a fixed limit."""

    def __init__(self, limit: int) -> None:
        self.limit = _positive_integer("call budget limit", limit)
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
        self.failure_threshold = _positive_integer("failure_threshold", failure_threshold)
        self.reset_timeout_seconds = _finite_number(
            "reset_timeout_seconds", reset_timeout_seconds, minimum=0
        )
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
            with self._lock:
                if self._state == "half_open":
                    self._state = "open"
                    self._opened_at = self._clock()
                self._probe_in_progress = False
            return
        with self._lock:
            self._failure_count += 1
            if self._state == "half_open" or self._failure_count >= self.failure_threshold:
                self._state = "open"
                self._opened_at = self._clock()
            self._probe_in_progress = False

    def record_aborted(self) -> None:
        """Release an interrupted probe without claiming dependency recovery."""

        with self._lock:
            if self._state == "half_open":
                self._state = "open"
                self._opened_at = self._clock()
            self._probe_in_progress = False

    def defer_awaitable_probe(self) -> None:
        """Release a probe until a returned awaitable actually starts executing."""

        with self._lock:
            if self._state == "half_open":
                self._state = "open"
                self._probe_in_progress = False

    def call[**P, R](self, operation: Callable[P, R], /, *args: P.args, **kwargs: P.kwargs) -> R:
        if _is_async_callable(operation):

            async def invoke_async_operation() -> Any:
                self.before_call()
                try:
                    resolved = await operation(*args, **kwargs)
                except Exception as error:
                    self.record_failure(error)
                    raise
                except BaseException:
                    self.record_aborted()
                    raise
                self.record_success()
                return resolved

            return cast(R, invoke_async_operation())

        self.before_call()
        try:
            result = operation(*args, **kwargs)
        except Exception as error:
            self.record_failure(error)
            raise
        except BaseException:
            self.record_aborted()
            raise
        if inspect.isawaitable(result):
            self.defer_awaitable_probe()

            async def finish_awaitable() -> Any:
                try:
                    self.before_call()
                except BaseException:
                    _close_awaitable(result)
                    raise
                try:
                    resolved = await result
                except Exception as error:
                    self.record_failure(error)
                    raise
                except BaseException:
                    self.record_aborted()
                    raise
                self.record_success()
                return resolved

            return cast(
                R,
                _DeferredCoroutine(
                    finish_awaitable,
                    close_before_start=lambda: _close_awaitable(result),
                ),
            )
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
        except BaseException:
            self.record_aborted()
            raise
        self.record_success()
        return result

    def reset(self) -> None:
        self.record_success()


class _DeferredCoroutine[R](Coroutine[Any, Any, R]):
    """Coroutine that performs no acquisition until its first execution step."""

    def __init__(
        self,
        runner_factory: Callable[[], Coroutine[Any, Any, R]],
        *,
        close_before_start: Callable[[], None] | None = None,
    ) -> None:
        self._runner_factory = runner_factory
        self._close_before_start = close_before_start
        self._runner: Coroutine[Any, Any, R] | None = None
        self._closed = False

    def __await__(self) -> _DeferredCoroutine[R]:
        return self

    def __iter__(self) -> _DeferredCoroutine[R]:
        return self

    def __next__(self) -> Any:
        return self.send(None)

    def _ensure_runner(self) -> Coroutine[Any, Any, R]:
        if self._closed:
            raise RuntimeError("cannot reuse already awaited coroutine")
        if self._runner is None:
            self._runner = self._runner_factory()
        return self._runner

    def send(self, value: Any) -> Any:
        return self._ensure_runner().send(value)

    def throw(self, typ: Any, val: Any = None, tb: Any = None) -> Any:
        if self._runner is not None:
            return self._runner.throw(typ, val, tb)
        self.close()
        if isinstance(typ, BaseException):
            error = typ
        elif isinstance(val, BaseException):
            error = val
        elif val is None:
            error = typ()
        else:
            error = typ(val)
        if tb is not None:
            raise error.with_traceback(tb)
        raise error

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._runner is not None:
            self._runner.close()
        elif self._close_before_start is not None:
            self._close_before_start()


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

    max_attempts = _positive_integer("max_attempts", max_attempts)
    base_delay_seconds = _finite_number("base_delay_seconds", base_delay_seconds, minimum=0)
    backoff_multiplier = _finite_number(
        "backoff_multiplier", backoff_multiplier, minimum=0, exclusive=True
    )
    if operation_kind not in {"read", "model", "write"}:
        raise ValueError("operation_kind must be 'read', 'model', or 'write'")

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

    if _is_async_callable(operation):

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
                    if breaker is not None:
                        breaker.record_aborted()
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
                except BaseException as error:
                    if breaker is not None:
                        breaker.record_aborted()
                    record_attempt(
                        status="error",
                        started_at=started_at,
                        attempt=attempt,
                        attributes={"error_type": type(error).__name__},
                    )
                    raise
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
                if breaker is not None:
                    breaker.record_aborted()
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
            except BaseException as error:
                if breaker is not None:
                    breaker.record_aborted()
                record_attempt(
                    status="error",
                    started_at=started_at,
                    attempt=attempt,
                    attributes={"error_type": type(error).__name__},
                )
                raise
            else:
                if inspect.isawaitable(result):
                    if breaker is not None:
                        breaker.defer_awaitable_probe()

                    async def continue_as_async(first_result: Any) -> Any:
                        current_result = first_result
                        current_started_at = started_at
                        for current_attempt in range(attempt, max_attempts + 1):
                            try:
                                if breaker is not None:
                                    breaker.before_call()
                                if current_attempt != attempt:
                                    current_started_at = time.monotonic()
                                    if budget is not None:
                                        budget.consume()
                                    current_result = operation(*args, **kwargs)
                                if inspect.isawaitable(current_result):
                                    resolved = await current_result
                                else:
                                    resolved = current_result
                            except CircuitOpenError as error:
                                if current_attempt == attempt:
                                    _close_awaitable(current_result)
                                record_attempt(
                                    status="circuit_open",
                                    started_at=current_started_at,
                                    attempt=current_attempt,
                                    attributes={"error_type": type(error).__name__},
                                )
                                raise
                            except CallBudgetExceeded as error:
                                if breaker is not None:
                                    breaker.record_aborted()
                                record_attempt(
                                    status="blocked",
                                    started_at=current_started_at,
                                    attempt=current_attempt,
                                    attributes={"error_type": type(error).__name__},
                                )
                                raise
                            except Exception as error:
                                if breaker is not None:
                                    breaker.record_failure(error)
                                retryable = operation_kind != "write" and should_retry(error)
                                if not retryable or current_attempt == max_attempts:
                                    record_attempt(
                                        status="error",
                                        started_at=current_started_at,
                                        attempt=current_attempt,
                                        attributes={"error_type": type(error).__name__},
                                    )
                                    raise
                                delay = base_delay_seconds * backoff_multiplier ** (
                                    current_attempt - 1
                                )
                                record_attempt(
                                    status="retry",
                                    started_at=current_started_at,
                                    attempt=current_attempt,
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
                            except BaseException as error:
                                if breaker is not None:
                                    breaker.record_aborted()
                                record_attempt(
                                    status="error",
                                    started_at=current_started_at,
                                    attempt=current_attempt,
                                    attributes={"error_type": type(error).__name__},
                                )
                                raise
                            else:
                                if breaker is not None:
                                    breaker.record_success()
                                record_attempt(
                                    status="success",
                                    started_at=current_started_at,
                                    attempt=current_attempt,
                                )
                                return resolved
                        raise RuntimeError("unreachable retry state")

                    return cast(
                        R,
                        _DeferredCoroutine(
                            lambda: continue_as_async(result),
                            close_before_start=lambda: _close_awaitable(result),
                        ),
                    )
                if breaker is not None:
                    breaker.record_success()
                record_attempt(status="success", started_at=started_at, attempt=attempt)
                return result
        raise RuntimeError("unreachable retry state")

    return sync_wrapper
