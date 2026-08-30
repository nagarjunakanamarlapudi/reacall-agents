"""Read-only matching, traceability, and reconciliation against the synthetic twin."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from recallops.config import get_settings
from recallops.data.loaders import load_demo_dataset
from recallops.models import RecallPredicate, Reconciliation


def _upc(value: str | None) -> str:
    return "".join(character for character in value or "" if character.isdigit())


class TraceabilityService:
    def __init__(
        self,
        dataset: dict[str, Any] | None = None,
        *,
        data_dir: Path | None = None,
        source_mode: Literal["snapshot", "live"] | None = None,
    ) -> None:
        settings = get_settings()
        self.data_dir = Path(data_dir) if data_dir is not None else settings.data_dir
        self.source_mode = source_mode or settings.source_mode
        if self.source_mode not in {"snapshot", "live"}:
            raise ValueError("source_mode must be 'snapshot' or 'live'")
        self.dataset = dataset if dataset is not None else load_demo_dataset(self.data_dir)

    def list_products(
        self, *, query: str | None = None, offset: int = 0, limit: int = 20
    ) -> dict[str, Any]:
        """Return a stable catalog page for scaled-table and filtering demos."""
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError("offset must be a nonnegative integer")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("limit must be an integer from 1 through 100")
        needle = (query or "").strip().casefold()
        rows = sorted(self.dataset["products"], key=lambda row: row["product_id"])
        if needle:
            rows = [
                row
                for row in rows
                if needle in f"{row['product_id']} {row['name']} {row.get('upc', '')}".casefold()
            ]
        total = len(rows)
        return {
            "items": rows[offset : offset + limit],
            "offset": offset,
            "limit": limit,
            "total": total,
            "has_more": offset + limit < total,
        }

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
        events = [event for event in self.dataset["events"] if event["lot_id"] == lot_id]
        by_id = {event["event_id"]: event for event in events}
        depths: dict[str, int] = {}

        def parent_depth(event: dict[str, Any]) -> int:
            event_id = event["event_id"]
            if event_id not in depths:
                parent_id = event.get("parent_event_id")
                depths[event_id] = 0 if parent_id is None else parent_depth(by_id[parent_id]) + 1
            return depths[event_id]

        # Descending ancestry depth guarantees every child precedes its parent and is
        # derived by following parent_event_id rather than reversing storage order.
        return sorted(events, key=lambda event: (-parent_depth(event), event["occurred_at"]))

    def get_inventory(self, lot_id: str | None = None) -> list[dict[str, Any]]:
        inventory = self.dataset["inventory_positions"]
        return [row for row in inventory if row["lot_id"] == lot_id] if lot_id else inventory

    def get_sales(self, lot_id: str) -> list[dict[str, Any]]:
        return [event for event in self.trace_forward(lot_id) if event["event_type"] == "sale"]

    def reconcile_units(self, lot_id: str) -> Reconciliation:
        events = self.trace_forward(lot_id)

        def quantity(event_type: str) -> int:
            return sum(event["quantity"] for event in events if event["event_type"] == event_type)

        inventory = self.get_inventory(lot_id)
        reconciliation = Reconciliation.from_quantities(
            lot_id,
            received=quantity("receiving"),
            on_hand=sum(item["on_hand"] for item in inventory),
            quarantined=quantity("quarantine"),
            sold=quantity("sale"),
            returned=quantity("return"),
            disposed=quantity("disposal"),
        )
        component_evidence = {
            "received": [item["event_id"] for item in events if item["event_type"] == "receiving"],
            "on_hand": [item["position_id"] for item in inventory],
            "quarantined": [
                item["event_id"] for item in events if item["event_type"] == "quarantine"
            ],
            "sold": [item["event_id"] for item in events if item["event_type"] == "sale"],
            "returned": [item["event_id"] for item in events if item["event_type"] == "return"],
            "disposed": [item["event_id"] for item in events if item["event_type"] == "disposal"],
        }
        evidence_ids = list(
            dict.fromkeys(
                item for identifiers in component_evidence.values() for item in identifiers
            )
        )
        component_evidence["unaccounted"] = evidence_ids
        return Reconciliation.model_validate(
            {
                **reconciliation.model_dump(),
                "component_evidence": component_evidence,
                "evidence_ids": evidence_ids,
                "verified": True,
            }
        )
