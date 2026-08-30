"""Deterministically generate the clearly labelled Northstar digital twin."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from recallops.paths import DEMO_DATA_DIR

SEED = 20260830
ORIGIN = "SYNTHETIC_RETAILER_DIGITAL_TWIN"


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode()).hexdigest()


def _dataset(seed: int) -> dict[str, Any]:
    """Return fixed instructional data; the explicit seed makes the contract auditable."""
    products = [
        {
            "product_id": "P-EXACT",
            "name": "Northstar Grade A Large Eggs 12 ct",
            "upc": "011110609038",
        },
        {
            "product_id": "P-PROBABLE",
            "name": "Northstar Farm Fresh Large Eggs 18 ct",
            "upc": "011110609039",
        },
        {
            "product_id": "P-NEAR",
            "name": "Northstar Cage Free Large Eggs 12 ct",
            "upc": "011110609038",
        },
        {
            "product_id": "P-CONTROL",
            "name": "Northstar Organic Brown Eggs 12 ct",
            "upc": "088888888881",
        },
    ]
    lots = [
        {
            "lot_id": "LOT-EXACT-170",
            "product_id": "P-EXACT",
            "plant_code": "P-1950",
            "julian_date": 170,
            "classification": "exact",
            "received_units": 1200,
            "on_hand": 300,
            "quarantined": 200,
            "sold": 550,
            "returned": 20,
            "disposed": 80,
            "unaccounted": 50,
        },
        {
            "lot_id": "LOT-PROBABLE-160",
            "product_id": "P-PROBABLE",
            "plant_code": "0840962",
            "julian_date": 160,
            "classification": "probable",
            "received_units": 900,
            "on_hand": 120,
            "quarantined": 160,
            "sold": 540,
            "returned": 10,
            "disposed": 70,
            "unaccounted": 0,
        },
        {
            "lot_id": "LOT-AMBIG-175",
            "product_id": "P-NEAR",
            "plant_code": "P-1950?",
            "julian_date": 175,
            "classification": "ambiguous",
            "received_units": 500,
            "on_hand": 50,
            "quarantined": 0,
            "sold": 400,
            "returned": 0,
            "disposed": 0,
            "unaccounted": 50,
        },
        {
            "lot_id": "LOT-REJECT-190",
            "product_id": "P-EXACT",
            "plant_code": "P-1950",
            "julian_date": 190,
            "classification": "rejected",
            "received_units": 450,
            "on_hand": 100,
            "quarantined": 0,
            "sold": 350,
            "returned": 0,
            "disposed": 0,
            "unaccounted": 0,
        },
        {
            "lot_id": "LOT-CONTROL-170",
            "product_id": "P-CONTROL",
            "plant_code": "P-9999",
            "julian_date": 170,
            "classification": "rejected",
            "received_units": 300,
            "on_hand": 100,
            "quarantined": 0,
            "sold": 200,
            "returned": 0,
            "disposed": 0,
            "unaccounted": 0,
        },
        {
            "lot_id": "LOT-NEAR-150",
            "product_id": "P-NEAR",
            "plant_code": "0840962",
            "julian_date": 150,
            "classification": "rejected",
            "received_units": 360,
            "on_hand": 60,
            "quarantined": 0,
            "sold": 300,
            "returned": 0,
            "disposed": 0,
            "unaccounted": 0,
        },
    ]
    facilities = [
        {"facility_id": "DC-NORTH", "kind": "distribution_center", "origin": ORIGIN},
        {"facility_id": "DC-SOUTH", "kind": "distribution_center", "origin": ORIGIN},
        *(
            {"facility_id": f"STORE-{number:02}", "kind": "store", "origin": ORIGIN}
            for number in range(1, 9)
        ),
    ]
    events = [
        {
            "event_id": "EV-001",
            "lot_id": "LOT-EXACT-170",
            "event_type": "receiving",
            "quantity": 1200,
            "to_facility": "DC-NORTH",
        },
        {
            "event_id": "EV-002",
            "lot_id": "LOT-EXACT-170",
            "event_type": "shipping",
            "quantity": 600,
            "from_facility": "DC-NORTH",
            "to_facility": "STORE-01",
        },
        {
            "event_id": "EV-003",
            "lot_id": "LOT-EXACT-170",
            "event_type": "shipping",
            "quantity": 300,
            "from_facility": "DC-NORTH",
            "to_facility": "STORE-02",
        },
        {
            "event_id": "EV-004",
            "lot_id": "LOT-PROBABLE-160",
            "event_type": "receiving",
            "quantity": 900,
            "to_facility": "DC-SOUTH",
        },
        {
            "event_id": "EV-005",
            "lot_id": "LOT-PROBABLE-160",
            "event_type": "transfer",
            "quantity": 450,
            "from_facility": "DC-SOUTH",
            "to_facility": "STORE-03",
        },
        {
            "event_id": "EV-006",
            "lot_id": "LOT-AMBIG-175",
            "event_type": "shipping",
            "quantity": 450,
            "from_facility": "DC-NORTH",
            "to_facility": "STORE-08",
        },
        {
            "event_id": "EV-007",
            "lot_id": "LOT-AMBIG-175",
            "event_type": "sale",
            "quantity": 400,
            "from_facility": "STORE-08",
        },
        {
            "event_id": "EV-008",
            "lot_id": "LOT-EXACT-170",
            "event_type": "quarantine",
            "quantity": 200,
            "to_facility": "DC-NORTH",
        },
    ]
    # Reconciliation derives from these immutable event and inventory records, not lot aggregates.
    for lot in lots:
        if not any(
            event["lot_id"] == lot["lot_id"] and event["event_type"] == "receiving"
            for event in events
        ):
            events.append(
                {
                    "event_id": f"EV-R-{lot['lot_id']}",
                    "lot_id": lot["lot_id"],
                    "event_type": "receiving",
                    "quantity": lot["received_units"],
                    "to_facility": "DC-NORTH",
                }
            )
        for event_type, field in (
            ("sale", "sold"),
            ("return", "returned"),
            ("quarantine", "quarantined"),
            ("disposal", "disposed"),
        ):
            if lot[field] and not any(
                event["lot_id"] == lot["lot_id"] and event["event_type"] == event_type
                for event in events
            ):
                events.append(
                    {
                        "event_id": f"EV-{event_type[:1].upper()}-{lot['lot_id']}",
                        "lot_id": lot["lot_id"],
                        "event_type": event_type,
                        "quantity": lot[field],
                        "from_facility": "STORE-01",
                    }
                )
    last_event: dict[str, str] = {}
    for event in events:
        event["parent_event_id"] = last_event.get(event["lot_id"])
        last_event[event["lot_id"]] = event["event_id"]
    return {
        "dataset_id": "northstar-demo-20260830",
        "seed": seed,
        "source_label": "SYNTHETIC — ACADEMIC DEMO",
        "origin": ORIGIN,
        "retailer": "Northstar Grocers",
        "products": [{**product, "origin": ORIGIN} for product in products],
        "lots": [{**lot, "origin": ORIGIN} for lot in lots],
        "facilities": facilities,
        "events": [
            {**event, "origin": ORIGIN, "occurred_at": f"2026-08-01T12:{index:02}:00Z"}
            for index, event in enumerate(events)
        ],
        "inventory_positions": [
            {
                "position_id": f"INV-{lot['lot_id']}",
                "lot_id": lot["lot_id"],
                "facility_id": "DC-NORTH",
                "on_hand": lot["on_hand"],
                "origin": ORIGIN,
            }
            for lot in lots
        ],
        "supplier_shipments": [
            {"shipment_id": "SHIP-001", "lot_id": "LOT-EXACT-170", "origin": ORIGIN}
        ],
        "facility_acknowledgements": [
            {
                "facility_id": facility["facility_id"],
                "acknowledged": facility["facility_id"] != "STORE-08",
                "origin": ORIGIN,
            }
            for facility in facilities
        ],
        "cases": [],
        "tasks": [],
        "audit_receipts": [],
    }


def generate_demo_dataset(*, output_dir: Path = DEMO_DATA_DIR, seed: int = SEED) -> dict[str, Any]:
    if seed != SEED:
        raise ValueError(f"only the pinned deterministic seed {SEED} is supported")
    dataset = _dataset(seed)
    manifest = {
        "dataset_id": dataset["dataset_id"],
        "seed": seed,
        "sha256": _sha256(dataset),
        "files": ["dataset.json"],
        "origin": ORIGIN,
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "dataset.json").write_text(_canonical_json(dataset) + "\n")
    (output_dir / "manifest.json").write_text(_canonical_json(manifest) + "\n")
    return {**dataset, "manifest": manifest}
