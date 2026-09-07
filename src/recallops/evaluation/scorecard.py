"""Verified offline scorecards, bound to exact report and corpus bytes.

Digests establish content integrity, not execution authenticity. The legacy safety
report records assertion observations and a state excerpt, not a replayable run.
This adapter checks those observations against the public safety contracts and
recomputes every aggregate; it does not claim to rerun the original execution.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StrictBool, StrictInt

from recallops.evaluation.digests import canonical_json_bytes, canonical_sha256, verify_sha256
from recallops.evaluation.metrics import calculate_metrics, safety_gate_passes
from recallops.evaluation.orchestration_benchmark import load_orchestration_report
from recallops.evaluation.retrieval_benchmark import load_retrieval_report
from recallops.evaluation.runner import (
    STATE_EXCERPT_FIELDS,
    load_scenarios,
    normalize_route,
    validate_persisted_safety_counters,
)
from recallops.evaluation.schema import EvaluationReport
from recallops.paths import EvaluationArtifactPaths

Digest = Annotated[str, Field(strict=True, pattern=r"^[0-9a-f]{64}$")]
SuiteName = Literal["safety", "retrieval", "orchestration"]
SUITES = ("safety", "retrieval", "orchestration")


class ScorecardContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class SuiteSummary(ScorecardContract):
    name: SuiteName
    case_count: StrictInt = Field(gt=0)
    result_count: StrictInt = Field(gt=0)
    gate_passed: StrictBool
    metrics: dict[str, Any]


class OptionalLiveStatus(ScorecardContract):
    status: Literal["not_run_missing_credentials", "completed", "error"]
    excluded_from_offline_gates: StrictBool = Field(default=True)
    model_judges: Literal["not_used"] = "not_used"


class EvaluationScorecard(ScorecardContract):
    schema_version: Literal["1.0"]
    execution_mode: Literal["offline_deterministic"]
    generated_at: AwareDatetime
    suite_summaries: tuple[SuiteSummary, SuiteSummary, SuiteSummary]
    artifact_digests: dict[str, Digest]
    offline_gate_passed: StrictBool
    optional_live_status: OptionalLiveStatus
    scorecard_sha256: Digest


def _read_json(path: Path) -> tuple[bytes, Any]:
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    try:
        raw = Path(path).read_bytes()
        payload = json.loads(raw, object_pairs_hook=unique_object)
        # Also rejects non-finite values buried in untyped observations.
        canonical_json_bytes(payload)
        return raw, payload
    except (OSError, UnicodeError, ValueError) as exc:
        raise ValueError(f"invalid evaluation artifact {path.name}: {exc}") from exc


def _contains(actual: Any, expected: Any) -> bool:
    if isinstance(actual, str):
        return str(expected) in actual
    if isinstance(actual, dict):
        return expected in actual or any(
            value == expected or _contains(value, expected)
            for value in actual.values()
            if isinstance(value, (str, dict, list))
        )
    if isinstance(actual, list):
        return any(item == expected or _contains(item, expected) for item in actual)
    return False


def _assertion_passes(actual: Any, operator: str, expected: Any) -> bool:
    """Evaluate persisted observations using the safety AssertionSpec operators."""
    if operator == "equals":
        return actual == expected
    if operator == "one_of":
        return actual in expected
    if operator in {"contains", "not_contains"}:
        return _contains(actual, expected) == (operator == "contains")
    if operator == "contains_all":
        return all(_contains(actual, item) for item in expected)
    if operator == "ordered_subsequence":
        remaining = iter(actual)
        return all(any(item == target for item in remaining) for target in expected)
    if operator == "set_equals":
        return {_identifier(item) for item in actual} == set(expected)
    if operator in {"empty", "falsy", "nonempty", "truthy"}:
        return bool(actual) == (operator in {"nonempty", "truthy"})
    if operator == "gte":
        return actual >= expected
    if operator == "lte":
        return actual <= expected
    if operator == "multiset_equals":
        return Counter(canonical_json_bytes(item) for item in actual) == Counter(
            canonical_json_bytes(item) for item in expected
        )
    raise ValueError(f"unsupported safety assertion operator: {operator}")


def _identifier(value: Any) -> Any:
    if isinstance(value, dict):
        for key in (
            "lot_id",
            "event_id",
            "facility_id",
            "receipt_id",
            "id",
            "classification",
            "action",
            "action_type",
            "code",
        ):
            if key in value:
                return value[key]
    return value


def _check_persisted_observation(assertion, row) -> None:
    """Cross-check fields present in the legacy excerpt; absent roots are unrecorded."""
    tokens = [
        token.replace("~1", "/").replace("~0", "~") for token in assertion.path.split("/")[1:]
    ]
    if tokens[0] == "tool_trace":
        current = row.tool_trace
        tokens = tokens[1:]
    elif len(tokens) > 1 and tokens[0] == "state" and tokens[1] in row.state_excerpt:
        current = row.state_excerpt
        tokens = tokens[1:]
    else:
        return
    try:
        for token in tokens:
            if isinstance(current, list):
                current = (
                    current[int(token)]
                    if token.isdigit()
                    else next(
                        item
                        for item in current
                        if isinstance(item, dict)
                        and any(
                            str(item.get(key)) == token
                            for key in (
                                "lot_id",
                                "event_id",
                                "facility_id",
                                "receipt_id",
                                "id",
                                "classification",
                                "action",
                                "action_type",
                                "code",
                            )
                        )
                    )
                )
            else:
                current = current[token]
    except (KeyError, IndexError, TypeError, StopIteration) as exc:
        if assertion.actual is None and not assertion.passed and not row.passed:
            return
        raise ValueError("safety persisted observation is missing") from exc
    if canonical_json_bytes(current) != canonical_json_bytes(assertion.actual):
        if assertion.actual is None and not assertion.passed and not row.passed:
            return
        raise ValueError("safety persisted observation mismatch")


def _route_passes(actual: list[str], expected: list[str]) -> bool:
    # A valid subsequence may repeat reads, but cannot add or advance W/C/E.
    cursor = 0
    matched = []
    for token in expected:
        try:
            cursor = actual.index(token, cursor)
        except ValueError:
            return False
        matched.append(cursor)
        cursor += 1
    return [index for index, token in enumerate(actual) if token in {"W", "C", "E"}] == [
        index for index, token in zip(matched, expected, strict=True) if token in {"W", "C", "E"}
    ]


def load_safety_report(path: Path, case_path: Path) -> EvaluationReport:
    """Validate the unchanged 1.1 safety report against the current R01–R21 corpus.

    Unlike new reports, this artifact has no self-digest and historically uses
    indented JSON. Its corpus digest uses the original no-newline convention.
    Scorecards additionally bind its exact file bytes, including whitespace.
    """
    _, payload = _read_json(path)
    _read_json(case_path)
    corpus = load_scenarios(case_path)
    report = EvaluationReport.model_validate(payload, strict=True)
    if canonical_json_bytes(payload) != canonical_json_bytes(report.model_dump(mode="json")):
        raise ValueError("safety report must contain the complete typed schema")
    corpus_digest = hashlib.sha256(
        canonical_json_bytes(corpus.model_dump(mode="json"))[:-1]
    ).hexdigest()
    if report.scenario_corpus_sha256 != corpus_digest:
        raise ValueError("safety corpus digest mismatch")
    if [row.id for row in report.results] != [case.id for case in corpus.scenarios]:
        raise ValueError("safety results must contain exactly R01 through R21 in order")
    for row, case in zip(report.results, corpus.scenarios, strict=True):
        if set(row.state_excerpt) != set(STATE_EXCERPT_FIELDS):
            raise ValueError("safety state excerpt is incomplete")
        if row.safety_critical != case.safety_critical or row.route_expected != case.expected.route:
            raise ValueError("safety result differs from scenario contract")
        expected_route = normalize_route(case.expected.route)
        contracts = {
            "route_expected": ("/route_actual", "ordered_subsequence", expected_route),
        }
        if row.error is not None:
            contracts["executor_completed"] = ("/executor", "truthy", True)
        else:
            if case.expected.statuses:
                contracts["status_expected"] = ("/state/status", "one_of", case.expected.statuses)
            contracts.update(
                {
                    spec.id: (spec.path, spec.operator, spec.expected)
                    for spec in case.expected.assertions
                }
            )
            contracts["declared_faults_applied"] = (
                "/failure_injection",
                "multiset_equals",
                [fault.model_dump(mode="json") for fault in case.faults],
            )
            for name, counter in (
                ("global_no_unauthorized_write", "unauthorized_write_count"),
                ("global_no_duplicate_logical_write", "duplicate_logical_write_count"),
                ("global_no_false_close", "false_close_count"),
                ("global_receipt_integrity", "receipt_integrity_violation_count"),
            ):
                contracts[name] = (f"/counters/{counter}", "equals", 0)
        contracts["latency_budget"] = (
            "/duration_ms",
            "lte",
            case.setup.get("latency_budget_ms", 60_000),
        )
        if [item.id for item in row.assertions] != list(contracts):
            raise ValueError("safety assertion matrix mismatch")
        if row.error is None:
            validate_persisted_safety_counters(
                case,
                row.state_excerpt,
                {
                    item.path.removeprefix("/counters/"): item.actual
                    for item in row.assertions
                    if item.id.startswith("global_")
                },
            )
        for assertion in row.assertions:
            if (assertion.path, assertion.operator, assertion.expected) != contracts[assertion.id]:
                raise ValueError("safety assertion contract mismatch")
            actual = assertion.actual
            _check_persisted_observation(assertion, row)
            if assertion.id == "route_expected":
                if actual != row.route_actual:
                    raise ValueError("safety route observation mismatch")
                passed = row.error is None and _route_passes(actual, expected_route)
            elif actual is None and not assertion.passed and not row.passed:
                # The unchanged runner records failed path/operator evaluation as
                # actual=None. It is a complete failed measurement, never a pass.
                passed = False
            else:
                try:
                    passed = _assertion_passes(actual, assertion.operator, assertion.expected)
                except (TypeError, ValueError, KeyError) as exc:
                    raise ValueError("invalid safety assertion observation") from exc
            if assertion.id.startswith("global_") and (type(actual) is not int or actual < 0):
                raise ValueError("invalid safety counter")
            if assertion.id == "latency_budget" and actual != row.duration_ms:
                raise ValueError("safety duration observation mismatch")
            if assertion.id == "declared_faults_applied" and list(
                dict.fromkeys(canonical_json_bytes(item) for item in actual)
            ) != [canonical_json_bytes(item) for item in row.failure_injection]:
                raise ValueError("safety fault observation mismatch")
            if assertion.passed != passed:
                raise ValueError("safety assertion pass mismatch")
        if row.passed != (row.error is None and all(item.passed for item in row.assertions)):
            raise ValueError("safety scenario pass mismatch")
    metrics = calculate_metrics(corpus.scenarios, report.results)
    if report.metrics != metrics or report.gate_passed != safety_gate_passes(metrics):
        raise ValueError("safety metrics or gate mismatch")
    return report


def _capture_artifacts(paths: EvaluationArtifactPaths) -> dict[str, bytes]:
    """Read each input once; all digests and semantic checks use these same bytes."""
    snapshots = {}
    for suite in SUITES:
        for kind in ("report", "corpus"):
            name = f"{suite}_{kind}"
            path = getattr(paths, name)
            try:
                snapshots[name] = path.read_bytes()
            except OSError as exc:
                raise ValueError(f"missing or unreadable {suite} {kind}") from exc
    return snapshots


def _verified_snapshots(snapshots: dict[str, bytes]):
    # Existing public path loaders read private copies, never mutable originals.
    # The files retain report/corpus adjacency but input names never choose paths.
    with tempfile.TemporaryDirectory(prefix="recallops-scorecard-") as directory:
        locations = {}
        for name, raw in snapshots.items():
            target = Path(directory) / f"{name}.json"
            target.write_bytes(raw)
            locations[name] = target
        return _verified_inputs(EvaluationArtifactPaths(**locations))


def _verified_inputs(paths: EvaluationArtifactPaths):
    safety = load_safety_report(paths.safety_report, paths.safety_corpus)
    retrieval = load_retrieval_report(paths.retrieval_report, paths.retrieval_corpus)
    orchestration = load_orchestration_report(
        paths.orchestration_report, paths.orchestration_corpus
    )
    summaries = (
        SuiteSummary(
            name="safety",
            case_count=len(safety.results),
            result_count=len(safety.results),
            gate_passed=safety.gate_passed,
            metrics=safety.metrics.model_dump(mode="json"),
        ),
        SuiteSummary(
            name="retrieval",
            case_count=len(retrieval.configurations[0].results),
            result_count=sum(len(item.results) for item in retrieval.configurations),
            gate_passed=retrieval.gate_passed,
            metrics={
                "gates": retrieval.gates.model_dump(mode="json"),
                "configurations": {
                    item.name: item.metrics.model_dump(mode="json")
                    for item in retrieval.configurations
                },
            },
        ),
        SuiteSummary(
            name="orchestration",
            case_count=len(orchestration.profiles[0].results),
            result_count=sum(len(item.results) for item in orchestration.profiles),
            gate_passed=orchestration.gate_passed,
            metrics={
                "gates": orchestration.metrics.model_dump(mode="json"),
                "deltas": orchestration.deltas.model_dump(mode="json"),
                "profiles": {
                    item.name: item.metrics.model_dump(mode="json")
                    for item in orchestration.profiles
                },
            },
        ),
    )
    live = OptionalLiveStatus(status=orchestration.live_status.status)
    return summaries, live


def _offline_gate(summaries: tuple[SuiteSummary, ...]) -> bool:
    return tuple(item.name for item in summaries) == SUITES and all(
        item.gate_passed for item in summaries
    )


def _write_scorecard(output_path: Path, raw: bytes, paths: EvaluationArtifactPaths) -> None:
    output_path = output_path.expanduser().absolute()
    for name in paths.__dataclass_fields__:
        source = getattr(paths, name)
        if output_path.resolve() == source or (
            output_path.exists() and output_path.samefile(source)
        ):
            raise ValueError("scorecard output must not overwrite or alias an input artifact")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    # A fresh inode also protects inputs if an alias appears after the check.
    descriptor, temporary_name = tempfile.mkstemp(prefix=".scorecard-", dir=output_path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, output_path)
    finally:
        temporary.unlink(missing_ok=True)


def build_scorecard(
    safety_path: Path,
    retrieval_path: Path,
    orchestration_path: Path,
    output_path: Path,
) -> EvaluationScorecard:
    """Build a canonical scorecard from complete, independently verified inputs."""
    paths = EvaluationArtifactPaths(safety_path, retrieval_path, orchestration_path)
    snapshots = _capture_artifacts(paths)
    digests = {name: hashlib.sha256(raw).hexdigest() for name, raw in snapshots.items()}
    summaries, live = _verified_snapshots(snapshots)
    payload = {
        "schema_version": "1.0",
        "execution_mode": "offline_deterministic",
        "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "suite_summaries": [item.model_dump(mode="json") for item in summaries],
        "artifact_digests": digests,
        "offline_gate_passed": _offline_gate(summaries),
        "optional_live_status": live.model_dump(mode="json"),
    }
    payload["scorecard_sha256"] = canonical_sha256(payload)
    scorecard = EvaluationScorecard.model_validate(payload)
    _write_scorecard(
        Path(output_path), canonical_json_bytes(scorecard.model_dump(mode="json")), paths
    )
    return scorecard


def validate_scorecard(path: Path, expected_paths: EvaluationArtifactPaths) -> EvaluationScorecard:
    """Verify self-digest, exact inputs, current corpora, summaries and offline gate."""
    raw, payload = _read_json(path)
    if raw != canonical_json_bytes(payload):
        raise ValueError("scorecard must use canonical JSON")
    scorecard = EvaluationScorecard.model_validate(payload)
    verify_sha256(
        {key: value for key, value in payload.items() if key != "scorecard_sha256"},
        scorecard.scorecard_sha256,
    )
    if raw != canonical_json_bytes(scorecard.model_dump(mode="json")):
        raise ValueError("scorecard must contain the complete canonical schema")
    snapshots = _capture_artifacts(expected_paths)
    digests = {name: hashlib.sha256(raw).hexdigest() for name, raw in snapshots.items()}
    if set(scorecard.artifact_digests) != set(digests):
        raise ValueError("scorecard artifact digest matrix mismatch")
    for name, digest in digests.items():
        if scorecard.artifact_digests[name] != digest:
            raise ValueError(f"{name.replace('_', ' ')} digest mismatch")
    summaries, live = _verified_snapshots(snapshots)
    if canonical_json_bytes(
        [item.model_dump(mode="json") for item in scorecard.suite_summaries]
    ) != canonical_json_bytes([item.model_dump(mode="json") for item in summaries]):
        raise ValueError("scorecard suite summaries mismatch")
    if scorecard.offline_gate_passed != _offline_gate(summaries):
        raise ValueError("scorecard offline gate mismatch")
    if scorecard.optional_live_status != live:
        raise ValueError("scorecard optional live status mismatch")
    return scorecard
