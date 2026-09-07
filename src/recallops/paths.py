"""Repository-relative paths used by deterministic loaders."""

from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True, slots=True)
class RepositoryPaths:
    """Resolved repository artifacts, injectable for product and test runtimes."""

    root: Path

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", Path(self.root).expanduser().resolve())

    @property
    def evaluation_report(self) -> Path:
        return self.root / "data" / "evals" / "report.json"

    @property
    def evaluation_corpus(self) -> Path:
        return self.root / "data" / "evals" / "scenarios.json"

    @property
    def retrieval_evaluation_corpus(self) -> Path:
        return self.root / "data" / "evals" / "retrieval_cases.json"

    @property
    def retrieval_evaluation_report(self) -> Path:
        return self.root / "data" / "evals" / "retrieval_report.json"


DATA_DIR = PROJECT_ROOT / "data"
PUBLIC_DATA_DIR = DATA_DIR / "public"
DEMO_DATA_DIR = DATA_DIR / "synthetic" / "northstar_demo"
OPERATIONS_STATE_PATH = PROJECT_ROOT / ".recallops-runtime" / "operations.sqlite3"
