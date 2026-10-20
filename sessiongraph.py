#!/usr/bin/env python3
"""Turn Claude Code session transcripts into a heterogeneous entity graph.

Reads the JSONL transcripts under ~/.claude/projects/ and emits nodes + edges:
the conversation walk (a DAG, because rewinds branch it) layered over the
entities it touched (files, commands, commits, searches, URLs, repos, agents).

  sessiongraph.py --report                 # newest session, text summary
  sessiongraph.py --all -o graph.json      # every session, cross-linked
  sessiongraph.py path/to/x.jsonl --dot | dot -Tsvg -o g.svg

Stdlib only. The transcript format is undocumented and shifts between Claude
Code versions, so every extractor is defensive: unknown record types are
counted and skipped, never fatal.
"""
import argparse, json, os, re, sys, time
from collections import Counter, defaultdict

import abstractions as AB

# ---------------------------------------------------------------- pricing ---
# USD per million tokens: (input, output, cache_write_5m, cache_write_1h, cache_read)
# Cache writes are 1.25x input at the 5-minute TTL and 2x at 1-hour. Cache reads
# are 0.1x input, except Opus 5.5 (0.05x) and Fable/Mythos 5.1 (0.025x).
PRICES = {
    "claude-fable-5-1":  (10, 50, 12.5, 20, 0.25),
    "claude-mythos-5-1": (10, 50, 12.5, 20, 0.25),
    "claude-fable-5":    (10, 50, 12.5, 20, 1.00),
    "claude-mythos-5":   (10, 50, 12.5, 20, 1.00),
    "claude-opus-5-5":   (4, 20, 5.0, 8.0, 0.20),
    "claude-opus-5":     (5, 25, 6.25, 10, 0.50),
    "claude-opus-4-8":   (5, 25, 6.25, 10, 0.50),
    "claude-opus-4-7":   (5, 25, 6.25, 10, 0.50),
    "claude-opus-4-6":   (5, 25, 6.25, 10, 0.50),
    "claude-sonnet-5-5": (2, 10, 2.5, 4.0, 0.20),
    "claude-sonnet-5":   (2, 10, 2.5, 4.0, 0.20),
    "claude-sonnet-4-6": (3, 15, 3.75, 6.0, 0.30),
    "claude-haiku-5-5":  (0.10, 0.50, 0.125, 0.20, 0.01),
    "claude-haiku-4-5":  (1, 5, 1.25, 2.0, 0.10),
}
# Haiku 5.5 switches rate card above a 100K-token prompt.
HAIKU55_LONG = (0.50, 2.50, 0.625, 1.00, 0.05)
FAMILY_FALLBACK = [("fable", "claude-fable-5-1"), ("mythos", "claude-mythos-5-1"),
                   ("opus", "claude-opus-5-5"), ("sonnet", "claude-sonnet-5-5"),
                   ("haiku", "claude-haiku-5-5")]


def norm_model(m):
    if not m:
        return None, True
    m = str(m).lower()
    for p in ("us.", "eu.", "apac.", "anthropic."):
        while m.startswith(p):
            m = m[len(p):]
    m = re.sub(r"[-@]\d{8}$", "", m)
    if m in PRICES:
        return m, False
    for frag, target in FAMILY_FALLBACK:
        if frag in m:
            return target, True      # priced as current gen; flagged as a guess
    return None, True


def price(model, usage):
    """-> (usd, breakdown_usd, tokens_dict). Never raises on odd usage shapes."""
    u = usage or {}
    g = lambda k: int(u.get(k) or 0)
    inp, out = g("input_tokens"), g("output_tokens")
    cr = g("cache_read_input_tokens")
    cc = u.get("cache_creation") or {}
    w5 = int(cc.get("ephemeral_5m_input_tokens") or 0)
    w1 = int(cc.get("ephemeral_1h_input_tokens") or 0)
    if not (w5 or w1):
        w5 = g("cache_creation_input_tokens")
    toks = {"in": inp, "out": out, "cache_read": cr, "cache_write": w5 + w1,
            "thinking": int(((u.get("output_tokens_details") or {}).get("thinking_tokens")) or 0)}
    key, guessed = norm_model(model)
    rates = PRICES.get(key)
    if key == "claude-haiku-5-5" and (inp + cr + w5 + w1) > 100_000:
        rates = HAIKU55_LONG
    if not rates:
        return 0.0, {}, toks, True
    pi, po, p5, p1, pr = rates
    parts = {"in": inp * pi, "out": out * po, "cache_write": w5 * p5 + w1 * p1,
             "cache_read": cr * pr}
    parts = {k: v / 1e6 for k, v in parts.items()}
    return sum(parts.values()), parts, toks, guessed


# ------------------------------------------------------------- extraction ---
GH_RE = re.compile(r"github\.com[/:]([A-Za-z0-9][\w.-]*)/([\w.-]+?)(?:\.git)?(?=[/\s\"')\]]|$)")
PR_RE = re.compile(r"github\.com/([\w.-]+)/([\w.-]+)/(pull|issues)/(\d+)")
COMMIT_RE = re.compile(r"\[([^\s\]]+)\s+([0-9a-f]{7,40})\]\s*(.*)")
def domain_of(url):
    m = re.match(r"https?://([^/]+)", url or "")
    return (m.group(1).lower() if m else None)


def epoch(ts):
    """ISO timestamp -> float seconds, or None. Transcript timestamps are ISO-Z."""
    if not ts:
        return None
    try:
        from datetime import datetime
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


class SessionGraph:
    """Incremental builder. Feed it records; ask for nodes/edges any time."""

    def __init__(self, attribution="caused", vocab=None):
        self.vocab = vocab if isinstance(vocab, dict) else AB.load_vocab(vocab)
        self.vocab_explicit = bool(vocab)
        self.vocab_by_cwd = {}
        self.known_by_session = {}        # names seen, per session: a VM stays a VM
        self._last_ctx = {}               # per session: the cwd/branch API records name only once
        self._cur_ts = None
        self._prev_uuid = None
        self.seen = set()
        self.nodes = {}
        self.edges = {}
        self.calls = []            # flat tool-call log for drill-down
        self.skipped = Counter()
        self.attribution = attribution
        self.last_results = []     # entities whose results feed the next step
        self.pending = {}          # tool_use_id -> (step_uuid, tool, input)
        self.totals = Counter()
        self.usd = 0.0
        self.price_guessed = False

    # --- graph primitives
    def node(self, ntype, key, label=None, **attrs):
        nid = f"{ntype}:{key}"
        n = self.nodes.get(nid)
        if not n:
            n = self.nodes[nid] = {"id": nid, "type": ntype, "label": label or key,
                                   "attrs": {}, "touches": 0, "usd": 0.0,
                                   "t0": self._cur_ts, "t1": self._cur_ts}
        if self._cur_ts:                      # when it first appeared / was last touched
            n["t0"] = min(n.get("t0") or self._cur_ts, self._cur_ts)
            n["t1"] = max(n.get("t1") or self._cur_ts, self._cur_ts)
        for k, v in attrs.items():
            if v is None:
                continue
            if isinstance(v, (int, float)) and not isinstance(v, bool) and k.startswith("n_"):
                n["attrs"][k] = n["attrs"].get(k, 0) + v
            else:
                n["attrs"].setdefault(k, v)
        return n

    def edge(self, src, dst, etype, **attrs):
        if not src or not dst or src == dst:
            return
        k = (src, dst, etype)
        e = self.edges.get(k)
        if not e:
            e = self.edges[k] = {"src": src, "dst": dst, "type": etype, "weight": 0,
                                 "attrs": {}, "t0": self._cur_ts, "t1": self._cur_ts}
        e["weight"] += 1
        if self._cur_ts:
            e["t0"] = min(e.get("t0") or self._cur_ts, self._cur_ts)
            e["t1"] = max(e.get("t1") or self._cur_ts, self._cur_ts)
        for kk, vv in attrs.items():
            if vv is not None:
                e["attrs"][kk] = vv
        return e

    # --- ingest
    @staticmethod
    def normalize(rec):
        """Accept both transcript shapes.

        Local files (~/.claude/projects/**.jsonl) use camelCase and carry
        `type`/`parentUuid`/`cwd`. The remote events API (list_events on a
        Claude Code Remote session) returns the same records nested under a
        role key, in snake_case, with the walk links dropped. Map the second
        onto the first; the caller chains parents by arrival order.
        """
        if not isinstance(rec, dict):
            return None
        # The REST events route wraps each record: {payload, created_at, source,
        # sequence_num}. Unwrap, keeping the envelope's timestamp as a fallback.
        if isinstance(rec.get("payload"), dict) and "type" not in rec:
            inner = dict(rec["payload"])
            inner.setdefault("timestamp", rec.get("created_at"))
            inner.setdefault("created_at", rec.get("created_at"))
            return SessionGraph.normalize(inner)
        if rec.get("type") in ("assistant", "user"):
            return rec                                   # already local shape
        for role in ("assistant", "user"):
            body = rec.get(role)
            if isinstance(body, dict):
                r = dict(body)
                r.update(r.pop("internal_anthropic_catchall", None) or {})
                cwd = r.get("cwd")
                wic = r.get("wire_ingest_context")      # {tool_use_id: {cwd}} on API records
                if not cwd and isinstance(wic, dict):
                    cwd = next((v.get("cwd") for v in wic.values() if isinstance(v, dict) and v.get("cwd")), None)
                out = {"type": role, "message": r.get("message"),
                       "uuid": r.get("uuid"),
                       "timestamp": r.get("timestamp") or rec.get("created_at"),
                       "sessionId": r.get("session_id") or r.get("sessionId"),
                       "cwd": cwd, "gitBranch": r.get("gitBranch"),
                       "requestedModel": (r.get("message") or {}).get("model"),
                       "thinkingDurationMs": r.get("thinking_duration_ms"),
                       "isSidechain": bool(r.get("parent_tool_use_id")),
                       "toolUseResult": r.get("toolUseResult") or r.get("tool_use_result")}
                return out
        return None

    def ingest(self, rec):
        """Records may arrive more than once (an event-API pump re-posts the
        newest page each poll), so a record seen before is dropped."""
        t = rec.get("type") if isinstance(rec, dict) else None
        if t and t not in ("assistant", "user"):
            self.skipped[t] += 1          # keep the per-type breakdown
            return
        rec = self.normalize(rec)
        if rec is None:
            self.skipped["unrecognised"] += 1
            return
        uid = rec.get("uuid")
        if uid:
            if uid in self.seen:
                self.skipped["duplicate"] += 1
                return
            self.seen.add(uid)
        # API-shaped tool-result records carry no session id; inherit the last one
        # seen rather than inventing an "unknown" session node for them
        if rec.get("sessionId"):
            self._last_session = rec["sessionId"]
        elif getattr(self, "_last_session", None):
            rec["sessionId"] = self._last_session
        # likewise the working tree: API-shaped records name it once, at the start
        sid = rec.get("sessionId") or "?"
        last = self._last_ctx.setdefault(sid, {})
        for k in ("cwd", "gitBranch"):
            if rec.get(k):
                last[k] = rec[k]
            elif last.get(k):
                rec[k] = last[k]
        if not rec.get("parentUuid") and rec.get("uuid"):
            rec["parentUuid"] = self._prev_uuid          # synthesise a linear walk
            self._prev_uuid = rec["uuid"]
        return self._ingest(rec)

    def _ingest(self, rec):
        self._cur_ts = epoch(rec.get("timestamp")) or self._cur_ts
        t = rec.get("type")
        if t == "assistant":
            self._assistant(rec)
        elif t == "user":
            self._user(rec)
        else:
            self.skipped[t] += 1

    def _session_node(self, rec):
        sid = rec.get("sessionId") or "unknown"
        n = self.node("session", sid, label=sid[:8],
                      cwd=rec.get("cwd"), branch=rec.get("gitBranch"),
                      version=rec.get("version"), entrypoint=rec.get("entrypoint"))
        if rec.get("cwd"):
            r = self.node("repo", rec["cwd"], label=os.path.basename(rec["cwd"]) or rec["cwd"],
                          branch=rec.get("gitBranch"))
            self.edge(n["id"], r["id"], "WORKS_IN")
        return n

    def _step(self, rec, role):
        uid = rec.get("uuid") or f"anon{len(self.nodes)}"
        s = self.node("step", uid, label=role,
                      role=role, ts=rec.get("timestamp"), model=rec.get("requestedModel"),
                      effort=rec.get("effort"), sidechain=bool(rec.get("isSidechain")),
                      thinking_ms=rec.get("thinkingDurationMs"), cwd=rec.get("cwd"))
        sess = self._session_node(rec)
        self.edge(s["id"], sess["id"], "IN_SESSION")
        if rec.get("parentUuid"):
            self.edge(s["id"], f"step:{rec['parentUuid']}", "PARENT")
        return s

    def _assistant(self, rec):
        msg = rec.get("message") or {}
        s = self._step(rec, "assistant")
        model = msg.get("model") or rec.get("requestedModel")
        usd, parts, toks, guessed = price(model, msg.get("usage"))
        self.price_guessed |= bool(guessed)
        s["attrs"].update({"model": model, "usd": round(usd, 6), "tokens": toks,
                           "cost_parts": {k: round(v, 6) for k, v in parts.items()}})
        s["usd"] = usd
        self.usd += usd
        for k, v in toks.items():
            self.totals[k] += v
        self.totals["steps"] += 1
        self.totals["usd_micros"] += int(usd * 1e6)

        text = []
        touched = []
        for c in (msg.get("content") or []) if isinstance(msg.get("content"), list) else []:
            if not isinstance(c, dict):
                continue
            if c.get("type") == "text":
                text.append(c.get("text") or "")
            elif c.get("type") == "tool_use":
                touched += self._tool_use(rec, s, c)
        s["attrs"]["text_preview"] = (" ".join(text).strip()[:280]) or None
        s["attrs"]["n_tools"] = len(touched)

        # cost attribution
        targets = touched if self.attribution == "caused" else self.last_results
        if targets and usd:
            share = usd / len(targets)
            for nid in targets:
                n = self.nodes.get(nid)
                if n:
                    n["usd"] += share
        self.last_results = []

    def _tool_use(self, rec, step, block):
        name = block.get("name") or "?"
        inp = block.get("input") if isinstance(block.get("input"), dict) else {}
        tid = block.get("id")
        self._links = []                      # what this call wired to what, and how
        acts = self._actions(name, inp, rec)
        hits = self._materialize(step, acts, rec)
        verbs = [a["verb"] for a in acts if a["src"] != "agent"] or [a["verb"] for a in acts]
        verb = Counter(verbs).most_common(1)[0][0] if verbs else "run"
        self.pending[tid] = (step["id"], name, inp, hits)
        self.calls.append({"id": tid, "step": step["id"], "tool": name, "nodes": hits,
                           "links": self._links, "verb": verb,
                           "ts": rec.get("timestamp"), "input": self._thin(inp)})
        return hits

    def _known_for(self, rec):
        """Names seen in *this* session: a VM in one session is not a VM in another."""
        sid = rec.get("sessionId") or "?"
        k = self.known_by_session.get(sid)
        if k is None:
            k = self.known_by_session[sid] = {"vm": set(), "host": set(), "service": set()}
        return k

    def _vocab_for(self, cwd):
        """--vocab wins; otherwise the working tree's own .sessiongraph.json, if any."""
        if self.vocab_explicit or not cwd:
            return self.vocab
        v = self.vocab_by_cwd.get(cwd)
        if v is None:
            v = self.vocab_by_cwd[cwd] = AB.load_vocab(cwd=cwd)
        return v

    def _actions(self, name, inp, rec):
        ctx = AB.Context(cwd=rec.get("cwd"), vocab=self._vocab_for(rec.get("cwd")), known=self._known_for(rec))
        try:
            return AB.tool_actions(name, inp, ctx)
        except Exception:                      # heuristics never take the parser down
            return []

    def _thing(self, t, rec):
        """A classifier `thing` -> node id, with its structure (repo ⊃ dir ⊃ file,
        domain ⊃ url) wired in."""
        if not t:
            return None
        kind, key, label, attrs = t["kind"], t["key"], t.get("label"), dict(t.get("attrs") or {})
        cwd = rec.get("cwd")
        if kind == "file":
            role = attrs.pop("role", None)
            return self._file(key, cwd, role=role, **attrs)
        if kind == "dir":
            if cwd and key.startswith(cwd + os.sep):
                label = os.path.relpath(key, cwd)
            n = self.node("dir", key, label=label, **attrs)
            n["touches"] += 1
            self._contain(key, cwd, n["id"])
            return n["id"]
        if kind == "repo":
            n = self.node("repo", key, label=label or os.path.basename(key),
                          branch=rec.get("gitBranch") if key == cwd else None)
            return n["id"]
        if kind == "url":
            dm = attrs.pop("domain", None) or domain_of(key)
            n = self.node("url", key, label=label, **attrs)
            if dm:
                self.edge(self.node("domain", dm)["id"], n["id"], "CONTAINS")
            return n["id"]
        if kind in ("vm", "host", "service"):
            self._known_for(rec)[kind].add(key)
        n = self.node(kind, key, label=label, **attrs)
        n["touches"] += 1
        return n["id"]

    def _materialize(self, step, acts, rec):
        """actions -> nodes, step edges (verb-typed), and the call's links."""
        hit = []
        for a in acts:
            dst = self._thing(a["dst"], rec)
            if not dst:
                continue
            src = "agent" if a["src"] == "agent" else self._thing(a["src"], rec)
            if not src:
                continue
            verb = a["verb"]
            if src == "agent":
                self.edge(step["id"], dst, verb.upper(), op=a.get("op"))
            elif a.get("remote") or a.get("clone"):          # repo -> remote, vm -> its template: structure
                self.edge(src, dst, "REMOTE" if a.get("remote") else "CLONED")
                self._link(src, dst, verb)
                continue
            else:
                self.edge(step["id"], src, "RAN")
                self.edge(src, dst, verb.upper(), via=True)
                if a.get("against"):
                    self.edge(src, dst, "AGAINST")
                if src not in hit:
                    hit.append(src)
            self._link(src, dst, verb)
            if dst not in hit:
                hit.append(dst)
        return hit

    def _link(self, src, dst, verb):
        """A call's own structure: tool -> command -> file, tool -> repo, ..."""
        if src and dst and src != dst:
            self._links.append([src, dst, verb])

    def _repo_for(self, rec):
        cwd = rec.get("cwd")
        if not cwd:
            return None
        return self.node("repo", cwd, label=os.path.basename(cwd) or cwd,
                         branch=rec.get("gitBranch"))

    @staticmethod
    def _thin(inp):
        out = {}
        for k, v in list(inp.items())[:8]:
            sv = v if isinstance(v, (int, float, bool)) else str(v)
            out[k] = sv if not isinstance(sv, str) or len(sv) <= 400 else sv[:400] + "…"
        return out

    def _file(self, path, cwd=None, role=None, **attrs):
        if not path:
            return None
        if not os.path.isabs(path) and cwd and not re.match(r"^[A-Za-z]:[\\/]", path):
            path = os.path.normpath(os.path.join(cwd, path))
        f = self.node("file", path, label=os.path.basename(path),
                      ext=(os.path.splitext(path)[1] or "").lstrip("."), n_touch=1, **attrs)
        f["touches"] += 1
        if role:                              # a file that is run becomes a tool for good
            if role == "tool" or not f["attrs"].get("role"):
                f["attrs"]["role"] = role
        self._contain(path, cwd, f["id"])
        return f["id"]

    def _contain(self, path, cwd, nid):
        d = os.path.dirname(path.rstrip("/"))
        repo_id = f"repo:{cwd}" if cwd and path.startswith(str(cwd) + os.sep) else None
        if repo_id and repo_id in self.nodes:
            if d and d != cwd:                              # repo ⊃ dir ⊃ file
                dn = self.node("dir", d, label=os.path.relpath(d, cwd))
                self.edge(repo_id, dn["id"], "CONTAINS")
                if dn["id"] != nid:
                    self.edge(dn["id"], nid, "CONTAINS")
            elif nid != repo_id:
                self.edge(repo_id, nid, "CONTAINS")
        elif d and d not in ("/", ""):
            dn = self.node("dir", d, label=os.path.basename(d) or d)
            if dn["id"] != nid:
                self.edge(dn["id"], nid, "CONTAINS")

    def _gh(self, step, text, source):
        out = []
        if not text:
            return out
        text = text[:40000]
        for owner, repo, kind, num in PR_RE.findall(text):
            n = self.node("gh_repo", f"{owner}/{repo}", n_mentions=1, source=source)
            p = self.node("pr" if kind == "pull" else "issue", f"{owner}/{repo}#{num}",
                          label=f"{repo}#{num}")
            self.edge(step["id"], p["id"], "MENTIONS")
            self.edge(p["id"], n["id"], "IN_REPO")
            out += [n["id"], p["id"]]
        for owner, repo in GH_RE.findall(text):
            if repo in ("pull", "issues", "blob", "tree"):
                continue
            n = self.node("gh_repo", f"{owner}/{repo}", n_mentions=1, source=source)
            self.edge(step["id"], n["id"], "MENTIONS")
            out.append(n["id"])
        return out

    def _user(self, rec):
        tur = rec.get("toolUseResult")
        msg = rec.get("message") or {}
        content = msg.get("content")
        ids = [c.get("tool_use_id") for c in content
               if isinstance(c, dict) and c.get("type") == "tool_result"] if isinstance(content, list) else []
        if tur is None and not ids:
            if isinstance(content, str) or isinstance(content, list):
                s = self._step(rec, "prompt")
                txt = content if isinstance(content, str) else " ".join(
                    c.get("text", "") for c in content if isinstance(c, dict))
                s["attrs"]["text_preview"] = txt.strip()[:280] or None
                self.totals["prompts"] += 1
            return
        # a tool result: enrich the call's entities and remember them for `context` mode
        tid = ids[0] if ids else None
        step_id, tool, inp, _hits = self.pending.pop(tid, (None, None, {}, []))
        step = self.nodes.get(step_id) or {"id": step_id or "step:?"}
        res = tur if isinstance(tur, dict) else {}
        blocks = [c for c in content if isinstance(c, dict) and c.get("type") == "tool_result"] \
            if isinstance(content, list) else []
        block_err = any(b.get("is_error") for b in blocks)
        blob = ""
        produced = []
        if "stdout" in res:
            blob = (str(res.get("stdout") or "") + "\n" + str(res.get("stderr") or ""))
        elif isinstance(tur, str):
            blob = tur
        if "stdout" in res or isinstance(tur, str):
            call = next((c for c in reversed(self.calls) if c["id"] == tid), None)
            found = [(b, sha, subj) for b, sha, subj in COMMIT_RE.findall(blob[:8000])]
            if not found and call and any(v == "commit" for _, _, v in call["links"]):
                # `git commit -q` prints nothing; the agent's own `git log --oneline` does
                m = re.search(r"(?m)^([0-9a-f]{7,12})\s+(\S.*)$", blob[:4000])
                if m:
                    found = [(rec.get("gitBranch") or "", m.group(1), m.group(2))]
            for branch, sha, subj in found[:1]:
                c = self.node("commit", sha[:7], label=sha[:7], branch=branch or None, subject=subj[:100])
                self.edge(step["id"], c["id"], "COMMITTED")
                if rec.get("cwd"):
                    self.edge(c["id"], f"repo:{rec['cwd']}", "IN_REPO")
                    if call:
                        call["links"].append([f"repo:{rec['cwd']}", c["id"], "commit"])
                produced.append(c["id"])
        elif "results" in res:                       # WebSearch
            q = (res.get("query") or "")[:160]
            sn = self.nodes.get(f"search:{q}")
            urls = []
            for item in (res.get("results") or []):
                for c in (item.get("content") or []) if isinstance(item, dict) else []:
                    u = c.get("url") if isinstance(c, dict) else None
                    if u:
                        urls.append(u)
            for u in urls[:40]:
                ut = AB.url_thing(u) or {"kind": "url", "key": u[:400], "label": (domain_of(u) or u)[:40]}
                un = self.node(ut["kind"], ut["key"], label=ut["label"])
                dm = domain_of(u)
                if dm:
                    self.edge(self.node("domain", dm)["id"], un["id"], "CONTAINS")
                if sn:
                    self.edge(sn["id"], un["id"], "YIELDED")
                    for call in reversed(self.calls):
                        if call["id"] == tid:
                            call["links"].append([sn["id"], un["id"], "yield"]); break
            if sn:
                sn["attrs"]["n_results"] = len(urls)
                sn["attrs"]["duration_s"] = res.get("durationSeconds")
                produced.append(sn["id"])
            blob = " ".join(urls)
        elif "bytes" in res and "url" in res:        # WebFetch
            un = self.nodes.get(f"url:{str(res['url'])[:400]}")
            if un:
                un["attrs"]["bytes"] = res.get("bytes")
                un["attrs"]["status"] = res.get("code")
                produced.append(un["id"])
            blob = str(res.get("result") or "")
        elif "file" in res and isinstance(res.get("file"), dict):   # Read
            fi = res["file"]
            fn = self.nodes.get(f"file:{fi.get('filePath')}")
            if fn:
                fn["attrs"]["n_lines"] = fi.get("numLines")
                fn["attrs"]["bytes_in"] = len(str(fi.get("content") or ""))
                produced.append(fn["id"])
        if not blob and isinstance(content, list):
            blob = " ".join(str(c.get("content") if not isinstance(c.get("content"), list) else
                                " ".join(x.get("text", "") for x in c["content"] if isinstance(x, dict)))
                            for c in content if isinstance(c, dict) and c.get("type") == "tool_result")
        if step_id:
            produced += self._gh(step, blob, "result")
            for c in self.calls:
                if c["id"] == tid:
                    c["result_bytes"] = len(json.dumps(res, default=str)) if res else len(blob)
                    c["is_error"] = bool(res.get("is_error") or res.get("interrupted") or block_err)
                    acted = any(v in AB.OUTCOME_VERBS for _, _, v in c["links"])
                    c["outcome"] = AB.outcome_of(blob, c["is_error"]) if (acted or c["is_error"]) else None
                    produced += self._result_rules(step, c, blob, rec)
                    break
        self.last_results += [p for p in produced if p]

    def _result_rules(self, step, call, blob, rec):
        """Vocabulary `results` rules: text in a result that proves an instrument
        ran (a hook that graded the file just written, say) becomes
        instrument —verb→ the call's things, —against→ the API it called, and
        the scores the API returned land on that API node and on the call."""
        out = []
        import fnmatch
        for rule in self._vocab_for(rec.get("cwd")).get("results", []):
            try:
                if not re.search(rule["pattern"], blob[:20000]):
                    continue
            except Exception:
                continue
            name = rule.get("instrument", "hook")
            scores = self._scores(rule.get("score"), blob)
            verb = rule.get("verb", "evaluate")
            ag = rule.get("against")
            # the call already names the instrument on its command line: it is the
            # instrument node; otherwise the rule proves a hidden run of it
            explicit = [s for s, _, _ in call["links"]
                        if s != "agent" and fnmatch.fnmatch(self.nodes.get(s, {}).get("label", ""), name + "*")]
            if explicit:
                inst = self.nodes[explicit[0]]
            else:
                made = [d for s, d, v in call["links"] if v in ("write", "edit", "commit")]
                if not made:                   # a tail of an old log is not a new evaluation
                    continue
                inst = self.node("prog", name, label=name)
                inst["touches"] += 1
                self.edge(step["id"], inst["id"], "RAN")
                targets = [d for d in made if self.nodes.get(d, {}).get("type") in ("file", "repo", "commit")][:3]
                for t in targets:
                    self.edge(inst["id"], t, verb.upper(), via=True)
                    call["links"].append([inst["id"], t, verb])
                out.append(inst["id"])
            # the API it called: from the rule, or from an `against` link already on the call
            api_id = next((d for s, d, v in call["links"] if s == inst["id"] and v == "against"), None)
            if not api_id and ag and ":" in ag:
                k, _, key = ag.partition(":")
                api_id = self.node(k, key, label=key)["id"]
                self.edge(inst["id"], api_id, "AGAINST")
                call["links"].append([inst["id"], api_id, "against"])
                out.append(api_id)
            if api_id:
                api = self.nodes[api_id]
                api["touches"] += 1
                api["attrs"]["n_calls"] = api["attrs"].get("n_calls", 0) + 1
                if scores:
                    api["attrs"]["last_scores"] = scores
                call["api"] = api_id
            if scores:
                inst["attrs"]["last_scores"] = scores
                call["scores"] = scores
        return out

    @staticmethod
    def _scores(pattern, blob):
        """Every score in a result: `noul=([0-9.]+)` gives [0.95, 0.85, …];
        a two-group pattern ``(\\w+) noul=([0-9.]+)`` gives {name: value}."""
        if not pattern:
            return None
        try:
            ms = list(re.finditer(pattern, blob[:20000]))
        except Exception:
            return None
        if not ms:
            return None
        def num(x):
            try:
                return float(x)
            except Exception:
                return x
        if ms[0].re.groups >= 2:
            return {m.group(1): num(m.group(2)) for m in ms[:12]}
        return [num(m.group(1)) for m in ms[:12]]

    # --- output
    def to_dict(self):
        for n in self.nodes.values():
            n["usd"] = round(n["usd"], 6)
        return {"generated": time.time(), "attribution": self.attribution,
                "totals": dict(self.totals), "usd": round(self.usd, 4),
                "price_guessed": self.price_guessed,
                "skipped_record_types": dict(self.skipped),
                "nodes": list(self.nodes.values()),
                "edges": list(self.edges.values()), "calls": self.calls}


def iter_records(path):
    """Yield records from a local JSONL transcript, or from a dump of the
    remote events API (a JSON array, the {"ccr":{"data":[...]}} envelope, or a
    tool result with prose around it).

    Decided by structure, not by sniffing for JSON anywhere in the file: a
    transcript starts with `{` too, and one that happens to *contain* a pasted
    API dump was being parsed as that dump, yielding nothing.
    """
    def expand(doc):
        if isinstance(doc, dict):
            doc = doc.get("ccr") or doc
            doc = doc.get("data") or doc.get("events") or []
        return doc if isinstance(doc, list) else []

    with open(path, "r", errors="replace") as fh:
        first = ""
        while True:
            line = fh.readline()
            if not line:
                break
            if line.strip():
                first = line.strip()
                break
        if not first:
            return
        try:
            head = json.loads(first)
        except Exception:
            head = None

        if isinstance(head, dict) and any(k in head for k in ("ccr", "data", "events")):
            for rec in expand(head):            # a one-line API dump
                yield rec
            return
        if head is not None:                    # JSONL: stream it, line by line
            yield head
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except Exception:
                    continue
            return

        # neither: a JSON payload with something wrapped around it
        fh.seek(0)
        raw = fh.read()
        dec = json.JSONDecoder()
        for marker in ('{"ccr"', '{"data"', '{"events"', "[", "{"):
            i = raw.find(marker)
            if i < 0:
                continue
            try:
                doc, _ = dec.raw_decode(raw[i:])
            except ValueError:
                continue
            out = expand(doc) if isinstance(doc, dict) else (doc if isinstance(doc, list) else [])
            if out:
                for rec in out:
                    yield rec
                return


def find_sessions(root=None):
    root = root or os.path.expanduser("~/.claude/projects")
    out = []
    for dirpath, _, files in os.walk(root):
        for f in files:
            if f.endswith(".jsonl"):
                p = os.path.join(dirpath, f)
                try:
                    out.append((os.path.getmtime(p), p))
                except OSError:
                    pass
    return [p for _, p in sorted(out, reverse=True)]


def report(g):
    d = g.to_dict()
    t = d["totals"]
    L = []
    sess = [n for n in d["nodes"] if n["type"] == "session"]
    L.append(f"sessions {len(sess)}  steps {t.get('steps',0)}  prompts {t.get('prompts',0)}  "
             f"nodes {len(d['nodes'])}  edges {len(d['edges'])}")
    L.append(f"tokens  in {t.get('in',0):,}  out {t.get('out',0):,}  "
             f"cache_write {t.get('cache_write',0):,}  cache_read {t.get('cache_read',0):,}  "
             f"thinking {t.get('thinking',0):,}")
    L.append(f"API-equivalent cost  ${d['usd']:.4f}" + ("  (model price guessed)" if d["price_guessed"] else ""))
    if d["skipped_record_types"]:
        L.append("non-conversation records: " + ", ".join(
            f"{k}×{v}" for k, v in sorted(d["skipped_record_types"].items(), key=lambda x: -x[1])))
    by_type = Counter(n["type"] for n in d["nodes"])
    L.append("node mix: " + "  ".join(f"{k}×{v}" for k, v in by_type.most_common()))
    L.append("edge mix: " + "  ".join(f"{k}×{v}" for k, v in
                                      Counter(e["type"] for e in d["edges"]).most_common()))
    inst = sorted((n for n in d["nodes"] if n["type"] == "prog" or n["attrs"].get("role") == "tool"),
                  key=lambda n: -n["touches"])
    if inst:
        L.append("instruments: " + "  ".join(f"{n['label']}×{n['touches']}" for n in inst[:12]))
    verbs = Counter(e["type"].lower() for e in d["edges"] if e["type"].lower() in AB.VERB_DIR)
    if verbs:
        L.append("verbs: " + "  ".join(f"{k}×{v}" for k, v in verbs.most_common()))
    ents = sorted((n for n in d["nodes"] if n["type"] not in ("step", "session")),
                  key=lambda n: -n["usd"])
    if ents:
        L.append("\ntop entities by attributed cost (%s):" % d["attribution"])
        for n in ents[:12]:
            L.append(f"  ${n['usd']:.4f}  {n['type']:<9} {n['label'][:62]}")
    # branch points in the walk = rewinds
    kids = Counter(e["dst"] for e in d["edges"] if e["type"] == "PARENT")
    forks = [k for k, v in kids.items() if v > 1]
    if forks:
        L.append(f"\nwalk branches at {len(forks)} step(s) — rewound/edited prompts")
    return "\n".join(L)


DOT_STYLE = {"session": "box3d,#7dd3fc", "step": "circle,#64748b", "tool": "box,#a78bfa",
             "file": "note,#34d399", "dir": "folder,#10b981", "repo": "folder3d,#22d3ee",
             "gh_repo": "component,#f472b6", "commit": "hexagon,#fbbf24", "command": "cds,#f59e0b",
             "search": "ellipse,#e879f9", "url": "oval,#60a5fa", "domain": "doublecircle,#3b82f6",
             "agent": "septagon,#fb7185", "skill": "trapezium,#c084fc", "mcp": "box,#2dd4bf",
             "pr": "hexagon,#f472b6", "issue": "hexagon,#fda4af"}


def to_dot(g, skip_steps=False):
    d = g.to_dict()
    out = ["digraph session {", '  bgcolor="#0b1020"; node [style=filled,fontcolor="#0b1020",fontname=Helvetica];',
           '  edge [color="#475569",fontcolor="#94a3b8",fontsize=9];']
    keep = set()
    for n in d["nodes"]:
        if skip_steps and n["type"] == "step":
            continue
        keep.add(n["id"])
        shape, color = DOT_STYLE.get(n["type"], "box,#94a3b8").split(",")
        lbl = n["label"].replace('"', "'")[:40]
        extra = f"\\n${n['usd']:.3f}" if n["usd"] >= 0.0005 else ""
        out.append(f'  "{n["id"]}" [label="{lbl}{extra}",shape={shape},fillcolor="{color}"];')
    for e in d["edges"]:
        if e["src"] in keep and e["dst"] in keep:
            w = f' label="{e["weight"]}"' if e["weight"] > 1 else ""
            out.append(f'  "{e["src"]}" -> "{e["dst"]}" [xlabel="{e["type"].lower()}"{w}];')
    out.append("}")
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("paths", nargs="*", help="jsonl transcripts (default: newest session)")
    ap.add_argument("--all", action="store_true", help="every session under ~/.claude/projects")
    ap.add_argument("-n", type=int, default=1, help="how many recent sessions (default 1)")
    ap.add_argument("-o", "--out", help="write graph JSON here")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--dot", action="store_true")
    ap.add_argument("--no-steps", action="store_true", help="--dot: entities only")
    ap.add_argument("--attribution", choices=["caused", "context"], default="caused")
    ap.add_argument("--root", help="override ~/.claude/projects")
    ap.add_argument("--vocab", help="project vocabulary (.sessiongraph.json), see ABSTRACTIONS.md")
    a = ap.parse_args()

    paths = a.paths or find_sessions(a.root)[: (None if a.all else a.n)]
    if not paths:
        sys.exit("no transcripts found; pass a path or --root")
    g = SessionGraph(attribution=a.attribution, vocab=a.vocab)
    for p in paths:
        for rec in iter_records(p):
            g.ingest(rec)
    if a.dot:
        print(to_dot(g, a.no_steps))
    if a.out:
        with open(a.out, "w") as fh:
            json.dump(g.to_dict(), fh)
        print(f"wrote {a.out} ({len(g.nodes)} nodes, {len(g.edges)} edges) from {len(paths)} session(s)",
              file=sys.stderr)
    if a.report or not (a.out or a.dot):
        print(report(g))


if __name__ == "__main__":
    main()
