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

    @property
    def orchestration_evaluation_corpus(self) -> Path:
        return self.root / "data" / "evals" / "orchestration_cases.json"

    @property
    def orchestration_evaluation_report(self) -> Path:
        return self.root / "data" / "evals" / "orchestration_report.json"

    @property
    def evaluation_scorecard(self) -> Path:
        return self.root / "data" / "evals" / "scorecard.json"


@dataclass(frozen=True, slots=True)
class EvaluationArtifactPaths:
    """Trusted input locations; corpus defaults are adjacent to each report.

    Resolve once, before callers change cwd. Artifact content never chooses paths.
    """

    safety_report: Path = PROJECT_ROOT / "data/evals/report.json"
    retrieval_report: Path = PROJECT_ROOT / "data/evals/retrieval_report.json"
    orchestration_report: Path = PROJECT_ROOT / "data/evals/orchestration_report.json"
    safety_corpus: Path | None = None
    retrieval_corpus: Path | None = None
    orchestration_corpus: Path | None = None

    def __post_init__(self) -> None:
        for suite, filename in (
            ("safety", "scenarios.json"),
            ("retrieval", "retrieval_cases.json"),
            ("orchestration", "orchestration_cases.json"),
        ):
            report = Path(getattr(self, f"{suite}_report")).expanduser().resolve()
            corpus = getattr(self, f"{suite}_corpus") or report.with_name(filename)
            object.__setattr__(self, f"{suite}_report", report)
            object.__setattr__(self, f"{suite}_corpus", Path(corpus).expanduser().resolve())


DATA_DIR = PROJECT_ROOT / "data"
PUBLIC_DATA_DIR = DATA_DIR / "public"
DEMO_DATA_DIR = DATA_DIR / "synthetic" / "northstar_demo"
OPERATIONS_STATE_PATH = PROJECT_ROOT / ".recallops-runtime" / "operations.sqlite3"
