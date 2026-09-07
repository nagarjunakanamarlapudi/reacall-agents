"""Complete, validator-accepted optional-live fixture; no provider is invoked."""

import copy
import json
import shutil

import pytest

from recallops.evaluation.digests import canonical_json_bytes, canonical_sha256
from recallops.evaluation.orchestration_benchmark import score_trajectory
from recallops.evaluation.orchestration_schema import (
    TrajectoryObservation,
    load_orchestration_cases,
)
from recallops.evaluation.scorecard import build_scorecard
from recallops.paths import PROJECT_ROOT, RepositoryPaths


@pytest.fixture
def completed_live_repository(tmp_path, request):
    target = tmp_path / "completed-live" / "data" / "evals"
    target.mkdir(parents=True)
    for path in (PROJECT_ROOT / "data" / "evals").glob("*.json"):
        shutil.copyfile(path, target / path.name)
    paths = RepositoryPaths(target.parents[1])
    payload = json.loads(paths.orchestration_evaluation_report.read_bytes())
    cases = load_orchestration_cases(paths.orchestration_evaluation_corpus)
    rows = copy.deepcopy(payload["profiles"][1]["results"])
    usage_available = getattr(request, "param", True)
    for row, case in zip(rows, cases.cases, strict=True):
        row["observation"].update(
            tokens=11 if usage_available else None,
            estimated_cost=0.125 if usage_available else None,
        )
        row["metrics"] = score_trajectory(
            case, TrajectoryObservation.model_validate(row["observation"]), "deep_agents_live"
        ).model_dump(mode="json")
    payload["live_status"].update(
        status="completed",
        provider="sk-sensitivefixture123",
        model_sha256="a" * 64,
        prompt_sha256="b" * 64,
        repetitions=1,
        executed_case_count=24,
        results=rows,
        tokens=264 if usage_available else None,
        tokens_available=usage_available,
        estimated_cost=3.0 if usage_available else None,
        cost_available=usage_available,
        duration_ms=1234.5,
    )
    payload.pop("report_sha256")
    payload["report_sha256"] = canonical_sha256(payload)
    paths.orchestration_evaluation_report.write_bytes(canonical_json_bytes(payload))
    build_scorecard(
        paths.evaluation_report,
        paths.retrieval_evaluation_report,
        paths.orchestration_evaluation_report,
        paths.evaluation_scorecard,
    )
    return paths
