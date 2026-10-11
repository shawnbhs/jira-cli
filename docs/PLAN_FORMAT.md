# Plan file format

A plan is a YAML file describing a batch of issues to create. It exists so
the list can be drafted, reviewed by a human, corrected and only then
applied — and so applying it twice does not create everything twice.

The CLI workflow is four commands:

```bash
jira plan new      sprint27.yaml   # write a commented template
jira plan validate sprint27.yaml   # check it locally and against the live instance
jira plan preview  sprint27.yaml   # one row per issue + estimate totals
jira plan apply    sprint27.yaml   # create, parents before children, resumable
```

Over MCP the same steps are `jira_plan_template`, `jira_plan_validate`,
`jira_plan_preview` and `jira_plan_apply`; there the plan may also be inline
YAML text or a [CSV file](#csv-form-mcp-only). An MCP plan *file* must live in
the [MCP plan directory](#mcp-plan-directory).

Related: [COMMANDS.md](COMMANDS.md) · [MCP_TOOLS.md](MCP_TOOLS.md) ·
[README.md](../README.md)

## Shape

```yaml
name: "Sprint 27"            # optional
defaults:
  project: PROJ
  board: 12
  sprint: active
  issue_type: Task
  priority: Medium
  labels: [ai]
  estimate_by: human

issues:
  - ref: t1
    summary: "Extract the import pipeline into a service"
    description: |
      Current importer runs inside the web process and blocks requests.
      Move it behind a queue.
    assignee: alice
    estimate: 2d
    estimate_by: human
    due: 2026-10-20
    type: Story
    epic: PROJ-12
    labels: [backend]
    story_points: 5

  - ref: t2
    summary: "Add the queue consumer"
    parent: t1
    assignee: bob
    estimate: ask
    links:
      - { type: Blocks, to: t1 }

  - ref: t3
    summary: "Write the runbook"
    estimate: 3h
    estimate_by: agent
    assignee: alice

  - ref: t4
    summary: "Spike: evaluate the queue library"
    estimate: none
```

Unknown keys are rejected — at the top level, in `defaults`, in an issue and
in a link — and the message names the offending ref and field.

## Top level

| Key | Meaning |
|---|---|
| `issues` | **Required.** List of issues (see below). |
| `defaults` | Optional mapping applied to every issue that does not set the field itself. |
| `name`, `description`, `version` | Optional, informational. |

## `defaults`

All keys optional. An issue's own value wins; `labels` are merged instead.

| Key | Meaning |
|---|---|
| `project` | Project key. Required, here or per issue. |
| `board` | Board id, used to resolve `sprint: active`. |
| `sprint` | `active` (the board's active sprint) or a numeric sprint id. Omit for the backlog. |
| `issue_type` / `type` | Default issue type name. Falls back to `Task`. |
| `priority` | Default priority name. |
| `labels` | Labels added to every issue, merged with the issue's own labels. |
| `assignee` | Default assignee username. |
| `epic` | Default existing epic key. |
| `estimate` | Default estimate (a duration, `ask` or `none`). |
| `estimate_by` | Default estimate owner: `human` or `agent`. |
| `due` | Default due date. |
| `components` | List of component names. |
| `fix_versions` | List of version names. |

## `issues`

A list. Each entry:

| Key | Type | Meaning |
|---|---|---|
| `ref` | string | Plan-local id that `parent` and `links[].to` point at, and the key used to record what was already created. Must be unique. Never sent to Jira. If omitted, the issue gets `auto<N>` (its 1-based position) — give every issue an explicit `ref` so resumes stay stable when the list is edited. |
| `summary` | string | **Required**, non-empty. |
| `description` | string | Issue description (Jira wiki markup). A YAML block scalar (`\|`) keeps newlines. |
| `assignee` | username | Jira Server username, not an email or display name. Validated as assignable in the project. |
| `estimate` | string | Original estimate. See [estimate semantics](#estimate-semantics). |
| `estimate_by` | `human` \| `agent` | Who produced the estimate. See [estimate semantics](#estimate-semantics). |
| `due` | date | `YYYY-MM-DD` (recommended). `today` and `yesterday` also work. Relative values such as `3d`, `2w`, `1m` are counted **backwards** from today (the same parser serves report windows), so they produce a past date — use an explicit date for a deadline. |
| `type` / `issue_type` | string | Issue type name. Overrides the default. |
| `priority` | string | Priority name. |
| `project` | string | Project key. Overrides `defaults.project`. |
| `epic` | issue key | An **existing** epic key such as `PROJ-12`. Not a `ref`. Validated as an issue key before it is sent. |
| `parent` | `ref` or key | Creates this issue as a sub-task of another entry's `ref` or of an existing issue key. A value is treated as an existing key only when it is already a valid upper-case key (`PROJ-12`); `t-1` stays a `ref`. |
| `labels` | list of strings | Merged with `defaults.labels`. |
| `components` | list of strings | Component names. |
| `fix_versions` | list of strings | Version names. |
| `story_points` | number | Written to the Story Points field. |
| `sprint` | `active` or id | Overrides `defaults.sprint`. |
| `board` | number | Overrides `defaults.board`. |
| `links` | list | Each `{type: <link type name>, to: <ref or issue key>}`, e.g. `Blocks`, `Relates`. Created after all issues exist. |

### Estimate semantics

`estimate` has three forms, and the distinction is the point of the field:

| Value | Meaning |
|---|---|
| a duration — `30m`, `4h`, `2d`, `1w 2d 3h`, `1.5h` | Set as the original estimate. |
| `ask` | **A human still has to decide.** `validate` reports it as an **error**, so a plan with any `ask` left cannot be applied. `preview` shows `ask` and lists the ref under `needs_estimate_answer`. |
| `none` | **Deliberately unestimated.** No error, no warning, created with no estimate. |
| key omitted | Created with no estimate; no error or warning. |

`estimate_by` records who produced the number:

| Value | Meaning |
|---|---|
| `human` | You set it. No message. |
| `agent` | The agent suggested it. `validate` emits a **warning** for the row (never an error, so an agreed plan can still be applied) and `preview` lists the ref under `agent_estimated`. Change it to `human` once you have confirmed the number. |

Any other `estimate_by` value is rejected when the plan is loaded.

The intended loop with an agent: the agent drafts the list with
`estimate: ask` everywhere; you answer each one with a duration, with `none`,
or by telling the agent to estimate it — in which case it writes its guess
**and** `estimate_by: agent`. Validation fails while any `ask` remains and
warns on every agent estimate, so nothing is created before you agreed.

Durations use `JIRA_HOURS_PER_DAY` and `JIRA_DAYS_PER_WEEK` to convert `d`
and `w`, so `1d` is 8 hours by default.

## CSV form (MCP only)

The MCP plan tools accept a path to a `.csv` file in the
[MCP plan directory](#mcp-plan-directory); the CLI `plan` commands read YAML
only. One header row, one issue per line. Allowed columns:

```
ref,summary,description,assignee,estimate,due,type,epic,parent,labels,story_points,project,estimate_by
```

```csv
ref,summary,type,assignee,estimate,due,epic,parent,labels,story_points,estimate_by
t1,Extract the import pipeline,Story,alice,2d,2026-10-20,PROJ-12,,backend;api,5,human
t2,Add the queue consumer,Task,bob,ask,,,t1,backend,3,
t3,Write the runbook,Task,alice,none,,,,,,
```

- `summary` is a required column; an unknown column is an error.
- `labels` is **semicolon** separated.
- Empty cells are omitted; blank rows are skipped.
- CSV has no `defaults` block and no `links`. Use YAML when you need either.

## MCP plan directory

The MCP server does not let a client read arbitrary local files. When the
`plan` argument of `jira_plan_validate`, `jira_plan_preview` or
`jira_plan_apply` is a single line that is not itself a YAML mapping, it is
treated as a file path and these rules apply (`jira_cli/mcp_server.py`):

| Rule | Value |
|---|---|
| Plan directory | `$JIRA_CLI_PLAN_DIR` if set, otherwise `<config dir>/plans` (config dir is `$JIRA_CONFIG_DIR`, default `~/.config/jira-cli`). Not created automatically. |
| Relative paths | Taken from the plan directory. |
| Containment | After resolving symlinks and `..`, the file must be inside the plan directory. |
| File type | A regular file ending in `.yaml`, `.yml` or `.csv` (case-insensitive). |
| Size | At most 1 MB (1 048 576 bytes). |
| Encoding | UTF-8 (a BOM is accepted). |
| Error detail | A YAML syntax error reports only the line and column; schema or CSV errors report no file content. Run `jira plan validate FILE` in a terminal for details. |
| `state_path` (`jira_plan_apply`) | Must name a `*.applied.json` file inside the same plan directory, and not a directory. When omitted, the state file is `<plan-stem>.applied.json` beside a plan file; an inline plan has no state file. |

Multi-line `plan` text is always parsed as inline YAML. The CLI `jira plan`
commands are **not** confined to this directory.

Example: with `JIRA_CLI_PLAN_DIR=~/jira-plans`, the plan
`~/jira-plans/sprint27.yaml` is passed as `"plan": "sprint27.yaml"`, and
`"state_path": "sprint27.applied.json"` writes the state next to it.

## What `validate` checks

Result: `{"ok": bool, "errors": [{"ref", "field", "message"}], "warnings": [...]}`.
Errors block `apply`; warnings do not.

**Errors**

- schema: unknown keys, wrong types, an empty `issues` list
- `summary` missing or empty
- duplicate `ref`
- `parent` or `links[].to` that is neither a `ref` in this plan nor an issue
  key like `PROJ-12`; an issue that is its own parent or links to itself; a
  `parent` cycle
- `estimate: ask`, or an estimate that does not parse as a duration
- `due` that does not parse as a date
- `project` missing, or unknown on the server
- issue `type` not available in the project
- `assignee` not assignable in the project
- `sprint` that is neither `active` nor a number; `sprint: active` without a
  numeric `board`; a board with no active sprint

**Warnings**

- `estimate_by: agent`
- no `assignee`; no `description`
- a server lookup that failed for a reason other than authentication (an
  authentication failure aborts validation instead)

## What `apply` does

- **Validates first.** A plan with any validation error is refused.
- **Parents before children.** Plan order, except that an issue is never
  created before its in-plan parent.
- **Resumable.** After *each* successful create, `ref -> {key, url}` is
  written to the state file — `<plan-stem>.applied.json` beside the plan by
  default, `--state FILE` (CLI) or `state_path` (MCP, confined to the
  [plan directory](#mcp-plan-directory)) to choose another — atomically
  and mode `0600`. A re-run skips every `ref` already in that file and retries
  only what failed. Fix the cause and run the same command again.
- **Sprint membership** is set right after each issue is created; a failure
  there becomes a warning on an issue that exists, not a lost issue.
- **Links** are created after all issues exist, and each created link is also
  recorded in the state file so a resume does not duplicate it.
- **Stops** at the first authentication or permission error; any other
  failure is recorded and the run continues.
- **Dry run** (`--dry-run`, or `dry_run=true` over MCP, which is the MCP
  default) validates and returns the planned creation order without creating
  anything.

The state file is gitignored (`*.applied.json`). Delete it only if you intend
the next `apply` to create everything again.

Return shape (abridged):

```json
{
  "ok": true,
  "dry_run": false,
  "stopped": false,
  "created": [{"ref": "t1", "key": "PROJ-41", "url": "https://jira.example.com/browse/PROJ-41", "warnings": []}],
  "skipped": [{"ref": "t2", "key": "PROJ-42", "url": "...", "reason": "already created in a previous run"}],
  "failed":  [{"ref": "t3", "error": "..."}],
  "links":   [{"from": "t2", "to": "t1", "type": "Blocks", "ok": true}],
  "order":   ["t1", "t2", "t3"],
  "planned": [],
  "validation": {"ok": true, "errors": [], "warnings": []},
  "warnings": [],
  "state_path": "sprint27.applied.json"
}
```

## Preview

`preview` returns the report shape with columns
`ref, project, type, summary, assignee, estimate, due, parent, sprint` and a
summary of `issues`, `total_estimate`, `without_estimate`,
`needs_estimate_answer`, `agent_estimated` and per-assignee counts and
estimate totals. It makes no requests and works on an invalid plan.

## Recommended flow

1. Draft the list with whoever owns the work. Leave `estimate: ask` wherever
   the number is not yours to pick.
2. `jira plan validate` — fix every error, read every warning.
3. `jira plan preview --format md` — paste it where the scope is being agreed.
4. The estimate owner replaces each `ask` with a duration or `none`, and flips
   any `estimate_by: agent` they accept to `human`.
5. `jira plan apply --dry-run`, then `jira plan apply`.
