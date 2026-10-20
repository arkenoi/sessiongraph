#!/usr/bin/env python3
"""Live tail of the current Claude Code session -> SSE -> the HUD.

  python3 server.py                 # newest session under ~/.claude/projects
  python3 server.py --root /root/.claude/projects --port 8777
  open http://127.0.0.1:8777/

Polls the transcript's byte offset (cheap: reads only what was appended),
re-ingests the new records into the graph, and pushes a snapshot to every
connected browser. Stdlib only; binds to localhost.
"""
import argparse, json, os, queue, sys, threading, time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sessiongraph import SessionGraph, find_sessions  # noqa: E402
import abstractions as AB  # noqa: E402
from campaign import Campaign  # noqa: E402
VOCAB = AB.load_vocab()

HERE = os.path.dirname(os.path.abspath(__file__))


def parse_ts(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def target_of(tool, inp):
    inp = inp or {}
    for k in ("file_path", "notebook_path", "url", "query", "pattern", "skill",
              "subagent_type", "command", "description"):
        v = inp.get(k)
        if v:
            v = str(v)
            if k in ("file_path", "notebook_path"):
                return os.path.basename(v)
            return v[:70]
    return ""


def describe(call, labels):
    """What a call did, in the graph's own words: `watch matrix.log · probe
    win11de-tup`, or `qtest capture win10-acc` when an instrument did it."""
    links = call.get("links") or []
    if not links:
        return target_of(call.get("tool"), call.get("input"))
    def lbl(nid):
        return labels.get(nid) or nid.split(":", 1)[-1].rstrip("/").split("/")[-1]
    via = [f"{lbl(s)} {v} {lbl(d)}" for s, d, v in links if s != "agent"]
    own = [f"{v} {lbl(d)}" for s, d, v in links if s == "agent"]
    parts, seen = [], set()
    for p in via + own:
        if p not in seen:
            seen.add(p); parts.append(p)
    return " · ".join(parts[:3]) + (f" +{len(parts) - 3}" if len(parts) > 3 else "")


def parse_meta(text):
    """First prompt, cwd, branch and turn count from the head of a transcript."""
    title, cwd, branch, turns = None, None, None, 0
    if True:
        if True:
            for line in text.split("\n"):
                if not line.strip():
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                cwd = cwd or rec.get("cwd")
                branch = branch or rec.get("gitBranch")
                if rec.get("type") == "assistant":
                    turns += 1
                if title is None and rec.get("type") == "user" and not rec.get("toolUseResult"):
                    c = (rec.get("message") or {}).get("content")
                    txt = c if isinstance(c, str) else " ".join(
                        b.get("text", "") for b in c if isinstance(b, dict)) if isinstance(c, list) else ""
                    txt = " ".join(txt.split())
                    if txt:
                        title = txt[:90]
    return {"title": title or "(no prompt yet)", "cwd": cwd, "branch": branch,
            "turns_seen": turns}


def session_meta(path, limit=24000):
    try:
        st = os.stat(path)
        with open(path, "r", errors="replace") as fh:
            m = parse_meta(fh.read(limit))
    except OSError:
        return None
    m.update({"file": path, "name": os.path.basename(path)[:8],
              "size": st.st_size, "mtime": st.st_mtime})
    return m


def sh(cmd, timeout=25):
    import subprocess
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return r.stdout, r.stderr
    except Exception as e:
        return "", str(e)


class Remote:
    """Another computer, reached by a command prefix you supply (e.g. "ssh box").

    Only that prefix is ours to run; credentials, keys and agent forwarding are
    whatever your own ssh config already does. Nothing is stored here.
    """

    def __init__(self, prefix, root="~/.claude/projects"):
        self.prefix, self.root = prefix, root
        self.listed = []

    def list(self, n=24):
        out, err = sh(f"{self.prefix} 'ls -t {self.root}/*/*.jsonl 2>/dev/null | head -{n}'")
        paths = [l.strip() for l in out.split("\n") if l.strip().endswith(".jsonl")]
        if not paths:
            return [], (err.strip()[:200] or "no transcripts found on the remote host")
        quoted = " ".join("'" + p.replace("'", "'\\''") + "'" for p in paths)
        heads, _ = sh(f"{self.prefix} 'for f in {quoted}; do echo \"<<<F $f\"; "
                      f"head -c 2500 \"$f\"; echo; done'", timeout=45)
        blobs = {}
        for chunk in heads.split("<<<F ")[1:]:
            nl = chunk.find("\n")
            blobs[chunk[:nl].strip()] = chunk[nl+1:]
        items = []
        for rank, pth in enumerate(paths):
            m = parse_meta(blobs.get(pth, ""))
            m.update({"file": pth, "name": os.path.basename(pth)[:8],
                      "rank": rank, "size": None, "mtime": None, "remote": True})
            items.append(m)
        self.listed = [i["file"] for i in items]
        return items, None

    def stream_cmd(self, path):
        return f"{self.prefix} tail -n +1 -F '" + path.replace("'", "'\\''") + "'"


class Tailer:
    """One session file -> one incrementally-built graph."""

    def __init__(self, path, root=None, follow_newest=True, attribution="caused",
                 events_cmd=None, remote=None, watch_dir=None):
        self.root, self.follow = root, follow_newest
        self.events_cmd = events_cmd
        self.cmd_sig = None
        self.remote = remote
        self.proc = None
        self.stream_path = None
        self.watch_dir = watch_dir
        self.seen_files = {}
        self.lock = threading.Lock()
        self.subs = []
        self.set_path(path)

    def set_path(self, path):
        self.path = path
        self.off = 0
        self.buf = b""
        self.g = SessionGraph(vocab=VOCAB)
        self.steps = []          # ordered step snapshots for the burn series
        self.last_ts = None
        self.active_step = None
        self.step_ids = set()
        self.seen_models = []
        self.vmstate = {}        # vm name -> {"state", "ts", "why"}: the last the agent saw
        self.vm_pending = {}     # tool_use_id -> (tool, [(vm, state, why)]) until its result lands
        self.campaign = Campaign(VOCAB.get("campaign"))

    # -- ingest
    def _ingest_line(self, line):
        try:
            rec = json.loads(line)
        except Exception:
            return
        self._ingest_rec(rec)

    def _ingest_rec(self, rec):
        # Records from the events API carry no top-level `type`, so normalise
        # first or the cost series silently stays empty while the graph fills.
        norm = SessionGraph.normalize(rec) or rec
        self.g.ingest(rec)
        ts = parse_ts(norm.get("timestamp"))
        if ts:
            self.last_ts = max(self.last_ts or 0, ts)
        self._observe_vms(norm, ts)
        if norm.get("type") == "assistant":
            uid = f"step:{norm.get('uuid')}"
            if uid in self.step_ids:            # a re-posted page: already counted
                return
            self.step_ids.add(uid)
            n = self.g.nodes.get(uid)
            if n:
                self.active_step = uid
                m = n["attrs"].get("model")
                if m and m not in self.seen_models:
                    self.seen_models.append(m)
                self.steps.append({"ts": ts, "usd": n["attrs"].get("usd", 0),
                                   "t": n["attrs"].get("tokens", {}),
                                   "model": m, "effort": n["attrs"].get("effort"),
                                   "n_tools": n["attrs"].get("n_tools", 0),
                                   "think_ms": n["attrs"].get("thinking_ms"),
                                   "text": n["attrs"].get("text_preview")})

    def _is_vm(self, name):
        if f"vm:{name}" in self.g.nodes or name in self.vmstate:
            return True
        return any(r.match(name) for r in VOCAB.get("_compiled", {}).get("vm", []))

    def _observe_vms(self, norm, ts):
        """Run state as the agent saw it: its qvm/qrexec calls (applied when the
        call succeeds) and the state lines its Bash commands printed."""
        msg = norm.get("message") or {}
        content = msg.get("content") if isinstance(msg, dict) else None
        if not isinstance(content, list) or not ts:
            return
        for c in content:
            if not isinstance(c, dict):
                continue
            if c.get("type") == "tool_use":
                cmd = (c.get("input") or {}).get("command") if c.get("name") == "Bash" else None
                self.vm_pending[c.get("id")] = (c.get("name"), AB.vm_state_commands(cmd) if cmd else [])
            elif c.get("type") == "tool_result":
                tool, calls = self.vm_pending.pop(c.get("tool_use_id"), (None, []))
                if tool != "Bash":                     # a Read of a script is not a VM starting
                    continue
                seen = [] if c.get("is_error") else list(calls)
                body = c.get("content")
                if isinstance(body, list):
                    body = "\n".join(b.get("text", "") for b in body if isinstance(b, dict))
                body = body if isinstance(body, str) else ""
                seen += AB.vm_state_text(body, self._is_vm, VOCAB.get("_vm_states", []))
                self.campaign.observe(body, ts)
                for name, state, why in seen:
                    self.vmstate[name] = {"state": state, "ts": ts, "why": why}

    def poll(self):
        if self.watch_dir:
            return self._poll_dir()
        if self.remote:
            if self.proc and self.proc.poll() is not None:   # ssh dropped
                print("remote stream ended; retrying", file=sys.stderr)
                time.sleep(3)
                if self.stream_path:
                    self.start_stream(self.stream_path)
                    return True
            return False
        if self.events_cmd:                       # remote: whatever you give us
            return self._poll_cmd()
        if self.follow:
            newest = (find_sessions(self.root) or [None])[0]
            if newest and newest != self.path:
                with self.lock:
                    self.set_path(newest)
        try:
            size = os.path.getsize(self.path)
        except OSError:
            return False
        if size < self.off:          # truncated/rotated
            with self.lock:
                self.set_path(self.path)
        if size == self.off:
            return False
        with self.lock, open(self.path, "rb") as fh:
            fh.seek(self.off)
            chunk = fh.read()
            self.off = fh.tell()
            self.buf += chunk
            *lines, self.buf = self.buf.split(b"\n")
            for ln in lines:
                if ln.strip():
                    self._ingest_line(ln.decode("utf-8", "replace"))
        return True

    def _poll_dir(self):
        """Merge every dump dropped into a directory.

        The transport is somebody else's problem: anything that can reach a
        session's events -- a Claude session with the remote tools, a Routine,
        a script -- writes files here and they are merged as they land. Ingest
        is keyed by record uuid, so overlapping pages are free.
        """
        from sessiongraph import iter_records
        changed = False
        try:
            names = sorted(os.listdir(self.watch_dir))
        except OSError:
            return False
        # Pages land newest-first (hist0001 is the latest), so ingest in time
        # order, not file order: otherwise "now" is the oldest page and every
        # result arrives before its call, leaving the call pending forever.
        batch = []
        for name in names:
            if name.startswith("."):
                continue
            f = os.path.join(self.watch_dir, name)
            try:
                st = os.stat(f)
            except OSError:
                continue
            if not os.path.isfile(f):
                continue
            sig = (st.st_mtime, st.st_size)
            if self.seen_files.get(f) == sig:
                continue
            self.seen_files[f] = sig
            try:
                recs = list(iter_records(f))
            except Exception as e:
                print(f"dump {name}: {e}", file=sys.stderr)
                continue
            batch.extend(recs)
            print(f"merged {name}: {len(recs)} records", file=sys.stderr)
            changed = True
        def when(rec):
            norm = SessionGraph.normalize(rec) or rec
            return (parse_ts(norm.get("timestamp")) or parse_ts(rec.get("created_at")) or 0,
                    int(rec.get("sequence_num") or 0) if str(rec.get("sequence_num") or 0).isdigit() else 0)
        batch.sort(key=when)
        with self.lock:
            for rec in batch:
                self._ingest_rec(rec)
        return changed

    def start_stream(self, path):
        """Follow an append-only transcript on another machine, live."""
        import subprocess, threading as th
        self.stop_stream()
        with self.lock:
            self.set_path(path)
        self.stream_path = path
        cmd = self.remote.stream_cmd(path)
        self.proc = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, text=True, bufsize=1)
        def pump(proc):
            n = 0
            for line in proc.stdout:
                if proc is not self.proc:
                    return                       # superseded by another selection
                if line.strip():
                    with self.lock:
                        self._ingest_line(line)
                    n += 1
                    if n % 12 == 0:
                        self.broadcast()
            self.broadcast()
        th.Thread(target=pump, args=(self.proc,), daemon=True).start()
        return True

    def stop_stream(self):
        if self.proc:
            try:
                self.proc.kill()
            except Exception:
                pass
            self.proc = None

    def _poll_cmd(self):
        """Run the user's own command and rebuild from whatever it prints.

        The command carries its own credentials; this process never sees a
        token. A dump is usually a whole JSON array, so it is re-read entire
        rather than tailed.
        """
        import subprocess
        try:
            out = subprocess.run(self.events_cmd, shell=True, capture_output=True,
                                 text=True, timeout=120).stdout
        except Exception as e:
            print("events-cmd failed:", e, file=sys.stderr)
            return False
        sig = (len(out), hash(out))
        if sig == self.cmd_sig:
            return False
        self.cmd_sig = sig
        with self.lock:
            self.set_path(self.path)              # reset the graph
            tmp = os.path.join(HERE, ".events-cache.json")
            try:
                with open(tmp, "w") as fh:
                    fh.write(out)
                from sessiongraph import iter_records
                for rec in iter_records(tmp):
                    self._ingest_rec(rec)
            except Exception as e:
                print("events-cmd parse failed:", e, file=sys.stderr)
                return False
        return True

    def select(self, path):
        """Attach to a specific transcript and stop following the newest."""
        with self.lock:
            self.follow = False
            self.set_path(path)
        self.poll()
        self.broadcast()

    # -- what the HUD needs
    def snapshot(self):
        with self.lock:
            d = self.g.to_dict()
            now = time.time()
            idle = (now - self.last_ts) if self.last_ts else None
            labels = {n["id"]: n.get("label") for n in d["nodes"] if n["type"] != "step"}
            by_id = {c["id"]: c for c in d["calls"][-2000:]}
            inflight = [{"tool": t, "target": describe(by_id.get(k) or {"tool": t, "input": i}, labels),
                         "raw": target_of(t, i), "id": k, "nodes": h}
                        for k, (s, t, i, h) in list(self.g.pending.items())]
            # the stream needs every call, not just the tail: one event per touch
            # what each call cost: its turn's price, split across that turn's calls
            per_step = {}
            for c in d["calls"]:
                per_step[c["step"]] = per_step.get(c["step"], 0) + 1
            step_usd = {n["id"]: n.get("usd", 0.0) for n in d["nodes"] if n["type"] == "step"}
            step_side = {n["id"]: bool(n["attrs"].get("sidechain")) for n in d["nodes"] if n["type"] == "step"}
            events = []
            for c in d["calls"][-2000:]:
                events.append({"tool": c["tool"], "target": describe(c, labels),
                               "raw": target_of(c["tool"], c.get("input")),
                               "scores": c.get("scores"), "api": c.get("api"),
                               "ts": parse_ts(c.get("ts")), "bytes": c.get("result_bytes"),
                               "err": bool(c.get("is_error")), "nodes": c.get("nodes") or [],
                               "verb": c.get("verb") or "execute",
                               "sidechain": step_side.get(c["step"], False),
                               "links": c.get("links") or [], "outcome": c.get("outcome"),
                               "usd": step_usd.get(c["step"], 0.0) / max(1, per_step.get(c["step"], 1))})
            recent = events[-26:]
            last = self.steps[-1] if self.steps else {}
            ctx = (last.get("t") or {})
            context_tokens = (ctx.get("cache_read", 0) + ctx.get("in", 0) + ctx.get("cache_write", 0))
            # burn rate over the trailing 5 minutes of wall clock
            cut = now - 300
            win = [s for s in self.steps if (s.get("ts") or 0) >= cut]
            span = max(1e-9, (now - min([s["ts"] for s in win], default=now)))
            burn_min = sum(s["usd"] for s in win) / span * 60 if win else 0.0
            tot = d["totals"]
            reads, writes = tot.get("cache_read", 0), tot.get("cache_write", 0)
            status = ("executing" if inflight else
                      "live" if idle is not None and idle < 12 else
                      "awaiting you" if idle is not None and idle < 900 else "idle")
            vms = dict(self.vmstate)
            camp = self.campaign.project(now) if self.campaign.enabled else None
            if camp and (not camp["over"] or camp.get("since")):
                # what the campaign is driving, unless seen since; one machine at a time means the rest are
                # down, and a campaign that has ended leaves none of its machines up on its account
                for name in camp["vms"]:
                    up = not camp["over"] and name == camp["vm"]
                    if not up and not camp["serial"]:
                        continue
                    seen = vms.get(name)
                    if seen and seen["ts"] > camp["since"]:
                        continue
                    hm = f"{datetime.fromtimestamp(camp['since'], timezone.utc):%H:%M}"
                    why = (f"campaign over ~{hm} · {camp['basis']}" if camp["over"] else
                           f"campaign {camp['how']}: {camp['phase']} since ~{hm} · {camp['basis']}")
                    vms[name] = {"state": "running" if up else "halted", "ts": camp["since"], "projected": True,
                                 "why": why}
            for n in d["nodes"]:
                name = n["id"].split(":", 1)[-1]
                if n["type"] == "vm" and name in vms:
                    n["attrs"] = {**n.get("attrs", {}), **{"vm_" + k: v for k, v in vms[name].items()}}
            have = {n["id"] for n in d["nodes"]}
            for name, st in vms.items():                # running, but never acted on: still worth a body
                if st["state"] == "running" and f"vm:{name}" not in have:
                    d["nodes"].append({"id": f"vm:{name}", "type": "vm", "label": name,
                                       "attrs": {"vm_" + k: v for k, v in st.items()}})
            sess = next((n for n in d["nodes"] if n["type"] == "session"), None)
            return {
                "nodes": d["nodes"], "edges": d["edges"], "totals": tot,
                "usd": d["usd"], "price_guessed": d["price_guessed"],
                "state": {
                    "status": status, "idle_s": idle, "inflight": inflight,
                    "recent": recent, "events": events, "steps": self.steps[-240:],
                    "models": self.seen_models, "effort": last.get("effort"),
                    "context_tokens": context_tokens,
                    "cache_hit": (reads / (reads + writes) * 100) if (reads + writes) else None,
                    "burn_min": burn_min, "session": (sess or {}).get("label"),
                    "cwd": (sess or {}).get("attrs", {}).get("cwd"),
                    "branch": (sess or {}).get("attrs", {}).get("branch"),
                    "file": os.path.basename(self.path), "now": now,
                    "last_text": last.get("text"), "think_ms": last.get("think_ms"),
                    "active_step": self.active_step,
                    "campaign": camp,
                    "t_start": min((s["ts"] for s in self.steps if s.get("ts")), default=None),
                    "t_end": self.last_ts,
                },
            }

    # -- fan-out
    def subscribe(self):
        q = queue.Queue(maxsize=4)
        self.subs.append(q)
        return q

    def broadcast(self):
        payload = json.dumps(self.snapshot(), default=str)
        for q in list(self.subs):
            try:
                q.put_nowait(payload)
            except queue.Full:
                pass

    def run(self, interval=0.5):
        self.poll()
        while True:
            try:
                if self.poll():
                    self.broadcast()
            except Exception as e:                       # never die on a bad record
                print("poll error:", e, file=sys.stderr)
            time.sleep(interval)


def make_handler(tailer, token=None):
    TOKEN = token
    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def _send(self, body, ctype="text/html; charset=utf-8", code=200):
            if isinstance(body, str):
                body = body.encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _authed(self):
            if not TOKEN:
                return True
            from urllib.parse import urlparse, parse_qs
            q = parse_qs(urlparse(self.path).query)
            return (self.headers.get("X-Token") == TOKEN or
                    (q.get("token") or [""])[0] == TOKEN)

        def do_POST(self):
            p = self.path.split("?")[0]
            if p != "/ingest":
                return self._send("not found", code=404)
            if not self._authed():
                return self._send(json.dumps({"ok": False, "error": "bad token"}),
                                  "application/json", code=403)
            try:
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(min(n, 32 * 1024 * 1024)).decode("utf-8", "replace")
            except Exception as e:
                return self._send(json.dumps({"ok": False, "error": str(e)}),
                                  "application/json", code=400)
            recs, count = [], 0
            body = body.strip()
            if body[:1] == "[":                      # a JSON array
                try:
                    recs = json.loads(body)
                except Exception:
                    recs = []
            else:                                     # JSONL, or one object
                for line in body.split("\n"):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        recs.append(json.loads(line))
                    except Exception:
                        pass
            with tailer.lock:
                for rec in recs:
                    tailer._ingest_rec(rec)
                    count += 1
            if count:
                tailer.broadcast()
            return self._send(json.dumps({"ok": True, "records": count}), "application/json")

        def do_GET(self):
            p = self.path.split("?")[0]
            if p in ("/", "/index.html"):
                try:
                    with open(os.path.join(HERE, "hud.html"), "rb") as fh:
                        return self._send(fh.read())
                except OSError:
                    return self._send("hud.html missing next to server.py", code=500)
            if p == "/sessions":
                if tailer.remote:
                    items, err = tailer.remote.list()
                    for i in items:
                        i["active"] = (i["file"] == tailer.stream_path)
                    return self._send(json.dumps({"sessions": items, "follow": False,
                                                  "remote": tailer.remote.prefix,
                                                  "error": err, "current": tailer.stream_path}),
                                      "application/json")
                items = [session_meta(f) for f in find_sessions(tailer.root)[:60]]
                items = [i for i in items if i]
                for i in items:
                    i["active"] = (i["file"] == tailer.path)
                return self._send(json.dumps({"sessions": items,
                                              "follow": tailer.follow,
                                              "current": tailer.path}),
                                  "application/json")
            if p == "/select":
                from urllib.parse import urlparse, parse_qs, unquote
                q = parse_qs(urlparse(self.path).query)
                want = unquote((q.get("file") or [""])[0])
                if want == "__newest__":
                    tailer.follow = True
                    tailer.poll(); tailer.broadcast()
                    return self._send(json.dumps({"ok": True, "follow": True}), "application/json")
                if tailer.remote:
                    if want not in tailer.remote.listed:
                        return self._send(json.dumps({"ok": False, "error": "unknown session"}),
                                          "application/json", code=400)
                    tailer.start_stream(want)
                    return self._send(json.dumps({"ok": True, "file": want}), "application/json")
                known = set(find_sessions(tailer.root))     # only files we listed
                if want not in known:
                    return self._send(json.dumps({"ok": False, "error": "unknown session"}),
                                      "application/json", code=400)
                tailer.select(want)
                return self._send(json.dumps({"ok": True, "file": want}), "application/json")
            if p == "/graph.json":
                return self._send(json.dumps(tailer.snapshot(), default=str),
                                  "application/json")
            if p == "/stream":
                q = tailer.subscribe()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "keep-alive")
                self.end_headers()
                try:
                    self.wfile.write(b": hello\n\n")
                    self.wfile.write(("data: " + json.dumps(tailer.snapshot(), default=str)
                                      + "\n\n").encode())
                    self.wfile.flush()
                    while True:
                        try:
                            msg = q.get(timeout=15)
                            self.wfile.write(("data: " + msg + "\n\n").encode())
                        except queue.Empty:
                            self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass
                finally:
                    if q in tailer.subs:
                        tailer.subs.remove(q)
                return
            self._send("not found", code=404)
    return H


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", help="transcript to tail (default: newest)")
    ap.add_argument("--root", help="override ~/.claude/projects")
    ap.add_argument("--port", type=int, default=8777)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--ingest-token", metavar="TOK",
                    help="shared secret required on POST /ingest. Invent one; it is not "
                         "an Anthropic credential. Use it whenever --host is not localhost.")
    ap.add_argument("--watch-dir", metavar="DIR",
                    help="merge event dumps dropped into this directory. Any process "
                         "that can reach a session's events writes files here; records "
                         "are de-duplicated by uuid, so overlapping pages are harmless.")
    ap.add_argument("--remote", metavar="CMD",
                    help="watch sessions on another machine, e.g. --remote 'ssh box'. "
                         "Lists transcripts there and streams the one you pick. Your own "
                         "ssh config supplies the credentials; none are stored here.")
    ap.add_argument("--remote-root", default="~/.claude/projects",
                    help="transcript directory on the remote machine")
    ap.add_argument("--events-cmd", help="shell command that prints a session's events "
                                        "(JSON array or JSONL); re-run every --interval "
                                        "seconds. It carries its own credentials.")
    ap.add_argument("--interval", type=float, default=0.5, help="poll seconds")
    ap.add_argument("--vocab", help="project vocabulary (.sessiongraph.json): instruments, entity names, result rules")
    ap.add_argument("--no-follow", action="store_true",
                    help="stay on one file instead of jumping to the newest session")
    a = ap.parse_args()
    global VOCAB
    VOCAB = AB.load_vocab(a.vocab)
    path = a.file or (find_sessions(a.root) or [None])[0]
    if a.watch_dir:
        os.makedirs(a.watch_dir, exist_ok=True)
        path = path or ""
    if not path and not a.events_cmd and not a.remote and not a.watch_dir:
        sys.exit("no transcripts found under %s" % (a.root or "~/.claude/projects"))
    remote = Remote(a.remote, a.remote_root) if a.remote else None
    t = Tailer(path or "", root=a.root,
               follow_newest=not (a.no_follow or a.file or a.events_cmd or remote or a.watch_dir),
               events_cmd=a.events_cmd, remote=remote, watch_dir=a.watch_dir)
    if remote:                                   # attach to the newest one over there
        items, err = remote.list()
        if err:
            sys.exit("remote: " + err)
        t.start_stream(items[0]["file"])
        print(f"remote  {a.remote}  {len(items)} session(s); attached to {items[0]['name']}")
    threading.Thread(target=t.run, kwargs={"interval": max(0.2, a.interval)},
                     daemon=True).start()
    srv = ThreadingHTTPServer((a.host, a.port), make_handler(t, a.ingest_token))
    src = ("watching " + a.watch_dir) if a.watch_dir else \
          ("remote: " + a.remote) if a.remote else \
          ("events-cmd: " + a.events_cmd[:60]) if a.events_cmd else \
          ("tailing " + os.path.basename(path)) if path else "waiting for a session"
    print(f"HUD  http://{a.host}:{a.port}/   {src}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
