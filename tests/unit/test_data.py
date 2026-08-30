from copy import deepcopy
from pathlib import Path

import pytest

from recallops.data.generator import generate_demo_dataset
from recallops.data.loaders import load_demo_dataset, load_recall_snapshot, validate_manifest


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
    assert len(dataset["products"]) == 4
    assert len(dataset["lots"]) == 6
    assert validate_manifest(dataset) == []
    assert {lot["classification"] for lot in dataset["lots"]} == {
        "exact",
        "probable",
        "ambiguous",
        "rejected",
    }


def test_demo_generation_is_deterministic_and_preserves_quantity_fixture(tmp_path: Path) -> None:
    first = generate_demo_dataset(output_dir=tmp_path / "one", seed=20260830)
    second = generate_demo_dataset(output_dir=tmp_path / "two", seed=20260830)

    assert first["manifest"]["sha256"] == second["manifest"]["sha256"]
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
