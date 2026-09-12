"""OpenAI model construction and sanitized provider-facing failures."""

import os

from langchain_core.language_models import BaseChatModel
from langchain_openai import ChatOpenAI
from pydantic import SecretStr

from recallops.llm.config import LLMSettings

_ERROR_MESSAGES = {
    "authentication": "OpenAI authentication failed. Check OPENAI_API_KEY.",
    "rate_limit": "OpenAI rate limit reached. Try again later.",
    "timeout": "OpenAI request timed out. Try again.",
    "invalid_response": "OpenAI returned an invalid response. Try again.",
    "provider_error": "OpenAI request failed. Try again.",
}


def build_chat_model(settings: LLMSettings) -> BaseChatModel:
    """Construct the live chat model only for a validated OpenAI configuration."""
    if settings.mode != "openai":
        raise ValueError("OpenAI chat model is unavailable in deterministic mode")

    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise ValueError("OPENAI_API_KEY must be set for RECALLOPS_MODEL_MODE=openai")

    return ChatOpenAI(
        model=settings.model,
        api_key=SecretStr(api_key),
        timeout=settings.timeout_seconds,
        max_retries=settings.max_retries,
    )


def sanitize_llm_error(error: BaseException) -> tuple[str, str]:
    """Classify provider failures without forwarding raw provider text or credentials."""
    error_name = type(error).__name__
    if error_name == "AuthenticationError":
        category = "authentication"
    elif error_name == "RateLimitError":
        category = "rate_limit"
    elif error_name == "APITimeoutError":
        category = "timeout"
    elif error_name == "APIResponseValidationError":
        category = "invalid_response"
    else:
        category = "provider_error"
    return category, _ERROR_MESSAGES[category]
