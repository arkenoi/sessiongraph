# Session graph schema

A Claude Code session is two graphs layered on one another:

1. **The walk** — a DAG of conversation steps, linked by `parentUuid`. It branches
   wherever you rewound or edited a prompt, so it is a tree, not a line.
2. **The entity graph** — the things the walk touched: files, commands, commits,
   searches, URLs, GitHub repos, subagents, skills. These are shared *across*
   sessions, which is what makes the graph worth having.

Steps carry the money. Entities carry the meaning. Edges carry the verb.

## Node types

| type | id | key attrs |
|---|---|---|
| `session` | `session:<uuid>` | cwd, branch, models, started, ended, turns, usd, tokens |
| `step` | `step:<uuid>` | role, ts, model, effort, usd, tokens{in,out,cache_r,cache_w}, thinking_ms, sidechain, n_tools, text_preview |
| `tool` | `tool:<Name>` | calls, errors, usd_attributed (a *type*, aggregated) |
| `file` | `file:<abspath>` | ext, reads, writes, edits, bytes_in, repo |
| `dir` | `dir:<abspath>` | files |
| `repo` | `repo:<root>` | branch, commits, files |
| `gh_repo` | `gh_repo:<owner/name>` | mentions, source (fetch/search/clone/pr) |
| `commit` | `commit:<sha7>` | branch, subject, repo |
| `command` | `cmd:<git commit\|npm\|…>` | runs, exit_nonzero (bash head, 2 tokens for git/npm/cargo/gh) |
| `search` | `search:<query>` | engine, results, duration_s |
| `url` | `url:<url>` | bytes, status, domain |
| `domain` | `domain:<host>` | fetches, urls |
| `agent` | `agent:<subagent_type>` | spawns |
| `skill` | `skill:<name>` | invocations |
| `mcp` | `mcp:<server>` | tools, calls |

## Edge types

Steps → steps: `PARENT` (the walk; reverse it for chronology).
Steps → everything else, with `tool` + `ts` on the edge:

`READ` `WROTE` `EDITED` → file · `GREPPED` → dir/file · `RAN` → command
`SEARCHED` → search · `FETCHED` → url · `SPAWNED` → agent · `USED` → skill/tool
`CALLED` → mcp · `COMMITTED` → commit · `MENTIONS` → gh_repo

Structural, no step involved:
`CONTAINS` repo→file, dir→file, domain→url · `IN_REPO` commit→repo
`YIELDED` search→url · `TOUCHED` commit→file · `IN_SESSION` step→session

Every edge is `(src, dst, type)` with `weight` = number of occurrences, so the
same file read nine times is one edge of weight 9, not nine edges.

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
