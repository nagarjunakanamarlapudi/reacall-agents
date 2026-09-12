"""Independent source truth, completeness, and receipt coverage control live authority."""

import copy
import importlib.util

import pytest


def verifier():
    assert importlib.util.find_spec("recallops.agents.verification"), (
        "independent live verifier missing"
    )
    from recallops.agents import verification

    return verification


def test_source_verifier_exists_before_live_authority():
    assert verifier().verify_live_investigation


async def test_source_backed_partial_containment_passes(live_case, live_request):
    from recallops.llm.artifacts import project_specialist_claims

    mod = verifier()
    evidence = await mod.resolve_trusted_evidence(live_request)
    claims = project_specialist_claims(live_request, live_case.raw)
    accepted = mod.verify_live_investigation(
        live_request, claims, evidence, receipts=evidence.required_receipts
    )
    assert accepted.result.passed is True
    assert accepted.projection["confirmed_lot_ids"] == ["LOT-EXACT-170", "LOT-PROBABLE-160"]
    assert accepted.projection["ambiguous_lot_ids"] == ["LOT-AMBIG-175"]
    assert accepted.projection["evidence_gaps"]


@pytest.mark.parametrize(
    "mutation",
    [
        "classification",
        "missing_candidate",
        "false_hazard",
        "missing_facility",
        "foreign_event",
        "reversed_trace",
        "quantity",
        "component",
        "empty_containment",
        "ambiguous_hold",
        "citation",
        "unknown_gap",
        "missing_receipt",
        "wrong_receipt",
        "source_revision",
        "invented_universe",
        "missing_event",
        "false_gap_absence",
    ],
)
async def test_false_or_incomplete_claim_never_produces_actionable_projection(
    live_case, live_request, mutation
):
    from recallops.llm.artifacts import project_specialist_claims

    mod = verifier()
    evidence = await mod.resolve_trusted_evidence(live_request)
    raw = copy.deepcopy(live_case.raw)
    match = raw["product-lot-matching"]
    trace = raw["traceability-reconciliation"]
    contain = raw["containment-communications"]
    if mutation == "classification":
        match["decisions"][0]["classification"] = "probable"
    elif mutation == "missing_candidate":
        match["decisions"].pop()
    elif mutation == "false_hazard":
        raw["recall-intelligence"]["predicate"]["hazard"] = "Harmless"
    elif mutation == "missing_facility":
        facility = trace["coverage"][0]["facility_ids"].pop()
        trace["coverage"][0]["facility_evidence"].pop(facility)
        trace["affected_facilities"] = sorted(
            {f for row in trace["coverage"] for f in row["facility_ids"]}
        )
    elif mutation == "foreign_event":
        trace["coverage"][0]["facility_evidence"][trace["coverage"][0]["facility_ids"][0]] = [
            trace["coverage"][1]["event_ids"][0]
        ]
    elif mutation == "reversed_trace":
        trace["coverage"][0]["forward_event_ids"].reverse()
        trace["forward_traces"][trace["lot_ids"][0]].reverse()
    elif mutation == "quantity":
        trace["reconciliations"][0]["sold"] += 1
        trace["reconciliations"][0]["unaccounted"] -= 1
    elif mutation == "component":
        trace["reconciliations"][0]["component_evidence"]["sold"] = trace["reconciliations"][0][
            "component_evidence"
        ]["received"]
    elif mutation == "empty_containment":
        contain["proposed_actions"] = []
        contain["communication_drafts"] = []
    elif mutation == "ambiguous_hold":
        contain["proposed_actions"][0]["target_ids"].append("LOT-AMBIG-175")
    elif mutation == "citation":
        raw["recall-intelligence"]["citations"] = ["openfda:FOREIGN"]
    elif mutation == "unknown_gap":
        trace["evidence_gaps"].append("PRIVATE_GAP_CANARY")
    elif mutation == "invented_universe":

        def invent(value):
            if isinstance(value, str) and value.startswith(("EV-", "INV-", "STORE-", "DC-")):
                return "FABRICATED-" + value
            if isinstance(value, list):
                return [invent(row) for row in value]
            if isinstance(value, dict):
                return {invent(key): invent(row) for key, row in value.items()}
            return value

        raw = invent(raw)
    elif mutation == "missing_event":
        event = trace["coverage"][0]["event_ids"][0]

        def omit(value):
            if isinstance(value, list):
                return [omit(row) for row in value if row != event]
            if isinstance(value, dict):
                return {key: omit(row) for key, row in value.items()}
            return value

        raw = omit(raw)
    elif mutation == "false_gap_absence":
        trace["evidence_gaps"] = []
    claims = project_specialist_claims(live_request, raw)
    receipts = evidence.required_receipts
    if mutation == "missing_receipt":
        receipts = receipts[1:]
    if mutation == "wrong_receipt":
        receipts = (receipts[0].model_copy(update={"result_digest": "0" * 64}), *receipts[1:])
    if mutation == "source_revision":
        live_request = live_request.model_copy(update={"source_digest": "0" * 64})
    accepted = mod.verify_live_investigation(live_request, claims, evidence, receipts=receipts)
    assert accepted.result.passed is False
    assert accepted.projection is None
    assert accepted.result.violations
    assert "PRIVATE_GAP_CANARY" not in accepted.result.model_dump_json()


@pytest.mark.parametrize("fault", ["parent_cycle", "missing_parent", "facility_continuity"])
async def test_resolver_independently_checks_source_lineage(live_request, fault):
    from recallops.paths import DATA_DIR
    from recallops.services.recall_registry import RecallRegistryService
    from recallops.services.traceability import TraceabilityService

    trace = TraceabilityService(data_dir=DATA_DIR, source_mode="snapshot")
    registry = RecallRegistryService(data_dir=DATA_DIR, source_mode="snapshot")

    class SourceGateway:
        def __getattr__(self, name):
            if name == "get_recall":
                return registry.get_recall
            if name != "trace_forward":
                return getattr(trace, name)

            def read(lot_id):
                records = copy.deepcopy(trace.trace_forward(lot_id))
                if fault == "parent_cycle":
                    records[0]["parent_event_id"] = records[-1]["event_id"]
                elif fault == "missing_parent":
                    records[-1]["parent_event_id"] = "EV-NOT-PRESENT"
                else:
                    records[-1]["from_facility"] = "FACILITY-FOREIGN"
                return records

            return read

    with pytest.raises(ValueError):
        await verifier().resolve_trusted_evidence(live_request, gateway=SourceGateway())
