# Security Policy

## Reporting a vulnerability

Please do **not** open a public issue for a security problem. Open a GitHub
security advisory on this repository (Security -> Report a vulnerability), or
contact the maintainer privately through the address on the GitHub profile.

Include the version (`pip show jira-cli-manager`), what you did, what
happened, and what you expected. A proof of concept against your *own* Jira
instance is welcome; never include a real token, hostname or issue content in
the report.

Expect an acknowledgement within a week. Fixes land in a patch release and are
noted in [CHANGELOG.md](CHANGELOG.md).

## Supported versions

| Version | Supported |
|---|---|
| 2.x | yes |
| 1.x | no — single-file script, superseded, no fixes |

## Security model

This section describes what the code does, and names the module that
implements each control so it can be checked. Paths are relative to the
repository root.

### Threats and defences

| Threat | Defence |
|---|---|
| A credential leaking into output, logs or error messages | Environment-only secrets, `repr` suppression, redaction of every error line and `--debug` traceback |
| The credential being sent to the wrong host | Same-origin check on every URL; no `~/.netrc`; `http://` refused by default; no userinfo in the base URL; at most 5 redirects |
| A crafted key or id walking a REST path (`PROJ-1/../../project/PROJ`) | Strict key/id validation plus a request-path check in the client |
| A prompt-injected agent writing to Jira | Read-only mode, and a two-step `confirm_token` on every MCP write tool |
| An MCP client reading arbitrary local files through the plan tools | Plan files confined to one directory, suffix allow-list, 1 MB cap |
| Hostile text from Jira rewriting the terminal | Control and bidi characters removed before printing |
| An exported CSV executing as a spreadsheet formula | Formula-prefix escaping |
| Other local users reading or changing credentials or reports | Permission and owner warnings on env files; `0600` files and `0700` new directories for what the tool writes |

### Credentials

- **Secrets come from the environment only** — the process environment or an
  environment file (`jira_cli/config.py`). There is no credential flag, no
  credential prompt that stores anything, and no token baked into a default.
- The process environment wins over the env file. Env-file lookup order:
  `--env-file`, `$JIRA_CLI_ENV_FILE`, `<config dir>/.env`, then the 1.x
  location `~/.jira-cli/config.env`. The config directory is
  `$JIRA_CONFIG_DIR`, default `~/.config/jira-cli`.
- The env file is parsed as `KEY=VALUE` lines with **no shell expansion**:
  `$HOME` and `${X}` stay literal.
- **Env-file permission and owner warnings.** When an env file is read, the
  tool prints one `warning:` line on stderr if the file has any group or world
  permission bit set (suggesting `chmod 600`), and — on POSIX — one line if
  the file is owned by a uid other than the current user's. These are
  warnings, not refusals, and each is printed at most once per process.
  `jira doctor` additionally reports the permission bits as a `WARN` row.
- `Settings.secret` is excluded from `repr()`; `Settings.__repr__` and
  `__str__` print `secret=<hidden>`.
- `jira config show`, `jira doctor` and the MCP `jira_doctor` tool render the
  secret as `set (N chars)`, never its value.
- **No `~/.netrc`.** The HTTP session always has `auth` set (a bearer-token
  object for a PAT, a username/password tuple for basic auth), so `requests`
  never substitutes netrc credentials; the redirect hook is overridden so it
  can strip an `Authorization` header on a cross-host redirect but never add
  one (`jira_cli/client.py`, `_NoNetrcSession`). Proxy and CA-bundle variables
  (`HTTPS_PROXY`, `NO_PROXY`, `REQUESTS_CA_BUNDLE`) are still honoured.

### Redaction

`jira_cli/redact.py` replaces credentials with `***` in two ways:

1. **Literal** — the values of `JIRA_PAT`, `JIRA_PASSWORD` and
   `JIRA_API_TOKEN` (and the resolved secret) are registered as soon as they
   are loaded, and every occurrence is masked. Values shorter than 6
   characters are not registered, because masking them would corrupt ordinary
   text.
2. **Pattern** — `Bearer …` / `Basic …` header values; `KEY=value` or
   `KEY: value` (case-insensitive) for `JIRA_PAT`, `JIRA_PASSWORD`,
   `JIRA_API_TOKEN`, `JIRA_TOKEN`, `GITLAB_TOKEN`, `PRIVATE-TOKEN`, `token`,
   `password`, `passwd`, `pwd`, `secret`, `api_token`, `apitoken`,
   `access_token` and `pat`; and the password in `user:password@` URLs.

Every `JiraError` (the base of every error the package raises) redacts its
message and URL when constructed, so `str()` and `repr()` of an error are
redacted. Jira's own error bodies are cut to 500 characters and redacted
before they are raised. MCP tool errors are redacted again before they are
returned.

The CLI's last-resort handler (`main` in `jira_cli/cli/main.py`) turns every
failure into one `error:` line on stderr that is redacted and made
terminal-safe. For an exception type the package does not own, only the class
name is printed (`error: unexpected ValueError`), because its message is
unvetted.

**`--debug` tracebacks.** With `--debug` or `JIRA_CLI_DEBUG=1` the full
traceback, chained `__cause__` / `__context__` exceptions included, is
formatted as one block, passed through `redact()` and the terminal-safe
filter, and printed on stderr. Redaction only masks what it recognises (see
above); review `--debug` output before you share it.

### Transport and TLS

- **`https://` is required.** `JIRA_BASE_URL` must start with `https://`. A
  plain `http://` URL is refused with a configuration error unless
  `JIRA_ALLOW_HTTP=1` is set; then the tool prints one stderr warning per
  process that credentials are sent unencrypted. Any other scheme is always
  refused.
- **No credentials in the URL.** A `JIRA_BASE_URL` containing `user@` or
  `user:password@` is refused with a configuration error whose message shows
  the URL with the credentials removed (`config.display_url()`).
- **TLS verification is on by default.** `JIRA_VERIFY_TLS` accepts a boolean
  or a path to a CA bundle for a private CA. A false value (`0`, `false`,
  `no`, `off`, `n`) disables verification and prints one stderr warning per
  process recommending a CA-bundle path instead.
- **Same origin only.** The client builds every URL from `JIRA_BASE_URL`. An
  absolute URL whose scheme, host or port differs is refused with
  `InputError`, so the credential cannot be sent to a third-party host.
- **At most 5 redirects** (`MAX_REDIRECTS` in `jira_cli/client.py`), instead
  of the `requests` default of 30.
- Every request carries an explicit timeout (`JIRA_TIMEOUT`, default 20 s).
- Only `GET` is retried: up to 3 attempts on HTTP 429/502/503/504 and on
  connection errors, honouring `Retry-After` capped at 60 s. **Writes are
  never retried**, so a timed-out write is never silently duplicated.
- Paginated reads raise `PartialResultError` when the server returns fewer
  items than the `total` it reported, instead of returning a short list.

### Identifier validation

Every issue key, project key and numeric id that reaches a REST URL path is
validated first (`jira_cli/ids.py`):

| Kind | Rule | Example |
|---|---|---|
| Issue key | `^[A-Z][A-Z0-9_]*-\d+$` after trimming and upper-casing | `proj-12` becomes `PROJ-12` |
| Project key | `^[A-Z][A-Z0-9_]*$` after trimming and upper-casing | `proj` becomes `PROJ` |
| Numeric id (board, sprint, issue type, issue id) | A positive integer in canonical form | `12` is accepted; `012`, `-1`, `1.0`, `true` are refused |

Lower-case keys are therefore accepted and upper-cased. The project service
(`jira_cli/services/projects.py`) applies a stricter rule on top:
`^[A-Z][A-Z0-9]{1,9}$`, i.e. 2–10 letters and digits. An issue argument made
only of digits is treated as a numeric issue id. A value that fails
validation raises `InputError` before any request is made. In plan files, a
`parent` or link target is treated as an existing issue key only when it is
already upper-case; a lower-case value such as `t-1` stays a plan `ref`.

Independently, the client refuses any request path containing a `.` or `..`
segment (including Tomcat's `..;` form), a backslash, a `#`, more than one
`?`, or the encodings `%2e`, `%2f`, `%5c`, `%25` or `%00`.

### Read-only mode

`JIRA_READ_ONLY=1` (or `true`, `yes`, `on`, `y`) makes the client refuse
every `POST`, `PUT` and `DELETE` with `ReadOnlyError` before the request is
sent. The one exception is `POST` to the search endpoint, which is a read: it
is allowed only when the request path is **exactly**
`<base path>/rest/api/2/search` with no query string, so a URL that merely
contains that text cannot pass as a read.

In the MCP server, every write tool — including the ones that only edit the
local `teams.yaml` or `filters.yaml` — returns `{"status": "rejected"}` on
the first call when read-only mode is on: no preview is built and no token is
issued.

### Confirmation

- **CLI.** Every write command prints what it is about to do and asks
  `Proceed? [y/N]`. Only `y` or `yes` (case-insensitive) proceeds; EOF, a
  closed stdin and Ctrl-C all mean "no". `--yes` answers the prompt and
  echoes `y`. A declined confirmation sends no request, prints
  `Aborted, nothing changed.` on stderr and exits **4**. Each preview line is
  folded to one terminal-safe line, so a value cannot fake or erase a line of
  the preview. Under `--json` the preview and prompt go to stderr.
- **MCP.** Every write tool requires the two-step `confirm_token` handshake
  (`jira_cli/confirm.py`), described in [docs/MCP_TOOLS.md](docs/MCP_TOOLS.md):
  - the first call only builds a preview and mints a token;
  - the token comes from `secrets.token_urlsafe(16)`, is held in memory only,
    expires after 900 s, and is single-use;
  - it is bound to the tool name and to the SHA-256 of the canonical JSON of
    the arguments (excluding `confirm_token`), so any change to the arguments
    invalidates it;
  - any presentation of a stored token consumes it, even with the wrong tool
    or arguments, so a tampered retry burns the approval;
  - tokens are compared in constant time, and at most 256 are pending (the
    oldest is evicted).

### MCP plan directory

An MCP client is not trusted with the local filesystem. When a plan tool
(`jira_plan_validate`, `jira_plan_preview`, `jira_plan_apply`) is given a file
path rather than inline YAML (`jira_cli/mcp_server.py`):

- the path is resolved (symlinks included), relative paths from the **plan
  directory**, and the result must stay inside it. The plan directory is
  `$JIRA_CLI_PLAN_DIR` when set, otherwise `<config dir>/plans`. It is not
  created automatically;
- the file must be a regular file ending in `.yaml`, `.yml` or `.csv`;
- the file must be at most 1 MB (1 048 576 bytes) and valid UTF-8;
- YAML and schema errors are reported with a line/column only, never with
  file content;
- `state_path` for `jira_plan_apply` must name a `*.applied.json` file inside
  the same directory.

The human CLI (`jira plan …`) is not restricted in this way.

### Terminal-safe output

Text that comes from Jira (summaries, descriptions, comments, names, report
cells, titles, error lines) is passed through `jira_cli/textsafe.py` before
the CLI prints it. All C0 control characters except newline and tab (so
carriage return and ESC too), DEL, the whole C1 block (including the 8-bit
CSI `U+009B`) and the bidi embedding, override and isolate characters are
removed, so an escape sequence planted in Jira cannot recolour, erase or
rewrite the screen, set the window title, write to the clipboard (OSC 52), or
reorder a line. Table cells, preview lines and error lines also fold newlines
and tabs into spaces so a value cannot fake an extra row.

With `--json`, stdout carries only the JSON document: previews, the
`Proceed?` prompt and notices go to stderr. JSON strings keep their data, but
DEL, C1 and bidi characters are written as `\uXXXX` escapes.

### Files the tool writes

| File | Mode | Module |
|---|---|---|
| Report export (`--out`, `jira export`) | `0600`, atomic rename; parent directories it creates `0700` (existing ones untouched) | `jira_cli/services/export.py` |
| `teams.yaml` | `0600`, atomic; directory `0700` | `jira_cli/services/people.py` |
| `filters.yaml` | `0600`, atomic; directory `0700` | `jira_cli/services/filters.py` |
| Plan state `*.applied.json` | `0600`, atomic | `jira_cli/services/plan.py` |
| Env file and config directory | `0600` / `0700` | `install.sh` |

Report export refuses a target that is a directory or lies inside a `.git`
directory (matched case-insensitively, ignoring trailing dots and spaces).
`safe_filename()` turns arbitrary text into a file name using only
`[A-Za-z0-9._-]`, with no leading dot or dash and at most 100 characters.
Where the file system has no POSIX modes (FAT, SMB), the `chmod` is skipped.

**CSV formula escaping.** In CSV exports, a cell that begins with `=`, `+`,
`-`, `@`, a tab or a carriage return is prefixed with a single quote, so a
spreadsheet does not execute it as a formula.

### Repository hygiene

`.gitignore` excludes `.env`, `.env*`, `*.env` (keeping `.env.example`, which
contains placeholders only), `teams.yaml`, `filters.yaml`, `*.applied.json`,
`plans/` and `reports/`.

## Coding rules enforced in this repository

- No `shell=True`, no `os.system`, no `eval`, no `exec`, no `pickle`.
- `yaml.safe_load` only — plan files, `teams.yaml` and `filters.yaml` never
  construct objects.
- Every subprocess argument vector is a list.

## Automated checks

CI (`.github/workflows/ci.yml`) runs on every push and pull request:

- `ruff check .` — rule sets `E`, `F`, `W`, `I`, `B`, `UP` and `S`
  (flake8-bandit), line length 110
- `pytest -q` — offline, no network access
- `bandit -r jira_cli -ll`
- `pip-audit` — known vulnerabilities in the dependency tree
- `gitleaks` over the **full history** (`fetch-depth: 0`, `--redact`)

The workflow declares `permissions: contents: read`.

Scanners are a floor, not a ceiling. A clean run is not an audit.

## Known limits

- The MCP confirmation flow proves that a second call repeated the first one;
  it cannot prove that a human read the preview. Use `JIRA_READ_ONLY=1` for an
  agent you have not evaluated.
- Env-file permission and owner problems produce warnings, not refusals.
- A CA-bundle path in `JIRA_VERIFY_TLS` is passed to `requests` as given; it
  is not checked for existence when settings are loaded.
- The MCP plan-directory confinement does not apply to the CLI, which trusts
  the person at the keyboard.
