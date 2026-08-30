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

    def event(
        event_id: str,
        lot_id: str,
        event_type: str,
        quantity: int,
        *,
        parent: str | None = None,
        source: str | None = None,
        destination: str | None = None,
    ) -> dict[str, Any]:
        return {
            "event_id": event_id,
            "lot_id": lot_id,
            "event_type": event_type,
            "quantity": quantity,
            "from_facility": source,
            "to_facility": destination,
            "parent_event_id": parent,
        }

    # Explicit branches preserve facility continuity. STORE outcomes always descend from
    # a shipment/transfer that actually arrived at that store.
    events = [
        event("EV-001", "LOT-EXACT-170", "receiving", 1200, destination="DC-NORTH"),
        event(
            "EV-002",
            "LOT-EXACT-170",
            "shipping",
            600,
            parent="EV-001",
            source="DC-NORTH",
            destination="STORE-01",
        ),
        event(
            "EV-003",
            "LOT-EXACT-170",
            "shipping",
            300,
            parent="EV-001",
            source="DC-NORTH",
            destination="STORE-02",
        ),
        event(
            "EV-008",
            "LOT-EXACT-170",
            "quarantine",
            200,
            parent="EV-001",
            source="DC-NORTH",
            destination="DC-NORTH",
        ),
        event(
            "EV-S-LOT-EXACT-170",
            "LOT-EXACT-170",
            "sale",
            550,
            parent="EV-002",
            source="STORE-01",
        ),
        event(
            "EV-R-LOT-EXACT-170",
            "LOT-EXACT-170",
            "return",
            20,
            parent="EV-002",
            source="STORE-01",
            destination="STORE-01",
        ),
        event(
            "EV-D-LOT-EXACT-170",
            "LOT-EXACT-170",
            "disposal",
            80,
            parent="EV-002",
            source="STORE-01",
        ),
        event("EV-004", "LOT-PROBABLE-160", "receiving", 900, destination="DC-SOUTH"),
        event(
            "EV-005",
            "LOT-PROBABLE-160",
            "transfer",
            780,
            parent="EV-004",
            source="DC-SOUTH",
            destination="STORE-03",
        ),
        event(
            "EV-S-LOT-PROBABLE-160",
            "LOT-PROBABLE-160",
            "sale",
            540,
            parent="EV-005",
            source="STORE-03",
        ),
        event(
            "EV-R-LOT-PROBABLE-160",
            "LOT-PROBABLE-160",
            "return",
            10,
            parent="EV-005",
            source="STORE-03",
            destination="STORE-03",
        ),
        event(
            "EV-Q-LOT-PROBABLE-160",
            "LOT-PROBABLE-160",
            "quarantine",
            160,
            parent="EV-005",
            source="STORE-03",
            destination="STORE-03",
        ),
        event(
            "EV-D-LOT-PROBABLE-160",
            "LOT-PROBABLE-160",
            "disposal",
            70,
            parent="EV-005",
            source="STORE-03",
        ),
        event(
            "EV-R-LOT-AMBIG-175",
            "LOT-AMBIG-175",
            "receiving",
            500,
            destination="DC-NORTH",
        ),
        event(
            "EV-006",
            "LOT-AMBIG-175",
            "shipping",
            450,
            parent="EV-R-LOT-AMBIG-175",
            source="DC-NORTH",
            destination="STORE-08",
        ),
        event(
            "EV-007",
            "LOT-AMBIG-175",
            "sale",
            400,
            parent="EV-006",
            source="STORE-08",
        ),
    ]
    for lot_id, received, sold in (
        ("LOT-REJECT-190", 450, 350),
        ("LOT-CONTROL-170", 300, 200),
        ("LOT-NEAR-150", 360, 300),
    ):
        receiving_id = f"EV-R-{lot_id}"
        movement_id = f"EV-M-{lot_id}"
        events.extend(
            [
                event(receiving_id, lot_id, "receiving", received, destination="DC-NORTH"),
                event(
                    movement_id,
                    lot_id,
                    "shipping",
                    sold,
                    parent=receiving_id,
                    source="DC-NORTH",
                    destination="STORE-01",
                ),
                event(
                    f"EV-S-{lot_id}",
                    lot_id,
                    "sale",
                    sold,
                    parent=movement_id,
                    source="STORE-01",
                ),
            ]
        )
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
                "facility_id": ("DC-SOUTH" if lot["lot_id"] == "LOT-PROBABLE-160" else "DC-NORTH"),
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
    dataset_text = _canonical_json(dataset) + "\n"
    checksum = hashlib.sha256(dataset_text.encode()).hexdigest()
    manifest = {
        "dataset_id": dataset["dataset_id"],
        "seed": seed,
        "sha256": checksum,
        "files": ["dataset.json"],
        "checksums": {"dataset.json": checksum},
        "origin": ORIGIN,
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "dataset.json").write_text(dataset_text)
    (output_dir / "manifest.json").write_text(_canonical_json(manifest) + "\n")
    return {**dataset, "manifest": manifest}
