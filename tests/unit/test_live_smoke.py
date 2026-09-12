"""The one-case smoke must retain honest outcomes without touching full reports."""

import json

import pytest

from recallops.llm import LLMSettings


@pytest.mark.parametrize("invalid", [False, True])
async def test_smoke_scores_one_real_graph_result_and_never_overstates_verification(
    tmp_path, monkeypatch, live_case, invalid
):
    import recallops.llm.live_reasoning as live
    from recallops.agents.deep_supervisor import live_prompt_fingerprint
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
    assert report["prompt_sha256"] == live_prompt_fingerprint()
    assert len(report["read_tool_sequence"]) == 15
    assert all(set(row) <= {"kind", "name", "status"} for row in report["events"])
    assert "PRIVATE_LIVE_MODEL_CANARY" not in output.read_text()
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


async def test_failed_smoke_keeps_safe_observed_reads_not_only_accepted_receipts(
    tmp_path, monkeypatch, live_case
):
    import recallops.llm.live_reasoning as live
    from recallops.evaluation.live_smoke import run_live_smoke

    del live_case.raw["recall-intelligence"]["evidence_gaps"]
    monkeypatch.setattr(live, "build_chat_model", lambda settings: live_case.script())
    output = tmp_path / "semantic.json"
    report = await run_live_smoke(LLMSettings(mode="openai", model="test-model"), output)
    assert report["result_status"] == "semantic_failure"
    assert report["read_receipt_count"] == 0
    assert report["read_tool_sequence"] == ["get_recall"]
    assert any(row.get("name") == "get_recall" for row in report["events"])
    assert "PRIVATE_LIVE_MODEL_CANARY" not in output.read_text()


def test_prompt_fingerprint_covers_all_runtime_contracts(monkeypatch):
    import recallops.agents.deep_supervisor as deep
    from recallops.llm.artifacts import canonical_digest

    baseline = canonical_digest(deep.live_prompt_contract())
    assert deep.live_prompt_fingerprint() == baseline
    assert deep.live_prompt_contract()["contract_version"] == 1
    for name in (
        "SUPERVISOR_PROMPT",
        "RECALL_INTELLIGENCE_PROMPT",
        "PRODUCT_LOT_MATCHING_PROMPT",
        "TRACEABILITY_RECONCILIATION_PROMPT",
        "CONTAINMENT_COMMUNICATIONS_PROMPT",
        "DELEGATION_CONTEXT_PROMPT",
        "SUPERVISOR_RUNTIME_PROMPT",
        "INVESTIGATION_REQUEST_PROMPT",
    ):
        with monkeypatch.context() as patch:
            patch.setattr(deep, name, getattr(deep, name) + " contract revision")
            assert canonical_digest(deep.live_prompt_contract()) != baseline, name


async def test_builtin_and_legacy_evaluation_metadata_share_composed_fingerprint():
    from dataclasses import replace

    from recallops.agents.deep_supervisor import live_prompt_fingerprint
    from recallops.evaluation.openai_live_adapter import build_openai_live_factory
    from recallops.evaluation.orchestration_benchmark import _run_live_profile

    def unavailable():
        raise RuntimeError("offline factory boundary")

    factory = build_openai_live_factory(LLMSettings(mode="openai", model="test-model"))
    assert factory.prompt_sha256 == live_prompt_fingerprint()
    result = await _run_live_profile(None, replace(factory, factory=unavailable))
    assert result.error_code == "factory_error"
    assert result.prompt_sha256 == live_prompt_fingerprint()
    legacy = await _run_live_profile(None, None)
    assert legacy.error_code == "factory_error"
    assert legacy.prompt_sha256 == live_prompt_fingerprint()
