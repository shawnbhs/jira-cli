"""Render a report dict (see ``jira_cli.report.make_report``) and write it out.

Three renderers and one writer. The renderers are pure functions of the report
dict, so they are the whole of the formatting logic and are trivially testable;
``write_report`` is the only part that touches the filesystem.

Two deliberate pieces of defensive behaviour live here:

* Markdown table cells escape ``|`` and flatten newlines, because a GitHub
  pipe table has no way to express either and an un-escaped summary silently
  shears the table into the wrong number of columns.
* CSV cells that begin with a formula character are prefixed with a single
  quote. A spreadsheet treats ``=cmd|'...'`` in a downloaded CSV as a live
  formula, which turns an exported Jira summary into code execution on the
  reader's machine (CSV injection).
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from jira_cli.errors import InputError
from jira_cli.textsafe import csv_safe_cell

#: Formats ``write_report`` understands, mapped to the suffixes that select them.
FORMATS: dict[str, tuple[str, ...]] = {
    "md": (".md", ".markdown"),
    "csv": (".csv",),
    "json": (".json",),
}

#: Anything ``safe_filename`` must replace.
_UNSAFE_FILENAME_CHARS = re.compile(r"[^A-Za-z0-9._-]")

#: Reports hold people and worklog data: owner-only file, owner-only new dirs.
_FILE_MODE = 0o600
_DIR_MODE = 0o700


def to_markdown(report: dict) -> str:
    """Render a report as GitHub-flavoured Markdown.

    Args:
        report: a report dict with ``title``, ``columns``, ``rows`` and
            optional ``summary``/``meta``.

    Returns:
        Markdown text: an H1 title, a summary bullet list, a pipe table of the
        rows, and a trailing ``generated_at`` line. Ends with a newline.
    """
    report = _as_report(report)
    columns = _columns(report)
    lines: list[str] = [f"# {report.get('title') or 'Report'}", ""]

    summary = report.get("summary") or {}
    if isinstance(summary, dict) and summary:
        lines.append("## Summary")
        lines.append("")
        lines.extend(f"- **{key}**: {_scalar(value)}" for key, value in summary.items())
        lines.append("")

    if columns:
        lines.append("| " + " | ".join(_cell(col) for col in columns) + " |")
        lines.append("| " + " | ".join("---" for _ in columns) + " |")
        for row in _rows(report):
            lines.append(
                "| " + " | ".join(_cell(row.get(col)) for col in columns) + " |"
            )
        lines.append("")

    generated_at = (report.get("meta") or {}).get("generated_at")
    if generated_at:
        lines.append(f"_generated_at: {generated_at}_")
        lines.append("")
    return "\n".join(lines)


def to_csv(report: dict) -> str:
    """Render a report's rows as CSV text (header row plus one row per entry).

    Args:
        report: a report dict.

    Returns:
        CSV text with ``\\r\\n`` line endings (the format's own convention, as
        produced by :mod:`csv`). Cells that could be read as a spreadsheet
        formula are prefixed with a single quote.
    """
    report = _as_report(report)
    columns = _columns(report)
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer)
    writer.writerow([_csv_cell(col) for col in columns])
    for row in _rows(report):
        writer.writerow([_csv_cell(row.get(col)) for col in columns])
    return buffer.getvalue()


def to_json(report: dict) -> str:
    """Render the whole report dict as indented JSON.

    Args:
        report: a report dict.

    Returns:
        JSON text, two-space indent, non-ASCII characters kept as themselves,
        ending in a newline.
    """
    report = _as_report(report)
    return json.dumps(report, indent=2, ensure_ascii=False, default=str) + "\n"


def write_report(
    report: dict,
    path: Path,
    fmt: str | None = None,
    *,
    base_dir: Path | None = None,
) -> Path:
    """Render ``report`` and write it to ``path``.

    The file is created ``0600`` and any directory this call creates is
    ``0700``: reports carry people, worklog and sprint data, so they are
    private by default (existing directories are left as they are).

    Args:
        report: a report dict.
        path: destination file. Missing parent directories are created.
        fmt: ``"md"``, ``"csv"`` or ``"json"``. When ``None`` the format is
            inferred from the suffix of ``path``.
        base_dir: when given, the resolved target must lie inside this
            directory. Pass it whenever ``path`` was assembled from data the
            caller does not control (a report title, an issue summary), so a
            ``..`` component cannot steer the write elsewhere.

    Returns:
        The resolved path written.

    Raises:
        InputError: unknown/unspecifiable format, the target is a directory,
            lies inside a ``.git`` directory, or escapes ``base_dir``.
        OSError: the write itself failed.
    """
    report = _as_report(report)
    target = Path(path).expanduser()
    resolved = _check_target(target)
    if base_dir is not None:
        _check_contained(resolved, Path(base_dir).expanduser())
    text = _render(report, _resolve_fmt(resolved, fmt))

    _make_private_dirs(resolved.parent)
    _atomic_write(resolved, text)
    return resolved


def safe_filename(text: object, default: str = "report") -> str:
    """Turn arbitrary text (a report title, an issue summary) into a file name.

    Every character outside ``[A-Za-z0-9._-]`` becomes ``-``, runs of ``-``
    collapse, and leading dots/dashes are stripped, so the result can never be
    a path separator, ``.``, ``..``, a hidden file or an option-looking name.

    Args:
        text: the raw text; ``None`` and blanks fall back to ``default``.
        default: the name used when nothing usable is left.

    Returns:
        A non-empty name of at most 100 characters.
    """
    raw = "" if text is None else str(text)
    name = _UNSAFE_FILENAME_CHARS.sub("-", raw)
    name = re.sub(r"-{2,}", "-", name).lstrip(".-").rstrip("-")[:100].rstrip(".-")
    return name or default


# --------------------------------------------------------------------------- #
# rendering helpers
# --------------------------------------------------------------------------- #


def _render(report: dict, fmt: str) -> str:
    """Dispatch to the renderer for an already-validated format name."""
    return {"md": to_markdown, "csv": to_csv, "json": to_json}[fmt](report)


def _as_report(report: Any) -> dict:
    """Validate that ``report`` is a report-shaped mapping."""
    if not isinstance(report, dict):
        raise InputError("report must be a dict")
    return report


def _columns(report: dict) -> list[str]:
    """Column names, falling back to the union of the keys the rows carry."""
    columns = report.get("columns")
    if isinstance(columns, (list, tuple)) and columns:
        return [str(col) for col in columns]
    seen: dict[str, None] = {}
    for row in _rows(report):
        for key in row:
            seen.setdefault(str(key), None)
    return list(seen)


def _rows(report: dict) -> list[dict]:
    """The report's rows, ignoring any entry that is not a mapping."""
    rows = report.get("rows")
    if not isinstance(rows, (list, tuple)):
        return []
    return [row for row in rows if isinstance(row, dict)]


def _scalar(value: Any) -> str:
    """Render a cell value as a single-line string (``""`` for ``None``)."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple)):
        return ", ".join(_scalar(item) for item in value)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return str(value)


def _cell(value: Any) -> str:
    """Escape a value for a Markdown pipe table cell."""
    text = _scalar(value).replace("\\", "\\\\").replace("|", "\\|")
    return " ".join(text.split())


def _csv_cell(value: Any) -> str:
    """Render a value for CSV: control characters stripped, formulas neutralised.

    The same :func:`jira_cli.textsafe.csv_safe_cell` is used by the stdout
    renderer in ``cli.output``, so ``--out`` files and piped stdout agree.
    """
    return csv_safe_cell(_scalar(value))


# --------------------------------------------------------------------------- #
# filesystem helpers
# --------------------------------------------------------------------------- #


def _resolve_fmt(path: Path, fmt: str | None) -> str:
    """Normalise an explicit format, or infer one from ``path``'s suffix."""
    if fmt is not None:
        name = str(fmt).strip().lower().lstrip(".")
        if name in ("markdown",):
            name = "md"
        if name not in FORMATS:
            raise InputError(
                f"unknown format {fmt!r}; expected one of {', '.join(FORMATS)}"
            )
        return name
    suffix = path.suffix.lower()
    for name, suffixes in FORMATS.items():
        if suffix in suffixes:
            return name
    raise InputError(
        f"cannot infer a format from {path.name!r}; pass fmt=md|csv|json"
    )


def _check_target(target: Path) -> Path:
    """Resolve the destination and refuse the paths we must never write."""
    if not target.name:
        raise InputError(f"{target} is not a file path")
    resolved = Path(os.path.abspath(target))
    if resolved.is_dir():
        raise InputError(f"{resolved} is a directory, not a report file")
    if any(_is_git_dir_name(part) for part in resolved.parts):
        raise InputError(f"refusing to write inside a .git directory: {resolved}")
    return resolved


def _is_git_dir_name(part: str) -> bool:
    """True for ``.git`` in any case, and for Windows aliases like ``.git.``.

    Windows and macOS file systems are case-insensitive, so ``.GIT`` *is*
    ``.git`` there, and Win32 silently drops trailing dots and spaces.
    """
    return part.rstrip(". ").lower() == ".git"


def _check_contained(resolved: Path, base_dir: Path) -> None:
    """Refuse a target that, after resolving ``..`` and symlinks, leaves ``base_dir``."""
    base = Path(os.path.realpath(base_dir))
    real_target = Path(os.path.realpath(resolved))
    if real_target == base or not real_target.is_relative_to(base):
        raise InputError(f"refusing to write outside {base}: {resolved}")


def _make_private_dirs(directory: Path) -> None:
    """Create ``directory`` and any missing ancestors with mode ``0700``.

    ``Path.mkdir(parents=True, mode=...)`` applies the mode only to the leaf,
    so the missing ancestors are created one by one. Directories that already
    exist are not touched: they may be the user's own (``~/Desktop``).
    """
    missing: list[Path] = []
    current = directory
    while not current.exists() and current != current.parent:
        missing.append(current)
        current = current.parent
    for path in reversed(missing):
        try:
            path.mkdir(mode=_DIR_MODE)
        except FileExistsError:
            continue  # created concurrently; leave its mode alone
        _best_effort_chmod(path, _DIR_MODE)


def _best_effort_chmod(path: Path, mode: int) -> None:
    """Tighten permissions where the file system supports POSIX modes."""
    try:
        os.chmod(path, mode)
    except (NotImplementedError, OSError):
        pass  # e.g. FAT/SMB mounts: nothing stronger is available


def _atomic_write(target: Path, text: str) -> None:
    """Write ``text`` to ``target`` via a temp file in the same directory.

    A partial report is worse than none: a reader cannot tell a truncated
    export from a short one. Writing beside the target and renaming means the
    destination either holds the previous content or the complete new content.
    ``mkstemp`` creates the temp file ``0600`` and the rename keeps that mode,
    so the report is never readable by other users, not even briefly.
    """
    handle, temp_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent)
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        _best_effort_chmod(temp_path, _FILE_MODE)
        os.replace(temp_path, target)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise
