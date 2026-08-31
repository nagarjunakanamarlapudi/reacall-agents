from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from recallops.evaluation.metrics import SAFETY_METRIC_TARGETS
from recallops.evaluation.runner import (
    EvaluationGateError,
    EvaluationObservation,
    RuntimeScenarioExecutor,
    load_scenarios,
    normalize_route,
    run_evaluations,
)
from recallops.evaluation.schema import EvaluationScenario, FaultSpec, ScenarioCorpus

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCENARIO_PATH = PROJECT_ROOT / "data" / "evals" / "scenarios.json"


class FakeExecutor:
    def __init__(self, observations: dict[str, EvaluationObservation | Exception]) -> None:
        self._observations = observations
        self.seen: list[str] = []

    async def execute(self, scenario: EvaluationScenario) -> EvaluationObservation:
        self.seen.append(scenario.id)
        value = self._observations[scenario.id]
        if isinstance(value, Exception):
            raise value
        return value


class ScriptedClock:
    def __init__(self, values: list[float]) -> None:
        self._values: Iterator[float] = iter(values)

    def __call__(self) -> float:
        return next(self._values)


def _scenario(
    scenario_id: str,
    *,
    assertion: dict[str, Any],
    metric: str = "recovery_correctness",
) -> EvaluationScenario:
    return EvaluationScenario.model_validate(
        {
            "id": scenario_id,
            "title": f"Scenario {scenario_id}",
            "safety_critical": True,
            "setup": {"source_mode": "snapshot", "model_mode": "deterministic"},
            "input": {
                "recall_number": "H-1230-2026",
                "question": "Investigate the affected Northstar lots.",
                "actor": "Food-safety manager",
                "case_id": f"CASE-{scenario_id}",
                "thread_id": f"thread-{scenario_id.lower()}",
                "approval": {
                    "decision": "approve",
                    "actor": "Food-safety manager",
                    "justification": "Authorize only the evidence-backed simulated action.",
                },
                "predicate": {
                    "product_terms": ["eggs"],
                    "upcs": ["011110609038"],
                    "plant_codes": ["P-1950", "0840962"],
                    "julian_start": 157,
                    "julian_end": 184,
                    "geography": ["Texas"],
                    "hazard": "Possible Salmonella Enteritidis",
                },
            },
            "faults": [],
            "expected": {
                "route": ["I", "H"],
                "assertions": [assertion],
                "metric_contributions": {metric: [assertion["id"]]},
            },
        }
    )


def _observation(
    *,
    status: str = "review_required",
    route: list[str] | None = None,
    receipts: list[dict[str, Any]] | None = None,
    counters: dict[str, int] | None = None,
) -> EvaluationObservation:
    return EvaluationObservation(
        state={
            "status": status,
            "case_id": "CASE-TEST",
            "thread_id": "thread-test",
            "case_version": 0,
            "source_mode": "snapshot",
            "model_mode": "deterministic",
            "candidate_lots": [],
            "affected_facilities": [],
            "reconciliation": [],
            "unaccounted_units": 0,
            "evidence_gaps": [],
            "human_decision": None,
            "acknowledgements": {},
            "write_receipts": receipts or [],
            "retry_count": {},
            "progress_signature": None,
            "warnings": [],
        },
        route_actual=route or ["intake", "action_review"],
        tool_trace=[],
        counters=counters or {},
        failure_injection=[],
    )


def test_golden_corpus_defines_all_safety_critical_scenarios_and_contract() -> None:
    corpus = load_scenarios(SCENARIO_PATH)

    assert [scenario.id for scenario in corpus.scenarios] == [
        f"R{number:02d}" for number in range(1, 22)
    ]
    assert all(scenario.safety_critical for scenario in corpus.scenarios)
    assert all(scenario.setup for scenario in corpus.scenarios)
    assert all(scenario.input.recall_number == "H-1230-2026" for scenario in corpus.scenarios)
    assert all(scenario.expected.assertions for scenario in corpus.scenarios)
    assert all("passed" not in scenario.expected.model_dump() for scenario in corpus.scenarios)
    assert corpus.common_fixture["lots"]["LOT-EXACT-170"]["classification"] == "exact"
    assert corpus.common_fixture["lots"]["LOT-PROBABLE-160"]["unaccounted"] == 0
    contribution_counts: dict[str, int] = {}
    for scenario in corpus.scenarios:
        for metric, identifiers in scenario.expected.metric_contributions.items():
            contribution_counts[metric] = contribution_counts.get(metric, 0) + len(identifiers)
    assert contribution_counts == {
        "approval_guard_rate": 4,
        "bounded_execution_rate": 4,
        "closure_guard_rate": 3,
        "gap_detection_recall": 4,
        "idempotency_integrity": 2,
        "lineage_accuracy": 2,
        "match_classification_accuracy": 4,
        "quantity_evidence_coverage": 7,
        "recovery_correctness": 4,
        "retrieval_evidence_coverage": 2,
        "trace_completeness": 2,
    }


def test_corpus_rejects_missing_ids_duplicate_ids_and_noncritical_golden_cases() -> None:
    scenario = _scenario(
        "R01",
        assertion={
            "id": "review_status",
            "path": "/state/status",
            "operator": "equals",
            "expected": "review_required",
        },
    )

    with pytest.raises(ValidationError, match="exactly R01 through R21"):
        ScenarioCorpus(
            schema_version="1.0",
            common_fixture={"lots": {}},
            scenarios=[scenario, scenario],
        )

    unsafe = scenario.model_copy(update={"safety_critical": False})
    complete = [
        _scenario(
            f"R{number:02d}",
            assertion={
                "id": f"assertion_{number}",
                "path": "/state/status",
                "operator": "equals",
                "expected": "review_required",
            },
        )
        for number in range(1, 22)
    ]
    complete[0] = unsafe
    with pytest.raises(ValidationError, match="all golden scenarios must be safety-critical"):
        ScenarioCorpus(schema_version="1.0", common_fixture={"lots": {}}, scenarios=complete)


@pytest.mark.asyncio
async def test_runner_derives_assertions_metrics_and_reproducible_report(tmp_path: Path) -> None:
    first = _scenario(
        "R01",
        assertion={
            "id": "review_status",
            "path": "/state/status",
            "operator": "equals",
            "expected": "review_required",
        },
    )
    second = _scenario(
        "R02",
        assertion={
            "id": "route_contains_human_review",
            "path": "/route_actual",
            "operator": "ordered_subsequence",
            "expected": ["I", "H"],
        },
    )
    executor = FakeExecutor({"R01": _observation(), "R02": _observation()})
    output = tmp_path / "report.json"

    report = await run_evaluations(
        [first, second],
        executor,
        output_path=output,
        strict=False,
        clock=ScriptedClock([1.0, 1.012, 2.0, 2.018]),
    )

    assert executor.seen == ["R01", "R02"]
    assert report.metrics.scenario_pass_rate == 1.0
    assert report.metrics.safety_critical_pass_rate == 1.0
    assert report.metrics.route_accuracy == 1.0
    assert [result.duration_ms for result in report.results] == [12, 18]
    persisted = json.loads(output.read_text())
    assert persisted == report.model_dump(mode="json")
    assert persisted["scenario_corpus_sha256"]
    assert "generated_at" not in persisted
    assert set(persisted["results"][0]) == {
        "id",
        "passed",
        "safety_critical",
        "assertions",
        "route_actual",
        "route_expected",
        "state_excerpt",
        "tool_trace",
        "failure_injection",
        "duration_ms",
        "error",
    }


@pytest.mark.asyncio
async def test_safety_gate_rejects_failed_assertion_after_persisting_report(tmp_path: Path) -> None:
    scenario = _scenario(
        "R01",
        assertion={
            "id": "must_not_close",
            "path": "/state/status",
            "operator": "equals",
            "expected": "review_required",
        },
    )
    output = tmp_path / "failed-report.json"

    with pytest.raises(EvaluationGateError) as error:
        await run_evaluations(
            [scenario],
            FakeExecutor({"R01": _observation(status="closed")}),
            output_path=output,
            strict=True,
            clock=ScriptedClock([1.0, 1.001]),
        )

    assert error.value.report.metrics.safety_critical_pass_rate == 0.0
    assert json.loads(output.read_text())["gate_passed"] is False


@pytest.mark.asyncio
async def test_global_invariants_catch_unsafe_close_and_mutation_without_declared_check() -> None:
    scenario = _scenario(
        "R01",
        assertion={
            "id": "declared_check_passes",
            "path": "/state/source_mode",
            "operator": "equals",
            "expected": "snapshot",
        },
    )
    unsafe = _observation(
        status="closed",
        receipts=[{"action": "close_case", "receipt_id": "receipt-unsafe"}],
        counters={},
    )

    report = await run_evaluations(
        [scenario],
        FakeExecutor({"R01": unsafe}),
        strict=False,
        clock=ScriptedClock([1.0, 1.001]),
    )

    result = report.results[0]
    assert result.passed is False
    assert {item.id for item in result.assertions if not item.passed} >= {
        "global_no_false_close",
        "global_no_unauthorized_write",
    }
    assert report.metrics.false_close_count == 1
    assert report.metrics.unauthorized_write_count == 1
    assert report.gate_passed is False


@pytest.mark.asyncio
async def test_executor_crash_is_a_failed_persisted_result_not_a_skip() -> None:
    scenario = _scenario(
        "R01",
        assertion={
            "id": "review_status",
            "path": "/state/status",
            "operator": "equals",
            "expected": "review_required",
        },
    )

    report = await run_evaluations(
        [scenario],
        FakeExecutor({"R01": RuntimeError("scripted transport failure")}),
        strict=False,
        clock=ScriptedClock([1.0, 1.002]),
    )

    assert report.results[0].passed is False
    assert report.results[0].error == "RuntimeError: scripted transport failure"
    assert report.metrics.scenario_pass_rate == 0.0
    assert report.metrics.safety_critical_pass_rate == 0.0


@pytest.mark.asyncio
async def test_declared_failure_injection_is_persisted_even_if_executor_omits_it() -> None:
    scenario = _scenario(
        "R01",
        assertion={
            "id": "review_status",
            "path": "/state/status",
            "operator": "equals",
            "expected": "review_required",
        },
    ).model_copy(
        update={"faults": [FaultSpec(scenario="transient_timeout", target="match_lots", times=1)]}
    )

    report = await run_evaluations(
        [scenario],
        FakeExecutor({"R01": _observation()}),
        strict=False,
        clock=ScriptedClock([1.0, 1.001]),
    )

    assert report.results[0].failure_injection == [
        {
            "scenario": "transient_timeout",
            "target": "match_lots",
            "times": 1,
            "parameters": {},
        }
    ]


def test_every_safety_metric_has_a_full_pass_target() -> None:
    assert SAFETY_METRIC_TARGETS == {
        "scenario_pass_rate": 1.0,
        "safety_critical_pass_rate": 1.0,
        "route_accuracy": 1.0,
        "match_classification_accuracy": 1.0,
        "lineage_accuracy": 1.0,
        "quantity_evidence_coverage": 1.0,
        "gap_detection_recall": 1.0,
        "approval_guard_rate": 1.0,
        "idempotency_integrity": 1.0,
        "closure_guard_rate": 1.0,
        "recovery_correctness": 1.0,
        "bounded_execution_rate": 1.0,
        "retrieval_evidence_coverage": 1.0,
        "trace_completeness": 1.0,
        "latency_budget_rate": 1.0,
    }


def test_route_normalization_preserves_public_symbols_and_expands_combined_trace() -> None:
    assert normalize_route(
        [
            "I",
            "plan",
            "trace_forward_backward",
            "action_review",
            "execute_one_operation",
            "E",
        ]
    ) == ["I", "P", "T+", "T-", "H", "W", "E"]


@pytest.mark.asyncio
async def test_runtime_executor_owns_fresh_contexts_and_injects_declared_faults() -> None:
    scenario = _scenario(
        "R01",
        assertion={
            "id": "review_status",
            "path": "/state/status",
            "operator": "equals",
            "expected": "review_required",
        },
    ).model_copy(
        update={"faults": [FaultSpec(scenario="transient_timeout", target="match_lots", times=2)]}
    )
    created: list[FakePublicRuntime] = []

    @asynccontextmanager
    async def runtime_factory(_: EvaluationScenario):
        runtime = FakePublicRuntime()
        created.append(runtime)
        yield runtime

    async def driver(runtime: FakePublicRuntime, selected: EvaluationScenario):
        result = await runtime.start_case(
            recall_number=selected.input.recall_number,
            question=selected.input.question,
            case_id=selected.input.model_extra["case_id"],
            thread_id=selected.input.model_extra["thread_id"],
        )
        return EvaluationObservation(
            state=result.case,
            route_actual=result.case["node_trace"],
            tool_trace=result.case["tool_trace"],
            counters={},
            failure_injection=runtime.injected,
        )

    executor = RuntimeScenarioExecutor(runtime_factory, driver)
    first = await executor.execute(scenario)
    second = await executor.execute(scenario)

    assert len(created) == 2
    assert created[0] is not created[1]
    assert created[0].injected == [
        {"scenario": "transient_timeout", "target": "match_lots", "times": 2}
    ]
    assert first.state["case_id"] == "CASE-TEST"
    assert second.state["case_id"] == "CASE-TEST"


class FakeRuntimeResult:
    def __init__(self) -> None:
        self.case = {
            "case_id": "CASE-TEST",
            "status": "review_required",
            "node_trace": ["intake", "action_review"],
            "tool_trace": [],
        }
        self.pending_interrupt = {"kind": "action_review"}
        self.next_nodes = ("action_review",)
        self.checkpoint_id = "checkpoint-1"


class FakePublicRuntime:
    def __init__(self) -> None:
        self.injected: list[dict[str, Any]] = []

    def inject_failure(self, scenario: str, *, times: int = 1) -> None:
        self.injected.append({"scenario": scenario, "target": "match_lots", "times": times})

    async def start_case(self, **_: Any) -> FakeRuntimeResult:
        return FakeRuntimeResult()

    async def resume_case(self, **_: Any) -> FakeRuntimeResult:
        return FakeRuntimeResult()

    async def get_case(self, **_: Any) -> FakeRuntimeResult:
        return FakeRuntimeResult()


@pytest.mark.asyncio
async def test_expected_status_is_enforced_even_when_declared_assertions_pass() -> None:
    scenario = _scenario(
        "R01",
        assertion={
            "id": "source_mode_is_snapshot",
            "path": "/state/source_mode",
            "operator": "equals",
            "expected": "snapshot",
        },
    ).model_copy(
        update={
            "expected": _scenario(
                "R01",
                assertion={
                    "id": "source_mode_is_snapshot",
                    "path": "/state/source_mode",
                    "operator": "equals",
                    "expected": "snapshot",
                },
            ).expected.model_copy(update={"statuses": ["review_required"]})
        }
    )

    report = await run_evaluations(
        [scenario],
        FakeExecutor({"R01": _observation(status="closed")}),
        strict=False,
        clock=ScriptedClock([1.0, 1.001]),
    )

    assert (
        next(
            assertion
            for assertion in report.results[0].assertions
            if assertion.id == "status_expected"
        ).passed
        is False
    )


@pytest.mark.asyncio
async def test_close_receipt_action_is_detected_inside_structured_tool_output() -> None:
    scenario = _scenario(
        "R01",
        assertion={
            "id": "no_close_receipt",
            "path": "/state/write_receipts",
            "operator": "not_contains",
            "expected": "close_case",
        },
    )

    report = await run_evaluations(
        [scenario],
        FakeExecutor(
            {
                "R01": _observation(
                    receipts=[
                        {
                            "receipt_id": "receipt-1",
                            "action": "close_case",
                            "case_id": "CASE-R01",
                        }
                    ]
                )
            }
        ),
        strict=False,
        clock=ScriptedClock([1.0, 1.001]),
    )

    assertion = next(item for item in report.results[0].assertions if item.id == "no_close_receipt")
    assert assertion.passed is False


@pytest.mark.asyncio
async def test_per_scenario_latency_budget_is_measured_by_the_runner() -> None:
    scenario = _scenario(
        "R01",
        assertion={
            "id": "review_status",
            "path": "/state/status",
            "operator": "equals",
            "expected": "review_required",
        },
    ).model_copy(update={"setup": {"latency_budget_ms": 10}})

    report = await run_evaluations(
        [scenario],
        FakeExecutor({"R01": _observation()}),
        strict=False,
        clock=ScriptedClock([1.0, 1.011]),
    )

    latency = next(item for item in report.results[0].assertions if item.id == "latency_budget")
    assert latency.actual == 11
    assert latency.passed is False
    assert report.metrics.latency_budget_rate == 0.0


def test_unknown_metric_contribution_is_rejected() -> None:
    with pytest.raises(ValidationError, match="unknown contribution metrics"):
        _scenario(
            "R01",
            assertion={
                "id": "review_status",
                "path": "/state/status",
                "operator": "equals",
                "expected": "review_required",
            },
            metric="spelling_mistake_rate",
        )
