"""Validated access to frozen public and generated synthetic records."""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

from recallops.config import get_settings
from recallops.data.generator import (
    EXPECTED_RECORD_COUNTS,
    GENERATED_AT,
    ORIGIN,
    SCHEMA_NAME,
    SCHEMA_VERSION,
    SEED,
    SOURCE_LABEL,
)
from recallops.models import RecallRecord

ID_FIELDS = {
    "products": "product_id",
    "lots": "lot_id",
    "facilities": "facility_id",
    "events": "event_id",
    "inventory_positions": "position_id",
    "supplier_shipments": "shipment_id",
    "facility_acknowledgements": "facility_id",
    "cases": "case_id",
    "tasks": "task_id",
    "audit_receipts": "receipt_id",
}

ANCHOR_PRODUCT_IDENTITIES = {
    "P-EXACT": ("Northstar Grade A Large Eggs 12 ct", "011110609038"),
    "P-PROBABLE": ("Northstar Farm Fresh Large Eggs 18 ct", "011110609039"),
    "P-NEAR": ("Northstar Cage Free Large Eggs 12 ct", "011110609038"),
    "P-CONTROL": ("Northstar Organic Brown Eggs 12 ct", "088888888881"),
}

ANCHOR_LOT_IDENTITIES = {
    "LOT-EXACT-170": ("P-EXACT", "P-1950", 170, "exact", 1200, 300, 200, 550, 20, 80, 50),
    "LOT-PROBABLE-160": (
        "P-PROBABLE",
        "0840962",
        160,
        "probable",
        900,
        120,
        160,
        540,
        10,
        70,
        0,
    ),
    "LOT-AMBIG-175": ("P-NEAR", "P-1950?", 175, "ambiguous", 500, 50, 0, 400, 0, 0, 50),
    "LOT-REJECT-190": ("P-EXACT", "P-1950", 190, "rejected", 450, 100, 0, 350, 0, 0, 0),
    "LOT-CONTROL-170": ("P-CONTROL", "P-9999", 170, "rejected", 300, 100, 0, 200, 0, 0, 0),
    "LOT-NEAR-150": ("P-NEAR", "0840962", 150, "rejected", 360, 60, 0, 300, 0, 0, 0),
}

LOT_IDENTITY_FIELDS = (
    "product_id",
    "plant_code",
    "julian_date",
    "classification",
    "received_units",
    "on_hand",
    "quarantined",
    "sold",
    "returned",
    "disposed",
    "unaccounted",
)
PINNED_DATASET_SHA256 = "6f60ce4a3119aae2d68b3ea3c5105d79cc0df9fd335c2c2132218a886f5c61d9"


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text())


def _has_timezone(value: Any) -> bool:
    try:
        return datetime.fromisoformat(value).tzinfo is not None
    except (TypeError, ValueError):
        return False


def _is_nonnegative_integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def load_recall_snapshot(
    recall_number: str = "H-1230-2026", data_dir: Path | None = None
) -> RecallRecord:
    public_dir = (Path(data_dir) if data_dir is not None else get_settings().data_dir) / "public"
    payload_path = public_dir / f"{recall_number}.json"
    metadata = _read_json(public_dir / f"{recall_number}.metadata.json")
    raw_payload = payload_path.read_bytes()
    checksum = hashlib.sha256(raw_payload).hexdigest()
    if checksum != metadata["sha256"]:
        raise ValueError("pinned openFDA snapshot checksum mismatch")
    response = json.loads(raw_payload)
    payload = response["results"][0] if "results" in response else response
    return RecallRecord(
        recall_number=payload["recall_number"],
        source="openFDA Food Enforcement API",
        provenance="OFFICIAL_OPENFDA_SNAPSHOT",
        retrieved_at=datetime.fromisoformat(metadata["retrieved_at"]),
        payload=payload,
        sha256=checksum,
        source_url=metadata["source_url"],
    )


def validate_manifest(dataset: dict[str, Any]) -> list[str]:
    """Return every dataset-contract error instead of failing at the first one."""
    errors: list[str] = []
    expected_metadata = {
        "schema_name": SCHEMA_NAME,
        "schema_version": SCHEMA_VERSION,
        "dataset_id": "northstar-demo-20260830",
        "generated_at": GENERATED_AT,
        "seed": SEED,
        "origin": ORIGIN,
        "source_label": SOURCE_LABEL,
    }
    for field, expected in expected_metadata.items():
        if dataset.get(field) != expected:
            errors.append(f"unexpected dataset {field}")
    if not _has_timezone(dataset.get("generated_at")):
        errors.append("invalid dataset generated_at timestamp")

    actual_ids: dict[str, set[str]] = {}
    collection_rows: dict[str, list[dict[str, Any]]] = {}
    for collection, expected_count in EXPECTED_RECORD_COUNTS.items():
        rows = dataset.get(collection)
        if not isinstance(rows, list):
            errors.append(f"missing collection {collection}")
            rows = []
        if len(rows) != expected_count:
            errors.append(
                f"unexpected record count for {collection}: expected {expected_count}, got {len(rows)}"
            )
        valid_rows = [row for row in rows if isinstance(row, dict)]
        collection_rows[collection] = valid_rows
        field = ID_FIELDS[collection]
        values = [row.get(field) for row in valid_rows]
        string_values = [value for value in values if isinstance(value, str) and value]
        if len(string_values) != len(set(string_values)) or len(string_values) != len(rows):
            errors.append(f"duplicate or missing {collection} ids")
        actual_ids[collection] = set(string_values)
        if any(not isinstance(row, dict) or row.get("origin") != ORIGIN for row in rows):
            errors.append(f"unlabelled origin in {collection}")

    products_by_id = {
        row["product_id"]: row
        for row in collection_rows["products"]
        if isinstance(row.get("product_id"), str)
    }
    for product_id, expected in ANCHOR_PRODUCT_IDENTITIES.items():
        product = products_by_id.get(product_id)
        if product is None or (product.get("name"), product.get("upc")) != expected:
            errors.append(f"anchor product identity mismatch for {product_id}")
    for product in collection_rows["products"]:
        upc = product.get("upc")
        if not isinstance(upc, str) or len(upc) != 12 or not upc.isdigit():
            errors.append(f"invalid synthetic UPC for {product.get('product_id', 'unknown')}")

    product_ids = actual_ids["products"]
    lot_ids = actual_ids["lots"]
    facility_ids = actual_ids["facilities"]
    lots_by_id = {
        row["lot_id"]: row for row in collection_rows["lots"] if isinstance(row.get("lot_id"), str)
    }
    quantity_fields = (
        "received_units",
        "on_hand",
        "quarantined",
        "sold",
        "returned",
        "disposed",
        "unaccounted",
    )
    for lot in collection_rows["lots"]:
        lot_id = lot.get("lot_id", "unknown")
        if lot.get("product_id") not in product_ids:
            errors.append(f"unknown product for {lot_id}")
        if lot.get("classification") not in {"exact", "probable", "ambiguous", "rejected"}:
            errors.append(f"invalid classification for {lot_id}")
        if any(not _is_nonnegative_integer(lot.get(field)) for field in quantity_fields):
            errors.append(f"negative or invalid quantity for {lot_id}")
        if not isinstance(lot.get("julian_date"), int) or lot.get("julian_date") not in range(
            1, 367
        ):
            errors.append(f"invalid lot date for {lot_id}")
        if all(_is_nonnegative_integer(lot.get(field)) for field in quantity_fields):
            accounted = sum(lot[field] for field in quantity_fields[1:])
            if lot["received_units"] != accounted:
                errors.append(f"quantity equation mismatch for {lot_id}")
    for lot_id, expected in ANCHOR_LOT_IDENTITIES.items():
        lot = lots_by_id.get(lot_id)
        if lot is None or tuple(lot.get(field) for field in LOT_IDENTITY_FIELDS) != expected:
            errors.append(f"anchor lot identity mismatch for {lot_id}")

    event_by_id = {
        event["event_id"]: event
        for event in collection_rows["events"]
        if isinstance(event.get("event_id"), str)
    }
    event_types = {"receiving", "shipping", "transfer", "sale", "return", "quarantine", "disposal"}
    for event in collection_rows["events"]:
        event_id = event.get("event_id", "unknown")
        if event.get("event_type") not in event_types:
            errors.append(f"invalid event type for {event_id}")
        if event.get("lot_id") not in lot_ids:
            errors.append(f"unknown lot for {event_id}")
        for key in ("from_facility", "to_facility"):
            if event.get(key) and event[key] not in facility_ids:
                errors.append(f"unknown facility for {event_id}")
        if not _is_nonnegative_integer(event.get("quantity")):
            errors.append(f"negative or invalid event quantity for {event_id}")
        if event.get("event_type") in {"shipping", "transfer"} and (
            not _is_nonnegative_integer(event.get("quantity")) or event["quantity"] == 0
        ):
            errors.append(f"positive movement quantity required for {event_id}")
        if not _has_timezone(event.get("occurred_at")):
            errors.append(f"invalid ISO timestamp for {event_id}")
        parent_id = event.get("parent_event_id")
        if parent_id is None:
            if event.get("event_type") != "receiving" or not event.get("to_facility"):
                errors.append(f"invalid lineage root for {event_id}")
            continue
        parent = event_by_id.get(parent_id)
        if not parent:
            errors.append(f"unknown parent for {event_id}")
            continue
        if parent.get("lot_id") != event.get("lot_id"):
            errors.append(f"parent lot mismatch for {event_id}")
        if _is_nonnegative_integer(parent.get("quantity")) and _is_nonnegative_integer(
            event.get("quantity")
        ):
            if event["quantity"] > parent["quantity"]:
                errors.append(f"lineage quantity mismatch for {event_id}")
        parent_facility = parent.get("to_facility") or parent.get("from_facility")
        child_facility = event.get("from_facility") or event.get("to_facility")
        if parent_facility != child_facility:
            errors.append(f"facility continuity mismatch for {event_id}")
        if _has_timezone(parent.get("occurred_at")) and _has_timezone(event.get("occurred_at")):
            parent_time = datetime.fromisoformat(parent["occurred_at"])
            child_time = datetime.fromisoformat(event["occurred_at"])
            if child_time <= parent_time:
                errors.append(f"lineage chronology mismatch for {event_id}")

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(event_id: str) -> None:
        if event_id in visiting:
            errors.append(f"lineage cycle at {event_id}")
            return
        if event_id in visited:
            return
        visiting.add(event_id)
        parent_id = event_by_id[event_id].get("parent_event_id")
        if parent_id in event_by_id:
            visit(parent_id)
        visiting.remove(event_id)
        visited.add(event_id)

    for event_id in event_by_id:
        visit(event_id)

    inventory_by_lot: Counter[str] = Counter()
    event_quantities: dict[str, Counter[str]] = defaultdict(Counter)
    shipment_by_lot: Counter[str] = Counter()
    receiving_facilities: dict[str, set[str]] = defaultdict(set)
    receiving_times: dict[tuple[str, str], list[datetime]] = defaultdict(list)
    for event in collection_rows["events"]:
        if event.get("event_type") == "receiving" and event.get("to_facility"):
            receiving_facilities[event.get("lot_id", "")].add(event["to_facility"])
            if _has_timezone(event.get("occurred_at")):
                receiving_times[(event.get("lot_id", ""), event["to_facility"])].append(
                    datetime.fromisoformat(event["occurred_at"])
                )
    for row in collection_rows["inventory_positions"]:
        lot_id = row.get("lot_id")
        facility_id = row.get("facility_id")
        if (
            lot_id not in lot_ids
            or facility_id not in facility_ids
            or not _is_nonnegative_integer(row.get("on_hand"))
        ):
            errors.append("invalid inventory position")
            continue
        traced_facilities = {
            event.get(key)
            for event in collection_rows["events"]
            if event.get("lot_id") == lot_id
            for key in ("from_facility", "to_facility")
            if event.get(key)
        }
        if facility_id not in traced_facilities:
            errors.append(f"inventory facility is outside event lineage for {lot_id}")
        inventory_by_lot[lot_id] += row["on_hand"]

    for event in collection_rows["events"]:
        if event.get("lot_id") in lot_ids and _is_nonnegative_integer(event.get("quantity")):
            event_quantities[event["lot_id"]][event.get("event_type", "invalid")] += event[
                "quantity"
            ]

    for row in collection_rows["supplier_shipments"]:
        lot_id = row.get("lot_id")
        if (
            lot_id not in lot_ids
            or row.get("to_facility") not in facility_ids
            or not _is_nonnegative_integer(row.get("quantity"))
            or not _has_timezone(row.get("shipped_at"))
        ):
            errors.append(f"invalid supplier shipment {row.get('shipment_id', 'unknown')}")
            continue
        if row["to_facility"] not in receiving_facilities[lot_id]:
            errors.append(
                f"supplier shipment lineage mismatch for {row.get('shipment_id', 'unknown')}"
            )
        else:
            matching_receipts = receiving_times.get((lot_id, row["to_facility"]), [])
            if matching_receipts and datetime.fromisoformat(row["shipped_at"]) > min(
                matching_receipts
            ):
                errors.append(
                    f"supplier shipment chronology mismatch for {row.get('shipment_id', 'unknown')}"
                )
        shipment_by_lot[lot_id] += row["quantity"]

    for lot_id, lot in lots_by_id.items():
        if inventory_by_lot[lot_id] != lot.get("on_hand"):
            errors.append(f"inventory aggregate mismatch for {lot_id}")
        expected_event_fields = {
            "receiving": "received_units",
            "quarantine": "quarantined",
            "sale": "sold",
            "return": "returned",
            "disposal": "disposed",
        }
        for event_type, field in expected_event_fields.items():
            if event_quantities[lot_id][event_type] != lot.get(field):
                errors.append(f"event aggregate mismatch for {lot_id} {event_type}")
        if shipment_by_lot[lot_id] != lot.get("received_units"):
            errors.append(f"supplier shipment aggregate mismatch for {lot_id}")

    acknowledgement_ids = actual_ids["facility_acknowledgements"]
    if acknowledgement_ids != facility_ids:
        errors.append("facility acknowledgement coverage mismatch")
    for row in collection_rows["facility_acknowledgements"]:
        if row.get("facility_id") not in facility_ids or not isinstance(
            row.get("acknowledged"), bool
        ):
            errors.append("invalid facility acknowledgement")
    return errors


def load_demo_dataset(data_dir: Path | None = None) -> dict[str, Any]:
    root = Path(data_dir) if data_dir is not None else get_settings().data_dir
    demo_dir = root / "synthetic" / "northstar_demo"
    manifest = _read_json(demo_dir / "manifest.json")
    actual_files = {
        path.name for path in demo_dir.iterdir() if path.is_file() and path.name != "manifest.json"
    }
    if actual_files != set(manifest.get("files", [])):
        raise ValueError("synthetic dataset manifest files do not match directory")
    dataset_path = demo_dir / "dataset.json"
    raw_dataset = dataset_path.read_bytes()
    dataset = json.loads(raw_dataset)
    checksum = hashlib.sha256(raw_dataset).hexdigest()
    expected_manifest_keys = {
        "schema_name",
        "schema_version",
        "dataset_id",
        "generated_at",
        "seed",
        "record_counts",
        "origin",
        "source_label",
        "files",
        "checksums",
        "sha256",
    }
    if set(manifest) != expected_manifest_keys:
        raise ValueError("synthetic dataset manifest fields mismatch")
    if manifest.get("files") != ["dataset.json"] or set(manifest.get("checksums", {})) != {
        "dataset.json"
    }:
        raise ValueError("synthetic dataset manifest files/checksums mismatch")
    if (
        checksum != PINNED_DATASET_SHA256
        or manifest["checksums"]["dataset.json"] != PINNED_DATASET_SHA256
        or manifest["sha256"] != PINNED_DATASET_SHA256
    ):
        raise ValueError("trusted dataset checksum mismatch")
    expected_metadata = {
        "schema_name": SCHEMA_NAME,
        "schema_version": SCHEMA_VERSION,
        "dataset_id": "northstar-demo-20260830",
        "generated_at": GENERATED_AT,
        "seed": SEED,
        "record_counts": EXPECTED_RECORD_COUNTS,
        "origin": ORIGIN,
        "source_label": SOURCE_LABEL,
    }
    if any(manifest.get(field) != expected for field, expected in expected_metadata.items()):
        raise ValueError("synthetic dataset manifest metadata or record count mismatch")
    actual_counts = {name: len(dataset.get(name, [])) for name in EXPECTED_RECORD_COUNTS}
    if manifest["record_counts"] != actual_counts:
        raise ValueError("synthetic dataset manifest record count mismatch")
    errors = validate_manifest(dataset)
    if errors:
        raise ValueError("; ".join(errors))
    return {**dataset, "manifest": manifest}
