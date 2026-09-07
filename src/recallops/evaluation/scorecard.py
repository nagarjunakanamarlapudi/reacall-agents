"""Verified offline scorecards, bound to exact report and corpus bytes.

Digests establish content integrity, not execution authenticity. The legacy safety
report records assertion observations and a state excerpt, not a replayable run.
This adapter checks those observations against the public safety contracts and
recomputes every aggregate; it does not claim to rerun the original execution.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import stat
import unicodedata
from collections import Counter
from contextlib import ExitStack
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StrictBool, StrictInt

from recallops.evaluation.digests import (
    artifact_validation,
    canonical_json_bytes,
    canonical_sha256,
    decode_artifact_bytes,
    verify_sha256,
)
from recallops.evaluation.metrics import calculate_metrics, safety_gate_passes
from recallops.evaluation.orchestration_benchmark import (
    load_orchestration_report as load_orchestration_report,
)
from recallops.evaluation.orchestration_benchmark import load_orchestration_report_bytes
from recallops.evaluation.retrieval_benchmark import load_retrieval_report as load_retrieval_report
from recallops.evaluation.retrieval_benchmark import load_retrieval_report_bytes
from recallops.evaluation.runner import (
    STATE_EXCERPT_FIELDS,
    load_scenarios_bytes,
    normalize_route,
    validate_persisted_safety_containers,
    validate_persisted_safety_counters,
)
from recallops.evaluation.schema import EvaluationReport
from recallops.paths import DATA_DIR, EvaluationArtifactPaths

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
    try:
        raw = Path(path).read_bytes()
    except OSError:
        raise ValueError("evaluation artifact could not be read") from None
    return raw, decode_artifact_bytes(raw)


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
    try:
        raw, case_raw = Path(path).read_bytes(), Path(case_path).read_bytes()
    except OSError:
        raise ValueError("safety report inputs could not be read") from None
    return load_safety_report_bytes(raw, case_raw)


def load_safety_report_bytes(raw: bytes, case_raw: bytes) -> EvaluationReport:
    """Validate exact immutable bytes against the captured legacy scenario corpus."""
    with artifact_validation("safety report"):
        return _load_safety_report_bytes(raw, case_raw)


def _load_safety_report_bytes(raw: bytes, case_raw: bytes) -> EvaluationReport:
    payload = decode_artifact_bytes(raw)
    decode_artifact_bytes(case_raw)
    corpus = load_scenarios_bytes(case_raw)
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
        validate_persisted_safety_containers(row.state_excerpt)
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
    safety = load_safety_report_bytes(snapshots["safety_report"], snapshots["safety_corpus"])
    retrieval = load_retrieval_report_bytes(
        snapshots["retrieval_report"],
        snapshots["retrieval_corpus"],
        data_dir=DATA_DIR,
    )
    orchestration = load_orchestration_report_bytes(
        snapshots["orchestration_report"],
        snapshots["orchestration_corpus"],
        data_dir=DATA_DIR,
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


def _open_output_directory(parent: Path, *, create: bool = True) -> int:
    """Resolve once, then open and verify each directory component without following links."""
    directory = parent.resolve()
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    current = os.open(directory.anchor, flags)
    try:
        for component in directory.parts[1:]:
            try:
                child = os.open(component, flags, dir_fd=current)
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(component, mode=0o755, dir_fd=current)
                child = os.open(component, flags, dir_fd=current)
            try:
                entry = os.stat(component, dir_fd=current, follow_symlinks=False)
                opened = os.fstat(child)
                if not stat.S_ISDIR(entry.st_mode) or (entry.st_dev, entry.st_ino) != (
                    opened.st_dev,
                    opened.st_ino,
                ):
                    raise ValueError("scorecard destination directory identity changed")
            except BaseException:
                os.close(child)
                raise
            os.close(current)
            current = child
        return current
    except BaseException:
        os.close(current)
        raise


def _normalized_component(value: str) -> str:
    """Conservatively collide canonical Unicode and case-equivalent spellings."""
    return unicodedata.normalize("NFC", unicodedata.normalize("NFC", value).casefold())


def _path_key(path: Path) -> tuple[str, ...]:
    absolute = Path(os.path.abspath(path.expanduser()))
    return tuple(_normalized_component(part) for part in absolute.parts)


def _write_scorecard(
    output_path: Path,
    raw: bytes,
    paths: EvaluationArtifactPaths,
    input_entries: set[tuple[int, int, str]],
    input_keys: set[tuple[str, ...]],
    input_parents: dict[Path, tuple[int, int]],
) -> None:
    output_path = output_path.expanduser().absolute()
    output_keys = {_path_key(output_path), _path_key(output_path.resolve())}
    input_inodes = set()
    for name in paths.__dataclass_fields__:
        source = getattr(paths, name).stat()
        input_inodes.add((source.st_dev, source.st_ino))
    directory_fd = _open_output_directory(output_path.parent)
    identity = os.fstat(directory_fd)
    leaf = output_path.name
    temporary = None

    def check_leaf():
        # Keep the initial lexical/resolved exclusions even when the pathname
        # now resolves through a replacement directory or a fresh file inode.
        current_keys = output_keys | {_path_key(output_path.resolve())}
        if (
            current_keys & input_keys
            or (
                identity.st_dev,
                identity.st_ino,
                _normalized_component(leaf),
            )
            in input_entries
        ):
            raise ValueError("scorecard output must not overwrite or alias an input artifact")
        for parent_path, expected_identity in input_parents.items():
            try:
                current_parent = parent_path.stat()
            except OSError as exc:
                raise ValueError("protected input parent is unavailable") from exc
            if (current_parent.st_dev, current_parent.st_ino) != expected_identity:
                raise ValueError("protected input parent identity changed")
        try:
            entry = os.stat(leaf, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            return
        if (
            stat.S_ISLNK(entry.st_mode)
            or entry.st_nlink > 1
            or (entry.st_dev, entry.st_ino) in input_inodes
        ):
            raise ValueError("scorecard output must not overwrite or alias an input artifact")
        if not stat.S_ISREG(entry.st_mode):
            raise ValueError("scorecard output must be a regular file")

    try:
        check_leaf()
        for _ in range(10):
            candidate = f".scorecard-{secrets.token_hex(16)}"
            try:
                descriptor = os.open(
                    candidate,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=directory_fd,
                )
            except FileExistsError:
                continue
            temporary = candidate
            break
        else:
            raise OSError("could not create scorecard temporary inode")
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        current = os.fstat(directory_fd)
        if (current.st_dev, current.st_ino) != (identity.st_dev, identity.st_ino):
            raise ValueError("scorecard destination directory identity changed")
        check_leaf()
        os.replace(temporary, leaf, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        os.fsync(directory_fd)
    finally:
        try:
            if temporary is not None:
                try:
                    os.unlink(temporary, dir_fd=directory_fd)
                except FileNotFoundError:
                    pass
        finally:
            os.close(directory_fd)


def build_scorecard(
    safety_path: Path,
    retrieval_path: Path,
    orchestration_path: Path,
    output_path: Path,
) -> EvaluationScorecard:
    """Build a canonical scorecard from complete, independently verified inputs."""
    paths = EvaluationArtifactPaths(safety_path, retrieval_path, orchestration_path)
    # Preserve lexical and initially resolved paths before validating snapshots.
    locations = [Path(safety_path), Path(retrieval_path), Path(orchestration_path)]
    locations.extend(getattr(paths, name) for name in paths.__dataclass_fields__)
    for suite, filename in zip(
        SUITES, ("scenarios.json", "retrieval_cases.json", "orchestration_cases.json"), strict=True
    ):
        locations.append(getattr(paths, f"{suite}_report").with_name(filename))
    locations = {location.expanduser().absolute() for location in locations}
    locations |= {location.resolve() for location in locations}
    input_keys = {_path_key(location) for location in locations}
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
    with artifact_validation("scorecard"):
        scorecard = EvaluationScorecard.model_validate(payload)
    # Pin both lexical entries and resolved targets: replacing an input inode
    # (including a caller-supplied symlink) must not make its location writable.
    with ExitStack() as directories:
        input_entries = set()
        input_parents = {}
        for location in locations:
            if location.parent not in input_parents:
                descriptor = _open_output_directory(location.parent, create=False)
                directories.callback(os.close, descriptor)
                parent = os.fstat(descriptor)
                input_parents[location.parent] = (parent.st_dev, parent.st_ino)
            input_entries.add(
                (*input_parents[location.parent], _normalized_component(location.name))
            )
        _write_scorecard(
            Path(output_path),
            canonical_json_bytes(scorecard.model_dump(mode="json")),
            paths,
            input_entries,
            input_keys,
            input_parents,
        )
    return scorecard


def validate_scorecard(path: Path, expected_paths: EvaluationArtifactPaths) -> EvaluationScorecard:
    """Verify self-digest, exact inputs, current corpora, summaries and offline gate."""
    raw, payload = _read_json(path)
    if raw != canonical_json_bytes(payload):
        raise ValueError("scorecard must use canonical JSON")
    with artifact_validation("scorecard"):
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
