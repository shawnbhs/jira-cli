# Command reference

Every command, with its arguments and an example. The grammar is

```
jira [--env-file FILE] [--json] [--no-color] [--yes] [--debug] <command> [args]
```

Run `jira` with no command to get the interactive menu. Naming a command group
without a subcommand (for example `jira issue`) prints the help and exits `1`.
There is no `--version` flag; the installed version is
`pip show jira-cli-manager`.

Related: [README.md](../README.md) · [PLAN_FORMAT.md](PLAN_FORMAT.md) ·
[MCP_TOOLS.md](MCP_TOOLS.md)

## Global options

Global options go **before** the command.

| Option | Effect |
|---|---|
| `--env-file FILE` | Read configuration from `FILE` instead of the default env file. |
| `--json` | Print the raw result as JSON instead of text. For reports it is the same as `--format json`. stdout then carries only JSON: previews, the `Proceed?` prompt and notices go to stderr. |
| `--no-color` | Never emit ANSI colour. Colour is also off when `NO_COLOR` is set or output is not a TTY. |
| `--yes` | Answer `y` to the `Proceed? [y/N]` prompt on write commands (the answer is still echoed). Use deliberately. |
| `--debug` | Print a traceback on failure, redacted and terminal-safe. Same as `JIRA_CLI_DEBUG=1`. |

```bash
jira --env-file ./ci.env --json --no-color search 'project = PROJ AND status = Open'
```

Environment variables are listed in
[README.md — Environment variables](../README.md#environment-variables).

### Write commands

Every command marked **write** below prints exactly what it is about to do
and asks `Proceed? [y/N]` unless `--yes` was given. Only `y` or `yes`
proceeds; any other answer, EOF on stdin or Ctrl-C sends nothing, prints
`Aborted, nothing changed.` on stderr and exits `4`. Each preview line is
shown as one terminal-safe line. With `JIRA_READ_ONLY=1` every write to Jira
is refused with `ReadOnlyError`.

### Keys and ids

Issue keys, project keys and numeric ids (board, sprint) are validated before
any request. Lower-case keys are accepted and upper-cased (`proj-12` ->
`PROJ-12`); an issue argument made only of digits is used as a numeric issue
id; a value such as `PROJ-1/../x` is refused with an `error:` line (exit
`1`). `projects create` additionally requires 2–10 letters and digits.

### Output

Text that came from Jira is printed with terminal control characters, C1
controls and bidi overrides removed, so issue content cannot rewrite the
terminal. Failures print one redacted, terminal-safe `error: <message>` line
on stderr; an unexpected internal exception prints only its class name
(`error: unexpected ValueError`) plus the hint `rerun with --debug for
details`.

### Exit codes

Defined in `jira_cli/cli/main.py`:

| Code | Meaning |
|---|---|
| 0 | success |
| 1 | error — input error, Jira/HTTP error, read-only refusal, incomplete command, unexpected exception (also: invalid plan on `plan validate`, any failed issue on `plan apply`, a failed check in `doctor`) |
| 2 | configuration error (`ConfigError`) — a variable is missing or invalid, or `http://` without `JIRA_ALLOW_HTTP=1` |
| 3 | authentication error (`AuthError`) — hint printed: `run: jira doctor` |
| 4 | a write confirmation was declined — nothing was sent |
| 5 | local file or I/O error (`OSError`) |
| 130 | interrupted with Ctrl-C |

Argument errors detected by the parser itself exit `2` with a usage message.

## Session and configuration

### `jira me`

Who the configured credential belongs to.

```bash
jira me
```

### `jira doctor`

Check the settings, the env file and its permissions, server reachability,
authentication, whether writes are disabled, and custom field discovery. The first thing to run when a command fails.

```bash
jira doctor
```

### `jira config show`

Print the effective settings. The secret is rendered `set (N chars)` — never
its value.

```bash
jira config show
```

## Projects

### `jira projects list`

```bash
jira projects list
```

### `jira projects show KEY`

Project detail: lead, issue types, components, versions.

```bash
jira projects show PROJ
```

### `jira projects create KEY NAME --lead USER [--template kanban|scrum|basic] [--description TEXT]`

Create a project (default template `kanban`) and make sure it has an agile
board. **Write.**

```bash
jira projects create PROJ "Platform Rewrite" --lead alice --template scrum
```

### `jira projects component PROJECT NAME [--lead USER]`

Create a component. **Write.**

```bash
jira projects component PROJ "Importer" --lead alice
```

### `jira projects version PROJECT NAME [--release-date YYYY-MM-DD]`

Create a version. **Write.**

```bash
jira projects version PROJ 4.2 --release-date 2026-11-01
```

### `jira projects types PROJECT`

Issue types valid in the project.

```bash
jira projects types PROJ
```

### `jira projects priorities`

Priority names on the server.

```bash
jira projects priorities
```

### `jira projects statuses PROJECT`

Statuses used by the project.

```bash
jira projects statuses PROJ
```

## Boards and sprints

### `jira boards list [--project KEY] [--type kanban|scrum]`

```bash
jira boards list --project PROJ
```

### `jira boards show BOARD_ID`

```bash
jira boards show 12
```

### `jira boards ensure PROJECT [--type kanban|scrum] [--name NAME]`

Reuse the project's existing board if there is one, otherwise create it
(default type `kanban`). **Write.**

```bash
jira boards ensure PROJ --type scrum
```

### `jira boards issues BOARD_ID [--jql JQL] [--limit N]`

Issues on a board, optionally narrowed by JQL. Default limit 200.

```bash
jira boards issues 12 --jql 'assignee = alice'
```

### `jira sprints BOARD_ID [--state STATE]`

Default state filter is `active,future`. Pass `closed` for history.

```bash
jira sprints 12
jira sprints 12 --state closed
```

### `jira sprint active BOARD_ID`

The board's active sprint.

```bash
jira sprint active 12
```

### `jira sprint create BOARD_ID NAME [--start D] [--end D] [--goal TEXT]`

**Write.**

```bash
jira sprint create 12 "Sprint 27" --start 2026-10-13 --end 2026-10-27 --goal "Ship the import pipeline"
```

### `jira sprint add SPRINT_ID KEY [KEY ...]`

Move one or more existing issues into a sprint. **Write.**

```bash
jira sprint add 341 PROJ-12 PROJ-13 PROJ-14
```

### `jira sprint backlog KEY [KEY ...]`

Move issues out of their sprint, back to the backlog. **Write.**

```bash
jira sprint backlog PROJ-13 PROJ-14
```

## Issues

### `jira issue show KEY [--changelog]`

Full issue with comments; `--changelog` adds the change history.

```bash
jira issue show PROJ-12 --changelog
```

### `jira issue create`

```
jira issue create --project KEY --summary TEXT
       [--type TYPE] [--assignee USER] [--priority NAME] [--estimate DUR]
       [--due YYYY-MM-DD] [--label L ...] [--epic KEY] [--parent KEY] [--desc TEXT]
       [--components A,B] [--fix-versions A,B] [--story-points N] [--sprint SPRINT_ID]
```

Default type `Task`. `--parent` makes the new issue a sub-task of that key.
Repeat `--label` per label. `--sprint` adds the new issue to that sprint.
**Write.**

```bash
jira issue create --project PROJ --summary "Rotate the signing key" \
  --type Task --assignee alice --priority High --estimate 3h \
  --due 2026-10-20 --label security --label backend --epic PROJ-1 \
  --desc "Quarterly rotation; coordinate with the release freeze."
```

### `jira issue edit KEY`

```
jira issue edit KEY [--summary TEXT] [--desc TEXT] [--priority NAME] [--due D]
                    [--estimate DUR] [--remaining DUR] [--label L ...]
                    [--components A,B] [--fix-versions A,B] [--story-points N]
```

`--label` replaces all labels. **Write.**

```bash
jira issue edit PROJ-12 --priority Highest --remaining 2h --due 2026-10-15
```

### `jira issue assign KEY USER`

`USER` of `none`, `-` or `unassigned` unassigns. **Write.**

```bash
jira issue assign PROJ-12 bob
jira issue assign PROJ-12 none
```

### `jira issue transitions KEY`

List the transitions available from the issue's current status — run this when
a `jira issue move` is rejected as ambiguous or unavailable.

```bash
jira issue transitions PROJ-12
```

### `jira issue move KEY STATUS [--comment TEXT]`

`STATUS` matches a transition name, a target status name, or a transition id.
An ambiguous or unknown value fails and lists the options. **Write.**

```bash
jira issue move PROJ-12 "In Progress"
jira issue move PROJ-12 Done --comment "Deployed in release 4.2."
```

### `jira issue comment KEY TEXT`

**Write.**

```bash
jira issue comment PROJ-12 "Blocked on the vendor ticket; revisit Thursday."
```

### `jira issue comments KEY`

```bash
jira issue comments PROJ-12
```

### `jira issue link FROM TO [--type TYPE]`

Default link type is `Relates`. **Write.**

```bash
jira issue link PROJ-12 PROJ-30 --type Blocks
```

### `jira issue linktypes`

Link type names the server supports.

```bash
jira issue linktypes
```

### `jira issue delete KEY`

Irreversible. **Write.**

```bash
jira issue delete PROJ-99
```

### `jira issue worklog KEY TIME [--comment TEXT] [--started WHEN]`

`TIME` accepts `1w 2d 3h 30m`, `90m`, `1.5h`. `--started` defaults to now;
it also takes `YYYY-MM-DD`, `today`, `yesterday`, `3d` (three days ago) or a
full ISO timestamp. A date without a time is logged at midday. **Write.**

```bash
jira issue worklog PROJ-12 "2h 30m" --comment "Pairing on the migration" --started yesterday
```

### `jira issue worklogs KEY`

Worklog entries on an issue.

```bash
jira issue worklogs PROJ-12
```

### `jira issue estimate KEY [--original DUR] [--remaining DUR]`

**Write.**

```bash
jira issue estimate PROJ-12 --original 2d --remaining 6h
```

## Search

### `jira search JQL [--limit N]`

Default limit 50.

```bash
jira search 'project = PROJ AND status != Done ORDER BY updated DESC' --limit 100
```

### `jira mine [--all] [--limit N]`

Issues assigned to you. `--all` includes resolved issues. Default limit 200.

```bash
jira mine
jira mine --all
```

## Users

### `jira users search QUERY [--limit N]`

Default limit 50.

```bash
jira users search alice
```

### `jira users assignable PROJECT [QUERY]`

Who can actually be assigned in that project — use this before scripting an
assignment.

```bash
jira users assignable PROJ
jira users assignable PROJ ali
```

## Teams

Teams live in `teams.yaml` in the config directory (default
`~/.config/jira-cli/`) and are gitignored. A team is accepted anywhere a user
list is: `--team backend`, `--users team:backend`, `--users @backend`.

### `jira team list`

```bash
jira team list
```

### `jira team show NAME`

```bash
jira team show backend
```

### `jira team set NAME --members a,b [--projects X,Y] [--boards 1,2]`

Creates or replaces the team. **Write** (local file).

```bash
jira team set backend --members alice,bob --projects PROJ,OPS --boards 12,15
```

### `jira team remove NAME`

**Write** (local file).

```bash
jira team remove backend
```

## Reports

```
jira report KIND
     [--team NAME] [--users a,b|team:NAME] [--user U] [--projects X,Y]
     [--since 7d] [--until D] [--days N] [--board ID] [--sprint ID] [--last N]
     [--format table|md|csv|json] [--out FILE]

KIND: team | person | stale | overdue | incomplete | unassigned |
      workload | worklog | sprint | velocity
```

| Option | Default | Used by |
|---|---|---|
| `--team NAME` | — | people-based reports |
| `--users a,b` | — | people-based reports; also `team:NAME` |
| `--user U` | — | `person` |
| `--projects X,Y` | — | most reports |
| `--since` | `7d` | windowed reports |
| `--until` | today | windowed reports |
| `--days N` | `7` | `stale` |
| `--board ID` | — | `sprint`, `velocity` (required) |
| `--sprint ID` | the active sprint | `sprint` |
| `--last N` | `5` | `velocity` |
| `--format` | `table` | all |
| `--out FILE` | stdout | all — format follows the file suffix when `--format` is omitted |

`--since`/`--until` take `YYYY-MM-DD`, `today`, `yesterday`, or a relative
age such as `7d`, `2w`, `1m` (a month is 30 days), counted backwards from
today.

### `report team`

Per person: open to-do, in progress, done in the window, overdue, stale,
total open estimate, time logged.

```bash
jira report team --team backend --projects PROJ --since 7d
```

### `report person`

One person's event stream — issues created, status changes, assignee changes,
comments, worklogs.

```bash
jira report person --user alice --since 14d --format md --out reports/alice.md
```

### `report stale`

Not done and untouched for `--days` (default 7).

```bash
jira report stale --days 10 --projects PROJ,OPS
```

### `report overdue`

Past due date and not done.

```bash
jira report overdue --projects PROJ
```

### `report incomplete`

Issues missing hygiene fields — description, priority, estimate, assignee,
due date.

```bash
jira report incomplete --projects PROJ --team backend
```

### `report unassigned`

```bash
jira report unassigned --projects PROJ,OPS
```

### `report workload`

Open issues and remaining estimate per person.

```bash
jira report workload --team backend
```

### `report worklog`

Time logged per person per issue, with totals.

```bash
jira report worklog --users alice,bob --since 2026-10-01 --until 2026-10-07 --format csv --out reports/week40.csv
```

### `report sprint`

Committed, completed, not completed, added after start. Defaults to the
board's active sprint when `--sprint` is omitted.

```bash
jira report sprint --board 12
jira report sprint --board 12 --sprint 341 --format md --out reports/sprint-27.md
```

### `report velocity`

Committed vs completed over the last `--last` closed sprints (default 5).

```bash
jira report velocity --board 12 --last 8
```

## Export

### `jira export FILE [--format table|md|csv|json] [--out FILE]`

Re-render a report previously saved as JSON (`--format json --out x.json`)
into another format, without querying Jira again. Files written with `--out`
(here and on every report command) are created `0600` with an atomic rename;
any parent directories the tool creates are `0700`. A target that is a
directory or lies inside a `.git` directory is refused. In CSV output, a cell
that starts with `=`, `+`, `-`, `@`, a tab or a carriage return is prefixed
with `'` so a spreadsheet does not run it as a formula.

```bash
jira report team --team backend --format json --out reports/team.json
jira export reports/team.json --format md --out reports/team.md
```

## Saved filters

Stored in `filters.yaml` in the config directory (gitignored).

### `jira filter list`

```bash
jira filter list
```

### `jira filter save NAME JQL [--description TEXT]`

**Write** (local file).

```bash
jira filter save hotspots 'project = PROJ AND priority in (High, Highest) AND status != Done'
```

### `jira filter run NAME [--limit N]`

Default limit 50.

```bash
jira filter run hotspots
```

### `jira filter delete NAME`

**Write** (local file).

```bash
jira filter delete hotspots
```

### `jira filter server`

Your favourite filters as defined on the Jira server (read-only).

```bash
jira filter server
```

## Plan files

The draft-then-create workflow. Format and semantics:
[PLAN_FORMAT.md](PLAN_FORMAT.md).

### `jira plan new FILE`

Write a commented template. Refuses to overwrite an existing file.

```bash
jira plan new sprint27.yaml
```

### `jira plan validate FILE`

Check the plan locally and against the live instance. Prints errors and
warnings; exits `1` when there is any error.

```bash
jira plan validate sprint27.yaml
```

### `jira plan preview FILE [--format table|md|csv|json] [--out FILE]`

One row per issue plus estimate totals. Changes nothing.

```bash
jira plan preview sprint27.yaml --format md
```

### `jira plan apply FILE [--dry-run] [--state STATE_FILE]`

Create the issues, parents before children. Each success is recorded in the
state file (default `<plan-stem>.applied.json` beside the plan, written
`0600`), so a re-run skips what already exists. `--dry-run` validates and
creates nothing (add `--json` to see the planned creation order).
Refuses a plan with validation errors. Exits `1` if any issue failed, `4` if
the confirmation is declined.
**Write.**

The CLI reads plan and state files from any path you give it. The
`$JIRA_CLI_PLAN_DIR` confinement applies only to the MCP plan tools (see
[PLAN_FORMAT.md](PLAN_FORMAT.md#mcp-plan-directory)).

```bash
jira plan apply sprint27.yaml --dry-run
jira plan apply sprint27.yaml
```

## MCP server

### `jira mcp`

Run the MCP server over stdio in the foreground. Equivalent to the `jira-mcp`
console script. Tool list and the confirmation flow:
[MCP_TOOLS.md](MCP_TOOLS.md).

```bash
jira mcp
```
