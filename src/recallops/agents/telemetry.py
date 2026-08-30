"""Structured, JSON-serializable telemetry for RecallOps boundaries."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from recallops.agents.policies import mask_sensitive

TraceBoundary = Literal["agent", "tool", "model", "middleware"]
TraceStatus = Literal["success", "retry", "error", "blocked", "circuit_open"]


class TraceEvent(BaseModel):
    """One observable outcome at an agent, model, tool, or policy boundary."""

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

    @field_validator("timestamp")
    @classmethod
    def timestamp_must_include_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("trace timestamp must be timezone-aware")
        return value


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
        self.case_id = case_id
        self.thread_id = thread_id
        self._wall_clock = wall_clock
        self._monotonic_clock = monotonic_clock
        self._events: list[TraceEvent] = []

    @property
    def events(self) -> tuple[TraceEvent, ...]:
        return tuple(self._events)

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
        return event

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
        return [event.model_dump(mode="json") for event in self._events]
