"""UI scope must be explicit before spending and immutable after investigation starts."""

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from recallops.data.loaders import load_demo_dataset
from recallops.demo_contract import investigation_lot_options, validate_investigation_scope
from recallops.llm import LLMSettings
from recallops.ui.adapter import DurableRuntimeAdapter

APP = Path(__file__).parents[2] / "src/recallops/ui/app.py"
FLAGSHIP = ["LOT-EXACT-170", "LOT-PROBABLE-160", "LOT-AMBIG-175", "LOT-REJECT-190"]


@pytest.mark.parametrize("count", [1, 64])
def test_scope_bounds_accept_valid_unique_lots_without_aliasing(count):
    selected = list(investigation_lot_options()[:count])
    validated = validate_investigation_scope(selected)
    assert validated == selected
    assert validated is not selected


@pytest.mark.parametrize("mode", ["deterministic", "openai"])
async def test_opened_flagship_has_explicit_detached_four_lot_scope(tmp_path, mode):
    adapter = DurableRuntimeAdapter(
        checkpoint_path=tmp_path / "checkpoints.sqlite3",
        operations_path=tmp_path / "operations.sqlite3",
        llm_settings=LLMSettings(mode=mode, model="test-model"),
    )
    opened = await adapter.open_case("H-1230-2026")
    assert opened["scope_lot_ids"] == FLAGSHIP
    opened["scope_lot_ids"].clear()
    assert (await adapter.open_case("H-1230-2026"))["scope_lot_ids"] == FLAGSHIP
    assert not (tmp_path / "checkpoints.sqlite3").exists()


@pytest.mark.parametrize("scope", [[], None, ["UNKNOWN"], ["LOT-EXACT-170"] * 2, "oversized"])
async def test_invalid_scope_is_rejected_before_runtime_or_provider(tmp_path, monkeypatch, scope):
    import recallops.llm.live_reasoning as live

    calls = []

    def forbidden(settings):
        calls.append(settings)
        raise AssertionError("provider must not be constructed")

    monkeypatch.setattr(live, "build_chat_model", forbidden)
    adapter = DurableRuntimeAdapter(
        checkpoint_path=tmp_path / "checkpoints.sqlite3",
        operations_path=tmp_path / "operations.sqlite3",
        llm_settings=LLMSettings(mode="openai", model="test-model"),
    )
    opened = await adapter.open_case("H-1230-2026")
    opened["scope_lot_ids"] = (
        [row["lot_id"] for row in load_demo_dataset()["lots"]][:65]
        if scope == "oversized"
        else scope
    )
    with pytest.raises(ValueError):
        await adapter.run_investigation(opened)
    assert calls == []
    assert not (tmp_path / "checkpoints.sqlite3").exists()


async def test_selected_scope_is_checkpointed_and_stale_edits_cannot_replace_it(tmp_path):
    adapter = DurableRuntimeAdapter(
        checkpoint_path=tmp_path / "checkpoints.sqlite3",
        operations_path=tmp_path / "operations.sqlite3",
    )
    opened = await adapter.open_case("H-1230-2026")
    opened["scope_lot_ids"] = ["LOT-EXACT-170"]
    result = await adapter.run_investigation(opened)
    assert result["scope_lot_ids"] == ["LOT-EXACT-170"]
    opened["scope_lot_ids"] = ["LOT-PROBABLE-160"]
    replay = await adapter.run_investigation(opened)
    assert replay["scope_lot_ids"] == ["LOT-EXACT-170"]
    assert replay["checkpoint_id"] == result["checkpoint_id"]


def _scope_app(monkeypatch, tmp_path):
    monkeypatch.setenv("RECALLOPS_UI_MODE", "durable")
    monkeypatch.setenv("RECALLOPS_MODEL_MODE", "deterministic")
    monkeypatch.setenv("RECALLOPS_RUNTIME_DIR", str(tmp_path))
    app = AppTest.from_file(str(APP), default_timeout=30).run()
    app.button(key="open_case_button").click().run()
    app.radio(key="ui_active_view").set_value("Investigation").run()
    assert not app.exception
    return app


def test_investigation_shows_all_144_lots_with_bounded_default(monkeypatch, tmp_path):
    app = _scope_app(monkeypatch, tmp_path)
    control = next(item for item in app.multiselect if item.label == "Investigation lot scope")
    assert len(control.options) == 144
    assert set(control.options) == {row["lot_id"] for row in load_demo_dataset()["lots"]}
    assert control.value == FLAGSHIP
    assert control.proto.max_selections == 64
    assert not control.disabled
    captions = " ".join(item.value for item in app.caption)
    assert "144 lots" in captions and "bounded" in captions


def test_selected_ui_scope_survives_navigation_then_locks_after_start(monkeypatch, tmp_path):
    app = _scope_app(monkeypatch, tmp_path)
    app.multiselect(key="ui_investigation_lot_scope").set_value(["LOT-EXACT-170"]).run()
    app.radio(key="ui_active_view").set_value("Command Center").run()
    app.radio(key="ui_active_view").set_value("Investigation").run()
    assert app.multiselect(key="ui_investigation_lot_scope").value == ["LOT-EXACT-170"]
    app.button(key="run_investigation_button").click().run()
    assert not app.exception
    original = app.session_state["ui_case"]
    assert original["scope_lot_ids"] == ["LOT-EXACT-170"]
    assert {row["lot_id"] for row in original["matches"]} == {"LOT-EXACT-170"}
    assert app.multiselect(key="ui_investigation_lot_scope").disabled
    app.multiselect(key="ui_investigation_lot_scope").set_value(["LOT-PROBABLE-160"]).run()
    app.button(key="run_investigation_button").click().run()
    assert app.session_state["ui_case"]["scope_lot_ids"] == ["LOT-EXACT-170"]
    assert app.session_state["ui_case"]["checkpoint_id"] == original["checkpoint_id"]
    assert app.multiselect(key="ui_investigation_lot_scope").value == ["LOT-EXACT-170"]


def test_new_case_resets_previous_scope_and_unlocks_selector(monkeypatch, tmp_path):
    app = _scope_app(monkeypatch, tmp_path)
    app.multiselect(key="ui_investigation_lot_scope").set_value(["LOT-EXACT-170"]).run()
    app.button(key="run_investigation_button").click().run()
    app.radio(key="ui_active_view").set_value("Command Center").run()
    app.button(key="open_case_button").click().run()
    app.radio(key="ui_active_view").set_value("Investigation").run()
    assert not app.exception
    assert app.multiselect(key="ui_investigation_lot_scope").value == FLAGSHIP
    assert not app.multiselect(key="ui_investigation_lot_scope").disabled


def test_fixture_retains_fixed_four_lot_scope(monkeypatch, tmp_path):
    monkeypatch.setenv("RECALLOPS_UI_MODE", "demo")
    monkeypatch.setenv("RECALLOPS_RUNTIME_DIR", str(tmp_path))
    app = AppTest.from_file(str(APP), default_timeout=30).run()
    app.button(key="open_case_button").click().run()
    app.radio(key="ui_active_view").set_value("Investigation").run()
    control = app.multiselect(key="ui_investigation_lot_scope")
    assert control.value == FLAGSHIP
    assert control.disabled
    assert len(control.options) == 144
    assert any("fixed four-lot walkthrough" in item.value for item in app.caption)


def test_empty_ui_selection_cannot_start_an_unbounded_investigation(monkeypatch, tmp_path):
    app = _scope_app(monkeypatch, tmp_path)
    app.multiselect(key="ui_investigation_lot_scope").set_value([]).run()
    assert app.button(key="run_investigation_button").disabled
    assert app.session_state["ui_case"]["checkpoint_id"] is None
    assert not (tmp_path / "checkpoints.sqlite3").exists()
