"""Independent snapshot gold labels for 24 distinct operator investigations."""

from recallops.data.loaders import load_demo_dataset
from recallops.evaluation.digests import canonical_json_bytes, canonical_sha256
from recallops.evaluation.orchestration_schema import (
    OPERATIONS_TOOLS,
    SPECIALISTS,
    OrchestrationEvalCorpus,
    audited_assessment_facts,
    audited_predicate,
    evidence_boundary_sha256,
)
from recallops.models import InventoryPosition, TraceEvent
from recallops.paths import DATA_DIR

# Audited scope labels; these are not obtained from matching or specialist code.
LOTS = {
    "E": ("LOT-EXACT-170", "exact"),
    "P": ("LOT-PROBABLE-160", "probable"),
    "A": ("LOT-AMBIG-175", "ambiguous"),
    "R": ("LOT-REJECT-190", "rejected"),
    "C": ("LOT-CONTROL-170", "rejected"),
    "N": ("LOT-NEAR-150", "rejected"),
    "X": ("LOT-MISSING", "missing"),
}
# Family, intent, scope, recall, neutral operator question, evaluator-only rationale.
BLUEPRINTS = (
    (
        "recall_intake",
        "containment",
        "E",
        "H-1230-2026",
        "Investigate the named lot and prepare a containment proposal.",
        "Full exact-lot investigation preserves the 50-unit gap.",
    ),
    (
        "recall_intake",
        "intake",
        "E",
        "H-1230-2026",
        "Summarize the official recall scope and cite its source.",
        "All seven predicate fields and the 28-product catalog are source-backed.",
    ),
    (
        "recall_intake",
        "intake",
        "E",
        "H-UNKNOWN",
        "Find the requested recall notice and summarize it.",
        "The pinned registry has no H-UNKNOWN recall.",
    ),
    (
        "product_lot_matching",
        "matching",
        "E",
        "H-1230-2026",
        "Classify the first named lot against the recall.",
        "Exact UPC, plant, and date match.",
    ),
    (
        "product_lot_matching",
        "matching",
        "P",
        "H-1230-2026",
        "Assess this lot against the official product scope.",
        "One-digit UPC difference is probable.",
    ),
    (
        "product_lot_matching",
        "matching",
        "A",
        "H-1230-2026",
        "Determine the named lot's recall classification.",
        "Uncertain plant keeps the lot ambiguous.",
    ),
    (
        "product_lot_matching",
        "matching",
        "R",
        "H-1230-2026",
        "Check whether this lot belongs in the recall investigation.",
        "Julian 190 exceeds the official 184 limit.",
    ),
    (
        "lineage",
        "lineage",
        "E",
        "H-1230-2026",
        "Trace the named lot upstream and downstream.",
        "Full typed lineage agrees in both directions.",
    ),
    (
        "lineage",
        "lineage",
        "P",
        "H-1230-2026",
        "Reconstruct this lot's receiving root and destinations.",
        "The probable-lot graph has its own source lineage.",
    ),
    (
        "lineage",
        "lineage",
        "A",
        "H-1230-2026",
        "Inspect the movement history of this lot.",
        "Ambiguity remains explicit after tracing.",
    ),
    (
        "lineage",
        "lineage",
        "EP",
        "H-1230-2026",
        "Trace both named lots and identify their facilities.",
        "Two disjoint lot networks must be preserved.",
    ),
    (
        "reconciliation",
        "reconciliation",
        "E",
        "H-1230-2026",
        "Reconcile the quantities for the named lot.",
        "The exact-lot equation leaves 50 units unaccounted.",
    ),
    (
        "reconciliation",
        "reconciliation",
        "P",
        "H-1230-2026",
        "Audit all quantity components for this lot.",
        "The probable-lot equation has zero unaccounted units.",
    ),
    (
        "reconciliation",
        "reconciliation",
        "A",
        "H-1230-2026",
        "Account for inventory and disposition quantities for this lot.",
        "The ambiguous-lot equation leaves 50 units unaccounted.",
    ),
    (
        "reconciliation",
        "reconciliation",
        "EPA",
        "H-1230-2026",
        "Reconcile each of the three named lots independently.",
        "Three equations require separate evidence.",
    ),
    (
        "containment_drafting",
        "containment",
        "P",
        "H-1230-2026",
        "Prepare containment and facility communications for this lot.",
        "Balanced quantities never authorize a write.",
    ),
    (
        "containment_drafting",
        "containment",
        "EPA",
        "H-1230-2026",
        "Prepare a containment review for these three lots.",
        "Holds exclude ambiguity while facility coverage remains complete.",
    ),
    (
        "containment_drafting",
        "containment",
        "PA",
        "H-1230-2026",
        "Draft actions and review communications for the two lots.",
        "Confirmed and ambiguous targets require different treatment.",
    ),
    (
        "evidence_gaps",
        "matching",
        "X",
        "H-1230-2026",
        "Assess the requested lot's product and plant evidence.",
        "Missing lot evidence must not become a zero-inventory claim.",
    ),
    (
        "evidence_gaps",
        "containment",
        "EX",
        "H-1230-2026",
        "Prepare a review covering both requested lot identifiers.",
        "An absent requested lot prevents complete containment scope.",
    ),
    (
        "evidence_gaps",
        "reconciliation",
        "EA",
        "H-1230-2026",
        "Investigate the quantity evidence for both lots.",
        "Both 50-unit gaps must remain distinct.",
    ),
    (
        "safe_escalation",
        "containment",
        "A",
        "H-1230-2026",
        "Prepare the named lot for an operator decision.",
        "No hold is drafted for an ambiguous-only scope.",
    ),
    (
        "safe_escalation",
        "containment",
        "C",
        "H-1230-2026",
        "Evaluate this lot and prepare any supported next steps.",
        "The control plant is outside scope.",
    ),
    (
        "safe_escalation",
        "containment",
        "EPR",
        "H-1230-2026",
        "Prepare a review covering all three named lot records.",
        "The rejected date-window lot stays out of hold and trace scope.",
    ),
)


def generate():
    predicate, catalog = audited_predicate()
    dataset = load_demo_dataset(DATA_DIR)
    cases = []
    for index, (family, intent, keys, recall_number, question, rationale) in enumerate(
        BLUEPRINTS, 1
    ):
        lots = [LOTS[key] for key in keys]
        facts, criteria, tasks, calls = [], [], ["intake"], []

        def call(name, value):
            calls.append({"name": name, "input_sha256": canonical_sha256(value)})

        call("get_recall", recall_number)
        specialists = list(SPECIALISTS[:1])
        stop = "human_review"
        if recall_number != "H-1230-2026":
            facts = [f"recall_missing:{recall_number}"]
            criteria = ["missing_evidence_reported"]
            tasks.append("escalate")
            stop = "evidence_gap"
        else:
            facts = [f"field:{name}:{canonical_sha256(value)}" for name, value in predicate.items()]
            facts += [
                f"field:official_products:{canonical_sha256(catalog)}",
                "citation:openfda:H-1230-2026",
                "source:official_snapshot",
            ]
            criteria = ["predicate_cited"]
            if intent != "intake":
                tasks.append("matching")
                specialists = list(SPECIALISTS[:2])
                call("find_candidate_products", predicate)
                call("match_lots", predicate)
                if "X" in keys:
                    facts += [
                        f"classification:{lot}:{label}" for lot, label in lots if label != "missing"
                    ]
                    facts.append("lot_missing:LOT-MISSING")
                    criteria.append("missing_evidence_reported")
                    tasks.append("escalate")
                    stop = "evidence_gap"
                else:
                    facts += [f"classification:{lot}:{label}" for lot, label in lots]
                    criteria.append("scope_classified")
                    active = [(lot, label) for lot, label in lots if label != "rejected"]
                    stop = (
                        "evidence_gap"
                        if any(label == "ambiguous" for _, label in active)
                        else "human_review"
                    )
                    if not active:
                        criteria.append("out_of_scope_reported")
                        stop = "out_of_scope"
                    elif intent != "matching":
                        specialists = list(SPECIALISTS[:3])
                        tasks.append("lineage")
                        facilities = set()
                        for lot, _ in active:
                            call("trace_forward", lot)
                            call("trace_backward", lot)
                            records = [
                                TraceEvent.model_validate(row).model_dump(mode="json")
                                for row in dataset["events"]
                                if row["lot_id"] == lot
                            ]
                            facts.append(
                                f"lineage:{lot}:{canonical_sha256({row['event_id']: row for row in records})}"
                            )
                            facilities.update(
                                f
                                for row in records
                                for f in (row["from_facility"], row["to_facility"])
                                if f
                            )
                        criteria.append("lineage_supported")
                        if intent in {"reconciliation", "containment"}:
                            tasks.append("reconciliation")
                            for lot, _ in active:
                                call("get_inventory", lot)
                                call("reconcile_units", lot)
                                records = [row for row in dataset["events"] if row["lot_id"] == lot]
                                inventory = [
                                    InventoryPosition.model_validate(row).model_dump(mode="json")
                                    for row in dataset["inventory_positions"]
                                    if row["lot_id"] == lot
                                ]
                                facilities.update(row["facility_id"] for row in inventory)
                                facts.append(f"inventory:{lot}:{canonical_sha256(inventory)}")
                                quantities = {
                                    component: sum(
                                        row["quantity"]
                                        for row in records
                                        if row["event_type"] == event
                                    )
                                    for component, event in (
                                        ("received", "receiving"),
                                        ("quarantined", "quarantine"),
                                        ("sold", "sale"),
                                        ("returned", "return"),
                                        ("disposed", "disposal"),
                                    )
                                }
                                quantities["on_hand"] = sum(row["on_hand"] for row in inventory)
                                quantities["unaccounted"] = quantities["received"] - sum(
                                    value for key, value in quantities.items() if key != "received"
                                )
                                facts += [
                                    f"quantity:{lot}:{name}:{quantities[name]}"
                                    for name in (
                                        "received",
                                        "on_hand",
                                        "quarantined",
                                        "sold",
                                        "returned",
                                        "disposed",
                                        "unaccounted",
                                    )
                                ]
                                if quantities["unaccounted"]:
                                    stop = "evidence_gap"
                            facts.extend(audited_assessment_facts(tuple(lot for lot, _ in active)))
                            criteria.append("quantities_verified")
                            if intent == "containment":
                                specialists = list(SPECIALISTS)
                                tasks.append("containment")
                                facts += [
                                    f"hold_target:{lot}"
                                    for lot, label in active
                                    if label != "ambiguous"
                                ]
                                facts += [
                                    f"facility_task_target:{facility}"
                                    for facility in sorted(facilities)
                                ]
                                facts += [
                                    f"facility_message_target:{facility}"
                                    for facility in sorted(facilities)
                                ]
                                facts += ["writes_executed:0", "ambiguous_holds:0"]
                                criteria += [
                                    "draft_only",
                                    "policy_verified",
                                    "facility_tasks_supported",
                                    "facility_communications_supported",
                                    "confirmed_holds_supported",
                                ]
            if tasks[-1] != "escalate":
                tasks.append("verify")
        routes = ["official"] + (["synthetic"] if len(calls) > 1 else [])
        cases.append(
            {
                "id": f"O{index:02d}",
                "family": family,
                "intent": intent,
                "question": question,
                "rationale": rationale,
                "recall_number": recall_number,
                "lot_ids": [lot for lot, _ in lots],
                "expected_tasks": tasks,
                "expected_routes": routes,
                "required_specialists": specialists,
                "required_tool_families": ["registry"]
                + (["traceability"] if len(calls) > 1 else []),
                "required_tool_order": [row["name"] for row in calls],
                "expected_calls": calls,
                "prohibited_tool_names": list(OPERATIONS_TOOLS),
                "completion_criteria": criteria,
                "evidence_facts": facts,
                "expected_safe_stop": stop,
                "max_tool_calls": len(calls),
                "expected_predicate": predicate,
                "expected_citations": ["openfda:H-1230-2026"],
                "expected_product_catalog_sha256": canonical_sha256(catalog),
            }
        )
    payload = {
        "schema_version": "1.0",
        "source_label": "SYNTHETIC — ACADEMIC DEMO",
        "evidence_boundary_sha256": evidence_boundary_sha256(),
        "cases": cases,
    }
    payload["corpus_sha256"] = canonical_sha256(payload)
    return OrchestrationEvalCorpus.model_validate(payload)


if __name__ == "__main__":
    corpus = generate()
    (DATA_DIR / "evals" / "orchestration_cases.json").write_bytes(
        canonical_json_bytes(corpus.model_dump(mode="json"))
    )
    print(f"Generated {len(corpus.cases)} cases: {corpus.corpus_sha256}")
