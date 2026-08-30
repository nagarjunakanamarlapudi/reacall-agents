"""Small, explicit settings surface for the offline-first demo."""

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class Settings:
    data_dir: Path = Path("data")
    source_mode: str = "snapshot"
    operations_db_path: Path = Path(".recallops-runtime/operations.sqlite3")


def get_settings() -> Settings:
    return Settings(
        data_dir=Path(os.environ.get("RECALLOPS_DATA_DIR", "data")),
        source_mode=os.environ.get("RECALLOPS_SOURCE_MODE", "snapshot"),
        operations_db_path=Path(
            os.environ.get("RECALLOPS_OPERATIONS_DB", ".recallops-runtime/operations.sqlite3")
        ),
    )
