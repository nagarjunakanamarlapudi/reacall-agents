"""One bounded completion correction; source and provider failures never retry."""

from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Annotated, Any, NamedTuple, NotRequired

from langchain.agents.middleware import AgentMiddleware, hook_config
from langchain.agents.middleware.types import AgentState, PrivateStateAttr
from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage

from recallops.agents.prompts import CHILD_COMPLETION_CORRECTION_PROMPT


class _CompletionBinding(NamedTuple):
    role: str
    request_digest: str
    source_digest: str
    required_reads: tuple[tuple[str, str], ...]
    observation_start: int
    observations_identity: int


_CHILD_BINDING: ContextVar[_CompletionBinding | None] = ContextVar(
    "recallops_child_completion_binding", default=None
)


@contextmanager
def _bind_child_completion(role, request, prerequisites):
    """Bind validated parent facts out of band, never from a child/model prompt."""
    from recallops.agents.deep_supervisor import READ_TOOL_OBSERVATIONS
    from recallops.llm.artifacts import (
        ROLE_MODELS,
        ROLES,
        LiveInvestigationRequest,
        canonical_digest,
        validate_artifact_shape,
    )

    if type(request) is not LiveInvestigationRequest or type(role) is not str or role not in ROLES:
        raise ValueError("child completion requires a typed investigation binding")
    if list(prerequisites) != list(ROLES[: ROLES.index(role)]):
        raise ValueError("child completion requires ordered validated prerequisites")
    for prior_role, raw in prerequisites.items():
        validate_artifact_shape(prior_role, raw)
        ROLE_MODELS[prior_role].model_validate(raw)
    required = []
    if role == "recall-intelligence":
        required = [("get_recall", {"recall_number": request.recall_number})]
    elif role == "product-lot-matching":
        predicate = prerequisites["recall-intelligence"]["predicate"]
        required = [
            (name, {"predicate": predicate}) for name in ("find_candidate_products", "match_lots")
        ]
    elif role == "traceability-reconciliation":
        matching = prerequisites["product-lot-matching"]
        required = [
            (name, {"lot_id": lot})
            for lot in (*matching["confirmed_lot_ids"], *matching["ambiguous_lot_ids"])
            for name in ("trace_forward", "trace_backward", "get_inventory", "reconcile_units")
        ]
    reads_token = None
    observations = READ_TOOL_OBSERVATIONS.get()
    if (
        observations is not None and type(observations) is not list
    ) or _CHILD_BINDING.get() is not None:
        raise ValueError("invalid child completion context")
    if observations is None:
        observations = []
        reads_token = READ_TOOL_OBSERVATIONS.set(observations)
    token = _CHILD_BINDING.set(
        _CompletionBinding(
            role,
            request.request_digest,
            request.source_digest,
            tuple((name, canonical_digest(payload)) for name, payload in required),
            len(observations),
            id(observations),
        )
    )
    try:
        yield
    finally:
        _CHILD_BINDING.reset(token)
        if reads_token is not None:
            READ_TOOL_OBSERVATIONS.reset(reads_token)


class ChildCompletionState(AgentState):
    child_completion_corrections: NotRequired[Annotated[int, PrivateStateAttr]]
    child_completion_binding: NotRequired[Annotated[str, PrivateStateAttr]]


@dataclass(frozen=True)
class ChildCompletionMiddleware(AgentMiddleware):
    """Each child must read and validate before returning to its parent."""

    role: str
    request_digest: str | None
    allowed_tool_names: tuple[str, ...]
    state_schema = ChildCompletionState

    def _binding(self):
        binding = _CHILD_BINDING.get()
        if (
            type(binding) is not _CompletionBinding
            or binding.role != self.role
            or binding.request_digest != self.request_digest
        ):
            raise ValueError("child completion binding mismatch")
        return binding

    def before_agent(self, state, runtime):
        from recallops.llm.artifacts import canonical_digest

        del state, runtime
        return {
            "child_completion_corrections": 0,
            "child_completion_binding": canonical_digest(self._binding()),
        }

    @hook_config(can_jump_to=["model"])
    def after_model(self, state: ChildCompletionState, runtime: Any):
        from recallops.agents.deep_supervisor import READ_TOOL_OBSERVATIONS, ReadToolObservation
        from recallops.llm.artifacts import ROLE_MODELS, canonical_digest, validate_artifact_shape

        del runtime
        binding = self._binding()
        corrections = state.get("child_completion_corrections")
        if (
            type(corrections) is not int
            or corrections not in (0, 1)
            or state.get("child_completion_binding") != canonical_digest(binding)
        ):
            raise ValueError("invalid private child completion state")
        observations = READ_TOOL_OBSERVATIONS.get()
        if type(observations) is not list or id(observations) != binding.observations_identity:
            raise ValueError("child completion observation binding mismatch")
        reads = observations[binding.observation_start :]
        if any(
            type(row) is not ReadToolObservation
            or row.status != "completed"
            or row.source_digest != binding.source_digest
            for row in reads
        ):
            raise ValueError("child completion source read failed")
        messages = state.get("messages", [])
        position = next(
            (i for i in range(len(messages) - 1, -1, -1) if isinstance(messages[i], AIMessage)),
            None,
        )
        if position is None:
            raise ValueError("child completion requires a model response")
        last = messages[position]
        response_name = ROLE_MODELS[self.role].__name__
        allowed = {*self.allowed_tool_names, "ls", "read_file", response_name}
        if last.invalid_tool_calls or any(call["name"] not in allowed for call in last.tool_calls):
            raise ValueError("child attempted an invalid or unauthorized tool")
        identities = [
            call["id"]
            for message in messages
            if isinstance(message, AIMessage)
            for call in message.tool_calls
        ]
        if any(type(identity) is not str or not identity for identity in identities) or len(
            identities
        ) != len(set(identities)):
            raise ValueError("child tool identities must be unique")
        if last.tool_calls and all(call["name"] != response_name for call in last.tool_calls):
            return None  # Normal evidence-read turn, not a completion attempt.
        valid = False
        try:
            raw = state.get("structured_response")
            if len(last.tool_calls) != 1 or last.tool_calls[0]["name"] != response_name:
                raise ValueError("child structured response missing")
            validate_artifact_shape(self.role, raw)
            if canonical_digest(raw) != canonical_digest(last.tool_calls[0]["args"]):
                raise ValueError("child structured response differs from original arguments")
            ROLE_MODELS[self.role].model_validate(raw)
            actual = Counter((row.name, row.input_digest) for row in reads)
            valid = actual >= Counter(binding.required_reads)
        except ValueError:
            pass  # Never reflect rejected values or validator prose into instructions.
        if valid:
            return None
        if corrections == 1:
            raise ValueError("child completion contract failed after one correction")
        rejected = messages[position:]
        if any(type(message.id) is not str or not message.id for message in rejected):
            raise ValueError("child completion messages require exact identities")
        return {
            "child_completion_corrections": 1,
            "structured_response": None,
            "messages": [
                *(RemoveMessage(id=message.id) for message in rejected),
                HumanMessage(content=CHILD_COMPLETION_CORRECTION_PROMPT),
            ],
            "jump_to": "model",
        }
