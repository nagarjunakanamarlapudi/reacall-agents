"""Deterministically generate the clearly labelled Northstar digital twin."""

from __future__ import annotations

import hashlib
import json
import random
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from recallops.paths import DEMO_DATA_DIR

SEED = 20260830
ORIGIN = "SYNTHETIC_RETAILER_DIGITAL_TWIN"
SOURCE_LABEL = "SYNTHETIC — ACADEMIC DEMO"
SCHEMA_NAME = "recallops.synthetic-retailer-digital-twin"
SCHEMA_VERSION = "1.1.0"
GENERATED_AT = "2026-08-30T00:00:00Z"
EXPECTED_RECORD_COUNTS = {
    "products": 48,
    "lots": 144,
    "facilities": 18,
    "events": 577,
    "inventory_positions": 216,
    "supplier_shipments": 144,
    "facility_acknowledgements": 18,
    "cases": 0,
    "tasks": 0,
    "audit_receipts": 0,
}


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _timestamp(index: int) -> str:
    value = datetime(2026, 8, 1, 12, tzinfo=UTC) + timedelta(minutes=index)
    return value.isoformat().replace("+00:00", "Z")


def _event(
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


def _anchor_products() -> list[dict[str, Any]]:
    return [
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


def _anchor_lots() -> list[dict[str, Any]]:
    return [
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


def _anchor_events() -> list[dict[str, Any]]:
    events = [
        _event("EV-001", "LOT-EXACT-170", "receiving", 1200, destination="DC-NORTH"),
        _event(
            "EV-002",
            "LOT-EXACT-170",
            "shipping",
            600,
            parent="EV-001",
            source="DC-NORTH",
            destination="STORE-01",
        ),
        _event(
            "EV-003",
            "LOT-EXACT-170",
            "shipping",
            300,
            parent="EV-001",
            source="DC-NORTH",
            destination="STORE-02",
        ),
        _event(
            "EV-008",
            "LOT-EXACT-170",
            "quarantine",
            200,
            parent="EV-001",
            source="DC-NORTH",
            destination="DC-NORTH",
        ),
        _event(
            "EV-S-LOT-EXACT-170", "LOT-EXACT-170", "sale", 550, parent="EV-002", source="STORE-01"
        ),
        _event(
            "EV-R-LOT-EXACT-170",
            "LOT-EXACT-170",
            "return",
            20,
            parent="EV-002",
            source="STORE-01",
            destination="STORE-01",
        ),
        _event(
            "EV-D-LOT-EXACT-170",
            "LOT-EXACT-170",
            "disposal",
            80,
            parent="EV-002",
            source="STORE-01",
        ),
        _event("EV-004", "LOT-PROBABLE-160", "receiving", 900, destination="DC-SOUTH"),
        _event(
            "EV-005",
            "LOT-PROBABLE-160",
            "transfer",
            780,
            parent="EV-004",
            source="DC-SOUTH",
            destination="STORE-03",
        ),
        _event(
            "EV-S-LOT-PROBABLE-160",
            "LOT-PROBABLE-160",
            "sale",
            540,
            parent="EV-005",
            source="STORE-03",
        ),
        _event(
            "EV-R-LOT-PROBABLE-160",
            "LOT-PROBABLE-160",
            "return",
            10,
            parent="EV-005",
            source="STORE-03",
            destination="STORE-03",
        ),
        _event(
            "EV-Q-LOT-PROBABLE-160",
            "LOT-PROBABLE-160",
            "quarantine",
            160,
            parent="EV-005",
            source="STORE-03",
            destination="STORE-03",
        ),
        _event(
            "EV-D-LOT-PROBABLE-160",
            "LOT-PROBABLE-160",
            "disposal",
            70,
            parent="EV-005",
            source="STORE-03",
        ),
        _event("EV-R-LOT-AMBIG-175", "LOT-AMBIG-175", "receiving", 500, destination="DC-NORTH"),
        _event(
            "EV-006",
            "LOT-AMBIG-175",
            "shipping",
            450,
            parent="EV-R-LOT-AMBIG-175",
            source="DC-NORTH",
            destination="STORE-08",
        ),
        _event("EV-007", "LOT-AMBIG-175", "sale", 400, parent="EV-006", source="STORE-08"),
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
                _event(receiving_id, lot_id, "receiving", received, destination="DC-NORTH"),
                _event(
                    movement_id,
                    lot_id,
                    "shipping",
                    sold,
                    parent=receiving_id,
                    source="DC-NORTH",
                    destination="STORE-01",
                ),
                _event(
                    f"EV-S-{lot_id}", lot_id, "sale", sold, parent=movement_id, source="STORE-01"
                ),
            ]
        )
    return events


def _background_products() -> list[dict[str, Any]]:
    categories = (
        "Frozen Berries",
        "Bagged Spinach",
        "Soft Cheese",
        "Peanut Butter",
        "Prepared Salad",
        "Oat Cereal",
        "Almond Beverage",
        "Deli Turkey",
    )
    products: list[dict[str, Any]] = []
    for index in range(44):
        if index == 0:
            upc = "090000000000"
        elif index == 1:
            upc = "090000000001"
        else:
            upc = f"08{index:010d}"
        products.append(
            {
                "product_id": f"P-BG-{index:03}",
                "name": f"Northstar {categories[index % len(categories)]} SKU {index:03}",
                "upc": upc,
                "department": categories[index % len(categories)],
            }
        )
    return products


def _background_portfolio(
    rng: random.Random,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    lots: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    positions: list[dict[str, Any]] = []
    lot_index = 0
    for product_index in range(44):
        lot_count = 4 if product_index < 6 else 3
        for ordinal in range(1, lot_count + 1):
            lot_id = f"LOT-BG-{product_index:03}-{ordinal:02}"
            plant_code = f"BG-P-{product_index % 7 + 2:02}"
            julian_date = 90 + (lot_index * 7) % 240
            classification = "rejected"
            if product_index == 0 and ordinal == 1:
                plant_code, julian_date, classification = "BG-P-01", 200, "exact"
            elif product_index == 1 and ordinal == 1:
                plant_code, julian_date, classification = "BG-P-01", 200, "probable"
            elif product_index == 0 and ordinal == 2:
                plant_code, julian_date, classification = "BG-P-01?", 201, "ambiguous"
            elif product_index == 0 and ordinal == 3:
                plant_code, julian_date = "BG-P-01", 240

            received = rng.randrange(360, 961)
            sold = rng.randrange(received * 45 // 100, received * 61 // 100)
            disposition = rng.randrange(18, 71)
            unaccounted = 7 + lot_index % 11 if lot_index % 5 == 0 else 0
            on_hand = received - sold - disposition - unaccounted
            disposition_type = ("quarantine", "disposal", "return")[lot_index % 3]
            quantities = {"quarantined": 0, "returned": 0, "disposed": 0}
            quantity_field = {
                "quarantine": "quarantined",
                "disposal": "disposed",
                "return": "returned",
            }[disposition_type]
            quantities[quantity_field] = disposition

            lots.append(
                {
                    "lot_id": lot_id,
                    "product_id": f"P-BG-{product_index:03}",
                    "plant_code": plant_code,
                    "julian_date": julian_date,
                    "classification": classification,
                    "received_units": received,
                    "on_hand": on_hand,
                    "quarantined": quantities["quarantined"],
                    "sold": sold,
                    "returned": quantities["returned"],
                    "disposed": quantities["disposed"],
                    "unaccounted": unaccounted,
                    "scenario_id": "BG-MATCH-PORTFOLIO"
                    if product_index < 2
                    else "BG-OPERATIONS-PORTFOLIO",
                }
            )

            dc = "DC-NORTH" if lot_index % 2 == 0 else "DC-SOUTH"
            store = f"STORE-{lot_index % 16 + 1:02}"
            root_id = f"EV-{lot_id}-ROOT"
            movement_id = f"EV-{lot_id}-MOVE"
            events.extend(
                [
                    _event(root_id, lot_id, "receiving", received, destination=dc),
                    _event(
                        movement_id,
                        lot_id,
                        "shipping" if lot_index % 2 == 0 else "transfer",
                        sold + disposition + on_hand // 2,
                        parent=root_id,
                        source=dc,
                        destination=store,
                    ),
                    _event(
                        f"EV-{lot_id}-SALE", lot_id, "sale", sold, parent=movement_id, source=store
                    ),
                    _event(
                        f"EV-{lot_id}-DISP",
                        lot_id,
                        disposition_type,
                        disposition,
                        parent=movement_id,
                        source=store,
                        destination=store if disposition_type in {"return", "quarantine"} else None,
                    ),
                ]
            )

            if lot_index < 72:
                dc_on_hand = on_hand // 2
                positions.extend(
                    [
                        {
                            "position_id": f"INV-{lot_id}-DC",
                            "lot_id": lot_id,
                            "facility_id": dc,
                            "on_hand": dc_on_hand,
                        },
                        {
                            "position_id": f"INV-{lot_id}-STORE",
                            "lot_id": lot_id,
                            "facility_id": store,
                            "on_hand": on_hand - dc_on_hand,
                        },
                    ]
                )
            else:
                positions.append(
                    {
                        "position_id": f"INV-{lot_id}",
                        "lot_id": lot_id,
                        "facility_id": store,
                        "on_hand": on_hand,
                    }
                )
            lot_index += 1
    return lots, events, positions


def _dataset(seed: int) -> dict[str, Any]:
    """Return a seeded portfolio with six stable, hand-auditable anchor lots."""
    rng = random.Random(seed)
    anchor_products = _anchor_products()
    anchor_lots = _anchor_lots()
    background_lots, background_events, background_positions = _background_portfolio(rng)
    products = [*anchor_products, *_background_products()]
    lots = [*anchor_lots, *background_lots]
    facilities = [
        {"facility_id": "DC-NORTH", "kind": "distribution_center"},
        {"facility_id": "DC-SOUTH", "kind": "distribution_center"},
        *({"facility_id": f"STORE-{number:02}", "kind": "store"} for number in range(1, 17)),
    ]
    raw_events = [*_anchor_events(), *background_events]
    events = [
        {**row, "origin": ORIGIN, "occurred_at": _timestamp(index)}
        for index, row in enumerate(raw_events)
    ]
    anchor_positions = [
        {
            "position_id": f"INV-{lot['lot_id']}",
            "lot_id": lot["lot_id"],
            "facility_id": "DC-SOUTH" if lot["lot_id"] == "LOT-PROBABLE-160" else "DC-NORTH",
            "on_hand": lot["on_hand"],
        }
        for lot in anchor_lots
    ]
    inventory_positions = [*anchor_positions, *background_positions]
    root_facility = {
        row["lot_id"]: row["to_facility"] for row in raw_events if row["parent_event_id"] is None
    }
    supplier_shipments = []
    for index, lot in enumerate(lots):
        shipment_id = "SHIP-001" if lot["lot_id"] == "LOT-EXACT-170" else f"SHIP-{lot['lot_id']}"
        supplier_shipments.append(
            {
                "shipment_id": shipment_id,
                "lot_id": lot["lot_id"],
                "quantity": lot["received_units"],
                "to_facility": root_facility[lot["lot_id"]],
                "shipped_at": _timestamp(index),
                "supplier": "SYNTHETIC Northstar Supplier Network",
            }
        )

    def labelled(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [{**row, "origin": ORIGIN} for row in rows]

    dataset = {
        "schema_name": SCHEMA_NAME,
        "schema_version": SCHEMA_VERSION,
        "dataset_id": "northstar-demo-20260830",
        "generated_at": GENERATED_AT,
        "seed": seed,
        "source_label": SOURCE_LABEL,
        "origin": ORIGIN,
        "retailer": "Northstar Grocers",
        "products": labelled(products),
        "lots": labelled(lots),
        "facilities": labelled(facilities),
        "events": events,
        "inventory_positions": labelled(inventory_positions),
        "supplier_shipments": labelled(supplier_shipments),
        "facility_acknowledgements": labelled(
            [
                {
                    "facility_id": facility["facility_id"],
                    "acknowledged": facility["facility_id"] != "STORE-08",
                }
                for facility in facilities
            ]
        ),
        "cases": [],
        "tasks": [],
        "audit_receipts": [],
    }
    actual_counts = {name: len(dataset[name]) for name in EXPECTED_RECORD_COUNTS}
    if actual_counts != EXPECTED_RECORD_COUNTS:
        raise AssertionError(f"generator count contract drift: {actual_counts}")
    return dataset


def generate_demo_dataset(*, output_dir: Path = DEMO_DATA_DIR, seed: int = SEED) -> dict[str, Any]:
    if seed != SEED:
        raise ValueError(f"only the pinned deterministic seed {SEED} is supported")
    dataset = _dataset(seed)
    dataset_text = _canonical_json(dataset) + "\n"
    checksum = hashlib.sha256(dataset_text.encode()).hexdigest()
    manifest = {
        "schema_name": SCHEMA_NAME,
        "schema_version": SCHEMA_VERSION,
        "dataset_id": dataset["dataset_id"],
        "generated_at": GENERATED_AT,
        "seed": seed,
        "record_counts": dict(EXPECTED_RECORD_COUNTS),
        "sha256": checksum,
        "files": ["dataset.json"],
        "checksums": {"dataset.json": checksum},
        "origin": ORIGIN,
        "source_label": SOURCE_LABEL,
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "dataset.json").write_text(dataset_text)
    (output_dir / "manifest.json").write_text(_canonical_json(manifest) + "\n")
    return {**dataset, "manifest": manifest}
