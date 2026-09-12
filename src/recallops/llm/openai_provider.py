"""OpenAI model construction and sanitized provider-facing failures."""

import os

from langchain_core.language_models import BaseChatModel
from langchain_openai import ChatOpenAI
from openai import BaseModel as OpenAIResponse
from pydantic import SecretStr

from recallops.llm.config import LLMSettings
from recallops.llm.strict_json import load_bounded_json

_ERROR_MESSAGES = {
    "authentication": "OpenAI authentication failed. Check OPENAI_API_KEY.",
    "rate_limit": "OpenAI rate limit reached. Try again later.",
    "timeout": "OpenAI request timed out. Try again.",
    "invalid_response": "OpenAI returned an invalid response. Try again.",
    "provider_error": "OpenAI request failed. Try again.",
}


class _StrictChatOpenAI(ChatOpenAI):
    """Validate argument strings before LangChain discards the provider originals.

    The live factory pins non-streaming Chat Completions so both sync and async
    invocations cross this boundary before tool dispatch or ToolStrategy parsing.
    """

    def _create_chat_result(self, response, generation_info=None):
        if not isinstance(response, (dict, OpenAIResponse)):
            raise ValueError("invalid provider response envelope")
        raw = response if isinstance(response, dict) else response.model_dump()
        try:
            for choice in raw.get("choices") or ():
                message = choice["message"]
                for call in message.get("tool_calls") or ():
                    if (
                        call.get("type") != "function"
                        or type(load_bounded_json(call["function"]["arguments"])) is not dict
                    ):
                        raise ValueError("structured tool arguments must be a JSON object")
                if message.get("function_call") is not None:
                    raise ValueError("legacy function responses are not supported")
        except (KeyError, TypeError, AttributeError):
            raise ValueError("invalid provider response envelope") from None
        return super()._create_chat_result(response, generation_info)


def build_chat_model(settings: LLMSettings) -> BaseChatModel:
    """Construct the live chat model only for a validated OpenAI configuration."""
    if settings.mode != "openai":
        raise ValueError("OpenAI chat model is unavailable in deterministic mode")

    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise ValueError("OPENAI_API_KEY must be set for RECALLOPS_MODEL_MODE=openai")

    return _StrictChatOpenAI(
        model=settings.model,
        api_key=SecretStr(api_key),
        timeout=settings.timeout_seconds,
        max_retries=settings.max_retries,
        use_responses_api=False,
        disable_streaming=True,
        streaming=False,
    )


def sanitize_llm_error(error: BaseException) -> tuple[str, str]:
    """Classify provider failures without forwarding raw provider text or credentials."""
    error_name = type(error).__name__
    if error_name == "AuthenticationError":
        category = "authentication"
    elif error_name == "RateLimitError":
        category = "rate_limit"
    elif error_name in {"APITimeoutError", "OpenAITimeoutError"}:
        category = "timeout"
    elif error_name == "APIResponseValidationError":
        category = "invalid_response"
    else:
        category = "provider_error"
    return category, _ERROR_MESSAGES[category]
