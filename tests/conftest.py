"""Source-backed live model fixtures; no provider or production authority is mocked."""

from copy import deepcopy
from types import SimpleNamespace

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

from recallops.agents.specialists import (
    assess_product_lots,
    assess_traceability,
    draft_containment,
    investigate_recall,
)
from recallops.data.loaders import load_recall_snapshot
from recallops.paths import DATA_DIR
from recallops.services.traceability import TraceabilityService


class SourceScriptModel(GenericFakeChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


@pytest.fixture
def live_case():
    recall = load_recall_snapshot(data_dir=DATA_DIR)
    intelligence = investigate_recall(recall)
    trace = TraceabilityService(data_dir=DATA_DIR, source_mode="snapshot")
    scope = ["LOT-EXACT-170", "LOT-PROBABLE-160", "LOT-AMBIG-175", "LOT-REJECT-190"]
    products = trace.find_candidate_products(intelligence.predicate)
    lots = [lot for lot in trace.match_lots(intelligence.predicate) if lot["lot_id"] in scope]
    matching = assess_product_lots(
        predicate=intelligence.predicate, candidate_products=products, candidate_lots=lots
    )
    relevant = [*matching.confirmed_lot_ids, *matching.ambiguous_lot_ids]
    events = [event for lot in relevant for event in trace.trace_forward(lot)]
    inventory = [position for lot in relevant for position in trace.get_inventory(lot)]
    reconciliations = [trace.reconcile_units(lot) for lot in relevant]
    tracing = assess_traceability(
        lot_ids=relevant,
        events=events,
        inventory_positions=inventory,
        reconciliations=reconciliations,
    )
    containment = draft_containment(
        case_id="CASE-LIVE", expected_case_version=0, matching=matching, traceability=tracing
    )
    roles = [
        "recall-intelligence",
        "product-lot-matching",
        "traceability-reconciliation",
        "containment-communications",
    ]
    artifacts = dict(
        zip(
            roles,
            [
                item.model_dump(mode="json")
                for item in (intelligence, matching, tracing, containment)
            ],
            strict=True,
        )
    )

    def script(raw=None, *, canary="PRIVATE_LIVE_MODEL_CANARY"):
        raw = deepcopy(artifacts if raw is None else raw)
        messages = []

        def call(name, args):
            messages.append(
                AIMessage(
                    content=canary,
                    tool_calls=[{"name": name, "args": args, "id": f"live-call-{len(messages)}"}],
                    usage_metadata={"input_tokens": 3, "output_tokens": 2, "total_tokens": 5},
                )
            )

        call(
            "write_todos",
            {"todos": [{"content": f"[{role}] {canary}", "status": "pending"} for role in roles]},
        )
        names = [
            "RecallIntelligence",
            "ProductLotAssessment",
            "TraceabilityAssessment",
            "ContainmentProposal",
        ]
        for role, name in zip(roles, names, strict=True):
            call("task", {"description": canary, "subagent_type": role})
            if role == roles[0]:
                call("get_recall", {"recall_number": "H-1230-2026"})
            if role == roles[1]:
                for tool in ("find_candidate_products", "match_lots"):
                    call(tool, {"predicate": intelligence.predicate.model_dump(mode="json")})
            if role == roles[2]:
                for lot in relevant:
                    for tool in (
                        "trace_forward",
                        "trace_backward",
                        "get_inventory",
                        "reconcile_units",
                    ):
                        call(tool, {"lot_id": lot})
            call(name, raw[role])
        call(
            "SupervisorResponse",
            {
                "outcome": "human_review",
                "evidence_count": 0,
                "confirmed_lot_count": 0,
                "ambiguous_lot_count": 0,
                "proposed_action_count": 0,
                "executed": False,
            },
        )
        return SourceScriptModel(messages=iter(messages))

    return SimpleNamespace(
        raw=artifacts,
        script=script,
        roles=roles,
        scope=scope,
        recall=recall,
        intelligence=intelligence,
        matching=matching,
        tracing=tracing,
        containment=containment,
        products=products,
        lots=lots,
        events=events,
        inventory=inventory,
        reconciliations=reconciliations,
    )


@pytest.fixture
async def live_request(live_case):
    import importlib.util

    assert importlib.util.find_spec("recallops.llm.artifacts"), "safe live claims are missing"
    from recallops.llm.artifacts import build_live_request
    from recallops.retrieval.agentic import AgenticRetriever, ClosedRetrievalGateway

    retriever = AgenticRetriever(ClosedRetrievalGateway.direct(data_dir=DATA_DIR))
    rag = await retriever.resume(
        retriever.start(
            "Investigate H-1230-2026 eggs recall",
            authoritative_facts={"recall_number": "H-1230-2026"},
        )
    )
    return build_live_request(
        case_id="CASE-LIVE",
        thread_id="THREAD-LIVE",
        case_version=0,
        recall_number="H-1230-2026",
        question="Investigate H-1230-2026 eggs recall",
        scope_lot_ids=live_case.scope,
        rag_result=rag.model_dump(mode="json"),
    )
