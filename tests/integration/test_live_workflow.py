"""Live claims become authoritative only inside the durable verification gate."""

import asyncio
import json
import sqlite3

import pytest

from recallops.agents.runtime import RecallOpsRuntime
from recallops.llm import LLMSettings


def rows(path, table):
    with sqlite3.connect(path) as connection:
        return connection.execute(f'SELECT * FROM "{table}"').fetchall()


async def start(tmp_path, scope):
    async with RecallOpsRuntime.open(
        checkpoint_path=tmp_path / "checkpoint.sqlite",
        operations_path=tmp_path / "operations.sqlite",
        llm_settings=LLMSettings(mode="openai", model="test-model"),
    ) as runtime:
        return await runtime.start_case(
            case_id="CASE-LIVE",
            thread_id="THREAD-LIVE",
            recall_number="H-1230-2026",
            question="Investigate H-1230-2026 eggs recall",
            scope_lot_ids=scope,
        )


async def test_live_route_skips_specialist_generators_and_stops_at_review(
    monkeypatch, live_case, tmp_path
):
    import recallops.llm.live_reasoning as live

    monkeypatch.setattr(live, "build_chat_model", lambda settings: live_case.script())
    result = await start(tmp_path, live_case.scope)
    assert result.pending_interrupt["kind"] == "action_review"
    assert result.case["verification"]["passed"] is True
    assert result.case["investigation_source"] == "openai"
    assert result.case["node_trace"].index("retrieve_context") < result.case["node_trace"].index(
        "live_investigate"
    )
    assert not set(result.case["node_trace"]) & {
        "plan",
        "dispatch_specialist",
        "regulatory_intake",
        "product_lot_match",
        "trace_forward_backward",
        "reconcile",
        "containment_draft",
    }
    assert result.case["live_claims"]["matching"]["confirmed_lot_ids"] == [
        "LOT-EXACT-170",
        "LOT-PROBABLE-160",
    ]
    assert not rows(tmp_path / "operations.sqlite", "receipts")
    assert not rows(tmp_path / "operations.sqlite", "cases")


@pytest.mark.parametrize("fault", ["classification", "empty", "missing_role"])
async def test_semantic_failure_never_falls_back_or_reviews(
    monkeypatch, live_case, tmp_path, fault
):
    import recallops.llm.live_reasoning as live

    raw = live_case.raw
    if fault == "classification":
        raw[live_case.roles[1]]["decisions"][0]["classification"] = "probable"
    if fault == "empty":
        raw[live_case.roles[3]]["proposed_actions"] = []
    model = live_case.script(raw)
    if fault == "missing_role":
        messages = list(model.messages)
        messages = messages[:-3] + messages[-1:]
        model = type(model)(messages=iter(messages))
    monkeypatch.setattr(live, "build_chat_model", lambda settings: model)
    result = await start(tmp_path, live_case.scope)
    assert result.pending_interrupt is None
    assert result.case["status"] == "escalated"
    assert result.case["verification"]["passed"] is False
    assert result.case["investigation_source"] == "openai"
    assert not result.case.get("confirmed_lot_ids")
    assert not result.case["action_queue"]
    assert not rows(tmp_path / "operations.sqlite", "receipts")


async def test_execution_failure_discards_claims_and_labels_fallback(
    monkeypatch, live_case, tmp_path
):
    import recallops.llm.live_reasoning as live

    def fail(settings):
        raise TimeoutError("PRIVATE_PROVIDER_CANARY")

    monkeypatch.setattr(live, "build_chat_model", fail)
    result = await start(tmp_path, live_case.scope)
    assert result.pending_interrupt["kind"] == "action_review"
    assert result.case["investigation_source"] == "deterministic_fallback"
    assert result.case["live_claims"] is None
    assert result.case["live_run"]["summary"]["fallback_used"] is True
    assert "PRIVATE_PROVIDER_CANARY" not in result.model_dump_json()


async def test_source_change_after_model_result_stops_closed_without_echo(
    monkeypatch, live_case, tmp_path
):
    import recallops.agents.workflow as workflow
    import recallops.llm.live_reasoning as live

    monkeypatch.setattr(live, "build_chat_model", lambda settings: live_case.script())
    run = live.LiveReasoningService.run

    async def change_source(self, request, *, transport):
        result = await run(self, request, transport=transport)

        def unavailable(**kwargs):
            raise ValueError("PRIVATE_SOURCE_CHANGE_CANARY")

        monkeypatch.setattr(workflow, "build_live_request", unavailable)
        return result

    monkeypatch.setattr(live.LiveReasoningService, "run", change_source)
    result = await start(tmp_path, live_case.scope)
    assert result.case["status"] == "escalated"
    assert result.case["verification"]["passed"] is False
    assert result.pending_interrupt is None
    assert not rows(tmp_path / "operations.sqlite", "receipts")
    assert "PRIVATE_SOURCE_CHANGE_CANARY" not in result.model_dump_json()


async def test_inner_graph_cannot_write_raw_messages_to_any_sqlite_namespace(
    monkeypatch, live_case, tmp_path, capsys
):
    from langchain_core.caches import InMemoryCache
    from langchain_core.globals import (
        get_debug,
        get_llm_cache,
        get_verbose,
        set_debug,
        set_llm_cache,
        set_verbose,
    )
    from langsmith import Client, tracing_context

    import recallops.llm.live_reasoning as live

    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    telemetry = []

    def collect_trace(self, *args, **kwargs):
        telemetry.append(json.dumps({"args": args, "kwargs": kwargs}, default=str))

    monkeypatch.setattr(Client, "create_run", collect_trace)
    monkeypatch.setattr(Client, "update_run", collect_trace)
    tracing_client = Client(api_key="test-not-a-real-key", auto_batch_tracing=False)
    canary = "PRIVATE_MODEL_SQLITE_CANARY"
    import recallops.agents.deep_supervisor as deep

    monkeypatch.setattr(deep, "SUPERVISOR_PROMPT", deep.SUPERVISOR_PROMPT + canary)
    raw = live_case.raw
    for decision in raw["product-lot-matching"]["decisions"]:
        decision["rationale"] = canary
    for action in raw["containment-communications"]["proposed_actions"]:
        action["rationale"] = canary
    for draft in raw["containment-communications"]["communication_drafts"]:
        draft["subject"] = canary
        draft["body"] += canary
    monkeypatch.setattr(
        live, "build_chat_model", lambda settings: live_case.script(raw, canary=canary)
    )
    original = get_debug(), get_verbose(), get_llm_cache()
    cache = InMemoryCache()
    set_debug(True)
    set_verbose(True)
    set_llm_cache(cache)
    try:
        with tracing_context(enabled=True, client=tracing_client):
            result = await start(tmp_path, live_case.scope)
        assert result.pending_interrupt["kind"] == "action_review"
        assert canary not in result.model_dump_json()
        assert telemetry
        assert canary not in "".join(telemetry)
        captured = capsys.readouterr()
        assert canary not in captured.out + captured.err
        assert not cache._cache
        with sqlite3.connect(tmp_path / "checkpoint.sqlite") as connection:
            for table in ("checkpoints", "writes"):
                for row in connection.execute(f'SELECT * FROM "{table}"'):
                    assert all(
                        canary.encode()
                        not in (cell if isinstance(cell, bytes) else str(cell).encode())
                        for cell in row
                    )
            assert connection.execute(
                "SELECT DISTINCT checkpoint_ns FROM checkpoints"
            ).fetchall() == [("",)]
    finally:
        set_debug(original[0])
        set_verbose(original[1])
        set_llm_cache(original[2])


async def test_cancelled_live_run_is_durable_unfinished_and_never_implicitly_replayed(
    monkeypatch, live_case, tmp_path
):
    from langchain_core.globals import get_debug, set_debug

    import recallops.llm.live_reasoning as live

    started = asyncio.Event()
    original = get_debug()
    model = live_case.script()
    import recallops.agents.deep_supervisor as deep

    canary = "PRIVATE_CANCELLED_PROMPT_CANARY"
    monkeypatch.setattr(deep, "SUPERVISOR_PROMPT", deep.SUPERVISOR_PROMPT + canary)

    class WaitingModel(type(model)):
        async def _agenerate(self, *args, **kwargs):
            started.set()
            await asyncio.Event().wait()

    monkeypatch.setattr(live, "build_chat_model", lambda settings: WaitingModel(messages=iter([])))
    set_debug(True)
    task = asyncio.create_task(start(tmp_path, live_case.scope))
    try:
        await asyncio.wait_for(started.wait(), 10)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert get_debug() is True
        async with RecallOpsRuntime.open(
            checkpoint_path=tmp_path / "checkpoint.sqlite",
            operations_path=tmp_path / "operations.sqlite",
        ) as runtime:
            result = await runtime.get_case(thread_id="THREAD-LIVE")
            assert result.case["live_run"]["status"] == "started"
            assert result.pending_interrupt is None
            for table in ("checkpoints", "writes"):
                for row in rows(tmp_path / "checkpoint.sqlite", table):
                    assert all(
                        canary.encode()
                        not in (cell if isinstance(cell, bytes) else str(cell).encode())
                        for cell in row
                    )
            with pytest.raises(ValueError, match="already has"):
                await runtime.start_case(
                    case_id="CASE-LIVE",
                    thread_id="THREAD-LIVE",
                    recall_number="H-1230-2026",
                    question="same",
                )
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        set_debug(original)
