"""Strict, credential-free configuration for optional live model use."""

import math
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv

from recallops.paths import PROJECT_ROOT

_DEFAULT_EMBEDDING_MODEL = "text-embedding-3-small"
_DEFAULT_TIMEOUT_SECONDS = 30.0
_DEFAULT_MAX_RETRIES = 2


@dataclass(frozen=True, slots=True)
class LLMSettings:
    """Non-secret settings shared by the live UI and evaluation lanes."""

    mode: Literal["deterministic", "openai"] = "deterministic"
    provider: Literal["openai"] = "openai"
    model: str = ""
    embedding_model: str = _DEFAULT_EMBEDDING_MODEL
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS
    max_retries: int = _DEFAULT_MAX_RETRIES

    def __post_init__(self) -> None:
        if self.mode not in {"deterministic", "openai"}:
            raise ValueError("RECALLOPS_MODEL_MODE must be 'deterministic' or 'openai'")
        if self.provider != "openai":
            raise ValueError("RECALLOPS model provider must be 'openai'")
        if self.mode == "openai" and not self.model.strip():
            raise ValueError("OPENAI_MODEL must be set for RECALLOPS_MODEL_MODE=openai")
        if not self.embedding_model.strip():
            raise ValueError("OPENAI_EMBEDDING_MODEL must be nonblank")
        if not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be a positive finite number")
        if type(self.max_retries) is not int or self.max_retries < 0:
            raise ValueError("max_retries must be a non-negative integer")


def load_project_env(path: Path = PROJECT_ROOT / ".env") -> None:
    """Load the repository-local environment without replacing process configuration."""
    load_dotenv(dotenv_path=path, override=False)


def get_llm_settings() -> LLMSettings:
    """Return validated, non-secret provider settings from the process environment."""
    mode = os.environ.get("RECALLOPS_MODEL_MODE", "deterministic").strip().lower()
    model = os.environ.get("OPENAI_MODEL", "").strip()
    embedding_model = os.environ.get("OPENAI_EMBEDDING_MODEL", _DEFAULT_EMBEDDING_MODEL).strip()

    if mode not in {"deterministic", "openai"}:
        raise ValueError("RECALLOPS_MODEL_MODE must be 'deterministic' or 'openai'")
    if mode == "openai":
        _require_nonblank_environment("OPENAI_API_KEY")
        if not model:
            raise ValueError("OPENAI_MODEL must be set for RECALLOPS_MODEL_MODE=openai")

    return LLMSettings(
        mode=mode,
        model=model,
        embedding_model=embedding_model,
    )


def _require_nonblank_environment(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ValueError(f"{name} must be set for RECALLOPS_MODEL_MODE=openai")
    return value
