# Session graph schema

A Claude Code session is two graphs layered on one another:

1. **The walk** — a DAG of conversation steps, linked by `parentUuid`. It branches
   wherever you rewound or edited a prompt, so it is a tree, not a line.
2. **The entity graph** — the things the walk acted on: repos, files, VMs,
   services, APIs, processes, searches, URLs, GitHub repos, subagents, and the
   instruments it acted through. These are shared *across* sessions, which is
   what makes the graph worth having.

Steps carry the money. Entities carry the meaning. Edges carry the verb.
What counts as an entity and a verb is defined in `ABSTRACTIONS.md`; this
file is the storage shape.

## Node types

| type | id | key attrs |
|---|---|---|
| `session` | `session:<uuid>` | cwd, branch, models, started, ended, turns, usd, tokens |
| `step` | `step:<uuid>` | role, ts, model, effort, usd, tokens{in,out,cache_r,cache_w}, thinking_ms, sidechain, n_tools, text_preview |
| `agent` | `agent:<subagent_type>` | spawns |
| `prog` | `prog:<name>` | a named program used as an instrument (pytest, make, docker, pre-commit) |
| `skill` | `skill:<name>` | invocations |
| `repo` | `repo:<cwd>` | branch; every git/gh verb lands here |
| `dir` | `dir:<abspath>` | |
| `file` | `file:<abspath>` | ext, **role** (source doc config ci test log media archive data scratch tool), n_lines, bytes_in |
| `commit` | `commit:<sha7>` | branch, subject |
| `vm` | `vm:<name>` | a guest: qvm-*, virsh, VBoxManage, vagrant, multipass, docker |
| `host` | `host:<name>` | a machine reached by ssh/scp/ping; `localhost` is this box |
| `service` | `service:<id>` | a listening endpoint (`localhost:8777`), a unit, an MCP server, compose |
| `api` | `api:<host/prefix>` | an HTTP API family |
| `url` / `domain` | `url:<url>` / `domain:<host>` | bytes, status |
| `search` | `search:<query>` | engine, n_results, duration_s |
| `gh_repo` / `pr` / `issue` / `run` | `gh_repo:<o/r>` … | GitHub remote, PR, issue, Actions run |
| `process` | `process:<pid\|task>` | a pid or a background task |
| `package` | `package:<name>` | something installed |

Every node carries `touches`, `usd` (attributed), `t0`/`t1` (first/last seen).

## Edge types

Steps → steps: `PARENT` (the walk; reverse it for chronology). `IN_SESSION` step→session.

Steps → things, typed by **verb** (upper-cased): `READ SEARCH WATCH PROBE FETCH
CAPTURE PULL EXTRACT EVALUATE RUN CALL EDIT WRITE DELETE COMMIT PUSH REVERT
BUILD INSTALL LAUNCH STOP SPAWN SKILL`. `RAN` step→instrument marks that the
step ran an instrument; the instrument's own verb edge (`instrument —EVALUATE→
file`, attr `via`) says what it did.

Structural, no step involved:
`CONTAINS` repo→dir→file, domain→url · `IN_REPO` commit→repo · `REMOTE` repo→gh_repo
`YIELDED` search→url · `AGAINST` instrument→api/vm it judged against · `MENTIONS` → gh_repo

Every edge is `(src, dst, type)` with `weight` = number of occurrences, so the
same file read nine times is one edge of weight 9, not nine edges.

Each tool call is also kept flat in `calls[]` with `links: [[src, dst, verb]…]`
(`src` is `"agent"` or an instrument's node id), `verb`, `outcome` (`ok` /
`fail` / null, from `rc=`, `PASS`/`FAIL`, `N passed, M failed`, tracebacks)
and `is_error`. The HUD animates from `links`.

## Cost attribution — the part everyone gets wrong

Per-step cost comes from `message.usage` priced per model: regular input,
cache **writes** (1.25× input at 5-min TTL, 2× at 1-hour), cache **reads**
(0.1× input, 0.05× on Opus 5.5, 0.025× on Fable 5.1), and output. Summing raw
token counts instead of pricing them overstates long sessions by ~10×, because
nearly all of a long session's tokens are cheap cache reads.

Entity attribution has two defensible modes, and they answer different questions:

- **`caused`** (default) — a step's cost is split evenly across the entities its
  own tool calls touched. Answers *"what was I working on when I spent this?"*
- **`context`** — a step's cost is charged to the entities whose tool results
  entered the request that produced it. Answers *"what is sitting in my context
  window costing me on every subsequent turn?"* This is the one that finds the
  450 KB file you read once and then paid to re-read on all 40 turns after.

Neither is "the" answer; `result_bytes` per entity is the honest raw signal.

Subscription users don't pay these rates — the dollar figures are API-equivalent
cost, useful as a *relative* weight. Label them that way in any UI.
