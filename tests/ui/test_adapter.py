from __future__ import annotations

import pytest

from recallops.ui.adapter import DeterministicDemoAdapter
from recallops.ui.presenters import APPROVAL_JUSTIFICATION, can_simulate, reduce_case_snapshot


@pytest.mark.asyncio
async def test_demo_adapter_requires_two_distinct_consents_and_one_write_per_version() -> None:
    adapter = DeterministicDemoAdapter()
    opened = await adapter.open_case("H-1230-2026")
    assert opened["matches"] == []
    assert opened["receipts"] == []

    investigated = await adapter.run_investigation(opened)
    assert investigated["status"] == "review_required"
    assert investigated["pending_interrupt"]["kind"] == "action_review"
    assert investigated["receipts"] == []

    approved = await adapter.resume_review(
        investigated,
        decision="approve",
        actor="Food-safety manager",
        justification=APPROVAL_JUSTIFICATION,
        edited_action="",
    )
    assert approved["status"] == "approved_pending_execution"
    assert approved["pending_interrupt"]["kind"] == "execution_confirmation"
    assert approved["receipts"] == []
    assert can_simulate(reduce_case_snapshot(approved))[0]

    executed = await adapter.simulate_approved_actions(approved)
    assert executed["case_version"] == 1
    assert len(executed["receipts"]) == 1
    assert executed["receipts"][0]["action"] == "create_case"
    assert executed["pending_interrupt"]["kind"] == "action_review"
    assert executed["proposed_actions"][0]["action_type"] == "apply_inventory_hold"


@pytest.mark.asyncio
async def test_demo_adapter_flagship_closure_and_failure_injection_are_truthful() -> None:
    adapter = DeterministicDemoAdapter()
    case = await adapter.run_investigation(await adapter.open_case("H-1230-2026"))
    closure = await adapter.request_closure(case)
    assert closure["closure"]["status"] == "Open — closure blocked"
    assert closure["closure"]["blockers"]

    injected = await adapter.inject_failure(case, "lost write response → same-key replay")
    assert injected["failure_result"]["status"] == "injected_fixture"
    assert injected["failure_result"]["safe_outcome"] == "same-key recovery required"
    assert injected["case_version"] == case["case_version"]
