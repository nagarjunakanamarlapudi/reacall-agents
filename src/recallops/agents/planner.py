"""Deterministic, write_todos-compatible planning for recall investigations."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field


class SpecialistName(StrEnum):
    RECALL_INTELLIGENCE = "recall-intelligence"
    PRODUCT_LOT_MATCHING = "product-lot-matching"
    TRACEABILITY_RECONCILIATION = "traceability-reconciliation"
    CONTAINMENT_COMMUNICATIONS = "containment-communications"


class InvestigationTodo(BaseModel):
    todo_id: str
    task: str = Field(min_length=1)
    specialist: SpecialistName
    completion_criteria: str = Field(min_length=1)
    status: Literal["pending", "in_progress", "completed"] = "pending"


class InvestigationPlan(BaseModel):
    case_id: str = Field(min_length=1)
    objective: str = Field(min_length=1)
    todos: list[InvestigationTodo] = Field(min_length=4, max_length=4)

    def write_todos_payload(self) -> list[dict[str, str]]:
        """Return the same JSON-safe shape consumed by a write_todos planning tool."""
        return [todo.model_dump(mode="json") for todo in self.todos]


_TODO_BLUEPRINTS = (
    (
        SpecialistName.RECALL_INTELLIGENCE,
        "Extract the authoritative recall predicate and source citations.",
        "Product, UPC, plant, date window, geography, hazard, and provenance are cited.",
    ),
    (
        SpecialistName.PRODUCT_LOT_MATCHING,
        "Classify internal products and lots against the recall predicate.",
        "Every candidate is exact, probable, ambiguous, or rejected with field rationale.",
    ),
    (
        SpecialistName.TRACEABILITY_RECONCILIATION,
        "Trace affected lots and reconcile units by facility.",
        "Lineage, facility coverage, component evidence, and quantity gaps are explicit.",
    ),
    (
        SpecialistName.CONTAINMENT_COMMUNICATIONS,
        "Draft evidence-cited containment actions and communications for review.",
        "Drafts cite known evidence, exclude ambiguous holds, and execute no writes.",
    ),
)


def plan_investigation(*, case_id: str, question: str, max_todos: int = 4) -> InvestigationPlan:
    """Build the fixed, bounded plan shared by offline and live-model modes."""
    if max_todos != len(_TODO_BLUEPRINTS):
        raise ValueError("RecallOps requires four specialist todos")
    objective = question.strip() or "Investigate the recall and prepare safe containment review."
    todos = [
        InvestigationTodo(
            todo_id=f"todo-{index}",
            specialist=specialist,
            task=task,
            completion_criteria=criteria,
        )
        for index, (specialist, task, criteria) in enumerate(_TODO_BLUEPRINTS, start=1)
    ]
    return InvestigationPlan(case_id=case_id, objective=objective, todos=todos)
