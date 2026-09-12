"""Configuration and provider-boundary tests for the optional live model lane."""

from pathlib import Path

import pytest

from recallops.llm.config import get_llm_settings, load_project_env
from recallops.llm.openai_provider import build_chat_model, sanitize_llm_error


def _clear_llm_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "RECALLOPS_MODEL_MODE",
        "OPENAI_API_KEY",
        "OPENAI_MODEL",
        "OPENAI_EMBEDDING_MODEL",
        "OPENAI_TIMEOUT_SECONDS",
        "OPENAI_MAX_RETRIES",
    ):
        monkeypatch.delenv(name, raising=False)


def test_settings_default_to_credential_free_deterministic_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """This catches an accidental live-provider default in credential-free environments."""
    _clear_llm_environment(monkeypatch)

    settings = get_llm_settings()

    assert settings.mode == "deterministic"
    assert settings.provider == "openai"
    assert settings.model == ""
    assert settings.embedding_model == "text-embedding-3-small"
    assert settings.timeout_seconds == 120.0
    assert settings.max_retries == 0


@pytest.mark.parametrize(
    ("environment", "missing_name"),
    [
        ({"RECALLOPS_MODEL_MODE": "openai", "OPENAI_MODEL": "gpt-4.1-mini"}, "OPENAI_API_KEY"),
        ({"RECALLOPS_MODEL_MODE": "openai", "OPENAI_API_KEY": "credential-marker"}, "OPENAI_MODEL"),
    ],
)
def test_openai_mode_requires_nonblank_credential_and_model(
    monkeypatch: pytest.MonkeyPatch,
    environment: dict[str, str],
    missing_name: str,
) -> None:
    """This catches live mode starting without the minimum safe configuration."""
    _clear_llm_environment(monkeypatch)
    for name, value in environment.items():
        monkeypatch.setenv(name, value)

    with pytest.raises(ValueError, match=missing_name) as error:
        get_llm_settings()

    assert "credential-marker" not in str(error.value)


def test_process_environment_wins_over_repository_env_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """This catches local .env values overriding deployment-supplied configuration."""
    _clear_llm_environment(monkeypatch)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "RECALLOPS_MODEL_MODE=openai\n"
        "OPENAI_API_KEY=file-credential-marker\n"
        "OPENAI_MODEL=file-model\n"
        "OPENAI_EMBEDDING_MODEL=file-embedding\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENAI_API_KEY", "process-credential-marker")
    monkeypatch.setenv("OPENAI_MODEL", "process-model")

    load_project_env(env_file)
    settings = get_llm_settings()

    assert settings.mode == "openai"
    assert settings.model == "process-model"
    assert settings.embedding_model == "file-embedding"


def test_openai_model_construction_keeps_credential_out_of_representation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """This catches provider construction that stores a credential as plain text."""
    _clear_llm_environment(monkeypatch)
    monkeypatch.setenv("RECALLOPS_MODEL_MODE", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "credential-marker")
    monkeypatch.setenv("OPENAI_MODEL", "gpt-4.1-mini")

    model = build_chat_model(get_llm_settings())

    assert model.model_name == "gpt-4.1-mini"
    assert model.request_timeout == 120.0
    assert model.max_retries == 0
    assert "credential-marker" not in repr(model)


def test_explicit_timeout_and_retry_environment_reaches_actual_client(monkeypatch):
    _clear_llm_environment(monkeypatch)
    monkeypatch.setenv("RECALLOPS_MODEL_MODE", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "credential-marker")
    monkeypatch.setenv("OPENAI_MODEL", "gpt-5-mini")
    monkeypatch.setenv("OPENAI_TIMEOUT_SECONDS", "90.5")
    monkeypatch.setenv("OPENAI_MAX_RETRIES", "1")
    model = build_chat_model(get_llm_settings())
    assert model.request_timeout == 90.5
    assert model.max_retries == 1


@pytest.mark.parametrize(
    "name,value",
    [
        ("OPENAI_TIMEOUT_SECONDS", value)
        for value in ("", "0", "-1", "nan", "inf", "601", "true", "credential-marker")
    ]
    + [
        ("OPENAI_MAX_RETRIES", value)
        for value in ("", "-1", "1.0", "4", "true", "credential-marker")
    ],
)
def test_invalid_timeout_retry_values_fail_without_echo(monkeypatch, name, value):
    _clear_llm_environment(monkeypatch)
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError) as error:
        get_llm_settings()
    assert name in str(error.value)
    assert "credential-marker" not in str(error.value)


@pytest.mark.parametrize(
    ("error_name", "category"),
    [
        ("AuthenticationError", "authentication"),
        ("RateLimitError", "rate_limit"),
        ("APITimeoutError", "timeout"),
        ("OpenAITimeoutError", "timeout"),
        ("APIResponseValidationError", "invalid_response"),
        ("RuntimeError", "provider_error"),
    ],
)
def test_provider_errors_are_categorized_without_exposing_their_text(
    error_name: str, category: str
) -> None:
    """This catches an error boundary that could surface provider or credential text."""
    error = type(error_name, (Exception,), {})("credential-marker")

    actual_category, display_message = sanitize_llm_error(error)

    assert actual_category == category
    assert "credential-marker" not in display_message
