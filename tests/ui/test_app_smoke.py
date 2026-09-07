from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

APP = Path(__file__).parents[2] / "src" / "recallops" / "ui" / "app.py"


@pytest.mark.parametrize("mode", ["demo", "durable"])
def test_audit_symlink_loop_keeps_all_sections_and_closure_controls(monkeypatch, tmp_path, mode):
    repository = tmp_path / "repository"
    _write_compact_evaluation_report(repository)
    path = repository / "data" / "evals" / "retrieval_report.json"
    path.unlink()
    path.symlink_to(path.name)
    monkeypatch.setenv("RECALLOPS_UI_MODE", mode)
    monkeypatch.setenv("RECALLOPS_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("RECALLOPS_REPOSITORY_ROOT", str(repository))
    app = AppTest.from_file(str(APP), default_timeout=30).run()
    app.button(key="open_case_button").click().run()
    app.radio(key="ui_active_view").set_value("Audit & Evaluation").run()
    assert not app.exception
    assert any("Unavailable" in item.value for item in app.warning)
    rendered = "\n".join(item.value for item in app.markdown)
    assert all(
        name in rendered for name in ("Safety", "Retrieval quality", "Orchestration quality")
    )
    assert sum(item.value == "Unavailable" for item in app.markdown) == 3
    assert not app.button(key="request_closure_button").disabled
    assert app.button(key="inject_failure_button")
    assert not app.success


def test_completed_live_metrics_render_separately(monkeypatch, completed_live_repository):
    monkeypatch.setenv("RECALLOPS_UI_MODE", "demo")
    monkeypatch.setenv("RECALLOPS_REPOSITORY_ROOT", str(completed_live_repository.root))
    app = AppTest.from_file(str(APP), default_timeout=30).run()
    app.radio(key="ui_active_view").set_value("Audit & Evaluation").run()
    assert not app.exception
    frames = [item.value for item in app.dataframe]
    live = next(frame for frame in frames if "Live metric" in frame.columns)
    values = dict(zip(live["Live metric"], live["Value"], strict=True))
    assert values["Executed cases"] == "24"
    assert values["Tokens"] == "264"
    assert values["Estimated cost"] == "3.0"
    assert values["Provider"] == "[MASKED]"
    assert values["Model SHA-256"] == "a" * 64
    assert "excluded from offline gate" in " ".join(item.value.lower() for item in app.caption)
    assert any("Verified offline scorecard" in item.value for item in app.success)


def test_audit_evaluation_available_without_opening_case_and_family_filter(monkeypatch, tmp_path):
    repository = tmp_path / "repository"
    _write_compact_evaluation_report(repository)
    monkeypatch.setenv("RECALLOPS_UI_MODE", "demo")
    monkeypatch.setenv("RECALLOPS_REPOSITORY_ROOT", str(repository))
    app = AppTest.from_file(str(APP), default_timeout=30).run()
    app.radio(key="ui_active_view").set_value("Audit & Evaluation").run()
    assert not app.exception
    rendered = "\n".join(item.value for item in (*app.markdown, *app.caption, *app.success))
    for label in (
        "Safety",
        "Retrieval quality",
        "Orchestration quality",
        "576 results",
        "48 results",
        "offline_deterministic",
        "not_run_missing_credentials",
    ):
        assert label in rendered
    frames = [item.value for item in app.dataframe]
    assert any(len(frame) == 6 and "Recall@5" in frame.columns for frame in frames)
    assert any(len(frame) == 2 and "profile" in frame.columns for frame in frames)
    chart = json.loads(app.get("vega_lite_chart")[0].proto.spec)
    assert chart["encoding"]["xOffset"]["field"] == "Metric"
    assert chart["encoding"]["y"]["stack"] is None
    assert chart["encoding"]["y"]["scale"]["domain"] == [0, 1]
    app.selectbox(key="ui_evaluation_family").set_value("lineage").run()
    frame = next(item.value for item in app.dataframe if "Recall@5" in item.value.columns)
    assert list(frame["Cases"]) == [16] * 6


@pytest.mark.parametrize(
    "name", ["scorecard.json", "report.json", "retrieval_report.json", "orchestration_report.json"]
)
def test_audit_invalid_artifact_has_no_scores_or_passing_badge(monkeypatch, tmp_path, name):
    repository = tmp_path / "repository"
    _write_compact_evaluation_report(repository)
    (repository / "data" / "evals" / name).write_text('{"secret":"do-not-render",broken')
    monkeypatch.setenv("RECALLOPS_UI_MODE", "demo")
    monkeypatch.setenv("RECALLOPS_REPOSITORY_ROOT", str(repository))
    app = AppTest.from_file(str(APP), default_timeout=30).run()
    app.radio(key="ui_active_view").set_value("Audit & Evaluation").run()
    assert not app.exception
    assert any("Unavailable" in item.value for item in app.warning)
    assert not app.success
    assert not app.dataframe
    assert not any(item.label in {"Scenario pass rate", "Unsafe counters"} for item in app.metric)


def _write_compact_evaluation_report(repository_root: Path) -> None:
    eval_dir = repository_root / "data" / "evals"
    eval_dir.mkdir(parents=True)
    for path in (APP.parents[3] / "data" / "evals").glob("*.json"):
        shutil.copyfile(path, eval_dir / path.name)


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
        item.value
        for item in (*app.success, *app.warning, *app.error, *app.info, *app.markdown, *app.caption)
    )
    frames = "\n".join(str(item.value) for item in app.dataframe)
    metrics = {item.label: item.value for item in app.metric}
    assert "21 safety scenarios" in rendered
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
    assert "Unavailable" in rendered
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
