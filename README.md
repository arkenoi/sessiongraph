# sessiongraph

Your Claude Code session as a live graph walk: tools, files, commands, commits,
searches, URLs and GitHub repos as nodes, with token burn priced per model.

Three pieces:

| file | what it is |
|---|---|
| `SCHEMA.md` | the node/edge model and the cost-attribution argument |
| `sessiongraph.py` | transcript -> graph (JSON / graphviz DOT / text report). Stdlib only |
| `server.py` + `hud.html` | tails the live session and serves the HUD |

## Quickstart

```sh
git clone https://github.com/<you>/sessiongraph && cd sessiongraph
python3 server.py                 # no dependencies, Python 3.9+
open http://127.0.0.1:8777/
```

That's it — no install, no build, stdlib only. It binds to `127.0.0.1` and never
writes to `~/.claude`.

## Attaching it to a session

**It reads transcripts, so it has to run where the transcript is written.**

| session | where the transcript lives | what to do |
|---|---|---|
| Claude Code CLI, local | `~/.claude/projects/` on that machine | `python3 server.py` — nothing to attach |
| **Remote Control (RC)** | on the computer the RC bridge points at — Claude Code runs there, the cloud side is only a control channel | run it there, or watch from your laptop with `--remote` |
| Claude Code on the web / cloud | inside the session container | see *Cloud sessions* below |
| claude.ai chat (not Code) | nowhere on disk | not supported |

## Watching a machine you are not sitting at

This is the usual case for RC: the session runs on a box somewhere, you want to
watch it from your laptop. Point the HUD at that machine:

```sh
python3 server.py --remote 'ssh build-box'
open http://127.0.0.1:8777/
```

It lists the transcripts over there, attaches to the newest, and streams it live
(`tail -n +1 -F` over your own ssh). Press `S` to see the other sessions on that
host and click to switch — same picker, remote list. If the connection drops it
reconnects and rebuilds.

`--remote` takes any command prefix with ssh's argument semantics, so
`--remote 'ssh -p 2222 me@host'` or a wrapper script both work;
`--remote-root` if transcripts live somewhere other than `~/.claude/projects`.

**Credentials stay yours.** The prefix is run as you typed it, so keys, agent
forwarding and host config are whatever your ssh already does. Nothing is stored
and nothing is read from the environment.

The alternative, if you would rather not stream: run the HUD *on* that box and
forward the port.

```sh
ssh -L 8777:127.0.0.1:8777 you@build-box     # then open http://127.0.0.1:8777/
```

Either way the HUD reads only the disk of whatever machine it can reach. It
cannot see a session whose transcript is not on a disk it can read — a cloud
session's transcript lives inside its container (see *Cloud sessions*).

### Picking one

By default it follows whichever session file is newest, so when an RC session
wakes up the stream re-points to it on its own. To choose explicitly, press `S`
in the HUD (or click the session line at the top right) and pick from the list —
each row shows the session's first prompt, its working directory and branch, and
how long ago it was last written:

```
auto        Follow whichever session is newest
bbbb2222    investigate the flaky test        /home/user/other · main      29s ago
aaaa1111    add a parser                      /home/user/proj  · main      2.0h ago
```

Picking one pins it; the `auto` row hands control back. The same thing from the
command line, if you already know which file you want:

```sh
python3 server.py --file ~/.claude/projects/<project>/<uuid>.jsonl --no-follow
```

Under the hood that is `GET /sessions` and `GET /select?file=…`, and `/select`
only accepts paths it just listed under `--root`, so the endpoint can't be talked
into reading arbitrary files.

Useful flags: `--root /path/to/projects` for a non-default location,
`--file <session>.jsonl` plus `--no-follow` to pin one session, `--port`.

## Credentials

**There are none, and the tool has no way to accept a token.** It reads files off
your disk; it makes no outbound connections and reads no environment variables.
That is the whole security model for a local or RC session.

The one case that needs credentials is pulling a *cloud* session's events over the
API — and rather than hold your token, the server runs a command you give it and
parses whatever that prints:

```sh
python3 server.py --events-cmd 'your-command-that-prints-events' --interval 10
```

The command runs in your shell, with your credentials, under your control; this
process never sees them. It can print either shape — a JSON array, the
`{"ccr":{"data":[…]}}` envelope, or JSONL — and is re-read whole each time, so a
command that overwrites a dump works fine. Anything that can produce that dump
will do: a CLI you already have, a script, or a Claude session with access to the
remote session's events writing them to a file (`--events-cmd 'cat dump.json'`).

If you ever find yourself wanting to paste a token into this tool, don't — put it
in the command instead.

### Cloud sessions

The transcript is inside the container and the container has no inbound port, so
you can't point a browser at it. Two options:

1. Run it in the container and screenshot it (what the recording in this README was).
2. Dump the events API and view them: anything that can call `list_events` on the
   session writes the result to a file, then

   ```sh
   python3 sessiongraph.py events-dump.json --report
   ```

   The parser accepts both shapes — the local JSONL and the API's (snake_case,
   nested under a role key). Two things are missing from the API form:
   `parentUuid`, so rewinds don't show as branches (a linear walk is synthesised
   from arrival order), and `cwd`, so there is no repo/branch attribution.

## Live HUD

The top bar answers *what is happening right now*: status, the in-flight tool and
its target, a cadence strip of tool calls over the last three minutes, seconds
since the last record, model and effort. Then burn rate over the trailing five
minutes, total spend, context size of the last request, cache hit rate.

**The stream.** The main panel is time, left to right. Down the middle runs the
walk itself — a spine whose height is what each turn cost — flowing toward a head
at *now*. Entities are channels above and below it: artifacts above, commands and
tools just above the spine, the outside world below. Every tool call fires a
tendril from the spine out to the thing it touched, which flares white at the
moment of contact and then cools. Touch the same file again and a lifeline links
the two along its channel, so you see the stream crossing the same node twice.

Channels are allocated only to entities with traffic in view and packed tight, so
a quiet band closes up and the lanes slide as the window moves. An entity that
goes quiet for longer than the window gives its channel back.

Idle time is compressed: the x axis is *flow time*, which advances with the clock
but clamps any silence to a few seconds, so the stream keeps moving and a
seven-hour session stays legible.

**Replay.** On load the head starts at the first record and sweeps the whole
session in about 25 seconds, lighting each touch as it passes and counting the
tiles up as they stood at the time. Drag the stream to scrub, wheel to zoom the
time axis, click the session minimap to jump, `Escape` or `Enter` to return to
live, `R` to replay, `[` and `]` for speed, `T` for the table, space to freeze.

## Static analysis

```sh
python3 sessiongraph.py --report                    # newest session
python3 sessiongraph.py --all -o graph.json         # every session, cross-linked
python3 sessiongraph.py --all --dot --no-steps | dot -Tsvg -o entities.svg
python3 sessiongraph.py --attribution context --report
```

`--all` is where it earns its keep: file and repo nodes are shared across
sessions, so you see the file that fourteen different sessions kept re-reading.

## Caveats

- **Costs are API-equivalent.** Priced from `message.usage` at published API
  rates. On a subscription you don't pay them; use them as a relative weight.
  An unrecognised model is priced as the current model of its family and the
  report says so.
- **The transcript format is undocumented** and moves between Claude Code
  versions. Unknown record types are counted and skipped, never fatal.
- Cache reads dominate long sessions (often >90% of tokens at ~0.1x the input
  price), so weight by *cost*, not raw token count.

## Privacy

Session transcripts contain your prompts, file contents and command output. This
tool only reads them locally and serves them to `127.0.0.1`, but:

- `.gitignore` excludes `*.jsonl` and `graph.json` so a transcript or an exported
  graph can't be committed by accident. Keep it that way.
- `--host 0.0.0.0` would expose your session to the network. There is no auth.
  Use the SSH port-forward above instead.
- Nothing is sent anywhere. The tool makes no outbound connections and reads no
  environment variables.
