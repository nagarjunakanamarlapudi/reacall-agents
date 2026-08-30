"""Typed, deterministic specialists for the offline RecallOps reasoning plane."""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from recallops.models import (
    CandidateProduct,
    InventoryPosition,
    LotMatch,
    MatchClassification,
    ProposedAction,
    Provenance,
    RecallPredicate,
    RecallRecord,
    Reconciliation,
    TraceEvent,
)

SYNTHETIC_LABEL = "SYNTHETIC — ACADEMIC DEMO"


class OfficialProductScope(BaseModel):
    item_number: int = Field(ge=1)
    description: str = Field(min_length=1)
    upc: str | None = None


class RecallIntelligence(BaseModel):
    recall_number: str
    predicate: RecallPredicate
    official_products: list[OfficialProductScope] = Field(min_length=1)
    classification: str
    status: str
    source_provenance: Provenance
    citations: list[str] = Field(min_length=1)
    evidence_gaps: list[str] = Field(default_factory=list)


class MatchDecision(BaseModel):
    product_id: str
    lot_id: str
    classification: MatchClassification
    product_score: float = Field(ge=0, le=1)
    product_classification: Literal["exact", "probable", "rejected"]
    matched_fields: list[str] = Field(default_factory=list)
    rationale: str = Field(min_length=1)
    requires_human_review: bool = False
    evidence_ids: list[str] = Field(min_length=2)

    @model_validator(mode="after")
    def ambiguity_requires_review(self) -> MatchDecision:
        if self.classification == "ambiguous" and not self.requires_human_review:
            raise ValueError("ambiguous match requires human review")
        if self.classification != "ambiguous" and self.requires_human_review:
            raise ValueError("only ambiguous matches require match review")
        return self


class ProductLotAssessment(BaseModel):
    decisions: list[MatchDecision]
    confirmed_lot_ids: list[str]
    ambiguous_lot_ids: list[str]

    @model_validator(mode="after")
    def summaries_match_decisions(self) -> ProductLotAssessment:
        confirmed = [
            item.lot_id for item in self.decisions if item.classification in {"exact", "probable"}
        ]
        ambiguous = [item.lot_id for item in self.decisions if item.classification == "ambiguous"]
        if self.confirmed_lot_ids != confirmed:
            raise ValueError("confirmed lot summary does not match decisions")
        if self.ambiguous_lot_ids != ambiguous:
            raise ValueError("ambiguous lot summary does not match decisions")
        return self


class LotTraceCoverage(BaseModel):
    lot_id: str
    facility_ids: list[str]
    event_ids: list[str]
    forward_event_ids: list[str]
    backward_event_ids: list[str]
    inventory_evidence_ids: list[str]
    reconciliation_evidence_ids: list[str]
    facility_evidence: dict[str, list[str]]
    unaccounted_units: int = Field(ge=0)
    complete: bool


class TraceabilityAssessment(BaseModel):
    lot_ids: list[str]
    affected_facilities: list[str]
    coverage: list[LotTraceCoverage]
    forward_traces: dict[str, list[str]]
    backward_traces: dict[str, list[str]]
    reconciliations: list[Reconciliation]
    evidence_ids: list[str]
    evidence_gaps: list[str]

    @model_validator(mode="after")
    def evidence_is_declared(self) -> TraceabilityAssessment:
        known = set(self.evidence_ids)
        referenced = {
            evidence_id
            for item in self.coverage
            for evidence_id in (
                *item.event_ids,
                *item.inventory_evidence_ids,
                *item.reconciliation_evidence_ids,
            )
        }
        if not referenced <= known:
            raise ValueError("coverage cites unknown evidence")
        coverage_lots = [item.lot_id for item in self.coverage]
        if coverage_lots != self.lot_ids:
            raise ValueError("trace coverage must follow delegated lot order")
        if set(self.forward_traces) != set(self.lot_ids) or set(self.backward_traces) != set(
            self.lot_ids
        ):
            raise ValueError("forward and backward traces are required for every lot")
        facilities = sorted({facility for item in self.coverage for facility in item.facility_ids})
        if self.affected_facilities != facilities:
            raise ValueError("affected facilities do not match trace coverage")
        for item in self.coverage:
            if set(item.facility_evidence) != set(item.facility_ids) or any(
                not identifiers for identifiers in item.facility_evidence.values()
            ):
                raise ValueError(f"{item.lot_id} has a facility without evidence support")
            facility_citations = {
                evidence_id
                for identifiers in item.facility_evidence.values()
                for evidence_id in identifiers
            }
            if not facility_citations <= known:
                raise ValueError(f"{item.lot_id} has unknown facility evidence")
            if item.forward_event_ids != self.forward_traces[item.lot_id]:
                raise ValueError("forward trace summary mismatch")
            if item.backward_event_ids != self.backward_traces[item.lot_id]:
                raise ValueError("backward trace summary mismatch")
            if set(item.forward_event_ids) != set(item.event_ids) or set(
                item.backward_event_ids
            ) != set(item.event_ids):
                raise ValueError("forward/backward trace does not cover every event")
        return self


class CommunicationDraft(BaseModel):
    audience: Literal["facility", "food_safety_manager"]
    subject: str = Field(min_length=1)
    body: str = Field(min_length=1)
    target_ids: list[str] = Field(default_factory=list)
    evidence_by_target: dict[str, list[str]] = Field(default_factory=dict)
    evidence_ids: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def synthetic_context_is_visible(self) -> CommunicationDraft:
        if SYNTHETIC_LABEL not in self.body:
            raise ValueError("communication must retain the synthetic demo label")
        if set(self.evidence_by_target) != set(self.target_ids):
            raise ValueError("communication evidence must cover each target")
        if any(not identifiers for identifiers in self.evidence_by_target.values()):
            raise ValueError("communication target lacks evidence")
        cited = {
            evidence_id
            for identifiers in self.evidence_by_target.values()
            for evidence_id in identifiers
        }
        if self.target_ids and cited != set(self.evidence_ids):
            raise ValueError("communication evidence must equal target evidence union")
        return self


class ContainmentProposal(BaseModel):
    proposed_actions: list[ProposedAction]
    communication_drafts: list[CommunicationDraft]
    all_cited_evidence_ids: list[str]
    executed: Literal[False] = False

    @model_validator(mode="after")
    def citations_stay_in_quarantined_context(self) -> ContainmentProposal:
        allowed = set(self.all_cited_evidence_ids)
        cited = {
            evidence_id for draft in self.communication_drafts for evidence_id in draft.evidence_ids
        } | {evidence_id for action in self.proposed_actions for evidence_id in action.evidence_ids}
        if not cited <= allowed:
            raise ValueError("draft cites unknown evidence")
        for action in self.proposed_actions:
            if not action.evidence_by_target and action.target_ids:
                raise ValueError(f"action {action.action_id} lacks target-specific evidence")
        return self


def _digits(value: str) -> str:
    return "".join(character for character in value if character.isdigit())


def _normalize_spaces(value: str) -> str:
    return " ".join(value.split()).strip(" ,")


def _extract_official_products(description: str) -> list[OfficialProductScope]:
    packaged_pattern = re.compile(
        r"(?:^|\s+)(?P<number>(?:[1-9]|1\d|2[01]))\.\s+"
        r"(?P<description>.*?),\s*UPC\s+"
        r"(?P<upc>(?:\d[\s-]*){12})\.",
        flags=re.IGNORECASE | re.DOTALL,
    )
    products = [
        OfficialProductScope(
            item_number=int(match.group("number")),
            description=_normalize_spaces(match.group("description")),
            upc=_digits(match.group("upc")),
        )
        for match in packaged_pattern.finditer(description)
    ]
    for number in range(22, 29):
        start = re.search(rf"(?:^|\s){number}\.\s+", description)
        if start is None:
            continue
        if number < 28:
            end = re.search(rf"\s+{number + 1}\.\s+", description[start.end() :])
            end_index = start.end() + end.start() if end else len(description)
        else:
            refrigerated = description.find("Keep Refrigerated", start.end())
            end_index = refrigerated if refrigerated >= 0 else len(description)
        product_text = description[start.end() : end_index]
        product_text = re.split(
            r",\s*(?:MPS Egg Farms|Distributed by MPS Egg Farms)",
            product_text,
            maxsplit=1,
            flags=re.IGNORECASE,
        )[0]
        products.append(
            OfficialProductScope(
                item_number=number,
                description=_normalize_spaces(product_text.rstrip(".")),
            )
        )
    products.sort(key=lambda item: item.item_number)
    expected_numbers = list(range(1, 29))
    if [item.item_number for item in products] != expected_numbers:
        raise ValueError("recall product description does not contain complete items 1–28")
    if any(item.upc is not None and len(item.upc) != 12 for item in products):
        raise ValueError("recall contains a malformed UPC")
    return products


def investigate_recall(recall: RecallRecord) -> RecallIntelligence:
    """Extract the pinned product predicate without consulting synthetic operations."""
    payload = recall.payload
    description = str(payload.get("product_description", ""))
    code_info = str(payload.get("code_info", ""))
    plant_match = re.search(r"Codes\s+([^\s]+)\s+or\s+([^\s]+)", code_info, re.IGNORECASE)
    date_match = re.search(r"Julian Date between\s+(\d{1,3})\s+and\s+(\d{1,3})", code_info)
    gaps: list[str] = []
    if plant_match is None:
        gaps.append("Plant-code scope is absent from authoritative recall evidence")
    if date_match is None:
        gaps.append("Julian-date scope is absent from authoritative recall evidence")
    if gaps:
        raise ValueError("; ".join(gaps))

    geography = [
        item.strip()
        for item in str(payload.get("distribution_pattern", "")).split(",")
        if item.strip()
    ]
    official_products = _extract_official_products(description)
    predicate = RecallPredicate(
        product_terms=[item.description for item in official_products],
        upcs=list(dict.fromkeys(item.upc for item in official_products if item.upc is not None)),
        plant_codes=[plant_match.group(1), plant_match.group(2)],
        julian_start=int(date_match.group(1)),
        julian_end=int(date_match.group(2)),
        geography=geography,
        hazard=str(payload.get("reason_for_recall", "Unknown hazard")),
    )
    return RecallIntelligence(
        recall_number=recall.recall_number,
        predicate=predicate,
        official_products=official_products,
        classification=str(payload.get("classification", "Unknown")),
        status=str(payload.get("status", "Unknown")),
        source_provenance=recall.provenance,
        citations=[f"openfda:{recall.recall_number}"],
        evidence_gaps=[],
    )


def _derive_lot_classification(
    lot: LotMatch, product: CandidateProduct, predicate: RecallPredicate
) -> MatchClassification:
    within_window = predicate.julian_start <= lot.julian_date <= predicate.julian_end
    exact_plant = lot.plant_code in predicate.plant_codes
    ambiguous_plant = (
        lot.plant_code.endswith("?") and lot.plant_code.rstrip("?") in predicate.plant_codes
    )
    if not within_window or not product.score:
        return "rejected"
    if ambiguous_plant:
        return "ambiguous"
    if exact_plant and product.classification == "exact":
        return "exact"
    if exact_plant:
        return "probable"
    return "rejected"


def _compute_product_match(
    product: CandidateProduct, predicate: RecallPredicate
) -> tuple[float, Literal["exact", "probable", "rejected"]]:
    product_upc = _digits(product.upc or "")
    distances = [
        sum(left != right for left, right in zip(product_upc, _digits(upc), strict=True))
        for upc in predicate.upcs
        if product_upc and len(product_upc) == len(_digits(upc))
    ]
    distance = min(distances, default=99)
    if distance == 0:
        return 1.0, "exact"
    if distance == 1:
        return 0.8, "probable"
    return 0.0, "rejected"


def _match_rationale(
    lot: LotMatch, product: CandidateProduct, predicate: RecallPredicate
) -> tuple[list[str], str]:
    within_window = predicate.julian_start <= lot.julian_date <= predicate.julian_end
    exact_plant = lot.plant_code in predicate.plant_codes
    ambiguous_plant = (
        lot.plant_code.endswith("?") and lot.plant_code.rstrip("?") in predicate.plant_codes
    )
    fields: list[str] = []
    reasons: list[str] = []
    if product.classification == "exact":
        fields.append("upc")
        reasons.append("UPC exact")
    elif product.classification == "probable":
        reasons.append("UPC one-digit difference")
    else:
        reasons.append("UPC does not match")
    if exact_plant:
        fields.append("plant_code")
        reasons.append("plant code exact")
    elif ambiguous_plant:
        reasons.append(f"plant code {lot.plant_code} uncertain")
    else:
        reasons.append(f"plant code {lot.plant_code} outside recall scope")
    if within_window:
        fields.append("julian_date")
        reasons.append(
            f"Julian date {lot.julian_date} within {predicate.julian_start}–{predicate.julian_end}"
        )
    else:
        reasons.append(
            f"Julian date {lot.julian_date} outside {predicate.julian_start}–{predicate.julian_end}"
        )
    return fields, "; ".join(reasons)


def assess_product_lots(
    *,
    predicate: RecallPredicate,
    candidate_products: Sequence[CandidateProduct | dict],
    candidate_lots: Sequence[LotMatch | dict],
) -> ProductLotAssessment:
    """Validate and explain product/lot classifications from read-only observations."""
    products = [CandidateProduct.model_validate(item) for item in candidate_products]
    lots = [LotMatch.model_validate(item) for item in candidate_lots]
    if len({item.product_id for item in products}) != len(products):
        raise ValueError("duplicate candidate product identifiers")
    verified_products: list[CandidateProduct] = []
    for product in products:
        computed_score, computed_classification = _compute_product_match(product, predicate)
        if product.score != computed_score or product.classification != computed_classification:
            raise ValueError(
                f"product {product.product_id} caller label conflicts with computed UPC evidence"
            )
        verified_products.append(
            product.model_copy(
                update={"score": computed_score, "classification": computed_classification}
            )
        )
    product_by_id = {item.product_id: item for item in verified_products}
    decisions: list[MatchDecision] = []
    for lot in lots:
        product = product_by_id.get(lot.product_id)
        if product is None:
            raise ValueError(f"lot {lot.lot_id} references unknown candidate product")
        derived = _derive_lot_classification(lot, product, predicate)
        if lot.classification != derived:
            raise ValueError(f"lot {lot.lot_id} classification conflicts with predicate evidence")
        fields, rationale = _match_rationale(lot, product, predicate)
        decisions.append(
            MatchDecision(
                product_id=lot.product_id,
                lot_id=lot.lot_id,
                classification=derived,
                product_score=product.score,
                product_classification=product.classification,
                matched_fields=fields,
                rationale=rationale,
                requires_human_review=derived == "ambiguous",
                evidence_ids=[lot.product_id, lot.lot_id],
            )
        )
    return ProductLotAssessment(
        decisions=decisions,
        confirmed_lot_ids=[
            item.lot_id for item in decisions if item.classification in {"exact", "probable"}
        ],
        ambiguous_lot_ids=[item.lot_id for item in decisions if item.classification == "ambiguous"],
    )


def assess_traceability(
    *,
    lot_ids: Sequence[str],
    events: Sequence[TraceEvent | dict],
    inventory_positions: Sequence[InventoryPosition | dict],
    reconciliations: Sequence[Reconciliation | dict],
) -> TraceabilityAssessment:
    """Validate and summarize typed trace evidence inside delegated lot scope."""
    delegated_lots = list(dict.fromkeys(lot_ids))
    allowed_lots = set(delegated_lots)
    typed_events = [TraceEvent.model_validate(event) for event in events]
    typed_inventory = [InventoryPosition.model_validate(item) for item in inventory_positions]
    typed_reconciliations = [Reconciliation.model_validate(item) for item in reconciliations]
    event_ids = [event.event_id for event in typed_events]
    inventory_ids = [item.position_id for item in typed_inventory]
    if len(event_ids) != len(set(event_ids)):
        raise ValueError("duplicate trace event identifiers")
    if len(inventory_ids) != len(set(inventory_ids)):
        raise ValueError("duplicate inventory evidence identifiers")
    if set(event_ids) & set(inventory_ids):
        raise ValueError("event and inventory evidence identifiers collide")
    event_by_id = {event.event_id: event for event in typed_events}
    for event in typed_events:
        if event.lot_id not in allowed_lots:
            raise ValueError(f"event {event.event_id} is outside delegated lot scope")
        if event.parent_event_id:
            parent = event_by_id.get(event.parent_event_id)
            if parent is None:
                raise ValueError(
                    f"{event.lot_id}: event {event.event_id} missing parent {event.parent_event_id}"
                )
            if parent.lot_id != event.lot_id:
                raise ValueError(f"parent lot mismatch for {event.event_id}")
        elif event.event_type != "receiving" or not event.to_facility:
            raise ValueError(f"event {event.event_id} is not a valid receiving root")
    for position in typed_inventory:
        if position.lot_id not in allowed_lots:
            raise ValueError(f"inventory {position.position_id} is outside delegated lot scope")

    depths: dict[str, int] = {}
    visiting: set[str] = set()

    def depth(event_id: str) -> int:
        if event_id in visiting:
            raise ValueError(f"trace lineage cycle at {event_id}")
        if event_id in depths:
            return depths[event_id]
        visiting.add(event_id)
        parent_id = event_by_id[event_id].parent_event_id
        value = 0 if parent_id is None else depth(parent_id) + 1
        visiting.remove(event_id)
        depths[event_id] = value
        return value

    for event_id in event_by_id:
        depth(event_id)
    for event in typed_events:
        if event.parent_event_id:
            parent = event_by_id[event.parent_event_id]
            parent_facility = parent.to_facility or parent.from_facility
            child_facility = event.from_facility or event.to_facility
            if parent_facility != child_facility:
                raise ValueError(f"facility continuity mismatch for {event.event_id}")

    event_components = {
        "received": "receiving",
        "quarantined": "quarantine",
        "sold": "sale",
        "returned": "return",
        "disposed": "disposal",
    }

    def expected_reconciliation(lot_id: str) -> Reconciliation:
        lot_events = [event for event in typed_events if event.lot_id == lot_id]
        lot_inventory = [item for item in typed_inventory if item.lot_id == lot_id]
        quantities = {
            component: sum(event.quantity for event in lot_events if event.event_type == event_type)
            for component, event_type in event_components.items()
        }
        quantities["on_hand"] = sum(position.on_hand for position in lot_inventory)
        derived = Reconciliation.from_quantities(lot_id, **quantities)
        component_evidence = {
            component: [event.event_id for event in lot_events if event.event_type == event_type]
            for component, event_type in event_components.items()
        }
        component_evidence["on_hand"] = [position.position_id for position in lot_inventory]
        # Match TraceabilityService semantics exactly: returns remain a separate
        # disposition, and shipping/transfer movements do not change reconciliation.
        ordered_components = (
            "received",
            "on_hand",
            "quarantined",
            "sold",
            "returned",
            "disposed",
        )
        evidence_ids = list(
            dict.fromkeys(
                evidence_id
                for component in ordered_components
                for evidence_id in component_evidence[component]
            )
        )
        component_evidence["unaccounted"] = evidence_ids
        return Reconciliation.model_validate(
            {
                **derived.model_dump(mode="python"),
                "evidence_ids": evidence_ids,
                "component_evidence": component_evidence,
                "verified": True,
            }
        )

    reconciliation_by_lot: dict[str, Reconciliation] = {}
    for reconciliation in typed_reconciliations:
        if reconciliation.lot_id not in allowed_lots:
            raise ValueError(
                f"reconciliation {reconciliation.lot_id} is outside delegated lot scope"
            )
        if reconciliation.lot_id in reconciliation_by_lot:
            raise ValueError(f"duplicate reconciliation for {reconciliation.lot_id}")
        expected = expected_reconciliation(reconciliation.lot_id)
        supplied_lot_evidence = {
            evidence_id
            for identifiers in reconciliation.component_evidence.values()
            for evidence_id in identifiers
        } | set(reconciliation.evidence_ids)
        unknown_evidence = supplied_lot_evidence - (set(event_ids) | set(inventory_ids))
        if unknown_evidence:
            raise ValueError(
                f"{reconciliation.lot_id} has unknown reconciliation evidence: "
                f"{sorted(unknown_evidence)}"
            )
        quantity_mismatches = [
            component
            for component in (
                "received",
                "on_hand",
                "quarantined",
                "sold",
                "returned",
                "disposed",
                "unaccounted",
            )
            if getattr(reconciliation, component) != getattr(expected, component)
        ]
        if quantity_mismatches:
            raise ValueError(
                f"{reconciliation.lot_id} {', '.join(quantity_mismatches)} conflicts with typed evidence"
            )
        for component in (
            "received",
            "on_hand",
            "quarantined",
            "sold",
            "returned",
            "disposed",
            "unaccounted",
        ):
            supplied_ids = reconciliation.component_evidence.get(component)
            expected_ids = expected.component_evidence[component]
            if supplied_ids != expected_ids:
                raise ValueError(
                    f"{reconciliation.lot_id} {component} evidence must be complete and exact"
                )
        if reconciliation.evidence_ids != expected.evidence_ids:
            raise ValueError(
                f"{reconciliation.lot_id} reconciliation evidence must be complete and exact"
            )
        if not reconciliation.verified:
            raise ValueError(f"{reconciliation.lot_id} reconciliation evidence is unverified")
        reconciliation_by_lot[reconciliation.lot_id] = reconciliation

    gaps: list[str] = []
    coverage: list[LotTraceCoverage] = []
    all_evidence: list[str] = []
    all_facilities: set[str] = set()
    forward_traces: dict[str, list[str]] = {}
    backward_traces: dict[str, list[str]] = {}
    for lot_id in delegated_lots:
        lot_events = [event for event in typed_events if event.lot_id == lot_id]
        lot_inventory = [item for item in typed_inventory if item.lot_id == lot_id]
        lot_event_ids = [event.event_id for event in lot_events]
        children: dict[str | None, list[TraceEvent]] = {}
        for event in lot_events:
            children.setdefault(event.parent_event_id, []).append(event)
        for group in children.values():
            group.sort(key=lambda item: (item.occurred_at, item.event_id))
        forward: list[str] = []

        def walk(event: TraceEvent) -> None:
            forward.append(event.event_id)
            for child in children.get(event.event_id, []):
                walk(child)

        for root in children.get(None, []):
            walk(root)
        if set(forward) != set(lot_event_ids):
            raise ValueError(f"{lot_id} events are not reachable from a receiving root")
        backward = [
            event.event_id
            for event in sorted(
                lot_events,
                key=lambda item: (-depth(item.event_id), item.occurred_at, item.event_id),
            )
        ]
        forward_traces[lot_id] = forward
        backward_traces[lot_id] = backward
        facility_evidence: dict[str, list[str]] = {}
        for event in lot_events:
            for facility in (event.from_facility, event.to_facility):
                if facility:
                    facility_evidence.setdefault(facility, []).append(event.event_id)
        for position in lot_inventory:
            facility_evidence.setdefault(position.facility_id, []).append(position.position_id)
        facility_evidence = {
            facility: list(dict.fromkeys(identifiers))
            for facility, identifiers in sorted(facility_evidence.items())
        }
        facilities = list(facility_evidence)
        all_facilities.update(facilities)
        lot_gaps: list[str] = []
        if not any(event.event_type == "receiving" for event in lot_events):
            lot_gaps.append(f"{lot_id}: missing receiving event")
        reconciliation = reconciliation_by_lot.get(lot_id)
        if reconciliation is None:
            lot_gaps.append(f"{lot_id}: missing reconciliation")
            reconciliation_evidence: list[str] = []
            unaccounted = 0
        else:
            reconciliation_evidence = reconciliation.evidence_ids
            unaccounted = reconciliation.unaccounted
            if unaccounted:
                lot_gaps.append(f"{lot_id}: {unaccounted} unaccounted units")
            if not reconciliation.verified:
                lot_gaps.append(f"{lot_id}: reconciliation evidence is unverified")
        gaps.extend(lot_gaps)
        coverage.append(
            LotTraceCoverage(
                lot_id=lot_id,
                facility_ids=facilities,
                event_ids=lot_event_ids,
                forward_event_ids=forward,
                backward_event_ids=backward,
                inventory_evidence_ids=[item.position_id for item in lot_inventory],
                reconciliation_evidence_ids=reconciliation_evidence,
                facility_evidence=facility_evidence,
                unaccounted_units=unaccounted,
                complete=not lot_gaps,
            )
        )
        all_evidence.extend(lot_event_ids)
        all_evidence.extend(item.position_id for item in lot_inventory)
        all_evidence.extend(reconciliation_evidence)
    return TraceabilityAssessment(
        lot_ids=delegated_lots,
        affected_facilities=sorted(all_facilities),
        coverage=coverage,
        forward_traces=forward_traces,
        backward_traces=backward_traces,
        reconciliations=[
            reconciliation_by_lot[lot_id]
            for lot_id in delegated_lots
            if lot_id in reconciliation_by_lot
        ],
        evidence_ids=list(dict.fromkeys(all_evidence)),
        evidence_gaps=list(dict.fromkeys(gaps)),
    )


def draft_containment(
    *,
    case_id: str,
    expected_case_version: int,
    matching: ProductLotAssessment,
    traceability: TraceabilityAssessment,
) -> ContainmentProposal:
    """Draft evidence-backed actions; execution remains an approval-gated graph concern."""
    if not traceability.evidence_ids:
        raise ValueError("containment requires trace evidence")
    supported_facilities = {
        facility for coverage in traceability.coverage for facility in coverage.facility_evidence
    }
    unsupported_facilities = set(traceability.affected_facilities) - supported_facilities
    if unsupported_facilities:
        facility = sorted(unsupported_facilities)[0]
        raise ValueError(f"{facility} lacks claim-specific evidence support")
    traceability = TraceabilityAssessment.model_validate(traceability.model_dump(mode="python"))
    matching = ProductLotAssessment.model_validate(matching.model_dump(mode="python"))
    coverage_by_lot = {item.lot_id: item for item in traceability.coverage}

    def ordered_union(evidence_by_target: dict[str, list[str]]) -> list[str]:
        return list(
            dict.fromkeys(
                evidence_id
                for identifiers in evidence_by_target.values()
                for evidence_id in identifiers
            )
        )

    lot_evidence: dict[str, list[str]] = {}
    for lot_id in [*matching.confirmed_lot_ids, *matching.ambiguous_lot_ids]:
        lot_coverage = coverage_by_lot.get(lot_id)
        if lot_coverage is None:
            raise ValueError(f"{lot_id} lacks traceability support")
        identifiers = list(
            dict.fromkeys(
                [
                    *lot_coverage.event_ids,
                    *lot_coverage.inventory_evidence_ids,
                    *lot_coverage.reconciliation_evidence_ids,
                ]
            )
        )
        if not lot_coverage.reconciliation_evidence_ids or not identifiers:
            raise ValueError(f"{lot_id} lacks reconciliation evidence support")
        lot_evidence[lot_id] = identifiers
    facility_evidence: dict[str, list[str]] = {}
    for facility in traceability.affected_facilities:
        identifiers = list(
            dict.fromkeys(
                evidence_id
                for coverage in traceability.coverage
                if facility in coverage.facility_evidence
                for evidence_id in (
                    *coverage.facility_evidence[facility],
                    *coverage.reconciliation_evidence_ids,
                )
            )
        )
        if not identifiers:
            raise ValueError(f"{facility} lacks claim-specific evidence support")
        facility_evidence[facility] = identifiers

    proposed_actions: list[ProposedAction] = []
    if matching.confirmed_lot_ids:
        action_id = f"{case_id}-hold-v{expected_case_version}"
        evidence_by_lot = {lot_id: lot_evidence[lot_id] for lot_id in matching.confirmed_lot_ids}
        proposed_actions.append(
            ProposedAction(
                action_id=action_id,
                action_type="apply_inventory_hold",
                case_id=case_id,
                target_ids=matching.confirmed_lot_ids,
                rationale="Hold only confirmed exact/probable lots pending human authorization.",
                evidence_ids=ordered_union(evidence_by_lot),
                evidence_by_target=evidence_by_lot,
                expected_case_version=expected_case_version,
            )
        )
    if traceability.affected_facilities:
        action_id = f"{case_id}-facility-tasks-v{expected_case_version}"
        proposed_actions.append(
            ProposedAction(
                action_id=action_id,
                action_type="create_facility_tasks",
                case_id=case_id,
                target_ids=traceability.affected_facilities,
                rationale="Request inventory verification and acknowledgement at traced facilities.",
                evidence_ids=ordered_union(facility_evidence),
                evidence_by_target=facility_evidence,
                expected_case_version=expected_case_version,
            )
        )

    reviewed_lots = [*matching.confirmed_lot_ids, *matching.ambiguous_lot_ids]
    review_by_lot = {lot_id: lot_evidence[lot_id] for lot_id in reviewed_lots}
    drafts = [
        CommunicationDraft(
            audience="facility",
            subject="Recall inventory verification request",
            body=(
                f"{SYNTHETIC_LABEL}. Verify traced inventory for case {case_id}; do not act "
                "outside the approved task and report discrepancies."
            ),
            target_ids=traceability.affected_facilities,
            evidence_by_target=facility_evidence,
            evidence_ids=ordered_union(facility_evidence),
        ),
        CommunicationDraft(
            audience="food_safety_manager",
            subject="Containment proposal ready for human review",
            body=(
                f"{SYNTHETIC_LABEL}. Review confirmed holds, ambiguous matches, facility coverage, "
                "and reconciliation gaps before authorizing any simulated write."
            ),
            target_ids=reviewed_lots,
            evidence_by_target=review_by_lot,
            evidence_ids=ordered_union(review_by_lot),
        ),
    ]
    cited = list(
        dict.fromkeys(
            evidence_id
            for identifiers in (
                *(action.evidence_ids for action in proposed_actions),
                *(draft.evidence_ids for draft in drafts),
            )
            for evidence_id in identifiers
        )
    )
    return ContainmentProposal(
        proposed_actions=proposed_actions,
        communication_drafts=drafts,
        all_cited_evidence_ids=cited,
        executed=False,
    )
