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


def session_meta(path, limit=24000):
    """Cheap description of a transcript: first prompt, cwd, branch, size, mtime."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    title, cwd, branch, turns = None, None, None, 0
    try:
        with open(path, "r", errors="replace") as fh:
            for line in fh.read(limit).split("\n"):
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
    except OSError:
        return None
    return {"file": path, "name": os.path.basename(path)[:8],
            "title": title or "(no prompt yet)", "cwd": cwd, "branch": branch,
            "turns_seen": turns, "size": st.st_size, "mtime": st.st_mtime}


class Tailer:
    """One session file -> one incrementally-built graph."""

    def __init__(self, path, root=None, follow_newest=True, attribution="caused",
                 events_cmd=None):
        self.root, self.follow = root, follow_newest
        self.events_cmd = events_cmd
        self.cmd_sig = None
        self.lock = threading.Lock()
        self.subs = []
        self.set_path(path)

    def set_path(self, path):
        self.path = path
        self.off = 0
        self.buf = b""
        self.g = SessionGraph()
        self.steps = []          # ordered step snapshots for the burn series
        self.last_ts = None
        self.active_step = None
        self.seen_models = []

    # -- ingest
    def _ingest_line(self, line):
        try:
            rec = json.loads(line)
        except Exception:
            return
        self._ingest_rec(rec)

    def _ingest_rec(self, rec):
        self.g.ingest(rec)
        ts = parse_ts(rec.get("timestamp"))
        if ts:
            self.last_ts = max(self.last_ts or 0, ts)
        if rec.get("type") == "assistant":
            uid = f"step:{rec.get('uuid')}"
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

    def poll(self):
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
            inflight = [{"tool": t, "target": target_of(t, i), "id": k, "nodes": h}
                        for k, (s, t, i, h) in list(self.g.pending.items())]
            # the stream needs every call, not just the tail: one event per touch
            events = []
            for c in d["calls"][-2000:]:
                events.append({"tool": c["tool"], "target": target_of(c["tool"], c.get("input")),
                               "ts": parse_ts(c.get("ts")), "bytes": c.get("result_bytes"),
                               "err": bool(c.get("is_error")), "nodes": c.get("nodes") or [],
                               "verb": c.get("verb") or "execute"})
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


def make_handler(tailer):
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

        def do_GET(self):
            p = self.path.split("?")[0]
            if p in ("/", "/index.html"):
                try:
                    with open(os.path.join(HERE, "hud.html"), "rb") as fh:
                        return self._send(fh.read())
                except OSError:
                    return self._send("hud.html missing next to server.py", code=500)
            if p == "/sessions":
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
    ap.add_argument("--events-cmd", help="shell command that prints a session's events "
                                        "(JSON array or JSONL); re-run every --interval "
                                        "seconds. It carries its own credentials.")
    ap.add_argument("--interval", type=float, default=0.5, help="poll seconds")
    ap.add_argument("--no-follow", action="store_true",
                    help="stay on one file instead of jumping to the newest session")
    a = ap.parse_args()
    path = a.file or (find_sessions(a.root) or [None])[0]
    if not path and not a.events_cmd:
        sys.exit("no transcripts found under %s" % (a.root or "~/.claude/projects"))
    t = Tailer(path or "", root=a.root,
               follow_newest=not (a.no_follow or a.file or a.events_cmd),
               events_cmd=a.events_cmd)
    threading.Thread(target=t.run, kwargs={"interval": max(0.2, a.interval)},
                     daemon=True).start()
    srv = ThreadingHTTPServer((a.host, a.port), make_handler(t))
    src = ("events-cmd: " + a.events_cmd[:60]) if a.events_cmd else \
          ("tailing " + os.path.basename(path)) if path else "waiting for a session"
    print(f"HUD  http://{a.host}:{a.port}/   {src}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
