"""Strict, credential-free configuration for optional live model use."""

import math
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv

from recallops.paths import PROJECT_ROOT

_DEFAULT_EMBEDDING_MODEL = "text-embedding-3-small"
_DEFAULT_TIMEOUT_SECONDS = 120.0
_DEFAULT_MAX_RETRIES = 0
_DEFAULT_REASONING_EFFORT = "medium"
_REASONING_EFFORTS = frozenset({"none", "low", "medium", "high", "xhigh"})


@dataclass(frozen=True, slots=True)
class LLMSettings:
    """Non-secret settings shared by the live UI and evaluation lanes."""

    mode: Literal["deterministic", "openai"] = "deterministic"
    provider: Literal["openai"] = "openai"
    model: str = ""
    embedding_model: str = _DEFAULT_EMBEDDING_MODEL
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS
    max_retries: int = _DEFAULT_MAX_RETRIES
    reasoning_effort: Literal["none", "low", "medium", "high", "xhigh"] = _DEFAULT_REASONING_EFFORT

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
        if (
            type(self.reasoning_effort) is not str
            or self.reasoning_effort not in _REASONING_EFFORTS
        ):
            raise ValueError("OPENAI_REASONING_EFFORT must be none, low, medium, high, or xhigh")


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
        timeout_seconds=_timeout_environment(),
        max_retries=_retry_environment(),
        reasoning_effort=os.environ.get(
            "OPENAI_REASONING_EFFORT", _DEFAULT_REASONING_EFFORT
        ).strip(),
    )


def _timeout_environment() -> float:
    raw = os.environ.get("OPENAI_TIMEOUT_SECONDS", str(_DEFAULT_TIMEOUT_SECONDS)).strip()
    if re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", raw):
        value = float(raw)
        if math.isfinite(value) and 0 < value <= 600:
            return value
    raise ValueError("OPENAI_TIMEOUT_SECONDS must be a finite decimal within (0, 600]")


def _retry_environment() -> int:
    raw = os.environ.get("OPENAI_MAX_RETRIES", str(_DEFAULT_MAX_RETRIES)).strip()
    if re.fullmatch(r"[0-3]", raw):
        return int(raw)
    raise ValueError("OPENAI_MAX_RETRIES must be an integer from 0 through 3")


def _require_nonblank_environment(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ValueError(f"{name} must be set for RECALLOPS_MODEL_MODE=openai")
    return value
