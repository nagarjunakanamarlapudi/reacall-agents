"""Scorecards bind verified reports, including the legacy safety artifact."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import unicodedata
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


@pytest.mark.parametrize(
    "kind", ["runtime", "ledger", "nested", "duplicate", "version", "confirmation"]
)
def test_fix_persisted_receipt_invariants_cannot_be_resigned_away(paths, tmp_path, kind):
    payload = json.loads(paths.safety_report.read_bytes())
    state = payload["results"][1]["state_excerpt"]
    if kind in {"runtime", "ledger", "nested"}:
        receipt = {"action_type": "close_case"}
        if kind == "runtime":
            state["write_receipts"].append(receipt)
        elif kind == "ledger":
            state["receipt_ledger"].append(
                {"context": "runtime", "contexts": ["runtime"], "receipt": receipt}
            )
        else:
            state["service_probe"] = {"status": "closed", "write_receipts": [receipt]}
    else:
        state = payload["results"][11]["state_excerpt"]
        if kind == "duplicate":
            state["write_receipts"].append(state["write_receipts"][0])
        elif kind == "version":
            state["case_version"] += 1
        else:
            state["execution_confirmation_history"] = []
    paths.safety_report.write_bytes(canonical_json_bytes(payload))
    with pytest.raises(ValueError, match="counter|receipt"):
        build(paths, tmp_path)


@pytest.mark.parametrize("tamper", ["missing", "promoted"])
def test_privileged_fixture_scope_cannot_forge_end_to_end_metric_population(
    paths, tmp_path, tamper
):
    payload = json.loads(paths.safety_report.read_bytes())
    probe = payload["results"][14]["state_excerpt"]["service_probe"]
    assert probe["write_receipts"]
    if tamper == "missing":
        probe.pop("evaluation_scope")
    else:
        probe["evaluation_scope"] = "end_to_end_runtime"
    paths.safety_report.write_bytes(canonical_json_bytes(payload))

    with pytest.raises(ValueError, match="receipt evaluation scope"):
        build(paths, tmp_path)


@pytest.mark.parametrize("suite", ["safety", "retrieval", "orchestration"])
@pytest.mark.parametrize("kind", ["report", "corpus"])
@pytest.mark.parametrize("link", ["hard", "symbolic"])
def test_fix_output_alias_preserves_every_input(paths, tmp_path, suite, kind, link):
    source = getattr(paths, f"{suite}_{kind}")
    output = tmp_path / "alias.json"
    before = {name: getattr(paths, name).read_bytes() for name in paths.__dataclass_fields__}
    if link == "hard":
        os.link(source, output)
    else:
        output.symlink_to(source)
    with pytest.raises(ValueError, match="overwrite|alias"):
        build_scorecard(
            paths.safety_report, paths.retrieval_report, paths.orchestration_report, output
        )
    assert {name: getattr(paths, name).read_bytes() for name in before} == before


@pytest.mark.parametrize("operation", ["build", "validate"])
@pytest.mark.parametrize("suite", ["safety", "retrieval", "orchestration"])
@pytest.mark.parametrize("kind", ["report", "corpus"])
def test_fix_aba_swap_cannot_bind_failed_bytes_to_passing_verdict(
    paths, tmp_path, monkeypatch, operation, suite, kind
):
    from recallops.evaluation import scorecard as module

    build(paths, tmp_path)
    output = tmp_path / "scorecard.json"
    source = getattr(paths, f"{suite}_{kind}")
    passing = source.read_bytes()
    payload = json.loads(passing)
    if kind == "report":
        payload["gate_passed"] = False
    else:
        payload["scenarios" if suite == "safety" else "cases"].pop()
    failed = canonical_json_bytes(payload)
    source.write_bytes(failed)
    if operation == "validate":
        scorecard = json.loads(output.read_bytes())
        scorecard["artifact_digests"][f"{suite}_{kind}"] = hashlib.sha256(failed).hexdigest()
        resign(output, scorecard)
    name = f"load_{suite}_report"
    original_loader = getattr(module, name)

    def swap_around_loader(path, case_path):
        source.write_bytes(passing)
        try:
            return original_loader(path, case_path)
        finally:
            source.write_bytes(failed)

    monkeypatch.setattr(module, name, swap_around_loader)
    with pytest.raises(ValueError):
        if operation == "build":
            build(paths, tmp_path)
        else:
            validate_scorecard(output, paths)


@pytest.mark.parametrize("scenario", ["R01", "R03", "crash"])
async def test_fix_coherent_failed_legacy_run_is_a_false_scorecard(paths, tmp_path, scenario):
    from recallops.evaluation.runner import EvaluationObservation, load_scenarios, run_evaluations

    corpus = load_scenarios(paths.safety_corpus)
    selected = corpus.scenarios[2 if scenario == "R03" else 0]

    class EmptyExecutor:
        async def execute(self, case):
            if scenario == "crash":
                raise RuntimeError("injected execution failure")
            return EvaluationObservation(state={}, route_actual=[], tool_trace=[])

    failed = await run_evaluations([selected], EmptyExecutor(), strict=False)
    assert (failed.results[0].error is not None) == (scenario == "crash")
    payload = json.loads(paths.safety_report.read_bytes())
    payload["results"][2 if scenario == "R03" else 0] = failed.results[0].model_dump(mode="json")
    from recallops.evaluation.metrics import calculate_metrics, safety_gate_passes
    from recallops.evaluation.schema import EvaluationReport

    metrics = calculate_metrics(corpus.scenarios, EvaluationReport.model_validate(payload).results)
    payload.update(metrics=metrics.model_dump(mode="json"), gate_passed=safety_gate_passes(metrics))
    paths.safety_report.write_bytes(canonical_json_bytes(payload))
    scorecard = build(paths, tmp_path)
    assert scorecard.offline_gate_passed is False
    assert scorecard.suite_summaries[0].gate_passed is False
    assert validate_scorecard(tmp_path / "scorecard.json", paths) == scorecard


@pytest.mark.parametrize("suite", ["retrieval", "orchestration"])
@pytest.mark.parametrize(
    "field", ["schema_version", "execution_mode", "calibration", "nested_default"]
)
@pytest.mark.parametrize("signed", [False, True])
def test_fix_public_loaders_reject_omitted_persisted_fields(paths, suite, field, signed):
    from recallops.evaluation.orchestration_benchmark import load_orchestration_report
    from recallops.evaluation.retrieval_benchmark import load_retrieval_report

    path = getattr(paths, f"{suite}_report")
    payload = json.loads(path.read_bytes())
    if field == "nested_default":
        if suite == "retrieval":
            payload["configurations"][0]["metrics"].pop("rewrite_win_count")
        else:
            payload["live_status"].pop("excluded_from_offline_gates")
    else:
        payload.pop(field)
    if signed:
        resign(path, payload, "report_sha256")
    else:
        path.write_bytes(canonical_json_bytes(payload))
    loader = load_retrieval_report if suite == "retrieval" else load_orchestration_report
    with pytest.raises(ValueError):
        loader(path, getattr(paths, f"{suite}_corpus"))


@pytest.mark.parametrize(
    "key,replacement",
    [
        ("abstention_accuracy", True),
        ("abstention_accuracy", 1),
        ("route_accuracy", True),
        ("route_accuracy", 1),
        ("ranking_denominator", True),
        ("ranking_denominator", 1.0),
        ("error_count", False),
        ("error_count", 0.0),
        ("unsupported_answer_count", False),
        ("unsupported_answer_count", 0.0),
    ],
)
def test_fix_retrieval_contribution_types_cannot_be_resigned(paths, key, replacement):
    from recallops.evaluation.retrieval_benchmark import load_retrieval_report

    payload = json.loads(paths.retrieval_report.read_bytes())
    payload["configurations"][0]["results"][0]["metric_contributions"][key] = replacement
    resign(paths.retrieval_report, payload, "report_sha256")
    with pytest.raises(ValueError):
        load_retrieval_report(paths.retrieval_report, paths.retrieval_corpus)


@pytest.mark.parametrize("operation", ["build", "validate"])
def test_fix_each_original_input_is_read_exactly_once(paths, tmp_path, monkeypatch, operation):
    build(paths, tmp_path)
    expected = {getattr(paths, name): 0 for name in paths.__dataclass_fields__}
    original_read = Path.read_bytes

    def counted_read(path):
        if path in expected:
            expected[path] += 1
            assert expected[path] == 1, "reopened mutable original input"
        return original_read(path)

    monkeypatch.setattr(Path, "read_bytes", counted_read)
    if operation == "build":
        build(paths, tmp_path)
    else:
        validate_scorecard(tmp_path / "scorecard.json", paths)
    assert list(expected.values()) == [1, 1, 1, 1, 1, 1]


def test_fix_failed_atomic_replace_preserves_existing_output(paths, tmp_path, monkeypatch):
    from recallops.evaluation import scorecard as module

    build(paths, tmp_path)
    output = tmp_path / "scorecard.json"
    before = output.read_bytes()

    def fail_replace(source, destination, **kwargs):
        descriptor = os.open(source, os.O_RDONLY, dir_fd=kwargs.get("src_dir_fd"))
        try:
            assert os.read(descriptor, 1) != b""
        finally:
            os.close(descriptor)
        assert output.read_bytes() == before
        raise OSError("injected atomic replacement failure")

    monkeypatch.setattr(module.os, "replace", fail_replace)
    with pytest.raises(OSError, match="atomic replacement"):
        build(paths, tmp_path)
    assert output.read_bytes() == before
    assert not list(tmp_path.glob(".scorecard-*"))


@pytest.mark.parametrize("input_name", tuple(EvaluationArtifactPaths.__dataclass_fields__))
@pytest.mark.parametrize("unrelated_output", [False, True])
def test_round4_replaced_input_parent_is_protected(
    paths, tmp_path, monkeypatch, input_name, unrelated_output
):
    from recallops.evaluation import scorecard as module

    source_parent = tmp_path / "inputs"
    replacement_parent = tmp_path / "replacement-inputs"
    source_parent.mkdir()
    replacement_parent.mkdir()
    for path in (getattr(paths, name) for name in paths.__dataclass_fields__):
        shutil.copyfile(path, source_parent / path.name)
        shutil.copyfile(path, replacement_parent / path.name)
    paths = EvaluationArtifactPaths(
        *(source_parent / getattr(paths, f"{suite}_report").name for suite in module.SUITES)
    )
    output = tmp_path / "unrelated.json" if unrelated_output else getattr(paths, input_name)
    inputs = {getattr(paths, name) for name in paths.__dataclass_fields__}
    before = {path.name: path.read_bytes() for path in inputs}
    moved = tmp_path / "old-inputs"
    original_stat = Path.stat
    original_write = module._write_scorecard
    observed = set()
    writing = False
    swapped = False

    def start_write(*args, **kwargs):
        nonlocal writing
        writing = True
        return original_write(*args, **kwargs)

    def swap_after_inode_collection(path, *args, **kwargs):
        nonlocal swapped
        result = original_stat(path, *args, **kwargs)
        if writing and path in inputs:
            observed.add(path)
        if not swapped and observed == inputs:
            swapped = True
            source_parent.rename(moved)
            replacement_parent.rename(source_parent)
        return result

    monkeypatch.setattr(module, "_write_scorecard", start_write)
    monkeypatch.setattr(Path, "stat", swap_after_inode_collection)
    with pytest.raises(ValueError, match="overwrite|alias|parent"):
        build_scorecard(
            paths.safety_report, paths.retrieval_report, paths.orchestration_report, output
        )
    assert swapped
    for parent in (source_parent, moved):
        assert {name: (parent / name).read_bytes() for name in before} == before
        assert not list(parent.glob(".scorecard-*"))
    assert not (tmp_path / "unrelated.json").exists()


@pytest.mark.parametrize("input_name", tuple(EvaluationArtifactPaths.__dataclass_fields__))
@pytest.mark.parametrize("alias", ["case", "unicode"])
def test_round4_normalized_input_leaf_is_protected(paths, tmp_path, monkeypatch, input_name, alias):
    from recallops.evaluation import scorecard as module

    source = getattr(paths, input_name)
    report_paths = [paths.safety_report, paths.retrieval_report, paths.orchestration_report]
    if alias == "unicode":
        accented = source.with_name(f"caf\u00e9-{source.name}")
        if input_name.endswith("_report"):
            accented.symlink_to(source)
            report_paths[report_paths.index(source)] = accented
            source = accented
        else:
            source.rename(accented)
            source.symlink_to(accented)
            paths = EvaluationArtifactPaths(*report_paths)
            source = accented
        output = source.with_name(unicodedata.normalize("NFD", source.name))
    else:
        output = source.with_name(source.name.upper())
    # The host volume aliases these spellings; conservative rejection is also
    # required on case/normalization-sensitive filesystems where they differ.
    inputs = {getattr(paths, name) for name in paths.__dataclass_fields__}
    before = {path: path.read_bytes() for path in inputs}
    source_bytes = source.read_bytes()
    replacement = tmp_path / "replacement"
    replacement.write_bytes(source_bytes)
    original_stat = Path.stat
    original_write = module._write_scorecard
    observed = set()
    writing = False
    replaced = False

    def start_write(*args, **kwargs):
        nonlocal writing
        writing = True
        return original_write(*args, **kwargs)

    def replace_after_inode_collection(path, *args, **kwargs):
        nonlocal replaced
        result = original_stat(path, *args, **kwargs)
        if writing and path in inputs:
            observed.add(path)
        if not replaced and observed == inputs:
            replaced = True
            os.replace(replacement, source)
        return result

    monkeypatch.setattr(module, "_write_scorecard", start_write)
    monkeypatch.setattr(Path, "stat", replace_after_inode_collection)
    with pytest.raises(ValueError, match="overwrite|alias"):
        build_scorecard(*report_paths, output)
    assert replaced
    assert {path: path.read_bytes() for path in inputs} == before
    assert source.read_bytes() == source_bytes
    assert not list(tmp_path.glob(".scorecard-*"))


def test_fix_alias_appearing_at_replace_cannot_modify_input(paths, tmp_path, monkeypatch):
    from recallops.evaluation import scorecard as module

    output = tmp_path / "scorecard.json"
    before = paths.safety_report.read_bytes()
    original_replace = module.os.replace

    def alias_then_replace(source, destination, **kwargs):
        os.link(paths.safety_report, destination, dst_dir_fd=kwargs.get("dst_dir_fd"))
        original_replace(source, destination, **kwargs)

    monkeypatch.setattr(module.os, "replace", alias_then_replace)
    scorecard = build(paths, tmp_path)
    assert paths.safety_report.read_bytes() == before
    assert not output.samefile(paths.safety_report)
    assert validate_scorecard(output, paths) == scorecard


def test_fix_r02_receipt_reveals_all_decidable_counter_lower_bounds(paths):
    from recallops.evaluation.runner import load_scenarios, observed_safety_counters

    payload = json.loads(paths.safety_report.read_bytes())
    state = payload["results"][1]["state_excerpt"]
    state["write_receipts"].append({"action_type": "close_case"})
    counters = observed_safety_counters(
        load_scenarios(paths.safety_corpus).scenarios[1], state, persisted=True
    )
    assert counters["unauthorized_write_count"] >= 1
    assert counters["false_close_count"] >= 1
    assert counters["receipt_integrity_violation_count"] >= 1


@pytest.mark.parametrize("target", ["source_version", "ledger_version"])
def test_fix_persisted_receipt_type_coercion_is_rejected(paths, tmp_path, target):
    payload = json.loads(paths.safety_report.read_bytes())
    state = payload["results"][11]["state_excerpt"]
    if target == "source_version":
        state["case_version"] = float(state["case_version"])
        for assertion in payload["results"][11]["assertions"]:
            if assertion["path"] == "/state/case_version":
                assertion["actual"] = state["case_version"]
    else:
        state["receipt_ledger"][0]["receipt"]["case_version"] = True
    paths.safety_report.write_bytes(canonical_json_bytes(payload))
    with pytest.raises(ValueError, match="counter|receipt"):
        build(paths, tmp_path)


@pytest.mark.parametrize(
    "field",
    [
        "execution_confirmation_history",
        "service_authorization_evidence",
        "receipt_ledger",
        "write_receipts",
        "candidate_lots",
        "review_lifecycle",
        "service_probe",
        "acknowledgements",
        "retry_count",
        "human_decision",
    ],
)
@pytest.mark.parametrize("bad", [1, "PRIVATE_SENTINEL" * 100], ids=["integer", "private_string"])
@pytest.mark.parametrize("api", ["load", "build"])
def test_round2_malformed_safety_containers_are_bounded_value_errors(
    paths, tmp_path, field, bad, api
):
    from recallops.evaluation.scorecard import load_safety_report

    payload = json.loads(paths.safety_report.read_bytes())
    payload["results"][11]["state_excerpt"][field] = bad
    paths.safety_report.write_bytes(canonical_json_bytes(payload))
    with pytest.raises(ValueError) as failure:
        if api == "load":
            load_safety_report(paths.safety_report, paths.safety_corpus)
        else:
            build(paths, tmp_path)
    assert type(failure.value) is ValueError
    assert len(str(failure.value)) <= 200
    assert "PRIVATE_SENTINEL" not in str(failure.value)


@pytest.mark.parametrize("suite", ["safety", "retrieval", "orchestration"])
def test_round2_schema_errors_do_not_echo_observations(paths, tmp_path, suite):
    source = getattr(paths, f"{suite}_report")
    payload = json.loads(source.read_bytes())
    payload["execution_mode"] = "PRIVATE_SENTINEL" * 100
    if suite == "safety":
        source.write_bytes(canonical_json_bytes(payload))
    else:
        resign(source, payload, "report_sha256")
    with pytest.raises(ValueError) as failure:
        build(paths, tmp_path)
    assert type(failure.value) is ValueError
    assert len(str(failure.value)) <= 200
    assert "PRIVATE_SENTINEL" not in str(failure.value)


@pytest.mark.parametrize("operation", ["build", "validate"])
@pytest.mark.parametrize("suite", ["safety", "retrieval", "orchestration"])
def test_round2_private_copy_aba_has_no_mutable_loader_path(
    paths, tmp_path, monkeypatch, operation, suite
):
    from recallops.evaluation import scorecard as module

    build(paths, tmp_path)
    source = getattr(paths, f"{suite}_report")
    passing = source.read_bytes()
    payload = json.loads(passing)
    payload["gate_passed"] = False
    failed = canonical_json_bytes(payload)
    source.write_bytes(failed)
    output = tmp_path / "scorecard.json"
    if operation == "validate":
        scorecard = json.loads(output.read_bytes())
        scorecard["artifact_digests"][f"{suite}_report"] = hashlib.sha256(failed).hexdigest()
        resign(output, scorecard)
    original = getattr(module, f"load_{suite}_report")

    def swap_private_copy(path, case_path):
        before = path.read_bytes()
        path.write_bytes(passing)
        try:
            return original(path, case_path)
        finally:
            path.write_bytes(before)

    monkeypatch.setattr(module, f"load_{suite}_report", swap_private_copy)
    with pytest.raises(ValueError):
        if operation == "build":
            build(paths, tmp_path)
        else:
            validate_scorecard(output, paths)


@pytest.mark.parametrize("boundary", ["create", "replace"])
@pytest.mark.parametrize("swap", ["symlink", "rename"])
def test_round2_output_parent_swap_never_overwrites_input(
    paths, tmp_path, monkeypatch, boundary, swap
):
    from recallops.evaluation import scorecard as module

    intended = tmp_path / "intended"
    intended.mkdir()
    parent = tmp_path / "output-parent"
    if swap == "symlink":
        parent.symlink_to(intended, target_is_directory=True)
    else:
        parent.mkdir()
        intended = parent
    moved = tmp_path / "pinned-original"
    output = parent / paths.safety_report.name
    before = {name: getattr(paths, name).read_bytes() for name in paths.__dataclass_fields__}
    swapped = False

    def redirect():
        nonlocal swapped, intended
        if swapped:
            return
        swapped = True
        if swap == "symlink":
            parent.unlink()
        else:
            parent.rename(moved)
            intended = moved
        parent.symlink_to(paths.safety_report.parent, target_is_directory=True)

    if boundary == "create":
        original_open = module.os.open
        temporary_module = getattr(module, "tempfile", None)
        original_mkstemp = temporary_module.mkstemp if temporary_module is not None else None

        def redirected_open(path, flags, *args, **kwargs):
            if flags & os.O_CREAT:
                redirect()
            return original_open(path, flags, *args, **kwargs)

        def redirected_mkstemp(*args, **kwargs):
            redirect()
            return original_mkstemp(*args, **kwargs)

        monkeypatch.setattr(module.os, "open", redirected_open)
        if temporary_module is not None:
            monkeypatch.setattr(temporary_module, "mkstemp", redirected_mkstemp)
    else:
        original_replace = module.os.replace

        def redirected_replace(*args, **kwargs):
            redirect()
            return original_replace(*args, **kwargs)

        monkeypatch.setattr(module.os, "replace", redirected_replace)
    try:
        build_scorecard(
            paths.safety_report, paths.retrieval_report, paths.orchestration_report, output
        )
    except (ValueError, OSError):
        pass
    assert {name: getattr(paths, name).read_bytes() for name in before} == before
    if (intended / output.name).exists():
        assert validate_scorecard(intended / output.name, paths).offline_gate_passed


@pytest.mark.parametrize("kind", ["symbolic", "hard"])
def test_round2_unrelated_output_leaf_symlink_is_rejected(paths, tmp_path, kind):
    target = tmp_path / "other.json"
    target.write_text("keep me")
    output = tmp_path / "scorecard.json"
    if kind == "symbolic":
        output.symlink_to(target)
    else:
        os.link(target, output)
    with pytest.raises(ValueError):
        build(paths, tmp_path)
    assert target.read_text() == "keep me"


@pytest.mark.parametrize("suite", ["safety", "retrieval", "orchestration"])
def test_round2_public_path_and_bytes_loaders_agree(paths, suite):
    from recallops.evaluation import orchestration_benchmark, retrieval_benchmark, scorecard

    module = {
        "safety": scorecard,
        "retrieval": retrieval_benchmark,
        "orchestration": orchestration_benchmark,
    }[suite]
    path = getattr(paths, f"{suite}_report")
    corpus = getattr(paths, f"{suite}_corpus")
    path_loader = getattr(module, f"load_{suite}_report")
    bytes_loader = getattr(module, f"load_{suite}_report_bytes")
    kwargs = {} if suite == "safety" else {"data_dir": DATA_DIR}
    assert bytes_loader(path.read_bytes(), corpus.read_bytes(), **kwargs) == path_loader(
        path, corpus, **kwargs
    )


def test_round2_semantic_validation_materializes_no_snapshot_files(paths, tmp_path, monkeypatch):
    def forbid_materialized_snapshot(*args, **kwargs):
        raise AssertionError("validation must not materialize mutable snapshot files")

    monkeypatch.setattr(Path, "write_bytes", forbid_materialized_snapshot)
    scorecard = build(paths, tmp_path)
    assert validate_scorecard(tmp_path / "scorecard.json", paths) == scorecard


@pytest.mark.parametrize("suite", ["safety", "retrieval", "orchestration"])
def test_round2_public_corpus_bytes_loaders_reject_mutable_buffers(paths, suite):
    from recallops.evaluation.orchestration_schema import load_orchestration_cases_bytes
    from recallops.evaluation.retrieval_schema import load_retrieval_cases_bytes
    from recallops.evaluation.runner import load_scenarios_bytes

    loader = {
        "safety": load_scenarios_bytes,
        "retrieval": load_retrieval_cases_bytes,
        "orchestration": load_orchestration_cases_bytes,
    }[suite]
    raw = bytearray(getattr(paths, f"{suite}_corpus").read_bytes())
    with pytest.raises(ValueError, match="immutable bytes"):
        loader(raw)


@pytest.mark.parametrize(
    "field,bad",
    [
        ("execution_confirmation_history", {}),
        ("service_authorization_evidence", {}),
        ("candidate_lots", {}),
        ("receipt_ledger", {}),
        ("write_receipts", {}),
        ("service_probe", []),
        ("acknowledgements", []),
        ("retry_count", []),
        ("human_decision", []),
        ("execution_confirmation_history", [1]),
        ("receipt_ledger", [1]),
        ("service_authorization_evidence", [1]),
        ("service_probe", {"authorization_evidence": 1}),
        ("service_probe", {"authorization_evidence": [1]}),
        ("service_probe", {"write_receipts": {}}),
    ],
)
def test_round2_nested_and_opposite_container_shapes_fail_closed(paths, tmp_path, field, bad):
    payload = json.loads(paths.safety_report.read_bytes())
    payload["results"][11]["state_excerpt"][field] = bad
    paths.safety_report.write_bytes(canonical_json_bytes(payload))
    with pytest.raises(ValueError) as failure:
        build(paths, tmp_path)
    assert type(failure.value) is ValueError
    assert len(str(failure.value)) <= 200


@pytest.mark.parametrize("input_name", tuple(EvaluationArtifactPaths.__dataclass_fields__))
@pytest.mark.parametrize("alias", ["exact", "parent", "leaf"])
def test_round3_replaced_input_entry_is_protected(paths, tmp_path, monkeypatch, input_name, alias):
    from recallops.evaluation import scorecard as module

    output = getattr(paths, input_name)
    report_paths = [paths.safety_report, paths.retrieval_report, paths.orchestration_report]
    if alias == "leaf":
        if input_name.endswith("_report"):
            output = tmp_path / f"alias-{output.name}"
            output.symlink_to(getattr(paths, input_name))
            report_paths[report_paths.index(getattr(paths, input_name))] = output
        else:
            target = tmp_path / f"target-{output.name}"
            output.rename(target)
            output.symlink_to(target)
            paths = EvaluationArtifactPaths(*report_paths)
    elif alias == "parent":
        parent_alias = tmp_path / "alias-parent"
        parent_alias.symlink_to(tmp_path, target_is_directory=True)
        output = parent_alias / output.name
    inputs = {getattr(paths, name) for name in paths.__dataclass_fields__}
    before = {path: path.read_bytes() for path in inputs}
    replacement = tmp_path / "replacement"
    replacement.write_bytes(output.read_bytes())
    original_stat = Path.stat
    original_open = module.os.open
    original_write = module._write_scorecard
    observed = set()
    replaced = False
    writing = False

    def start_write(*args, **kwargs):
        nonlocal writing
        writing = True
        return original_write(*args, **kwargs)

    def replace_after_inode_collection(path, *args, **kwargs):
        nonlocal replaced
        result = original_stat(path, *args, **kwargs)
        if writing and path in inputs:
            observed.add(path)
        if not replaced and observed == inputs:
            replaced = True
            os.replace(replacement, output)
        return result

    def reject_temporary_creation(path, flags, *args, **kwargs):
        assert not flags & os.O_CREAT, "protected entries must reject before temp creation"
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", replace_after_inode_collection)
    monkeypatch.setattr(module.os, "open", reject_temporary_creation)
    monkeypatch.setattr(module, "_write_scorecard", start_write)
    with pytest.raises(ValueError, match="overwrite|alias"):
        build_scorecard(*report_paths, output)
    assert replaced
    assert {path: path.read_bytes() for path in inputs} == before
    assert output.read_bytes() == before[getattr(paths, input_name)]
    assert not list(tmp_path.glob(".scorecard-*"))
