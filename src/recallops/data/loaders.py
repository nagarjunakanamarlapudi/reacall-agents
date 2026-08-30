"""Validated access to frozen public and generated synthetic records."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from recallops.data.generator import ORIGIN, _canonical_json
from recallops.models import RecallRecord
from recallops.paths import DEMO_DATA_DIR, PUBLIC_DATA_DIR


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text())


def load_recall_snapshot(recall_number: str = "H-1230-2026") -> RecallRecord:
    payload_path = PUBLIC_DATA_DIR / f"{recall_number}.json"
    metadata = _read_json(PUBLIC_DATA_DIR / f"{recall_number}.metadata.json")
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
    if dataset.get("origin") != ORIGIN:
        errors.append("dataset is not labelled synthetic")
    product_ids = {product["product_id"] for product in dataset.get("products", [])}
    lot_ids = {lot["lot_id"] for lot in dataset.get("lots", [])}
    facility_ids = {facility["facility_id"] for facility in dataset.get("facilities", [])}
    for lot in dataset.get("lots", []):
        if lot["product_id"] not in product_ids:
            errors.append(f"unknown product for {lot['lot_id']}")
        expected = sum(
            lot[key]
            for key in ("on_hand", "quarantined", "sold", "returned", "disposed", "unaccounted")
        )
        if lot["received_units"] != expected:
            errors.append(f"quantity equation mismatch for {lot['lot_id']}")
    for event in dataset.get("events", []):
        if event["lot_id"] not in lot_ids:
            errors.append(f"unknown lot for {event['event_id']}")
        for key in ("from_facility", "to_facility"):
            if event.get(key) and event[key] not in facility_ids:
                errors.append(f"unknown facility for {event['event_id']}")
    return errors


def load_demo_dataset(data_dir: Path = DEMO_DATA_DIR) -> dict[str, Any]:
    dataset = _read_json(data_dir / "dataset.json")
    manifest = _read_json(data_dir / "manifest.json")
    checksum = hashlib.sha256(_canonical_json(dataset).encode()).hexdigest()
    if checksum != manifest["sha256"]:
        raise ValueError("synthetic dataset checksum mismatch")
    errors = validate_manifest(dataset)
    if errors:
        raise ValueError("; ".join(errors))
    return {**dataset, "manifest": manifest}
