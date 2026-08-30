"""Read-only registry service for the frozen official recall snapshot."""

from recallops.data.loaders import load_recall_snapshot
from recallops.models import RecallRecord


class RecallRegistryService:
    def get_recall(self, recall_number: str) -> RecallRecord | None:
        record = load_recall_snapshot()
        return record if record.recall_number == recall_number else None

    def search_recalls(self, query: str) -> list[RecallRecord]:
        record = load_recall_snapshot()
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
        record = load_recall_snapshot()
        normalized = "".join(character for character in upc if character.isdigit())
        if normalized in "".join(
            character
            for character in record.payload["product_description"]
            if character.isdigit() or character == " "
        ):
            return {"upc": normalized, "source": "openFDA recall product description"}
        return None
