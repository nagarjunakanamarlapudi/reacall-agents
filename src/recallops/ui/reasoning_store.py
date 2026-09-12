"""Advisory summaries stored separately from authority-bearing graph checkpoints."""

import math
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from recallops.agents.deep_supervisor import specialist_catalog
from recallops.llm.live_reasoning import LiveReasoningSummary

_ROLES = frozenset(item.name for item in specialist_catalog())
_READ_TOOLS = frozenset(name for item in specialist_catalog() for name in item.allowed_tool_names)
_EVENT_TOOLS = _READ_TOOLS | {"write_todos", "task", "ls", "read_file"}
_MODEL_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_CREDENTIAL_MARKER = re.compile(
    r"(?:sk|pk)[-_]|bearer|api[-_]?key|password|secret|credential|token|authorization|"
    r"AKIA|ASIA|AIza|gh[pousr]_|github_pat_|xox[baprs]-|eyJ",
    re.IGNORECASE,
)


class ReasoningTelemetryUnavailable(RuntimeError):
    """A constant safe error; never include corrupt rows or database exception text."""

    def __init__(self) -> None:
        super().__init__("Reasoning telemetry unavailable.")


class ReasoningInProgress(RuntimeError):
    """An unfinished claim is distinct from missing/corrupt advisory telemetry."""


def _validated_summary(payload: str) -> LiveReasoningSummary:
    summary = LiveReasoningSummary.model_validate_json(payload, strict=True)
    if not _MODEL_IDENTIFIER.fullmatch(summary.model) or _CREDENTIAL_MARKER.search(summary.model):
        raise ReasoningTelemetryUnavailable()
    if (
        set(summary.plan) - _ROLES
        or set(summary.specialist_sequence) - _ROLES
        or set(summary.read_tool_sequence) - _READ_TOOLS
        or not math.isfinite(summary.duration_ms)
        or any(
            value is not None and value < 0
            for value in (summary.input_tokens, summary.output_tokens, summary.total_tokens)
        )
    ):
        raise ReasoningTelemetryUnavailable()
    for event in summary.events:
        if (
            not math.isfinite(event.duration_ms)
            or (event.kind == "model" and event.name != summary.model)
            or (event.kind == "tool" and event.name not in _EVENT_TOOLS)
        ):
            raise ReasoningTelemetryUnavailable()
    return summary


class ReasoningStore:
    """Claim each durable thread once, retaining only the public summary schema.

    A null summary is an unfinished claim. Never retry that provider call implicitly:
    a process interruption cannot prove whether the provider already completed it.
    """

    def __init__(self, path: Path) -> None:
        self.path = path

    @contextmanager
    def _connection(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path)
        try:
            with connection:
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS reasoning_summaries "
                    "(thread_id TEXT PRIMARY KEY, summary_json TEXT)"
                )
                yield connection
        finally:
            connection.close()

    def claim(self, thread_id: str) -> bool:
        with self._connection() as connection:
            return (
                connection.execute(
                    "INSERT OR IGNORE INTO reasoning_summaries (thread_id) VALUES (?)", (thread_id,)
                ).rowcount
                == 1
            )

    def get(self, thread_id: str) -> LiveReasoningSummary | None:
        try:
            with self._connection() as connection:
                row = connection.execute(
                    "SELECT summary_json FROM reasoning_summaries WHERE thread_id = ?", (thread_id,)
                ).fetchone()
            if row is None:
                return None
            if row[0] is None:
                raise ReasoningInProgress(
                    "Live reasoning is in progress or was interrupted before its summary was saved. "
                    "No live success is claimed. Open a new case to start a new investigation."
                )
            return _validated_summary(row[0])
        except (ValueError, OSError, sqlite3.Error):
            raise ReasoningTelemetryUnavailable() from None

    def finish(self, thread_id: str, summary: LiveReasoningSummary) -> None:
        with self._connection() as connection:
            connection.execute(
                "UPDATE reasoning_summaries SET summary_json = ? WHERE thread_id = ? "
                "AND summary_json IS NULL",
                (summary.model_dump_json(), thread_id),
            )
