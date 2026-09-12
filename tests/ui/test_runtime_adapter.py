from __future__ import annotations

import asyncio
import json
import shutil
import sqlite3
from pathlib import Path

import pytest

from recallops import paths as repository_paths_module
from recallops.agents.runtime import RecallOpsRuntime
from recallops.llm import LLMSettings
from recallops.llm.live_reasoning import LiveReasoningSummary
from recallops.paths import PROJECT_ROOT, RepositoryPaths
from recallops.ui.adapter import (
    DeterministicDemoAdapter,
    DurableRuntimeAdapter,
    normalize_runtime_result,
)
from recallops.ui.presenters import (
    APPROVAL_JUSTIFICATION,
    build_match_rows,
    build_reasoning_presentation,
    build_retrieval_rows,
    can_simulate,
    reduce_case_snapshot,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["completed", "failed", "semantic_failure"])
async def test_live_reasoning_runs_once_and_survives_reload_without_authority_changes(
    tmp_path: Path, outcome: str, monkeypatch, live_case
) -> None:
    """Catch duplicate provider runs, lost summaries, hidden fallback, and checkpoint pollution."""
    checkpoint = tmp_path / "checkpoints.sqlite3"
    operations = tmp_path / "operations.sqlite3"
    calls = []

    import recallops.llm.live_reasoning as live

    def model(settings):
        calls.append(settings.model)
        with sqlite3.connect(checkpoint) as connection:
            assert connection.execute("SELECT COUNT(*) FROM checkpoints").fetchone()[0] > 1
        if outcome == "failed":
            raise TimeoutError("sk-secret-do-not-persist provider payload")
        raw = live_case.raw
        for action in raw[live_case.roles[3]]["proposed_actions"]:
            action["case_id"] = opened["case_id"]
        if outcome == "semantic_failure":
            raw[live_case.roles[1]]["decisions"][0]["classification"] = "probable"
        return live_case.script(raw)

    monkeypatch.setattr(live, "build_chat_model", model)

    adapter = DurableRuntimeAdapter(
        checkpoint_path=checkpoint,
        operations_path=operations,
        llm_settings=LLMSettings(mode="openai", model="test-model"),
    )
    opened = await adapter.open_case("H-1230-2026")
    assert opened["scope_lot_ids"] == live_case.scope
    assert opened["reasoning_mode"] == "openai"
    assert opened["llm_status"] == "ready"
    assert opened["llm_run"] is None
    reviewed = await adapter.run_investigation(opened)
    assert reviewed["scope_lot_ids"] == live_case.scope
    assert calls == ["test-model"]
    assert reviewed["llm_status"] == (
        "verification_failed" if outcome == "semantic_failure" else outcome
    )
    assert reviewed["llm_run"]["fallback_used"] is (outcome == "failed")
    if outcome == "failed":
        assert any("deterministic fallback" in item.lower() for item in reviewed["warnings"])
    assert "sk-secret" not in json.dumps(reviewed)
    if outcome == "semantic_failure":
        assert reviewed["pending_interrupt"] is None
        assert reviewed["verification"]["passed"] is False
    else:
        assert reviewed["pending_interrupt"]["kind"] == "action_review"
    assert _receipt_count(operations) == 0

    # A stale intake snapshot cannot invoke the model or restart the durable graph.
    replayed = await adapter.run_investigation(opened)
    assert replayed["checkpoint_id"] == reviewed["checkpoint_id"]
    restarted = DurableRuntimeAdapter(checkpoint_path=checkpoint, operations_path=operations)
    restored = await restarted.load_case(reviewed["thread_id"])
    assert restored["llm_run"] == reviewed["llm_run"]
    assert restored["reasoning_mode"] == "openai"
    if outcome == "semantic_failure":
        assert restored["llm_status"] == "verification_failed"
        assert restored["verification"]["passed"] is False
        return
    approved = await restarted.resume_review(
        restored,
        decision="approve",
        actor="Food-safety manager",
        justification=APPROVAL_JUSTIFICATION,
        edited_action="",
    )
    assert approved["llm_run"] == reviewed["llm_run"]
    assert approved["pending_interrupt"]["kind"] == "execution_confirmation"
    assert len(calls) == 1
    async with RecallOpsRuntime.open(
        checkpoint_path=checkpoint, operations_path=operations
    ) as runtime:
        raw = await runtime.get_case(thread_id=reviewed["thread_id"])
    assert raw.case["requested_reasoning_mode"] == "openai"
    assert raw.case["live_run"]["summary"] == reviewed["llm_run"]
    assert (raw.case["live_claims"] is not None) is (outcome == "completed")
    for key in ("case_version", "verification"):
        assert approved[key] == json.loads(raw.model_dump_json())["case"][key]
    assert approved["receipts"] == list(raw.case["write_receipts"]) == []
    for key, value in json.loads(raw.model_dump_json())["pending_interrupt"].items():
        assert approved["pending_interrupt"][key] == value


async def test_source_binding_stop_is_not_displayed_as_a_deterministic_fallback(
    tmp_path, monkeypatch
):
    import recallops.agents.workflow as workflow

    def unavailable(**kwargs):
        raise ValueError("PRIVATE_SOURCE_BINDING_CANARY")

    monkeypatch.setattr(workflow, "build_live_request", unavailable)
    adapter = DurableRuntimeAdapter(
        checkpoint_path=tmp_path / "checkpoints.sqlite3",
        operations_path=tmp_path / "operations.sqlite3",
        llm_settings=LLMSettings(mode="openai", model="test-model"),
    )
    result = await adapter.run_investigation(await adapter.open_case("H-1230-2026"))
    assert result["pending_interrupt"] is None
    assert result["investigation_source"] == "openai"
    assert result["llm_status"] == "verification_failed"
    assert not build_reasoning_presentation(reduce_case_snapshot(result))["warning"]
    assert "PRIVATE_SOURCE_BINDING_CANARY" not in json.dumps(result)


def _receipt_count(path: Path) -> int:
    connection = sqlite3.connect(path)
    try:
        return int(connection.execute("SELECT COUNT(*) FROM receipts").fetchone()[0])
    finally:
        connection.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["malformed", "credential", "database"])
async def test_corrupt_telemetry_cannot_block_authoritative_load_review_or_committed_receipt(
    tmp_path, monkeypatch, fault
):
    """Catch advisory read failures hiding checkpoint state or an already committed operation."""
    adapter = DurableRuntimeAdapter(
        checkpoint_path=tmp_path / "checkpoints.sqlite3",
        operations_path=tmp_path / "operations.sqlite3",
    )
    reviewed = await adapter.run_investigation(await adapter.open_case("H-1230-2026"))
    if fault == "database":

        def unavailable(thread_id):
            raise sqlite3.DatabaseError("sk-canary-private-credential database failure")

        monkeypatch.setattr(adapter.reasoning_store, "get", unavailable)
    else:
        adapter.reasoning_store.claim(reviewed["thread_id"])
        payload = '{"sk-canary-private-credential":'
        if fault == "credential":
            payload = LiveReasoningSummary(
                model="gpt-4.1",
                status="completed",
                duration_ms=10,
                plan=[],
            ).model_dump(mode="json")
            payload["plan"] = ["sk-canary-private-credential"]
            payload = json.dumps(payload)
        with sqlite3.connect(adapter.reasoning_store.path) as connection:
            connection.execute("UPDATE reasoning_summaries SET summary_json = ?", (payload,))

    restored = await adapter.load_case(reviewed["thread_id"])
    assert restored["pending_interrupt"] == reviewed["pending_interrupt"]
    approved = await adapter.resume_review(
        restored,
        decision="approve",
        actor="Food-safety manager",
        justification=APPROVAL_JUSTIFICATION,
        edited_action="",
    )
    assert approved["pending_interrupt"]["kind"] == "execution_confirmation"
    created = await adapter.simulate_approved_actions(approved)
    assert created["case_version"] == 1
    assert created["receipts"][0]["action_type"] == "create_case"
    assert _receipt_count(adapter.operations_path) == 1
    for result in (restored, approved, created):
        assert result["llm_status"] == "not_run"
        assert result["llm_run"] is None
        assert result["reasoning_mode"] == "deterministic"
        assert "canary-private-credential" not in json.dumps(result)


@pytest.mark.asyncio
async def test_unfinished_live_claim_still_blocks_initial_investigation(tmp_path):
    adapter = DurableRuntimeAdapter(
        checkpoint_path=tmp_path / "checkpoints.sqlite3",
        operations_path=tmp_path / "operations.sqlite3",
        llm_settings=LLMSettings(mode="openai", model="gpt-4.1"),
    )
    opened = await adapter.open_case("H-1230-2026")
    adapter.reasoning_store.claim(opened["thread_id"])
    with pytest.raises(RuntimeError, match="in progress|interrupted"):
        await adapter.run_investigation(opened)
    async with RecallOpsRuntime.open(
        checkpoint_path=adapter.checkpoint_path, operations_path=adapter.operations_path
    ) as runtime:
        assert await runtime.get_case(thread_id=opened["thread_id"]) is None


_RATE_METRICS = (
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
_UNSAFE_COUNTERS = (
    "unauthorized_write_count",
    "duplicate_logical_write_count",
    "false_close_count",
    "receipt_integrity_violation_count",
)


def _write_evaluation_report(
    root: Path,
    *,
    digest: str | None = None,
    raw_report: str | None = None,
) -> None:
    eval_dir = root / "data" / "evals"
    eval_dir.mkdir(parents=True)
    for name in ("report.json", "scenarios.json"):
        shutil.copyfile(PROJECT_ROOT / "data" / "evals" / name, eval_dir / name)
    if raw_report is not None:
        (eval_dir / "report.json").write_text(raw_report, encoding="utf-8")
    elif digest is not None:
        report = json.loads((eval_dir / "report.json").read_bytes())
        report["scenario_corpus_sha256"] = digest
        (eval_dir / "report.json").write_text(json.dumps(report), encoding="utf-8")


def test_repository_paths_exposes_evaluation_artifacts_from_configured_root(
    tmp_path: Path,
) -> None:
    assert hasattr(repository_paths_module, "RepositoryPaths")
    paths = repository_paths_module.RepositoryPaths(tmp_path)

    assert paths.root == tmp_path.resolve()
    assert paths.evaluation_report == tmp_path.resolve() / "data" / "evals" / "report.json"
    assert paths.evaluation_corpus == tmp_path.resolve() / "data" / "evals" / "scenarios.json"


@pytest.mark.asyncio
async def test_durable_adapter_projects_verified_committed_evaluation_without_raw_traces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository_root = tmp_path / "repository"
    _write_evaluation_report(repository_root)
    elsewhere = tmp_path / "unrelated-working-directory"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    adapter = DurableRuntimeAdapter(
        checkpoint_path=tmp_path / "checkpoints.sqlite3",
        operations_path=tmp_path / "operations.sqlite3",
        repository_paths=RepositoryPaths(repository_root),
    )

    report = (await adapter.open_case("H-1230-2026"))["evaluation_report"]

    assert report["status"] == "verified"
    assert report["source"] == "committed_evaluation_report"
    assert report["scenario_count"] == 21
    assert [item["scenario"] for item in report["scenarios"]] == [f"R{n:02d}" for n in range(1, 22)]
    assert report["metrics"]["scenario_pass_rate"] == 1.0
    assert report["unsafe_counters"] == {name: 0 for name in _UNSAFE_COUNTERS}
    assert all("tool_trace" not in item for item in report["scenarios"])
    assert all("state_excerpt" not in item for item in report["scenarios"])
    assert all("assertions" not in item for item in report["scenarios"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arrange", "expected_status", "message_fragment"),
    [
        ("missing", "missing", "missing"),
        ("invalid", "invalid", "invalid"),
        ("digest_mismatch", "stale", "digest"),
    ],
)
async def test_durable_adapter_labels_untrusted_evaluation_report_without_crashing(
    tmp_path: Path,
    arrange: str,
    expected_status: str,
    message_fragment: str,
) -> None:
    repository_root = tmp_path / "repository"
    if arrange == "invalid":
        _write_evaluation_report(repository_root, raw_report="{not valid json")
    elif arrange == "digest_mismatch":
        _write_evaluation_report(repository_root, digest="0" * 64)
    adapter = DurableRuntimeAdapter(
        checkpoint_path=tmp_path / "checkpoints.sqlite3",
        operations_path=tmp_path / "operations.sqlite3",
        repository_paths=RepositoryPaths(repository_root),
    )

    report = (await adapter.open_case("H-1230-2026"))["evaluation_report"]

    assert report["status"] == expected_status
    assert message_fragment in report["message"].casefold()
    assert report["scenarios"] == []
    assert report["gate_passed"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["schema", "gate_status"])
async def test_durable_adapter_rejects_schema_or_gate_status_inconsistency(
    tmp_path: Path, mutation: str
) -> None:
    repository_root = tmp_path / "repository"
    _write_evaluation_report(repository_root)
    report_path = RepositoryPaths(repository_root).evaluation_report
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if mutation == "schema":
        report["schema_version"] = "9.9"
    else:
        report["gate_passed"] = False
    report_path.write_text(json.dumps(report), encoding="utf-8")
    adapter = DurableRuntimeAdapter(
        checkpoint_path=tmp_path / "checkpoints.sqlite3",
        operations_path=tmp_path / "operations.sqlite3",
        repository_paths=RepositoryPaths(repository_root),
    )

    projected = (await adapter.open_case("H-1230-2026"))["evaluation_report"]

    assert projected["status"] == "invalid"
    assert "no passing score is claimed" in projected["message"].casefold()


@pytest.mark.asyncio
async def test_durable_adapter_projects_actual_committed_21_scenario_report_if_available(
    tmp_path: Path,
) -> None:
    candidates = (PROJECT_ROOT, PROJECT_ROOT.parent / "evals")
    actual_root = next(
        (
            root
            for root in candidates
            if RepositoryPaths(root).evaluation_report.exists()
            and RepositoryPaths(root).evaluation_corpus.exists()
        ),
        None,
    )
    if actual_root is None:
        pytest.skip("final evaluator branch has not been integrated into this worktree")
    adapter = DurableRuntimeAdapter(
        checkpoint_path=tmp_path / "checkpoints.sqlite3",
        operations_path=tmp_path / "operations.sqlite3",
        repository_paths=RepositoryPaths(actual_root),
    )

    report = (await adapter.open_case("H-1230-2026"))["evaluation_report"]

    assert report["status"] == "verified"
    assert report["scenario_count"] == 21
    assert {item["scenario"] for item in report["scenarios"]} == {
        f"R{number:02d}" for number in range(1, 22)
    }


def test_deterministic_fixture_evaluation_is_explicitly_demo_only() -> None:
    report = DeterministicDemoAdapter._evaluation_report()

    assert report["status"] == "demo_only"
    assert "demo-only" in report["message"].casefold()
    assert report["source"] == "explicit_demo_fixture"


@pytest.mark.asyncio
async def test_durable_adapter_projects_compact_checkpoint_history(tmp_path: Path) -> None:
    """Break caught: the product loses durable history when raw graph access is removed."""

    adapter = DurableRuntimeAdapter(
        checkpoint_path=tmp_path / "checkpoints.sqlite3",
        operations_path=tmp_path / "operations.sqlite3",
    )
    reviewed = await adapter.run_investigation(await adapter.open_case("H-1230-2026"))

    assert reviewed["checkpoint_history"]
    assert reviewed["checkpoint_history"][0] == {
        "checkpoint_id": reviewed["checkpoint_id"],
        "status": "review_required",
        "case_version": 0,
        "pending_kind": "action_review",
        "next_nodes": ["action_review"],
    }
    assert json.loads(json.dumps(reviewed["checkpoint_history"])) == reviewed["checkpoint_history"]


@pytest.mark.asyncio
async def test_immutable_runtime_result_normalizes_to_detached_ui_json(tmp_path: Path) -> None:
    """Break caught: a UI projection mutates or leaks the runtime's frozen audit result."""

    checkpoint = tmp_path / "checkpoints.sqlite3"
    operations = tmp_path / "operations.sqlite3"
    async with RecallOpsRuntime.open(
        checkpoint_path=checkpoint,
        operations_path=operations,
    ) as runtime:
        result = await runtime.start_case(
            recall_number="H-1230-2026",
            question="Project immutable runtime results into detached product state.",
            case_id="CASE-IMMUTABLE-UI",
            thread_id="THREAD-IMMUTABLE-UI",
        )
        projected = normalize_runtime_result(result)
        projected["status"] = "caller-only"
        projected["pending_interrupt"]["kind"] = "caller-only"
        fresh = await runtime.get_case(thread_id="THREAD-IMMUTABLE-UI")

    with pytest.raises(TypeError):
        result.case["status"] = "forged"
    assert fresh.case["status"] == "review_required"
    assert fresh.pending_interrupt["kind"] == "action_review"
    assert json.loads(json.dumps(projected))["status"] == "caller-only"


@pytest.mark.asyncio
async def test_copied_store_adapters_surface_one_fenced_review_winner(tmp_path: Path) -> None:
    """Break caught: two product adapters fork one copied durable checkpoint head."""

    original_checkpoint = tmp_path / "original-checkpoints.sqlite3"
    copied_checkpoint = tmp_path / "copied-checkpoints.sqlite3"
    operations = tmp_path / "operations.sqlite3"
    original = DurableRuntimeAdapter(
        checkpoint_path=original_checkpoint,
        operations_path=operations,
    )
    reviewed = await original.run_investigation(await original.open_case("H-1230-2026"))
    shutil.copy2(original_checkpoint, copied_checkpoint)
    copied = DurableRuntimeAdapter(
        checkpoint_path=copied_checkpoint,
        operations_path=operations,
    )

    outcomes = await asyncio.gather(
        original.resume_review(
            reviewed,
            decision="approve",
            actor="Food-safety manager",
            justification=APPROVAL_JUSTIFICATION,
            edited_action="",
        ),
        copied.resume_review(
            reviewed,
            decision="reject",
            actor="Food-safety manager",
            justification="Reject the proposed action while evidence is rechecked.",
            edited_action="",
        ),
        return_exceptions=True,
    )

    assert sum(isinstance(item, dict) for item in outcomes) == 1
    assert sum(isinstance(item, ValueError) for item in outcomes) == 1
    stale = copied if isinstance(outcomes[0], dict) else original
    with pytest.raises(ValueError, match="checkpoint|head|stale|mutation"):
        await stale.resume_review(
            reviewed,
            decision="approve",
            actor="Food-safety manager",
            justification=APPROVAL_JUSTIFICATION,
            edited_action="",
        )


def test_durable_adapter_exposes_only_real_runtime_transports(tmp_path: Path) -> None:
    direct = DurableRuntimeAdapter(
        checkpoint_path=tmp_path / "direct-checkpoints.sqlite3",
        operations_path=tmp_path / "direct-operations.sqlite3",
    )
    assert direct.transport == "direct"
    assert direct.transport_label == "direct MCP gateway"

    stdio = DurableRuntimeAdapter(
        checkpoint_path=tmp_path / "stdio-checkpoints.sqlite3",
        operations_path=tmp_path / "stdio-operations.sqlite3",
        transport="stdio",
    )
    assert stdio.transport == "stdio"
    assert stdio.transport_label == "stdio MCP subprocesses"

    with pytest.raises(ValueError, match="direct.*stdio"):
        DurableRuntimeAdapter(
            checkpoint_path=tmp_path / "bad-checkpoints.sqlite3",
            operations_path=tmp_path / "bad-operations.sqlite3",
            transport="pretend",  # type: ignore[arg-type]
        )


@pytest.mark.asyncio
async def test_durable_adapter_projects_runtime_and_survives_reopen(tmp_path: Path) -> None:
    checkpoint = tmp_path / "checkpoints.sqlite3"
    operations = tmp_path / "operations.sqlite3"
    adapter = DurableRuntimeAdapter(
        checkpoint_path=checkpoint,
        operations_path=operations,
    )
    opened = await adapter.open_case("H-1230-2026")
    assert opened["runtime_mode"] == "Durable LangGraph + SQLite"
    assert opened["matches"] == []

    reviewed = await adapter.run_investigation(opened)
    assert reviewed["pending_interrupt"]["kind"] == "action_review"
    assert reviewed["pending_interrupt"]["scope"] == "create_case"
    assert reviewed["status"] == "review_required"
    assert [row["specialist"] for row in reviewed["specialists"]] == [
        "Regulatory Intake",
        "Product & Lot Matching",
        "Traceability",
        "Containment",
        "Independent Evidence Verification",
    ]
    assert reviewed["specialist_execution_order"] == [
        "recall-intelligence",
        "product-lot-matching",
        "traceability-reconciliation",
        "containment-communications",
    ]
    assert len(build_match_rows(reduce_case_snapshot(reviewed))) == 4
    retrieval = build_retrieval_rows(reduce_case_snapshot(reviewed))
    assert retrieval and retrieval[0].query
    assert "BM25" in retrieval[0].sparse
    assert _receipt_count(operations) == 0

    approved = await adapter.resume_review(
        reviewed,
        decision="approve",
        actor="Food-safety manager",
        justification=APPROVAL_JUSTIFICATION,
        edited_action="",
    )
    assert approved["pending_interrupt"]["kind"] == "execution_confirmation"
    assert can_simulate(reduce_case_snapshot(approved))[0]
    assert _receipt_count(operations) == 0

    restored_adapter = DurableRuntimeAdapter(
        checkpoint_path=checkpoint,
        operations_path=operations,
    )
    restored = await restored_adapter.load_case(approved["thread_id"])
    assert restored["checkpoint_id"] == approved["checkpoint_id"]
    assert restored["pending_interrupt"] == approved["pending_interrupt"]

    created = await restored_adapter.simulate_approved_actions(restored)
    assert created["case_version"] == 1
    assert created["receipts"][0]["action_type"] == "create_case"
    assert created["pending_interrupt"]["scope"] == "apply_inventory_hold"
    assert _receipt_count(operations) == 1


@pytest.mark.asyncio
async def test_durable_failure_selector_arms_only_supported_runtime_failure(tmp_path: Path) -> None:
    adapter = DurableRuntimeAdapter(
        checkpoint_path=tmp_path / "checkpoints.sqlite3",
        operations_path=tmp_path / "operations.sqlite3",
    )
    opened = await adapter.open_case("H-1230-2026")
    armed = await adapter.inject_failure(opened, "openFDA unavailable → labelled frozen snapshot")
    assert armed["failure_result"]["status"] == "armed"
    assert armed["failure_result"]["next_step"] == "Run investigation"
    investigated = await adapter.run_investigation(armed)
    assert any(
        "pinned OFFICIAL_OPENFDA_SNAPSHOT fallback" in warning
        for warning in investigated["warnings"]
    )

    with pytest.raises(ValueError, match="fresh case"):
        await adapter.inject_failure(
            investigated, "read timeout/429 → bounded retry then circuit-open/fallback"
        )


@pytest.mark.asyncio
async def test_durable_adapter_rejects_coercive_case_and_thread_bindings(tmp_path: Path) -> None:
    adapter = DurableRuntimeAdapter(
        checkpoint_path=tmp_path / "checkpoints.sqlite3",
        operations_path=tmp_path / "operations.sqlite3",
    )
    with pytest.raises(ValueError, match="nonblank strings"):
        await adapter.run_investigation(
            {
                "recall_number": "H-1230-2026",
                "question": "Investigate safely.",
                "case_id": 123,
                "thread_id": ["THREAD"],
                "case_version": 0,
            }
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides",
    [
        {"question": 123},
        {"scope_lot_ids": [True]},
        {"scope_lot_ids": "LOT-EXACT-170"},
    ],
)
async def test_durable_adapter_rejects_coercive_investigation_inputs(
    tmp_path: Path, overrides: dict[str, object]
) -> None:
    adapter = DurableRuntimeAdapter(
        checkpoint_path=tmp_path / "checkpoints.sqlite3",
        operations_path=tmp_path / "operations.sqlite3",
    )
    opened = await adapter.open_case("H-1230-2026")
    opened.update(overrides)
    with pytest.raises(ValueError, match="question|scope_lot_ids"):
        await adapter.run_investigation(opened)


@pytest.mark.asyncio
async def test_durable_lost_response_is_observed_then_recovers_with_same_key(
    tmp_path: Path,
) -> None:
    operations = tmp_path / "operations.sqlite3"
    adapter = DurableRuntimeAdapter(
        checkpoint_path=tmp_path / "checkpoints.sqlite3",
        operations_path=operations,
    )
    reviewed = await adapter.run_investigation(await adapter.open_case("H-1230-2026"))
    approved = await adapter.resume_review(
        reviewed,
        decision="approve",
        actor="Food-safety manager",
        justification=APPROVAL_JUSTIFICATION,
        edited_action="",
    )
    original_key = approved["pending_interrupt"]["idempotency_key"]
    armed = await adapter.inject_failure(approved, "lost write response → same-key replay")
    unknown = await adapter.simulate_approved_actions(armed)
    assert unknown["status"] == "write_outcome_unknown"
    assert unknown["pending_interrupt"]["kind"] == "write_outcome_recovery"
    assert unknown["pending_interrupt"]["idempotency_key"] == original_key
    assert unknown["failure_result"]["status"] == "observed"
    assert _receipt_count(operations) == 1

    recovered = await adapter.simulate_approved_actions(unknown)
    assert recovered["case_version"] == 1
    assert recovered["receipts"][0]["idempotency_key"] == original_key
    assert _receipt_count(operations) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["direct", "stdio"])
async def test_restarted_product_adapter_exposes_exact_key_unknown_outcome_recovery(
    tmp_path: Path, transport: str
) -> None:
    """Break caught: restart restores the recovery interrupt but the product disables retry."""

    checkpoint = tmp_path / f"{transport}-checkpoints.sqlite3"
    operations = tmp_path / f"{transport}-operations.sqlite3"
    adapter = DurableRuntimeAdapter(
        checkpoint_path=checkpoint,
        operations_path=operations,
        transport=transport,  # type: ignore[arg-type]
    )
    opened = await adapter.open_case("H-1230-2026")
    opened["scope_lot_ids"] = ["LOT-PROBABLE-160"]
    reviewed = await adapter.run_investigation(opened)
    approved = await adapter.resume_review(
        reviewed,
        decision="approve",
        actor="Food-safety manager",
        justification=APPROVAL_JUSTIFICATION,
        edited_action="",
    )
    original_execution = approved["pending_interrupt"]["execution_id"]
    original_key = approved["pending_interrupt"]["idempotency_key"]
    armed = await adapter.inject_failure(approved, "lost write response → same-key replay")
    unknown = await adapter.simulate_approved_actions(armed)
    assert unknown["status"] == "write_outcome_unknown"
    assert _receipt_count(operations) == 1

    restarted = DurableRuntimeAdapter(
        checkpoint_path=checkpoint,
        operations_path=operations,
        transport=transport,  # type: ignore[arg-type]
    )
    restored = await restarted.load_case(unknown["thread_id"])
    assert restored["pending_interrupt"]["execution_id"] == original_execution
    assert restored["pending_interrupt"]["idempotency_key"] == original_key
    assert can_simulate(reduce_case_snapshot(restored)) == (
        True,
        "Recover the recorded outcome using the exact original idempotency key.",
    )

    recovered = await restarted.simulate_approved_actions(restored)
    assert recovered["case_version"] == 1
    assert len(recovered["receipts"]) == 1
    assert recovered["receipts"][0]["idempotency_key"] == original_key
    assert _receipt_count(operations) == 1


@pytest.mark.asyncio
async def test_durable_repeated_progress_surfaces_fail_closed_state(tmp_path: Path) -> None:
    adapter = DurableRuntimeAdapter(
        checkpoint_path=tmp_path / "checkpoints.sqlite3",
        operations_path=tmp_path / "operations.sqlite3",
    )
    opened = await adapter.open_case("H-1230-2026")
    armed = await adapter.inject_failure(opened, "repeated graph progress → watchdog escalation")
    escalated = await adapter.run_investigation(armed)
    assert escalated["status"] == "escalated"
    assert escalated["pending_interrupt"] is None
    assert escalated["failure_result"]["status"] == "observed"
    assert escalated["watchdog"]["repeat_count"] == 2


@pytest.mark.asyncio
async def test_closure_presentation_preserves_review_and_closed_lifecycle_states(
    tmp_path: Path,
) -> None:
    adapter = DurableRuntimeAdapter(
        checkpoint_path=tmp_path / "checkpoints.sqlite3",
        operations_path=tmp_path / "operations.sqlite3",
    )
    ready = await adapter.open_case("H-1230-2026")
    ready.update(
        status="closure_review_required",
        current_node="closure_review",
        pending_interrupt={
            "kind": "closure_review",
            "case_id": ready["case_id"],
            "thread_id": ready["thread_id"],
            "expected_version": 8,
            "action_digest": "closure-digest",
        },
        case_version=8,
        evidence_gaps=[],
        ambiguous_lot_ids=[],
        required_facilities=["DC-NORTH"],
        acknowledgements={"DC-NORTH": True},
        verification={"violations": []},
    )
    review = await adapter.request_closure(ready)
    assert review["status"] == "closure_review_required"
    assert review["closure"]["status"] == "closure_review_required"
    assert any(gate["state"] == "review" for gate in review["closure"]["gates"])
    assert review["closure"]["blockers"] == []

    closed = {**review, "status": "closed", "pending_interrupt": None}
    completed = await adapter.request_closure(closed)
    assert completed["status"] == "closed"
    assert completed["closure"]["status"] == "Closed — simulated"


@pytest.mark.asyncio
async def test_product_adapter_runs_every_probable_lot_action_through_closure(
    tmp_path: Path,
) -> None:
    operations = tmp_path / "operations.sqlite3"
    adapter = DurableRuntimeAdapter(
        checkpoint_path=tmp_path / "checkpoints.sqlite3",
        operations_path=operations,
    )
    opened = await adapter.open_case("H-1230-2026")
    opened["scope_lot_ids"] = ["LOT-PROBABLE-160"]
    case = await adapter.run_investigation(opened)
    reviewed_actions: list[str] = []
    receipt_counts: list[int] = []
    edited_closure = False

    while case.get("pending_interrupt") is not None:
        pending = case["pending_interrupt"]
        if pending["kind"] in {"action_review", "closure_review"}:
            action_type = pending["action"]["action_type"]
            reviewed_actions.append(action_type)
            if pending["kind"] == "closure_review" and not edited_closure:
                case = await adapter.resume_review(
                    case,
                    decision="edit",
                    actor="Food-safety manager",
                    justification="Clarify closure rationale without changing action or scope.",
                    edited_action="Close only after every disposition and acknowledgement.",
                )
                assert case["status"] == "closure_review_required"
                assert case["pending_interrupt"]["kind"] == "closure_review"
                edited_closure = True
            else:
                case = await adapter.resume_review(
                    case,
                    decision="approve",
                    actor="Food-safety manager",
                    justification=APPROVAL_JUSTIFICATION,
                    edited_action="",
                )
        elif pending["kind"] == "execution_confirmation":
            before = len(case["receipts"])
            case = await adapter.simulate_approved_actions(case)
            assert len(case["receipts"]) == before + 1
            receipt_counts.append(len(case["receipts"]))
        else:  # pragma: no cover - explicit contract assertion is clearer
            raise AssertionError(f"unexpected interrupt: {pending['kind']}")

    assert reviewed_actions == [
        "create_case",
        "apply_inventory_hold",
        "create_facility_tasks",
        "record_acknowledgment",
        "record_acknowledgment",
        "close_case",
        "close_case",
    ]
    assert receipt_counts == list(range(1, 7))
    assert [item["action_type"] for item in case["receipts"]].count("record_acknowledgment") == 2
    assert all(item["action_type"] != "record_disposition" for item in case["receipts"])
    assert case["status"] == "closed"
    assert _receipt_count(operations) == 6


@pytest.mark.asyncio
async def test_product_adapter_routes_exact_lot_through_required_disposition(
    tmp_path: Path,
) -> None:
    operations = tmp_path / "operations.sqlite3"
    adapter = DurableRuntimeAdapter(
        checkpoint_path=tmp_path / "checkpoints.sqlite3",
        operations_path=operations,
    )
    opened = await adapter.open_case("H-1230-2026")
    opened["scope_lot_ids"] = ["LOT-EXACT-170"]
    case = await adapter.run_investigation(opened)
    reviewed_actions: list[str] = []
    receipt_versions: list[int] = []

    while case.get("pending_interrupt") is not None:
        pending = case["pending_interrupt"]
        if pending["kind"] in {"action_review", "closure_review"}:
            reviewed_actions.append(pending["action"]["action_type"])
            case = await adapter.resume_review(
                case,
                decision="approve",
                actor="Food-safety manager",
                justification=APPROVAL_JUSTIFICATION,
                edited_action="",
            )
        elif pending["kind"] == "execution_confirmation":
            before = len(case["receipts"])
            action_type = pending["action"]["action_type"]
            case = await adapter.simulate_approved_actions(case)
            if action_type == "close_case" and case["status"] == "open_closure_blocked":
                assert len(case["receipts"]) == before
            else:
                assert len(case["receipts"]) == before + 1
                receipt_versions.append(case["receipts"][-1]["case_version"])
        else:  # pragma: no cover - explicit contract assertion is clearer
            raise AssertionError(f"unexpected interrupt: {pending['kind']}")

    assert reviewed_actions[:3] == [
        "create_case",
        "apply_inventory_hold",
        "record_disposition",
    ]
    assert reviewed_actions.index("record_disposition") < reviewed_actions.index(
        "create_facility_tasks"
    )
    assert reviewed_actions[-1] == "close_case"
    assert receipt_versions == list(range(1, len(receipt_versions) + 1))
    assert [item["action_type"] for item in case["receipts"]] == reviewed_actions
    assert case["status"] == "closed"
    assert case["closure_outcome"] == {"eligible": True, "closed": True}
    assert _receipt_count(operations) == len(reviewed_actions)
