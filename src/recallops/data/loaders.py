"""Validated access to frozen public and generated synthetic records."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from recallops.config import get_settings
from recallops.data.generator import ORIGIN
from recallops.models import RecallRecord

EXPECTED_COLLECTION_IDS: dict[str, set[str]] = {
    "products": {"P-EXACT", "P-PROBABLE", "P-NEAR", "P-CONTROL"},
    "lots": {
        "LOT-EXACT-170",
        "LOT-PROBABLE-160",
        "LOT-AMBIG-175",
        "LOT-REJECT-190",
        "LOT-CONTROL-170",
        "LOT-NEAR-150",
    },
    "facilities": {
        "DC-NORTH",
        "DC-SOUTH",
        *(f"STORE-{number:02}" for number in range(1, 9)),
    },
    "events": {
        "EV-001",
        "EV-002",
        "EV-003",
        "EV-004",
        "EV-005",
        "EV-006",
        "EV-007",
        "EV-008",
        "EV-S-LOT-EXACT-170",
        "EV-R-LOT-EXACT-170",
        "EV-D-LOT-EXACT-170",
        "EV-S-LOT-PROBABLE-160",
        "EV-R-LOT-PROBABLE-160",
        "EV-Q-LOT-PROBABLE-160",
        "EV-D-LOT-PROBABLE-160",
        "EV-R-LOT-AMBIG-175",
        "EV-R-LOT-REJECT-190",
        "EV-M-LOT-REJECT-190",
        "EV-S-LOT-REJECT-190",
        "EV-R-LOT-CONTROL-170",
        "EV-M-LOT-CONTROL-170",
        "EV-S-LOT-CONTROL-170",
        "EV-R-LOT-NEAR-150",
        "EV-M-LOT-NEAR-150",
        "EV-S-LOT-NEAR-150",
    },
    "inventory_positions": {
        "INV-LOT-EXACT-170",
        "INV-LOT-PROBABLE-160",
        "INV-LOT-AMBIG-175",
        "INV-LOT-REJECT-190",
        "INV-LOT-CONTROL-170",
        "INV-LOT-NEAR-150",
    },
    "supplier_shipments": {"SHIP-001"},
    "facility_acknowledgements": {
        "DC-NORTH",
        "DC-SOUTH",
        *(f"STORE-{number:02}" for number in range(1, 9)),
    },
    "cases": set(),
    "tasks": set(),
    "audit_receipts": set(),
}

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

EXPECTED_CLASSIFICATIONS = {
    "LOT-EXACT-170": "exact",
    "LOT-PROBABLE-160": "probable",
    "LOT-AMBIG-175": "ambiguous",
    "LOT-REJECT-190": "rejected",
    "LOT-CONTROL-170": "rejected",
    "LOT-NEAR-150": "rejected",
}


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text())


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
    errors: list[str] = []
    if dataset.get("dataset_id") != "northstar-demo-20260830":
        errors.append("unexpected dataset_id")
    if dataset.get("seed") != 20260830:
        errors.append("unexpected dataset seed")
    if dataset.get("origin") != ORIGIN:
        errors.append("dataset is not labelled synthetic")
    if dataset.get("source_label") != "SYNTHETIC — ACADEMIC DEMO":
        errors.append("unexpected synthetic source label")

    actual_ids: dict[str, set[str]] = {}
    collection_rows: dict[str, list[dict[str, Any]]] = {}
    for collection, expected_ids in EXPECTED_COLLECTION_IDS.items():
        rows = dataset.get(collection)
        if not isinstance(rows, list):
            errors.append(f"missing collection {collection}")
            rows = []
        valid_rows = [row for row in rows if isinstance(row, dict)]
        collection_rows[collection] = valid_rows
        field = ID_FIELDS[collection]
        values = [row.get(field) for row in valid_rows]
        string_values = [value for value in values if isinstance(value, str)]
        if len(string_values) != len(set(string_values)) or len(string_values) != len(rows):
            errors.append(f"duplicate or missing {collection} ids")
        actual_ids[collection] = set(string_values)
        if actual_ids[collection] != expected_ids or len(rows) != len(expected_ids):
            errors.append(f"unexpected exact ids/count for {collection}")
        if any(not isinstance(row, dict) or row.get("origin") != ORIGIN for row in rows):
            errors.append(f"unlabelled origin in {collection}")

    product_ids = actual_ids["products"]
    lot_ids = actual_ids["lots"]
    facility_ids = actual_ids["facilities"]

    def nonnegative_integer(value: Any) -> bool:
        return isinstance(value, int) and not isinstance(value, bool) and value >= 0

    for lot in collection_rows["lots"]:
        lot_id = lot.get("lot_id", "unknown")
        if lot.get("product_id") not in product_ids:
            errors.append(f"unknown product for {lot_id}")
        quantity_fields = (
            "received_units",
            "on_hand",
            "quarantined",
            "sold",
            "returned",
            "disposed",
            "unaccounted",
        )
        if any(not nonnegative_integer(lot.get(field)) for field in quantity_fields):
            errors.append(f"negative or invalid quantity for {lot_id}")
        if not isinstance(lot.get("julian_date"), int) or lot.get("julian_date") not in range(
            1, 367
        ):
            errors.append(f"invalid lot date for {lot_id}")
        if all(isinstance(lot.get(field), int) for field in quantity_fields):
            expected = sum(
                lot[key]
                for key in (
                    "on_hand",
                    "quarantined",
                    "sold",
                    "returned",
                    "disposed",
                    "unaccounted",
                )
            )
            if lot["received_units"] != expected:
                errors.append(f"quantity equation mismatch for {lot_id}")
        if EXPECTED_CLASSIFICATIONS.get(lot_id) != lot.get("classification"):
            errors.append(f"unexpected named control classification for {lot_id}")

    event_by_id = {
        event.get("event_id"): event for event in collection_rows["events"] if event.get("event_id")
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
        if not nonnegative_integer(event.get("quantity")):
            errors.append(f"negative or invalid event quantity for {event_id}")
        try:
            occurred_at = datetime.fromisoformat(event.get("occurred_at", ""))
            if occurred_at.tzinfo is None:
                raise ValueError
        except (TypeError, ValueError):
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
        parent_facility = parent.get("to_facility") or parent.get("from_facility")
        child_facility = event.get("from_facility") or event.get("to_facility")
        if parent_facility != child_facility:
            errors.append(f"facility continuity mismatch for {event_id}")

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

    for row in collection_rows["inventory_positions"]:
        if (
            row.get("lot_id") not in lot_ids
            or row.get("facility_id") not in facility_ids
            or not nonnegative_integer(row.get("on_hand"))
        ):
            errors.append("invalid inventory position")
        traced_facilities = {
            event.get("from_facility")
            for event in collection_rows["events"]
            if event.get("lot_id") == row.get("lot_id")
        } | {
            event.get("to_facility")
            for event in collection_rows["events"]
            if event.get("lot_id") == row.get("lot_id")
        }
        if row.get("facility_id") not in traced_facilities:
            errors.append("inventory facility is outside event lineage")
    for row in collection_rows["supplier_shipments"]:
        if row.get("lot_id") not in lot_ids:
            errors.append("invalid supplier shipment")
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
    expected_manifest_keys = {"dataset_id", "seed", "origin", "files", "checksums", "sha256"}
    if set(manifest) != expected_manifest_keys:
        raise ValueError("synthetic dataset manifest fields mismatch")
    if manifest.get("files") != ["dataset.json"] or set(manifest.get("checksums", {})) != {
        "dataset.json"
    }:
        raise ValueError("synthetic dataset manifest files/checksums mismatch")
    if checksum != manifest["checksums"]["dataset.json"] or checksum != manifest["sha256"]:
        raise ValueError("synthetic dataset checksum mismatch")
    if (
        manifest.get("dataset_id") != dataset.get("dataset_id")
        or manifest.get("seed") != 20260830
        or manifest.get("origin") != ORIGIN
        or manifest.get("files") != ["dataset.json"]
    ):
        raise ValueError("synthetic dataset manifest metadata mismatch")
    errors = validate_manifest(dataset)
    if errors:
        raise ValueError("; ".join(errors))
    return {**dataset, "manifest": manifest}
