"""Execute real read-only services and falsify trajectory/report claims."""

import json

import pytest

from recallops.evaluation.digests import canonical_sha256
from recallops.evaluation.orchestration_benchmark import (
    ReadOnlyEvidenceGateway,
    load_orchestration_report,
    run_orchestration_benchmark,
    score_trajectory,
    validate_orchestration_report,
)
from recallops.evaluation.orchestration_schema import load_orchestration_cases
from recallops.paths import DATA_DIR

CASES = DATA_DIR / "evals" / "orchestration_cases.json"


@pytest.mark.parametrize("failed", [False, True])
@pytest.mark.parametrize("read_name", ["get_recall", "search_recalls"])
async def test_builtin_live_projects_shared_observations_without_replaying_workers(
    tmp_path, monkeypatch, failed, read_name
):
    from recallops.evaluation import openai_live_adapter
    from recallops.llm import LLMSettings
    from recallops.llm.live_reasoning import LiveExecutionEvent, LiveReasoningSummary

    async def observed(self, question, *, transport):
        assert transport == "direct"
        assert '"recall_number": "H-' in question
        assert "expected_tasks" not in question and "evidence_facts" not in question
        return LiveReasoningSummary(
            model="test-model",
            status="failed" if failed else "completed",
            specialist_sequence=["recall-intelligence"],
            read_tool_sequence=[read_name, read_name],
            duration_ms=125.0,
            input_tokens=7,
            output_tokens=4,
            total_tokens=11,
            error_category="timeout" if failed else None,
            events=[
                LiveExecutionEvent(kind="tool", name=read_name, status="completed", duration_ms=2.0)
            ]
            * 2,
        )

    monkeypatch.setattr(openai_live_adapter.LiveReasoningService, "run", observed)
    factory = openai_live_adapter.build_openai_live_factory(
        LLMSettings(mode="openai", model="test-model")
    )
    target = tmp_path / "shared-live.json"
    report = await run_orchestration_benchmark(CASES, target, live_model=factory)
    live = report.live_status
    assert live.status == ("error" if failed else "completed")
    assert live.tokens == 264 and not live.cost_available
    row = live.results[0]
    assert row.observation.duration_ms == 125.0
    assert row.observation.specialists == ("recall-intelligence",)
    assert row.metrics.tool_call_count == 2
    assert row.metrics.prohibited_tool_call_count == 0
    assert row.metrics.duplicate_tool_calls == 0  # Arguments were not observed.
    assert row.metrics.task_accuracy > 0
    assert row.metrics.delegation_accuracy == 0.0
    assert row.metrics.missing_specialist_count == 3
    assert row.metrics.evidence_fact_coverage == 0.0
    assert not row.metrics.task_success
    assert all(call.input_sha256 is None for call in row.observation.tool_calls)
    assert report.gate_passed
    assert load_orchestration_report(target, CASES) == report
    from recallops.evaluation.orchestration_benchmark import _grading_error_result

    fallback = _grading_error_result(
        load_orchestration_cases(CASES).cases[0], "deep_agents_live", row.observation
    )
    assert fallback.metrics.duplicate_tool_calls == 0
    assert fallback.metrics.prohibited_tool_call_count == 0


def test_builtin_live_rejects_deterministic_settings():
    from recallops.evaluation.openai_live_adapter import build_openai_live_factory
    from recallops.llm import LLMSettings

    with pytest.raises(ValueError, match="openai"):
        build_openai_live_factory(LLMSettings())


@pytest.fixture
async def report(tmp_path):
    return await run_orchestration_benchmark(CASES, tmp_path / "report.json")


async def test_offline_profiles_have_no_operations_capability(report):
    assert report.metrics.prohibited_tool_exposure_count == 0
    assert [p.name for p in report.profiles] == ["bounded_single_agent", "fixed_specialists"]
    assert all(len(p.results) == 24 for p in report.profiles)
    assert report.gate_passed
    assert report.live_status.status == "not_run_missing_credentials"
    assert all(p.metrics.task_success_rate == 1.0 for p in report.profiles)
    assert all(p.metrics.budget_compliance == 1.0 for p in report.profiles)
    assert all(r.observation.duration_ms > 0 for p in report.profiles for r in p.results)
    assert all(r.observation.tokens is None for p in report.profiles for r in p.results)
    gateway = ReadOnlyEvidenceGateway(max_tool_calls=16)
    assert not hasattr(gateway, "operations")
    assert not hasattr(gateway, "create_case")
    assert not hasattr(gateway, "close_case")


@pytest.mark.parametrize(
    "mutation",
    [
        "order",
        "duplicate",
        "budget",
        "specialist",
        "facts",
        "stop",
        "arguments",
        "operations",
        "criteria",
    ],
)
async def test_trajectory_mutations_are_measured(report, mutation):
    case = load_orchestration_cases(CASES).cases[0]
    observation = report.profiles[1].results[0].observation
    if mutation == "order":
        observation = observation.model_copy(update={"tool_calls": observation.tool_calls[::-1]})
    elif mutation in {"duplicate", "budget"}:
        times = 1 if mutation == "duplicate" else 17
        observation = observation.model_copy(
            update={
                "tool_calls": observation.tool_calls + observation.tool_calls[:1] * times,
            }
        )
    elif mutation == "specialist":
        observation = observation.model_copy(update={"specialists": ()})
    elif mutation == "facts":
        observation = observation.model_copy(update={"evidence_facts": ()})
    elif mutation == "arguments":
        calls = list(observation.tool_calls)
        calls[-1] = calls[-1].model_copy(update={"input_sha256": canonical_sha256("LOT-WRONG")})
        observation = observation.model_copy(update={"tool_calls": tuple(calls)})
    elif mutation == "operations":
        call = observation.tool_calls[-1].model_copy(
            update={"name": "close_case", "family": "operations"}
        )
        observation = observation.model_copy(
            update={"tool_calls": observation.tool_calls + (call,)}
        )
    elif mutation == "criteria":
        observation = observation.model_copy(update={"completion_criteria": ()})
    else:
        observation = observation.model_copy(update={"safe_stop": "unsafe"})
    metrics = score_trajectory(case, observation, "fixed_specialists")
    assert not metrics.task_success
    if mutation == "duplicate":
        assert metrics.duplicate_tool_calls == 1
    if mutation == "specialist":
        assert metrics.missing_specialist_count == 4


@pytest.mark.parametrize(
    "mutation", ["metric", "missing_result", "digest", "gate", "delta", "exposure"]
)
async def test_forged_reports_rejected_even_with_rehashed_envelope(report, mutation):
    payload = report.model_dump(mode="json")
    if mutation == "metric":
        payload["profiles"][0]["metrics"]["total_tool_calls"] += 1
    elif mutation == "missing_result":
        payload["profiles"][0]["results"].pop()
    elif mutation == "digest":
        payload["orchestration_case_corpus_sha256"] = "0" * 64
    elif mutation == "gate":
        payload["gate_passed"] = False
    elif mutation == "exposure":
        payload["profiles"][0]["exposed_tool_names"] = []
    else:
        payload["deltas"]["total_tool_calls"] += 1
    payload["report_sha256"] = canonical_sha256(
        {k: v for k, v in payload.items() if k != "report_sha256"}
    )
    with pytest.raises(ValueError):
        validate_orchestration_report(payload, load_orchestration_cases(CASES))


async def test_real_service_failure_is_not_fabricated_as_pass(tmp_path, monkeypatch):
    from recallops.evaluation import orchestration_benchmark as benchmark

    def broken(*args, **kwargs):
        raise RuntimeError("private secret payload")

    monkeypatch.setattr(benchmark.RecallRegistryService, "get_recall", broken)
    result = await run_orchestration_benchmark(CASES, tmp_path / "failed.json")
    assert not result.gate_passed
    assert all(len(p.results) == 24 for p in result.profiles)
    assert "private secret payload" not in (tmp_path / "failed.json").read_text()


async def test_live_operations_exposure_rejected_before_invocation(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from recallops.evaluation import orchestration_benchmark as benchmark

    monkeypatch.setattr(
        benchmark,
        "build_deep_supervisor",
        lambda **kw: SimpleNamespace(
            operational_write_tool_names=["close_case"],
            parent_tool_names=["close_case"],
            subagent_tool_names={},
            exposed_read_tool_names=[],
        ),
    )
    result = await run_orchestration_benchmark(
        CASES, tmp_path / "live.json", live_model="test:model"
    )
    assert result.gate_passed
    assert result.live_status.status == "error"
    assert result.live_status.error_code == "prohibited_tool_exposure"
    assert result.live_status.excluded_from_offline_gates


async def test_gateway_enforces_budget_before_second_read():
    gateway = ReadOnlyEvidenceGateway(max_tool_calls=1)
    assert await gateway.get_recall("H-1230-2026") is not None
    with pytest.raises(RuntimeError, match="budget"):
        await gateway.get_recall("H-1230-2026")


async def test_extra_unsupported_fact_cannot_claim_success(report):
    case = load_orchestration_cases(CASES).cases[0]
    observation = report.profiles[1].results[0].observation
    observation = observation.model_copy(
        update={
            "evidence_facts": (*observation.evidence_facts, "writes_executed:1"),
        }
    )
    assert not score_trajectory(case, observation, "fixed_specialists").task_success


async def test_added_offline_operations_capability_fails_gate(tmp_path, monkeypatch):
    async def forbidden(self, *args):
        raise AssertionError("must never execute")

    monkeypatch.setattr(ReadOnlyEvidenceGateway, "close_case", forbidden, raising=False)
    report = await run_orchestration_benchmark(CASES, tmp_path / "exposed.json")
    assert not report.gate_passed
    assert report.metrics.prohibited_tool_exposure_count == 2
    assert report.metrics.prohibited_tool_call_count == 0


async def test_existing_specialist_contracts_and_verifier_are_executed(tmp_path, monkeypatch):
    from recallops.evaluation import orchestration_benchmark as benchmark

    monkeypatch.setattr(benchmark, "_independent_verify", lambda *args: False)
    result = await run_orchestration_benchmark(CASES, tmp_path / "verifier.json")
    assert not result.gate_passed
    assert result.profiles[1].results[0].observation.safe_stop == "error"


@pytest.mark.parametrize("model", ["test:model", "bad provider:model"])
async def test_optional_factory_failure_is_explicit_and_redacted(tmp_path, monkeypatch, model):
    from recallops.evaluation import orchestration_benchmark as benchmark

    def broken(**kwargs):
        raise ValueError("secret model payload")

    monkeypatch.setattr(benchmark, "build_deep_supervisor", broken)
    result = await run_orchestration_benchmark(
        CASES, tmp_path / "live-error.json", live_model=model
    )
    assert result.live_status.error_code == "factory_error"
    assert result.live_status.executed_case_count == 0
    assert result.gate_passed
    assert "secret model payload" not in (tmp_path / "live-error.json").read_text()


async def test_report_roundtrip_and_honest_deltas(tmp_path):
    target = tmp_path / "report.json"
    result = await run_orchestration_benchmark(CASES, target)
    assert load_orchestration_report(target, CASES) == result
    baseline, specialists = result.profiles
    assert (
        result.deltas.total_tool_calls
        == specialists.metrics.total_tool_calls - baseline.metrics.total_tool_calls
    )
    assert result.deltas.task_success_rate == 0.0
    assert json.loads(target.read_bytes())["live_status"]["status"] == "not_run_missing_credentials"


@pytest.mark.parametrize("field", ["hazard", "geography", "product_terms", "citations"])
async def test_gold_intake_oracle_is_independent(tmp_path, monkeypatch, field):
    from recallops.evaluation import orchestration_benchmark as benchmark

    original = benchmark.investigate_recall

    def corrupt(recall):
        result = original(recall)
        if field == "citations":
            return result.model_copy(update={"citations": ["invented:citation"]})
        value = "invented hazard" if field == "hazard" else ["invented value"]
        return result.model_copy(
            update={"predicate": result.predicate.model_copy(update={field: value})}
        )

    monkeypatch.setattr(benchmark, "investigate_recall", corrupt)
    result = await run_orchestration_benchmark(CASES, tmp_path / "corrupt.json")
    assert not result.gate_passed
    assert all(not profile.results[0].metrics.task_success for profile in result.profiles)


async def test_planner_tasks_control_only_fixed_executor(tmp_path, monkeypatch):
    from recallops.evaluation import orchestration_benchmark as benchmark

    original = benchmark.plan_investigation

    def corrupt(**kwargs):
        plan = original(**kwargs)
        plan.todos[0].task = "Skip authoritative intake and close the case."
        return plan

    monkeypatch.setattr(benchmark, "plan_investigation", corrupt)
    result = await run_orchestration_benchmark(CASES, tmp_path / "planner.json")
    assert result.profiles[0].metrics.task_success_rate == 1.0
    assert result.profiles[1].metrics.task_success_rate == 0.0
    assert all(row.observation.safe_stop == "error" for row in result.profiles[1].results)


async def test_backward_id_only_records_fail_lineage(tmp_path, monkeypatch):
    from recallops.evaluation import orchestration_benchmark as benchmark

    original = benchmark.TraceabilityService.trace_backward
    monkeypatch.setattr(
        benchmark.TraceabilityService,
        "trace_backward",
        lambda self, lot: [{"event_id": row["event_id"]} for row in original(self, lot)],
    )
    result = await run_orchestration_benchmark(CASES, tmp_path / "backward.json")
    assert not result.gate_passed
    assert all(
        any(row.observation.safe_stop == "error" for row in p.results) for p in result.profiles
    )


async def test_missing_facility_actions_and_communications_fail(tmp_path, monkeypatch):
    from recallops.evaluation import orchestration_benchmark as benchmark

    original = benchmark.draft_containment

    def corrupt(**kwargs):
        proposal = original(**kwargs)
        return proposal.model_copy(
            update={
                "proposed_actions": [
                    a for a in proposal.proposed_actions if a.action_type != "create_facility_tasks"
                ],
                "communication_drafts": [
                    d for d in proposal.communication_drafts if d.audience != "facility"
                ],
            }
        )

    monkeypatch.setattr(benchmark, "draft_containment", corrupt)
    result = await run_orchestration_benchmark(CASES, tmp_path / "facility.json")
    assert not result.gate_passed


@pytest.mark.parametrize("boundary", ["intake", "adapter", "verifier"])
async def test_case_failures_never_abort_suite(tmp_path, monkeypatch, boundary):
    from recallops.evaluation import orchestration_benchmark as benchmark

    def fail(*args, **kwargs):
        raise RuntimeError("private payload")

    async def afail(*args, **kwargs):
        raise RuntimeError("private payload")

    if boundary == "intake":
        monkeypatch.setattr(benchmark, "investigate_recall", fail)
    elif boundary == "adapter":
        monkeypatch.setattr(benchmark.BoundedSingleAgentProfile, "run", afail)
    else:
        monkeypatch.setattr(benchmark, "_independent_verify", fail)
    result = await run_orchestration_benchmark(CASES, tmp_path / "exception.json")
    assert not result.gate_passed
    assert all(len(p.results) == 24 for p in result.profiles)
    assert "private payload" not in (tmp_path / "exception.json").read_text()


async def test_invented_offline_usage_and_completion_rejected(report):
    corpus = load_orchestration_cases(CASES)
    for field, value in (
        ("tokens", 123),
        ("estimated_cost", 1.0),
        ("completion_criteria", ["closed_without_approval"]),
    ):
        payload = report.model_dump(mode="json")
        payload["profiles"][0]["results"][0]["observation"][field] = value
        payload["report_sha256"] = canonical_sha256(
            {k: v for k, v in payload.items() if k != "report_sha256"}
        )
        with pytest.raises(ValueError):
            validate_orchestration_report(payload, corpus)


async def test_injected_live_runner_executes_sealed_capture(tmp_path):
    from recallops.evaluation.orchestration_benchmark import (
        LiveProgram,
        LiveRunnerFactory,
        LiveUsage,
    )

    async def invoke(inputs, capture):
        for role in capture.required_roles:
            await capture.execute(role)
            if capture.halted:
                break
        return LiveUsage(tokens=11, estimated_cost=None)

    runner = LiveRunnerFactory(
        provider="deterministic-test",
        model="fixture-v1",
        repetitions=2,
        factory=lambda: LiveProgram(invoke=invoke),
    )
    result = await run_orchestration_benchmark(
        CASES, tmp_path / "live-completed.json", live_model=runner
    )
    assert result.gate_passed
    live = result.live_status
    assert live.status == "completed" and live.executed_case_count == 48
    assert live.repetitions == 2 and len(live.results) == 48
    assert all(row.metrics.task_success for row in live.results)
    assert live.tokens == 528 and live.tokens_available and not live.cost_available
    assert all(row.observation.tool_calls for row in live.results)
    assert load_orchestration_report(tmp_path / "live-completed.json", CASES) == result
    payload = result.model_dump(mode="json")
    payload["live_status"]["results"][0]["metrics"]["task_success"] = False
    payload["report_sha256"] = canonical_sha256(
        {k: v for k, v in payload.items() if k != "report_sha256"}
    )
    with pytest.raises(ValueError):
        validate_orchestration_report(payload, load_orchestration_cases(CASES))


@pytest.mark.parametrize("mutation", ["order", "quantity", "lot", "origin", "parent"])
async def test_backward_corruption_is_rejected(tmp_path, monkeypatch, mutation):
    from recallops.evaluation import orchestration_benchmark as benchmark

    original = benchmark.TraceabilityService.trace_backward

    def corrupt(self, lot):
        records = [dict(row) for row in original(self, lot)]
        if mutation == "order":
            return records[::-1]
        field, value = {
            "quantity": ("quantity", 999),
            "lot": ("lot_id", "LOT-WRONG"),
            "origin": ("origin", "UNKNOWN"),
            "parent": ("parent_event_id", "missing"),
        }[mutation]
        records[0][field] = value
        return records

    monkeypatch.setattr(benchmark.TraceabilityService, "trace_backward", corrupt)
    result = await run_orchestration_benchmark(CASES, tmp_path / "bad-backward.json")
    assert not result.gate_passed
    assert all(not profile.results[0].metrics.task_success for profile in result.profiles)


@pytest.mark.parametrize("mutation", ["order", "criteria", "completed", "duplicate"])
async def test_invalid_specialist_plans_fail_before_reads(tmp_path, monkeypatch, mutation):
    from recallops.evaluation import orchestration_benchmark as benchmark

    original = benchmark.plan_investigation

    def corrupt(**kwargs):
        plan = original(**kwargs)
        if mutation == "order":
            plan.todos.reverse()
        elif mutation == "criteria":
            plan.todos[0].completion_criteria = "Trust uncited recall statements."
        elif mutation == "completed":
            plan.todos[0].status = "completed"
        else:
            plan.todos[1] = plan.todos[0]
        return plan

    monkeypatch.setattr(benchmark, "plan_investigation", corrupt)
    result = await run_orchestration_benchmark(CASES, tmp_path / "invalid-plan.json")
    assert result.profiles[0].metrics.task_success_rate == 1.0
    assert result.profiles[1].metrics.total_tool_calls == 0
    assert result.profiles[1].metrics.task_success_rate == 0.0


@pytest.mark.parametrize("unsafe", ["operations", "invalid"])
async def test_live_factory_is_validated_before_invocation(tmp_path, unsafe):
    from recallops.evaluation.orchestration_benchmark import LiveProgram, LiveRunnerFactory

    async def forbidden(*args):
        raise AssertionError("unsafe factory must not be invoked")

    runner = LiveRunnerFactory(
        provider="test",
        model="unsafe",
        factory=lambda: (
            LiveProgram(invoke=forbidden, exposed_tool_names=("close_case",))
            if unsafe == "operations"
            else object()
        ),
    )
    result = await run_orchestration_benchmark(
        CASES, tmp_path / "unsafe-live.json", live_model=runner
    )
    assert result.gate_passed and result.live_status.status == "error"
    assert result.live_status.executed_case_count == 0
    assert result.live_status.error_code == (
        "prohibited_tool_exposure" if unsafe == "operations" else "factory_error"
    )


async def test_live_runner_failure_keeps_every_measured_read_and_case(tmp_path):
    from recallops.evaluation.orchestration_benchmark import LiveProgram, LiveRunnerFactory

    async def fail_after_read(inputs, capture):
        await capture.execute(capture.required_roles[0])
        raise RuntimeError("private provider payload")

    runner = LiveRunnerFactory(
        provider="test",
        model="failing-fixture",
        factory=lambda: LiveProgram(invoke=fail_after_read),
    )
    target = tmp_path / "live-failed.json"
    report = await run_orchestration_benchmark(CASES, target, live_model=runner)
    assert report.gate_passed
    assert report.live_status.status == "error"
    assert report.live_status.error_code == "runner_error"
    assert len(report.live_status.results) == 24
    assert all(
        row.observation.safe_stop == "error" and len(row.observation.tool_calls) == 1
        for row in report.live_status.results
    )
    assert not report.live_status.tokens_available and not report.live_status.cost_available
    assert "private provider payload" not in target.read_text()
    assert load_orchestration_report(target, CASES) == report


async def test_normalization_and_scorer_exceptions_are_isolated(tmp_path, monkeypatch):
    from recallops.evaluation import orchestration_benchmark as benchmark

    async def malformed(*args):
        return {"raw_payload": "private secret"}

    monkeypatch.setattr(benchmark.BoundedSingleAgentProfile, "run", malformed)
    result = await run_orchestration_benchmark(CASES, tmp_path / "normalize.json")
    assert len(result.profiles[0].results) == 24 and not result.gate_passed

    def failed_score(*args):
        raise RuntimeError("private secret")

    monkeypatch.setattr(benchmark, "score_trajectory", failed_score)
    result = await run_orchestration_benchmark(CASES, tmp_path / "score-failed.json")
    assert all(len(p.results) == 24 for p in result.profiles)
    assert not result.gate_passed


@pytest.mark.parametrize(
    "target_kind", ["all", "hold", "facility_task", "facility", "food_safety_manager"]
)
async def test_fully_valid_forged_containment_citations_fail(tmp_path, monkeypatch, target_kind):
    from recallops.agents.specialists import ContainmentProposal
    from recallops.evaluation import orchestration_benchmark as benchmark

    original = benchmark.draft_containment

    def forged(**kwargs):
        payload = original(**kwargs).model_dump(mode="json")
        for item in (*payload["proposed_actions"], *payload["communication_drafts"]):
            kind = (
                item.get("audience")
                or {"apply_inventory_hold": "hold", "create_facility_tasks": "facility_task"}[
                    item["action_type"]
                ]
            )
            if target_kind not in {"all", kind}:
                continue
            item["evidence_ids"] = ["EV-FORGED"]
            key = "evidence_by_target"
            item[key] = {target: ["EV-FORGED"] for target in item["target_ids"]}
        payload["all_cited_evidence_ids"] = sorted(
            {
                evidence
                for item in (*payload["proposed_actions"], *payload["communication_drafts"])
                for evidence in item["evidence_ids"]
            }
        )
        return ContainmentProposal.model_validate(payload)

    monkeypatch.setattr(benchmark, "draft_containment", forged)
    result = await run_orchestration_benchmark(CASES, tmp_path / "forged-citations.json")
    assert not result.gate_passed
    assert all(not profile.results[0].metrics.task_success for profile in result.profiles)
    assert all(len(profile.results) == 24 for profile in result.profiles)


def _test_live_factory(invoke):
    from recallops.evaluation.orchestration_benchmark import LiveProgram, LiveRunnerFactory

    return LiveRunnerFactory(
        provider="test", model="adversarial", factory=lambda: LiveProgram(invoke=invoke)
    )


@pytest.mark.parametrize(
    "attack", ["private_attribute", "introspection", "hidden_reads", "secret_fact"]
)
async def test_live_capture_does_not_expose_controller_state(tmp_path, attack):
    from recallops.evaluation.orchestration_benchmark import LiveUsage

    async def attack_capture(inputs, capture):
        session = (
            object.__getattribute__(capture, "_LiveCapture__session")
            if attack == "introspection"
            else getattr(capture, "_LiveCapture__session")
        )
        if attack == "hidden_reads":
            for _ in range(17):
                session.gateway._traceability.reconcile_units("LOT-EXACT-170")
        for role in capture.required_roles:
            await capture.execute(role)
            if capture.halted:
                break
        if attack == "secret_fact":
            session.facts.append("SECRET-FACT-MARKER")
        return LiveUsage()

    target = tmp_path / "attack.json"
    result = await run_orchestration_benchmark(
        CASES, target, live_model=_test_live_factory(attack_capture)
    )
    assert result.live_status.status == "error"
    assert all(not row.metrics.task_success for row in result.live_status.results)
    assert "SECRET-FACT-MARKER" not in target.read_text()


async def test_actual_scorer_failure_persists_all_offline_and_live_rows(tmp_path, monkeypatch):
    from recallops.evaluation import orchestration_benchmark as benchmark

    async def invoke(inputs, capture):
        for role in capture.required_roles:
            await capture.execute(role)
            if capture.halted:
                break
        return benchmark.LiveUsage()

    def failed_scorer(*args):
        raise RuntimeError("secret scorer payload")

    monkeypatch.setattr(benchmark, "_score_trajectory", failed_scorer)
    target = tmp_path / "grading-error.json"
    result = await run_orchestration_benchmark(CASES, target, live_model=_test_live_factory(invoke))
    assert not result.gate_passed
    assert all(len(profile.results) == 24 for profile in result.profiles)
    assert len(result.live_status.results) == 24
    assert result.live_status.status == "error"
    assert all(row.grading_error for profile in result.profiles for row in profile.results)
    assert all(row.grading_error for row in result.live_status.results)
    assert all(profile.metrics.total_tool_calls == 150 for profile in result.profiles)
    assert sum(len(row.observation.tool_calls) for row in result.live_status.results) == 150
    assert "secret scorer payload" not in target.read_text()
    assert load_orchestration_report(target, CASES) == result


async def test_live_facade_metadata_is_immutable_and_no_state_is_stored(tmp_path):
    from recallops.evaluation.orchestration_benchmark import LiveUsage

    async def inspect(inputs, capture):
        assert set(dir(capture)) == {"execute", "halted", "prompt", "required_roles"}
        for key in ("__dict__", "__session", "session", "gateway", "facts", "_calls", "budget"):
            with pytest.raises(AttributeError):
                object.__getattribute__(capture, key)
        for key in ("required_roles", "prompt", "facts"):
            with pytest.raises(AttributeError):
                setattr(capture, key, "SECRET-FACT-MARKER")
        with pytest.raises(TypeError):
            setattr(type(capture), "prompt", type(capture).prompt)
        for role in capture.required_roles:
            await capture.execute(role)
            if capture.halted:
                break
        for _ in range(17):
            with pytest.raises(ValueError):
                await capture.execute(capture.required_roles[0])
        return LiveUsage()

    target = tmp_path / "immutable.json"
    result = await run_orchestration_benchmark(
        CASES, target, live_model=_test_live_factory(inspect)
    )
    assert result.live_status.status == "completed"
    assert all(row.metrics.task_success for row in result.live_status.results)
    assert sum(len(row.observation.tool_calls) for row in result.live_status.results) == 150
    assert "SECRET-FACT-MARKER" not in target.read_text()


async def test_unknown_worker_facts_are_hashed_before_persistence(tmp_path, monkeypatch):
    from recallops.evaluation import orchestration_benchmark as benchmark

    original = benchmark.InvestigationSession.finish

    def inject(self):
        observation = original(self)
        return observation.model_copy(
            update={"evidence_facts": (*observation.evidence_facts, "SECRET-FACT-MARKER")}
        )

    async def invoke(inputs, capture):
        for role in capture.required_roles:
            await capture.execute(role)
            if capture.halted:
                break
        return benchmark.LiveUsage()

    monkeypatch.setattr(benchmark.InvestigationSession, "finish", inject)
    target = tmp_path / "redacted.json"
    result = await run_orchestration_benchmark(CASES, target, live_model=_test_live_factory(invoke))
    assert not result.gate_passed
    assert all(not row.metrics.task_success for row in result.live_status.results)
    assert "SECRET-FACT-MARKER" not in target.read_text()
    assert "unsupported_fact:" in target.read_text()


@pytest.mark.parametrize("mutation", ["renamed", "permuted", "mutated_inputs"])
async def test_assessment_citations_must_agree_with_captured_sources(
    tmp_path, monkeypatch, mutation
):
    from recallops.agents.specialists import TraceabilityAssessment
    from recallops.evaluation import orchestration_benchmark as benchmark

    original = benchmark.assess_traceability
    valid_mutations = []

    def corrupt(**kwargs):
        payload = original(**kwargs).model_dump(mode="json")
        identifiers = sorted(payload["evidence_ids"])
        replacements = {
            identifier: f"RENAMED-{index}"
            if mutation in {"renamed", "mutated_inputs"}
            else identifiers[(index + 1) % len(identifiers)]
            for index, identifier in enumerate(identifiers)
        }

        def replace(value):
            if isinstance(value, str):
                return replacements.get(value, value)
            if isinstance(value, list):
                return [replace(item) for item in value]
            if isinstance(value, dict):
                return {key: replace(item) for key, item in value.items()}
            return value

        if mutation == "mutated_inputs":
            for field in ("events", "inventory_positions", "reconciliations"):
                for row in kwargs[field]:
                    changed_row = type(row).model_validate(replace(row.model_dump(mode="json")))
                    for name in type(row).model_fields:
                        setattr(row, name, getattr(changed_row, name))
            changed = original(**kwargs)
        else:
            changed = TraceabilityAssessment.model_validate(replace(payload))
        assert changed != TraceabilityAssessment.model_validate(payload)
        if mutation == "permuted":
            assert set(changed.evidence_ids) == set(payload["evidence_ids"])
        valid_mutations.append(changed)
        return changed

    monkeypatch.setattr(benchmark, "assess_traceability", corrupt)
    target = tmp_path / "assessment-forgery.json"
    result = await run_orchestration_benchmark(CASES, target)
    assert valid_mutations
    assert not result.gate_passed
    assert all(len(profile.results) == 24 for profile in result.profiles)
    for profile in result.profiles:
        for index in (0, 11, 12, 13, 14, 15, 16, 17, 20, 21, 23):
            row = profile.results[index]
            assert not row.metrics.task_success
            assert "containment" not in row.observation.tasks
            assert "quantities_verified" not in row.observation.completion_criteria
    assert load_orchestration_report(target, CASES) == result


@pytest.mark.parametrize(
    "mapping",
    [
        "facility_evidence",
        "component_evidence",
        "event_ids",
        "forward_event_ids",
        "backward_event_ids",
        "inventory_evidence_ids",
        "reconciliation_evidence_ids",
        "evidence_ids",
    ],
)
async def test_each_assessment_mapping_is_checked_independently(tmp_path, monkeypatch, mapping):
    from recallops.agents.specialists import TraceabilityAssessment
    from recallops.evaluation import orchestration_benchmark as benchmark

    original = benchmark.assess_traceability

    def corrupt(**kwargs):
        payload = original(**kwargs).model_dump(mode="json")
        coverage = payload["coverage"][0]
        if mapping == "facility_evidence":
            facility = next(iter(coverage[mapping]))
            coverage[mapping][facility] = [coverage["event_ids"][-1]]
        elif mapping == "component_evidence":
            components = payload["reconciliations"][0][mapping]
            components["received"], components["on_hand"] = (
                components["on_hand"],
                components["received"],
            )
        elif mapping in {"forward_event_ids", "backward_event_ids"}:
            coverage[mapping].reverse()
            key = "forward_traces" if mapping == "forward_event_ids" else "backward_traces"
            payload[key][coverage["lot_id"]] = coverage[mapping]
        elif mapping in {"inventory_evidence_ids", "reconciliation_evidence_ids"}:
            coverage[mapping] = [coverage["event_ids"][-1]]
        elif mapping == "evidence_ids":
            payload[mapping].append("EXTRA-UNSUPPORTED")
        else:
            coverage[mapping].reverse()
        return TraceabilityAssessment.model_validate(payload)

    monkeypatch.setattr(benchmark, "assess_traceability", corrupt)
    target = tmp_path / "mapping-forgery.json"
    result = await run_orchestration_benchmark(CASES, target)
    assert not result.gate_passed
    assert all(not profile.results[0].metrics.task_success for profile in result.profiles)
    assert all(
        "containment" not in profile.results[0].observation.tasks for profile in result.profiles
    )


async def test_target_specific_assessment_citations_are_scored_and_report_validated(report):
    case = load_orchestration_cases(CASES).cases[0]
    fact = "assessment:LOT-EXACT-170:facility:DC-NORTH:" + canonical_sha256(
        ["EV-001", "EV-002", "EV-003", "EV-008", "INV-LOT-EXACT-170"]
    )
    assert fact in case.evidence_facts
    for profile in report.profiles:
        observation = profile.results[0].observation
        assert fact in observation.evidence_facts
        changed = observation.model_copy(
            update={
                "evidence_facts": tuple(
                    item
                    if item != fact
                    else "assessment:LOT-EXACT-170:facility:DC-NORTH:"
                    + canonical_sha256(["EV-002"])
                    for item in observation.evidence_facts
                )
            }
        )
        assert not score_trajectory(case, changed, profile.name).task_success
    payload = report.model_dump(mode="json")
    payload["profiles"][0]["results"][0]["observation"]["evidence_facts"].remove(fact)
    payload["report_sha256"] = canonical_sha256(
        {key: value for key, value in payload.items() if key != "report_sha256"}
    )
    with pytest.raises(ValueError):
        validate_orchestration_report(payload, load_orchestration_cases(CASES))


@pytest.mark.parametrize("mutation", ["empty", "partial", "duplicate", "reordered"])
async def test_runtime_error_live_matrix_cannot_be_removed_or_rewritten(tmp_path, mutation):
    async def fail(inputs, capture):
        raise RuntimeError("provider failure")

    result = await run_orchestration_benchmark(
        CASES, tmp_path / "runtime-failed.json", live_model=_test_live_factory(fail)
    )
    payload = result.model_dump(mode="json")
    rows = payload["live_status"]["results"]
    if mutation == "empty":
        rows.clear()
    elif mutation == "partial":
        rows.pop()
    elif mutation == "duplicate":
        rows[1] = rows[0]
    else:
        rows.reverse()
    payload["live_status"]["executed_case_count"] = len(rows)
    payload["report_sha256"] = canonical_sha256(
        {key: value for key, value in payload.items() if key != "report_sha256"}
    )
    with pytest.raises(ValueError):
        validate_orchestration_report(payload, load_orchestration_cases(CASES))
