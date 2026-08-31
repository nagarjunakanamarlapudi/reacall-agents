from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

from streamlit.testing.v1 import AppTest

APP = Path(__file__).parents[2] / "src" / "recallops" / "ui" / "app.py"


def _write_compact_evaluation_report(repository_root: Path) -> None:
    eval_dir = repository_root / "data" / "evals"
    eval_dir.mkdir(parents=True)
    corpus = {
        "schema_version": "1.0",
        "common_fixture": {"name": "app-smoke"},
        "scenarios": [{"id": "R13"}, {"id": "R18"}],
    }
    corpus_text = json.dumps(corpus, sort_keys=True, separators=(",", ":"))
    (eval_dir / "scenarios.json").write_text(corpus_text, encoding="utf-8")
    assertions = [
        {
            "id": "route_expected",
            "passed": True,
            "path": "/route_actual",
            "operator": "ordered_subsequence",
            "expected": ["H", "W"],
            "actual": ["H", "W"],
            "detail": "",
        }
    ]
    rate_metrics = {
        name: 1.0
        for name in (
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
    }
    report = {
        "schema_version": "1.1",
        "scenario_corpus_sha256": hashlib.sha256(corpus_text.encode()).hexdigest(),
        "execution_mode": "offline_deterministic",
        "run_metadata": {
            "report_kind": "run_specific_observation",
            "telemetry_policy": "observed_only",
            "timing_source": "measured_wall_clock",
        },
        "results": [
            {
                "id": scenario_id,
                "passed": True,
                "safety_critical": True,
                "assertions": assertions,
                "route_actual": ["H", "W"],
                "route_expected": ["H", "W"],
                "state_excerpt": {"large": "not rendered"},
                "tool_trace": [{"large": "not rendered"}],
                "failure_injection": [],
                "duration_ms": 10,
                "error": None,
            }
            for scenario_id in ("R13", "R18")
        ],
        "metrics": {
            **rate_metrics,
            "unauthorized_write_count": 0,
            "duplicate_logical_write_count": 0,
            "false_close_count": 0,
            "receipt_integrity_violation_count": 0,
        },
        "gate_passed": True,
    }
    (eval_dir / "report.json").write_text(json.dumps(report), encoding="utf-8")


def test_app_starts_on_command_center_with_pinned_empty_state(monkeypatch) -> None:
    monkeypatch.setenv("RECALLOPS_UI_MODE", "demo")
    app = AppTest.from_file(str(APP), default_timeout=15).run()
    assert not app.exception
    assert app.title[0].value == "RecallOps Command Center"
    assert app.subheader[0].value == "Command Center"
    assert app.text_input(key="ui_recall_number").value == "H-1230-2026"
    assert app.button(key="open_case_button").label == "Open case"
    assert any("Northstar Grocers is fictional" in item.value for item in app.caption)
    assert any("Synthetic operations boundary" in item.value for item in app.markdown)


def test_app_open_then_investigate_surfaces_agentic_rag_and_review(monkeypatch) -> None:
    monkeypatch.setenv("RECALLOPS_UI_MODE", "demo")
    app = AppTest.from_file(str(APP), default_timeout=30).run()
    app.button(key="open_case_button").click().run()
    assert any("OFFICIAL — openFDA snapshot" in item.value for item in app.markdown)

    app.radio(key="ui_active_view").set_value("Investigation").run()
    app.button(key="run_investigation_button").click().run(timeout=30)
    markdown = "\n".join(item.value for item in app.markdown)
    assert "Agentic RAG retrieval trace" in markdown
    assert any(
        "Draft only — approval required before simulated action." in item.value
        for item in app.warning
    )

    app.radio(key="ui_active_view").set_value("Human Review").run()
    rendered = "\n".join(item.value for item in (*app.markdown, *app.success, *app.warning))
    assert "Review required" in rendered
    assert app.button(key="approve_button").label == "Approve"
    assert app.button(key="simulate_button").disabled


def test_app_dual_consent_receipt_reconciliation_and_closure_block(monkeypatch) -> None:
    monkeypatch.setenv("RECALLOPS_UI_MODE", "demo")
    app = AppTest.from_file(str(APP), default_timeout=30).run()
    app.button(key="open_case_button").click().run()
    app.radio(key="ui_active_view").set_value("Investigation").run()
    app.button(key="run_investigation_button").click().run(timeout=30)

    app.radio(key="ui_active_view").set_value("Reconciliation").run()
    assert any(
        "received = on_hand + quarantined + sold + returned + disposed + unaccounted" in item.value
        for item in app.markdown
    )

    app.radio(key="ui_active_view").set_value("Human Review").run()
    app.button(key="approve_button").click().run()
    assert app.session_state.ui_write_receipts == []
    assert not app.button(key="simulate_button").disabled
    app.button(key="simulate_button").click().run()
    assert len(app.session_state.ui_write_receipts) == 1
    assert any("Simulated action recorded" in item.value for item in app.success)

    app.radio(key="ui_active_view").set_value("Audit & Evaluation").run()
    app.button(key="request_closure_button").click().run()
    assert any("Open — closure blocked" in item.value for item in app.warning)
    assert app.session_state.ui_case["status"] == "open_closure_blocked"


def test_app_default_product_mode_uses_durable_runtime(monkeypatch, tmp_path) -> None:
    runtime_dir = tmp_path / "runtime"
    monkeypatch.delenv("RECALLOPS_UI_MODE", raising=False)
    monkeypatch.setenv("RECALLOPS_RUNTIME_DIR", str(runtime_dir))
    app = AppTest.from_file(str(APP), default_timeout=30).run()
    assert any("Durable LangGraph + SQLite" in item.value for item in app.caption)

    app.button(key="open_case_button").click().run()
    assert app.session_state.ui_case["status"] == "intake_ready"
    app.radio(key="ui_active_view").set_value("Audit & Evaluation").run()
    app.selectbox(key="ui_failure_scenario").set_value(
        "openFDA unavailable → labelled frozen snapshot"
    )
    app.button(key="inject_failure_button").click().run()
    assert any("Next step: Run investigation" in item.value for item in app.caption)

    app.radio(key="ui_active_view").set_value("Investigation").run()
    app.button(key="run_investigation_button").click().run(timeout=30)
    assert app.session_state.ui_case["pending_interrupt"]["kind"] == "action_review"
    assert app.session_state.ui_case["failure_result"]["status"] == "observed"
    assert any(
        "pinned OFFICIAL_OPENFDA_SNAPSHOT fallback" in warning
        for warning in app.session_state.ui_case["warnings"]
    )

    app.radio(key="ui_active_view").set_value("Human Review").run()
    app.button(key="approve_button").click().run(timeout=30)
    assert app.session_state.ui_case["pending_interrupt"]["kind"] == "execution_confirmation"
    assert not any(item.value == "Review required" for item in app.warning)
    app.button(key="simulate_button").click().run(timeout=30)
    assert app.session_state.ui_case_version == 1
    assert len(app.session_state.ui_write_receipts) == 1

    connection = sqlite3.connect(runtime_dir / "operations.sqlite3")
    try:
        assert connection.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 1
    finally:
        connection.close()


def test_app_durable_audit_shows_detached_checkpoint_history(monkeypatch, tmp_path) -> None:
    """Break caught: the audit view loses durable history after raw graph access is sealed."""

    monkeypatch.delenv("RECALLOPS_UI_MODE", raising=False)
    monkeypatch.setenv("RECALLOPS_RUNTIME_DIR", str(tmp_path / "runtime"))
    app = AppTest.from_file(str(APP), default_timeout=30).run()
    app.button(key="open_case_button").click().run()
    app.radio(key="ui_active_view").set_value("Investigation").run()
    app.button(key="run_investigation_button").click().run(timeout=30)
    app.radio(key="ui_active_view").set_value("Audit & Evaluation").run()

    assert any("Durable checkpoint history" in item.value for item in app.markdown)
    history = app.session_state.ui_case["checkpoint_history"]
    assert history[0]["checkpoint_id"] == app.session_state.ui_case["checkpoint_id"]
    assert history[0]["pending_kind"] == "action_review"


def test_app_durable_audit_renders_verified_report_metrics_and_r13_r18(
    monkeypatch, tmp_path
) -> None:
    repository_root = tmp_path / "repository"
    _write_compact_evaluation_report(repository_root)
    monkeypatch.delenv("RECALLOPS_UI_MODE", raising=False)
    monkeypatch.setenv("RECALLOPS_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("RECALLOPS_REPOSITORY_ROOT", str(repository_root))
    app = AppTest.from_file(str(APP), default_timeout=30).run()
    app.button(key="open_case_button").click().run()
    app.radio(key="ui_active_view").set_value("Audit & Evaluation").run()

    rendered = "\n".join(
        item.value for item in (*app.success, *app.warning, *app.error, *app.info, *app.markdown)
    )
    frames = "\n".join(str(item.value) for item in app.dataframe)
    metrics = {item.label: item.value for item in app.metric}
    assert "Committed evaluation report verified: 2/2 scenarios passed" in rendered
    assert "R13" in frames and "R18" in frames
    assert "Scenario pass rate" in metrics and metrics["Scenario pass rate"] == "100.0%"
    assert "Unsafe counters" in metrics and metrics["Unsafe counters"] == "0"
    assert "not rendered" not in frames


def test_app_durable_audit_labels_missing_report_without_claiming_success(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.delenv("RECALLOPS_UI_MODE", raising=False)
    monkeypatch.setenv("RECALLOPS_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("RECALLOPS_REPOSITORY_ROOT", str(tmp_path / "empty-repository"))
    app = AppTest.from_file(str(APP), default_timeout=30).run()
    app.button(key="open_case_button").click().run()
    app.radio(key="ui_active_view").set_value("Audit & Evaluation").run()

    rendered = "\n".join(item.value for item in (*app.info, *app.warning, *app.error))
    assert "Committed evaluation report is missing" in rendered
    assert "No passing score is claimed" in rendered


def test_app_recovers_unknown_write_with_one_exact_key_receipt(monkeypatch, tmp_path) -> None:
    """Break caught: the public UI disables the runtime's exact-key recovery interrupt."""

    runtime_dir = tmp_path / "runtime"
    monkeypatch.delenv("RECALLOPS_UI_MODE", raising=False)
    monkeypatch.setenv("RECALLOPS_RUNTIME_DIR", str(runtime_dir))
    app = AppTest.from_file(str(APP), default_timeout=30).run()
    app.button(key="open_case_button").click().run()
    app.radio(key="ui_active_view").set_value("Investigation").run()
    app.button(key="run_investigation_button").click().run(timeout=30)
    app.radio(key="ui_active_view").set_value("Human Review").run()
    app.button(key="approve_button").click().run(timeout=30)

    app.radio(key="ui_active_view").set_value("Audit & Evaluation").run()
    app.selectbox(key="ui_failure_scenario").set_value("lost write response → same-key replay")
    app.button(key="inject_failure_button").click().run()
    app.radio(key="ui_active_view").set_value("Human Review").run()
    original_key = app.session_state.ui_case["pending_interrupt"]["idempotency_key"]
    app.button(key="simulate_button").click().run(timeout=30)
    assert app.session_state.ui_case["status"] == "write_outcome_unknown"

    app.run()
    recovery = app.button(key="simulate_button")
    assert recovery.label == "Recover recorded outcome (same key)"
    assert not recovery.disabled
    recovery.click().run(timeout=30)
    assert app.session_state.ui_case_version == 1
    assert len(app.session_state.ui_write_receipts) == 1
    assert app.session_state.ui_write_receipts[0]["idempotency_key"] == original_key

    connection = sqlite3.connect(runtime_dir / "operations.sqlite3")
    try:
        assert connection.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 1
    finally:
        connection.close()
