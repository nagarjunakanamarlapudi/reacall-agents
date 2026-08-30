"""Repository-relative paths used by deterministic loaders."""

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = PROJECT_ROOT / "data"
PUBLIC_DATA_DIR = DATA_DIR / "public"
DEMO_DATA_DIR = DATA_DIR / "synthetic" / "northstar_demo"
OPERATIONS_STATE_PATH = PROJECT_ROOT / ".recallops-runtime" / "operations.json"
