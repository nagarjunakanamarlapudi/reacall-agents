"""Deterministic corpus construction from frozen public and synthetic records."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from recallops.config import get_settings
from recallops.data.loaders import (
    PINNED_OPENFDA_METADATA_SHA256,
    load_demo_dataset,
    load_recall_snapshot,
)
from recallops.models import OpenFDASnapshotMetadata
from recallops.paths import DATA_DIR
from recallops.retrieval.models import KnowledgeDocument, KnowledgeManifest

TRUSTED_POLICY_CORPUS_SHA256 = "e698fc4e113724b7e6819d8d70ea411d54cb777e32126a4218c417dac4798453"
TRUSTED_OPENFDA_SNAPSHOT_SHA256 = "086c80b789959dc0612f4d94ca4f199da621158416784a3e1ed0eeeecc260aa9"
TRUSTED_OPENFDA_METADATA_SHA256 = PINNED_OPENFDA_METADATA_SHA256
TRUSTED_SYNTHETIC_DATASET_SHA256 = (
    "6f60ce4a3119aae2d68b3ea3c5105d79cc0df9fd335c2c2132218a886f5c61d9"
)
TRUSTED_SYNTHETIC_MANIFEST_SHA256 = (
    "2356b37e583031e22512ec54472bb2336c0ae8c003addc7a6e0f8f78d85690ea"
)
TRUSTED_RAW_SOURCE_SHA256 = {
    "policy_corpus.json": TRUSTED_POLICY_CORPUS_SHA256,
    "public/H-1230-2026.json": TRUSTED_OPENFDA_SNAPSHOT_SHA256,
    "public/H-1230-2026.metadata.json": TRUSTED_OPENFDA_METADATA_SHA256,
    "synthetic/northstar_demo/dataset.json": TRUSTED_SYNTHETIC_DATASET_SHA256,
    "synthetic/northstar_demo/manifest.json": TRUSTED_SYNTHETIC_MANIFEST_SHA256,
}

SYNTHETIC_COLLECTIONS = {
    "products": ("product", "product_id"),
    "lots": ("lot", "lot_id"),
    "facilities": ("facility", "facility_id"),
    "events": ("event", "event_id"),
    "inventory_positions": ("inventory_position", "position_id"),
    "supplier_shipments": ("supplier_shipment", "shipment_id"),
    "facility_acknowledgements": ("facility_acknowledgement", "facility_id"),
    "cases": ("case", "case_id"),
    "tasks": ("task", "task_id"),
    "audit_receipts": ("audit_receipt", "receipt_id"),
}
GENERATED_AT = datetime(2026, 8, 30, tzinfo=UTC)


def _canonical_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n"
    ).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _document(**kwargs: Any) -> KnowledgeDocument:
    text = kwargs.pop("text").strip()
    return KnowledgeDocument(
        **kwargs,
        text=text,
        content_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
    )


def _load_policy_documents(policy_path: Path) -> list[KnowledgeDocument]:
    payload = json.loads(policy_path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("policy corpus must be a JSON list")
    return [KnowledgeDocument.model_validate(item) for item in payload]


def _openfda_documents(data_dir: Path) -> list[KnowledgeDocument]:
    snapshot_path = data_dir / "public" / "H-1230-2026.json"
    metadata = OpenFDASnapshotMetadata.model_validate_json(
        (data_dir / "public" / "H-1230-2026.metadata.json").read_text(encoding="utf-8")
    )
    payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
    rows = payload.get("results")
    if not isinstance(rows, list):
        raise ValueError("frozen openFDA snapshot must contain a results list")
    documents: list[KnowledgeDocument] = []
    for row in rows:
        recall_number = str(row.get("recall_number") or "").strip()
        event_id = str(row.get("event_id") or "unknown").strip()
        record_id = recall_number or f"EVENT-{event_id}"
        fields = (
            "recall_number",
            "event_id",
            "classification",
            "status",
            "product_type",
            "recalling_firm",
            "product_description",
            "product_quantity",
            "reason_for_recall",
            "code_info",
            "distribution_pattern",
            "voluntary_mandated",
            "initial_firm_notification",
            "recall_initiation_date",
            "center_classification_date",
            "report_date",
        )
        text = "OpenFDA food enforcement record. " + " ".join(
            f"{field.replace('_', ' ')}: {row[field]}"
            for field in fields
            if row.get(field) not in (None, "")
        )
        citation_id = f"OPENFDA-{recall_number}" if recall_number else f"OPENFDA-EVENT-{event_id}"
        documents.append(
            _document(
                citation_id=citation_id,
                source_class="official",
                origin="OFFICIAL_OPENFDA_SNAPSHOT",
                audience_label="OFFICIAL — openFDA frozen snapshot",
                record_type="openfda_recall",
                record_id=record_id,
                title=f"openFDA food enforcement record {record_id}",
                text=text,
                source_url=metadata.capture_url,
                retrieved_at=metadata.retrieved_at,
                metadata={
                    "event_id": event_id,
                    "recall_number": recall_number,
                    "snapshot_sha256": metadata.sha256,
                },
            )
        )
    return documents


def _synthetic_documents(
    data_dir: Path,
    *,
    validated_dataset: dict[str, Any] | None = None,
) -> list[KnowledgeDocument]:
    dataset = validated_dataset or json.loads(
        (data_dir / "synthetic" / "northstar_demo" / "dataset.json").read_text(encoding="utf-8")
    )
    documents: list[KnowledgeDocument] = []
    for collection, (record_type, id_field) in SYNTHETIC_COLLECTIONS.items():
        rows = dataset.get(collection)
        if not isinstance(rows, list):
            raise ValueError(f"synthetic dataset is missing {collection}")
        for row in rows:
            record_id = str(row[id_field])
            readable = " ".join(
                f"{key.replace('_', ' ')}: {value}"
                for key, value in sorted(row.items())
                if key != "origin"
            )
            text = (
                f"Northstar Grocers synthetic {record_type}. {readable}. SYNTHETIC — ACADEMIC DEMO."
            )
            documents.append(
                _document(
                    citation_id=f"NORTHSTAR-{collection.upper()}-{record_id}",
                    source_class="synthetic",
                    origin="SYNTHETIC_RETAILER_DIGITAL_TWIN",
                    audience_label="SYNTHETIC — ACADEMIC DEMO",
                    record_type=record_type,
                    record_id=record_id,
                    title=f"Northstar {record_type} {record_id}",
                    text=text,
                    source_url=f"recallops://synthetic/{collection}/{record_id}",
                    retrieved_at=GENERATED_AT,
                    metadata={"collection": collection, id_field: record_id},
                )
            )
    return documents


def _build_documents(
    data_dir: Path,
    knowledge_dir: Path,
    *,
    validated_dataset: dict[str, Any] | None = None,
) -> tuple[KnowledgeDocument, ...]:
    documents = (
        _openfda_documents(data_dir)
        + _load_policy_documents(knowledge_dir / "policy_corpus.json")
        + _synthetic_documents(data_dir, validated_dataset=validated_dataset)
    )
    ordered = tuple(sorted(documents, key=lambda item: item.citation_id))
    citation_ids = [item.citation_id for item in ordered]
    if len(citation_ids) != len(set(citation_ids)):
        raise ValueError("knowledge corpus contains duplicate citation IDs")
    return ordered


def _validate_trusted_sources(data_dir: Path, knowledge_dir: Path) -> dict[str, Any]:
    paths = {
        "policy_corpus.json": knowledge_dir / "policy_corpus.json",
        "public/H-1230-2026.json": data_dir / "public" / "H-1230-2026.json",
        "public/H-1230-2026.metadata.json": (data_dir / "public" / "H-1230-2026.metadata.json"),
        "synthetic/northstar_demo/dataset.json": (
            data_dir / "synthetic" / "northstar_demo" / "dataset.json"
        ),
        "synthetic/northstar_demo/manifest.json": (
            data_dir / "synthetic" / "northstar_demo" / "manifest.json"
        ),
    }
    for name, expected in TRUSTED_RAW_SOURCE_SHA256.items():
        actual = _sha256_bytes(paths[name].read_bytes())
        if actual != expected:
            raise ValueError(f"{name} failed its independent trust anchor")
    # Run the existing domain validators only after the independently reviewed
    # raw bytes are authenticated.
    load_recall_snapshot(data_dir=data_dir)
    return load_demo_dataset(data_dir)


def _validate_manifest_anchors(manifest: KnowledgeManifest) -> None:
    for name, expected in TRUSTED_RAW_SOURCE_SHA256.items():
        if manifest.source_checksums.get(name) != expected:
            raise ValueError(f"knowledge manifest differs from independent trust anchor: {name}")


def _computed_manifest(
    documents: tuple[KnowledgeDocument, ...], data_dir: Path, knowledge_dir: Path
) -> KnowledgeManifest:
    source_paths = {
        "policy_corpus.json": knowledge_dir / "policy_corpus.json",
        "public/H-1230-2026.json": data_dir / "public" / "H-1230-2026.json",
        "public/H-1230-2026.metadata.json": (data_dir / "public" / "H-1230-2026.metadata.json"),
        "synthetic/northstar_demo/dataset.json": (
            data_dir / "synthetic" / "northstar_demo" / "dataset.json"
        ),
        "synthetic/northstar_demo/manifest.json": (
            data_dir / "synthetic" / "northstar_demo" / "manifest.json"
        ),
    }
    corpus_bytes = _canonical_bytes([item.model_dump(mode="json") for item in documents])
    return KnowledgeManifest(
        schema_name="recallops.hybrid-knowledge-corpus",
        schema_version="1.0.0",
        generated_at=GENERATED_AT,
        document_count=len(documents),
        source_counts=dict(sorted(Counter(item.source_class for item in documents).items())),
        record_type_counts=dict(sorted(Counter(item.record_type for item in documents).items())),
        source_checksums={
            name: _sha256_bytes(path.read_bytes()) for name, path in sorted(source_paths.items())
        },
        corpus_sha256=_sha256_bytes(corpus_bytes),
    )


def build_knowledge_artifacts(
    *, data_dir: Path | None = None, knowledge_dir: Path | None = None
) -> dict[str, bytes]:
    """Build committed knowledge metadata twice identically without writing files."""

    resolved_data = Path(data_dir) if data_dir is not None else DATA_DIR
    resolved_knowledge = (
        Path(knowledge_dir) if knowledge_dir is not None else resolved_data / "knowledge"
    )
    validated_dataset = _validate_trusted_sources(resolved_data, resolved_knowledge)
    policy_bytes = (resolved_knowledge / "policy_corpus.json").read_bytes()
    documents = _build_documents(
        resolved_data,
        resolved_knowledge,
        validated_dataset=validated_dataset,
    )
    manifest = _computed_manifest(documents, resolved_data, resolved_knowledge)
    _validate_manifest_anchors(manifest)
    return {
        "policy_corpus.json": policy_bytes,
        "manifest.json": _canonical_bytes(manifest.model_dump(mode="json")),
    }


class KnowledgeCorpus:
    """Validated corpus plus constant-time citation resolution."""

    def __init__(
        self,
        documents: tuple[KnowledgeDocument, ...],
        manifest: KnowledgeManifest,
        *,
        data_dir: Path,
    ) -> None:
        self.documents = documents
        self.manifest = manifest
        self.data_dir = data_dir
        self._by_citation = {item.citation_id: item for item in documents}

    @classmethod
    def load(
        cls,
        *,
        data_dir: Path | None = None,
        knowledge_dir: Path | None = None,
    ) -> KnowledgeCorpus:
        settings = get_settings()
        resolved_data = Path(data_dir) if data_dir is not None else settings.data_dir
        resolved_knowledge = (
            Path(knowledge_dir) if knowledge_dir is not None else resolved_data / "knowledge"
        )
        validated_dataset = _validate_trusted_sources(resolved_data, resolved_knowledge)
        documents = _build_documents(
            resolved_data,
            resolved_knowledge,
            validated_dataset=validated_dataset,
        )
        computed = _computed_manifest(documents, resolved_data, resolved_knowledge)
        committed = KnowledgeManifest.model_validate_json(
            (resolved_knowledge / "manifest.json").read_text(encoding="utf-8")
        )
        _validate_manifest_anchors(committed)
        if committed != computed:
            raise ValueError("knowledge manifest does not match corpus inputs")
        return cls(documents, committed, data_dir=resolved_data)

    def resolve(self, citation_id: str) -> KnowledgeDocument:
        try:
            return self._by_citation[citation_id]
        except KeyError as error:
            raise KeyError(f"unknown citation ID {citation_id!r}") from error
