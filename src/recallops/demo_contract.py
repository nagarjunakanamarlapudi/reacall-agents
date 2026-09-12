"""Shared, explicit flagship scope for bounded UI and live-smoke investigations."""

from typing import Any

from recallops.data.loaders import load_demo_dataset

FLAGSHIP_RECALL_NUMBER = "H-1230-2026"
FLAGSHIP_SCOPE_LOT_IDS = (
    "LOT-EXACT-170",
    "LOT-PROBABLE-160",
    "LOT-AMBIG-175",
    "LOT-REJECT-190",
)
MAX_INVESTIGATION_LOTS = 64


def investigation_lot_options() -> tuple[str, ...]:
    """Expose the complete synthetic dataset without expanding the active scope."""
    return tuple(sorted(row["lot_id"] for row in load_demo_dataset()["lots"]))


def validate_investigation_scope(scope: Any) -> list[str]:
    """Reject an unbounded or invalid UI selection before starting a runtime."""
    if type(scope) is not list or not 1 <= len(scope) <= MAX_INVESTIGATION_LOTS:
        raise ValueError("Choose between 1 and 64 lots for this investigation.")
    if any(type(item) is not str for item in scope):
        raise ValueError("Choose lot IDs from the available synthetic dataset.")
    if len(set(scope)) != len(scope):
        raise ValueError("Choose each investigation lot only once.")
    if set(scope) - set(investigation_lot_options()):
        raise ValueError("Choose lot IDs from the available synthetic dataset.")
    return list(scope)
