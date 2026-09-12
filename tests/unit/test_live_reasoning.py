"""Source-backed orchestration, privacy, failure, cancellation and telemetry."""

import asyncio
from itertools import chain, repeat

import pytest
from langchain_core.messages import AIMessage

from recallops.llm import LLMSettings
from recallops.llm import live_reasoning as live

PRIVATE = "PRIVATE_LIVE_MODEL_CANARY"


def runner(monkeypatch, model):
    monkeypatch.setattr(live, "build_chat_model", lambda settings: model)
    return live.LiveReasoningService(LLMSettings(mode="openai", model="test-model"))


@pytest.mark.parametrize(
    "settings", [object(), LLMSettings(mode="openai", model="sk-proj-private-canary")]
)
def test_service_rejects_unsafe_settings_before_any_checkpoint_or_model(settings):
    with pytest.raises((TypeError, ValueError)) as error:
        live.LiveReasoningService(settings)
    assert "private-canary" not in str(error.value)


async def test_service_detaches_and_forwards_explicit_reasoning_effort(
    monkeypatch, live_case, live_request
):
    settings = LLMSettings(mode="openai", model="test-model", reasoning_effort="high")
    service = live.LiveReasoningService(settings)
    object.__setattr__(settings, "reasoning_effort", "low")
    observed = []

    def construct(bound_settings):
        observed.append(bound_settings.reasoning_effort)
        return live_case.script()

    monkeypatch.setattr(live, "build_chat_model", construct)
    result = await service.run(live_request, transport="direct")
    assert result.status == "success"
    assert service.settings is not settings
    assert observed == ["high"]


@pytest.mark.parametrize("value", [None, [], "invalid-private-canary"])
def test_service_rejects_tampered_reasoning_effort(value):
    settings = LLMSettings(mode="openai", model="test-model")
    object.__setattr__(settings, "reasoning_effort", value)
    with pytest.raises((TypeError, ValueError)) as error:
        live.LiveReasoningService(settings)
    assert "private-canary" not in str(error.value)


async def test_real_graph_projects_ordered_plan_reads_and_usage(
    monkeypatch, live_case, live_request
):
    result = await runner(monkeypatch, live_case.script()).run(live_request, transport="direct")
    summary = result.summary
    assert result.status == "success"
    assert summary.status == "completed"
    assert summary.plan == summary.specialist_sequence == tuple(live_case.roles)
    assert len(summary.read_tool_sequence) == 15
    assert summary.response_summary.confirmed_lot_count == 2
    assert summary.response_summary.executed is False
    assert (summary.input_tokens, summary.output_tokens, summary.total_tokens) == (75, 50, 125)
    assert summary.duration_ms > 0
    assert all(event.status == "completed" and event.duration_ms >= 0 for event in summary.events)
    assert PRIVATE not in result.model_dump_json()


@pytest.mark.parametrize(
    "fault", ["no_plan", "missing_role", "wrong_order", "duplicate", "no_structure"]
)
async def test_invalid_completion_returns_failed_summary(
    monkeypatch, live_case, live_request, fault
):
    model = live_case.script()
    messages = list(model.messages)
    if fault == "no_plan":
        messages.pop(0)
    elif fault == "missing_role":
        del messages[-3:-1]
    elif fault == "wrong_order":
        messages[1].tool_calls[0]["args"]["subagent_type"] = live_case.roles[1]
    elif fault == "duplicate":
        next(
            row
            for row in messages
            if row.tool_calls[0]["name"] == "task"
            and row.tool_calls[0]["args"]["subagent_type"] == live_case.roles[1]
        ).tool_calls[0]["args"]["subagent_type"] = live_case.roles[0]
    else:
        messages[-1] = AIMessage(content=PRIVATE)
    result = await runner(monkeypatch, type(model)(messages=iter(messages))).run(
        live_request, transport="direct"
    )
    assert result.status == "semantic_failure"
    assert result.claims is None and result.receipts == ()
    assert result.summary.error_category == "invalid_response"
    assert PRIVATE not in result.model_dump_json()


@pytest.mark.parametrize(
    "fault", ["coerced_score", "unknown_nested", "executed_zero", "missing_executed"]
)
async def test_original_child_response_cannot_be_laundered_by_sdk_coercion(
    monkeypatch, live_case, live_request, fault
):
    raw = live_case.raw
    if fault == "coerced_score":
        decision = raw["product-lot-matching"]["decisions"][0]
        decision["product_score"] = str(decision["product_score"])
    elif fault == "unknown_nested":
        raw["product-lot-matching"]["decisions"][0]["metadata"] = {"private": PRIVATE}
    elif fault == "executed_zero":
        raw["containment-communications"]["executed"] = 0
    else:
        del raw["containment-communications"]["executed"]
    result = await runner(monkeypatch, live_case.script(raw)).run(live_request, transport="direct")
    assert result.status == "semantic_failure"
    assert result.claims is None and result.receipts == ()
    assert PRIVATE not in result.model_dump_json()


async def test_provider_failure_is_sanitized_and_service_retains_no_model(
    monkeypatch, live_request
):
    class AuthenticationError(Exception):
        pass

    def fail(settings):
        raise AuthenticationError(PRIVATE)

    monkeypatch.setattr(live, "build_chat_model", fail)
    service = live.LiveReasoningService(LLMSettings(mode="openai", model="test-model"))
    result = await service.run(live_request, transport="direct")
    assert result.status == "execution_failure"
    assert result.summary.error_category == "authentication"
    assert result.summary.events == ()
    assert PRIVATE not in result.model_dump_json() + repr(vars(service))


async def test_unknown_construction_error_is_not_mislabelled_as_provider_fallback(
    monkeypatch, live_request
):
    def fail(settings):
        raise TypeError(PRIVATE)

    monkeypatch.setattr(live, "build_chat_model", fail)
    result = await live.LiveReasoningService(LLMSettings(mode="openai", model="test-model")).run(
        live_request, transport="direct"
    )
    assert result.status == "semantic_failure"
    assert PRIVATE not in result.model_dump_json()


@pytest.mark.parametrize(
    "error",
    [
        TimeoutError(PRIVATE),
        ConnectionError(PRIVATE),
        ExceptionGroup(PRIVATE, [ConnectionError(PRIVATE)]),
    ],
)
async def test_read_failure_is_recorded_without_payload(
    monkeypatch, live_case, live_request, error
):
    import recallops.agents.deep_supervisor as supervisor

    async def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(supervisor, "_invoke_trusted_read", fail)
    result = await runner(monkeypatch, live_case.script()).run(live_request, transport="direct")
    assert result.status == "execution_failure"
    assert result.claims is None and result.receipts == ()
    assert any(row.name == "get_recall" and row.status == "failed" for row in result.summary.events)
    assert PRIVATE not in result.model_dump_json()


@pytest.mark.parametrize(
    "error_type",
    [
        ValueError,
        KeyError,
        RuntimeError,
        lambda message: ExceptionGroup(message, [ConnectionError(message), ValueError(message)]),
    ],
)
async def test_semantic_read_failure_does_not_enable_fallback(
    monkeypatch, live_case, live_request, error_type
):
    import recallops.agents.deep_supervisor as supervisor

    async def fail(*args, **kwargs):
        raise error_type(PRIVATE)

    monkeypatch.setattr(supervisor, "_invoke_trusted_read", fail)
    result = await runner(monkeypatch, live_case.script()).run(live_request, transport="direct")
    assert result.status == "semantic_failure"
    assert result.claims is None and result.receipts == ()
    assert PRIVATE not in result.model_dump_json()


async def test_budget_exhaustion_is_bounded_and_discards_partial_claims(
    monkeypatch, live_case, live_request
):
    model = live_case.script()
    messages = list(model.messages)
    looping = AIMessage(
        content=PRIVATE, tool_calls=[{"name": "ls", "args": {"path": "/"}, "id": "loop"}]
    )
    result = await runner(
        monkeypatch, type(model)(messages=chain(messages[:-1], repeat(looping)))
    ).run(live_request, transport="direct")
    assert result.status == "execution_failure"
    assert result.summary.error_category == "budget_exceeded"
    assert result.summary.specialist_sequence == tuple(live_case.roles)
    assert 25 < len([row for row in result.summary.events if row.kind == "model"]) <= 64
    assert result.claims is None and result.receipts == ()


async def test_model_failure_after_start_is_sanitized(monkeypatch, live_case, live_request):
    class RateLimitError(Exception):
        pass

    class FailingModel(type(live_case.script())):
        def _generate(self, *args, **kwargs):
            raise RateLimitError(PRIVATE)

    result = await runner(monkeypatch, FailingModel(messages=iter([]))).run(
        live_request, transport="direct"
    )
    assert result.status == "execution_failure"
    assert result.summary.error_category == "rate_limit"
    assert len(result.summary.events) == 1 and result.summary.events[0].status == "failed"
    assert result.summary.total_tokens is None
    assert PRIVATE not in result.model_dump_json()


async def test_live_invocation_disables_external_tracing_and_global_cache(
    monkeypatch, live_case, live_request
):
    from langchain_core.caches import InMemoryCache
    from langchain_core.globals import get_llm_cache, set_llm_cache
    from langsmith.run_helpers import get_tracing_context

    model = live_case.script()

    class ObservingModel(type(model)):
        def _generate(self, *args, **kwargs):
            assert get_tracing_context()["enabled"] is False
            assert self.cache is False
            return super()._generate(*args, **kwargs)

    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    original = get_llm_cache()
    cache = InMemoryCache()
    set_llm_cache(cache)
    try:
        result = await runner(monkeypatch, ObservingModel(messages=model.messages)).run(
            live_request, transport="direct"
        )
        assert result.status == "success"
        assert cache._cache == {}
    finally:
        set_llm_cache(original)


async def test_complete_inner_graph_starts_without_outer_config_or_authority_context(
    monkeypatch, live_case, live_request
):
    from contextvars import ContextVar

    from langchain_core.callbacks import BaseCallbackHandler
    from langchain_core.runnables.config import var_child_runnable_config

    authority = ContextVar("outer_authority_canary", default=None)
    observed = []

    class OuterCallback(BaseCallbackHandler):
        def on_chat_model_start(self, *args, **kwargs):
            observed.append("raw_callback")

    parent_config = {
        "callbacks": [OuterCallback()],
        "configurable": {
            "__pregel_checkpointer": object(),
            "__pregel_store": object(),
            "__pregel_cache": object(),
        },
    }
    config_token = var_child_runnable_config.set(parent_config)
    authority_token = authority.set(object())

    def isolated_model(settings):
        assert var_child_runnable_config.get() is None
        assert authority.get() is None
        observed.append("isolated_factory")
        return live_case.script()

    monkeypatch.setattr(live, "build_chat_model", isolated_model)
    try:
        result = await live.LiveReasoningService(
            LLMSettings(mode="openai", model="test-model")
        ).run(live_request, transport="direct")
        assert result.status == "success"
        assert observed == ["isolated_factory"]
        assert var_child_runnable_config.get() is parent_config
        assert authority.get() is not None
    finally:
        var_child_runnable_config.reset(config_token)
        authority.reset(authority_token)


async def test_malformed_structured_response_is_classified_without_raw_values(
    monkeypatch, live_case, live_request
):
    model = live_case.script()
    messages = list(model.messages)
    messages[-1].tool_calls[0]["args"]["evidence_count"] = PRIVATE
    result = await runner(monkeypatch, type(model)(messages=iter(messages))).run(
        live_request, transport="direct"
    )
    assert result.status == "semantic_failure"
    assert result.summary.error_category == "invalid_response"
    assert result.summary.response_summary is None
    assert PRIVATE not in result.model_dump_json()


async def test_concurrent_runs_keep_read_observations_isolated(
    monkeypatch, live_case, live_request
):
    from recallops.agents.verification import resolve_trusted_evidence, verify_live_investigation

    first = live_case.script()
    messages = [
        row for row in live_case.script().messages if row.tool_calls[0]["name"] != "get_recall"
    ]
    models = iter([first, type(first)(messages=iter(messages))])
    monkeypatch.setattr(live, "build_chat_model", lambda settings: next(models))
    service = live.LiveReasoningService(LLMSettings(mode="openai", model="test-model"))
    one, two = await asyncio.gather(
        service.run(live_request, transport="direct"), service.run(live_request, transport="direct")
    )
    assert len(one.receipts) == 15 and len(two.receipts) == 14
    assert one.summary.total_tokens == 125 and two.summary.total_tokens == 120
    assert live.READ_TOOL_OBSERVATIONS.get() is None
    assert not verify_live_investigation(
        live_request,
        two.claims,
        await resolve_trusted_evidence(live_request),
        receipts=two.receipts,
    ).result.passed


async def test_unstructured_specialist_response_cannot_count_as_completion(
    monkeypatch, live_case, live_request
):
    model = live_case.script()
    messages = list(model.messages)
    messages[3] = AIMessage(content=PRIVATE)
    result = await runner(monkeypatch, type(model)(messages=iter(messages))).run(
        live_request, transport="direct"
    )
    assert result.status == "semantic_failure"
    assert result.summary.response_summary is None
    assert PRIVATE not in result.model_dump_json()


@pytest.mark.parametrize("debug,verbose", [(True, False), (False, True), (True, True)])
async def test_global_console_settings_cannot_log_private_content(
    monkeypatch, live_case, live_request, capsys, debug, verbose
):
    from langchain_core.globals import get_debug, get_verbose, set_debug, set_verbose

    original = get_debug(), get_verbose()
    set_debug(debug)
    set_verbose(verbose)
    try:
        result = await runner(monkeypatch, live_case.script()).run(live_request, transport="direct")
        captured = capsys.readouterr()
        assert result.status == "success"
        assert PRIVATE not in captured.out + captured.err
        assert (get_debug(), get_verbose()) == (debug, verbose)
    finally:
        set_debug(original[0])
        set_verbose(original[1])


async def test_overlapping_live_runs_keep_console_disabled_until_last_exit(
    monkeypatch, live_case, live_request, capsys
):
    from langchain_core.globals import get_debug, get_verbose, set_debug, set_verbose

    second_started, first_finished = asyncio.Event(), asyncio.Event()
    model = live_case.script()

    class OverlappingModel(type(model)):
        second: bool = False

        async def _agenerate(self, *args, **kwargs):
            if self.second:
                second_started.set()
                await first_finished.wait()
            else:
                await second_started.wait()
            return await super()._agenerate(*args, **kwargs)

    models = iter(
        [
            OverlappingModel(messages=model.messages),
            OverlappingModel(messages=live_case.script().messages, second=True),
        ]
    )
    monkeypatch.setattr(live, "build_chat_model", lambda settings: next(models))
    service = live.LiveReasoningService(LLMSettings(mode="openai", model="test-model"))
    original = get_debug(), get_verbose()
    set_debug(True)
    set_verbose(True)
    first = asyncio.create_task(service.run(live_request, transport="direct"))
    second = asyncio.create_task(service.run(live_request, transport="direct"))
    try:
        one = await asyncio.wait_for(first, 10)
        assert one.status == "success"
        assert (get_debug(), get_verbose()) == (False, False)
        first_finished.set()
        two = await asyncio.wait_for(second, 10)
        assert two.status == "success"
        assert len(one.receipts) == len(two.receipts) == 15
        assert (get_debug(), get_verbose()) == (True, True)
        captured = capsys.readouterr()
        assert PRIVATE not in captured.out + captured.err
    finally:
        first_finished.set()
        for task in (first, second):
            if not task.done():
                task.cancel()
        await asyncio.gather(first, second, return_exceptions=True)
        set_debug(original[0])
        set_verbose(original[1])


async def test_console_settings_restore_after_live_failure(monkeypatch, live_request, capsys):
    from langchain_core.globals import get_debug, get_verbose, set_debug, set_verbose

    original = get_debug(), get_verbose()

    def fail(settings):
        raise TimeoutError(PRIVATE)

    monkeypatch.setattr(live, "build_chat_model", fail)
    set_debug(True)
    set_verbose(True)
    try:
        result = await live.LiveReasoningService(
            LLMSettings(mode="openai", model="test-model")
        ).run(live_request, transport="direct")
        assert result.status == "execution_failure"
        assert (get_debug(), get_verbose()) == (True, True)
        captured = capsys.readouterr()
        assert PRIVATE not in captured.out + captured.err
    finally:
        set_debug(original[0])
        set_verbose(original[1])


async def test_console_settings_restore_after_live_cancellation(
    monkeypatch, live_case, live_request, capsys
):
    from langchain_core.globals import get_debug, get_verbose, set_debug, set_verbose

    started = asyncio.Event()
    model = live_case.script()

    class WaitingModel(type(model)):
        async def _agenerate(self, *args, **kwargs):
            started.set()
            await asyncio.Event().wait()

    service = runner(monkeypatch, WaitingModel(messages=iter([])))
    original = get_debug(), get_verbose()
    set_debug(True)
    set_verbose(True)
    task = asyncio.create_task(service.run(live_request, transport="direct"))
    try:
        await asyncio.wait_for(started.wait(), 10)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert (get_debug(), get_verbose()) == (True, True)
        assert live.READ_TOOL_OBSERVATIONS.get() is None
        captured = capsys.readouterr()
        assert PRIVATE not in captured.out + captured.err
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        set_debug(original[0])
        set_verbose(original[1])
