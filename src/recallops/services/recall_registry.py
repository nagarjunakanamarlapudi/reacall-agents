"""Read-only registry service with bounded openFDA live lookup and cached fallback."""

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import httpx

from recallops.config import get_settings
from recallops.data.loaders import load_recall_snapshot
from recallops.models import RecallRecord

OPENFDA_ENFORCEMENT_URL = "https://api.fda.gov/food/enforcement.json"


class RecallRegistryService:
    def __init__(
        self,
        *,
        data_dir: Path | None = None,
        source_mode: Literal["snapshot", "live"] | None = None,
        http_transport: httpx.BaseTransport | None = None,
        timeout_seconds: float = 2.0,
    ) -> None:
        settings = get_settings()
        self.data_dir = Path(data_dir) if data_dir is not None else settings.data_dir
        self.source_mode = source_mode or settings.source_mode
        if self.source_mode not in {"snapshot", "live"}:
            raise ValueError("source_mode must be 'snapshot' or 'live'")
        if timeout_seconds <= 0 or timeout_seconds > 5:
            raise ValueError("timeout_seconds must be within (0, 5]")
        self.http_transport = http_transport
        self.timeout_seconds = timeout_seconds

    def _snapshot(self, recall_number: str, *, fallback: bool = False) -> RecallRecord:
        record = load_recall_snapshot(recall_number, data_dir=self.data_dir)
        if fallback:
            return record.model_copy(
                update={
                    "source": "openFDA Food Enforcement API (cached fallback)",
                    "cached": True,
                }
            )
        return record

    def _live(self, recall_number: str) -> RecallRecord:
        try:
            with httpx.Client(
                transport=self.http_transport,
                timeout=httpx.Timeout(self.timeout_seconds),
                headers={"User-Agent": "RecallOps-Academic-Demo/0.1"},
            ) as client:
                response = client.get(
                    OPENFDA_ENFORCEMENT_URL,
                    params={"search": f'recall_number:"{recall_number}"', "limit": "1"},
                )
                response.raise_for_status()
                payload = response.json()["results"][0]
            if payload.get("recall_number") != recall_number:
                raise ValueError("openFDA returned a different recall")
            encoded = json.dumps(
                payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
            ).encode()
            return RecallRecord(
                recall_number=recall_number,
                source="openFDA Food Enforcement API (live)",
                provenance="LIVE_OPENFDA",
                retrieved_at=datetime.now(UTC),
                payload=payload,
                sha256=hashlib.sha256(encoded).hexdigest(),
                source_url=str(response.request.url),
                cached=False,
            )
        except (httpx.HTTPError, IndexError, KeyError, TypeError, ValueError):
            return self._snapshot(recall_number, fallback=True)

    def get_recall(self, recall_number: str) -> RecallRecord | None:
        if self.source_mode == "snapshot" and recall_number != "H-1230-2026":
            return None
        record = (
            self._live(recall_number)
            if self.source_mode == "live"
            else self._snapshot(recall_number)
        )
        return record if record.recall_number == recall_number else None

    def search_recalls(self, query: str) -> list[RecallRecord]:
        record = self.get_recall("H-1230-2026")
        if record is None:
            return []
        needle = query.casefold()
        searchable = " ".join(
            str(value)
            for value in (
                record.recall_number,
                record.payload.get("product_description"),
                record.payload.get("reason_for_recall"),
            )
        ).casefold()
        return [record] if needle in searchable else []

    def get_product_metadata(self, upc: str) -> dict[str, str] | None:
        record = self.get_recall("H-1230-2026")
        if record is None:
            return None
        normalized = "".join(character for character in upc if character.isdigit())
        haystack = "".join(
            character for character in record.payload["product_description"] if character.isdigit()
        )
        if normalized in haystack:
            return {"upc": normalized, "source": "openFDA recall product description"}
        return None
