"""Make untrusted text safe to print on a terminal.

Text that comes from the Jira server (summaries, comments, descriptions,
error messages) is controlled by whoever wrote it. Printed raw, an escape
sequence inside it can recolour or erase lines, overwrite a confirmation
preview, set the window title or write to the clipboard (OSC 52).

``terminal_safe`` removes every C0/C1 control character except newline and
tab, so ESC (U+001B) and the 8-bit CSI (U+009B) never reach the terminal.
It also drops Unicode bidi overrides, which can visually reorder a line.
"""

from __future__ import annotations

import re

__all__ = ["csv_safe_cell", "terminal_safe", "terminal_safe_line"]

# C0 controls except \t (0x09) and \n (0x0A), DEL, the C1 block, and the
# bidi embedding/override/isolate characters.
_UNSAFE_RE = re.compile(
    "[\x00-\x08\x0b-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069]"
)


def terminal_safe(text: object) -> str:
    """Return ``text`` as a string with terminal control characters removed.

    Carriage returns are removed too, so a line cannot be rewritten in place.
    Newlines and tabs are kept. ``None`` becomes an empty string.
    """
    if text is None:
        return ""
    return _UNSAFE_RE.sub("", str(text))


def terminal_safe_line(text: object) -> str:
    """Like :func:`terminal_safe`, but also folds newlines and tabs to spaces.

    Use it for table cells and one-line previews, where an embedded newline
    could fake an extra row.
    """
    return terminal_safe(text).replace("\n", " ").replace("\t", " ")


#: Leading characters a spreadsheet may read as the start of a formula.
CSV_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def csv_safe_cell(text: object) -> str:
    """Return ``text`` as a CSV cell that is terminal-safe and formula-inert.

    Control and bidi characters are removed FIRST, and only then is the
    formula check applied.  The order matters: checking first lets a value
    such as ``"\\x1b=HYPERLINK(...)"`` pass (its first character is ESC) and
    become a live ``=HYPERLINK(...)`` once a later pass strips the ESC.
    A cell gets a leading ``'`` (so spreadsheets show it as text) when either
    the raw value or the cleaned value, after leading blanks, starts with a
    formula character.  Newlines inside the cell are kept
    (CSV quotes them); stripping is idempotent, so running
    :func:`terminal_safe` over the rendered CSV again changes nothing.
    """
    raw = "" if text is None else str(text)
    cleaned = terminal_safe(raw)
    if raw.startswith(CSV_FORMULA_PREFIXES) or cleaned.lstrip(" \t\n").startswith(
        CSV_FORMULA_PREFIXES
    ):
        return "'" + cleaned
    return cleaned
