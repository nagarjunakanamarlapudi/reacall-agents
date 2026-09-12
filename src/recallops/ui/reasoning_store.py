"""Advisory summaries stored separately from authority-bearing graph checkpoints."""

import sqlite3
from contextlib import contextmanager
from pathlib import Path

from recallops.llm.live_reasoning import LiveReasoningSummary


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
        with self._connection() as connection:
            row = connection.execute(
                "SELECT summary_json FROM reasoning_summaries WHERE thread_id = ?", (thread_id,)
            ).fetchone()
        if row is None:
            return None
        if row[0] is None:
            raise RuntimeError(
                "Live reasoning is in progress or was interrupted before its summary was saved. "
                "No live success is claimed. Open a new case to start a new investigation."
            )
        return LiveReasoningSummary.model_validate_json(row[0])

    def finish(self, thread_id: str, summary: LiveReasoningSummary) -> None:
        with self._connection() as connection:
            connection.execute(
                "UPDATE reasoning_summaries SET summary_json = ? WHERE thread_id = ? "
                "AND summary_json IS NULL",
                (summary.model_dump_json(), thread_id),
            )
