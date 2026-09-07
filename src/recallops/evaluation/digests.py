"""Canonical serialization and integrity digests for evaluation artifacts."""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any


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
    if not isinstance(expected, str) or not hmac.compare_digest(actual, expected):
        raise ValueError("digest mismatch")
