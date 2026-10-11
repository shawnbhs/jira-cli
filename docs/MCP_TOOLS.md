# MCP tools

The package ships an MCP server (FastMCP, stdio transport, server name
`jira`). Start it with any of:

```bash
jira-mcp                       # console script
jira mcp                       # same server, via the CLI
python -m jira_cli.mcp_server  # same server, as a module
```

It reads exactly the same configuration as the CLI — see
[.env.example](../.env.example). Settings and the HTTP client are built lazily
on the first tool call, so the server starts even with a broken configuration
and every tool then returns a clear configuration error instead.

The server exposes **48 tools**: **31 read tools** and **17 write tools**.
Every tool name is prefixed `jira_`.

Related: [README.md](../README.md) · [COMMANDS.md](COMMANDS.md) ·
[PLAN_FORMAT.md](PLAN_FORMAT.md)

## Result shapes

No tool ever raises. Every result is a JSON object with a `status`:

| `status` | When |
|---|---|
| `ok` | A read tool succeeded. |
| `needs_confirmation` | First call of a write tool: preview plus token, nothing changed. |
| `done` | Second, approved call of a write tool: the change was made. |
| `rejected` | Write refused: read-only mode, or a bad/expired/reused token. |
| `error` | Anything failed: `{"status": "error", "error": "<redacted message>", "type": "<class name>"}`. |

List-returning read tools take a `limit` (default 50, clamped to `1..500`)
and report `count`, `total` and `truncated`, so a cut-off list is never
mistaken for a complete one. Report tools return the report shape
`{"title", "columns", "rows", "summary", "meta"}` with the same
`total`/`truncated` markers on `rows`.

Anywhere a tool takes `people`, it accepts `team:NAME`, `@NAME` (a team from
the local `teams.yaml`) or a comma-separated username list.

## Read tools (31) — execute immediately

### Session

| Tool | Description |
|---|---|
| `jira_me()` | Return the Jira account this server is authenticated as. |
| `jira_doctor()` | Run a configuration and connectivity self-check. Never prints the token; reports whether writes are disabled. |

### Projects, boards, sprints

| Tool | Description |
|---|---|
| `jira_list_projects(limit=50)` | List visible Jira projects as key, name, lead and type. |
| `jira_get_project(key)` | Show one project with its lead, issue types, components and versions. |
| `jira_list_boards(project=None, board_type=None, limit=50)` | List agile boards, optionally filtered by project key and board type. |
| `jira_board_for_project(project)` | Find the agile board belonging to a project key. |
| `jira_list_sprints(board_id, state="active,future", limit=50)` | List the sprints of a board. |

### Issues

| Tool | Description |
|---|---|
| `jira_get_issue(key, comments=True, changelog=False)` | Show one issue in normalised form. |
| `jira_search(jql, limit=50)` | Search issues with JQL and return them normalised. |
| `jira_my_issues(include_done=False, limit=50)` | List the issues assigned to the authenticated user. |
| `jira_list_transitions(key)` | List the workflow transitions currently available on an issue. |
| `jira_list_comments(key, limit=50)` | List the comments on an issue. |

### People and teams

| Tool | Description |
|---|---|
| `jira_search_users(query, limit=50)` | Search Jira users by name, username or email fragment. |
| `jira_assignable_users(project, query="", limit=50)` | List users who can be assigned issues in a project. |
| `jira_list_teams()` | List the locally defined teams with their members, projects and boards. |
| `jira_show_team(name)` | Show one locally defined team. |

### Reports

| Tool | Description |
|---|---|
| `jira_team_dashboard(people, projects=None, since="7d", limit=50)` | Per-person dashboard: open, in progress, done, overdue, stale and logged time. |
| `jira_person_activity(user, since="7d", until=None, projects=None, limit=50)` | Timeline of one person's Jira activity: creates, status changes, comments, worklogs. |
| `jira_stale_issues(days=7, projects=None, people=None, limit=50)` | Unresolved issues not updated for a number of days. |
| `jira_overdue_issues(projects=None, people=None, limit=50)` | Unresolved issues whose due date has passed. |
| `jira_incomplete_issues(projects=None, people=None, checks=None, limit=50)` | Issues missing required metadata (description, priority, estimate, assignee, due). |
| `jira_unassigned_issues(projects, limit=50)` | Unresolved issues with no assignee in the given projects. |
| `jira_workload(people, projects=None, limit=50)` | Open issue count and remaining estimate per person. |
| `jira_worklog_report(people, since="7d", until=None, projects=None, limit=50)` | Logged time per person per issue over a window, with totals. |
| `jira_sprint_report(board_id, sprint_id=None, limit=50)` | Sprint report: committed, completed, not completed and added after start. |
| `jira_velocity(board_id, last=5, limit=50)` | Velocity over the most recent closed sprints of a board. |

### Filters

| Tool | Description |
|---|---|
| `jira_list_filters()` | List the locally saved JQL filters (name, JQL, description). |
| `jira_run_filter(name, limit=50)` | Run a locally saved JQL filter and return the matching issues. |

### Plans

| Tool | Description |
|---|---|
| `jira_plan_template()` | Return a commented YAML plan template to fill in with the user. |
| `jira_plan_validate(plan)` | Validate a plan against Jira without changing anything. Returns `{"ok", "errors", "warnings"}`. |
| `jira_plan_preview(plan, limit=50)` | Preview the issues a plan would create, one row per issue, with estimate totals. |

`plan` is either inline YAML plan text or the path of a plan `.yaml`, `.yml`
or `.csv` file (at most 1 MB) inside the **plan directory** —
`$JIRA_CLI_PLAN_DIR`, default `<config dir>/plans`. Relative paths are taken
from that directory; a path that resolves (symlinks included) outside it is
rejected. YAML errors report only a line and column, never file content. Full
rules: [PLAN_FORMAT.md — MCP plan directory](PLAN_FORMAT.md#mcp-plan-directory).
Validating and previewing have no side effects, which is why they are read
tools even though they belong to a write workflow. See
[PLAN_FORMAT.md](PLAN_FORMAT.md).

## Write tools (17) — need a confirmation token

Every mutating tool takes an extra argument `confirm_token` (default `None`)
and runs in two steps.

**Step 1 — call without a token.** The server builds a side-effect-free
preview (GET requests only; edits show each field as `{"old", "new"}`) and
returns:

```json
{
  "status": "needs_confirmation",
  "preview": { "...": "exactly what will change" },
  "confirm_token": "x7Qd...",
  "expires_in": 900,
  "message": "Show this preview to the user and call again with confirm_token only after the user explicitly approves."
}
```

Nothing has changed in Jira at this point.

**Step 2 — call again with the token and the *identical* arguments.** The
server executes and returns:

```json
{ "status": "done", "result": { "key": "PROJ-41", "url": "https://jira.example.com/browse/PROJ-41" } }
```

**Rejection.** Anything else returns `{"status": "rejected", "reason": "..."}`:

| Cause | Why |
|---|---|
| any argument changed between the two calls | The token is bound to the tool name and to a SHA-256 of the canonical JSON of the arguments (excluding `confirm_token`). |
| the token was issued for a different tool | Same binding. |
| more than 15 minutes elapsed | Tokens expire (TTL 900 s). |
| the token was already used | Single use. Any match on the token value consumes it — even a mismatched one — so a tampered retry burns the approval. |
| unknown token | Not issued by this server process, or the process restarted. |
| `JIRA_READ_ONLY` is on | Refused on the *first* call: no preview, no token. |

Tokens come from `secrets.token_urlsafe(16)`, live in memory only (at most 256
pending, oldest evicted), and are compared in constant time. After a
rejection, call the tool again without a token to get a fresh preview.

This is the whole safety model for an agent driving Jira: the model can see
exactly what it is about to do, and a human — or a policy — can stop between
the two calls.

### Issues

| Tool | Description |
|---|---|
| `jira_create_issue(project, summary, issue_type="Task", description=None, assignee=None, priority=None, labels=None, due_date=None, original_estimate=None, epic_key=None, parent_key=None, story_points=None, sprint_id=None, confirm_token=None)` | Create a Jira issue. `parent_key` makes it a sub-task. |
| `jira_update_issue(key, changes, confirm_token=None)` | Update fields of an existing issue. Allowed `changes` keys: `summary`, `description`, `priority`, `labels`, `due_date`, `original_estimate`, `remaining_estimate`, `story_points`, `components`, `fix_versions`. |
| `jira_assign(key, user=None, confirm_token=None)` | Assign or unassign an issue. `user` is a username or a team token resolving to one person; omit it or pass `none` to unassign. |
| `jira_transition(key, to, comment=None, confirm_token=None)` | Move an issue through a workflow transition. `to` is a transition name, target status name or transition id. |
| `jira_add_comment(key, body, confirm_token=None)` | Add a comment to an issue. |
| `jira_link_issues(from_key, to_key, link_type="Relates", confirm_token=None)` | Link two issues. |
| `jira_add_worklog(key, time_spent, started=None, comment=None, confirm_token=None)` | Log work against an issue. |
| `jira_set_estimate(key, original=None, remaining=None, confirm_token=None)` | Set the original and/or remaining estimate of an issue. |
| `jira_delete_issue(key, confirm_token=None)` | Delete an issue permanently. This cannot be undone. |

### Projects, boards, sprints

| Tool | Description |
|---|---|
| `jira_create_project(key, name, lead, template="kanban", description=None, confirm_token=None)` | Create a Jira project. `template` is `kanban`, `scrum` or `basic`. |
| `jira_ensure_board(project, board_type="kanban", name=None, confirm_token=None)` | Make sure a project has an agile board, creating one if needed. |
| `jira_create_sprint(board_id, name, start=None, end=None, goal=None, confirm_token=None)` | Create a sprint on a board. |
| `jira_add_to_sprint(sprint_id, keys, confirm_token=None)` | Move issues into a sprint. |

### Teams and filters (local files)

| Tool | Description |
|---|---|
| `jira_save_team(name, members, projects=None, boards=None, confirm_token=None)` | Create or replace a local team definition in `teams.yaml`. |
| `jira_remove_team(name, confirm_token=None)` | Delete a local team definition. |
| `jira_save_filter(name, jql, description=None, confirm_token=None)` | Save a named JQL filter locally in `filters.yaml`. |

These change local files, not Jira, but still go through the confirmation
flow and are still refused in read-only mode.

### Plans

| Tool | Description |
|---|---|
| `jira_plan_apply(plan, dry_run=True, state_path=None, confirm_token=None)` | Create every issue in a plan. The preview is the validation result plus the row-per-issue preview. With `dry_run=True` (the default) nothing is created even after confirmation. |

`jira_plan_apply` is the one tool that can create many issues from a single
confirmation, so read its preview carefully, and pass `dry_run=False` to
create for real. Each created issue is recorded in the state file
(`state_path`, default `<plan-stem>.applied.json` beside a plan file; an
explicit `state_path` must name a `*.applied.json` file inside the plan
directory) so a re-run skips what already exists.

### CLI-only operations

A few write operations exist only in the CLI, not as MCP tools: creating a
component or version (`jira projects component` / `jira projects version`),
moving issues back to the backlog (`jira sprint backlog`), deleting a saved
filter (`jira filter delete`) and writing a plan template file
(`jira plan new`). Several read views are CLI-only too (`jira projects types`,
`priorities`, `statuses`, `jira boards show`, `jira boards issues`,
`jira sprint active`, `jira issue linktypes`, `jira issue worklogs`,
`jira filter server`). See [COMMANDS.md](COMMANDS.md).

## Read-only mode

With `JIRA_READ_ONLY=1` in the environment or the env file, **every write
tool returns `{"status": "rejected", ...}` on the first call — no preview and
no token is issued**, so there is no path to a mutation at all. Read tools are
unaffected. `jira_doctor` reports whether writes are disabled.

In the HTTP client, the only `POST` allowed in read-only mode is JQL search,
and only when the request path is exactly `/rest/api/2/search` under the base
URL with no query string.

This is the recommended setting for an agent you are still evaluating.

## Input validation

Every issue key, project key and numeric id an agent passes is validated
before a request is built. Lower-case keys are accepted and upper-cased
(`proj-12` -> `PROJ-12`); an all-digit issue argument is a numeric issue id;
anything else — for example `PROJ-1/../../project/PROJ` — comes back as
`{"status": "error", "type": "InputError", ...}`. Rules:
[SECURITY.md — Identifier validation](../SECURITY.md#identifier-validation).

## Environment

The server reads the same variables as the CLI (full table:
[README.md — Environment variables](../README.md#environment-variables)).
Two matter specifically here:

| Variable | Effect on the MCP server |
|---|---|
| `JIRA_READ_ONLY` | Every write tool is rejected on the first call. |
| `JIRA_CLI_PLAN_DIR` | The only directory plan tools read plan files from and write `*.applied.json` state to. Default `<config dir>/plans`. |

Configuration warnings (plain `http://` allowed via `JIRA_ALLOW_HTTP=1`, TLS
verification off, a loose env-file mode or owner) are printed once on the
server's stderr, not returned to the client.

## Registering the server

### Claude Code

```bash
claude mcp add jira -- jira-mcp
```

Or, with an explicit environment file rather than the default location:

```bash
claude mcp add jira --env JIRA_CLI_ENV_FILE=/home/alice/.config/jira-cli/.env -- jira-mcp
```

Verify with `claude mcp list`, then ask Claude to call `jira_me`.

### Hermes

Add the server to the Hermes MCP configuration:

```yaml
mcp_servers:
  jira:
    command: jira-mcp
    args: []
    env:
      JIRA_CLI_ENV_FILE: /home/alice/.config/jira-cli/.env
      JIRA_READ_ONLY: "1"      # drop this once you trust the write flow
```

Restart Hermes and confirm the `jira_*` tools appear.

### Any other MCP client

Command `jira-mcp`, no arguments, stdio transport. The server needs
`JIRA_BASE_URL` and a credential in its environment — either inherited from
the parent process or read from the environment file.

## What a tool never returns

- a token, password or any `Authorization` header
- a request header of any kind
- an unredacted Jira error body
- the content of a plan file that failed to parse

Every message, including errors raised by Jira itself, passes through
`redact()` first. See [SECURITY.md](../SECURITY.md).
