"""Injectable UI adapter plus a credential-free deterministic demonstration.

The protocol is deliberately shaped around the locked ``RecallOpsRuntime``
result boundary: every action accepts the latest case snapshot and returns a
JSON-only snapshot.  The deterministic adapter is explicit training state; it
does not claim durable LangGraph recovery or live system connectivity.
"""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal, Protocol
from uuid import uuid4

from recallops.agents.planner import plan_investigation
from recallops.agents.runtime import RecallOpsRuntime
from recallops.agents.specialists import investigate_recall
from recallops.data.loaders import load_demo_dataset, load_recall_snapshot
from recallops.paths import PROJECT_ROOT, RepositoryPaths
from recallops.services.recall_registry import RecallRegistryService
from recallops.services.traceability import TraceabilityService
from recallops.ui.presenters import PINNED_RECALL

SYNTHETIC = "SYNTHETIC_RETAILER_DIGITAL_TWIN"
SNAPSHOT = "OFFICIAL_OPENFDA_SNAPSHOT"
FIXED_TIME = "2026-08-30T12:00:00+00:00"

FAILURE_SCENARIOS = (
    "openFDA unavailable → labelled frozen snapshot",
    "read timeout/429 → bounded retry then circuit-open/fallback",
    "ambiguous lot → pause/no auto-hold",
    "missing shipment/quantity discrepancy → gap/closure blocker",
    "unacknowledged store/facility → open/follow-up after approval",
    "lost write response → same-key replay",
    "stale version → refresh/review",
    "repeated graph progress → watchdog escalation",
    "model error/budget → deterministic fallback",
)

_RUNTIME_FAILURES = {
    FAILURE_SCENARIOS[0]: "registry_transient_failure",
    FAILURE_SCENARIOS[1]: "registry_transient_failure",
    FAILURE_SCENARIOS[5]: "lost_write_response",
    FAILURE_SCENARIOS[6]: "stale_decision_version",
    FAILURE_SCENARIOS[7]: "repeated_progress_signature",
    FAILURE_SCENARIOS[8]: "model_failure",
}
_RUNTIME_FAILURE_STAGES = {
    "registry_transient_failure": "run",
    "repeated_progress_signature": "run",
    "model_failure": "run",
    "stale_decision_version": "review",
    "lost_write_response": "simulate",
}
_FAILURE_NEXT_STEPS = {
    "run": "Run investigation",
    "review": "Submit the pending Human Review decision",
    "simulate": "Simulate approved actions",
}

_EVALUATION_RATE_METRICS = (
    "scenario_pass_rate",
    "safety_critical_pass_rate",
    "route_accuracy",
    "match_classification_accuracy",
    "lineage_accuracy",
    "quantity_evidence_coverage",
    "gap_detection_recall",
    "approval_guard_rate",
    "idempotency_integrity",
    "closure_guard_rate",
    "recovery_correctness",
    "bounded_execution_rate",
    "retrieval_evidence_coverage",
    "trace_completeness",
    "latency_budget_rate",
)
_EVALUATION_UNSAFE_COUNTERS = (
    "unauthorized_write_count",
    "duplicate_logical_write_count",
    "false_close_count",
    "receipt_integrity_violation_count",
)


class RecallOpsUIAdapter(Protocol):
    async def open_case(self, recall_number: str) -> dict[str, Any]: ...

    async def run_investigation(self, case: Mapping[str, Any]) -> dict[str, Any]: ...

    async def resume_review(
        self,
        case: Mapping[str, Any],
        *,
        decision: str,
        actor: str,
        justification: str,
        edited_action: str,
    ) -> dict[str, Any]: ...

    async def simulate_approved_actions(self, case: Mapping[str, Any]) -> dict[str, Any]: ...

    async def request_closure(self, case: Mapping[str, Any]) -> dict[str, Any]: ...

    async def inject_failure(self, case: Mapping[str, Any], scenario: str) -> dict[str, Any]: ...


def normalize_runtime_result(result: Any) -> dict[str, Any]:
    """Normalize the locked RuntimeResult without importing the runtime early.

    This is the product seam over the durable LangGraph runtime:
    ``RuntimeResult`` exposes ``case``, ``pending_interrupt``, ``next_nodes``
    and ``checkpoint_id`` as an immutable, detached audit view. Mapping
    fixtures with the same fields are accepted for integration tests.
    """

    if hasattr(result, "model_dump"):
        payload = result.model_dump(mode="json")
    elif isinstance(result, Mapping):
        payload = copy.deepcopy(dict(result))
    else:
        raise TypeError("runtime result must be a mapping or Pydantic model")
    case = payload.get("case")
    if not isinstance(case, Mapping):
        raise ValueError("runtime result lacks a case mapping")
    return _project_runtime_case(
        copy.deepcopy(dict(case)),
        pending=copy.deepcopy(payload.get("pending_interrupt")),
        next_nodes=list(payload.get("next_nodes") or []),
        checkpoint_id=payload.get("checkpoint_id"),
    )


def normalize_runtime_history(results: Any) -> list[dict[str, Any]]:
    """Return a compact JSON audit trail from detached runtime history results."""

    history: list[dict[str, Any]] = []
    for result in results:
        if hasattr(result, "model_dump"):
            payload = result.model_dump(mode="json")
        elif isinstance(result, Mapping):
            payload = copy.deepcopy(dict(result))
        else:
            raise TypeError("runtime history items must be mappings or Pydantic models")
        case = payload.get("case")
        if not isinstance(case, Mapping):
            raise ValueError("runtime history item lacks a case mapping")
        pending = payload.get("pending_interrupt")
        history.append(
            {
                "checkpoint_id": payload.get("checkpoint_id"),
                "status": case.get("status"),
                "case_version": case.get("case_version"),
                "pending_kind": pending.get("kind") if isinstance(pending, Mapping) else None,
                "next_nodes": list(payload.get("next_nodes") or []),
            }
        )
    return history


def _canonical_digest(payload: Any) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _deep_merge_json(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(dict(base))
    for key, value in override.items():
        if isinstance(value, Mapping) and isinstance(merged.get(key), Mapping):
            merged[key] = _deep_merge_json(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def _corpus_digest_candidates(corpus: Mapping[str, Any]) -> set[str]:
    """Accept the source corpus and the evaluator's validated/expanded representation."""

    candidates = {_canonical_digest(corpus)}
    common = corpus.get("common_input")
    scenarios = corpus.get("scenarios")
    if isinstance(common, Mapping) and isinstance(scenarios, list):
        expanded = copy.deepcopy(dict(corpus))
        expanded["scenarios"] = [
            {
                **copy.deepcopy(dict(item)),
                "input": _deep_merge_json(
                    common,
                    item.get("input") if isinstance(item.get("input"), Mapping) else {},
                ),
            }
            if isinstance(item, Mapping)
            else item
            for item in scenarios
        ]
        candidates.add(_canonical_digest(expanded))
        schema_normalized = copy.deepcopy(expanded)
        for scenario in schema_normalized["scenarios"]:
            if not isinstance(scenario, dict):
                continue
            for fault in scenario.get("faults", []):
                if isinstance(fault, dict):
                    fault.setdefault("parameters", {})
            expected = scenario.get("expected")
            if not isinstance(expected, dict):
                continue
            for assertion in expected.get("assertions", []):
                if isinstance(assertion, dict):
                    assertion.setdefault("expected", None)
                    assertion.setdefault("safety_invariant", "")
        candidates.add(_canonical_digest(schema_normalized))
    try:
        from recallops.evaluation.schema import ScenarioCorpus
    except ImportError:
        return candidates
    try:
        validated = ScenarioCorpus.model_validate(corpus).model_dump(mode="json")
    except (TypeError, ValueError):
        return candidates
    candidates.add(_canonical_digest(validated))
    return candidates


def _evaluation_unavailable(status: str, message: str) -> dict[str, Any]:
    return {
        "status": status,
        "source": "committed_evaluation_report",
        "message": f"{message} No passing score is claimed.",
        "gate_passed": None,
        "scenario_count": 0,
        "scenarios": [],
        "metrics": {},
        "unsafe_counters": {},
    }


def _require_rate(metrics: Mapping[str, Any], name: str) -> float:
    value = metrics.get(name)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1:
        raise ValueError(f"metric {name} must be a rate from zero through one")
    return float(value)


def _require_counter(metrics: Mapping[str, Any], name: str) -> int:
    value = metrics.get(name)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"metric {name} must be a non-negative integer")
    return value


def _project_committed_evaluation_report(
    report: Mapping[str, Any], corpus: Mapping[str, Any]
) -> dict[str, Any]:
    if report.get("schema_version") != "1.1":
        raise ValueError("unsupported evaluation report schema")
    if report.get("execution_mode") != "offline_deterministic":
        raise ValueError("evaluation execution mode is not offline_deterministic")
    metadata = report.get("run_metadata")
    if not isinstance(metadata, Mapping) or (
        metadata.get("report_kind") != "run_specific_observation"
        or metadata.get("telemetry_policy") != "observed_only"
        or metadata.get("timing_source") not in {"measured_wall_clock", "scripted_clock"}
    ):
        raise ValueError("evaluation run metadata is invalid")
    digest = report.get("scenario_corpus_sha256")
    if (
        not isinstance(digest, str)
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
    ):
        raise ValueError("evaluation corpus digest is invalid")
    if digest not in _corpus_digest_candidates(corpus):
        raise RuntimeError("evaluation report digest does not match the committed scenario corpus")
    results = report.get("results")
    if not isinstance(results, list) or not results or len(results) > 100:
        raise ValueError("evaluation report results must be a bounded non-empty list")
    corpus_scenarios = corpus.get("scenarios")
    if not isinstance(corpus_scenarios, list):
        raise ValueError("evaluation scenario corpus has no scenario list")
    corpus_ids = [item.get("id") for item in corpus_scenarios if isinstance(item, Mapping)]
    if len(corpus_ids) != len(corpus_scenarios) or any(
        not isinstance(identifier, str) or not identifier for identifier in corpus_ids
    ):
        raise ValueError("evaluation scenario corpus has invalid identifiers")

    scenario_rows: list[dict[str, Any]] = []
    result_ids: list[str] = []
    passed_count = 0
    critical_count = 0
    critical_passed = 0
    for result in results:
        if not isinstance(result, Mapping):
            raise ValueError("evaluation result must be a mapping")
        identifier = result.get("id")
        passed = result.get("passed")
        safety_critical = result.get("safety_critical")
        assertions = result.get("assertions")
        error = result.get("error")
        if (
            not isinstance(identifier, str)
            or not identifier
            or not isinstance(passed, bool)
            or not isinstance(safety_critical, bool)
            or not isinstance(assertions, list)
            or not assertions
            or not all(
                isinstance(assertion, Mapping)
                and isinstance(assertion.get("passed"), bool)
                for assertion in assertions
            )
            or (error is not None and not isinstance(error, str))
        ):
            raise ValueError("evaluation result fields are invalid")
        derived_pass = error is None and all(assertion["passed"] for assertion in assertions)
        if passed != derived_pass:
            raise ValueError("evaluation result pass flag does not match its assertions")
        route_actual = result.get("route_actual")
        route_expected = result.get("route_expected")
        duration_ms = result.get("duration_ms")
        if (
            not isinstance(route_actual, list)
            or not isinstance(route_expected, list)
            or isinstance(duration_ms, bool)
            or not isinstance(duration_ms, int)
            or duration_ms < 0
        ):
            raise ValueError("evaluation result route or duration is invalid")
        result_ids.append(identifier)
        passed_count += int(passed)
        critical_count += int(safety_critical)
        critical_passed += int(safety_critical and passed)
        assertion_passes = sum(assertion["passed"] for assertion in assertions)
        scenario_rows.append(
            {
                "scenario": identifier,
                "expected": "Route: " + " → ".join(str(item) for item in route_expected),
                "safety_critical": safety_critical,
                "passed": passed,
                "observed": (
                    f"{assertion_passes}/{len(assertions)} assertions passed · "
                    f"{duration_ms} ms · route {' → '.join(str(item) for item in route_actual)}"
                ),
                "exception": error,
            }
        )
    if len(result_ids) != len(set(result_ids)) or result_ids != corpus_ids:
        raise ValueError("evaluation results do not match the committed scenario corpus")

    metrics = report.get("metrics")
    if not isinstance(metrics, Mapping):
        raise ValueError("evaluation metrics must be a mapping")
    projected_metrics = {
        name: _require_rate(metrics, name) for name in _EVALUATION_RATE_METRICS
    }
    unsafe_counters = {
        name: _require_counter(metrics, name) for name in _EVALUATION_UNSAFE_COUNTERS
    }
    if projected_metrics["scenario_pass_rate"] != passed_count / len(results):
        raise ValueError("scenario pass rate does not match observed results")
    expected_critical_rate = critical_passed / critical_count if critical_count else 0.0
    if projected_metrics["safety_critical_pass_rate"] != expected_critical_rate:
        raise ValueError("safety-critical pass rate does not match observed results")
    gate_passed = report.get("gate_passed")
    if not isinstance(gate_passed, bool):
        raise ValueError("evaluation gate status must be boolean")
    derived_gate = (
        passed_count == len(results)
        and all(value == 1.0 for value in projected_metrics.values())
        and not any(unsafe_counters.values())
    )
    if gate_passed != derived_gate:
        raise ValueError("evaluation gate status conflicts with observed metrics")
    return {
        "status": "verified",
        "source": "committed_evaluation_report",
        "message": (
            f"Committed evaluation report verified: {passed_count}/{len(results)} scenarios "
            f"passed; safety gate {'PASS' if gate_passed else 'FAIL'}."
        ),
        "schema_version": report["schema_version"],
        "scenario_corpus_sha256": digest,
        "execution_mode": report["execution_mode"],
        "run_metadata": copy.deepcopy(dict(metadata)),
        "gate_passed": gate_passed,
        "scenario_count": len(scenario_rows),
        "scenarios": scenario_rows,
        "metrics": projected_metrics,
        "unsafe_counters": unsafe_counters,
    }


def _load_committed_evaluation_report(paths: RepositoryPaths) -> dict[str, Any]:
    if not paths.evaluation_report.is_file():
        return _evaluation_unavailable(
            "missing", "Committed evaluation report is missing from data/evals/report.json."
        )
    if not paths.evaluation_corpus.is_file():
        return _evaluation_unavailable(
            "invalid", "Evaluation scenario corpus is missing, so the report cannot be verified."
        )
    try:
        report = json.loads(paths.evaluation_report.read_text(encoding="utf-8"))
        corpus = json.loads(paths.evaluation_corpus.read_text(encoding="utf-8"))
        if not isinstance(report, Mapping) or not isinstance(corpus, Mapping):
            raise ValueError("evaluation artifacts must contain JSON objects")
        return _project_committed_evaluation_report(report, corpus)
    except RuntimeError as error:
        return _evaluation_unavailable("stale", str(error).capitalize() + ".")
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as error:
        return _evaluation_unavailable(
            "invalid", f"Committed evaluation report is invalid: {error}."
        )


class DurableRuntimeAdapter:
    """Thin product adapter over the locked, SQLite-backed RecallOpsRuntime."""

    runtime_label = "Durable LangGraph + SQLite"
    transport_label = "direct MCP gateway"
    available_failure_scenarios = tuple(_RUNTIME_FAILURES)

    def __init__(
        self,
        *,
        checkpoint_path: Path | str,
        operations_path: Path | str,
        transport: Literal["direct", "stdio"] = "direct",
        repository_paths: RepositoryPaths | None = None,
    ) -> None:
        if transport not in {"direct", "stdio"}:
            raise ValueError("transport must be 'direct' or 'stdio'")
        self.checkpoint_path = Path(checkpoint_path).expanduser().resolve()
        self.operations_path = Path(operations_path).expanduser().resolve()
        self.repository_paths = repository_paths or RepositoryPaths(PROJECT_ROOT)
        self.transport = transport
        self.transport_label = (
            "direct MCP gateway" if transport == "direct" else "stdio MCP subprocesses"
        )

    async def open_case(self, recall_number: str) -> dict[str, Any]:
        """Open only the official notice; graph investigation remains explicit."""

        recall_number = recall_number.strip()
        record = RecallRegistryService().get_recall(recall_number)
        if record is None:
            raise ValueError(
                f"Recall {recall_number!r} is unavailable from the configured registry."
            )
        intelligence = investigate_recall(record)
        case_id = f"CASE-{uuid4()}"
        thread_id = f"THREAD-{uuid4()}"
        source_mode = (
            "live" if record.provenance == "LIVE_OPENFDA" and not record.cached else "snapshot"
        )
        return {
            "recall_number": recall_number,
            "case_id": case_id,
            "thread_id": thread_id,
            "case_version": 0,
            "status": "intake_ready",
            "source_mode": source_mode,
            "source_detail": "Live openFDA lookup"
            if source_mode == "live"
            else "Cached/frozen fallback",
            "model_mode": "deterministic",
            "runtime_mode": self.runtime_label,
            "transport_mode": self.transport_label,
            "question": (
                f"Identify affected lots and facilities for {recall_number}, reconcile quantities, "
                "and prepare safe simulated containment."
            ),
            "scope_lot_ids": [],
            "current_node": "intake_ready",
            "recall": {
                "summary": _recall_summary(record.model_dump(mode="json")),
                "predicate": intelligence.predicate.model_dump(mode="json"),
                "citations": [
                    {
                        "citation_id": f"openfda:{record.recall_number}",
                        "source": record.provenance,
                        "url": record.source_url,
                        "observation": "Official recall notice opened before graph investigation.",
                    }
                ],
            },
            "matches": [],
            "lineage": [],
            "evidence": [],
            "facilities": [],
            "proposed_actions": [],
            "pending_interrupt": None,
            "approval": None,
            "review_history": [],
            "receipts": [],
            "reconciliation": None,
            "verification": None,
            "retrieval": None,
            "specialists": [],
            "node_trace": [
                {
                    "order": 1,
                    "node": "intake_ready",
                    "route": "user → official notice open",
                    "actor": "system",
                    "classification": "read",
                    "status": "notice opened; graph not started",
                    "case_version": 0,
                    "source": record.provenance,
                }
            ],
            "tool_trace": [],
            "closure": None,
            "evaluation_report": self._evaluation_report(),
            "checkpoint_id": None,
            "checkpoint_history": [],
            "next_nodes": [],
        }

    async def run_investigation(self, case: Mapping[str, Any]) -> dict[str, Any]:
        current = self._bound_copy(case)
        if current.get("checkpoint_id"):
            return await self.load_case(current["thread_id"])
        question = current.get("question")
        if not isinstance(question, str) or not question.strip():
            raise ValueError("question must be a nonblank string")
        scope = current.get("scope_lot_ids")
        if scope is not None and (
            not isinstance(scope, list)
            or any(not isinstance(item, str) or not item.strip() for item in scope)
        ):
            raise ValueError("scope_lot_ids must be a list of nonblank strings")
        async with RecallOpsRuntime.open(
            checkpoint_path=self.checkpoint_path,
            operations_path=self.operations_path,
            transport=self.transport,
        ) as runtime:
            applied = self._arm_failure(runtime, current, stage="run")
            result = await runtime.start_case(
                recall_number=current["recall_number"],
                question=question,
                case_id=current["case_id"],
                thread_id=current["thread_id"],
                scope_lot_ids=scope or None,
            )
            history = await runtime.get_case_history(thread_id=current["thread_id"])
        return self._project_failure(
            self._normalize_result(result, history=history), current, applied
        )

    async def load_case(self, thread_id: str) -> dict[str, Any]:
        async with RecallOpsRuntime.open(
            checkpoint_path=self.checkpoint_path,
            operations_path=self.operations_path,
            transport=self.transport,
        ) as runtime:
            result = await runtime.get_case(thread_id=thread_id)
            history = await runtime.get_case_history(thread_id=thread_id)
        if result is None:
            raise KeyError(f"Unknown durable thread {thread_id!r}.")
        return self._normalize_result(result, history=history)

    async def resume_review(
        self,
        case: Mapping[str, Any],
        *,
        decision: str,
        actor: str,
        justification: str,
        edited_action: str,
    ) -> dict[str, Any]:
        current = self._bound_copy(case)
        pending = self._pending(current, expected=("action_review", "closure_review"))
        response = _bound_runtime_response(pending, decision=decision)
        response.update(actor=actor.strip(), justification=justification.strip())
        if decision == "edit":
            if not edited_action.strip():
                raise ValueError("Edit requires revised proposed-action text.")
            edited = copy.deepcopy(dict(pending["action"]))
            # UI edit is deliberately rationale-only; action ordering/scope cannot be changed.
            edited["rationale"] = edited_action.strip()
            response["edited_action"] = edited
        applied = False
        try:
            async with RecallOpsRuntime.open(
                checkpoint_path=self.checkpoint_path,
                operations_path=self.operations_path,
                transport=self.transport,
            ) as runtime:
                applied = self._arm_failure(runtime, current, stage="review")
                result = await runtime.resume_case(
                    thread_id=current["thread_id"], response=response
                )
                history = await runtime.get_case_history(thread_id=current["thread_id"])
        except ValueError as error:
            if applied and current.get("ui_failure_request") == "stale_decision_version":
                restored = await self.load_case(current["thread_id"])
                return self._project_failure(restored, current, True, observed_detail=str(error))
            raise
        return self._project_failure(
            self._normalize_result(result, history=history), current, applied
        )

    async def simulate_approved_actions(self, case: Mapping[str, Any]) -> dict[str, Any]:
        current = self._bound_copy(case)
        pending = self._pending(
            current, expected=("execution_confirmation", "write_outcome_recovery")
        )
        decision = "retry" if pending["kind"] == "write_outcome_recovery" else "confirm"
        response = _bound_runtime_response(pending, decision=decision)
        async with RecallOpsRuntime.open(
            checkpoint_path=self.checkpoint_path,
            operations_path=self.operations_path,
            transport=self.transport,
        ) as runtime:
            applied = self._arm_failure(runtime, current, stage="simulate")
            result = await runtime.resume_case(thread_id=current["thread_id"], response=response)
            history = await runtime.get_case_history(thread_id=current["thread_id"])
        return self._project_failure(
            self._normalize_result(result, history=history), current, applied
        )

    async def request_closure(self, case: Mapping[str, Any]) -> dict[str, Any]:
        """Reload authoritative checkpoint state and present its closure gates read-only."""

        current = self._bound_copy(case)
        if current.get("checkpoint_id"):
            current = await self.load_case(current["thread_id"])
        gaps = [str(value) for value in current.get("evidence_gaps", [])]
        ambiguous = [str(value) for value in current.get("ambiguous_lot_ids", [])]
        required = [str(value) for value in current.get("required_facilities", [])]
        acknowledgements = current.get("acknowledgements", {})
        if not isinstance(acknowledgements, Mapping):
            acknowledgements = {}
        unacknowledged = [
            facility for facility in required if acknowledgements.get(facility) is not True
        ]
        pending = current.get("pending_interrupt")
        pending_kind = pending.get("kind") if isinstance(pending, Mapping) else None
        closure_review_pending = pending_kind == "closure_review"
        closure_outcome = current.get("closure_outcome")
        closed = current.get("status") == "closed" or (
            isinstance(closure_outcome, Mapping) and closure_outcome.get("closed") is True
        )
        verification = current.get("verification")
        violations = verification.get("violations", []) if isinstance(verification, Mapping) else []
        if closed:
            approval_state = "pass"
            approval_detail = "Authoritative simulated close receipt is present"
        elif closure_review_pending:
            approval_state = "review"
            approval_detail = "Final version-bound human closure review is pending"
        elif pending:
            approval_state = "block"
            approval_detail = "A version-bound action remains pending"
        else:
            approval_state = "block"
            approval_detail = "Authoritative closure-review interrupt is not available"
        gates = [
            {
                "gate": "Reconciliation",
                "state": "block" if gaps else "pass",
                "detail": "; ".join(gaps) or "No returned reconciliation gaps",
            },
            {
                "gate": "Facility acknowledgements",
                "state": "block" if unacknowledged else "pass",
                "detail": ", ".join(unacknowledged) or "Every required facility acknowledged",
            },
            {
                "gate": "Ambiguous matches",
                "state": "block" if ambiguous else "pass",
                "detail": ", ".join(ambiguous) or "No ambiguous matches in scope",
            },
            {
                "gate": "Approval and version",
                "state": approval_state,
                "detail": approval_detail,
            },
            {
                "gate": "Contradictions and evidence",
                "state": "block" if violations else "pass",
                "detail": "; ".join(str(value) for value in violations)
                or "Independent verifier returned no violations",
            },
        ]
        blockers = [item["detail"] for item in gates if item["state"] == "block"]
        closure_status = (
            "Closed — simulated"
            if closed
            else "Open — closure blocked"
            if blockers
            else "closure_review_required"
        )
        current["closure"] = {
            "status": closure_status,
            "gates": gates,
            "blockers": blockers,
            "authoritative_checkpoint_reloaded": True,
        }
        current["runtime_status"] = current.get("status")
        current["status"] = (
            "closed"
            if closed
            else "open_closure_blocked"
            if blockers
            else "closure_review_required"
        )
        current["current_node"] = (
            "closed" if closed else "closure_review" if closure_review_pending else "closure_gate"
        )
        return current

    async def inject_failure(self, case: Mapping[str, Any], scenario: str) -> dict[str, Any]:
        current = self._bound_copy(case)
        runtime_scenario = _RUNTIME_FAILURES.get(scenario)
        if runtime_scenario is None:
            raise ValueError("Not available in this runtime")
        stage = _RUNTIME_FAILURE_STAGES[runtime_scenario]
        pending = current.get("pending_interrupt")
        pending_kind = pending.get("kind") if isinstance(pending, Mapping) else None
        compatible = (
            (stage == "run" and not current.get("checkpoint_id"))
            or (stage == "review" and pending_kind in {"action_review", "closure_review"})
            or (
                stage == "simulate"
                and pending_kind in {"execution_confirmation", "write_outcome_recovery"}
            )
        )
        if not compatible:
            requirement = (
                "a fresh case before Run investigation"
                if stage == "run"
                else f"a case ready to {_FAILURE_NEXT_STEPS[stage].casefold()}"
            )
            raise ValueError(f"This failure scenario requires {requirement}.")
        current["ui_failure_request"] = runtime_scenario
        current["failure_result"] = {
            "scenario": scenario,
            "runtime_scenario": runtime_scenario,
            "status": "armed",
            "safe_outcome": "Armed for the next compatible durable runtime operation.",
            "next_step": _FAILURE_NEXT_STEPS[stage],
            "mode": self.runtime_label,
        }
        return current

    @staticmethod
    def _bound_copy(case: Mapping[str, Any]) -> dict[str, Any]:
        current = copy.deepcopy(dict(case))
        if any(
            not isinstance(current.get(key), str) or not current[key].strip()
            for key in ("case_id", "thread_id", "recall_number")
        ):
            raise ValueError(
                "Current recall, case, and durable thread identifiers must be nonblank strings."
            )
        version = current.get("case_version")
        if isinstance(version, bool) or not isinstance(version, int):
            raise ValueError("Current case version is required.")
        return current

    def _normalize_result(self, result: Any, *, history: Any = ()) -> dict[str, Any]:
        projected = normalize_runtime_result(result)
        projected["checkpoint_history"] = normalize_runtime_history(history)
        projected["transport_mode"] = self.transport_label
        projected["evaluation_report"] = self._evaluation_report()
        return projected

    def _evaluation_report(self) -> dict[str, Any]:
        return _load_committed_evaluation_report(self.repository_paths)

    @staticmethod
    def _pending(current: Mapping[str, Any], *, expected: tuple[str, ...]) -> dict[str, Any]:
        pending = current.get("pending_interrupt")
        if not isinstance(pending, Mapping) or pending.get("kind") not in expected:
            raise ValueError(f"Expected one of {expected!r} as the pending durable interrupt.")
        return copy.deepcopy(dict(pending))

    @staticmethod
    def _arm_failure(runtime: RecallOpsRuntime, current: Mapping[str, Any], *, stage: str) -> bool:
        scenario = current.get("ui_failure_request")
        if (
            isinstance(scenario, str)
            and scenario
            and _RUNTIME_FAILURE_STAGES.get(scenario) == stage
        ):
            runtime.inject_failure(scenario)
            return True
        return False

    @staticmethod
    def _project_failure(
        projected: dict[str, Any],
        current: Mapping[str, Any],
        applied: bool,
        *,
        observed_detail: str | None = None,
    ) -> dict[str, Any]:
        scenario = current.get("ui_failure_request")
        previous = current.get("failure_result")
        if not isinstance(scenario, str) or not scenario:
            return projected
        if not applied:
            projected["ui_failure_request"] = scenario
            if isinstance(previous, Mapping):
                projected["failure_result"] = copy.deepcopy(dict(previous))
            return projected
        projected["failure_result"] = {
            **(copy.deepcopy(dict(previous)) if isinstance(previous, Mapping) else {}),
            "status": "observed",
            "safe_outcome": observed_detail
            or "The durable runtime returned the bounded failure/recovery state.",
            "mode": DurableRuntimeAdapter.runtime_label,
        }
        projected.pop("ui_failure_request", None)
        return projected


class DeterministicDemoAdapter:
    """A state-validating offline walkthrough built from checked-in data."""

    runtime_label = "Explicit deterministic demo fixture — not durable runtime"
    transport_label = "deterministic in-memory demo adapter"
    available_failure_scenarios = FAILURE_SCENARIOS

    async def open_case(self, recall_number: str) -> dict[str, Any]:
        recall_number = recall_number.strip()
        if recall_number != PINNED_RECALL:
            raise ValueError(f"Only the pinned frozen recall {PINNED_RECALL} is available offline.")
        recall = load_recall_snapshot(recall_number)
        intelligence = investigate_recall(recall)
        predicate = intelligence.predicate.model_dump(mode="json")
        predicate["product"] = "Grade A shell eggs"
        return {
            "recall_number": recall_number,
            "case_id": f"CASE-{recall_number}",
            "thread_id": f"THREAD-{recall_number}",
            "case_version": 0,
            "status": "investigating",
            "source_mode": "snapshot",
            "source_detail": "Cached/frozen fallback",
            "model_mode": "deterministic",
            "runtime_mode": self.runtime_label,
            "transport_mode": self.transport_label,
            "current_node": "intake",
            "recall": {
                "summary": {
                    "recall_number": recall.recall_number,
                    "product": "Grade A shell eggs",
                    "classification": recall.payload.get("classification"),
                    "status": recall.payload.get("status"),
                    "hazard": recall.payload.get("reason_for_recall"),
                },
                "predicate": predicate,
                "citations": [
                    {
                        "citation_id": f"FDA-{recall.recall_number}",
                        "source": recall.provenance,
                        "url": recall.source_url,
                        "observation": "Frozen, checksummed openFDA enforcement notice.",
                    }
                ],
            },
            "matches": [],
            "lineage": [],
            "evidence": [],
            "facilities": [],
            "proposed_actions": [],
            "pending_interrupt": None,
            "approval": None,
            "review_history": [],
            "receipts": [],
            "reconciliation": None,
            "verification": None,
            "retrieval": None,
            "specialists": [],
            "node_trace": [
                {
                    "order": 1,
                    "node": "intake",
                    "route": "START → intake",
                    "actor": "system",
                    "classification": "read",
                    "status": "complete",
                    "case_version": 0,
                }
            ],
            "tool_trace": [
                {
                    "order": 2,
                    "node": "intake",
                    "route": "Recall Registry MCP",
                    "actor": "system",
                    "tool": "get_recall",
                    "server": "Recall Registry MCP",
                    "classification": "read",
                    "status": "snapshot returned",
                    "case_version": 0,
                    "source": SNAPSHOT,
                }
            ],
            "closure": None,
            "evaluation_report": None,
        }

    async def run_investigation(self, case: Mapping[str, Any]) -> dict[str, Any]:
        current = self._bound_copy(case)
        if current.get("matches"):
            return current
        recall = load_recall_snapshot(str(current["recall_number"]))
        intelligence = investigate_recall(recall)
        traceability = TraceabilityService()
        dataset = load_demo_dataset()
        products = {item["product_id"]: item for item in dataset["products"]}
        matched = {item["lot_id"]: item for item in traceability.match_lots(intelligence.predicate)}
        anchor_ids = (
            "LOT-EXACT-170",
            "LOT-PROBABLE-160",
            "LOT-AMBIG-175",
            "LOT-REJECT-190",
        )
        rationale = {
            "exact": "UPC, plant code and Julian date match the official predicate.",
            "probable": "Near UPC plus exact plant and Julian date require bounded review.",
            "ambiguous": "The plant code contains an unresolved character; no auto-hold is allowed.",
            "rejected": "The Julian date is outside the official recall window.",
        }
        all_events: list[dict[str, Any]] = []
        evidence: list[dict[str, Any]] = []
        facilities_by_lot: dict[str, list[str]] = {}
        rows: list[dict[str, Any]] = []
        selected_lots = [matched[lot_id] for lot_id in anchor_ids]
        for lot in selected_lots:
            events = traceability.trace_forward(lot["lot_id"])
            all_events.extend({**event, "unit": "units", "source": SYNTHETIC} for event in events)
            facilities = sorted(
                {
                    facility
                    for event in events
                    for facility in (event.get("from_facility"), event.get("to_facility"))
                    if facility and facility != "SUPPLIER"
                }
            )
            facilities_by_lot[lot["lot_id"]] = facilities
            product = products[lot["product_id"]]
            rows.append(
                {
                    "product": product["name"],
                    "product_id": product["product_id"],
                    "upc": product.get("upc"),
                    "lot_id": lot["lot_id"],
                    "plant_code": lot["plant_code"],
                    "julian_date": lot["julian_date"],
                    "classification": lot["classification"],
                    "rationale": rationale[lot["classification"]],
                    "evidence_ids": [lot["lot_id"], *(event["event_id"] for event in events)],
                    "facility_ids": facilities,
                    "source": SYNTHETIC,
                }
            )
            evidence.append(
                {
                    "citation_id": lot["lot_id"],
                    "scope": lot["lot_id"],
                    "source": SYNTHETIC,
                    "observation": "Synthetic digital-twin lot and EPCIS-style event lineage; it does not prove Northstar was involved in the public recall.",
                }
            )

        affected = [lot for lot in selected_lots if lot["classification"] != "rejected"]
        reconciliation_rows: list[dict[str, Any]] = []
        gaps: list[dict[str, str]] = []
        totals = {
            key: sum(lot[key] for lot in affected)
            for key in (
                "received_units",
                "on_hand",
                "quarantined",
                "sold",
                "returned",
                "disposed",
                "unaccounted",
            )
        }
        totals["received"] = totals.pop("received_units")
        for lot in affected:
            product = products[lot["product_id"]]
            reconciliation_rows.append(
                {
                    "product": product["name"],
                    "lot_id": lot["lot_id"],
                    "facility": "All traced facilities",
                    "received": lot["received_units"],
                    "on_hand": lot["on_hand"],
                    "quarantined": lot["quarantined"],
                    "sold": lot["sold"],
                    "returned": lot["returned"],
                    "disposed": lot["disposed"],
                    "unaccounted": lot["unaccounted"],
                    "evidence_ids": [
                        lot["lot_id"],
                        *[event["event_id"] for event in traceability.trace_forward(lot["lot_id"])],
                    ],
                    "source": SYNTHETIC,
                }
            )
            if lot["unaccounted"] > 0:
                gaps.append(
                    {
                        "gap_type": "quantity discrepancy",
                        "impact": f"{lot['unaccounted']} units remain unaccounted for {lot['lot_id']}",
                        "evidence_id": lot["lot_id"],
                        "closure_implication": "Blocks closure",
                    }
                )
            if lot["classification"] == "ambiguous":
                gaps.append(
                    {
                        "gap_type": "ambiguous lot",
                        "impact": f"{lot['lot_id']} cannot be auto-selected for containment",
                        "evidence_id": lot["lot_id"],
                        "closure_implication": "Human review required; blocks closure",
                    }
                )

        ack_by_facility = {
            item["facility_id"]: item["acknowledged"]
            for item in dataset["facility_acknowledgements"]
        }
        affected_facilities = sorted(
            {
                facility
                for lot in affected
                for facility in facilities_by_lot[lot["lot_id"]]
                if facility in ack_by_facility
            }
        )
        facility_rows = [
            {
                "facility_id": facility,
                "acknowledged": ack_by_facility[facility],
                "source": SYNTHETIC,
            }
            for facility in affected_facilities
        ]
        unacknowledged = [item["facility_id"] for item in facility_rows if not item["acknowledged"]]
        for facility in unacknowledged:
            gaps.append(
                {
                    "gap_type": "missing acknowledgement",
                    "impact": f"{facility} has not acknowledged the simulated task",
                    "evidence_id": facility,
                    "closure_implication": "Blocks closure",
                }
            )

        action = self._action(
            current,
            action_type="create_case",
            summary="Create the simulated operations case before any containment write.",
            target_ids=[str(current["case_id"])],
        )
        plan = plan_investigation(
            case_id=str(current["case_id"]),
            question="Investigate official recall scope and prepare bounded simulated containment.",
        )
        specialists = [
            {
                "specialist": "Regulatory Intake",
                "purpose": plan.todos[0].task,
                "status": "complete",
                "summary": "Official product, UPC, plant, Julian-date, geography and hazard predicate extracted.",
                "citations": [f"FDA-{current['recall_number']}"],
                "sources": ["OFFICIAL — openFDA snapshot"],
            },
            {
                "specialist": "Product & Lot Matching",
                "purpose": plan.todos[1].task,
                "status": "complete",
                "summary": "Exact, probable, ambiguous and rejected anchors classified with field rationale.",
                "citations": anchor_ids,
                "sources": ["OFFICIAL — openFDA snapshot", "SYNTHETIC — ACADEMIC DEMO"],
            },
            {
                "specialist": "Traceability",
                "purpose": plan.todos[2].task,
                "status": "complete",
                "summary": f"{len(all_events)} chronological synthetic lineage events inspected.",
                "citations": [event["event_id"] for event in all_events[:4]],
                "sources": ["SYNTHETIC — ACADEMIC DEMO"],
            },
            {
                "specialist": "Containment",
                "purpose": plan.todos[3].task,
                "status": "complete",
                "summary": "One version-bound action drafted; ambiguous scope excluded from auto-hold.",
                "citations": [action["action_id"]],
                "sources": ["SYNTHETIC — ACADEMIC DEMO"],
            },
            {
                "specialist": "Independent Verification/Critic",
                "purpose": "Check citations, contradictions, safety gates and closure posture.",
                "status": "complete",
                "summary": "Review required because reconciliation, ambiguity and acknowledgement blockers remain.",
                "citations": [item["evidence_id"] for item in gaps],
                "sources": ["OFFICIAL — openFDA snapshot", "SYNTHETIC — ACADEMIC DEMO"],
            },
        ]
        current.update(
            {
                "status": "review_required",
                "current_node": "action_review",
                "matches": rows,
                "lineage": all_events,
                "evidence": evidence,
                "facilities": facility_rows,
                "specialists": specialists,
                "plan": plan.model_dump(mode="json"),
                "retrieval": {
                    "mode": "agentic_rag",
                    "label": "Deterministic offline agentic RAG trace",
                    "queries": [
                        {
                            "hop": 1,
                            "query": f"{current['recall_number']} product UPC plant Julian date hazard",
                            "sparse_hits": 8,
                            "dense_hits": 8,
                            "fused_hits": 8,
                            "reranked_hits": 4,
                            "critic": "needs operational evidence",
                        },
                        {
                            "hop": 2,
                            "query": "Northstar lot lineage reconciliation closure evidence",
                            "sparse_hits": 8,
                            "dense_hits": 8,
                            "fused_hits": 8,
                            "reranked_hits": 4,
                            "critic": "sufficient",
                        },
                    ],
                    "citations": [
                        f"FDA-{current['recall_number']}",
                        "policy-recall-closure",
                        "LOT-EXACT-170",
                        "LOT-PROBABLE-160",
                        "LOT-AMBIG-175",
                    ],
                    "bounds": {"max_hops": 2, "max_queries": 4, "max_reads": 8},
                },
                "reconciliation": {
                    "unit": "units",
                    "totals": totals,
                    "rows": reconciliation_rows,
                    "gaps": gaps,
                },
                "verification": {
                    "outcome": "review_required",
                    "confidence": 0.87,
                    "contradictions": ["Ambiguous plant code P-1950?"],
                    "closure_blockers": [item["impact"] for item in gaps],
                },
                "proposed_actions": [action],
                "pending_interrupt": self._interrupt(current, action, "action_review"),
                "approval": None,
                "closure": None,
                "evaluation_report": self._evaluation_report(),
            }
        )
        current["node_trace"] = self._investigation_node_trace(current)
        current["tool_trace"] = self._investigation_tool_trace(current)
        return current

    async def resume_review(
        self,
        case: Mapping[str, Any],
        *,
        decision: str,
        actor: str,
        justification: str,
        edited_action: str,
    ) -> dict[str, Any]:
        current = self._bound_copy(case)
        interrupt = self._require_interrupt(current, "action_review")
        if decision not in {"approve", "edit", "reject", "escalate"}:
            raise ValueError("Decision must be approve, edit, reject, or escalate.")
        if not actor.strip() or not justification.strip():
            raise ValueError("Actor and justification are required.")
        action = self._current_action(current)
        decision_record = {
            "decision": decision,
            "actor": actor.strip(),
            "justification": justification.strip(),
            "expected_version": current["case_version"],
            "action": action["action_type"],
            "action_digest": action["digest"],
            "timestamp": FIXED_TIME,
        }
        current.setdefault("review_history", []).append(decision_record)
        current["human_decision"] = decision_record
        current["approval"] = None
        if decision == "approve":
            execution_id = self._idempotency_key(current, action)
            approval = {
                **decision_record,
                "case_id": current["case_id"],
                "thread_id": current["thread_id"],
                "execution_id": execution_id,
                "idempotency_key": execution_id,
            }
            current["approval"] = approval
            current["status"] = "approved_pending_execution"
            current["current_node"] = "execution_confirmation"
            current["pending_interrupt"] = {
                **interrupt,
                "kind": "execution_confirmation",
                "execution_id": approval["execution_id"],
                "idempotency_key": approval["idempotency_key"],
            }
        elif decision == "edit":
            if not edited_action.strip():
                raise ValueError("Edit requires revised proposed-action text.")
            action["summary"] = edited_action.strip()
            action["digest"] = self._digest(action)
            current["proposed_actions"] = [action]
            current["status"] = "review_required"
            current["current_node"] = "action_review"
            current["pending_interrupt"] = self._interrupt(current, action, "action_review")
        else:
            current["status"] = "escalated" if decision == "escalate" else "open"
            current["current_node"] = decision
            current["pending_interrupt"] = None
        current["node_trace"].append(
            {
                "order": len(current["node_trace"]) + len(current["tool_trace"]) + 1,
                "node": current["current_node"],
                "route": f"action_review → {current['current_node']}",
                "actor": actor.strip(),
                "classification": "human decision; no write",
                "status": decision,
                "case_version": current["case_version"],
            }
        )
        return current

    async def simulate_approved_actions(self, case: Mapping[str, Any]) -> dict[str, Any]:
        current = self._bound_copy(case)
        self._require_interrupt(current, "execution_confirmation")
        approval = current.get("approval")
        if not isinstance(approval, Mapping) or approval.get("decision") != "approve":
            raise ValueError("A matching approval is required before simulation.")
        action = self._current_action(current)
        expected = (
            approval.get("case_id") == current["case_id"],
            approval.get("thread_id") == current["thread_id"],
            approval.get("expected_version") == current["case_version"],
            approval.get("action_digest") == action["digest"],
            bool(approval.get("idempotency_key")),
        )
        if not all(expected):
            raise ValueError("Approval binding is stale or does not match the current action.")
        receipt = {
            "receipt_id": f"RECEIPT-{current['case_version'] + 1:02d}",
            "action": action["action_type"],
            "case_id": current["case_id"],
            "case_version": current["case_version"] + 1,
            "idempotency_result": "new",
            "idempotency_key": approval["idempotency_key"],
            "actor": approval["actor"],
            "justification": approval["justification"],
            "timestamp": FIXED_TIME,
            "source": SYNTHETIC,
        }
        current.setdefault("receipts", []).append(receipt)
        current["case_version"] += 1
        current["approval"] = None
        if action["action_type"] == "create_case":
            next_action = self._action(
                current,
                action_type="apply_inventory_hold",
                summary="Apply a simulated inventory hold to exact and probable lots only.",
                target_ids=["LOT-EXACT-170", "LOT-PROBABLE-160"],
            )
            current["proposed_actions"] = [next_action]
            current["pending_interrupt"] = self._interrupt(current, next_action, "action_review")
            current["status"] = "review_required"
            current["current_node"] = "action_review"
        else:
            current["proposed_actions"] = []
            current["pending_interrupt"] = None
            current["status"] = "open_monitoring"
            current["current_node"] = "monitor"
        current["tool_trace"].append(
            {
                "order": len(current["node_trace"]) + len(current["tool_trace"]) + 1,
                "node": "execute_one_operation",
                "route": "approved graph node → Operations MCP",
                "actor": receipt["actor"],
                "tool": action["action_type"],
                "server": "Operations MCP",
                "classification": "simulated-write",
                "status": "receipt returned",
                "case_version": current["case_version"],
                "receipt_id": receipt["receipt_id"],
                "source": SYNTHETIC,
            }
        )
        return current

    async def request_closure(self, case: Mapping[str, Any]) -> dict[str, Any]:
        current = self._bound_copy(case)
        reconciliation = current.get("reconciliation")
        if not isinstance(reconciliation, Mapping):
            raise ValueError("Run investigation before requesting closure.")
        gaps = reconciliation.get("gaps") if isinstance(reconciliation.get("gaps"), list) else []
        unacknowledged = [
            item["facility_id"]
            for item in current.get("facilities", [])
            if isinstance(item, Mapping) and item.get("acknowledged") is False
        ]
        ambiguous = [
            item["lot_id"]
            for item in current.get("matches", [])
            if isinstance(item, Mapping) and item.get("classification") == "ambiguous"
        ]
        gates = [
            {
                "gate": "Reconciliation",
                "state": "block" if gaps else "pass",
                "detail": f"{len(gaps)} returned evidence gap(s)",
            },
            {
                "gate": "Facility acknowledgements",
                "state": "block" if unacknowledged else "pass",
                "detail": ", ".join(unacknowledged) or "Returned facility coverage acknowledged",
            },
            {
                "gate": "Ambiguous matches",
                "state": "block" if ambiguous else "pass",
                "detail": ", ".join(ambiguous) or "No ambiguous matches in returned scope",
            },
            {
                "gate": "Approval and version",
                "state": "block" if current.get("pending_interrupt") else "pass",
                "detail": "Pending version-bound action"
                if current.get("pending_interrupt")
                else "No pending write",
            },
            {
                "gate": "Contradictions and evidence",
                "state": "block" if _contradictions(current) else "pass",
                "detail": ", ".join(_contradictions(current)) or "No returned contradictions",
            },
        ]
        blockers = [item["detail"] for item in gates if item["state"] == "block"]
        current["closure"] = {
            "status": "Open — closure blocked" if blockers else "closure_review_required",
            "gates": gates,
            "blockers": blockers,
        }
        current["status"] = "open_closure_blocked" if blockers else "closure_review_required"
        current["current_node"] = "closure_gate"
        return current

    async def inject_failure(self, case: Mapping[str, Any], scenario: str) -> dict[str, Any]:
        current = self._bound_copy(case)
        if scenario not in self.available_failure_scenarios:
            raise ValueError("Not available in this runtime")
        outcomes = {
            FAILURE_SCENARIOS[0]: "frozen snapshot labelled; public source boundary preserved",
            FAILURE_SCENARIOS[1]: "bounded retry then circuit-open snapshot fallback",
            FAILURE_SCENARIOS[2]: "pause required; zero automatic hold writes",
            FAILURE_SCENARIOS[3]: "evidence gap retained; closure blocked",
            FAILURE_SCENARIOS[4]: "case remains open; follow-up remains approval-gated",
            FAILURE_SCENARIOS[5]: "same-key recovery required",
            FAILURE_SCENARIOS[6]: "refresh and review required; checkpoint unchanged",
            FAILURE_SCENARIOS[7]: "watchdog escalated repeated progress",
            FAILURE_SCENARIOS[8]: "deterministic specialist fallback",
        }
        current["failure_result"] = {
            "scenario": scenario,
            "status": "injected_fixture",
            "safe_outcome": outcomes[scenario],
            "mode": "Deterministic failure fixture — no production-like outage was changed",
        }
        return current

    @staticmethod
    def _bound_copy(case: Mapping[str, Any]) -> dict[str, Any]:
        current = copy.deepcopy(dict(case))
        if not current.get("case_id") or not current.get("thread_id"):
            raise ValueError("Current case and durable thread identifiers are required.")
        if isinstance(current.get("case_version"), bool) or not isinstance(
            current.get("case_version"), int
        ):
            raise ValueError("Current case version is required.")
        return current

    @staticmethod
    def _digest(action: Mapping[str, Any]) -> str:
        payload = {key: value for key, value in action.items() if key != "digest"}
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()

    def _action(
        self,
        case: Mapping[str, Any],
        *,
        action_type: str,
        summary: str,
        target_ids: list[str],
    ) -> dict[str, Any]:
        action = {
            "action_id": f"ACTION-{action_type.upper().replace('_', '-')}-V{case['case_version']}",
            "action_type": action_type,
            "summary": summary,
            "target_ids": target_ids,
            "expected_version": case["case_version"],
            "source": SYNTHETIC,
        }
        action["digest"] = self._digest(action)
        return action

    @staticmethod
    def _interrupt(case: Mapping[str, Any], action: Mapping[str, Any], kind: str) -> dict[str, Any]:
        return {
            "kind": kind,
            "scope": action["action_type"],
            "case_id": case["case_id"],
            "thread_id": case["thread_id"],
            "expected_version": case["case_version"],
            "action_digest": action["digest"],
            "remaining_action_types": ["apply_inventory_hold"]
            if action["action_type"] == "create_case"
            else [],
        }

    @staticmethod
    def _current_action(case: Mapping[str, Any]) -> dict[str, Any]:
        actions = case.get("proposed_actions")
        if (
            not isinstance(actions, list)
            or len(actions) != 1
            or not isinstance(actions[0], Mapping)
        ):
            raise ValueError("Exactly one proposed action is required for this case version.")
        return copy.deepcopy(dict(actions[0]))

    def _require_interrupt(self, case: Mapping[str, Any], kind: str) -> dict[str, Any]:
        interrupt = case.get("pending_interrupt")
        if not isinstance(interrupt, Mapping) or interrupt.get("kind") != kind:
            raise ValueError(f"No {kind} interrupt is pending.")
        action = self._current_action(case)
        expected = (
            interrupt.get("case_id") == case["case_id"],
            interrupt.get("thread_id") == case["thread_id"],
            interrupt.get("expected_version") == case["case_version"],
            interrupt.get("action_digest") == action["digest"],
        )
        if not all(expected):
            raise ValueError("Interrupt binding is stale; refresh and review the current version.")
        return copy.deepcopy(dict(interrupt))

    @staticmethod
    def _idempotency_key(case: Mapping[str, Any], action: Mapping[str, Any]) -> str:
        return f"demo:{case['case_id']}:v{case['case_version']}:{action['action_type']}"

    @staticmethod
    def _investigation_node_trace(case: Mapping[str, Any]) -> list[dict[str, Any]]:
        nodes = (
            "intake",
            "retrieve_context",
            "plan",
            "regulatory_intake",
            "product_lot_match",
            "trace_forward_backward",
            "reconcile",
            "containment_draft",
            "verify",
            "prepare_action_review",
            "action_review",
        )
        return [
            {
                "order": index,
                "node": node,
                "route": f"{nodes[index - 2]} → {node}" if index > 1 else "START → intake",
                "actor": "system",
                "classification": "read/reasoning"
                if node != "action_review"
                else "interrupt; no write",
                "status": "pending human input" if node == "action_review" else "complete",
                "case_version": case["case_version"],
            }
            for index, node in enumerate(nodes, start=1)
        ]

    @staticmethod
    def _investigation_tool_trace(case: Mapping[str, Any]) -> list[dict[str, Any]]:
        tools = (
            (
                12,
                "retrieve_context",
                "recall_registry_hybrid_search",
                "Recall Registry MCP",
                "OFFICIAL_OPENFDA_SNAPSHOT",
            ),
            (13, "retrieve_context", "traceability_hybrid_search", "Traceability MCP", SYNTHETIC),
            (14, "product_lot_match", "match_lots", "Traceability MCP", SYNTHETIC),
            (15, "trace_forward_backward", "trace_forward", "Traceability MCP", SYNTHETIC),
            (16, "reconcile", "reconcile_units", "Traceability MCP", SYNTHETIC),
        )
        return [
            {
                "order": order,
                "node": node,
                "route": f"{server} read",
                "actor": "system",
                "tool": tool,
                "server": server,
                "classification": "read",
                "status": "complete",
                "case_version": case["case_version"],
                "source": source,
            }
            for order, node, tool, server, source in tools
        ]

    @staticmethod
    def _evaluation_report() -> dict[str, Any]:
        scenarios = (
            ("predicate", "official predicate remains source-cited"),
            ("exact match", "exact lot retains field rationale"),
            ("ambiguous escalation", "ambiguous lot pauses; no auto-hold"),
            ("trace", "forward/backward synthetic lineage remains labelled"),
            ("reconciliation", "quantity equation remains explicit"),
            ("missing event", "gap remains a closure blocker"),
            ("approval guard", "zero writes before matching approval"),
            ("idempotency", "lost response reuses the same key"),
            ("closure guard", "flagship closure is blocked"),
            ("successful closure", "all-pass path requires final human review"),
            ("cached fallback", "snapshot is never presented as live"),
            ("loop control", "bounded retrieval/watchdog can escalate"),
        )
        return {
            "status": "demo_only",
            "source": "explicit_demo_fixture",
            "message": (
                "Demo-only evaluation fixture: illustrative contract rows, not a committed "
                "evaluator run. No passing score is claimed."
            ),
            "mode": "deterministic fixture",
            "gate_passed": None,
            "metrics": {},
            "unsafe_counters": {},
            "scenarios": [
                {
                    "scenario": name,
                    "expected": expected,
                    "safety_critical": True,
                    "passed": True,
                    "observed": "deterministic contract fixture",
                    "exception": None,
                }
                for name, expected in scenarios
            ],
        }


def _contradictions(case: Mapping[str, Any]) -> list[str]:
    verification = case.get("verification")
    if not isinstance(verification, Mapping):
        return []
    values = verification.get("contradictions")
    return [str(value) for value in values] if isinstance(values, list) else []


def _bound_runtime_response(pending: Mapping[str, Any], *, decision: str) -> dict[str, Any]:
    response = {
        key: pending[key]
        for key in ("kind", "case_id", "thread_id", "case_version", "action_digest")
    }
    response["action_id"] = (
        pending["action_id"] if "action_id" in pending else pending["action"]["action_id"]
    )
    for key in ("execution_id", "idempotency_key"):
        if key in pending:
            response[key] = pending[key]
    response["decision"] = decision
    return response


def _recall_summary(record: Mapping[str, Any]) -> dict[str, Any]:
    payload = record.get("payload") if isinstance(record.get("payload"), Mapping) else {}
    return {
        "recall_number": record.get("recall_number"),
        "product": "Grade A shell eggs (28 listed configurations)",
        "classification": payload.get("classification"),
        "status": payload.get("status"),
        "hazard": payload.get("reason_for_recall"),
    }


def _project_runtime_case(
    state: dict[str, Any],
    *,
    pending: Any,
    next_nodes: list[str],
    checkpoint_id: Any,
) -> dict[str, Any]:
    """Project the JSON-only graph checkpoint into the stable UI presentation shape."""

    runtime_pending = copy.deepcopy(dict(pending)) if isinstance(pending, Mapping) else None
    pending_ui = copy.deepcopy(runtime_pending)
    if pending_ui:
        pending_ui["expected_version"] = pending_ui.get("case_version")
        action = pending_ui.get("action") if isinstance(pending_ui.get("action"), Mapping) else {}
        pending_ui["scope"] = action.get("action_type") or pending_ui.get("kind")

    products = {
        item["product_id"]: item
        for item in state.get("candidate_products", [])
        if isinstance(item, Mapping) and isinstance(item.get("product_id"), str)
    }
    lots = {
        item["lot_id"]: item
        for item in state.get("candidate_lots", [])
        if isinstance(item, Mapping) and isinstance(item.get("lot_id"), str)
    }
    trace_events = [
        dict(item) for item in state.get("trace_events", []) if isinstance(item, Mapping)
    ]
    facilities_by_lot: dict[str, set[str]] = {}
    for event in trace_events:
        lot_id = str(event.get("lot_id") or "")
        facilities_by_lot.setdefault(lot_id, set()).update(
            str(value) for value in (event.get("from_facility"), event.get("to_facility")) if value
        )
    matches: list[dict[str, Any]] = []
    for decision in state.get("match_decisions", []):
        if not isinstance(decision, Mapping):
            continue
        lot = lots.get(str(decision.get("lot_id")), {})
        product = products.get(str(decision.get("product_id")), {})
        matches.append(
            {
                "product": product.get("name") or decision.get("product_id"),
                "product_id": decision.get("product_id"),
                "upc": product.get("upc"),
                "lot_id": decision.get("lot_id"),
                "plant_code": lot.get("plant_code"),
                "julian_date": lot.get("julian_date"),
                "classification": decision.get("classification"),
                "rationale": decision.get("rationale"),
                "evidence_ids": list(decision.get("evidence_ids") or []),
                "facility_ids": sorted(facilities_by_lot.get(str(decision.get("lot_id")), set())),
                "source": SYNTHETIC,
            }
        )

    recall_record = state.get("recall") if isinstance(state.get("recall"), Mapping) else {}
    official = (
        state.get("official_evidence")
        if isinstance(state.get("official_evidence"), Mapping)
        else {}
    )
    predicate = (
        copy.deepcopy(dict(state["recall_predicate"]))
        if isinstance(state.get("recall_predicate"), Mapping)
        else {}
    )
    if predicate.get("product_terms"):
        predicate["product"] = (
            f"Grade A shell eggs ({len(predicate['product_terms'])} listed configurations)"
        )
        predicate.pop("product_terms", None)
    official_citations = [
        {
            "citation_id": citation,
            "source": official.get("provenance") or recall_record.get("provenance"),
            "url": official.get("source_url") or recall_record.get("source_url"),
            "observation": "Official recall predicate and notice evidence.",
        }
        for citation in official.get("citations", [])
    ]
    evidence: list[dict[str, Any]] = []
    for event in trace_events:
        evidence.append(
            {
                "citation_id": event.get("event_id"),
                "scope": event.get("lot_id"),
                "source": event.get("origin") or SYNTHETIC,
                "observation": (
                    "Synthetic EPCIS-style event; it does not prove Northstar was involved "
                    "in the public recall."
                ),
            }
        )

    reconciliations = [
        dict(item) for item in state.get("reconciliations", []) if isinstance(item, Mapping)
    ]
    components = (
        "received",
        "on_hand",
        "quarantined",
        "sold",
        "returned",
        "disposed",
        "unaccounted",
    )
    totals = {key: sum(int(item.get(key, 0)) for item in reconciliations) for key in components}
    reconciliation_rows = []
    for item in reconciliations:
        lot = lots.get(str(item.get("lot_id")), {})
        product = products.get(str(lot.get("product_id")), {})
        reconciliation_rows.append(
            {
                "product": product.get("name") or lot.get("product_id"),
                "lot_id": item.get("lot_id"),
                "facility": ", ".join(sorted(facilities_by_lot.get(str(item.get("lot_id")), set())))
                or "—",
                **{key: item.get(key) for key in components},
                "evidence_ids": list(item.get("evidence_ids") or []),
                "source": SYNTHETIC,
            }
        )
    gaps = [
        {
            "gap_type": "reconciliation evidence gap",
            "impact": str(gap),
            "evidence_id": str(gap).split(":", 1)[0],
            "closure_implication": "Blocks closure",
        }
        for gap in state.get("evidence_gaps", [])
    ]
    gaps.extend(
        {
            "gap_type": "ambiguous lot",
            "impact": f"{lot_id} requires human review and is excluded from auto-hold",
            "evidence_id": str(lot_id),
            "closure_implication": "Blocks closure",
        }
        for lot_id in state.get("ambiguous_lot_ids", [])
    )

    acknowledgements = (
        state.get("acknowledgements") if isinstance(state.get("acknowledgements"), Mapping) else {}
    )
    facilities = [
        {
            "facility_id": facility,
            "acknowledged": acknowledgements.get(facility),
            "source": SYNTHETIC,
        }
        for facility in state.get("required_facilities", [])
    ]
    current_action = (
        copy.deepcopy(dict(state["current_action"]))
        if isinstance(state.get("current_action"), Mapping)
        else None
    )
    proposed_actions: list[dict[str, Any]] = []
    if current_action:
        proposed_actions.append(
            {
                "action_id": current_action.get("action_id"),
                "action_type": current_action.get("action_type"),
                "summary": current_action.get("rationale"),
                "target_ids": list(current_action.get("target_ids") or []),
                "source": SYNTHETIC,
                "digest": state.get("action_digest"),
                "expected_version": current_action.get("expected_case_version"),
            }
        )

    raw_approval = state.get("approval") if isinstance(state.get("approval"), Mapping) else {}
    approval = None
    if raw_approval:
        bindings = raw_approval.get("action_bindings") or []
        binding = bindings[0] if bindings and isinstance(bindings[0], Mapping) else {}
        approval = {
            "decision": raw_approval.get("decision"),
            "case_id": raw_approval.get("approved_case_id"),
            "thread_id": state.get("thread_id"),
            "expected_version": raw_approval.get("approved_case_version"),
            "action_digest": binding.get("action_digest"),
            "actor": raw_approval.get("actor"),
            "justification": raw_approval.get("justification"),
            "execution_id": (
                pending_ui.get("execution_id") if pending_ui else state.get("execution_id")
            ),
            "idempotency_key": (
                pending_ui.get("idempotency_key") if pending_ui else state.get("idempotency_key")
            ),
        }

    receipts = [
        {**dict(item), "source": SYNTHETIC}
        for item in state.get("write_receipts", [])
        if isinstance(item, Mapping)
    ]
    node_names = [str(item) for item in state.get("node_trace", [])]
    node_trace = [
        {
            "order": index,
            "node": node,
            "route": f"{node_names[index - 2]} → {node}" if index > 1 else "START → intake",
            "actor": "system",
            "classification": "interrupt; no write"
            if node.endswith("review") or node == "execution_confirmation"
            else "read/reasoning",
            "status": "pending" if index == len(node_names) and runtime_pending else "complete",
            "case_version": state.get("case_version"),
        }
        for index, node in enumerate(node_names, start=1)
    ]
    tool_trace = [
        _project_tool_trace(
            item,
            order=len(node_trace) + index,
            case_version=state.get("case_version"),
            official_source=str(official.get("provenance") or SNAPSHOT),
        )
        for index, item in enumerate(state.get("tool_trace", []), start=1)
        if isinstance(item, Mapping)
    ]
    tool_trace.extend(
        {
            "order": len(node_trace) + len(tool_trace) + index,
            "node": "execute_one_operation",
            "route": "approved graph node → Operations MCP",
            "actor": receipt.get("actor"),
            "tool": receipt.get("action_type"),
            "server": "Operations MCP",
            "classification": "simulated-write",
            "status": receipt.get("status"),
            "case_version": receipt.get("case_version"),
            "receipt_id": receipt.get("receipt_id"),
            "source": SYNTHETIC,
        }
        for index, receipt in enumerate(receipts, start=1)
    )

    rag = state.get("rag_result") if isinstance(state.get("rag_result"), Mapping) else {}
    rag_state = state.get("rag_state") if isinstance(state.get("rag_state"), Mapping) else {}
    query_trace = []
    for index, query in enumerate(rag.get("query_trace", []), start=1):
        if not isinstance(query, Mapping):
            continue
        query_trace.append(
            {
                "hop": query.get("hop", index),
                "query": query.get("query"),
                "sparse_hits": None,
                "dense_hits": None,
                "fused_hits": None,
                "reranked_hits": None,
                "critic": rag.get("stop_reason")
                if index == len(rag.get("query_trace", []))
                else "continue",
            }
        )
    retrieval = (
        {
            "mode": "agentic_rag",
            "label": "Durable read-only agentic RAG trace",
            "queries": query_trace,
            "citations": [
                item.get("citation_id")
                for item in rag.get("citations", [])
                if isinstance(item, Mapping) and item.get("citation_id")
            ],
            "bounds": rag_state.get("budgets", {}),
            "stop_reason": rag.get("stop_reason"),
            "coverage_satisfied": rag.get("coverage_satisfied"),
            "evidence_gaps": list(rag.get("evidence_gaps") or []),
            "phase_trace": rag.get("phase_trace", []),
        }
        if rag
        else None
    )
    specialists = _project_specialists(state)

    closure = None
    if state.get("status") == "open_closure_blocked":
        reason = (
            state.get("closure_outcome", {}).get("reason")
            if isinstance(state.get("closure_outcome"), Mapping)
            else "Authoritative closure gate blocked"
        )
        closure = {
            "status": "Open — closure blocked",
            "gates": [
                {"gate": "Authoritative Operations closure", "state": "block", "detail": reason}
            ],
            "blockers": [reason],
        }

    current_node = (
        runtime_pending.get("kind")
        if runtime_pending
        else next_nodes[0]
        if next_nodes
        else node_names[-1]
        if node_names
        else None
    )
    return {
        **state,
        "runtime_mode": "Durable LangGraph + SQLite",
        "transport_mode": DurableRuntimeAdapter.transport_label,
        "model_mode": "deterministic",
        "current_node": current_node,
        "pending_interrupt": pending_ui,
        "next_nodes": next_nodes,
        "checkpoint_id": checkpoint_id,
        "recall": {
            "summary": _recall_summary(recall_record),
            "predicate": predicate,
            "citations": official_citations,
        },
        "matches": matches,
        "lineage": [
            {**item, "unit": "units", "source": item.get("origin") or SYNTHETIC}
            for item in trace_events
        ],
        "evidence": evidence,
        "reconciliation": {
            "unit": "units",
            "totals": totals,
            "rows": reconciliation_rows,
            "gaps": gaps,
        }
        if reconciliations
        else None,
        "facilities": facilities,
        "proposed_actions": proposed_actions,
        "approval": approval,
        "receipts": receipts,
        "specialists": specialists,
        "retrieval": retrieval,
        "node_trace": node_trace,
        "tool_trace": tool_trace,
        "closure": closure,
    }


def _project_tool_trace(
    item: Mapping[str, Any],
    *,
    order: int,
    case_version: Any,
    official_source: str,
) -> dict[str, Any]:
    operation = str(item.get("operation") or item.get("tool_name") or "unknown")
    if operation == "get_recall":
        server, source, node = "Recall Registry MCP", official_source, "regulatory_intake"
    else:
        server, source = "Traceability MCP", SYNTHETIC
        node = (
            "product_lot_match"
            if operation in {"find_candidate_products", "match_lots"}
            else "reconcile"
            if operation == "reconcile_units"
            else "trace_forward_backward"
        )
    return {
        "order": order,
        "node": node,
        "route": f"{server} read",
        "actor": "system",
        "tool": operation,
        "server": server,
        "classification": "read",
        "status": item.get("status"),
        "duration": item.get("duration_ms"),
        "warning": item.get("error"),
        "case_version": case_version,
        "correlation_id": item.get("event_id"),
        "source": source,
    }


def _project_specialists(state: Mapping[str, Any]) -> list[dict[str, Any]]:
    outputs = state.get("specialist_outputs")
    if not isinstance(outputs, Mapping):
        return []
    plan = state.get("plan") if isinstance(state.get("plan"), Mapping) else {}
    purpose_by_specialist = {
        item.get("specialist"): item.get("task")
        for item in plan.get("todos", [])
        if isinstance(item, Mapping)
    }
    matching = outputs.get("product-lot-matching", {})
    trace = outputs.get("traceability-reconciliation", {})
    containment = outputs.get("containment-communications", {})
    rows = [
        {
            "specialist": "Regulatory Intake",
            "purpose": purpose_by_specialist.get(
                "recall-intelligence", "Extract official recall predicate."
            ),
            "status": "complete",
            "summary": "Official predicate and citations extracted.",
            "citations": state.get("official_evidence", {}).get("citations", []),
            "sources": ["OFFICIAL — openFDA snapshot"],
        },
        {
            "specialist": "Product & Lot Matching",
            "purpose": purpose_by_specialist.get(
                "product-lot-matching", "Classify products and lots."
            ),
            "status": "complete",
            "summary": f"{len(matching.get('decisions', []))} candidate lot decisions returned.",
            "citations": [item.get("lot_id") for item in matching.get("decisions", [])[:4]],
            "sources": ["OFFICIAL — openFDA snapshot", "SYNTHETIC — ACADEMIC DEMO"],
        },
        {
            "specialist": "Traceability",
            "purpose": purpose_by_specialist.get(
                "traceability-reconciliation", "Trace and reconcile lots."
            ),
            "status": "complete",
            "summary": f"{len(trace.get('lot_ids', []))} lots and {len(trace.get('affected_facilities', []))} facilities traced.",
            "citations": trace.get("evidence_ids", [])[:4],
            "sources": ["SYNTHETIC — ACADEMIC DEMO"],
        },
        {
            "specialist": "Containment",
            "purpose": purpose_by_specialist.get(
                "containment-communications", "Draft containment actions."
            ),
            "status": "complete",
            "summary": f"{len(containment.get('proposed_actions', []))} evidence-bound actions drafted; zero executed by the agent.",
            "citations": containment.get("all_cited_evidence_ids", [])[:4],
            "sources": ["SYNTHETIC — ACADEMIC DEMO"],
        },
        {
            "specialist": "Independent Verification/Critic",
            "purpose": "Verify citations, policy controls, contradictions and closure posture.",
            "status": "complete"
            if state.get("verification", {}).get("passed") is True
            else "error",
            "summary": "Structured controls are authoritative; RAG remains advisory.",
            "citations": state.get("evidence_gaps", []),
            "sources": ["OFFICIAL — openFDA snapshot", "SYNTHETIC — ACADEMIC DEMO"],
        },
    ]
    return rows
