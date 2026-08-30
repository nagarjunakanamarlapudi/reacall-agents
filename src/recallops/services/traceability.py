"""Read-only matching, traceability, and reconciliation against the synthetic twin."""

from __future__ import annotations

from typing import Any

from recallops.data.loaders import load_demo_dataset
from recallops.models import RecallPredicate, Reconciliation


class TraceabilityService:
    def __init__(self, dataset: dict[str, Any] | None = None) -> None:
        self.dataset = dataset or load_demo_dataset()

    def find_candidate_products(self, predicate: RecallPredicate) -> list[dict[str, Any]]:
        matches: list[dict[str, Any]] = []
        terms = " ".join(predicate.product_terms).casefold()
        for product in self.dataset["products"]:
            upc_match = product.get("upc") in predicate.upcs
            text_match = "egg" in product["name"].casefold() and "egg" in terms
            score = 1.0 if upc_match else 0.7 if text_match else 0.0
            classification = "exact" if score == 1.0 else "probable" if score >= 0.7 else "rejected"
            matches.append({**product, "score": score, "classification": classification})
        return sorted(matches, key=lambda item: item["score"], reverse=True)

    def match_lots(self, predicate: RecallPredicate) -> list[dict[str, Any]]:
        matched: list[dict[str, Any]] = []
        for lot in self.dataset["lots"]:
            within_window = predicate.julian_start <= lot["julian_date"] <= predicate.julian_end
            exact_plant = lot["plant_code"] in predicate.plant_codes
            ambiguous_plant = lot["plant_code"].rstrip("?") in predicate.plant_codes and lot[
                "plant_code"
            ].endswith("?")
            classification = (
                lot["classification"]
                if within_window and (exact_plant or ambiguous_plant)
                else "rejected"
            )
            matched.append({**lot, "classification": classification})
        return matched

    def trace_forward(self, lot_id: str) -> list[dict[str, Any]]:
        return [event for event in self.dataset["events"] if event["lot_id"] == lot_id]

    def trace_backward(self, lot_id: str) -> list[dict[str, Any]]:
        return sorted(self.trace_forward(lot_id), key=lambda event: event["event_id"])

    def get_inventory(self, lot_id: str | None = None) -> list[dict[str, Any]]:
        inventory = self.dataset["inventory_positions"]
        return [row for row in inventory if row["lot_id"] == lot_id] if lot_id else inventory

    def get_sales(self, lot_id: str) -> list[dict[str, Any]]:
        return [event for event in self.trace_forward(lot_id) if event["event_type"] == "sale"]

    def reconcile_units(self, lot_id: str) -> Reconciliation:
        lot = next(item for item in self.dataset["lots"] if item["lot_id"] == lot_id)
        return Reconciliation.from_quantities(
            lot_id,
            received=lot["received_units"],
            on_hand=lot["on_hand"],
            quarantined=lot["quarantined"],
            sold=lot["sold"],
            returned=lot["returned"],
            disposed=lot["disposed"],
        )
