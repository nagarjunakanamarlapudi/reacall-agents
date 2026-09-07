"""Scorecards bind verified reports, including the legacy safety artifact."""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from recallops.evaluation.digests import canonical_json_bytes, canonical_sha256
from recallops.evaluation.scorecard import build_scorecard, validate_scorecard
from recallops.paths import DATA_DIR, EvaluationArtifactPaths, RepositoryPaths


@pytest.fixture
def paths(tmp_path: Path) -> EvaluationArtifactPaths:
    # Never mutate the committed reports or case corpora, even in tamper tests.
    for name in (
        "report.json",
        "retrieval_report.json",
        "orchestration_report.json",
        "scenarios.json",
        "retrieval_cases.json",
        "orchestration_cases.json",
    ):
        shutil.copyfile(DATA_DIR / "evals" / name, tmp_path / name)
    return EvaluationArtifactPaths(
        safety_report=tmp_path / "report.json",
        retrieval_report=tmp_path / "retrieval_report.json",
        orchestration_report=tmp_path / "orchestration_report.json",
    )


def build(paths: EvaluationArtifactPaths, tmp_path: Path):
    return build_scorecard(
        paths.safety_report,
        paths.retrieval_report,
        paths.orchestration_report,
        tmp_path / "scorecard.json",
    )


def resign(path: Path, payload: dict, field: str = "scorecard_sha256") -> None:
    payload.pop(field, None)
    payload[field] = canonical_sha256(payload)
    path.write_bytes(canonical_json_bytes(payload))


def test_scorecard_requires_all_three_passing_reports(paths, tmp_path):
    scorecard = build(paths, tmp_path)
    assert scorecard.offline_gate_passed is True
    assert [suite.name for suite in scorecard.suite_summaries] == [
        "safety",
        "retrieval",
        "orchestration",
    ]
    assert [suite.case_count for suite in scorecard.suite_summaries] == [21, 96, 24]
    assert [suite.result_count for suite in scorecard.suite_summaries] == [21, 576, 48]
    assert validate_scorecard(tmp_path / "scorecard.json", paths) == scorecard
    for name in ("safety", "retrieval", "orchestration"):
        source = getattr(paths, f"{name}_report")
        assert (
            scorecard.artifact_digests[f"{name}_report"]
            == hashlib.sha256(source.read_bytes()).hexdigest()
        )


@pytest.mark.parametrize("suite", ["safety", "retrieval", "orchestration"])
def test_scorecard_rejects_changed_report_after_build(paths, tmp_path, suite):
    build(paths, tmp_path)
    source = getattr(paths, f"{suite}_report")
    source.write_bytes(source.read_bytes() + b"\n")
    with pytest.raises(ValueError, match=f"{suite} report digest mismatch"):
        validate_scorecard(tmp_path / "scorecard.json", paths)


@pytest.mark.parametrize("suite", ["safety", "retrieval", "orchestration"])
def test_scorecard_rejects_changed_corpus_after_build(paths, tmp_path, suite):
    build(paths, tmp_path)
    source = getattr(paths, f"{suite}_corpus")
    source.write_bytes(source.read_bytes() + b"\n")
    with pytest.raises(ValueError, match=f"{suite} corpus digest mismatch"):
        validate_scorecard(tmp_path / "scorecard.json", paths)


@pytest.mark.parametrize("suite", ["safety", "retrieval", "orchestration"])
@pytest.mark.parametrize(
    "corruption", ["missing", "malformed", "incomplete", "nonfinite", "stale", "gate"]
)
def test_invalid_inputs_never_produce_scorecard(paths, tmp_path, suite, corruption):
    source = getattr(paths, f"{suite}_report")
    payload = json.loads(source.read_bytes())
    if corruption == "missing":
        source.unlink()
    elif corruption == "malformed":
        source.write_text("{broken")
    else:
        if corruption == "incomplete":
            if suite == "safety":
                payload["results"].pop()
            elif suite == "retrieval":
                payload["configurations"][0]["results"].pop()
            else:
                payload["profiles"][0]["results"].pop()
        elif corruption == "nonfinite":
            if suite == "safety":
                payload["results"][0]["tool_trace"][0]["duration_ms"] = float("nan")
            elif suite == "retrieval":
                payload["gates"]["route_accuracy"] = float("inf")
            else:
                payload["live_status"]["duration_ms"] = float("nan")
        elif corruption == "stale":
            field = {
                "safety": "scenario_corpus_sha256",
                "retrieval": "retrieval_case_corpus_sha256",
                "orchestration": "orchestration_case_corpus_sha256",
            }[suite]
            payload[field] = "0" * 64
        else:
            payload["gate_passed"] = False
        if suite != "safety" and corruption != "nonfinite":
            resign(source, payload, "report_sha256")
        else:
            source.write_text(json.dumps(payload))
    with pytest.raises(ValueError):
        build(paths, tmp_path)
    assert not (tmp_path / "scorecard.json").exists()


@pytest.mark.parametrize(
    "tamper", ["gate", "count", "metric", "missing_suite", "duplicate_suite", "live"]
)
def test_resigned_scorecard_cannot_change_verified_summary(paths, tmp_path, tamper):
    build(paths, tmp_path)
    output = tmp_path / "scorecard.json"
    payload = json.loads(output.read_bytes())
    if tamper == "gate":
        payload["offline_gate_passed"] = False
    elif tamper == "count":
        payload["suite_summaries"][0]["case_count"] = 20
    elif tamper == "metric":
        payload["suite_summaries"][0]["metrics"]["scenario_pass_rate"] = 0.5
    elif tamper == "missing_suite":
        payload["suite_summaries"].pop()
    elif tamper == "duplicate_suite":
        payload["suite_summaries"][1] = payload["suite_summaries"][0]
    else:
        payload["optional_live_status"]["status"] = "completed"
    resign(output, payload)
    with pytest.raises(ValueError):
        validate_scorecard(output, paths)


@pytest.mark.parametrize(
    "tamper",
    [
        "assertion_missing",
        "assertion_actual",
        "assertion_expected",
        "assertion_pass",
        "metric",
        "critical",
        "bool",
    ],
)
def test_resigned_scorecard_cannot_bless_forged_safety(paths, tmp_path, tamper):
    build(paths, tmp_path)
    report = json.loads(paths.safety_report.read_bytes())
    row = report["results"][0]
    if tamper == "assertion_missing":
        row["assertions"].pop(3)
    elif tamper == "assertion_actual":
        row["assertions"][2]["actual"] = "closed"
    elif tamper == "assertion_expected":
        row["assertions"][2]["expected"] = "closed"
    elif tamper == "assertion_pass":
        row["assertions"][2]["passed"] = False
    elif tamper == "metric":
        report["metrics"]["route_accuracy"] = 0.5
    elif tamper == "critical":
        row["safety_critical"] = False
    else:
        report["gate_passed"] = 1
    paths.safety_report.write_bytes(canonical_json_bytes(report))
    output = tmp_path / "scorecard.json"
    payload = json.loads(output.read_bytes())
    payload["artifact_digests"]["safety_report"] = hashlib.sha256(
        paths.safety_report.read_bytes()
    ).hexdigest()
    resign(output, payload)
    with pytest.raises(ValueError):
        validate_scorecard(output, paths)


def test_valid_safety_failure_keeps_offline_gate_false(paths, tmp_path):
    from recallops.evaluation.metrics import calculate_metrics
    from recallops.evaluation.runner import load_scenarios
    from recallops.evaluation.schema import EvaluationReport

    payload = json.loads(paths.safety_report.read_bytes())
    row = payload["results"][0]
    row["duration_ms"] = 300001
    row["assertions"][-1].update(actual=300001, passed=False)
    row["passed"] = False
    report = EvaluationReport.model_validate(payload)
    payload["metrics"] = calculate_metrics(
        load_scenarios(paths.safety_corpus).scenarios, report.results
    ).model_dump(mode="json")
    payload["gate_passed"] = False
    paths.safety_report.write_bytes(canonical_json_bytes(payload))
    scorecard = build(paths, tmp_path)
    assert [suite.gate_passed for suite in scorecard.suite_summaries] == [False, True, True]
    assert scorecard.offline_gate_passed is False
    assert validate_scorecard(tmp_path / "scorecard.json", paths).offline_gate_passed is False


def test_optional_live_error_does_not_change_offline_verdict(paths, tmp_path):
    payload = json.loads(paths.orchestration_report.read_bytes())
    payload["live_status"].update(
        status="error",
        error_code="factory_error",
        provider="test",
        model_sha256="a" * 64,
        prompt_sha256="b" * 64,
    )
    resign(paths.orchestration_report, payload, "report_sha256")
    scorecard = build(paths, tmp_path)
    assert scorecard.optional_live_status.status == "error"
    assert scorecard.optional_live_status.excluded_from_offline_gates is True
    assert scorecard.offline_gate_passed is True
    assert validate_scorecard(tmp_path / "scorecard.json", paths) == scorecard


def test_scorecard_canonical_bytes_self_digest_and_cwd_independence(paths, tmp_path, monkeypatch):
    repository = RepositoryPaths(DATA_DIR.parent)
    expected_output = repository.evaluation_scorecard
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    assert repository.evaluation_scorecard == expected_output
    scorecard = build(paths, tmp_path)
    output = tmp_path / "scorecard.json"
    payload = json.loads(output.read_bytes())
    assert output.read_bytes() == canonical_json_bytes(payload)
    digest = payload.pop("scorecard_sha256")
    assert (
        digest
        == hashlib.sha256(
            (
                json.dumps(
                    payload,
                    ensure_ascii=False,
                    allow_nan=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
            ).encode()
        ).hexdigest()
    )
    assert validate_scorecard(output, paths) == scorecard
    output.write_bytes(output.read_bytes() + b"\n")
    with pytest.raises(ValueError, match="canonical"):
        validate_scorecard(output, paths)


def test_scorecard_rejects_unsigned_changes(paths, tmp_path):
    build(paths, tmp_path)
    output = tmp_path / "scorecard.json"
    payload = json.loads(output.read_bytes())
    payload["generated_at"] = "2000-01-01T00:00:00Z"
    output.write_bytes(canonical_json_bytes(payload))
    with pytest.raises(ValueError, match="digest mismatch"):
        validate_scorecard(output, paths)


def test_self_digest_binds_timestamp_representation(paths, tmp_path):
    build(paths, tmp_path)
    output = tmp_path / "scorecard.json"
    payload = json.loads(output.read_bytes())
    payload["generated_at"] = payload["generated_at"].replace("Z", "+00:00")
    output.write_bytes(canonical_json_bytes(payload))
    with pytest.raises(ValueError, match="digest mismatch"):
        validate_scorecard(output, paths)


def test_resigned_metric_type_coercion_is_rejected(paths, tmp_path):
    build(paths, tmp_path)
    output = tmp_path / "scorecard.json"
    payload = json.loads(output.read_bytes())
    payload["suite_summaries"][0]["metrics"]["scenario_pass_rate"] = True
    resign(output, payload)
    with pytest.raises(ValueError):
        validate_scorecard(output, paths)


def test_safety_duplicate_json_keys_are_rejected(paths, tmp_path):
    raw = paths.safety_report.read_text()
    paths.safety_report.write_text(
        raw.replace('"gate_passed": true', '"gate_passed": false, "gate_passed": true', 1)
    )
    with pytest.raises(ValueError, match="duplicate"):
        build(paths, tmp_path)


def test_safety_observation_cannot_disagree_with_persisted_state(paths, tmp_path):
    payload = json.loads(paths.safety_report.read_bytes())
    payload["results"][0]["state_excerpt"]["status"] = "closed"
    paths.safety_report.write_bytes(canonical_json_bytes(payload))
    with pytest.raises(ValueError, match="observation"):
        build(paths, tmp_path)


def test_safety_cannot_remove_excerpt_to_skip_observation_checks(paths, tmp_path):
    payload = json.loads(paths.safety_report.read_bytes())
    payload["results"][0]["state_excerpt"] = {}
    paths.safety_report.write_bytes(canonical_json_bytes(payload))
    with pytest.raises(ValueError, match="excerpt"):
        build(paths, tmp_path)


def test_output_must_not_overwrite_report(paths):
    before = paths.safety_report.read_bytes()
    with pytest.raises(ValueError, match="overwrite"):
        build_scorecard(
            paths.safety_report,
            paths.retrieval_report,
            paths.orchestration_report,
            paths.safety_report,
        )
    assert paths.safety_report.read_bytes() == before


@pytest.mark.parametrize(
    "field", ["schema_version", "execution_mode", "metadata_kind", "assertion_expected"]
)
def test_legacy_report_cannot_omit_defaulted_contract_fields(paths, tmp_path, field):
    payload = json.loads(paths.safety_report.read_bytes())
    if field == "metadata_kind":
        payload["run_metadata"].pop("report_kind")
    elif field == "assertion_expected":
        payload["results"][0]["assertions"][4].pop("expected")
    else:
        payload.pop(field)
    paths.safety_report.write_bytes(canonical_json_bytes(payload))
    with pytest.raises(ValueError):
        build(paths, tmp_path)


@pytest.mark.parametrize("suite", ["retrieval", "orchestration"])
async def test_real_execution_failure_in_either_new_suite_blocks_offline_gate(
    paths, tmp_path, monkeypatch, suite
):
    if suite == "retrieval":
        from recallops.evaluation import retrieval_benchmark as benchmark

        async def failed_read(*args, **kwargs):
            raise RuntimeError("injected retrieval failure")

        monkeypatch.setattr(benchmark.AgenticRetriever, "resume", failed_read)
        report = await benchmark.run_retrieval_benchmark(
            paths.retrieval_corpus, paths.retrieval_report
        )
    else:
        from recallops.evaluation import orchestration_benchmark as benchmark

        def failed_read(*args, **kwargs):
            raise RuntimeError("injected registry failure")

        monkeypatch.setattr(benchmark.RecallRegistryService, "get_recall", failed_read)
        report = await benchmark.run_orchestration_benchmark(
            paths.orchestration_corpus, paths.orchestration_report
        )
    assert report.gate_passed is False
    scorecard = build(paths, tmp_path)
    assert scorecard.offline_gate_passed is False
    assert {item.name: item.gate_passed for item in scorecard.suite_summaries}[suite] is False
    assert validate_scorecard(tmp_path / "scorecard.json", paths) == scorecard
    # Re-signing cannot promote a real suite failure into a passing offline gate.
    output = tmp_path / "scorecard.json"
    payload = json.loads(output.read_bytes())
    payload["offline_gate_passed"] = True
    resign(output, payload)
    with pytest.raises(ValueError, match="offline gate"):
        validate_scorecard(output, paths)
