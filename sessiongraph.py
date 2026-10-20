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
CMD_HEAD = re.compile(r"^\s*(?:\w+=\S*\s+)*([\w./-]+)(?:\s+([\w:-]+))?")
MULTI = {"git", "gh", "npm", "pnpm", "yarn", "cargo", "docker", "kubectl", "uv", "pip", "go", "make"}
# Shell plumbing that is never what the step was "about".
BORING = {"cd", "export", "set", "source", ".", "true", "false", "echo", "pushd", "popd",
          "unset", "for", "done", "while", "until", "if", "elif", "fi", "case", "esac",
          "in", "function", "break", "continue", "return", "exit", "shift", "trap", "wait"}
# Grammar words that *precede* the real command: peel them off, don't drop the segment.
PEEL = {"do", "then", "else", "time", "exec", "eval", "nohup", "sudo", "command", "env"}


HEREDOC = re.compile(r"<<-?\s*[\"\']?(\w+)[\"\']?")


def strip_shell(command):
    """Remove heredoc bodies, quoted strings and URLs before parsing a bash line.

    Without this, the *contents* of `cat > f <<EOF ... EOF` and `python3 -c "..."`
    get split on `;` and `|` and every `import`/`for`/`const` in the payload shows
    up as a command node. (Found by looking at the rendered graph.)
    """
    if not command:
        return ""
    for _ in range(12):                  # a single bash call can carry several
        m = HEREDOC.search(command)
        if not m:
            break
        term = m.group(1)
        head = command[:m.start()]
        rest = command[m.end():].split("\n")
        tail, closed = [], False
        for ln in rest[1:]:
            if closed:
                tail.append(ln)
            elif ln.strip() == term:
                closed = True
        command = head + "\n" + "\n".join(tail)
    # command substitution: surface the inner command as its own segment
    command = command.replace("$(", " ; ").replace("`", " ; ")
    command = re.sub(r"https?://\S+", " ", command)
    command = re.sub(r"'[^']*'|\"[^\"]*\"", " ", command)
    return command


def cmd_labels(command, limit=3):
    """A bash call is often a pipeline. Return the commands that carry meaning,
    skipping `cd foo &&` / `VAR=x` prefixes that otherwise dominate the graph."""
    command = strip_shell(command)
    if not command.strip():
        return []
    out, seen = [], set()
    for seg in re.split(r"&&|\|\||[;|]|\n", command):
        seg = seg.strip().lstrip("(").lstrip(")")
        seg = re.sub(r"^(?:\w+=\S*\s*)+", "", seg)      # VAR=x VAR2=y cmd ...
        if not seg or seg.startswith(("#", "-")):
            continue
        base = None
        for _ in range(4):                      # `do sleep 1`, `sudo nohup foo`
            m = CMD_HEAD.match(seg)
            if not m:
                base = None
                break
            base = os.path.basename(m.group(1))
            if base in PEEL:
                seg = seg[m.end(1):].strip()
                continue
            break
        if not base or base in BORING or base in PEEL or "=" in base or len(base) < 2:
            continue
        if not re.fullmatch(r"[\w.+-]{2,40}", base):
            continue
        m = CMD_HEAD.match(seg)
        sub = m.group(2)
        lbl = f"{base} {sub}" if base in MULTI and sub and not sub.startswith("-") else base
        if lbl not in seen:
            seen.add(lbl)
            out.append(lbl)
        if len(out) >= limit:
            break
    return out


def cmd_label(command):
    labels = cmd_labels(command, 1)
    return labels[0] if labels else None


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


# ------------------------------------------------------------------ verbs ---
# What was DONE, as distinct from what it was done to. Colour carries the kind
# of thing (3 validated hues); the verb is carried by glyph + direction.
# dir: -1 data came back to the agent, +1 data went out, 0 both/neither.
VERB_DIR = {"read":-1, "search":-1, "fetch":-1, "pull":-1, "list":-1,
            "write":1, "edit":0, "delete":1, "push":1, "commit":1,
            "execute":0, "spawn":1, "skill":0, "mcp":0, "note":1}
CMD_VERB = {
    "cat":"read", "head":"read", "tail":"read", "less":"read", "more":"read",
    "wc":"read", "ls":"read", "du":"read", "df":"read", "stat":"read",
    "file":"read", "diff":"read", "readlink":"read", "jq":"read", "awk":"read",
    "grep":"search", "rg":"search", "find":"search", "fd":"search", "locate":"search",
    "mkdir":"write", "touch":"write", "cp":"write", "mv":"write", "tee":"write",
    "sed":"write", "chmod":"write", "chown":"write", "ln":"write", "truncate":"write",
    "rm":"delete", "rmdir":"delete", "unlink":"delete", "shred":"delete",
    "curl":"fetch", "wget":"fetch", "scp":"fetch", "rsync":"fetch",
    "git commit":"commit", "git push":"push", "git tag":"commit", "git merge":"commit",
    "git pull":"pull", "git fetch":"pull", "git clone":"pull", "git checkout":"pull",
    "git status":"read", "git log":"read", "git diff":"read", "git show":"read",
    "gh pr":"push", "gh release":"push", "gh api":"fetch", "gh repo":"pull",
}
TOOL_VERB = {
    "Read":"read", "NotebookRead":"read", "Write":"write", "Edit":"edit",
    "NotebookEdit":"edit", "Grep":"search", "Glob":"search", "WebSearch":"search",
    "WebFetch":"fetch", "Task":"spawn", "Agent":"spawn", "Skill":"skill",
    "TodoWrite":"note", "ToolSearch":"search", "Artifact":"push",
}


def verb_of(tool, inp):
    """One verb per call. Bash is refined by what the command actually does."""
    if tool in TOOL_VERB:
        return TOOL_VERB[tool]
    if tool and tool.startswith("mcp__"):
        return "mcp"
    if tool == "Bash":
        for lbl in cmd_labels(inp.get("command") if isinstance(inp, dict) else None, 4):
            if lbl in CMD_VERB:
                return CMD_VERB[lbl]
            base = lbl.split(" ")[0]
            if base in CMD_VERB:
                return CMD_VERB[base]
        return "execute"
    return "execute"


class SessionGraph:
    """Incremental builder. Feed it records; ask for nodes/edges any time."""

    def __init__(self, attribution="caused"):
        self._cur_ts = None
        self._prev_uuid = None
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
        if rec.get("type") in ("assistant", "user"):
            return rec                                   # already local shape
        for role in ("assistant", "user"):
            body = rec.get(role)
            if isinstance(body, dict):
                r = dict(body)
                r.update(r.pop("internal_anthropic_catchall", None) or {})
                out = {"type": role, "message": r.get("message"),
                       "uuid": r.get("uuid"),
                       "timestamp": r.get("timestamp") or rec.get("created_at"),
                       "sessionId": r.get("session_id") or r.get("sessionId"),
                       "cwd": r.get("cwd"), "gitBranch": r.get("gitBranch"),
                       "requestedModel": (r.get("message") or {}).get("model"),
                       "thinkingDurationMs": r.get("thinking_duration_ms"),
                       "isSidechain": bool(r.get("parent_tool_use_id")),
                       "toolUseResult": r.get("toolUseResult") or r.get("tool_use_result")}
                return out
        return None

    def ingest(self, rec):
        t = rec.get("type") if isinstance(rec, dict) else None
        if t and t not in ("assistant", "user"):
            self.skipped[t] += 1          # keep the per-type breakdown
            return
        rec = self.normalize(rec)
        if rec is None:
            self.skipped["unrecognised"] += 1
            return
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
        tool = self.node("tool", name, n_calls=1)
        self.edge(step["id"], tool["id"], "USED", tool=name)
        hits = self._entities(step, name, inp, rec)
        self.pending[tid] = (step["id"], name, inp, hits)
        self.calls.append({"id": tid, "step": step["id"], "tool": name, "nodes": hits,
                           "verb": verb_of(name, inp),
                           "ts": rec.get("timestamp"), "input": self._thin(inp)})
        return hits

    @staticmethod
    def _thin(inp):
        out = {}
        for k, v in list(inp.items())[:8]:
            sv = v if isinstance(v, (int, float, bool)) else str(v)
            out[k] = sv if not isinstance(sv, str) or len(sv) <= 400 else sv[:400] + "…"
        return out

    def _file(self, step, path, verb, cwd=None):
        if not path:
            return None
        if not os.path.isabs(path) and cwd:
            path = os.path.normpath(os.path.join(cwd, path))
        f = self.node("file", path, label=os.path.basename(path),
                      ext=(os.path.splitext(path)[1] or "").lstrip("."), n_touch=1)
        f["touches"] += 1
        self.edge(step["id"], f["id"], verb, tool=verb)
        d = os.path.dirname(path)
        repo_id = f"repo:{cwd}" if cwd and path.startswith(str(cwd) + os.sep) else None
        if repo_id and repo_id in self.nodes:
            self.edge(repo_id, f["id"], "CONTAINS")
        elif d:
            self.edge(self.node("dir", d, label=os.path.basename(d) or d)["id"], f["id"], "CONTAINS")
        return f["id"]

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

    def _entities(self, step, name, inp, rec):
        cwd = rec.get("cwd")
        hit = []
        if name in ("Read", "NotebookEdit"):
            hit.append(self._file(step, inp.get("file_path") or inp.get("notebook_path"), "READ", cwd))
        elif name == "Write":
            hit.append(self._file(step, inp.get("file_path"), "WROTE", cwd))
        elif name == "Edit":
            hit.append(self._file(step, inp.get("file_path"), "EDITED", cwd))
        elif name in ("Grep", "Glob"):
            tgt = inp.get("path") or cwd
            if tgt:
                n = self.node("dir", tgt, label=os.path.basename(str(tgt)) or str(tgt))
                self.edge(step["id"], n["id"], "GREPPED", pattern=str(inp.get("pattern"))[:120])
                hit.append(n["id"])
        elif name == "Bash":
            for lbl in cmd_labels(inp.get("command")):
                n = self.node("command", lbl, n_runs=1)
                self.edge(step["id"], n["id"], "RAN", cmd=str(inp.get("command"))[:200])
                hit.append(n["id"])
            # file paths named on the command line are real touches too
            for tok in re.findall(r"(?<![\w:-])((?:/|\./|~/)[\w./@+-]{2,})",
                                  strip_shell(inp.get("command")))[:6]:
                if not tok.endswith("/") and os.path.splitext(tok)[1]:
                    hit.append(self._file(step, os.path.expanduser(tok), "READ", cwd))
            hit += self._gh(step, inp.get("command"), "bash")
        elif name == "WebSearch":
            q = (inp.get("query") or "").strip()
            if q:
                n = self.node("search", q[:160], label=q[:60], engine="web")
                self.edge(step["id"], n["id"], "SEARCHED")
                hit.append(n["id"])
        elif name == "WebFetch":
            u = inp.get("url")
            if u:
                n = self.node("url", u[:400], label=(domain_of(u) or u)[:40])
                self.edge(step["id"], n["id"], "FETCHED")
                dm = domain_of(u)
                if dm:
                    self.edge(self.node("domain", dm)["id"], n["id"], "CONTAINS")
                hit.append(n["id"])
            hit += self._gh(step, u, "fetch")
        elif name in ("Agent", "Task"):
            a = inp.get("subagent_type") or "general-purpose"
            n = self.node("agent", a, n_spawns=1)
            self.edge(step["id"], n["id"], "SPAWNED", desc=str(inp.get("description"))[:80])
            hit.append(n["id"])
        elif name == "Skill":
            n = self.node("skill", inp.get("skill") or "?", n_uses=1)
            self.edge(step["id"], n["id"], "USED")
            hit.append(n["id"])
        elif name.startswith("mcp__"):
            parts = name.split("__")
            if len(parts) >= 2:
                n = self.node("mcp", parts[1], n_calls=1)
                self.edge(step["id"], n["id"], "CALLED", tool=name)
                hit.append(n["id"])
        return [h for h in hit if h]

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
        blob = ""
        produced = []
        if "stdout" in res:
            blob = (str(res.get("stdout") or "") + "\n" + str(res.get("stderr") or ""))
            for branch, sha, subj in COMMIT_RE.findall(blob[:8000]):
                c = self.node("commit", sha[:7], label=sha[:7], branch=branch, subject=subj[:100])
                self.edge(step["id"], c["id"], "COMMITTED")
                if rec.get("cwd"):
                    self.edge(c["id"], f"repo:{rec['cwd']}", "IN_REPO")
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
                un = self.node("url", u[:400], label=(domain_of(u) or u)[:40])
                dm = domain_of(u)
                if dm:
                    self.edge(self.node("domain", dm)["id"], un["id"], "CONTAINS")
                if sn:
                    self.edge(sn["id"], un["id"], "YIELDED")
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
        if step_id:
            produced += self._gh(step, blob, "result")
            for c in self.calls:
                if c["id"] == tid:
                    c["result_bytes"] = len(json.dumps(res, default=str)) if res else 0
                    c["is_error"] = bool(res.get("is_error") or res.get("interrupted"))
                    break
        self.last_results += [p for p in produced if p]

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
    remote events API (a JSON array, or the {"ccr":{"data":[...]}} envelope)."""
    with open(path, "r", errors="replace") as fh:
        head = fh.read(4096)
        fh.seek(0)
        stripped = head.lstrip()
        if stripped[:1] == "[" or '"ccr"' in head[:200] or '"data"' in head[:200]:
            try:
                raw = fh.read()
                i = min((j for j in (raw.find("["), raw.find("{")) if j >= 0), default=0)
                doc = json.loads(raw[i:])
                if isinstance(doc, dict):
                    doc = (doc.get("ccr") or doc)
                    doc = doc.get("data") or doc.get("events") or []
                for rec in doc:
                    yield rec
                return
            except Exception:
                fh.seek(0)                      # not a dump after all; fall through
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except Exception:
                continue


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
    tools = sorted((n for n in d["nodes"] if n["type"] == "tool"),
                   key=lambda n: -n["attrs"].get("n_calls", 0))
    if tools:
        L.append("tools: " + "  ".join(f"{n['label']}×{n['attrs'].get('n_calls',0)}" for n in tools[:12]))
    ents = sorted((n for n in d["nodes"] if n["type"] not in ("step", "session", "tool")),
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
    a = ap.parse_args()

    paths = a.paths or find_sessions(a.root)[: (None if a.all else a.n)]
    if not paths:
        sys.exit("no transcripts found; pass a path or --root")
    g = SessionGraph(attribution=a.attribution)
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
