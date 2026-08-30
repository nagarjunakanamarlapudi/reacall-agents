import hashlib
import json
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter

import pytest

from recallops.data.generator import generate_demo_dataset
from recallops.data.loaders import load_demo_dataset, validate_manifest
from recallops.models import RecallPredicate
from recallops.services.traceability import TraceabilityService

EXPECTED_COUNTS = {
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


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _write_dataset(root: Path, dataset: dict, manifest: dict) -> None:
    demo_dir = root / "synthetic" / "northstar_demo"
    demo_dir.mkdir(parents=True)
    raw = (_canonical(dataset) + "\n").encode()
    (demo_dir / "dataset.json").write_bytes(raw)
    manifest["checksums"]["dataset.json"] = hashlib.sha256(raw).hexdigest()
    manifest["sha256"] = manifest["checksums"]["dataset.json"]
    (demo_dir / "manifest.json").write_text(_canonical(manifest) + "\n")


def _background_predicate() -> RecallPredicate:
    return RecallPredicate(
        product_terms=["frozen berries"],
        upcs=["090000000000"],
        plant_codes=["BG-P-01"],
        julian_start=195,
        julian_end=205,
        geography=["California"],
        hazard="Synthetic portfolio exercise",
    )


def test_committed_twin_is_a_portfolio_not_a_toy_fixture() -> None:
    dataset = load_demo_dataset()

    assert {name: len(dataset[name]) for name in EXPECTED_COUNTS} == EXPECTED_COUNTS
    assert dataset["schema_name"] == "recallops.synthetic-retailer-digital-twin"
    assert dataset["schema_version"] == "1.1.0"
    assert dataset["manifest"]["schema_name"] == dataset["schema_name"]
    assert dataset["manifest"]["schema_version"] == dataset["schema_version"]
    assert dataset["manifest"]["record_counts"] == EXPECTED_COUNTS
    assert dataset["manifest"]["seed"] == 20260830


def test_public_snapshot_stays_byte_for_byte_frozen() -> None:
    snapshot = Path("data/public/H-1230-2026.json").read_bytes()
    assert hashlib.sha256(snapshot).hexdigest() == (
        "086c80b789959dc0612f4d94ca4f199da621158416784a3e1ed0eeeecc260aa9"
    )


def test_six_hand_auditable_anchor_lots_keep_their_reviewed_outcomes() -> None:
    lots = {row["lot_id"]: row for row in load_demo_dataset()["lots"]}
    expected = {
        "LOT-EXACT-170": ("P-EXACT", "P-1950", 170, "exact", 1200, 50),
        "LOT-PROBABLE-160": ("P-PROBABLE", "0840962", 160, "probable", 900, 0),
        "LOT-AMBIG-175": ("P-NEAR", "P-1950?", 175, "ambiguous", 500, 50),
        "LOT-REJECT-190": ("P-EXACT", "P-1950", 190, "rejected", 450, 0),
        "LOT-CONTROL-170": ("P-CONTROL", "P-9999", 170, "rejected", 300, 0),
        "LOT-NEAR-150": ("P-NEAR", "0840962", 150, "rejected", 360, 0),
    }
    assert {
        lot_id: (
            lots[lot_id]["product_id"],
            lots[lot_id]["plant_code"],
            lots[lot_id]["julian_date"],
            lots[lot_id]["classification"],
            lots[lot_id]["received_units"],
            lots[lot_id]["unaccounted"],
        )
        for lot_id in expected
    } == expected


def test_background_portfolio_supports_all_match_classes_and_deep_lineage() -> None:
    service = TraceabilityService()
    matches = {
        row["lot_id"]: row["classification"] for row in service.match_lots(_background_predicate())
    }

    assert matches["LOT-BG-000-01"] == "exact"
    assert matches["LOT-BG-001-01"] == "probable"
    assert matches["LOT-BG-000-02"] == "ambiguous"
    assert matches["LOT-BG-000-03"] == "rejected"
    forward = service.trace_forward("LOT-BG-042-03")
    backward = service.trace_backward("LOT-BG-042-03")
    assert len(forward) == 4
    assert forward[0]["event_type"] == "receiving"
    assert backward[0]["event_type"] in {"sale", "quarantine", "disposal"}


def test_background_portfolio_contains_clean_and_gapped_reconciliations() -> None:
    service = TraceabilityService()
    clean = service.reconcile_units("LOT-BG-002-01")
    gapped = service.reconcile_units("LOT-BG-000-01")

    assert clean.unaccounted == 0
    assert gapped.unaccounted > 0
    assert clean.received == sum(
        (clean.on_hand, clean.quarantined, clean.sold, clean.returned, clean.disposed)
    )
    assert gapped.received > sum(
        (gapped.on_hand, gapped.quarantined, gapped.sold, gapped.returned, gapped.disposed)
    )


def test_product_catalog_filtering_and_pagination_are_stable() -> None:
    service = TraceabilityService()

    page = service.list_products(query="frozen berries", offset=2, limit=3)

    assert [row["product_id"] for row in page["items"]] == [
        "P-BG-016",
        "P-BG-024",
        "P-BG-032",
    ]
    assert page == {
        "items": page["items"],
        "offset": 2,
        "limit": 3,
        "total": 6,
        "has_more": True,
    }


@pytest.mark.parametrize(
    ("offset", "limit"),
    [(-1, 10), (0, 0), (0, 101), (True, 10), (0, False)],
)
def test_product_catalog_rejects_invalid_page_bounds(offset: int, limit: int) -> None:
    with pytest.raises(ValueError, match="offset|limit"):
        TraceabilityService().list_products(offset=offset, limit=limit)


def test_validation_rejects_count_drift_anchor_drift_and_missing_acknowledgement() -> None:
    dataset = deepcopy(load_demo_dataset())
    dataset.pop("manifest")
    dataset["products"].pop()
    assert any("products" in error and "count" in error for error in validate_manifest(dataset))

    dataset = deepcopy(load_demo_dataset())
    dataset.pop("manifest")
    next(row for row in dataset["lots"] if row["lot_id"] == "LOT-PROBABLE-160")["unaccounted"] = 1
    assert any("anchor" in error or "quantity" in error for error in validate_manifest(dataset))

    dataset = deepcopy(load_demo_dataset())
    dataset.pop("manifest")
    dataset["facility_acknowledgements"].pop()
    assert any("acknowledgement" in error for error in validate_manifest(dataset))


def test_loader_rejects_manifest_count_or_schema_tampering(tmp_path: Path) -> None:
    generated = generate_demo_dataset(output_dir=tmp_path / "generated")
    dataset = {key: value for key, value in generated.items() if key != "manifest"}
    manifest = deepcopy(generated["manifest"])
    manifest["record_counts"]["lots"] -= 1
    _write_dataset(tmp_path / "count-drift", dataset, manifest)
    with pytest.raises(ValueError, match="count"):
        load_demo_dataset(data_dir=tmp_path / "count-drift")

    manifest = deepcopy(generated["manifest"])
    manifest["schema_version"] = "0.0.0"
    _write_dataset(tmp_path / "schema-drift", dataset, manifest)
    with pytest.raises(ValueError, match="schema|metadata"):
        load_demo_dataset(data_dir=tmp_path / "schema-drift")


def test_validation_checks_aggregate_inventory_event_and_shipment_quantities() -> None:
    for collection, field in (
        ("inventory_positions", "on_hand"),
        ("events", "quantity"),
        ("supplier_shipments", "quantity"),
    ):
        dataset = deepcopy(load_demo_dataset())
        dataset.pop("manifest")
        dataset[collection][-1][field] += 1
        assert any(
            "aggregate" in error or "shipment" in error for error in validate_manifest(dataset)
        )


def test_validation_rejects_shipment_destination_outside_its_receiving_lineage() -> None:
    dataset = deepcopy(load_demo_dataset())
    dataset.pop("manifest")
    shipment = next(
        row for row in dataset["supplier_shipments"] if row["lot_id"] == "LOT-PROBABLE-160"
    )
    shipment["to_facility"] = "DC-NORTH"

    assert any("shipment" in error and "lineage" in error for error in validate_manifest(dataset))


def test_validation_rejects_child_event_before_its_parent() -> None:
    dataset = deepcopy(load_demo_dataset())
    dataset.pop("manifest")
    event = next(row for row in dataset["events"] if row["event_id"] == "EV-002")
    event["occurred_at"] = "2026-07-31T12:00:00Z"

    assert any("chronology" in error for error in validate_manifest(dataset))


def test_every_synthetic_timestamp_is_timezone_aware_and_every_facility_is_covered() -> None:
    dataset = load_demo_dataset()
    facility_ids = {row["facility_id"] for row in dataset["facilities"]}
    acknowledgement_ids = {row["facility_id"] for row in dataset["facility_acknowledgements"]}
    assert acknowledgement_ids == facility_ids
    for row in [*dataset["events"], *dataset["supplier_shipments"]]:
        timestamp = datetime.fromisoformat(
            row["occurred_at"] if "occurred_at" in row else row["shipped_at"]
        )
        assert timestamp.tzinfo is not None
        assert timestamp.utcoffset() == UTC.utcoffset(timestamp)


def test_normal_portfolio_queries_remain_fast_enough_for_the_live_demo() -> None:
    service = TraceabilityService()
    started = perf_counter()
    for _ in range(25):
        service.find_candidate_products(_background_predicate())
        service.match_lots(_background_predicate())
        service.trace_forward("LOT-BG-042-03")
        service.trace_backward("LOT-BG-042-03")
        service.reconcile_units("LOT-BG-002-01")
    elapsed = perf_counter() - started

    assert elapsed < 2.0
