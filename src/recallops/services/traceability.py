"""Read-only matching, traceability, and reconciliation against the synthetic twin."""

from __future__ import annotations

from typing import Any

from recallops.data.loaders import load_demo_dataset
from recallops.models import RecallPredicate, Reconciliation


def _upc(value: str | None) -> str:
    return "".join(character for character in value or "" if character.isdigit())


class TraceabilityService:
    def __init__(self, dataset: dict[str, Any] | None = None) -> None:
        self.dataset = dataset or load_demo_dataset()

    def find_candidate_products(self, predicate: RecallPredicate) -> list[dict[str, Any]]:
        matches: list[dict[str, Any]] = []
        for product in self.dataset["products"]:
            product_upc = _upc(product.get("upc"))
            distances = [
                sum(left != right for left, right in zip(product_upc, _upc(upc), strict=False))
                for upc in predicate.upcs
                if len(_upc(upc)) == len(product_upc)
            ]
            distance = min(distances, default=99)
            score = 1.0 if distance == 0 else 0.8 if distance == 1 else 0.0
            classification = "exact" if score == 1.0 else "probable" if score else "rejected"
            matches.append({**product, "score": score, "classification": classification})
        return sorted(matches, key=lambda item: item["score"], reverse=True)

    def match_lots(self, predicate: RecallPredicate) -> list[dict[str, Any]]:
        products = {item["product_id"]: item for item in self.find_candidate_products(predicate)}
        matched: list[dict[str, Any]] = []
        for lot in self.dataset["lots"]:
            product = products[lot["product_id"]]
            within_window = predicate.julian_start <= lot["julian_date"] <= predicate.julian_end
            exact_plant = lot["plant_code"] in predicate.plant_codes
            ambiguous_plant = lot["plant_code"].rstrip("?") in predicate.plant_codes and lot[
                "plant_code"
            ].endswith("?")
            classification = "rejected"
            if within_window and product["score"]:
                if ambiguous_plant:
                    classification = "ambiguous"
                elif exact_plant and product["score"] == 1.0:
                    classification = "exact"
                elif exact_plant:
                    classification = "probable"
            matched.append({**lot, "classification": classification})
        return matched

    def trace_forward(self, lot_id: str) -> list[dict[str, Any]]:
        events = [event for event in self.dataset["events"] if event["lot_id"] == lot_id]
        by_parent: dict[str | None, list[dict[str, Any]]] = {}
        for event in events:
            by_parent.setdefault(event.get("parent_event_id"), []).append(event)
        ordered: list[dict[str, Any]] = []

        def walk(event: dict[str, Any]) -> None:
            ordered.append(event)
            for child in by_parent.get(event["event_id"], []):
                walk(child)

        for root in by_parent.get(None, []):
            walk(root)
        return ordered

    def trace_backward(self, lot_id: str) -> list[dict[str, Any]]:
        return list(reversed(self.trace_forward(lot_id)))

    def get_inventory(self, lot_id: str | None = None) -> list[dict[str, Any]]:
        inventory = self.dataset["inventory_positions"]
        return [row for row in inventory if row["lot_id"] == lot_id] if lot_id else inventory

    def get_sales(self, lot_id: str) -> list[dict[str, Any]]:
        return [event for event in self.trace_forward(lot_id) if event["event_type"] == "sale"]

    def reconcile_units(self, lot_id: str) -> Reconciliation:
        events = self.trace_forward(lot_id)

        def quantity(event_type: str) -> int:
            return sum(event["quantity"] for event in events if event["event_type"] == event_type)

        return Reconciliation.from_quantities(
            lot_id,
            received=quantity("receiving"),
            on_hand=sum(item["on_hand"] for item in self.get_inventory(lot_id)),
            quarantined=quantity("quarantine"),
            sold=quantity("sale"),
            returned=quantity("return"),
            disposed=quantity("disposal"),
        )
