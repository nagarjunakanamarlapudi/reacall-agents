from __future__ import annotations

from pathlib import Path

from streamlit.testing.v1 import AppTest

APP = Path(__file__).parents[2] / "src" / "recallops" / "ui" / "app.py"


def test_app_starts_on_command_center_with_pinned_empty_state() -> None:
    app = AppTest.from_file(str(APP), default_timeout=15).run()
    assert not app.exception
    assert app.title[0].value == "RecallOps Command Center"
    assert app.subheader[0].value == "Command Center"
    assert app.text_input(key="ui_recall_number").value == "H-1230-2026"
    assert app.button(key="open_case_button").label == "Open case"
    assert any("Northstar Grocers is fictional" in item.value for item in app.caption)
    assert any("Synthetic operations boundary" in item.value for item in app.markdown)


def test_app_open_then_investigate_surfaces_agentic_rag_and_review() -> None:
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


def test_app_dual_consent_receipt_reconciliation_and_closure_block() -> None:
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
