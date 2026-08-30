"""Structured, JSON-serializable telemetry for RecallOps boundaries."""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from numbers import Real
from threading import Lock
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator

from recallops.agents.policies import mask_sensitive, strict_json_value

TraceBoundary = Literal["agent", "tool", "model", "middleware"]
TraceStatus = Literal["success", "retry", "error", "blocked", "circuit_open"]


def _required_identifier(name: str, value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonblank string")
    return value.strip()


def _optional_identifier(name: str, value: Any) -> str | None:
    if value is None:
        return None
    return _required_identifier(name, value)


class TraceEvent(BaseModel):
    """One observable outcome at an agent, model, tool, or policy boundary."""

    model_config = ConfigDict(frozen=True)

    event_id: str = Field(min_length=1)
    timestamp: datetime
    case_id: str | None = None
    thread_id: str | None = None
    boundary: TraceBoundary
    operation: str = Field(min_length=1)
    status: TraceStatus
    duration_ms: float = Field(ge=0)
    attempt: int | None = Field(default=None, ge=1)
    attributes: dict[str, Any] = Field(default_factory=dict)

    @field_validator("event_id", "operation", mode="before")
    @classmethod
    def identifiers_must_be_trimmed_and_nonblank(cls, value: Any, info: ValidationInfo) -> str:
        return _required_identifier(info.field_name, value)

    @field_validator("case_id", "thread_id", mode="before")
    @classmethod
    def optional_identifiers_must_be_trimmed_and_nonblank(
        cls, value: Any, info: ValidationInfo
    ) -> str | None:
        return _optional_identifier(info.field_name, value)

    @field_validator("duration_ms", mode="before")
    @classmethod
    def duration_must_be_strict_finite_real(cls, value: Any) -> float:
        if isinstance(value, bool) or not isinstance(value, Real):
            raise ValueError("duration_ms must be a finite nonnegative real number")
        duration = float(value)
        if not math.isfinite(duration) or duration < 0:
            raise ValueError("duration_ms must be a finite nonnegative real number")
        return duration

    @field_validator("attempt", mode="before")
    @classmethod
    def attempt_must_be_strict_positive_integer(cls, value: Any) -> int | None:
        if value is None:
            return None
        if type(value) is not int or value <= 0:
            raise ValueError("attempt must be a strict positive integer")
        return value

    @field_validator("timestamp")
    @classmethod
    def timestamp_must_include_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("trace timestamp must be timezone-aware")
        return value

    @field_validator("attributes", mode="before")
    @classmethod
    def attributes_must_be_strict_json(cls, value: Any) -> dict[str, Any]:
        normalized = strict_json_value(value)
        if not isinstance(normalized, dict):
            raise TypeError("trace attributes must be a JSON object")
        return normalized


class TraceRecorder:
    """Append-only in-memory recorder with deterministic IDs and masked attributes."""

    def __init__(
        self,
        *,
        case_id: str | None = None,
        thread_id: str | None = None,
        wall_clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        monotonic_clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.case_id = _optional_identifier("case_id", case_id)
        self.thread_id = _optional_identifier("thread_id", thread_id)
        self._wall_clock = wall_clock
        self._monotonic_clock = monotonic_clock
        self._events: list[TraceEvent] = []
        self._lock = Lock()

    @property
    def events(self) -> tuple[TraceEvent, ...]:
        with self._lock:
            return tuple(event.model_copy(deep=True) for event in self._events)

    def record(
        self,
        *,
        boundary: TraceBoundary,
        operation: str,
        status: TraceStatus,
        duration_ms: float,
        attempt: int | None = None,
        attributes: Mapping[str, Any] | None = None,
    ) -> TraceEvent:
        masked_attributes = mask_sensitive(dict(attributes or {}))
        with self._lock:
            event = TraceEvent(
                event_id=f"trace-{len(self._events) + 1:06d}",
                timestamp=self._wall_clock(),
                case_id=self.case_id,
                thread_id=self.thread_id,
                boundary=boundary,
                operation=operation,
                status=status,
                duration_ms=duration_ms,
                attempt=attempt,
                attributes=masked_attributes,
            )
            self._events.append(event)
            return event.model_copy(deep=True)

    @contextmanager
    def span(
        self,
        *,
        boundary: TraceBoundary,
        operation: str,
        attributes: Mapping[str, Any] | None = None,
    ) -> Iterator[None]:
        started_at = self._monotonic_clock()
        try:
            yield
        except Exception as error:
            duration_ms = max(0.0, (self._monotonic_clock() - started_at) * 1000)
            error_attributes = dict(attributes or {})
            error_attributes["error_type"] = type(error).__name__
            self.record(
                boundary=boundary,
                operation=operation,
                status="error",
                duration_ms=duration_ms,
                attributes=error_attributes,
            )
            raise
        else:
            duration_ms = max(0.0, (self._monotonic_clock() - started_at) * 1000)
            self.record(
                boundary=boundary,
                operation=operation,
                status="success",
                duration_ms=duration_ms,
                attributes=attributes,
            )

    def to_dicts(self) -> list[dict[str, Any]]:
        with self._lock:
            return [event.model_dump(mode="json") for event in self._events]
