# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [2.1.0] — 2026-10-26

Project members, a people directory, a boards overview, a rebuilt
interactive menu, and security hardening from two review waves. See
[SECURITY.md](SECURITY.md) for the full security model. Behaviour changes that
can affect scripts are marked **(breaking)**.

### Added

- Project-role membership. CLI: `jira projects members`, `add-member`,
  `remove-member`. MCP: `jira_project_members` (read) and
  `jira_add_project_members` / `jira_remove_project_members` (write, two-step
  confirmation). Usernames and group names are validated before any request,
  existing members / non-members are skipped, and the role is read back after
  every change.
- Interactive menu (`jira` with no arguments): new item **17. Project members**
  to show a project's roles, add people (usernames or `team:NAME`) and remove
  people by list number or username. Every change shows a preview and asks
  `y/N`; existing members and non-members are skipped.
- Interactive menu rebuilt into one module per section (`jira_cli/cli/menu_*.py`).
  The main menu now has 17 items; **7. Boards overview** is new (per-board and
  per-column counts, kanban and scrum aware, issue lists with title, assignee
  and reporter, assignee filter).
- People picker: every username prompt shows a numbered list of everyone with
  their projects, roles and admin rights (`/text` filters, ranges, `all`,
  `team:NAME`). Unknown usernames can no longer be typed by mistake. Projects
  are picked by number or key; `all` reaches every project.
- CLI: `jira people` (people directory) and `jira boards overview`.
  MCP: `jira_people_directory` and `jira_board_overview` (read). 53 MCP tools
  (34 read, 19 write).
- `jira-cli` — a third launcher and entry point, identical to `jira`, for
  machines where another program already owns `jira`.
- `config.config_dir()` — one helper that resolves the config directory
  (`$JIRA_CONFIG_DIR`, default `~/.config/jira-cli`) without loading
  credentials; the MCP plan directory defaults to `<config dir>/plans`.
- `JIRA_CLI_PLAN_DIR` — the MCP plan directory.
- `JIRA_ALLOW_HTTP` — explicit opt-in to plain `http://`.
- Exit code `4` (confirmation declined) and `5` (local file or I/O error).
- `MANIFEST.in`: the source distribution ships the package, its metadata and
  the top-level documents only (no tests, scripts, docs or CI config).

### Fixed

- Person activity no longer fails with HTTP 400 for a real user who was never
  an assignee / reporter / worklog author; rejected clauses are dropped and
  noted. A non-existent username gives an error with suggestions.
- Create issue no longer aborts on kanban boards (no sprint step there); `b`
  goes back one step and keeps answers; due dates accept `7d` / `2w`; parent
  and epic keys are checked to exist; a parent switches the type to sub-task.
- Sprint report: type and status names are filled in; completion is now the
  share of committed issues finished (was able to exceed 100%). Board and sprint
  are picked from lists; kanban boards are explained instead of failing.
- `b` at any `y/N` prompt now goes back instead of meaning "no".
- Every date, duration, number and issue key is validated at its own prompt
  and re-asked with a reason.
- Project list shows the lead; duplicate usernames differing only in case are
  merged; a plan with `sprint: active` on a kanban board is a validation error.
- Settings / doctor actually runs the doctor checks; saved filters are checked
  against the server before saving; export re-asks bad formats and shows the
  absolute path.

### Security

- **Plain `http://` is refused (breaking).** `JIRA_BASE_URL` must be
  `https://` unless `JIRA_ALLOW_HTTP=1` is set; then one stderr warning per
  process says credentials travel unencrypted. Other schemes are always
  refused.
- **No credentials in the URL (breaking).** A `JIRA_BASE_URL` with embedded
  `user:password@` is refused; the error shows the URL with the credentials
  removed.
- **TLS-off warning.** `JIRA_VERIFY_TLS=0` (or any false value) now prints one
  stderr warning per process recommending a CA-bundle path.
- **Env-file permission and owner warnings.** A group/world-accessible env
  file, or (POSIX) one owned by another uid, produces a one-line stderr
  warning (once per file per process).
- **No `~/.netrc`.** The HTTP session never picks up netrc credentials, and a
  redirect can only strip, never add, the `Authorization` header.
- **At most 5 redirects** instead of the `requests` default of 30.
- **Strict key and id validation (breaking for malformed input).** Issue keys,
  project keys and numeric ids (board, sprint, issue type, issue id) are
  validated before they reach a URL path; lower-case keys are upper-cased.
  The client additionally rejects `.`/`..` segments, backslashes, `#`, extra
  `?` and encoded `.`, `/`, `\`, `%` or NUL in request paths. Plan
  `parent`/`epic`/link targets go through the same validator.
- **Read-only search exception is exact.** In read-only mode a `POST` is
  allowed only when its path is exactly `<base path>/rest/api/2/search` with
  no query string.
- **MCP plan directory (breaking for MCP clients).** Plan files given to
  `jira_plan_validate`, `jira_plan_preview` and `jira_plan_apply` must lie
  inside `$JIRA_CLI_PLAN_DIR` (default `<config dir>/plans`), end in `.yaml`,
  `.yml` or `.csv`, and be at most 1 MB; `state_path` must be a
  `*.applied.json` file in the same directory. Parse errors report no file
  content.
- **Report files** are written `0600` with an atomic rename; directories the
  tool creates are `0700`. `safe_filename()` restricts generated names to
  `[A-Za-z0-9._-]`, at most 100 characters.
- **CSV formula escaping.** CSV cells starting with `=`, `+`, `-`, `@`, tab or
  carriage return are prefixed with `'`.
- **Terminal-safe output.** Text from Jira, error lines and preview lines are
  stripped of C0/C1 control characters, DEL and bidi overrides before they
  are printed; JSON output escapes C1, DEL and bidi characters as `\uXXXX`.
- **Redacted `--debug` tracebacks.** The traceback, chained exceptions
  included, is redacted and made terminal-safe before printing. An unexpected
  exception without `--debug` prints only its class name.
- **Installer never replaces another program's launcher.** `install.sh` and
  `uninstall.sh` refuse to replace or remove `~/.local/bin/jira`, `jira-cli`
  or `jira-mcp` when the file was not written by `install.sh` (a symlink always
  counts as foreign); nothing is changed. `--force` overrides.
- **`JIRA_CONFIG_DIR` is validated** by both scripts before anything is
  created or removed: it must be an absolute path with no `.` or `..`
  components, and must not be `/`, your home directory or one of its
  ancestors, or a system directory. `install.sh` also refuses an existing
  non-empty directory that holds none of `.env`, `teams.yaml`, `filters.yaml`
  or `plans/` (it would be chmodded `0700`) unless `--force` is given.
- **Launcher bodies are quoted.** The venv path is shell-quoted in each
  launcher, and launchers are written to a temporary file and renamed into
  place, so a symlink is replaced rather than written through.

### Changed

- **Declined confirmation exits `4` (breaking).** A "no" (or EOF / Ctrl-C)
  at `Proceed? [y/N]` sends nothing, prints `Aborted, nothing changed.` on
  stderr and exits `4` instead of `0`.
- **`--json` keeps stdout pure.** Previews, the `Proceed?` prompt and notices
  go to stderr when `--json` is set, so stdout is always one JSON document.
- `OSError` from a local file operation now exits `5` instead of `1`.
- Interactive-menu exports and new plan files default to the per-user data
  directory, `$XDG_DATA_HOME/jira-cli` (`~/.local/share/jira-cli`; on Windows
  `%LOCALAPPDATA%\jira-cli`), instead of the current working directory.
- `uninstall.sh` prints, but never runs, the `rm -rf` commands for the
  virtualenv and the config directory.
- Development dependencies are pinned to ranges (`pytest`, `ruff`, `bandit`,
  `pip-audit`) so a new major/minor release cannot change the CI verdict.

## [2.0.0] — 2026-10-11

Complete rewrite. 1.x was a **single-file script** with its configuration in
`~/.jira-cli/config.env`; 2.0 is an installable package (`jira-cli-manager`)
with a service layer, a reporting layer, an interactive menu and an MCP
server. Nothing of the 1.x internals survives, so this is a major release.

### Added

- **Package layout** — `jira_cli` with `errors`, `config`, `redact`, `client`,
  `fields`, `durations`, `report`, a `services/` layer and a `cli/` layer.
  Installable with `pip install -e .`; runnable as `python -m jira_cli`.
- **Two entry points** — `jira` (CLI) and `jira-mcp` (MCP server over stdio).
- **Personal Access Token auth** (sent as a bearer token in the
  `Authorization` header) as the default and
  recommended mode, alongside HTTP Basic. Jira Server identifies users by
  username, which the client resolves from `/myself` when needed.
- **Manager reports**, all returning one normalised report shape renderable as
  table, Markdown, CSV or JSON: team dashboard, person activity, stale,
  overdue, incomplete, unassigned, workload, worklog, sprint report, velocity.
- **Teams** — named groups of people in `teams.yaml`, usable anywhere a user
  list is accepted (`--team backend`, `team:backend`, `@backend`), managed
  from both the CLI and the MCP server.
- **Plan workflow** — draft a YAML task list (CSV is also accepted by the MCP
  plan tools), validate it against the live instance, preview the whole batch
  with totals, then apply it. Apply is resumable: each created issue and link
  is recorded in `<plan-stem>.applied.json`, so a re-run skips what already
  exists. `estimate: ask` (a validation error until a human answers) and
  `estimate: none` make the "who sets the estimate" decision explicit per
  issue, and `estimate_by: agent|human` flags agent-suggested estimates with a
  validation warning.
- **Saved filters** in `filters.yaml` plus read access to your favourite
  server-side filters.
- **Interactive menu** when `jira` is run with no arguments.
- **`jira export`** re-renders a report saved as JSON into table, Markdown or
  CSV without querying Jira again.
- **MCP server** (FastMCP, stdio) with 48 tools: 31 read tools that run
  directly and 17 write tools behind a two-step `confirm_token` handshake —
  preview first, execute only on a token that is single-use, 15-minute-TTL and
  bound to the tool name and a hash of the exact arguments. Usable from Claude
  Code and from Hermes.
- **Read-only mode** — `JIRA_READ_ONLY=1` refuses every write before a request
  is built, in the CLI and in the MCP server.
- **Safe pagination** — `PartialResultError` when a paged endpoint returns
  fewer items than the `total` it claimed, instead of silently short results.
- **Duration and date helpers** — `1w 2d 3h 30m`, `90m`, `1.5h` and relative
  dates `today`, `yesterday`, `7d`, `2w`, `1m`.
- **Custom field discovery** by display name, with `JIRA_FIELD_*` overrides.
- **Packaging and supply chain** — `pyproject.toml`, `install.sh`,
  `uninstall.sh`, MIT `LICENSE`, [SECURITY.md](SECURITY.md), and CI running
  ruff, pytest, bandit, pip-audit and full-history gitleaks.
- **Documentation** — [README.md](README.md),
  [docs/COMMANDS.md](docs/COMMANDS.md),
  [docs/PLAN_FORMAT.md](docs/PLAN_FORMAT.md),
  [docs/MCP_TOOLS.md](docs/MCP_TOOLS.md).

### Changed

- **Configuration moved** from `~/.jira-cli/config.env` to
  `~/.config/jira-cli/.env` (directory `0700`, file `0600`).
  `install.sh --import-legacy` migrates the keys without printing any value.
- **Environment variable names** are now explicit and documented in
  [.env.example](.env.example). The 1.x names are still read as aliases when
  the modern name is absent: `JIRA_EMAIL` -> `JIRA_USERNAME`,
  `JIRA_API_TOKEN` -> `JIRA_PASSWORD`, `JIRA_PROJECT` -> `JIRA_DEFAULT_PROJECT`.
- **Errors** are typed (`AuthError`, `JiraPermissionError`, `NotFoundError`,
  `ReadOnlyError`, `PartialResultError`, `InputError`, `ConfigError`) with
  distinct exit codes, instead of a single generic failure.
- Output is English everywhere.

### Removed

- The 1.x single-file script and its ad-hoc argument handling.
- Reading credentials from command-line flags.

### Security

- Secrets are environment-only, `repr`-suppressed, and redacted out of every
  message, including Jira's own error bodies.
- TLS verification on by default; an absolute URL pointing at another origin
  is refused so the token cannot leak to a third-party host.
- Writes are never retried; only `GET` is.
- See [SECURITY.md](SECURITY.md) for the full posture.

## [1.x] — before 2026-10

Single-file helper script, configuration in `~/.jira-cli/config.env`. Not
maintained and not supported; no changelog was kept.
