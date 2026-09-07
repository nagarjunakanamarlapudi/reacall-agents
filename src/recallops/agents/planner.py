"""Deterministic, write_todos-compatible planning for recall investigations."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field, model_validator


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
    depends_on: list[str] = Field(default_factory=list, max_length=3)
    status: Literal["pending", "in_progress", "completed"] = "pending"


class InvestigationPlan(BaseModel):
    case_id: str = Field(min_length=1)
    objective: str = Field(min_length=1)
    todos: list[InvestigationTodo] = Field(min_length=4, max_length=4)

    @model_validator(mode="after")
    def validate_dispatch_contract(self) -> InvestigationPlan:
        """Reject planner output that cannot safely drive the bounded dispatcher."""
        todo_ids = [todo.todo_id for todo in self.todos]
        if any(type(todo_id) is not str or not todo_id.strip() for todo_id in todo_ids):
            raise ValueError("todo IDs must be nonblank strings")
        if len(todo_ids) != len(set(todo_ids)):
            raise ValueError("todo IDs must be unique")
        roles = [todo.specialist for todo in self.todos]
        if len(roles) != len(set(roles)):
            raise ValueError("specialist roles must be unique")
        if set(roles) != set(SpecialistName):
            raise ValueError("plan must contain every required specialist exactly once")
        if any(todo.status != "pending" for todo in self.todos):
            raise ValueError("all specialist todos must begin pending")

        contract = {
            specialist: (task, criteria) for specialist, task, criteria, _ in _TODO_BLUEPRINTS
        }
        for todo in self.todos:
            if (todo.task, todo.completion_criteria) != contract[todo.specialist]:
                raise ValueError(f"unsupported task contract for {todo.specialist.value}")

        known = set(todo_ids)
        dependencies = {todo.todo_id: list(todo.depends_on) for todo in self.todos}
        for todo in self.todos:
            if len(todo.depends_on) != len(set(todo.depends_on)):
                raise ValueError("todo dependencies must be unique")
            if todo.todo_id in todo.depends_on:
                raise ValueError("todo dependency cycle detected")
            unknown = set(todo.depends_on) - known
            if unknown:
                raise ValueError(f"todo depends on unknown todo: {sorted(unknown)}")

        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(todo_id: str) -> None:
            if todo_id in visiting:
                raise ValueError("todo dependency cycle detected")
            if todo_id in visited:
                return
            visiting.add(todo_id)
            for dependency in dependencies[todo_id]:
                visit(dependency)
            visiting.remove(todo_id)
            visited.add(todo_id)

        for todo_id in todo_ids:
            visit(todo_id)

        role_by_id = {todo.todo_id: todo.specialist for todo in self.todos}
        expected_dependency_roles = {
            SpecialistName.RECALL_INTELLIGENCE: set(),
            SpecialistName.PRODUCT_LOT_MATCHING: set(),
            SpecialistName.TRACEABILITY_RECONCILIATION: {SpecialistName.PRODUCT_LOT_MATCHING},
            SpecialistName.CONTAINMENT_COMMUNICATIONS: {SpecialistName.TRACEABILITY_RECONCILIATION},
        }
        for todo in self.todos:
            dependency_roles = {role_by_id[todo_id] for todo_id in todo.depends_on}
            if dependency_roles != expected_dependency_roles[todo.specialist]:
                raise ValueError(f"invalid dependencies for {todo.specialist.value}")

        positions = {todo_id: index for index, todo_id in enumerate(todo_ids)}
        if any(
            positions[dependency] >= positions[todo.todo_id]
            for todo in self.todos
            for dependency in todo.depends_on
        ):
            raise ValueError("todo dependency must precede its dependent todo")
        return self

    def write_todos_payload(self) -> dict[str, list[dict[str, str]]]:
        """Return the exact argument schema consumed by the real write_todos tool."""
        return {
            "todos": [
                {
                    "content": (
                        f"[{todo.specialist}] {todo.task} Completion: {todo.completion_criteria}"
                    ),
                    "status": todo.status,
                }
                for todo in self.todos
            ]
        }


_TODO_BLUEPRINTS = (
    (
        SpecialistName.RECALL_INTELLIGENCE,
        "Extract the authoritative recall predicate and source citations.",
        "Product, UPC, plant, date window, geography, hazard, and provenance are cited.",
        (),
    ),
    (
        SpecialistName.PRODUCT_LOT_MATCHING,
        "Classify internal products and lots against the recall predicate.",
        "Every candidate is exact, probable, ambiguous, or rejected with field rationale.",
        (),
    ),
    (
        SpecialistName.TRACEABILITY_RECONCILIATION,
        "Trace affected lots and reconcile units by facility.",
        "Lineage, facility coverage, component evidence, and quantity gaps are explicit.",
        ("todo-2",),
    ),
    (
        SpecialistName.CONTAINMENT_COMMUNICATIONS,
        "Draft evidence-cited containment actions and communications for review.",
        "Drafts cite known evidence, exclude ambiguous holds, and execute no writes.",
        ("todo-3",),
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
            depends_on=list(depends_on),
        )
        for index, (specialist, task, criteria, depends_on) in enumerate(_TODO_BLUEPRINTS, start=1)
    ]
    return InvestigationPlan(case_id=case_id, objective=objective, todos=todos)
