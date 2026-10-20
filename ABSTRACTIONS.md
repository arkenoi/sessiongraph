# What the graph is made of

A session is not a list of shell commands. It is an agent acting on a small
set of **things** with a small set of **verbs**, sometimes through an
**instrument**. The parser turns every tool call into zero or more

    actor —verb→ thing            (the agent read a file, committed to a repo)
    actor —run→ instrument —verb→ thing   (the agent ran qtest, which captured a VM)

and the HUD draws exactly that. Shell plumbing (`echo`, `cut`, `date`,
`sleep`, `cd`, `$VAR=`) is folded away; a pipeline that greps a log *is* a
search of that log. `git commit` is not a node: it is a **commit** verb on the
**repository**. `jev-artifact.sh` is not a node by its filename: it is an
**instrument** that **evaluates** an artifact, optionally **against** an API.

## Things (nodes)

Three hues, locked: blue = who acts, amber = what is made, magenta = the
world beyond the working tree.

| class | kind | what it is | key |
|---|---|---|---|
| agent | `agent` | the main agent, its sidechains, spawned subagents | `agent:main`, `agent:<type>` |
| agent | `prog` | a named program used as an instrument: pytest, make, docker, ffmpeg, claude | `prog:<name>` |
| agent | `skill` | a procedure the agent followed | `skill:<name>` |
| artifact | `repo` | a working tree (the call's cwd); every git/gh verb lands here | `repo:<cwd>` |
| artifact | `dir` | a folder inside a repo | `dir:<path>` |
| artifact | `file` | a file, with a **role**: `source` `doc` `config` `ci` `test` `log` `media` `archive` `data` `scratch` `tool` | `file:<path>` |
| artifact | `commit` | a commit that landed | `commit:<sha7>` |
| world | `vm` | a virtual machine / guest (qvm-*, virsh, VBoxManage, vagrant, multipass, docker container) | `vm:<name>` |
| world | `host` | another machine reached by ssh/scp/rsync/ping | `host:<name>` |
| world | `service` | a listening endpoint or unit: `localhost:8782`, a systemd unit, an MCP server | `service:<id>` |
| world | `api` | an HTTP API family: `api.github.com/repos`, `api.anthropic.com/v1/code` | `api:<host/prefix>` |
| world | `url` / `domain` | a web page and its site | `url:<url>`, `domain:<host>` |
| world | `search` | a web search | `search:<query>` |
| world | `gh_repo` / `pr` / `issue` / `run` | GitHub remote, pull request, issue, Actions run | `gh_repo:<o/r>`, `pr:<o/r#n>`, `run:<id>` |
| world | `process` | a pid or a background task the agent watches or kills | `process:<pid>` / `process:<task>` |
| world | `package` | something installed with pip/npm/apt/cargo/brew | `package:<name>` |

A file that the agent *runs* is still a file (role `tool`) so it sits in its
repo, but it acts: its out-edges carry what it did to what.

## Verbs (edges)

Direction says which way the data went: `-1` back to the agent, `+1` out into
the world, `0` both.

| verb | dir | from | examples that fold into it |
|---|---|---|---|
| read | -1 | cat head tail less sed -n jq diff md5sum; Read; git status/log/diff/show; gh pr view; python `read_text` |
| search | -1 | grep rg find fd locate; Grep Glob; WebSearch |
| watch | -1 | tail/grep of a `.log`, `tail -f`, `gh run watch`, `docker logs`, `journalctl -u` |
| probe | -1 | qvm-check/ls/tags/prefs, docker ps/inspect, virsh list, ping, `/proc/<pid>`, pgrep ps lsof ss, systemctl status, `gh run view`, stat/ls/file of a file (its existence or size, not its content) |
| fetch | -1 | curl/wget GET, WebFetch, scp/rsync from, git pull/fetch/clone, docker pull |
| capture | -1 | qvm-screenshot, `qtest shot`, playwright screenshot/record, ffmpeg from a device |
| pull | -1 | git pull/fetch/clone/checkout <ref> |
| extract | 0 | tar x, unzip, gunzip |
| evaluate | 0 | pytest, `--selftest`, tools/tests/*.sh, lint/grade/check/verify/jev/bench, make test, cargo test, shellcheck, mypy, eslint |
| run | 0 | any instrument invoked without a more specific verb; qvm-run/docker exec/ssh host cmd |
| call | 0 | curl POST/PUT/DELETE, `gh api`, python `requests.post`, MCP calls |
| edit | 0 | Edit, sed -i, patch, python `write_text`/`replace` on an existing file, chmod |
| write | +1 | Write, `>` `>>` redirects, cat heredoc into a file, cp/mv/tee/touch, tar c, ffmpeg out, TodoWrite |
| delete | +1 | rm rmdir unlink, docker rm, qvm-remove |
| commit | +1 | git add/commit/tag/merge/rebase/cherry-pick |
| push | +1 | git push, gh pr create/merge/comment, gh release, docker push, scp/rsync to, Artifact publish |
| revert | +1 | git checkout -- <path>, restore, reset, revert, stash |
| build | +1 | make, cmake, cargo/go/npm build, gcc/clang, docker build, tsc, mvn/gradle |
| install | +1 | pip/npm/apt/brew/cargo install, setup/provision/deploy scripts |
| launch | +1 | qvm-start/create/clone, docker run/start, virsh start, vagrant up, systemctl start, nohup/`&`, server.py --port |
| stop | +1 | qvm-shutdown/kill, docker stop, kill pkill killall, systemctl stop, vagrant halt |
| spawn | +1 | Agent/Task, Workflow, `claude -p` |
| skill | 0 | Skill |
| against | 0 | what an instrument judged against (from the vocabulary): `jev —evaluate→ notes.txt`, `jev —against→ api:claude` |

An MCP call is a `read` (list/get/search ops) or a `call` on the `service`
that is the MCP server.

## How a call becomes actions

1. Expand `VAR=value` assignments that appear in the same command, so
   `"$S/probe.tar"` becomes the real path.
2. Pull out inline code (`python3 -c`, `python3 - <<PY`, `node -e`): the
   quoted paths in it are **edited** if the code writes, **read** if it only
   reads. The code itself is not a thing.
3. Heredocs and redirects: `cat > f <<EOF`, `> f`, `>> f`, `tee f` are a
   **write** of `f`.
4. Split the rest on `;` `|` `&&` `||` newlines; peel `command sudo env nohup
   timeout`; drop plumbing.
5. Each segment is one of: a **git/gh** verb on the repo (or PR/run/api);
   a **VM/host/service/process** verb; a **reader/searcher/writer** of the
   paths it names; a **network** fetch/call; a **package** install; or an
   **instrument**: a path that is executed, or `python3 x.py`, `bash x.sh`,
   `node x.js`, or a known program (pytest, make, docker, ffmpeg …).
6. An instrument gets a verb, in this order of precedence: a flag that is a
   verb (`--selftest`, `--check`, `--build`), its subcommand (`qtest shot` →
   capture, `make test` → evaluate), the vocabulary, then its name (`grade`,
   `jev`, `lint` → evaluate; `build` → build; `shot` → capture …), else
   `run`. Other flags never name verbs (`--watch-dir` is not a watch). Its
   targets come from its arguments: files, URLs, and any **known entity
   name** (a VM seen earlier in the session, `QTEST_VM=win10-acc`). With no
   targets the agent simply ran it, unless the verb is evaluate (then it
   evaluated itself: `grade.py --selftest`) or build/install (then the repo).
   A `cd dir &&` earlier on the line moves everything after it into `dir`.
7. A result that says `rc=N`, `PASS`/`FAIL`, `N passed, M failed`,
   `SELFTEST CLEAN|FAILED` marks the call's outcome; a commit hash in the
   result lands a `commit` in the repo; search results yield `url`s.

## Project vocabulary

Rules above are generic. A repo can add its own in `.sessiongraph.json` at
the repo root (or `--vocab path`):

```json
{
  "instruments": {
    "jev-*":        {"verb": "evaluate", "against": "api:claude"},
    "tools/qtest":  {"verb": "capture"},
    "wu-e2e":       {"verb": "evaluate"}
  },
  "entities": {
    "vm":   ["^win1[01]\\w*", "^dom0$"],
    "host": ["^rig$"]
  }
}
```

`instruments` keys are glob patterns matched against the instrument's
basename or repo-relative path; `against` adds `instrument —against→ entity`.
`entities` maps a kind to name patterns: any argument that matches is that
kind of thing (a `qvm-*` command picks the argument that matches a known VM
name first). Names and the file are per session and per working tree: a VM
remembered in one session is not a VM in another, and the file is read from
the `cwd` the transcript names, so it only works where that tree exists.

A third section, `results`, catches instruments that ran without being on
the command line (a hook, a pre-commit gate): when a tool result matches
`pattern` on a call that wrote, edited or committed something, the named
instrument gets `—verb→` those things (and `—against→` the API if given).
`score` is a regex run over the whole result: one group gives a list of
scores, two groups (`(\\w+) noul=([0-9.]+)`) give `{name: value}`. The
scores land on the API node (`last_scores`, `n_calls`) and on the call, so
the HUD can show each call to the API going out and its answer coming
back, with the span of scores under the API. If the instrument *was* on
the command line, the rule only adds the call and its scores.

```json
{ "results": [
  {"pattern": "jev-artifact: \\d+ bytes", "instrument": "jev", "verb": "evaluate",
   "against": "api:claude", "score": "noul=([0-9.]+)"} ] }
```

Built in: `pre-commit … REFUSED|failed` → `pre-commit —evaluate→` the
things committed.
