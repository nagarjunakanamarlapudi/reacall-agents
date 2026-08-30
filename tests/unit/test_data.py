import hashlib
import json
from copy import deepcopy
from pathlib import Path

import pytest

from recallops.data.generator import ORIGIN, generate_demo_dataset
from recallops.data.loaders import load_demo_dataset, load_recall_snapshot, validate_manifest

EXPECTED_IDS = {
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
        "STORE-01",
        "STORE-02",
        "STORE-03",
        "STORE-04",
        "STORE-05",
        "STORE-06",
        "STORE-07",
        "STORE-08",
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
        "STORE-01",
        "STORE-02",
        "STORE-03",
        "STORE-04",
        "STORE-05",
        "STORE-06",
        "STORE-07",
        "STORE-08",
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
}


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _write_twin(root: Path, dataset: dict, manifest: dict) -> None:
    demo = root / "synthetic" / "northstar_demo"
    demo.mkdir(parents=True)
    (demo / "dataset.json").write_text(_canonical(dataset) + "\n")
    (demo / "manifest.json").write_text(_canonical(manifest) + "\n")


def _load_tamper(root: Path, dataset: dict, manifest: dict) -> None:
    manifest["checksums"]["dataset.json"] = hashlib.sha256(
        (_canonical(dataset) + "\n").encode()
    ).hexdigest()
    _write_twin(root, dataset, manifest)
    load_demo_dataset(data_dir=root)


def test_loads_exact_pinned_openfda_recall_with_checksum() -> None:
    recall = load_recall_snapshot()

    assert recall.recall_number == "H-1230-2026"
    assert recall.provenance == "OFFICIAL_OPENFDA_SNAPSHOT"
    assert recall.payload["classification"] == "Class I"
    assert recall.payload["reason_for_recall"] == "Possible Salmonella Enteritidis"
    assert recall.sha256


def test_demo_twin_has_expected_referentially_valid_shape() -> None:
    dataset = load_demo_dataset()

    assert dataset["source_label"] == "SYNTHETIC — ACADEMIC DEMO"
    assert validate_manifest(dataset) == []
    for collection, expected in EXPECTED_IDS.items():
        if collection in ID_FIELDS:
            actual = {row[ID_FIELDS[collection]] for row in dataset[collection]}
        else:
            actual = set(dataset[collection])
        assert actual == expected


def test_named_matching_controls_are_stable() -> None:
    dataset = load_demo_dataset()
    controls = {lot["lot_id"]: lot["classification"] for lot in dataset["lots"]}
    assert controls == {
        "LOT-EXACT-170": "exact",
        "LOT-PROBABLE-160": "probable",
        "LOT-AMBIG-175": "ambiguous",
        "LOT-REJECT-190": "rejected",
        "LOT-CONTROL-170": "rejected",
        "LOT-NEAR-150": "rejected",
    }


def test_committed_manifest_has_the_current_reviewed_hash() -> None:
    manifest_path = Path("data/synthetic/northstar_demo/manifest.json")
    assert hashlib.sha256(manifest_path.read_bytes()).hexdigest() == (
        "282baa4ea9f9c1fcb90f6cd429b06275910c72679a7fffadebe719c22dd97f19"
    )


def test_demo_generation_is_deterministic_and_preserves_quantity_fixture(tmp_path: Path) -> None:
    first = generate_demo_dataset(output_dir=tmp_path / "one", seed=20260830)
    second = generate_demo_dataset(output_dir=tmp_path / "two", seed=20260830)

    assert first["manifest"]["sha256"] == second["manifest"]["sha256"]
    assert first["manifest"] == {
        "dataset_id": "northstar-demo-20260830",
        "seed": 20260830,
        "origin": ORIGIN,
        "files": ["dataset.json"],
        "checksums": {"dataset.json": first["manifest"]["checksums"]["dataset.json"]},
        "sha256": first["manifest"]["checksums"]["dataset.json"],
    }
    exact_lot = next(lot for lot in first["lots"] if lot["lot_id"] == "LOT-EXACT-170")
    assert exact_lot["received_units"] == 1_200
    assert exact_lot["received_units"] == sum(
        exact_lot[key]
        for key in ("on_hand", "quarantined", "sold", "returned", "disposed", "unaccounted")
    )


@pytest.mark.parametrize(
    ("collection", "field", "value"),
    [
        ("products", "origin", "bad"),
        ("lots", "product_id", "missing"),
        ("facilities", "facility_id", "DC-SOUTH"),
        ("events", "lot_id", "missing"),
        ("inventory_positions", "on_hand", -1),
        ("supplier_shipments", "lot_id", "missing"),
        ("facility_acknowledgements", "acknowledged", "yes"),
    ],
)
def test_manifest_validation_rejects_tampered_record_classes(
    collection: str, field: str, value: object
) -> None:
    dataset = deepcopy(load_demo_dataset())
    dataset.pop("manifest")
    dataset[collection][0][field] = value
    assert validate_manifest(dataset)


@pytest.mark.parametrize(
    "collection",
    [
        "products",
        "lots",
        "facilities",
        "events",
        "inventory_positions",
        "supplier_shipments",
        "facility_acknowledgements",
    ],
)
def test_validation_rejects_bad_origin_in_every_populated_collection(collection: str) -> None:
    dataset = deepcopy(load_demo_dataset())
    dataset.pop("manifest")
    dataset[collection][0]["origin"] = "UNLABELLED"
    assert any(collection in error and "origin" in error for error in validate_manifest(dataset))


@pytest.mark.parametrize("collection", ["cases", "tasks", "audit_receipts"])
def test_validation_rejects_unexpected_rows_in_empty_runtime_collections(collection: str) -> None:
    dataset = deepcopy(load_demo_dataset())
    dataset.pop("manifest")
    dataset[collection].append({"origin": ORIGIN})
    assert any(collection in error for error in validate_manifest(dataset))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("dataset_id", "wrong-dataset"),
        ("seed", 1),
        ("origin", "UNLABELLED"),
        ("files", ["dataset.json", "extra.json"]),
        ("checksums", {"other.json": "0" * 64}),
    ],
)
def test_loader_rejects_every_manifest_identity_tamper(
    tmp_path: Path, field: str, value: object
) -> None:
    generated = generate_demo_dataset(output_dir=tmp_path / "generated")
    dataset = {key: value for key, value in generated.items() if key != "manifest"}
    manifest = deepcopy(generated["manifest"])
    manifest[field] = value
    _write_twin(tmp_path / "data", dataset, manifest)

    with pytest.raises(ValueError, match="manifest|checksum"):
        load_demo_dataset(data_dir=tmp_path / "data")


def test_loader_checks_raw_file_bytes_and_rejects_unlisted_files(tmp_path: Path) -> None:
    root = tmp_path / "data"
    demo = root / "synthetic" / "northstar_demo"
    generate_demo_dataset(output_dir=demo)
    dataset_path = demo / "dataset.json"
    dataset_path.write_text(dataset_path.read_text() + " \n")
    with pytest.raises(ValueError, match="checksum"):
        load_demo_dataset(data_dir=root)

    other_root = tmp_path / "other-data"
    other_demo = other_root / "synthetic" / "northstar_demo"
    generate_demo_dataset(output_dir=other_demo)
    (other_demo / "unlisted.json").write_text("{}\n")
    with pytest.raises(ValueError, match="files"):
        load_demo_dataset(data_dir=other_root)


@pytest.mark.parametrize(
    ("collection", "field", "value"),
    [
        ("products", "product_id", "P-OTHER"),
        ("lots", "lot_id", "LOT-OTHER"),
        ("facilities", "facility_id", "FACILITY-OTHER"),
        ("events", "event_id", "EV-OTHER"),
        ("inventory_positions", "position_id", "INV-OTHER"),
        ("supplier_shipments", "shipment_id", "SHIP-OTHER"),
        ("facility_acknowledgements", "facility_id", "FACILITY-OTHER"),
    ],
)
def test_validation_rejects_wrong_exact_id_in_every_collection(
    collection: str, field: str, value: str
) -> None:
    dataset = deepcopy(load_demo_dataset())
    dataset.pop("manifest")
    dataset[collection][0][field] = value
    assert any("expected" in error or "id" in error for error in validate_manifest(dataset))


@pytest.mark.parametrize(
    ("field", "compensating_field"),
    [
        ("on_hand", "unaccounted"),
        ("quarantined", "unaccounted"),
        ("sold", "unaccounted"),
        ("returned", "unaccounted"),
        ("disposed", "unaccounted"),
        ("unaccounted", "on_hand"),
    ],
)
def test_validation_rejects_negative_lot_quantities_even_when_equation_balances(
    field: str, compensating_field: str
) -> None:
    dataset = deepcopy(load_demo_dataset())
    dataset.pop("manifest")
    lot = dataset["lots"][0]
    delta = lot[field] + 1
    lot[field] = -1
    lot[compensating_field] += delta
    assert any("negative" in error or "quantity" in error for error in validate_manifest(dataset))


def test_validation_parses_timestamps_and_rejects_bad_iso_values() -> None:
    dataset = deepcopy(load_demo_dataset())
    dataset.pop("manifest")
    dataset["events"][0]["occurred_at"] = "definitely-not-a-timestamp"
    assert any("timestamp" in error for error in validate_manifest(dataset))


def test_validation_rejects_unknown_event_types_and_malformed_rows() -> None:
    dataset = deepcopy(load_demo_dataset())
    dataset.pop("manifest")
    dataset["events"][0]["event_type"] = "teleport"
    assert any("event type" in error for error in validate_manifest(dataset))

    malformed = deepcopy(load_demo_dataset())
    malformed.pop("manifest")
    malformed["events"][0] = "not-a-record"
    assert validate_manifest(malformed)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda events: events[1].update(parent_event_id="EV-NOT-THERE"), "parent"),
        (lambda events: events[1].update(from_facility="DC-SOUTH"), "continuity"),
        (lambda events: events[0].update(parent_event_id="EV-002"), "cycle"),
        (lambda events: events[1].update(lot_id="LOT-PROBABLE-160"), "lot"),
    ],
)
def test_validation_rejects_broken_parent_lineage(mutation, message: str) -> None:
    dataset = deepcopy(load_demo_dataset())
    dataset.pop("manifest")
    mutation(dataset["events"])
    assert any(message in error for error in validate_manifest(dataset))


def test_configured_missing_data_path_fails_without_repository_fallback(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_recall_snapshot(data_dir=tmp_path / "missing")
    with pytest.raises(FileNotFoundError):
        load_demo_dataset(data_dir=tmp_path / "missing")
