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


class Tailer:
    """One session file -> one incrementally-built graph."""

    def __init__(self, path, root=None, follow_newest=True, attribution="caused"):
        self.root, self.follow = root, follow_newest
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
    ap.add_argument("--no-follow", action="store_true",
                    help="stay on one file instead of jumping to the newest session")
    a = ap.parse_args()
    path = a.file or (find_sessions(a.root) or [None])[0]
    if not path:
        sys.exit("no transcripts found under %s" % (a.root or "~/.claude/projects"))
    t = Tailer(path, root=a.root, follow_newest=not a.no_follow)
    threading.Thread(target=t.run, daemon=True).start()
    srv = ThreadingHTTPServer((a.host, a.port), make_handler(t))
    print(f"HUD  http://{a.host}:{a.port}/   tailing {os.path.basename(path)}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
