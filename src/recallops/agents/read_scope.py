"""Pure source-row selection shared by the sealed view and independent verifier."""

from typing import Any


def validate_read_scope(scope: Any) -> tuple[str, ...]:
    if type(scope) is not tuple or len(scope) > 64:
        raise ValueError("read scope must be an exact bounded tuple")
    if any(type(item) is not str or not item.strip() for item in scope):
        raise ValueError("read scope requires exact nonblank strings")
    if len(set(scope)) != len(scope):
        raise ValueError("read scope must be unique")
    return scope


def scoped_lot_rows(rows: list[dict[str, Any]], scope: tuple[str, ...]) -> list[dict[str, Any]]:
    """Preserve source ordering and values; never classify or manufacture a row."""
    validate_read_scope(scope)
    if not scope:
        return rows
    requested = set(scope)
    selected = [row for row in rows if row["lot_id"] in requested]
    if {row["lot_id"] for row in selected} != requested:
        raise ValueError("read scope is unavailable in the source")
    return selected
