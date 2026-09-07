"""Canonical serialization and integrity digests for evaluation artifacts."""

from __future__ import annotations

import hashlib
import hmac
import json
from contextlib import contextmanager
from typing import Any

from pydantic import ValidationError


def decode_artifact_bytes(raw: bytes) -> Any:
    """Decode immutable input without leaking its contents in parsing errors."""
    if type(raw) is not bytes:
        raise ValueError("artifact input must be immutable bytes")

    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    try:
        payload = json.loads(raw, object_pairs_hook=unique_object)
    except (UnicodeError, json.JSONDecodeError, RecursionError):
        raise ValueError("artifact is not valid UTF-8 JSON") from None
    canonical_json_bytes(payload)
    return payload


@contextmanager
def artifact_validation(label: str):
    """Bound public report errors and suppress raw schema/observation payloads."""
    try:
        yield
    except ValidationError:
        raise ValueError(f"{label} schema invalid") from None
    except (TypeError, AttributeError, KeyError, IndexError, OverflowError, RecursionError):
        raise ValueError(f"{label} observation invalid") from None
    except OSError:
        raise ValueError(f"{label} evidence could not be read") from None
    except ValueError as exc:
        # Validator diagnostics before ':' are bounded labels; details may contain
        # user-authored identifiers. Never propagate those details or trace payloads.
        message = str(exc).split("\n", 1)[0].split(":", 1)[0][:160]
        raise ValueError(f"{label}: {message}") from None


def canonical_json_bytes(value: Any) -> bytes:
    """Serialize JSON deterministically as UTF-8, terminated by one newline."""
    try:
        serialized = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("value cannot be represented as canonical JSON") from exc
    return (serialized + "\n").encode("utf-8")


def canonical_sha256(value: Any) -> str:
    """Return the SHA-256 hex digest of a canonical JSON value."""
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def verify_sha256(value: Any, expected: str) -> None:
    """Raise ``ValueError`` unless ``expected`` matches the canonical digest."""
    actual = canonical_sha256(value)
    valid_shape = (
        isinstance(expected, str)
        and len(expected) == 64
        and all(character in "0123456789abcdefABCDEF" for character in expected)
    )
    if not valid_shape or not hmac.compare_digest(actual, expected.lower()):
        raise ValueError("digest mismatch")
