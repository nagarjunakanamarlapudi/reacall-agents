"""Reject ambiguous provider JSON before any last-key-wins normalization."""

import json
import math


def load_bounded_json(value: str):
    """Parse raw arguments/task results without dropping duplicate object members."""
    if type(value) is not str or len(value) > 262_144:
        raise ValueError("structured response exceeds budget")

    def unique_object(pairs):
        result = {}
        for key, item in pairs:
            if key in result:
                raise ValueError("structured response contains duplicate JSON keys")
            result[key] = item
        return result

    def reject_constant(_):
        raise ValueError("structured response contains a nonfinite number")

    try:
        result = json.loads(value, object_pairs_hook=unique_object, parse_constant=reject_constant)
    except (json.JSONDecodeError, RecursionError) as error:
        raise ValueError("structured response is not bounded JSON") from error

    def bounded(item, depth=0):
        if depth > 24:
            raise ValueError("structured response nesting exceeds budget")
        if isinstance(item, (list, dict)):
            if len(item) > 512:
                raise ValueError("structured response collection exceeds budget")
            for child in item.values() if isinstance(item, dict) else item:
                bounded(child, depth + 1)
        elif isinstance(item, float) and not math.isfinite(item):
            raise ValueError("structured response contains a nonfinite number")

    bounded(result)
    return result
