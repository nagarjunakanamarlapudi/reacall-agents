"""Bounded child correction through real compiled subagent/model/tool surfaces."""

import asyncio
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import replace
from itertools import chain, count

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from pydantic import Field

from recallops.llm import LLMSettings
from recallops.llm import live_reasoning as live

ROLE_SCHEMAS = (
    ("recall-intelligence", "RecallIntelligence"),
    ("product-lot-matching", "ProductLotAssessment"),
    ("traceability-reconciliation", "TraceabilityAssessment"),
    ("containment-communications", "ContainmentProposal"),
)
PRIVATE = "PRIVATE_REJECTED_CHILD_VALUE"


def observed_model(live_case, messages):
    class ObservedModel(type(live_case.script())):
        seen: list = Field(default_factory=list)

        def _generate(self, messages, *args, **kwargs):
            self.seen.append(list(messages))
            return super()._generate(messages, *args, **kwargs)

    return ObservedModel(messages=iter(messages))


async def run_script(monkeypatch, live_case, live_request, messages, *, transport="direct"):
    model = observed_model(live_case, messages)
    monkeypatch.setattr(live, "build_chat_model", lambda settings: model)
    result = await live.LiveReasoningService(LLMSettings(mode="openai", model="test-model")).run(
        live_request, transport=transport
    )
    return result, model.seen


def index_of(messages, schema):
    return next(i for i, message in enumerate(messages) if message.tool_calls[0]["name"] == schema)


def invalid_response(valid, fault):
    if fault == "missing_response":
        return AIMessage(content=PRIVATE)
    invalid = deepcopy(valid)
    invalid.content = PRIVATE
    invalid.tool_calls[0]["id"] = "premature-response"
    raw = invalid.tool_calls[0]["args"]
    if fault == "raw_shape":
        raw["unexpected"] = PRIVATE
    else:
        name = invalid.tool_calls[0]["name"]
        if name == "RecallIntelligence":
            raw["official_products"] = []
        elif name == "ProductLotAssessment":
            raw["confirmed_lot_ids"] = []
        elif name == "TraceabilityAssessment":
            raw["affected_facilities"] = []
        else:
            raw["communication_drafts"][0]["body"] = PRIVATE
    return invalid


@pytest.mark.parametrize("role,schema", ROLE_SCHEMAS)
@pytest.mark.parametrize("fault", ["missing_response", "raw_shape", "semantic"])
async def test_one_invalid_child_completion_is_corrected_before_parent_observes_it(
    monkeypatch, live_case, live_request, role, schema, fault
):
    from recallops.agents.verification import resolve_trusted_evidence, verify_live_investigation

    messages = list(live_case.script().messages)
    position = index_of(messages, schema)
    messages.insert(position, invalid_response(messages[position], fault))
    result, seen = await run_script(monkeypatch, live_case, live_request, messages)
    assert result.status == "success"
    assert tuple(result.summary.specialist_sequence) == tuple(live_case.roles)
    assert len(seen) == 26
    assert len(result.receipts) == 15
    corrected_input = seen[position + 1]
    assert isinstance(corrected_input[-1], HumanMessage)
    assert "completion contract" in corrected_input[-1].content
    assert PRIVATE not in str(corrected_input)
    assert PRIVATE not in result.model_dump_json()
    assert verify_live_investigation(
        live_request,
        result.claims,
        await resolve_trusted_evidence(live_request),
        receipts=result.receipts,
    ).result.passed


@pytest.mark.parametrize("role,schema", ROLE_SCHEMAS)
async def test_second_invalid_completion_stops_without_a_third_attempt(
    monkeypatch, live_case, live_request, role, schema
):
    messages = list(live_case.script().messages)
    position = index_of(messages, schema)
    invalid = invalid_response(messages[position], "semantic")
    messages[position:position] = [invalid, deepcopy(invalid)]
    result, seen = await run_script(monkeypatch, live_case, live_request, messages)
    assert result.status == "semantic_failure"
    assert len(seen) == position + 2
    assert isinstance(seen[-1][-1], HumanMessage)
    assert "completion contract" in seen[-1][-1].content
    assert PRIVATE not in str(seen[-1])
    assert result.claims is None and result.receipts == ()
    assert role not in result.summary.specialist_sequence
    assert result.summary.error_category == "invalid_response"
    assert PRIVATE not in result.model_dump_json()


@pytest.mark.parametrize("role,schema", ROLE_SCHEMAS[:3])
async def test_premature_structured_completion_requires_real_sealed_reads(
    monkeypatch, live_case, live_request, role, schema
):
    messages = list(live_case.script().messages)
    end = index_of(messages, schema)
    start = next(
        i + 1
        for i, message in enumerate(messages)
        if message.tool_calls[0]["name"] == "task"
        and message.tool_calls[0]["args"]["subagent_type"] == role
    )
    premature = deepcopy(messages[end])
    premature.tool_calls[0]["id"] = "premature-readless"
    messages.insert(start, premature)
    result, seen = await run_script(monkeypatch, live_case, live_request, messages)
    assert result.status == "success"
    assert len(seen) == 26
    assert len(result.receipts) == 15
    assert isinstance(seen[start + 1][-1], HumanMessage)


@pytest.mark.parametrize(
    "schema,calls", [("RecallIntelligence", 4), ("TraceabilityAssessment", 22)]
)
@pytest.mark.parametrize("fault", ["malformed", "duplicate"])
async def test_raw_provider_json_failure_never_enters_correction(
    raw_openai_script, live_request, schema, calls, fault
):
    responses = raw_openai_script(
        **(
            {"malformed_arguments": (schema, "{" + PRIVATE)}
            if fault == "malformed"
            else {
                "duplicate": (
                    schema,
                    ("recall_number" if schema == "RecallIntelligence" else "lot_ids",),
                    PRIVATE,
                )
            }
        )
    )
    result = await live.LiveReasoningService(LLMSettings(mode="openai", model="test-model")).run(
        live_request, transport="direct"
    )
    assert result.status == "semantic_failure"
    assert len(responses) == calls
    assert result.claims is None and result.receipts == ()
    assert PRIVATE not in result.model_dump_json()


@pytest.mark.parametrize("error_type", [TimeoutError, ConnectionError, ValueError])
async def test_transport_and_source_errors_are_not_corrected(
    monkeypatch, live_case, live_request, error_type
):
    from recallops.agents import deep_supervisor as deep

    async def fail(*args, **kwargs):
        raise error_type(PRIVATE)

    monkeypatch.setattr(deep, "_invoke_trusted_read", fail)
    result, seen = await run_script(
        monkeypatch, live_case, live_request, list(live_case.script().messages)
    )
    assert len(seen) == 3
    assert result.status == (
        "semantic_failure" if error_type is ValueError else "execution_failure"
    )
    assert len(result.summary.read_tool_sequence) == 1
    assert result.summary.specialist_sequence == ()
    assert result.claims is None and PRIVATE not in result.model_dump_json()


@pytest.mark.parametrize("name", ["create_case", "task", "find_candidate_products"])
async def test_unauthorized_child_tool_is_rejected_without_execution_or_correction(
    monkeypatch, live_case, live_request, name
):
    messages = list(live_case.script().messages)
    messages.insert(
        2, AIMessage(content=PRIVATE, tool_calls=[{"name": name, "args": {}, "id": "unauthorized"}])
    )
    result, seen = await run_script(monkeypatch, live_case, live_request, messages)
    assert len(seen) == 3
    assert result.status == "semantic_failure"
    assert result.summary.read_tool_sequence == ()
    assert result.claims is None and PRIVATE not in result.model_dump_json()


async def test_claimed_read_output_without_sealed_observation_cannot_complete(
    monkeypatch, live_case, live_request
):
    messages = list(live_case.script().messages)
    premature = deepcopy(messages[3])
    premature.content = '{"name":"get_recall","status":"completed"}'
    messages[2:4] = [premature, deepcopy(premature)]
    result, seen = await run_script(monkeypatch, live_case, live_request, messages)
    assert len(seen) == 4
    assert result.status == "semantic_failure"
    assert result.summary.read_tool_sequence == ()
    assert "completion contract" in seen[-1][-1].content


async def test_required_recall_input_digest_is_not_only_a_matching_tool_name(
    monkeypatch, live_case, live_request
):
    messages = list(live_case.script().messages)
    wrong_read = deepcopy(messages[2])
    wrong_read.tool_calls[0]["id"] = "wrong-recall"
    wrong_read.tool_calls[0]["args"]["recall_number"] = "OTHER-RECALL"
    premature = deepcopy(messages[3])
    premature.tool_calls[0]["id"] = "premature-wrong-recall"
    messages[2:2] = [wrong_read, premature]
    result, seen = await run_script(monkeypatch, live_case, live_request, messages)
    assert result.status == "success"
    assert len(seen) == 27
    assert len(result.receipts) == 16
    assert isinstance(seen[4][-1], HumanMessage)
    assert result.receipts[0].input_digest != result.receipts[1].input_digest


@pytest.mark.parametrize("tool_name", ["find_candidate_products", "match_lots"])
async def test_matching_requires_both_exact_predicate_reads(
    monkeypatch, live_case, live_request, tool_name
):
    messages = list(live_case.script().messages)
    index = index_of(messages, tool_name)
    messages[index].tool_calls[0]["args"]["predicate"]["hazard"] = PRIVATE
    end = index_of(messages, "ProductLotAssessment")
    messages.insert(end, deepcopy(messages[end]))
    result, seen = await run_script(monkeypatch, live_case, live_request, messages)
    assert result.status == "semantic_failure"
    assert len(seen) == 9
    assert tuple(result.summary.specialist_sequence) == ("recall-intelligence",)
    assert len(result.summary.read_tool_sequence) == 3
    assert "completion contract" in seen[-1][-1].content
    assert PRIVATE not in result.model_dump_json()


@pytest.mark.parametrize("lot", ["LOT-EXACT-170", "LOT-PROBABLE-160", "LOT-AMBIG-175"])
@pytest.mark.parametrize(
    "tool", ["trace_forward", "trace_backward", "get_inventory", "reconcile_units"]
)
async def test_trace_requires_every_tool_for_each_prerequisite_lot(
    monkeypatch, live_case, live_request, lot, tool
):
    messages = [
        message
        for message in live_case.script().messages
        if not (
            message.tool_calls[0]["name"] == tool
            and message.tool_calls[0]["args"].get("lot_id") == lot
        )
    ]
    end = index_of(messages, "TraceabilityAssessment")
    messages.insert(end, deepcopy(messages[end]))
    result, seen = await run_script(monkeypatch, live_case, live_request, messages)
    assert result.status == "semantic_failure"
    assert len(seen) == 22
    assert len(result.summary.read_tool_sequence) == 14
    assert tuple(result.summary.specialist_sequence) == tuple(live_case.roles[:2])
    assert "completion contract" in seen[-1][-1].content


async def test_source_digest_mismatch_stops_without_correction(
    monkeypatch, live_case, live_request
):
    from recallops.agents import deep_supervisor as deep

    counts = []

    class ChangedSource(type(live_case.script())):
        def _generate(self, messages, *args, **kwargs):
            counts.append(True)
            reads = deep.READ_TOOL_OBSERVATIONS.get()
            if reads:
                reads[-1] = replace(reads[-1], source_digest="0" * 64)
            return super()._generate(messages, *args, **kwargs)

    monkeypatch.setattr(
        live,
        "build_chat_model",
        lambda settings: ChangedSource(messages=live_case.script().messages),
    )
    result = await live.LiveReasoningService(LLMSettings(mode="openai", model="test-model")).run(
        live_request, transport="direct"
    )
    assert result.status == "semantic_failure"
    assert len(counts) == 4
    assert result.claims is None and result.receipts == ()


async def test_same_compiled_graph_concurrent_calls_isolate_binding_and_correction(
    live_case, live_request
):
    from langchain_core.outputs import ChatGeneration, ChatResult

    from recallops.agents import deep_supervisor as deep
    from recallops.agents.child_completion import _CHILD_BINDING
    from recallops.config import Settings

    lane = ContextVar("test_child_lane")
    scripts, calls, observation_ids = {}, {"a": [], "b": []}, {"a": set(), "b": set()}
    for name, failures in (("a", 1), ("b", 2)):
        messages = list(live_case.script().messages)
        messages[3:3] = [invalid_response(messages[3], "semantic") for _ in range(failures)]
        scripts[name] = iter(messages)

    class RoutedModel(type(live_case.script())):
        def _generate(self, messages, *args, **kwargs):
            name = lane.get()
            calls[name].append(list(messages))
            binding = _CHILD_BINDING.get()
            if binding is not None:
                assert binding.request_digest == live_request.request_digest
                observation_ids[name].add(binding.observations_identity)
            return ChatResult(generations=[ChatGeneration(message=next(scripts[name]))])

    graph = deep.build_deep_supervisor(
        model=RoutedModel(messages=iter([])),
        request=live_request,
        _read_source=deep._make_read_config(
            "direct", Settings(), scope_lot_ids=live_request.scope_lot_ids
        ),
    ).graph

    async def invoke(name):
        lane_token = lane.set(name)
        reads = []
        token = deep.READ_TOOL_OBSERVATIONS.set(reads)
        try:
            try:
                result = await graph.ainvoke(
                    {"messages": [HumanMessage(content="Investigate bound case")]},
                    config={"recursion_limit": 100},
                )
            except ValueError as error:
                assert PRIVATE not in str(error)
                result = None
            assert _CHILD_BINDING.get() is None
            return result, reads
        finally:
            deep.READ_TOOL_OBSERVATIONS.reset(token)
            lane.reset(lane_token)

    first, second = await asyncio.gather(invoke("a"), invoke("b"))
    assert first[0]["structured_response"]["executed"] is False and second[0] is None
    assert len(calls["a"]) == 26 and len(calls["b"]) == 5
    assert len(first[1]) == 15 and len(second[1]) == 1
    assert observation_ids["a"].isdisjoint(observation_ids["b"])
    assert _CHILD_BINDING.get() is None


@pytest.mark.parametrize("transport", ["direct", "stdio"])
async def test_happy_path_has_no_correction_or_extra_calls(
    monkeypatch, live_case, live_request, transport
):
    result, seen = await run_script(
        monkeypatch, live_case, live_request, list(live_case.script().messages), transport=transport
    )
    assert result.status == "success"
    assert len(seen) == 25
    assert len(result.receipts) == 15


async def test_correction_does_not_reset_the_shared_64_model_call_budget(
    monkeypatch, live_case, live_request
):
    messages = list(live_case.script().messages)
    prefix = [*messages[:3], invalid_response(messages[3], "semantic")]
    looping = (
        AIMessage(
            content="", tool_calls=[{"name": "ls", "args": {"path": "/"}, "id": f"loop-{index}"}]
        )
        for index in count()
    )
    result, seen = await run_script(monkeypatch, live_case, live_request, chain(prefix, looping))
    assert len(seen) == 64
    assert "completion contract" in seen[4][-1].content
    assert result.status == "execution_failure"
    assert result.summary.error_category == "budget_exceeded"
    assert result.claims is None and result.receipts == ()


async def test_child_correction_cannot_override_a_smaller_recursion_limit(
    monkeypatch, live_case, live_request
):
    from recallops.agents import deep_supervisor as deep

    original = live.build_deep_supervisor

    def bounded(**kwargs):
        supervisor = original(**kwargs)
        registry = deep._compiled_subagent_graphs(supervisor.graph)
        child = registry["recall-intelligence"]
        registry["recall-intelligence"] = child.with_config(recursion_limit=8)
        return supervisor

    monkeypatch.setattr(live, "build_deep_supervisor", bounded)
    messages = list(live_case.script().messages)
    messages.insert(3, invalid_response(messages[3], "semantic"))
    result, seen = await run_script(monkeypatch, live_case, live_request, messages)
    assert len(seen) == 5
    assert result.status == "execution_failure"
    assert result.summary.error_category == "budget_exceeded"
    assert result.claims is None


@pytest.mark.parametrize("value", [None, True, -1, 2, "0", []])
def test_private_correction_counter_requires_exact_zero_or_one(live_request, value):
    from recallops.agents.child_completion import ChildCompletionMiddleware, _bind_child_completion

    middleware = ChildCompletionMiddleware(
        "recall-intelligence", live_request.request_digest, ("get_recall",)
    )
    with _bind_child_completion("recall-intelligence", live_request, {}):
        state = middleware.before_agent({}, None)
        state["child_completion_corrections"] = value
        with pytest.raises(ValueError, match="private child completion state"):
            middleware.after_model(state, None)


def test_child_binding_cannot_be_rebound_to_another_request_or_role(live_request):
    from recallops.agents.child_completion import ChildCompletionMiddleware, _bind_child_completion

    for role, request_digest in (
        ("product-lot-matching", live_request.request_digest),
        ("recall-intelligence", "0" * 64),
    ):
        middleware = ChildCompletionMiddleware(role, request_digest, ())
        with _bind_child_completion("recall-intelligence", live_request, {}):
            with pytest.raises(ValueError, match="binding mismatch"):
                middleware.before_agent({}, None)


@pytest.mark.parametrize("hook", ["before_agent", "after_model"])
def test_compilation_rejects_replaced_child_completion_identity(
    monkeypatch, live_request, live_case, hook
):
    from recallops.agents import deep_supervisor as deep

    original = deep.create_deep_agent

    def substituted(*args, **kwargs):
        graph = original(*args, **kwargs)
        children = deep._compiled_subagent_graphs(graph)
        children["recall-intelligence"].nodes[f"ChildCompletionMiddleware.{hook}"] = children[
            "product-lot-matching"
        ].nodes[f"ChildCompletionMiddleware.{hook}"]
        return graph

    monkeypatch.setattr(deep, "create_deep_agent", substituted)
    with pytest.raises(ValueError, match="child completion middleware identity"):
        deep.build_deep_supervisor(model=live_case.script(), request=live_request)


@pytest.mark.parametrize("hook", ["before_agent", "after_model"])
@pytest.mark.parametrize(
    "executable,async_replacement", [("func", False), ("afunc", False), ("afunc", True)]
)
def test_compilation_rejects_same_instance_child_method_substitution(
    monkeypatch, live_request, live_case, hook, executable, async_replacement
):
    from recallops.agents import deep_supervisor as deep

    original = deep.create_deep_agent

    def substituted(*args, **kwargs):
        graph = original(*args, **kwargs)
        child = deep._compiled_subagent_graphs(graph)["recall-intelligence"]
        runnable = child.nodes[f"ChildCompletionMiddleware.{hook}"].bound
        instance = runnable.func.__self__
        other_hook = "after_model" if hook == "before_agent" else "before_agent"
        if async_replacement:
            other_hook = "a" + other_hook
        setattr(runnable, executable, getattr(instance, other_hook))
        return graph

    monkeypatch.setattr(deep, "create_deep_agent", substituted)
    with pytest.raises(ValueError, match="child completion middleware identity"):
        deep.build_deep_supervisor(model=live_case.script(), request=live_request)


@pytest.mark.parametrize(
    "node_name",
    [
        "DelegationGuardMiddleware.after_model",
        "ToolCallLimitMiddleware[task].after_model",
        "TodoListMiddleware.after_model",
    ],
)
@pytest.mark.parametrize(
    "executable,replacement",
    [("func", "before_agent"), ("afunc", "before_agent"), ("afunc", "abefore_agent")],
)
def test_compilation_rejects_same_instance_parent_method_substitution(
    monkeypatch, live_request, live_case, node_name, executable, replacement
):
    from recallops.agents import deep_supervisor as deep

    original = deep.create_deep_agent

    def substituted(*args, **kwargs):
        graph = original(*args, **kwargs)
        runnable = graph.nodes[node_name].bound
        setattr(runnable, executable, getattr(runnable.func.__self__, replacement))
        return graph

    monkeypatch.setattr(deep, "create_deep_agent", substituted)
    with pytest.raises(ValueError, match="compiled middleware identity"):
        deep.build_deep_supervisor(model=live_case.script(), request=live_request)


async def test_missing_recall_read_with_substituted_completion_stops_before_model(
    monkeypatch, live_request, live_case
):
    from recallops.agents import deep_supervisor as deep

    original = deep.create_deep_agent

    def substituted(*args, **kwargs):
        graph = original(*args, **kwargs)
        child = deep._compiled_subagent_graphs(graph)["recall-intelligence"]
        runnable = child.nodes["ChildCompletionMiddleware.after_model"].bound
        runnable.func = runnable.func.__self__.before_agent
        return graph

    monkeypatch.setattr(deep, "create_deep_agent", substituted)
    messages = [
        message
        for message in live_case.script().messages
        if message.tool_calls[0]["name"] != "get_recall"
    ]
    result, seen = await run_script(monkeypatch, live_case, live_request, messages)
    assert result.status != "success"
    assert result.claims is None
    assert result.receipts == ()
    assert seen == []


def test_child_private_state_is_not_an_input_output_or_model_tool_argument(live_case, live_request):
    from recallops.agents import deep_supervisor as deep

    supervisor = deep.build_deep_supervisor(model=live_case.script(), request=live_request)
    for graph in deep._compiled_subagent_graphs(supervisor.graph).values():
        for schema in (graph.get_input_jsonschema(), graph.get_output_jsonschema()):
            assert "child_completion_corrections" not in schema.get("properties", {})
            assert "child_completion_binding" not in schema.get("properties", {})
