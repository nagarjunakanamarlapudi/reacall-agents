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
