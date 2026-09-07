"""UI projections must reject untrusted artifacts and never expose raw observations."""

import hashlib
import json
import shutil
from dataclasses import asdict

import pytest

from recallops.evaluation.digests import canonical_json_bytes, canonical_sha256
from recallops.paths import PROJECT_ROOT, RepositoryPaths
from recallops.ui import evaluation_reports


@pytest.fixture
def repository_paths(tmp_path):
    target = tmp_path / "repository" / "data" / "evals"
    target.mkdir(parents=True)
    for name in (
        "report.json",
        "scenarios.json",
        "retrieval_report.json",
        "retrieval_cases.json",
        "orchestration_report.json",
        "orchestration_cases.json",
        "scorecard.json",
    ):
        shutil.copyfile(PROJECT_ROOT / "data" / "evals" / name, target / name)
    return RepositoryPaths(target.parents[1])


def test_verified_projection_is_complete_compact_and_cwd_independent(
    repository_paths, monkeypatch, tmp_path
):
    monkeypatch.chdir(tmp_path)
    before = {
        p.name: p.read_bytes() for p in repository_paths.evaluation_scorecard.parent.iterdir()
    }
    projection = evaluation_reports.load_evaluation_scorecard(repository_paths)
    assert projection.verification_status == "verified"
    assert projection.offline_gate_passed is True
    assert projection.safety.scenario_count == 21
    assert projection.retrieval.case_count == 96
    assert projection.retrieval.result_count == 576
    assert projection.orchestration.case_count == 24
    assert projection.orchestration.result_count == 48
    assert len(projection.retrieval.configurations) == 6
    assert len(projection.orchestration.profiles) == 2
    assert projection.optional_live_status == "not_run_missing_credentials"
    serialized = json.dumps(asdict(projection))
    for prohibited in (
        "state_excerpt",
        "tool_trace",
        "ranked_document_ids",
        "cited_facts",
        "prompt",
        "observation",
    ):
        assert prohibited not in serialized
    assert before == {
        p.name: p.read_bytes() for p in repository_paths.evaluation_scorecard.parent.iterdir()
    }


@pytest.mark.parametrize(
    "name",
    [
        "report.json",
        "retrieval_report.json",
        "orchestration_report.json",
        "scorecard.json",
        "scenarios.json",
        "retrieval_cases.json",
        "orchestration_cases.json",
    ],
)
@pytest.mark.parametrize(
    "mutation", ["missing", "stale", "malformed", "nonfinite", "partial", "forged_gate"]
)
def test_tampered_artifacts_are_unavailable(repository_paths, name, mutation):
    path = repository_paths.evaluation_scorecard.with_name(name)
    if mutation == "missing":
        path.unlink()
    elif mutation == "stale":
        path.write_bytes(path.read_bytes() + b"\n")
    elif mutation == "malformed":
        path.write_text('{"secret":"sk-do-not-display",broken')
    else:
        payload = json.loads(path.read_bytes())
        if mutation == "nonfinite":
            payload["unexpected"] = float("nan")
        elif mutation == "partial":
            payload.pop(next(iter(payload)))
        else:
            payload["offline_gate_passed" if name == "scorecard.json" else "gate_passed"] = False
        path.write_text(json.dumps(payload))
    projection = evaluation_reports.load_evaluation_scorecard(repository_paths)
    assert projection.verification_status == "unavailable"
    assert projection.offline_gate_passed is False
    assert projection.safety.scenario_count is None
    assert projection.retrieval.configurations == ()
    assert projection.orchestration.profiles == ()
    assert "sk-do-not-display" not in json.dumps(asdict(projection))


def test_resigned_forged_scorecard_cannot_bless_metrics(repository_paths):
    path = repository_paths.evaluation_scorecard
    payload = json.loads(path.read_bytes())
    payload["suite_summaries"][1]["metrics"]["configurations"]["sparse_bm25"]["recall_at_5"] = 0.0
    payload.pop("scorecard_sha256")
    payload["scorecard_sha256"] = canonical_sha256(payload)
    path.write_bytes(canonical_json_bytes(payload))
    assert (
        evaluation_reports.load_evaluation_scorecard(repository_paths).verification_status
        == "unavailable"
    )


def test_projected_snapshots_must_match_validated_scorecard(repository_paths, monkeypatch):
    original = evaluation_reports.validate_scorecard

    def replace_after_validation(*args):
        result = original(*args)
        path = repository_paths.retrieval_evaluation_report
        path.write_bytes(b'{"secret":"do-not-display"}')
        return result

    monkeypatch.setattr(evaluation_reports, "validate_scorecard", replace_after_validation)
    projection = evaluation_reports.load_evaluation_scorecard(repository_paths)
    # Either the verified immutable snapshot is used, or the replacement is rejected.
    assert projection.verification_status in {"verified", "unavailable"}
    assert "do-not-display" not in json.dumps(asdict(projection))
    if projection.verification_status == "verified":
        assert (
            projection.artifact_digests["retrieval_report"]
            != hashlib.sha256(repository_paths.retrieval_evaluation_report.read_bytes()).hexdigest()
        )
