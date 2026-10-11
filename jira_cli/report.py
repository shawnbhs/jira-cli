"""The one report shape every report function in this package returns.

Having a single shape is what lets ``output.render_report`` and the
``md``/``csv``/``json`` exporters stay ignorant of what any particular report
means. Rows are flattened to JSON-serialisable scalars here, once, so no
exporter has to guess what to do with a nested dict.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

SCALARS = (str, int, float, bool)


def _as_scalar(value: Any) -> str | int | float | bool | None:
    """Coerce a cell to a JSON-serialisable scalar.

    Lists and tuples become comma-joined text (that is how they read in a
    table and a CSV alike); anything else exotic is stringified. ``bool`` is
    checked before ``int`` only implicitly — both are allowed through as-is.
    """
    if value is None or isinstance(value, SCALARS):
        return value
    if isinstance(value, (list, tuple, set, frozenset)):
        return ", ".join(str(item) for item in value)
    if isinstance(value, dict):
        return ", ".join(f"{k}={v}" for k, v in value.items())
    return str(value)


def utc_now_iso() -> str:
    """Current UTC time as ISO-8601 with a ``Z`` suffix, to the second."""
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def make_report(
    title: str,
    columns: list[str],
    rows: list[dict],
    *,
    summary: dict | None = None,
    meta: dict | None = None,
) -> dict:
    """Build the shared report dict.

    Every row is reduced to exactly ``columns``: a missing key becomes
    ``None`` and an extra key is dropped, so the table, CSV and JSON renderings
    can never disagree about the shape.

    Args:
        title: Human-readable report title.
        columns: Column names, in display order.
        rows: One dict per row, keyed by column name.
        summary: Aggregates (totals, counts) shown under the table.
        meta: Provenance. ``generated_at`` is always added; callers normally
            add ``jql`` so a reader can reproduce the numbers.

    Returns:
        ``{"title", "columns", "rows", "summary", "meta"}``.
    """
    ordered = [str(name) for name in (columns or [])]
    clean_rows: list[dict] = []
    for row in rows or []:
        source = row if isinstance(row, dict) else {}
        clean_rows.append({name: _as_scalar(source.get(name)) for name in ordered})

    report_meta: dict[str, Any] = {"generated_at": utc_now_iso()}
    report_meta.update(meta or {})

    return {
        "title": str(title),
        "columns": ordered,
        "rows": clean_rows,
        "summary": dict(summary or {}),
        "meta": report_meta,
    }


__all__ = ["make_report", "utc_now_iso"]
