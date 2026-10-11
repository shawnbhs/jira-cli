"""Duration and relative-date parsing in Jira's own vocabulary.

Jira counts a *day* as ``hours_per_day`` (8 by default) and a *week* as
``days_per_week`` days (5), not 24h and 7d. Both rates come from
:class:`jira_cli.config.Settings` so a team on a 6-hour day gets correct
numbers without patching code.
"""

from __future__ import annotations

import re
from datetime import date, timedelta

from .errors import InputError

SECONDS_PER_MINUTE = 60
MINUTES_PER_HOUR = 60
SECONDS_PER_HOUR = SECONDS_PER_MINUTE * MINUTES_PER_HOUR
DAYS_PER_MONTH = 30
"""A "month" in a relative date is exactly 30 days — reports must be stable."""

_UNIT_RE = re.compile(r"(?i)(\d+(?:\.\d+)?)\s*([wdhms])")
_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_RELATIVE_RE = re.compile(r"(?i)^(\d+)\s*([dwm])$")


def parse_duration(
    text: str,
    hours_per_day: float = 8,
    days_per_week: float = 5,
) -> int:
    """Parse a Jira duration expression into whole seconds.

    Accepts any combination of ``w``, ``d``, ``h``, ``m``, ``s`` units with
    optional whitespace and decimals: ``"1w 2d 3h 30m"``, ``"90m"``,
    ``"1.5h"``, ``"2d"``.

    Args:
        text: The expression.
        hours_per_day: Hours in a Jira working day.
        days_per_week: Working days in a Jira week.

    Returns:
        The duration in seconds, always positive.

    Raises:
        InputError: The text is empty, contains anything other than
            number+unit pairs, or resolves to zero or less.
    """
    raw = (text or "").strip()
    if not raw:
        raise InputError("a duration is required (e.g. 2d, 3h 30m, 90m)")
    if hours_per_day <= 0 or days_per_week <= 0:
        raise InputError("hours_per_day and days_per_week must be positive")

    matches = list(_UNIT_RE.finditer(raw))
    if not matches:
        raise InputError(
            f"cannot read duration {text!r}; use units w/d/h/m/s, e.g. '1w 2d 3h 30m'"
        )
    # Everything outside the matched spans must be whitespace, otherwise
    # "3 potatoes" would silently parse as nothing and "3hx" as 3h.
    consumed = "".join(raw[m.start() : m.end()] for m in matches)
    if len(re.sub(r"\s+", "", raw)) != len(re.sub(r"\s+", "", consumed)):
        raise InputError(
            f"cannot read duration {text!r}; use units w/d/h/m/s, e.g. '1w 2d 3h 30m'"
        )

    day_seconds = float(hours_per_day) * SECONDS_PER_HOUR
    per_unit = {
        "w": day_seconds * float(days_per_week),
        "d": day_seconds,
        "h": float(SECONDS_PER_HOUR),
        "m": float(SECONDS_PER_MINUTE),
        "s": 1.0,
    }

    total = 0.0
    for match in matches:
        total += float(match.group(1)) * per_unit[match.group(2).lower()]

    seconds = int(round(total))
    if seconds <= 0:
        raise InputError(f"duration {text!r} must be greater than zero")
    return seconds


def format_duration(seconds: int | None, hours_per_day: float = 8) -> str:
    """Render seconds as a compact Jira duration.

    Returns ``"-"`` for ``None``, zero and negative input, so a report cell is
    never blank or misleading. Output uses at most ``d``, ``h`` and ``m``
    (``"1d 2h"``, ``"45m"``); sub-minute remainders are dropped.
    """
    if seconds is None:
        return "-"
    try:
        total = int(seconds)
    except (TypeError, ValueError):
        return "-"
    if total <= 0:
        return "-"
    if hours_per_day <= 0:
        hours_per_day = 8

    day_seconds = int(round(float(hours_per_day) * SECONDS_PER_HOUR))
    days, rest = divmod(total, day_seconds)
    hours, rest = divmod(rest, SECONDS_PER_HOUR)
    minutes = rest // SECONDS_PER_MINUTE

    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    return " ".join(parts) or f"{total}s"


def to_jira(text: str) -> str:
    """Validate ``text`` and return it normalised (``"1d 2h 30m"``).

    Uses Jira's default 8h day / 5d week on purpose: the string is going back
    to Jira, which will re-interpret it with the *server's* rates, so applying
    a local override here would double-convert.
    """
    return format_duration(parse_duration(text))


def resolve_date(text: str, today: date | None = None) -> str:
    """Resolve a date expression to ``YYYY-MM-DD``.

    Accepted: an explicit ``YYYY-MM-DD``, ``today``, ``yesterday``, or a
    relative age such as ``7d``, ``2w``, ``1m`` (a month is 30 days), which is
    counted *backwards* from ``today`` because every caller uses it for a
    "since" window.

    Args:
        text: The expression.
        today: Reference date, injected by tests. Defaults to the real today.

    Raises:
        InputError: The expression is empty or unrecognised, or the explicit
            date is not a real calendar date.
    """
    raw = (text or "").strip()
    if not raw:
        raise InputError("a date is required (YYYY-MM-DD, today, yesterday, 7d, 2w, 1m)")
    base = today or date.today()

    explicit = _DATE_RE.match(raw)
    if explicit:
        year, month, day = (int(part) for part in explicit.groups())
        try:
            return date(year, month, day).isoformat()
        except ValueError as exc:
            raise InputError(f"{raw!r} is not a real date: {exc}") from exc

    lowered = raw.lower()
    if lowered == "today":
        return base.isoformat()
    if lowered == "yesterday":
        return (base - timedelta(days=1)).isoformat()

    relative = _RELATIVE_RE.match(lowered)
    if relative:
        amount = int(relative.group(1))
        unit = relative.group(2)
        days = {"d": 1, "w": 7, "m": DAYS_PER_MONTH}[unit] * amount
        return (base - timedelta(days=days)).isoformat()

    raise InputError(
        f"cannot read date {text!r}; use YYYY-MM-DD, today, yesterday, or 7d/2w/1m"
    )


__all__ = [
    "DAYS_PER_MONTH",
    "format_duration",
    "parse_duration",
    "resolve_date",
    "to_jira",
]
