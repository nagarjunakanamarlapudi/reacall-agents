"""Small, explicit settings surface for the offline-first demo."""

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class Settings:
    data_dir: Path = Path("data")
    source_mode: str = "snapshot"


def get_settings() -> Settings:
    return Settings()
