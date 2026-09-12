"""The one-case smoke must retain honest outcomes without touching full reports."""

import json

import pytest

from recallops.llm import LLMSettings


@pytest.mark.parametrize("invalid", [False, True])
async def test_smoke_scores_one_real_graph_result_and_never_overstates_verification(
    tmp_path, monkeypatch, live_case, invalid
):
    import recallops.llm.live_reasoning as live
    from recallops.evaluation.live_smoke import run_live_smoke

    raw = live_case.raw
    if invalid:
        raw["product-lot-matching"]["decisions"][0]["classification"] = "probable"
    monkeypatch.setattr(live, "build_chat_model", lambda settings: live_case.script(raw))
    output = tmp_path / "smoke.json"
    report = await run_live_smoke(LLMSettings(mode="openai", model="test-model"), output)
    assert report["evaluation_kind"] == "one_case_live_smoke"
    assert report["executed_case_count"] == 1
    assert report["verification_passed"] is (not invalid)
    assert report["passed"] is (not invalid)
    assert report["read_receipt_count"] == 15
    assert report["specialist_count"] == 4
    assert report["cost"] is None
    assert report["tokens"] == 125
    assert json.loads(output.read_text()) == report
    assert set(tmp_path.iterdir()) == {output}


async def test_smoke_refuses_overwriting_existing_reports_before_provider(tmp_path):
    from recallops.evaluation.live_smoke import run_live_smoke

    output = tmp_path / "orchestration_report.json"
    output.write_text("existing benchmark")
    with pytest.raises(FileExistsError):
        await run_live_smoke(LLMSettings(mode="openai", model="test-model"), output)
    assert output.read_text() == "existing benchmark"


async def test_provider_failure_has_no_verification_or_invented_usage(tmp_path, monkeypatch):
    import recallops.llm.live_reasoning as live
    from recallops.evaluation.live_smoke import run_live_smoke

    def unavailable(settings):
        raise TimeoutError("PRIVATE_SMOKE_ERROR")

    monkeypatch.setattr(live, "build_chat_model", unavailable)
    output = tmp_path / "failed.json"
    report = await run_live_smoke(LLMSettings(mode="openai", model="test-model"), output)
    assert report["passed"] is False
    assert report["verification_passed"] is False
    assert report["result_status"] == "execution_failure"
    assert report["tokens"] is None
    assert report["read_receipt_count"] == 0
    assert report["specialist_count"] == 0
    assert "PRIVATE_SMOKE_ERROR" not in output.read_text()
