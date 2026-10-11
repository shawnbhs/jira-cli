# Jira Server response shapes (sanitized fixtures)

Captured read-only (GET only) from a live **Jira Software Server 10.3.0**
(`deploymentType: Server`) by `scripts/capture_shapes.py`, then sanitized:
hosts -> `https://jira.example.com`, users -> `alice`/`bob`/`carol`/... with
`@example.com` addresses, project keys -> `PROJ`/`PRJ<n>`, project names ->
`Project <KEY>`, project categories -> `Example Corp`/`Example Client`,
component names -> `Component A..J`, board/sprint names -> `Sample name N`, all
free text (summaries, descriptions, comments, goals) -> `Sample text N`,
changelog user labels (`fromString`/`toString`) -> synthetic display names with
synthetic `JIRAUSER100nn` keys, every `timeZone` -> `UTC` and every timestamp
offset -> `+0000` / `+00:00`, org-specific custom field and project role names
-> neutral `Custom ...` names, avatars dropped, numeric ids kept.

These fixtures are for OFFLINE unit tests. They carry the *shape* of the real
server's responses; every identifying value has been replaced, and
`tests/test_fixture_hygiene.py` fails the suite if a real-looking email, host,
non-Latin-script name, or a term listed in `JIRA_FIXTURE_FORBIDDEN` reappears.

## Endpoints

| fixture | endpoint | status |
|---|---|---|
| `serverinfo.json` | `/rest/api/2/serverInfo` | 200 |
| `myself.json` | `/rest/api/2/myself` | 200 |
| `project_list.json` | `/rest/api/2/project` | 200 |
| `project_detail.json` | `/rest/api/2/project/PRJ19` | 200 |
| `createmeta.json` | `/rest/api/2/issue/createmeta` | 404 |
| `createmeta_issuetypes.json` | `/rest/api/2/issue/createmeta/PRJ19/issuetypes` | 200 |
| `createmeta_fields.json` | `/rest/api/2/issue/createmeta/PRJ19/issuetypes/10000` | 200 |
| `board_list.json` | `/rest/agile/1.0/board` | 200 |
| `scrum_board_lookup.json` | `/rest/agile/1.0/board` | 200 |
| `scrum_board_sprints.json` | `/rest/agile/1.0/board/18/sprint` | 200 |
| `scrum_board_sprints_1.json` | `/rest/agile/1.0/board/17/sprint` | 200 |
| `scrum_board_sprints_2.json` | `/rest/agile/1.0/board/15/sprint` | 200 |
| `scrum_board_sprints_3.json` | `/rest/agile/1.0/board/23/sprint` | 200 |
| `scrum_board_sprints_4.json` | `/rest/agile/1.0/board/13/sprint` | 200 |
| `scrum_board_sprints_5.json` | `/rest/agile/1.0/board/28/sprint` | 200 |
| `scrum_board_sprints_6.json` | `/rest/agile/1.0/board/25/sprint` | 200 |
| `scrum_board_sprints_7.json` | `/rest/agile/1.0/board/24/sprint` | 200 |
| `search_changelog.json` | `/rest/api/2/search` | 200 |
| `issue_worklog.json` | `/rest/api/2/issue/PRJ21-567/worklog` | 200 |
| `issue_comments.json` | `/rest/api/2/issue/PRJ21-567/comment` | 200 |
| `field_list.json` | `/rest/api/2/field` | 200 |
| `user_search.json` | `/rest/api/2/user/search` | 200 |
| `user_assignable.json` | `/rest/api/2/user/assignable/search` | 200 |
| `priority.json` | `/rest/api/2/priority` | 200 |
| `project_statuses.json` | `/rest/api/2/project/PRJ19/statuses` | 200 |
| `issue_link_types.json` | `/rest/api/2/issueLinkType` | 200 |
| `filter_favourite.json` | `/rest/api/2/filter/favourite` | 200 |
| `greenhopper_velocity.json` | `/rest/greenhopper/1.0/rapid/charts/velocity.json` | 200 |
| `greenhopper_sprintpicker.json` | `/rest/greenhopper/1.0/sprint/picker` | 200 |
| `sprint_detail.json` | `/rest/agile/1.0/sprint/6` | 200 |
| `sprint_issues.json` | `/rest/agile/1.0/sprint/6/issue` | 200 |
| `greenhopper_sprintreport.json` | `/rest/greenhopper/1.0/rapid/charts/sprintreport` | 200 |
| `greenhopper_velocity.json` | `/rest/greenhopper/1.0/rapid/charts/velocity.json` | 200 |
| `greenhopper_sprintpicker.json` | `/rest/greenhopper/1.0/sprint/picker` | 200 |
| `sprint_detail.json` | `/rest/agile/1.0/sprint/6` | 200 |
| `sprint_issues.json` | `/rest/agile/1.0/sprint/6/issue` | 200 |
| `greenhopper_sprintreport.json` | `/rest/greenhopper/1.0/rapid/charts/sprintreport` | 200 |

## SURPRISES (read before writing a parser)

### 1. The Sprint custom field is a Java `toString()` blob, not an object

On Jira **Server** the Sprint field (`customfield_10109` here) comes back as a
`list[str]`, each element being a serialized Java object:

```
com.atlassian.greenhopper.service.sprint.Sprint@1a2b3c[id=123,rapidViewId=45,
state=ACTIVE,name=Sample Sprint,startDate=2026-01-01T00:00:00.000Z,
endDate=...,completeDate=<null>,activatedDate=...,sequence=123,goal=Sample text,
autoStartStop=false,synced=false]
```

Implications:

* It is a **list**, so an issue can carry several sprints; the *last* element is
  the current one.
* `completeDate=<null>` - the literal string `<null>`, not JSON `null`.
* Parse with a regex over `key=value` pairs up to the next `,` or `]`. Values are
  NOT quoted and may themselves contain `,` in free text (the name/goal), so a
  naive `split(",")` corrupts names. Prefer named-key regexes
  (`id=(\d+)`, `state=([A-Z]+)`, `name=(.*?),startDate=`).
* On Jira **Cloud** the same field is `list[dict]`. Code must handle both.
* `/rest/agile/1.0/sprint/{id}` and the board sprint list DO return proper JSON
  objects - prefer those whenever a sprint id is already known.

### 2. Assignee / reporter keys are Server-shaped, not Cloud-shaped

A user object here carries:

```
self, name, key, emailAddress, avatarUrls, displayName, active, timeZone, locale?
```

* `name` is the **username** - this is what `PUT /issue/{key}/assignee` wants as
  `{"name": "<username>"}`. There is **no `accountId`** (Cloud only).
* `key` is a separate, stable internal user key and is often *equal* to `name` on
  older installs but must not be assumed equal.
* `displayName` may be non-ASCII on the real server; never use it as an identifier.
* Unassigned issues have `fields.assignee = null` (key present, value null).

### 3. `statusCategory` keys are the stable signal, `name` is not

`fields.status.statusCategory` is:

```
{self, id, key, colorName, name}
```

* `key` is one of exactly `new` | `indeterminate` | `done` (stable, API-level).
* `name` is the localised label (`To Do` | `In Progress` | `Done`) and will change
  with the instance language. **Branch on `key`, map to `name` only for display.**
* `id` is 2 (new), 4 (indeterminate), 3 (done) on this server.
* `colorName` is e.g. `blue-gray`, `yellow`, `green` - presentation only.

### 4. `timetracking` keys come in pairs and may be entirely absent

`fields.timetracking` is `{}` (empty dict) when nothing is tracked. When populated:

```
originalEstimate: str        e.g. "1d 4h"      (human)
remainingEstimate: str       e.g. "3h"
timeSpent: str               e.g. "5h 30m"
originalEstimateSeconds: int
remainingEstimateSeconds: int
timeSpentSeconds: int
```

* The `*Seconds` variants are the ones to compute with; the string forms are
  rendered with the instance's own hours-per-day / days-per-week settings.
* The SAME numbers are ALSO exposed as sibling top-level fields
  `fields.timeoriginalestimate`, `fields.timeestimate`, `fields.timespent`
  (all `int|None`, seconds). They can disagree with `timetracking` when a field
  configuration hides one of them - prefer `timetracking` and fall back.
* `aggregatetimeoriginalestimate` / `aggregatetimeestimate` /
  `aggregatetimespent` include sub-tasks.
* Any of these keys is simply MISSING (not null) when `fields=` restricted the
  response - always `.get()`.

### 5. GreenHopper layout is flat, undocumented and not REST

`/rest/greenhopper/1.0/rapid/charts/sprintreport?rapidViewId=&sprintId=`:

```
contents.completedIssues: list[dict]            # NOT normal issue objects
contents.issuesNotCompletedInCurrentSprint: list[dict]
contents.puntedIssues: list[dict]               # removed from the sprint
contents.issuesCompletedInAnotherSprint: list[dict]
contents.completedIssuesEstimateSum: {text: str, value: float|None}
contents.incompletedIssuesEstimateSum: {text, value}
contents.allIssuesEstimateSum: {text, value}
contents.puntedIssuesEstimateSum: {text, value}
contents.issueKeysAddedDuringSprint: {"PROJ-1": true, ...}   # dict, not list!
sprint: {id, name, state, startDate, endDate, completeDate, ...}  # DISPLAY dates
```

Implications:

* The per-issue dicts are a bespoke shape: `key, summary, typeName, typeId,
  priorityName, priorityUrl, done (bool), assignee, assigneeName, assigneeKey,
  avatarUrl, hasCustomUserAvatar, color, estimateStatistic, trackingStatistic,
  statusId, statusName, statusUrl, fixVersions, projectId`. There is no `fields`
  sub-object and no `self` URL.
* `estimateStatistic.statFieldValue` is `{value: float}` or `{}` - a missing
  estimate is an EMPTY DICT, not null.
* `issueKeysAddedDuringSprint` is a **dict keyed by issue key** mapping to `true`.
  Use `.keys()`; do not iterate it as a list.
* `sprint.startDate` / `endDate` here are **localised display strings**
  (e.g. `01/Jan/26 9:00 AM`), NOT ISO-8601. The ISO values live on
  `/rest/agile/1.0/sprint/{id}`.
* `*EstimateSum.value` is `None` when the board tracks issue count rather than
  story points.

`/rest/greenhopper/1.0/rapid/charts/velocity.json?rapidViewId=`:

```
sprints: list[{id, name, state, goal?, sequence, linkedPagesCount?}]
velocityStatEntries: { "<sprintId>": {estimated: {text, value},
                                      completed: {text, value}} }
```

* `velocityStatEntries` is a dict **keyed by the sprint id as a STRING**, while
  `sprints[].id` is an `int`. Cast before lookup - this is the single most common
  velocity bug.
* `sprints` is ordered oldest-first on this server, but `sequence` is the
  authoritative ordering key.
* This endpoint is unversioned and unsupported by Atlassian; guard every access
  and degrade gracefully rather than raising.

### 6. Other things worth knowing

* `/rest/api/2/project` (list form) does NOT include `lead`, `components` or
  `versions`; only `/rest/api/2/project/{key}` does.
* `/rest/agile/1.0/board` list items have **no `location.projectKey`** on this
  server, so a project's board can only be found via
  `?projectKeyOrId=<KEY>`.
* `/rest/api/2/search` returns `{expand, startAt, maxResults, total, issues}`.
  `total` is authoritative for counting (`maxResults=0`).
* `expand=changelog` puts history at `issues[].changelog.{startAt,maxResults,
  total,histories[]}` and each history is `{id, author, created, items[]}` with
  items `{field, fieldtype, fieldId?, from, fromString, to, toString}`.
  `from`/`to` are internal ids; `fromString`/`toString` are the labels.
  For `assignee` items `to` is the internal user key (`JIRAUSER10002`) and
  `toString` the display name - both identify a person, so the capture
  sanitizer rewrites them. `changelog.total` can exceed `len(histories)` when
  the embedded page is truncated; re-fetch `/issue/{key}?expand=changelog`
  then (the captured issues happen to be complete: `total == len`).
* `/rest/api/2/project/{key}/statuses` is a **list of issue types**, each with
  its own `statuses` list - the same status appears once per issue type.
* `/rest/api/2/field` entries are `{id, name, custom, orderable, navigable,
  searchable, clauseNames[], schema{type,items?,custom?,customId?}}`.
  System fields have no `schema.custom`.
* Worklog: `/issue/{key}/worklog` -> `{startAt, maxResults, total, worklogs[]}`,
  each `{self, author, updateAuthor, comment, created, updated, started,
  timeSpent, timeSpentSeconds, id, issueId}`. `started` is the one that matters
  for date filtering; `created` is when it was entered.


## Key fields and types

### board_list

```
maxResults: int
startAt: int
total: int
isLast: bool
values: list[1]
values[].id: int
values[].self: str
values[].name: str
values[].type: str
```

### createmeta_fields

```
maxResults: int
startAt: int
total: int
isLast: bool
values: list[14]
values[].required: bool
values[].schema: dict
values[].schema.type: str
values[].schema.custom: str
values[].schema.customId: int
values[].name: str
values[].fieldId: str
values[].hasDefaultValue: bool
values[].operations: list[1]
```

### createmeta_issuetypes

```
maxResults: int
startAt: int
total: int
isLast: bool
values: list[5]
values[].self: str
values[].id: str
values[].description: str
values[].name: str
values[].subtask: bool
```

### field_list

```
<root>: list[99]
[].id: str
[].name: str
[].custom: bool
[].orderable: bool
[].navigable: bool
[].searchable: bool
[].clauseNames: list[1]
[].schema: dict
[].schema.type: str
[].schema.items: str
[].schema.system: str
```

### filter_favourite

```
<root>: list[0]
```

### greenhopper_sprintpicker

```
suggestions: list[0]
allMatches: list[5]
allMatches[].name: str
allMatches[].id: int
allMatches[].stateKey: str
allMatches[].boardName: str
allMatches[].date: str
```

### greenhopper_sprintreport

```
contents: dict
contents.completedIssues: list[0]
contents.issuesNotCompletedInCurrentSprint: list[0]
contents.puntedIssues: list[0]
contents.issuesCompletedInAnotherSprint: list[0]
contents.entityData: dict
contents.entityData.statuses: dict
contents.entityData.priorities: dict
contents.entityData.types: dict
contents.entityData.epics: dict
contents.completedIssuesInitialEstimateSum: dict
contents.completedIssuesInitialEstimateSum.text: str
contents.completedIssuesEstimateSum: dict
contents.completedIssuesEstimateSum.text: str
contents.issuesNotCompletedInitialEstimateSum: dict
contents.issuesNotCompletedInitialEstimateSum.text: str
contents.issuesNotCompletedEstimateSum: dict
contents.issuesNotCompletedEstimateSum.text: str
contents.allIssuesEstimateSum: dict
contents.allIssuesEstimateSum.text: str
contents.puntedIssuesInitialEstimateSum: dict
contents.puntedIssuesInitialEstimateSum.text: str
contents.puntedIssuesEstimateSum: dict
contents.puntedIssuesEstimateSum.text: str
contents.issuesCompletedInAnotherSprintInitialEstimateSum: dict
contents.issuesCompletedInAnotherSprintInitialEstimateSum.text: str
contents.issuesCompletedInAnotherSprintEstimateSum: dict
contents.issuesCompletedInAnotherSprintEstimateSum.text: str
contents.issueKeysAddedDuringSprint: dict
sprint: dict
sprint.id: int
sprint.sequence: int
sprint.rapidViewId: int
sprint.name: str
sprint.state: str
sprint.goal: str
sprint.autoStartStop: bool
sprint.synced: bool
sprint.startDate: str
sprint.endDate: str
sprint.activatedDate: str
sprint.completeDate: str
sprint.canUpdateSprint: bool
sprint.canStartStopSprint: bool
sprint.canUpdateDates: bool
sprint.remoteLinks: list[0]
sprint.daysRemaining: int
lastUserToClose: str
userWhoStarted: str
supportsPages: bool
```

### greenhopper_velocity

```
sprints: list[0]
velocityStatEntries: dict
queryMetadata: dict
queryMetadata.allSprints: int
queryMetadata.querySprints: int
queryMetadata.maxSprints: int
queryMetadata.issuesLimit: int
queryMetadata.issuesInSprints: int
queryMetadata.queryLegacyMode: bool
```

### issue_comments

```
startAt: int
maxResults: int
total: int
comments: list[2]
comments[].self: str
comments[].id: str
comments[].author: dict
comments[].author.self: str
comments[].author.name: str
comments[].author.key: str
comments[].author.emailAddress: str
comments[].author.displayName: str
comments[].author.active: bool
comments[].author.timeZone: str
comments[].body: str
comments[].updateAuthor: dict
comments[].updateAuthor.self: str
comments[].updateAuthor.name: str
comments[].updateAuthor.key: str
comments[].updateAuthor.emailAddress: str
comments[].updateAuthor.displayName: str
comments[].updateAuthor.active: bool
comments[].updateAuthor.timeZone: str
comments[].created: str
comments[].updated: str
```

### issue_link_types

```
issueLinkTypes: list[16]
issueLinkTypes[].id: str
issueLinkTypes[].name: str
issueLinkTypes[].inward: str
issueLinkTypes[].outward: str
issueLinkTypes[].self: str
```

### issue_worklog

```
startAt: int
maxResults: int
total: int
worklogs: list[2]
worklogs[].self: str
worklogs[].author: dict
worklogs[].author.self: str
worklogs[].author.name: str
worklogs[].author.key: str
worklogs[].author.emailAddress: str
worklogs[].author.displayName: str
worklogs[].author.active: bool
worklogs[].author.timeZone: str
worklogs[].updateAuthor: dict
worklogs[].updateAuthor.self: str
worklogs[].updateAuthor.name: str
worklogs[].updateAuthor.key: str
worklogs[].updateAuthor.emailAddress: str
worklogs[].updateAuthor.displayName: str
worklogs[].updateAuthor.active: bool
worklogs[].updateAuthor.timeZone: str
worklogs[].comment: str
worklogs[].created: str
worklogs[].updated: str
worklogs[].started: str
worklogs[].timeSpent: str
worklogs[].timeSpentSeconds: int
worklogs[].id: str
worklogs[].issueId: str
```

### myself

```
self: str
key: str
name: str
emailAddress: str
displayName: str
active: bool
deleted: bool
timeZone: str
locale: str
groups: dict
groups.size: int
groups.items: list[0]
applicationRoles: dict
applicationRoles.size: int
applicationRoles.items: list[0]
expand: str
```

### priority

```
<root>: list[7]
[].self: str
[].statusColor: str
[].description: str
[].name: str
[].id: str
```

### project_detail

```
expand: str
self: str
id: str
key: str
description: str
lead: dict
lead.self: str
lead.key: str
lead.name: str
lead.displayName: str
lead.active: bool
components: list[10]
components[].self: str
components[].id: str
components[].name: str
components[].description: str
components[].isAssigneeTypeValid: bool
issueTypes: list[5]
issueTypes[].self: str
issueTypes[].id: str
issueTypes[].description: str
issueTypes[].name: str
issueTypes[].subtask: bool
assigneeType: str
versions: list[0]
name: str
roles: dict
roles.Custom Role A: str
roles.viewer: str
roles.Service Desk Team: str
roles.Developers: str
roles.Service Desk Customers: str
roles.Custom Role B: str
roles.Administrators: str
roles.ADMIN: str
roles.USER: str
projectTypeKey: str
archived: bool
```

### project_list

```
<root>: list[37]
[].expand: str
[].self: str
[].id: str
[].key: str
[].name: str
[].projectCategory: dict
[].projectCategory.self: str
[].projectCategory.id: str
[].projectCategory.name: str
[].projectCategory.description: str
[].projectTypeKey: str
[].archived: bool
```

### project_statuses

```
<root>: list[5]
[].self: str
[].id: str
[].name: str
[].subtask: bool
[].statuses: list[6]
[].statuses[].self: str
[].statuses[].description: str
[].statuses[].name: str
[].statuses[].id: str
[].statuses[].statusCategory: dict
[].statuses[].statusCategory.self: str
[].statuses[].statusCategory.id: int
[].statuses[].statusCategory.key: str
[].statuses[].statusCategory.colorName: str
[].statuses[].statusCategory.name: str
```

### scrum_board_lookup

```
maxResults: int
startAt: int
total: int
isLast: bool
values: list[20]
values[].id: int
values[].self: str
values[].name: str
values[].type: str
```

### scrum_board_sprints

```
maxResults: int
startAt: int
isLast: bool
values: list[0]
```

### search_changelog

```
expand: str
startAt: int
maxResults: int
total: int
issues: list[5]
issues[].expand: str
issues[].id: str
issues[].self: str
issues[].key: str
issues[].fields: dict
issues[].fields.issuetype: dict
issues[].fields.issuetype.self: str
issues[].fields.issuetype.id: str
issues[].fields.issuetype.description: str
issues[].fields.issuetype.name: str
issues[].fields.issuetype.subtask: bool
issues[].fields.timespent: int
issues[].fields.project: dict
issues[].fields.project.self: str
issues[].fields.project.id: str
issues[].fields.project.key: str
issues[].fields.project.name: str
issues[].fields.project.projectTypeKey: str
issues[].fields.fixVersions: list[0]
issues[].fields.customfield_10110: NoneType
issues[].fields.customfield_10111: NoneType
issues[].fields.aggregatetimespent: int
issues[].fields.resolution: dict
issues[].fields.resolution.self: str
issues[].fields.resolution.id: str
issues[].fields.resolution.description: str
issues[].fields.resolution.name: str
issues[].fields.customfield_10302: NoneType
issues[].fields.customfield_10700: NoneType
issues[].fields.customfield_10502: NoneType
issues[].fields.customfield_10305: NoneType
issues[].fields.customfield_10503: NoneType
issues[].fields.customfield_10306: NoneType
issues[].fields.customfield_10108: NoneType
issues[].fields.customfield_10504: NoneType
issues[].fields.customfield_10109: NoneType
issues[].fields.customfield_10505: NoneType
issues[].fields.customfield_10506: NoneType
issues[].fields.resolutiondate: str
issues[].fields.customfield_10705: NoneType
issues[].fields.customfield_10507: NoneType
issues[].fields.customfield_10706: NoneType
issues[].fields.customfield_10508: NoneType
issues[].fields.workratio: int
issues[].fields.lastViewed: NoneType
issues[].fields.watches: dict
issues[].fields.watches.self: str
issues[].fields.watches.watchCount: int
issues[].fields.watches.isWatching: bool
issues[].fields.created: str
issues[].fields.priority: dict
issues[].fields.priority.self: str
issues[].fields.priority.name: str
issues[].fields.priority.id: str
issues[].fields.customfield_10100: NoneType
issues[].fields.customfield_10101: NoneType
issues[].fields.customfield_10102: NoneType
issues[].fields.customfield_10300: NoneType
issues[].fields.labels: list[0]
issues[].fields.customfield_10103: NoneType
issues[].fields.customfield_10301: NoneType
issues[].fields.customfield_10731: dict
issues[].fields.customfield_10731.errorMessage: str
issues[].fields.customfield_10731.i18nErrorMessage: dict
issues[].fields.customfield_10731.i18nErrorMessage.i18nKey: str
issues[].fields.customfield_10731.i18nErrorMessage.parameters: list
issues[].fields.customfield_10732: dict
issues[].fields.customfield_10732.errorMessage: str
issues[].fields.customfield_10732.i18nErrorMessage: dict
issues[].fields.customfield_10732.i18nErrorMessage.i18nKey: str
issues[].fields.customfield_10732.i18nErrorMessage.parameters: list
issues[].fields.customfield_10733: dict
issues[].fields.customfield_10733.errorMessage: str
issues[].fields.customfield_10733.i18nErrorMessage: dict
issues[].fields.customfield_10733.i18nErrorMessage.i18nKey: str
issues[].fields.customfield_10733.i18nErrorMessage.parameters: list
issues[].fields.customfield_10734: dict
issues[].fields.customfield_10734.errorMessage: str
issues[].fields.customfield_10734.i18nErrorMessage: dict
issues[].fields.customfield_10734.i18nErrorMessage.i18nKey: str
issues[].fields.customfield_10734.i18nErrorMessage.parameters: list
issues[].fields.timeestimate: int
issues[].fields.aggregatetimeoriginalestimate: NoneType
issues[].fields.versions: list[0]
issues[].fields.issuelinks: list[0]
issues[].fields.assignee: dict
issues[].fields.assignee.self: str
issues[].fields.assignee.name: str
issues[].fields.assignee.key: str
issues[].fields.assignee.emailAddress: str
issues[].fields.assignee.displayName: str
issues[].fields.assignee.active: bool
issues[].fields.assignee.timeZone: str
issues[].fields.updated: str
issues[].fields.status: dict
issues[].fields.status.self: str
issues[].fields.status.description: str
issues[].fields.status.name: str
issues[].fields.status.id: str
issues[].fields.status.statusCategory: dict
issues[].fields.status.statusCategory.self: str
issues[].fields.status.statusCategory.id: int
issues[].fields.status.statusCategory.key: str
issues[].fields.status.statusCategory.colorName: str
issues[].fields.status.statusCategory.name: str
issues[].fields.components: list[0]
issues[].fields.timeoriginalestimate: NoneType
issues[].fields.description: str
issues[].fields.customfield_10730: NoneType
issues[].fields.customfield_10720: NoneType
issues[].fields.customfield_10600: list[0]
issues[].fields.customfield_10721: NoneType
issues[].fields.customfield_10601: NoneType
issues[].fields.customfield_10722: NoneType
issues[].fields.customfield_10602: list[0]
```

### serverinfo

```
baseUrl: str
version: str
versionNumbers: list[3]
deploymentType: str
buildNumber: int
buildDate: str
databaseBuildNumber: int
serverTime: str
scmInfo: str
serverTitle: str
```

### sprint_detail

```
id: int
self: str
state: str
name: str
startDate: str
endDate: str
activatedDate: str
originBoardId: int
goal: str
synced: bool
autoStartStop: bool
```

### sprint_issues

```
expand: str
startAt: int
maxResults: int
total: int
issues: list[3]
issues[].expand: str
issues[].id: str
issues[].self: str
issues[].key: str
issues[].fields: dict
issues[].fields.issuetype: dict
issues[].fields.issuetype.self: str
issues[].fields.issuetype.id: str
issues[].fields.issuetype.description: str
issues[].fields.issuetype.name: str
issues[].fields.issuetype.subtask: bool
issues[].fields.timespent: NoneType
issues[].fields.sprint: dict
issues[].fields.sprint.id: int
issues[].fields.sprint.self: str
issues[].fields.sprint.state: str
issues[].fields.sprint.name: str
issues[].fields.sprint.startDate: str
issues[].fields.sprint.endDate: str
issues[].fields.sprint.activatedDate: str
issues[].fields.sprint.originBoardId: int
issues[].fields.sprint.goal: str
issues[].fields.sprint.synced: bool
issues[].fields.sprint.autoStartStop: bool
issues[].fields.project: dict
issues[].fields.project.self: str
issues[].fields.project.id: str
issues[].fields.project.key: str
issues[].fields.project.name: str
issues[].fields.project.projectTypeKey: str
issues[].fields.project.projectCategory: dict
issues[].fields.project.projectCategory.self: str
issues[].fields.project.projectCategory.id: str
issues[].fields.project.projectCategory.description: str
issues[].fields.project.projectCategory.name: str
issues[].fields.fixVersions: list[0]
issues[].fields.customfield_10110: NoneType
issues[].fields.customfield_10111: NoneType
issues[].fields.aggregatetimespent: NoneType
issues[].fields.resolution: NoneType
issues[].fields.customfield_10302: NoneType
issues[].fields.customfield_10700: NoneType
issues[].fields.customfield_10502: NoneType
issues[].fields.customfield_10305: NoneType
issues[].fields.customfield_10503: NoneType
issues[].fields.customfield_10306: NoneType
issues[].fields.customfield_10108: str
issues[].fields.customfield_10504: NoneType
issues[].fields.customfield_10109: list[1]
issues[].fields.customfield_10505: NoneType
issues[].fields.customfield_10506: NoneType
issues[].fields.resolutiondate: NoneType
issues[].fields.customfield_10705: NoneType
issues[].fields.customfield_10507: NoneType
issues[].fields.customfield_10706: NoneType
issues[].fields.customfield_10508: NoneType
issues[].fields.workratio: int
issues[].fields.lastViewed: NoneType
issues[].fields.watches: dict
issues[].fields.watches.self: str
issues[].fields.watches.watchCount: int
issues[].fields.watches.isWatching: bool
issues[].fields.created: str
issues[].fields.epic: dict
issues[].fields.epic.id: int
issues[].fields.epic.key: str
issues[].fields.epic.self: str
issues[].fields.epic.name: str
issues[].fields.epic.summary: str
issues[].fields.epic.color: dict
issues[].fields.epic.color.key: str
issues[].fields.epic.done: bool
issues[].fields.priority: dict
issues[].fields.priority.self: str
issues[].fields.priority.name: str
issues[].fields.priority.id: str
issues[].fields.customfield_10100: NoneType
issues[].fields.customfield_10101: NoneType
issues[].fields.customfield_10102: NoneType
issues[].fields.customfield_10300: NoneType
issues[].fields.labels: list[0]
issues[].fields.customfield_10103: NoneType
issues[].fields.customfield_10301: NoneType
issues[].fields.customfield_10731: NoneType
issues[].fields.customfield_10732: NoneType
issues[].fields.customfield_10733: NoneType
issues[].fields.customfield_10734: NoneType
issues[].fields.timeestimate: NoneType
issues[].fields.aggregatetimeoriginalestimate: NoneType
issues[].fields.versions: list[0]
issues[].fields.issuelinks: list[0]
issues[].fields.assignee: dict
issues[].fields.assignee.self: str
issues[].fields.assignee.name: str
issues[].fields.assignee.key: str
issues[].fields.assignee.emailAddress: str
issues[].fields.assignee.displayName: str
issues[].fields.assignee.active: bool
issues[].fields.assignee.timeZone: str
issues[].fields.updated: str
issues[].fields.status: dict
issues[].fields.status.self: str
issues[].fields.status.description: str
issues[].fields.status.name: str
issues[].fields.status.id: str
issues[].fields.status.statusCategory: dict
issues[].fields.status.statusCategory.self: str
issues[].fields.status.statusCategory.id: int
issues[].fields.status.statusCategory.key: str
issues[].fields.status.statusCategory.colorName: str
issues[].fields.status.statusCategory.name: str
issues[].fields.components: list[1]
issues[].fields.components[].self: str
issues[].fields.components[].id: str
issues[].fields.components[].name: str
```

### user_assignable

```
<root>: list[5]
[].self: str
[].key: str
[].name: str
[].emailAddress: str
[].displayName: str
[].active: bool
[].deleted: bool
[].timeZone: str
[].locale: str
```

### user_search

```
<root>: list[5]
[].self: str
[].key: str
[].name: str
[].emailAddress: str
[].displayName: str
[].active: bool
[].deleted: bool
[].timeZone: str
[].locale: str
```

