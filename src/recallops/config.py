"""Small, explicit settings surface for the offline-first demo."""

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from recallops.paths import DATA_DIR, OPERATIONS_STATE_PATH


@dataclass(frozen=True, slots=True)
class Settings:
    data_dir: Path = DATA_DIR
    source_mode: Literal["snapshot", "live"] = "snapshot"
    operations_db_path: Path = OPERATIONS_STATE_PATH


def get_settings() -> Settings:
    source_mode = os.environ.get("RECALLOPS_SOURCE_MODE", "snapshot")
    if source_mode not in {"snapshot", "live"}:
        raise ValueError("RECALLOPS_SOURCE_MODE must be 'snapshot' or 'live'")
    return Settings(
        data_dir=Path(os.environ.get("RECALLOPS_DATA_DIR", DATA_DIR)).expanduser().resolve(),
        source_mode=source_mode,
        operations_db_path=Path(os.environ.get("RECALLOPS_OPERATIONS_DB", OPERATIONS_STATE_PATH))
        .expanduser()
        .resolve(),
    )
