"""Live orchestration contract exercised through a real compiled Deep Agents graph."""

import importlib.util
from typing import Any

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

from recallops.llm import LLMSettings

ROLES = [
    "recall-intelligence",
    "product-lot-matching",
    "traceability-reconciliation",
    "containment-communications",
]
PRIVATE = "PRIVATE_PAYLOAD_DO_NOT_RETAIN"


class ScriptedModel(GenericFakeChatModel):
    def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
        return self


def call(name: str, args: dict[str, Any], index: int) -> AIMessage:
    return AIMessage(
        content=PRIVATE,
        tool_calls=[{"name": name, "args": args, "id": f"call-{index}"}],
        usage_metadata={"input_tokens": 3, "output_tokens": 2, "total_tokens": 5},
    )


def script() -> list[AIMessage]:
    from recallops.agents.specialists import investigate_recall
    from recallops.data.loaders import load_recall_snapshot

    responses = [
        ("RecallIntelligence", investigate_recall(load_recall_snapshot()).model_dump(mode="json")),
        (
            "ProductLotAssessment",
            {"decisions": [], "confirmed_lot_ids": [], "ambiguous_lot_ids": []},
        ),
        (
            "TraceabilityAssessment",
            {
                "lot_ids": [],
                "affected_facilities": [],
                "coverage": [],
                "forward_traces": {},
                "backward_traces": {},
                "reconciliations": [],
                "evidence_ids": [],
                "evidence_gaps": [],
            },
        ),
        (
            "ContainmentProposal",
            {
                "proposed_actions": [],
                "communication_drafts": [],
                "all_cited_evidence_ids": [],
                "executed": False,
            },
        ),
    ]
    messages = [
        call(
            "write_todos",
            {"todos": [{"content": f"[{role}] {PRIVATE}", "status": "pending"} for role in ROLES]},
            0,
        )
    ]
    for index, (role, (response_name, payload)) in enumerate(zip(ROLES, responses, strict=True)):
        messages.append(call("task", {"description": PRIVATE, "subagent_type": role}, index + 1))
        if index == 0:
            messages.append(call("search_recalls", {"query": "H-1230-2026"}, 10))
        messages.append(call(response_name, payload, index + 20))
    messages.append(
        call(
            "SupervisorResponse",
            {
                "outcome": "human_review",
                "evidence_count": 1,
                "confirmed_lot_count": 0,
                "ambiguous_lot_count": 0,
                "proposed_action_count": 0,
                "executed": False,
            },
            40,
        )
    )
    return messages


def live_module():
    assert importlib.util.find_spec("recallops.llm.live_reasoning") is not None, (
        "Live reasoning service has not been implemented"
    )
    from recallops.llm import live_reasoning

    return live_reasoning


def service(live, monkeypatch, messages):
    monkeypatch.setattr(
        live, "build_chat_model", lambda settings: ScriptedModel(messages=iter(messages))
    )
    return live.LiveReasoningService(LLMSettings(mode="openai", model="test-model"))


@pytest.mark.parametrize("transport", ["direct", "stdio"])
async def test_real_graph_projects_ordered_plan_reads_and_usage(monkeypatch, transport):
    """Catches skipped invocation, missing subagent usage, or retaining private payloads."""
    live = live_module()
    runner = service(live, monkeypatch, script())
    summary = await runner.run(PRIVATE, transport=transport)
    assert summary.status == "completed", summary
    assert summary.provider == "openai"
    assert summary.model == "test-model"
    assert summary.plan == ROLES
    assert summary.specialist_sequence == ROLES
    assert summary.read_tool_sequence == ["search_recalls"]
    assert summary.response_summary.outcome == "human_review"
    assert summary.response_summary.executed is False
    assert summary.input_tokens == 33
    assert summary.output_tokens == 22
    assert summary.total_tokens == 55
    assert summary.duration_ms > 0
    assert summary.fallback_used is False
    assert summary.error_category is None
    assert PRIVATE not in summary.model_dump_json()
    assert PRIVATE not in repr(vars(runner))
    assert all(event.status == "completed" and event.duration_ms >= 0 for event in summary.events)
    assert len([event for event in summary.events if event.kind == "model"]) == 11
    assert len([event for event in summary.events if event.name == "search_recalls"]) == 1


@pytest.mark.parametrize(
    "fault", ["no_plan", "missing_role", "wrong_order", "duplicate", "no_structure"]
)
async def test_invalid_completion_returns_failed_summary(monkeypatch, fault):
    """Catches accepting incomplete, unordered, or unstructured supervisor output."""
    live = live_module()
    messages = script()
    if fault == "no_plan":
        messages.pop(0)
    elif fault == "missing_role":
        del messages[-3:-1]
    elif fault == "wrong_order":
        messages[1].tool_calls[0]["args"]["subagent_type"] = ROLES[1]
    elif fault == "duplicate":
        messages[4].tool_calls[0]["args"]["subagent_type"] = ROLES[0]
    else:
        messages[-1] = AIMessage(content=PRIVATE)
    summary = await service(live, monkeypatch, messages).run(PRIVATE, transport="direct")
    assert summary.status == "failed"
    assert summary.error_category in {"invalid_response", "budget_exceeded"}
    assert summary.response_summary is None
    assert summary.fallback_used is False
    assert PRIVATE not in summary.model_dump_json()


async def test_provider_failure_is_sanitized_and_service_retains_no_model(monkeypatch):
    """Catches forwarding provider errors or keeping credentials in service state."""
    live = live_module()

    class AuthenticationError(Exception):
        pass

    def fail(settings):
        raise AuthenticationError(PRIVATE)

    monkeypatch.setattr(live, "build_chat_model", fail)
    runner = live.LiveReasoningService(LLMSettings(mode="openai", model="test-model"))
    summary = await runner.run(PRIVATE, transport="direct")
    assert summary.status == "failed"
    assert summary.error_category == "authentication"
    assert summary.events == []
    assert PRIVATE not in summary.model_dump_json() + repr(vars(runner))


async def test_read_failure_is_recorded_without_payload(monkeypatch):
    """Catches reporting a failed read as completed or retaining its exception text."""
    import recallops.agents.deep_supervisor as supervisor

    live = live_module()

    async def fail(*args, **kwargs):
        raise RuntimeError(PRIVATE)

    monkeypatch.setattr(supervisor, "_invoke_trusted_read", fail)
    summary = await service(live, monkeypatch, script()).run(PRIVATE, transport="direct")
    assert summary.status == "failed"
    assert summary.read_tool_sequence == ["search_recalls"]
    assert any(
        event.name == "search_recalls" and event.status == "failed" for event in summary.events
    )
    assert PRIVATE not in summary.model_dump_json()
    assert summary.plan == ROLES


async def test_budget_exhaustion_is_bounded_and_preserves_completed_work(monkeypatch):
    """Catches an unbounded model loop or loss of already completed role observations."""
    from itertools import chain, repeat

    live = live_module()
    messages = script()
    runner = service(live, monkeypatch, chain(messages[:-1], repeat(call("ls", {"path": "/"}, 99))))
    summary = await runner.run(PRIVATE, transport="direct")
    assert summary.status == "failed"
    assert summary.error_category == "budget_exceeded"
    assert summary.plan == ROLES
    assert summary.specialist_sequence == ROLES
    assert 11 < len([event for event in summary.events if event.kind == "model"]) < 100


async def test_model_failure_after_start_is_sanitized(monkeypatch):
    """Catches leaked model error payloads and loss of measured failed-call latency."""
    live = live_module()

    class RateLimitError(Exception):
        pass

    class FailingModel(ScriptedModel):
        def _generate(self, *args, **kwargs):
            raise RateLimitError(PRIVATE)

    monkeypatch.setattr(live, "build_chat_model", lambda settings: FailingModel(messages=iter([])))
    runner = live.LiveReasoningService(LLMSettings(mode="openai", model="test-model"))
    summary = await runner.run(PRIVATE, transport="direct")
    assert summary.status == "failed"
    assert summary.error_category == "rate_limit"
    assert len(summary.events) == 1
    assert summary.events[0].status == "failed"
    assert summary.total_tokens is None
    assert PRIVATE not in summary.model_dump_json()


async def test_live_invocation_disables_external_tracing_and_global_cache(monkeypatch):
    """Catches raw prompts or tool payloads escaping into default tracing or model caches."""
    from langchain_core.caches import InMemoryCache
    from langchain_core.globals import get_llm_cache, set_llm_cache
    from langsmith.run_helpers import get_tracing_context

    live = live_module()

    class ObservingModel(ScriptedModel):
        def _generate(self, *args, **kwargs):
            assert get_tracing_context()["enabled"] is False
            assert self.cache is False
            return super()._generate(*args, **kwargs)

    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setattr(
        live, "build_chat_model", lambda settings: ObservingModel(messages=iter(script()))
    )
    original_cache = get_llm_cache()
    cache = InMemoryCache()
    set_llm_cache(cache)
    try:
        runner = live.LiveReasoningService(LLMSettings(mode="openai", model="test-model"))
        summary = await runner.run(PRIVATE, transport="direct")
        assert summary.status == "completed"
        assert cache._cache == {}
    finally:
        set_llm_cache(original_cache)


async def test_malformed_structured_response_is_classified_without_raw_values(monkeypatch):
    """Catches leaking schema-validation input or calling malformed output a provider outage."""
    live = live_module()
    messages = script()
    messages[-1].tool_calls[0]["args"]["evidence_count"] = PRIVATE
    summary = await service(live, monkeypatch, messages).run(PRIVATE, transport="direct")
    assert summary.status == "failed"
    assert summary.error_category == "invalid_response"
    assert summary.response_summary is None
    assert PRIVATE not in summary.model_dump_json()


async def test_concurrent_runs_keep_read_observations_isolated(monkeypatch):
    """Catches cross-request event retention through the sealed read observation context."""
    import asyncio

    live = live_module()
    messages_without_read = script()
    del messages_without_read[2]
    scripts = iter([script(), messages_without_read])
    monkeypatch.setattr(
        live, "build_chat_model", lambda settings: ScriptedModel(messages=iter(next(scripts)))
    )
    runner = live.LiveReasoningService(LLMSettings(mode="openai", model="test-model"))
    first, second = await asyncio.gather(
        runner.run(PRIVATE, transport="direct"),
        runner.run(PRIVATE, transport="direct"),
    )
    assert first.status == second.status == "completed"
    assert first.read_tool_sequence == ["search_recalls"]
    assert second.read_tool_sequence == []
    assert first.total_tokens == 55
    assert second.total_tokens == 50
    assert live.READ_TOOL_OBSERVATIONS.get() is None


async def test_unstructured_specialist_response_cannot_count_as_completion(monkeypatch):
    """Catches accepting raw specialist prose when the framework returns no typed artifact."""
    live = live_module()
    messages = script()
    messages[3] = AIMessage(content=PRIVATE)
    summary = await service(live, monkeypatch, messages).run(PRIVATE, transport="direct")
    assert summary.status == "failed"
    assert summary.error_category == "invalid_response"
    assert summary.response_summary is None
    assert PRIVATE not in summary.model_dump_json()
