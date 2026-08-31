from __future__ import annotations

import sqlite3
from pathlib import Path

from streamlit.testing.v1 import AppTest

APP = Path(__file__).parents[2] / "src" / "recallops" / "ui" / "app.py"


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
