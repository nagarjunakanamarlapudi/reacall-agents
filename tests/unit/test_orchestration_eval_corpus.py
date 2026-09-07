"""Corpus integrity and trajectory falsification contracts."""

import json

import pytest
from pydantic import ValidationError

from recallops.evaluation.digests import canonical_json_bytes
from recallops.evaluation.orchestration_schema import (
    LiveProfileStatus,
    OrchestrationEvalCorpus,
    ProfileMetrics,
    TrajectoryObservation,
    load_orchestration_cases,
)
from recallops.paths import DATA_DIR

CASES = DATA_DIR / "evals" / "orchestration_cases.json"


def test_orchestration_corpus_has_24_bounded_cases():
    corpus = load_orchestration_cases(CASES)
    assert len(corpus.cases) == 24
    assert len({case.id for case in corpus.cases}) == 24
    assert len({case.family for case in corpus.cases}) == 7
    assert all(case.max_tool_calls <= 16 for case in corpus.cases)
    assert all("operations" not in case.required_tool_families for case in corpus.cases)


@pytest.mark.parametrize("mutation", ["duplicate", "criteria", "budget", "operations", "bool"])
def test_invalid_case_contracts_are_rejected(mutation):
    payload = json.loads(CASES.read_bytes())
    if mutation == "duplicate":
        payload["cases"][1] = payload["cases"][0]
    elif mutation == "criteria":
        payload["cases"][0]["completion_criteria"] = []
    elif mutation == "budget":
        payload["cases"][0]["max_tool_calls"] = 17
    elif mutation == "operations":
        payload["cases"][0]["required_tool_families"] = ["operations"]
    else:
        payload["cases"][0]["max_tool_calls"] = True
    with pytest.raises(ValidationError):
        OrchestrationEvalCorpus.model_validate(payload)


def test_canonical_digest_and_immutable_contract(tmp_path):
    payload = json.loads(CASES.read_bytes())
    payload["cases"][0]["question"] += " changed"
    target = tmp_path / "cases.json"
    target.write_bytes(canonical_json_bytes(payload))
    with pytest.raises(ValueError, match="digest"):
        load_orchestration_cases(target)
    case = load_orchestration_cases(CASES).cases[0]
    with pytest.raises(ValidationError):
        case.question = "changed"
    assert isinstance(case.evidence_facts, tuple)
    assert set(TrajectoryObservation.model_fields) == {
        "tasks",
        "routes",
        "specialists",
        "tool_calls",
        "evidence_facts",
        "completion_criteria",
        "safe_stop",
        "duration_ms",
        "tokens",
        "estimated_cost",
    }


@pytest.mark.parametrize(
    "key,value", [("excluded_from_offline_gates", 1), ("executed_case_count", False)]
)
def test_live_status_rejects_bool_integer_coercion(key, value):
    with pytest.raises(ValidationError):
        LiveProfileStatus(status="not_run_missing_credentials", duration_ms=0.0, **{key: value})


@pytest.mark.parametrize(
    "key,value", [("task_success_rate", 1.1), ("total_tool_calls", -1), ("p50_duration_ms", -1.0)]
)
def test_metric_schema_rejects_impossible_values(key, value):
    payload = json.loads((DATA_DIR / "evals" / "orchestration_report.json").read_bytes())
    metrics = payload["profiles"][0]["metrics"]
    metrics[key] = value
    with pytest.raises(ValidationError):
        ProfileMetrics.model_validate(metrics)
