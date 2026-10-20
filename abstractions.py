#!/usr/bin/env python3
"""From tool calls to actions: actor —verb→ thing, sometimes via an instrument.

See ABSTRACTIONS.md. A shell command is not a node. `git commit` is a commit
verb on the repository; `tail matrix.log` is a watch of that log; `./tools/qtest
shot win10-acc out.tar` is the agent running the qtest instrument, which
captured the VM win10-acc into an archive. Plumbing (echo, cut, date, sleep,
cd, VAR=) is folded away.

Stdlib only. Everything here is a heuristic over text the agent typed; a repo
can refine it with a `.sessiongraph.json` vocabulary (see `load_vocab`).
"""
import fnmatch, json, os, re

# -------------------------------------------------------------------- kinds --
CLASS = {
    "agent": "agent", "prog": "agent", "skill": "agent",
    "repo": "artifact", "dir": "artifact", "file": "artifact", "commit": "artifact",
    "vm": "world", "host": "world", "service": "world", "api": "world", "url": "world",
    "domain": "world", "search": "world", "gh_repo": "world", "pr": "world",
    "issue": "world", "run": "world", "process": "world", "package": "world",
}
# dir: -1 data came back to the agent, +1 went out into the world, 0 both.
VERB_DIR = {
    "read": -1, "search": -1, "watch": -1, "probe": -1, "fetch": -1, "capture": -1,
    "pull": -1, "extract": 0, "evaluate": 0, "run": 0, "call": 0, "edit": 0, "skill": 0,
    "write": 1, "delete": 1, "commit": 1, "push": 1, "revert": 1, "build": 1,
    "install": 1, "launch": 1, "stop": 1, "spawn": 1, "against": 0,
}
# a result only says ok/fail about calls that *did* something; a cat of a log that
# says FAIL is not a failed call
OUTCOME_VERBS = {"evaluate", "run", "build", "install", "launch", "stop", "commit", "push",
                 "call", "capture", "extract", "pull", "fetch", "edit", "write", "delete",
                 "revert", "spawn"}


def thing(kind, key, label=None, **attrs):
    return {"kind": kind, "key": key, "label": label or key, "attrs": attrs}


def action(src, verb, dst, **extra):
    a = {"src": src, "verb": verb, "dst": dst}
    a.update(extra)
    return a


# ---------------------------------------------------------------- file roles --
MEDIA = {"png", "jpg", "jpeg", "gif", "webp", "webm", "mp4", "mov", "avi", "mkv", "mp3",
         "wav", "ogg", "svg", "pdf", "ico", "bmp", "tiff"}
ARCHIVE = {"tar", "tgz", "gz", "zip", "7z", "bz2", "xz", "zst", "iso", "img", "rpm", "deb",
           "whl", "jar", "bundle"}
DATA = {"json", "jsonl", "csv", "tsv", "sqlite", "db", "parquet", "pkl", "npy", "arrow"}
DOC = {"md", "rst", "txt", "adoc", "org", "tex", "html", "htm"}
CONFIG = {"yml", "yaml", "toml", "ini", "cfg", "conf", "env", "lock", "properties", "xml",
          "plist", "service", "editorconfig", "gitignore"}
SOURCE = {"py", "js", "mjs", "cjs", "ts", "tsx", "jsx", "c", "h", "cpp", "cc", "hpp", "rs",
          "go", "java", "rb", "php", "ps1", "psm1", "sh", "bash", "zsh", "fish", "lua",
          "swift", "kt", "cs", "css", "scss", "sql", "pl", "pm", "r", "jl", "ex", "exs",
          "erl", "hs", "ml", "scala", "dart", "vue", "svelte", "m", "mm", "asm", "s",
          "cmake", "mk", "nix", "zig", "v", "proto", "graphql", "inf", "def", "rc", "idl"}
TEST_DIRS = {"test", "tests", "spec", "specs", "__tests__", "selftest", "fixtures"}


def file_role(path):
    p = path.replace("\\", "/")
    base = os.path.basename(p)
    ext = (os.path.splitext(base)[1] or "").lstrip(".").lower()
    low = p.lower()
    if ext == "log" or ext in ("out", "err") or "/logs/" in low or base.endswith(".log.txt"):
        return "log"
    if ext in MEDIA:
        return "media"
    if ext in ARCHIVE:
        return "archive"
    if "/.github/workflows/" in low or base in ("jenkinsfile", ".gitlab-ci.yml", ".travis.yml",
                                                "azure-pipelines.yml"):
        return "ci"
    if "/scratchpad/" in low or low.startswith("/tmp/") or "/scratch/" in low:
        return "scratch"
    parts = set(low.split("/"))
    if parts & TEST_DIRS or re.search(r"(^|[-_.])(self)?tests?([-_.]|$)|_spec\.|\.spec\.", base.lower()):
        return "test"
    if ext in DOC:
        return "doc"
    if ext in DATA:
        return "data"
    if ext in CONFIG or base in ("makefile", "dockerfile", "cmakelists.txt", "package.json",
                                 "pyproject.toml", "cargo.toml", "go.mod"):
        return "config"
    if ext in SOURCE:
        return "source"
    return "file"


def file_label(path):
    return os.path.basename(path.rstrip("/")) or path


# --------------------------------------------------------------- vocabulary --
# Built-in result rules: text in a tool result that proves an instrument ran
# without being named on the command line (a hook, a CI gate).
DEFAULT_RESULTS = [
    {"pattern": r"pre-commit\b[^\n]*(REFUSED|failed|FAILED|rejected)", "instrument": "pre-commit", "verb": "evaluate"},
    {"pattern": r"(?m)^(?:hook|husky)\b[^\n]*(failed|FAILED|error)", "instrument": "hook", "verb": "evaluate"},
]


def load_vocab(path=None, cwd=None):
    """`.sessiongraph.json` at the repo root, or an explicit path. Shape:
    {"instruments": {"glob": {"verb": v, "against": "kind:key"}},
     "entities": {"vm": ["^win1[01]\\w*"], "host": [...]}}"""
    cands = [path] if path else []
    if cwd:
        cands.append(os.path.join(cwd, ".sessiongraph.json"))
    for c in cands:
        try:
            with open(c) as f:
                v = json.load(f)
            v.setdefault("instruments", {})
            v.setdefault("entities", {})
            v["results"] = list(v.get("results") or []) + DEFAULT_RESULTS
            v["_compiled"] = {k: [re.compile(p) for p in pats] for k, pats in v["entities"].items()}
            return v
        except Exception:
            continue
    return {"instruments": {}, "entities": {}, "results": list(DEFAULT_RESULTS), "_compiled": {}}


# ----------------------------------------------------------- shell scanning --
HEREDOC = re.compile(r"(?<!<)<<(?!<)-?\s*([\"']?)(\w+)\1")
ASSIGN = re.compile(r"^([A-Za-z_]\w*)=(.*)$", re.S)
PLUMBING = {"cd", "export", "set", "true", "false", "echo", "printf", "pushd", "popd",
            "unset", "for", "done", "while", "until", "if", "elif", "fi", "then", "else",
            "case", "esac", "in", "function", "break", "continue", "return", "exit",
            "shift", "trap", "wait", "sleep", "date", "cut", "awk", "sort", "uniq", "tr",
            "wc", "head", "tail", "sed", "basename", "dirname", "xargs", "seq", "column",
            "test", "[", "[[", "local", "declare", "readonly", "read", "mkdir", "which",
            "type", "hash", "alias", "yes", "nproc", "tee", "cat", "less", "more", "nl",
            "paste", "rev", "fold", "fmt", "expr", "let", "bc", "env", "printenv", "clear",
            "pwd", "tput", "stty", "sync", "umask", "ulimit", "times", "jobs", "fg", "bg",
            "disown", "getopts", "eval", "exec", "logout", "select", "caller", "enable",
            "builtin", "mapfile", "readarray", "compgen", "complete", "history", "help",
            "comm", "join", "shuf", "tac", "od", "hexdump", "xxd", "strings", "iconv",
            "dos2unix", "unix2dos", "realpath", "readlink", "numfmt", "factor", "sha256sum",
            "md5sum", "sha1sum", "cksum", "jq", "yq", "python-json", "ts", "timeout", "nice",
            "ionice", "chronic", "stdbuf", "unbuffer", "script", "tty", "id", "whoami",
            "groups", "hostname", "uname", "arch", "locale", "lscpu", "free", "uptime",
            "df", "du", "mount", "lsblk"}
# commands that *read* the files they name (plumbing above, but the file matters)
READERS = {"cat", "head", "tail", "less", "more", "nl", "wc", "stat", "file", "jq", "yq",
           "diff", "cmp", "md5sum", "sha256sum", "sha1sum", "od", "hexdump", "xxd", "strings",
           "column", "sort", "uniq", "cut", "awk", "sed", "tac", "ls", "tree", "du",
           "readlink", "realpath", "source", ".", "ffprobe", "identify", "exiftool",
           "mediainfo", "pdftotext", "pdfinfo", "zcat", "bzcat", "xzcat", "sqlite3",
           "python-json", "base64", "iconv", "dos2unix"}
SEARCHERS = {"grep", "egrep", "fgrep", "rg", "ag", "ack", "find", "fd", "fdfind", "locate",
             "ls", "tree", "git-grep"}
PEEL = {"command", "sudo", "doas", "env", "nohup", "time", "exec", "eval", "do", "then",
        "else", "nice", "ionice", "chronic", "stdbuf", "unbuffer", "caffeinate", "setsid",
        "!", "if", "while", "until", "builtin"}
PEEL_ARG = {"timeout", "xargs", "nice", "stdbuf", "ionice"}   # take one or more options
INTERP = {"python", "python2", "python3", "py", "node", "nodejs", "deno", "bun", "perl",
          "ruby", "php", "bash", "sh", "zsh", "dash", "ksh", "fish", "pwsh", "powershell",
          "ts-node", "tsx", "lua", "luajit", "Rscript", "julia", "osascript", "expect"}
WRITERS = {"cp": "write", "mv": "write", "touch": "write", "tee": "write", "ln": "write",
           "install": "write", "dd": "write", "rsync-local": "write", "truncate": "write",
           "chmod": "edit", "chown": "edit", "chgrp": "edit", "patch": "edit", "sed-i": "edit",
           "rm": "delete", "rmdir": "delete", "unlink": "delete", "shred": "delete"}
PKG_MANAGERS = {"pip", "pip3", "pipx", "uv", "npm", "pnpm", "yarn", "bun", "apt", "apt-get",
                "dnf", "yum", "brew", "pacman", "apk", "zypper", "gem", "cargo", "go",
                "conda", "mamba", "poetry", "pdm", "composer", "nix-env", "snap", "flatpak",
                "choco", "winget", "scoop"}
PKG_INSTALL = {"install", "add", "i", "get", "sync", "update", "upgrade", "reinstall", "ci"}
NET = {"curl", "wget", "http", "https", "xh", "httpie", "aria2c", "fetch"}
LOCAL_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "::1", "[::1]", "host.docker.internal"}
HOST_PROBES = {"ps", "top", "htop", "lsof", "ss", "netstat", "ip", "ifconfig", "pgrep",
               "pidof", "fuser", "vmstat", "iostat", "dmesg", "lsmod", "lspci", "lsusb",
               "nvidia-smi", "sensors", "systemd-analyze", "loginctl", "w", "who", "last"}

# instrument names (programs or scripts) -> verb, matched against name + subcommand + flags
NAME_VERBS = [
    (r"\b(pytest|py\.test|tox|nox|unittest|jest|vitest|mocha|ava|karma|cypress|bats|"
     r"shellcheck|flake8|ruff|pylint|mypy|pyright|eslint|hadolint|yamllint|jsonlint|"
     r"phpunit|rspec|minitest|ctest|clippy|vet|golangci-lint|stylelint|markdownlint|"
     r"luacheck|cppcheck|clang-tidy|lint\w*|checkstyle|spotbugs|pytype|bandit|semgrep|"
     r"trivy|grype|nuclei|nmap)\b", "evaluate"),
    (r"(^|[-_./ ])(self)?tests?([-_./ ]|$)|--selftest|--check\b|--dry-run|(^|[-_./ ])"
     r"(check|lint|grade|eval\w*|jev\w*|verify|verif\w*|bench\w*|assert\w*|audit|validate|"
     r"compare|judge|score|review|inspect|diagnos\w*|doctor)([-_./ ]|$)", "evaluate"),
    (r"(^|[-_./ ])(shot|screenshot|capture|snap\w*|record|rec\d*|grab|dump)([-_./ ]|$)", "capture"),
    (r"(^|[-_./ ])(build|compile|make|pack|bundle|assemble|link|cmake|ninja|meson|webpack|"
     r"rollup|esbuild|vite|tsc|gcc|g\+\+|clang\w*|cc|javac|mvn|gradle|rustc|cargo build|"
     r"go build|dotnet build|zig build)([-_./ ]|$)", "build"),
    (r"(^|[-_./ ])(probe|ping|status|health\w*|poll|watch|monitor|wait|liveness|heartbeat|"
     r"tail)([-_./ ]|$)", "probe"),
    (r"(^|[-_./ ])(deploy|install|setup|provision|bootstrap|prepare|configure|init)([-_./ ]|$)",
     "install"),
    (r"(^|[-_./ ])(launch|start|boot|spawn|serve|server|run-?campaign|up)([-_./ ]|$)", "launch"),
    (r"(^|[-_./ ])(stop|kill|halt|teardown|down|shutdown|cancel|abort)([-_./ ]|$)", "stop"),
    (r"(^|[-_./ ])(fetch|download|pull|sync|mirror|clone)([-_./ ]|$)", "fetch"),
    (r"(^|[-_./ ])(push|upload|publish|release|submit|send)([-_./ ]|$)", "push"),
    (r"(^|[-_./ ])(gen\w*|render|export|convert|transcode|ffmpeg|pandoc|dot|graphviz|"
     r"plot|report|summar\w*|extract\w*|scrape|parse)([-_./ ]|$)", "write"),
    (r"(^|[-_./ ])(clean\w*|purge|wipe|reset|rm|remove|delete)([-_./ ]|$)", "delete"),
]
EVALUATORS = {"pytest", "py.test", "tox", "nox", "jest", "vitest", "mocha", "ava", "karma",
              "cypress", "bats", "shellcheck", "flake8", "ruff", "pylint", "mypy", "pyright",
              "eslint", "hadolint", "yamllint", "phpunit", "rspec", "ctest", "golangci-lint",
              "stylelint", "markdownlint", "cppcheck", "clang-tidy", "bandit", "semgrep",
              "prettier", "black", "isort", "clang-format", "rustfmt", "gofmt", "tsc"}
BUILDERS = {"make", "cmake", "ninja", "meson", "webpack", "rollup", "esbuild", "vite", "gcc",
            "g++", "clang", "clang++", "cc", "c++", "javac", "mvn", "gradle", "rustc",
            "dotnet", "zig", "msbuild", "xcodebuild", "swift", "nasm", "ld", "ar", "bazel",
            "buck", "scons"}
AGENTS = {"claude", "codex", "aider", "gemini", "copilot", "cursor-agent", "opencode", "goose"}
URL_RE = re.compile(r"https?://[^\s\"'<>)\]]+")
PATHISH = re.compile(r"^(?:~|\.{1,2})?/|^[\w.@+*?-]+(?:/[\w.@+*?-]*)+$|^[\w.@+*?-]+\.[A-Za-z0-9*?]{1,6}$")
PROC_RE = re.compile(r"^/proc/(\d+|self)(?:/|$)")
TASK_RE = re.compile(r"/tasks/([A-Za-z0-9_-]+)\.output$")
VERSION_FLAG = re.compile(r"^--?(v|version|help|h)$")


def split_shell(command):
    """Quote- and heredoc-aware split of a bash line into segments.

    Returns [(tokens, flags)] where tokens are words with their quotes removed
    (`"$S/x.tar"` stays one token), redirections appear as '>' '>>' '<'
    tokens, `$(...)` bodies become segments of their own, and flags carry
    {"bg": True} for `cmd &` and {"heredoc": body} when the segment fed a
    heredoc. Assignments made earlier in the same command are expanded, so a
    scratch path spelled `$S/probe.tar` comes out whole.
    """
    bodies = []

    def _strip_heredocs(text):
        out, i = [], 0
        while True:
            m = HEREDOC.search(text, i)
            if not m:
                out.append(text[i:])
                break
            term = m.group(2)
            nl = text.find("\n", m.end())
            if nl < 0:
                out.append(text[i:])
                break
            rest = text[nl + 1:]
            lines, tail, closed = rest.split("\n"), [], False
            body = []
            for k, ln in enumerate(lines):
                if ln.strip() == term or ln.strip() == term.strip("-"):
                    closed, tail = True, lines[k + 1:]
                    break
                body.append(ln)
            if not closed:
                tail = []
            bodies.append("\n".join(body))
            out.append(text[i:m.start()] + f" __HEREDOC{len(bodies) - 1}__ " + text[m.end():nl] + "\n")
            text = "\n".join(tail)
            i = 0
            if not closed:
                break
        return "".join(out) if len(out) > 1 or not out else out[0]

    text = _strip_heredocs(command)
    text = re.sub(r"\$\(\([^()]*(?:\([^()]*\)[^()]*)*\)\)", " 0 ", text)   # $((L-14)) arithmetic
    text = re.sub(r"\d?>&\d", " ", text)          # 2>&1
    text = re.sub(r"&>\s*", " > ", text)
    text = re.sub(r"(?<!\S)\d>", ">", text)       # 2> /dev/null

    segs, cur, tok, stack = [], [], [], []
    flags = {}
    had_tok = False
    i, n = 0, len(text)

    def flush_tok():
        nonlocal had_tok
        if tok or had_tok:
            cur.append("".join(tok))
            tok.clear()
        had_tok = False

    def flush_seg():
        flush_tok()
        if cur:
            segs.append((cur[:], dict(flags)))
        cur.clear()
        flags.clear()

    while i < n:
        c = text[i]
        if c == "#" and not tok and (i == 0 or text[i - 1].isspace() or text[i - 1] in ";|&("):
            j = text.find("\n", i)                     # a comment, to end of line
            i = n if j < 0 else j
            continue
        if c == "'":
            j = text.find("'", i + 1)
            j = n if j < 0 else j
            tok.append(text[i + 1:j]); had_tok = True; i = j + 1
            continue
        if c == '"':
            j, buf = i + 1, []
            while j < n and text[j] != '"':
                if text[j] == "\\" and j + 1 < n and text[j + 1] in '"\\$`':
                    buf.append(text[j + 1]); j += 2; continue
                if text[j] == "$" and text[j + 1:j + 2] == "(":     # "$(stat ... )" inside quotes
                    depth, k = 1, j + 2
                    while k < n and depth:
                        depth += {"(": 1, ")": -1}.get(text[k], 0); k += 1
                    for s2 in split_shell(text[j + 2:k - 1]):
                        segs.append(s2)
                    j = k
                    continue
                buf.append(text[j]); j += 1
            tok.append("".join(buf)); had_tok = True; i = j + 1
            continue
        if c == "\\" and i + 1 < n:
            if text[i + 1] == "\n":
                i += 2; continue
            tok.append(text[i + 1]); had_tok = True; i += 2
            continue
        if c == "$" and text[i + 1:i + 2] == "(":
            flush_tok(); stack.append((cur[:], dict(flags))); cur.clear(); flags.clear(); i += 2
            continue
        if c == "`":
            j = text.find("`", i + 1)
            if j > 0:
                inner = text[i + 1:j]
                flush_tok()
                for s2 in split_shell(inner):
                    segs.append(s2)
                i = j + 1
                continue
        if c == ")" and stack:
            flush_seg()
            outer, oflags = stack.pop()
            cur.extend(outer); flags.update(oflags); i += 1
            continue
        if c == "(" and not stack and text[i + 1:i + 2] == ")" and tok:
            flags["funcdef"] = "".join(tok)           # name() { ... }: a definition, not a call
            flush_tok(); i += 2
            continue
        if c in "()" and not stack:
            flush_tok(); i += 1
            continue
        if c in "{}" and (i + 1 >= n or text[i + 1].isspace() or text[i + 1] in ";\n") and \
                (i == 0 or text[i - 1].isspace() or text[i - 1] in ";\n"):
            flush_tok(); i += 1
            continue
        if c == "\n" or c == ";":
            flush_seg(); i += 1
            continue
        if c == "|":
            flush_seg(); i += 2 if text[i + 1:i + 2] == "|" else 1
            continue
        if c == "&":
            if text[i + 1:i + 2] == "&":
                flush_seg(); i += 2
            else:
                flags["bg"] = True; flush_seg(); i += 1
            continue
        if c == ">":
            flush_tok()
            if text[i + 1:i + 2] == ">":
                cur.append(">>"); i += 2
            else:
                cur.append(">"); i += 1
            continue
        if c == "<":
            flush_tok(); cur.append("<"); i += 1
            continue
        if c.isspace():
            flush_tok(); i += 1
            continue
        tok.append(c); had_tok = True; i += 1
    while stack:
        flush_seg()
        outer, oflags = stack.pop()
        cur.extend(outer); flags.update(oflags)
    flush_seg()

    # expand VAR=value assignments seen earlier in the command, attach heredocs
    env, out = {}, []
    for toks, fl in segs:
        def sub(s):
            return re.sub(r"\$\{(\w+)\}|\$(\w+)",
                          lambda m: env.get(m.group(1) or m.group(2), m.group(0)), s)
        toks = [sub(t) for t in toks]
        k = 0
        while k < len(toks):
            m = ASSIGN.match(toks[k])
            if not m:
                break
            if "$(" not in m.group(2) and m.group(2) != "":   # FF=$(…) is unknowable: leave $FF alone
                env[m.group(1)] = m.group(2)
                fl.setdefault("assign", {})[m.group(1)] = m.group(2)
            k += 1
        if k and k < len(toks) and toks[k - 1].startswith("for ") is False and toks[0] == "for":
            pass
        if toks[:1] == ["for"] and len(toks) >= 4 and toks[2] == "in":
            items = [t for t in toks[3:] if t and t not in ("do", ";")]
            if items:
                env[toks[1]] = items[0]
                fl["loop"] = (toks[1], items)
        hd = [t for t in toks if t.startswith("__HEREDOC") and t.endswith("__")]
        if hd:
            idx = int(hd[0][9:-2])
            fl["heredoc"] = bodies[idx] if idx < len(bodies) else ""
            toks = [t for t in toks if not (t.startswith("__HEREDOC") and t.endswith("__"))]
        toks = [t for t in toks if t != ""]
        if toks:
            out.append((toks, fl))
    return out


# ------------------------------------------------------------ classification --
class Context:
    """Per-session memory the classifier needs: names of things seen before
    (a VM named once is a VM every time it is mentioned), the cwd, the vocab."""

    def __init__(self, cwd=None, vocab=None, known=None):
        self.cwd = cwd
        self.vocab = vocab or {"instruments": {}, "entities": {}, "_compiled": {}}
        self.known = known if known is not None else {"vm": set(), "host": set(), "service": set()}

    def remember(self, kind, name):
        if kind in self.known and name:
            self.known[kind].add(name)

    def entity_for_name(self, tok):
        """A bare word that names a thing we know (or the vocab declares)."""
        if not tok or tok.startswith("-") or "/" in tok or len(tok) > 64:
            return None
        for kind, pats in self.vocab.get("_compiled", {}).items():
            for p in pats:
                if p.search(tok):
                    return thing(kind, tok)
        for kind in ("vm", "host", "service"):
            if tok in self.known.get(kind, ()):
                return thing(kind, tok)
        return None

    def vocab_instrument(self, name, relpath=None):
        for pat, spec in self.vocab.get("instruments", {}).items():
            if fnmatch.fnmatch(name, pat) or (relpath and fnmatch.fnmatch(relpath, pat)):
                return spec
        return None

    def resolve(self, path):
        if not path:
            return None
        path = os.path.expanduser(path)
        if re.match(r"^[A-Za-z]:[\\/]", path):          # a Windows transcript
            return path
        if not os.path.isabs(path):
            if not self.cwd:
                return None
            path = os.path.join(self.cwd, path)
        return os.path.normpath(path)


def is_pathish(tok, bare=False):
    """`bare`: the command takes directories, so a plain word (`src`, `.`) counts."""
    if not tok or tok.startswith("-") or "$" in tok or tok == "/" or "://" in tok:
        return False
    if tok in (".", ".."):
        return bare
    if re.search(r"[\s^,;|&<>\"'()`{}=]", tok):      # a sed script, a regex, an assignment: not a path
        return False
    if tok.startswith(("/", "./", "../", "~/")):
        return True
    if bare and re.fullmatch(r"[\w.@+*?-]+/?", tok) and not tok.isdigit():
        return True
    return bool(PATHISH.match(tok)) and not tok.endswith(":")


def file_thing(ctx, tok, role=None, prefer_dir=False):
    """A file or directory named on the command line, or None for noise.

    `prefer_dir`: the command takes directories (find, grep -r, ls), so a
    path without an extension is a folder. A glob (`rel-*`) is the folder it
    enumerates, labelled with the star so it reads as a pattern."""
    if not tok or "$" in tok or tok.startswith("-") or "://" in tok:
        return None
    if tok.startswith("/dev/") or tok.startswith("/sys/"):
        return None
    m = PROC_RE.match(tok)
    if m:
        pid = m.group(1)
        return thing("process", pid, label=f"pid {pid}" if pid != "self" else "self")
    if tok == "/proc" or tok.startswith("/proc/"):
        return thing("host", "localhost", label="this host")
    m = TASK_RE.search(tok)
    if m:
        return thing("process", m.group(1), label=f"task {m.group(1)}")
    p = ctx.resolve(tok)
    if not p or p.count("/") == 0:
        return None
    base = os.path.basename(p)
    if "*" in base or "?" in base or "[" in base:
        return thing("dir", p, label=base)
    if ctx.cwd and (p == ctx.cwd or ctx.cwd.startswith(p + os.sep)):
        return thing("repo", p, label=file_label(p)) if p == ctx.cwd else thing("dir", p, label=file_label(p))
    ext = os.path.splitext(base)[1]
    if tok.endswith("/") or base in ("", ".") or (not ext and (prefer_dir or base.islower() and len(base) < 4)):
        return thing("dir", p, label=file_label(p))
    return thing("file", p, label=file_label(p), role=role or file_role(p))


def dir_thing(ctx, tok):
    p = ctx.resolve(tok)
    if not p:
        return None
    if ctx.cwd and p == ctx.cwd:
        return thing("repo", p, label=file_label(p))
    return thing("dir", p, label=file_label(p))


def url_thing(u):
    m = re.match(r"https?://([^/?#]+)([^?#]*)", u)
    if not m:
        return None
    host, path = m.group(1).lower(), m.group(2)
    hostname = host.split(":")[0]
    if hostname in LOCAL_HOSTS:
        port = host.split(":")[1] if ":" in host else ""
        key = f"localhost:{port}" if port else "localhost"     # 127.0.0.1:8777 == localhost:8777
        return thing("service", key, label=key)
    segs = [s for s in path.split("/") if s]
    if hostname.startswith("api.") or any(s in ("api", "graphql") or re.fullmatch(r"v\d+", s) for s in segs[:2]):
        pre = "/".join(segs[:2])
        key = f"{hostname}/{pre}" if pre else hostname
        return thing("api", key, label=key[:48])
    tail = segs[-1][:24] if segs else ""
    label = f"{hostname}/{tail}" if tail and tail not in ("index.html",) else hostname
    return thing("url", u[:400], label=label[:44], domain=hostname)


def pathlike_targets(ctx, args, limit=4, prefer_dir=True):
    out = []
    for a in args:
        if a.startswith("-") or a in (">", ">>", "<"):
            continue
        if a.startswith(("http://", "https://")):
            t = url_thing(a)
        else:
            t = ctx.entity_for_name(a) or (file_thing(ctx, a, prefer_dir=prefer_dir) if is_pathish(a) else None)
        if t and t not in out:
            out.append(t)
        if len(out) >= limit:
            break
    return out


FLAG_VERBS = {"--selftest": "evaluate", "--self-test": "evaluate", "--check": "evaluate", "--test": "evaluate",
              "--lint": "evaluate", "--verify": "evaluate", "--dry-run": "evaluate", "--validate": "evaluate",
              "--build": "build", "--install": "install", "--serve": "launch", "--watch": "watch",
              "--capture": "capture", "--screenshot": "capture", "--record": "capture"}


def verb_for_name(name, rest=""):
    hay = f"{name} {rest}".lower()
    for pat, verb in NAME_VERBS:
        if re.search(pat, hay):
            return verb
    return None


def instrument_actions(ctx, inst, name, args, flags, relpath=None):
    """The agent ran an instrument; the instrument did something to its targets."""
    acts = []
    spec = ctx.vocab_instrument(name, relpath) or {}
    sub = next((a for a in args if not a.startswith("-")), "")
    flag_verb = next((FLAG_VERBS[a.split("=")[0]] for a in args if a.split("=")[0] in FLAG_VERBS), None)
    sub_verb = verb_for_name("", sub) if sub and not is_pathish(sub) else None
    verb = flag_verb or sub_verb or spec.get("verb") or verb_for_name(name) or "run"
    if flags.get("bg") and verb == "run":
        verb = "launch"
    targets = []
    for k, v in (flags.get("assign") or {}).items():           # QTEST_VM=win10-acc ./tools/qtest
        if re.search(r"(VM|HOST|GUEST|TARGET|MACHINE)$", k.upper()):
            kind = "host" if "HOST" in k.upper() else "vm"
            ctx.remember(kind, v)
            targets.append(thing(kind, v))
    for t in pathlike_targets(ctx, args):
        if t["kind"] == "file" and t["key"] == inst.get("key"):
            continue
        targets.append(t)
    # a server flag names the service that came up
    m = re.search(r"--port[= ](\d+)|-p[= ]?(\d{2,5})\b", " ".join(args))
    if m and verb in ("launch", "run"):
        port = m.group(1) or m.group(2)
        targets.append(thing("service", f"localhost:{port}", label=f"localhost:{port}"))
        verb = "launch"
    acts.append(action("agent", "launch" if flags.get("bg") else "run", inst))
    if targets:
        for t in targets[:5]:
            acts.append(action(inst, verb, t))
    elif verb == "evaluate":                    # `grade.py --selftest`: the script tests itself
        acts[0]["verb"] = "evaluate"
    elif verb in ("build", "install") and ctx.cwd:
        acts.append(action(inst, verb, thing("repo", ctx.cwd, label=file_label(ctx.cwd))))
    against = spec.get("against")
    if against and ":" in against:
        k, _, key = against.partition(":")
        acts.append(action(inst, "against", thing(k, key), against=True))
    return acts


def code_actions(ctx, code, lang="python"):
    """Inline code (python3 -c, python3 - <<PY, node -e): what did it touch?"""
    acts = []
    writes = bool(re.search(r"write_text|write_bytes|\.write\(|open\([^)]*['\"][wa]b?['\"]|"
                            r"writeFileSync|writeFile\(|fs\.write|\.save\(|os\.rename|shutil\.(copy|move)|"
                            r"json\.dump\(|replace_text|unlink|os\.remove", code))
    paths = re.findall(r"""(?:Path|open|read_text|readFileSync|readFile|loads?|remove|unlink)\s*\(\s*['"]([^'"\n]{2,200})['"]""", code)
    paths += re.findall(r"""['"]((?:/|\./|~/)[\w./@+-]{3,200})['"]""", code)
    seen = set()
    for p in paths:
        if p in seen or not is_pathish(p):
            continue
        seen.add(p)
        base = p.rstrip("/").split("/")[-1]
        if not os.path.splitext(base)[1] and p.count("/") < 2:      # '/sessions', '/select': a route
            continue
        t = file_thing(ctx, p)
        if t:
            acts.append(action("agent", "edit" if writes and t["kind"] == "file" else "read", t))
        if len(acts) >= 5:
            break
    for u in URL_RE.findall(code)[:3]:
        t = url_thing(u)
        if t:
            call = bool(re.search(r"\.post\(|\.put\(|\.patch\(|\.delete\(|method\s*=\s*['\"](POST|PUT|PATCH|DELETE)|data=|json=", code))
            acts.append(action("agent", "call" if call else "fetch", t))
    if re.search(r"subprocess|os\.system|child_process|spawnSync|execSync", code) and not acts:
        pass
    return acts


def git_actions(ctx, args):
    acts = []
    repo = thing("repo", ctx.cwd, label=file_label(ctx.cwd)) if ctx.cwd else None
    a = list(args)
    while a and a[0].startswith("-"):
        if a[0] == "-C" and len(a) > 1:
            repo = thing("repo", ctx.resolve(a[1]) or a[1], label=file_label(a[1])); a = a[2:]
        else:
            a = a[1:]
    if not a or not repo:
        return acts
    sub, rest = a[0], a[1:]
    paths = [r for r in rest if not r.startswith("-") and r != "--" and is_pathish(r)]
    if sub in ("commit", "add", "tag", "merge", "rebase", "cherry-pick", "am", "notes"):
        acts.append(action("agent", "commit", repo, op=sub))
    elif sub == "push":
        acts.append(action("agent", "push", repo))
    elif sub in ("pull", "fetch", "clone", "switch", "submodule"):
        acts.append(action("agent", "pull", repo, op=sub))
    elif sub in ("checkout", "restore", "reset", "revert", "stash", "clean", "rm"):
        if sub == "checkout" and not paths and "--" not in rest:
            acts.append(action("agent", "pull", repo, op="checkout"))
        else:
            acts.append(action("agent", "revert" if sub != "rm" else "delete", repo, op=sub))
            for p in paths[:3]:
                t = file_thing(ctx, p)
                if t:
                    acts.append(action("agent", "revert" if sub != "rm" else "delete", t))
    elif sub in ("status", "log", "diff", "show", "blame", "ls-files", "rev-parse", "describe",
                 "remote", "branch", "config", "cat-file", "reflog", "shortlog", "bisect",
                 "ls-tree", "rev-list", "count-objects", "var", "for-each-ref", "name-rev",
                 "check-ignore", "whatchanged", "stash list"):
        if sub == "branch" and any(r in ("-D", "-d", "-m") for r in rest):
            acts.append(action("agent", "edit", repo, op="branch"))
        elif sub == "config" and not any(r.startswith("--get") or r in ("-l", "--list") for r in rest) and len(rest) >= 2:
            acts.append(action("agent", "edit", repo, op="config"))
        elif sub == "remote" and rest[:1] in (["add"], ["set-url"], ["remove"]):
            acts.append(action("agent", "edit", repo, op="remote"))
        else:
            acts.append(action("agent", "read", repo, op=sub))
    elif sub == "grep":
        acts.append(action("agent", "search", repo))
    elif sub in ("init", "worktree", "bundle", "mv", "apply", "format-patch", "archive"):
        acts.append(action("agent", "write", repo, op=sub))
    else:
        acts.append(action("agent", "read", repo, op=sub))
    for m in re.finditer(r"github\.com[/:]([\w.-]+)/([\w.-]+?)(?:\.git)?(?=[/\s\"')\]]|$)", " ".join(rest)):
        gh = thing("gh_repo", f"{m.group(1)}/{m.group(2)}")
        acts.append(action(repo, acts[0]["verb"] if acts else "pull", gh, remote=True))
    return acts


def gh_actions(ctx, args):
    acts = []
    a = [x for x in args if x != "--"]
    if not a:
        return acts
    grp, sub, rest = a[0], (a[1] if len(a) > 1 else ""), a[2:]
    repo_flag = next((rest[i + 1] for i, r in enumerate(rest) if r in ("-R", "--repo") and i + 1 < len(rest)), None)
    repo_flag = repo_flag or next((r.split("=", 1)[1] for r in rest if r.startswith("--repo=")), None)
    num = next((r for r in strip_flag_values(rest, {"-L", "--limit", "-q", "--jq", "--json", "-R", "--repo", "-t", "--template", "-s", "--state", "-l", "--label", "-a", "--assignee", "-B", "--base", "-H", "--head", "-b", "--body", "-F", "--body-file", "-m", "--milestone", "-p", "--project", "-r", "--reviewer", "-w", "--workflow"}) if r.isdigit()), None)
    gh = thing("gh_repo", repo_flag) if repo_flag else None
    local = thing("repo", ctx.cwd, label=file_label(ctx.cwd)) if ctx.cwd else None
    where = gh or local
    READ = {"view", "list", "status", "checks", "diff", "ls", "get"}
    if grp == "pr":
        t = thing("pr", f"{repo_flag or file_label(ctx.cwd or '?')}#{num}", label=f"PR #{num}") if num else where
        acts.append(action("agent", "read" if sub in READ else "push", t, op=f"pr {sub}"))
    elif grp == "issue":
        t = thing("issue", f"{repo_flag or file_label(ctx.cwd or '?')}#{num}", label=f"issue #{num}") if num else where
        acts.append(action("agent", "read" if sub in READ else "push", t, op=f"issue {sub}"))
    elif grp == "run":
        t = thing("run", num, label=f"run {num}") if num else where
        verb = {"watch": "watch", "view": "probe", "list": "read", "download": "fetch",
                "cancel": "stop", "rerun": "launch"}.get(sub, "probe")
        acts.append(action("agent", verb, t, op=f"run {sub}"))
    elif grp == "workflow":
        name = next((r for r in rest if not r.startswith("-")), "")
        t = thing("run", f"workflow:{name}", label=f"workflow {name}" if name else "workflow")
        acts.append(action("agent", "launch" if sub == "run" else "read", t))
    elif grp == "api":
        path = next((r for r in a[1:] if not r.startswith("-")), "")
        method = next((a[i + 1] for i, r in enumerate(a) if r in ("-X", "--method") and i + 1 < len(a)), "GET")
        segs = [s for s in path.split("/") if s][:2]
        key = "api.github.com/" + "/".join(segs) if segs else "api.github.com"
        acts.append(action("agent", "fetch" if method.upper() == "GET" and not any(r in ("-f", "-F", "--input") for r in a) else "call",
                           thing("api", key, label=key)))
    elif grp == "repo":
        name = next((r for r in rest if not r.startswith("-") and "/" in r), None)
        t = thing("gh_repo", name) if name else where
        acts.append(action("agent", {"clone": "pull", "create": "write", "fork": "write",
                                     "delete": "delete", "edit": "edit", "sync": "pull"}.get(sub, "read"), t))
    elif grp == "release":
        acts.append(action("agent", "push" if sub in ("create", "upload", "edit") else "read", where))
    elif grp in ("secret", "variable", "label"):
        acts.append(action("agent", "edit" if sub in ("set", "create", "delete") else "read", where))
    elif grp == "auth":
        acts.append(action("agent", "probe", thing("service", "github.com", label="github.com")))
    elif grp == "search":
        acts.append(action("agent", "search", thing("service", "github.com", label="github.com")))
    elif grp in ("gist", "codespace", "cache", "project", "ruleset", "ssh-key", "gpg-key"):
        acts.append(action("agent", "read" if sub in READ else "write", thing("service", "github.com", label="github.com")))
    elif where:
        acts.append(action("agent", "read", where))
    return acts


VM_OPS = {
    "start": "launch", "create": "launch", "clone": "launch", "unpause": "launch", "resume": "launch",
    "restart": "launch", "up": "launch", "boot": "launch", "launch": "launch", "startvm": "launch",
    "shutdown": "stop", "kill": "stop", "pause": "stop", "halt": "stop", "destroy": "stop",
    "stop": "stop", "poweroff": "stop", "suspend": "stop", "down": "stop",
    "remove": "delete", "rm": "delete", "delete": "delete", "unregistervm": "delete", "purge": "delete",
    "check": "probe", "ls": "probe", "list": "probe", "tags": "probe", "prefs": "probe",
    "features": "probe", "volume": "probe", "device": "probe", "usb": "probe", "pci": "probe",
    "block": "probe", "info": "probe", "inspect": "probe", "ps": "probe", "stats": "probe",
    "top": "probe", "status": "probe", "dominfo": "probe", "domstate": "probe",
    "showvminfo": "probe", "port": "probe", "diff": "probe", "wait": "probe", "events": "probe",
    "run": "run", "exec": "run", "ssh": "run", "console": "watch", "attach": "watch",
    "logs": "watch", "log": "watch", "screenshot": "capture", "screendump": "capture",
    "shot": "capture", "snapshot": "capture", "backup": "capture", "export": "capture",
    "commit": "capture", "save": "capture",
    "copy-to-vm": "push", "move-to-vm": "push", "cp": "push", "push": "push",
    "copy": "push", "scp": "push", "sync": "push", "upload": "push",
    "template": "edit", "service": "edit", "firewall": "edit", "set": "edit", "controlvm": "stop",
    "modifyvm": "edit", "tag": "edit", "rename": "edit", "update": "edit",
    "build": "build", "pull": "fetch", "import": "fetch", "load": "fetch", "provision": "install",
    "apply": "launch", "rollout": "launch", "scale": "edit", "patch": "edit", "get": "probe",
    "describe": "probe", "portforward": "run", "port-forward": "run",
}


DOCKER_VALUED = {"-v", "--volume", "-p", "--publish", "-e", "--env", "-w", "--workdir", "-u", "--user",
                 "-c", "--cpus", "-m", "--memory", "--network", "--net", "--mount", "-l", "--label",
                 "--entrypoint", "--platform", "--restart", "--env-file", "--hostname", "-h", "--add-host",
                 "--device", "--gpus", "--tmpfs", "--log-driver", "--log-opt", "--cap-add", "--cap-drop",
                 "--security-opt", "--ulimit", "--shm-size", "--pid", "--ipc", "--expose", "--dns", "-f",
                 "--file", "--format", "--filter", "--target", "--build-arg", "--since", "--tail", "-n"}
QVM_VALUED = {"-u", "--user", "-p", "--pass-io", "--dispvm", "-c", "--class", "-t", "--template", "-l",
              "--label", "-P", "--pool", "--property", "--prop", "-f", "--filter", "--fields", "-O"}


def strip_flag_values(args, valued):
    """Drop `-v X` pairs (and `--flag=x`) so a flag's value is never taken for a name."""
    out, i = [], 0
    while i < len(args):
        a = args[i]
        if a in valued and i + 1 < len(args):
            i += 2; continue
        if a.startswith("--") and "=" in a:
            i += 1; continue
        out.append(a); i += 1
    return out


def vm_actions(ctx, base, args, flags):
    """Hypervisors, container runtimes, orchestrators: a verb on a machine."""
    acts = []
    kind = "vm"
    if base.startswith("qvm-") or base.startswith("qubes-"):
        op = base.split("-", 1)[1]
        rest = args
        rest = strip_flag_values(args, QVM_VALUED)
        name = next((a for a in rest if not a.startswith("-") and (ctx.entity_for_name(a) or {}).get("kind") == "vm"), None) \
            or next((a for a in rest if not a.startswith("-")), None)
        if op == "clone" and len([a for a in args if not a.startswith("-")]) >= 2:
            src, dst = [a for a in args if not a.startswith("-")][:2]
            ctx.remember("vm", src); ctx.remember("vm", dst)
            acts.append(action("agent", "launch", thing("vm", dst)))
            acts.append(action(thing("vm", dst), "pull", thing("vm", src), clone=True))
            return acts
        if op in ("ls", "pool", "template") and not name:
            return [action("agent", "probe", thing("host", "dom0", label="dom0"))]
        verb = VM_OPS.get(op, "probe")
        if op in ("prefs", "features", "tags", "service", "volume") and len(rest) >= 3:
            verb = "edit"
        if op == "run" and not name:
            verb = "run"
    elif base in ("docker", "podman", "nerdctl"):
        a = [x for x in args if x != "--"]
        op = next((x for x in a if not x.startswith("-")), "")
        rest = a[a.index(op) + 1:] if op in a else []
        if op in ("compose", "stack", "container", "image", "volume", "network", "system", "buildx"):
            sub = next((x for x in rest if not x.startswith("-")), "")
            rest = rest[rest.index(sub) + 1:] if sub in rest else []
            if op == "compose":
                verb = VM_OPS.get(sub, "probe")
                return [action("agent", verb, thing("service", "compose:" + file_label(ctx.cwd or "compose"),
                                                    label="compose " + file_label(ctx.cwd or "")))]
            op = sub
        if op == "build":
            tag = next((rest[i + 1] for i, r in enumerate(rest) if r in ("-t", "--tag") and i + 1 < len(rest)), None)
            t = thing("package", tag, label=tag) if tag else (thing("repo", ctx.cwd, label=file_label(ctx.cwd)) if ctx.cwd else None)
            return [action("agent", "build", t)] if t else []
        if op in ("pull", "push", "rmi", "images", "tag", "save", "load"):
            img = next((x for x in rest if not x.startswith("-")), None)
            if img:
                return [action("agent", {"pull": "fetch", "push": "push", "rmi": "delete", "tag": "edit",
                                         "save": "capture", "load": "fetch"}.get(op, "probe"),
                               thing("package", img, label=img))]
            return [action("agent", "probe", thing("host", "docker", label="docker"))]
        if op in ("ps", "info", "version", "system", "stats") and not rest:
            return [action("agent", "probe", thing("host", "docker", label="docker"))]
        name = next((rest[i + 1] for i, r in enumerate(rest) if r == "--name" and i + 1 < len(rest)), None) \
            or next((x.split("=", 1)[1] for x in rest if x.startswith("--name=")), None)
        if not name:
            name = next((x for x in strip_flag_values(rest, DOCKER_VALUED) if not x.startswith("-")), None)
        verb = "launch" if op == "run" else VM_OPS.get(op, "probe")    # docker run = start a container
    elif base == "virsh":
        op = next((x for x in args if not x.startswith("-")), "")
        rest = args[args.index(op) + 1:] if op in args else []
        name = next((x for x in rest if not x.startswith("-")), None)
        verb = VM_OPS.get(op, "probe")
        if op == "list" or not name:
            return [action("agent", "probe", thing("host", "libvirt", label="libvirt"))]
    elif base.lower() == "vboxmanage":
        op = next((x for x in args if not x.startswith("-")), "").lower()
        rest = args[args.index(op) + 1:] if op in args else []
        name = next((x for x in rest if not x.startswith("-")), None)
        verb = VM_OPS.get(op, "probe")
        if op == "list" or not name:
            return [action("agent", "probe", thing("host", "virtualbox", label="virtualbox"))]
    elif base in ("vagrant", "multipass", "limactl", "orb", "colima", "minikube", "kind"):
        op = next((x for x in args if not x.startswith("-")), "")
        rest = strip_flag_values(args[args.index(op) + 1:] if op in args else [], {"-c", "--cpus", "-m", "--memory", "-d", "--disk", "-n", "--name"})
        name = next((rest[i + 1] for i, r in enumerate(args) if r in ("-n", "--name") and i + 1 < len(args)), None) \
            or next((x for x in rest if not x.startswith("-")), None) or base
        verb = VM_OPS.get(op, "probe")
    elif base in ("kubectl", "oc", "helm", "k9s"):
        op = next((x for x in args if not x.startswith("-")), "")
        rest = args[args.index(op) + 1:] if op in args else []
        names = [x for x in rest if not x.startswith("-")][:2]
        name = "/".join(names) if names else None
        verb = VM_OPS.get(op, "probe")
        kind = "service"
    else:
        return acts
    if name and not name.startswith("-"):
        ctx.remember(kind, name)
        acts.append(action("agent", verb, thing(kind, name, label=name)))
    elif ctx.cwd:
        acts.append(action("agent", verb, thing("host", base, label=base)))
    return acts


def ssh_actions(ctx, base, args, flags):
    if base == "ssh":
        a = [x for x in args]
        host, cmd = None, []
        i = 0
        while i < len(a):
            t = a[i]
            if t.startswith("-"):
                if t in ("-p", "-i", "-o", "-l", "-J", "-F", "-L", "-R", "-D", "-W", "-b", "-c", "-e", "-m", "-E") and i + 1 < len(a):
                    i += 2; continue
                i += 1; continue
            if host is None:
                host = t.split("@")[-1]; i += 1; continue
            cmd = a[i:]; break
        if not host:
            return []
        ctx.remember("host", host)
        return [action("agent", "run" if cmd else "probe", thing("host", host, label=host))]
    if base in ("scp", "rsync", "sftp"):
        remote = [x for x in args if re.match(r"^[\w.@-]+:", x) and not x.startswith("-")]
        if not remote:
            if base == "rsync":
                paths = pathlike_targets(ctx, args, 2)
                return [action("agent", "write", t) for t in paths[1:]] + [action("agent", "read", t) for t in paths[:1]]
            return []
        last = args[-1] if args else ""
        host = remote[0].split(":")[0].split("@")[-1]
        ctx.remember("host", host)
        verb = "push" if last.split(":")[0] == remote[0].split(":")[0] and last in remote else "fetch"
        return [action("agent", verb, thing("host", host, label=host))]
    if base in ("ping", "ping6", "traceroute", "tracepath", "mtr", "dig", "nslookup", "host", "nc", "ncat", "telnet", "nmap", "arp-scan"):
        tgt = next((x for x in args if not x.startswith("-") and not x.isdigit()), None)
        if tgt:
            if tgt in LOCAL_HOSTS:
                port = next((x for x in args if x.isdigit()), None)
                return [action("agent", "probe", thing("service", f"localhost:{port}" if port else "localhost"))]
            ctx.remember("host", tgt)
            return [action("agent", "probe", thing("host", tgt, label=tgt))]
    return []


def net_actions(ctx, base, args):
    urls = [a for a in args if a.startswith(("http://", "https://"))]
    if not urls:
        urls = [a for a in args if re.match(r"^(localhost|127\.0\.0\.1|\[::1\])(:\d+)?(/|$)", a)]
        urls = ["http://" + u for u in urls]
    if not urls:
        return []
    joined = " ".join(args)
    method = re.search(r"(?:^|\s)(?:-X|--request)[= ]?(\w+)", joined)
    writes = bool(method and method.group(1).upper() != "GET") or bool(
        re.search(r"(?:^|\s)(-d|--data\S*|--json|-F|--form|-T|--upload-file|--post-data|--method=(?:POST|PUT|PATCH|DELETE))(?:\s|=|$)", joined))
    acts = []
    for u in urls[:3]:
        t = url_thing(u)
        if t:
            acts.append(action("agent", "push" if "-T" in args or "--upload-file" in args else ("call" if writes else "fetch"), t))
    out = next((args[i + 1] for i, a in enumerate(args) if a in ("-o", "--output", "-O") and i + 1 < len(args)), None)
    if out and out != "-":
        t = file_thing(ctx, out)
        if t:
            acts.append(action("agent", "write", t))
    return acts


def package_actions(ctx, base, args):
    a = [x for x in args if x != "--"]
    sub = next((x for x in a if not x.startswith("-")), "")
    if base in ("uv", "poetry", "pdm", "conda", "mamba") and sub in ("pip", "run", "tool"):
        rest = a[a.index(sub) + 1:]
        if sub == "run":
            return None                     # `uv run pytest` is the program behind it
        sub = next((x for x in rest if not x.startswith("-")), "")
        a = [sub] + rest[rest.index(sub) + 1:] if sub in rest else [sub]
    if base in ("cargo", "go", "npm", "pnpm", "yarn", "bun") and sub not in PKG_INSTALL and sub != "remove":
        return None                         # build/test/run subcommands are instruments
    if sub in PKG_INSTALL:
        names = [x for x in a[a.index(sub) + 1:] if not x.startswith("-") and not is_pathish(x)] if sub in a else []
        if names and not any(x in ("-r", "--requirement", "-e", ".") for x in a):
            return [action("agent", "install", thing("package", n.split("==")[0].split("@")[0], label=n[:40])) for n in names[:3]]
        if ctx.cwd:
            return [action("agent", "install", thing("repo", ctx.cwd, label=file_label(ctx.cwd)))]
        return []
    if sub in ("remove", "uninstall", "rm", "purge"):
        names = [x for x in a[a.index(sub) + 1:] if not x.startswith("-")] if sub in a else []
        return [action("agent", "delete", thing("package", n, label=n)) for n in names[:3]]
    if sub in ("list", "show", "search", "info", "freeze", "outdated", "ls", "view", "why", "tree"):
        return [action("agent", "probe", thing("host", "packages", label="packages"))]
    return []


def process_actions(ctx, base, args):
    def plain(pat):      # `[s]erver\.py --root` -> `server.py --root`: a name, not a regex
        return re.sub(r"\[(\w)\]", r"\1", pat).replace("\\.", ".").replace(".*", "…").strip("^$")[:40]
    if base in ("kill", "pkill", "killall"):
        tgt = next((x for x in args if not x.startswith("-") and "$" not in x and "{" not in x), None)
        if tgt:
            return [action("agent", "stop", thing("process", plain(tgt), label=f"pid {tgt}" if tgt.isdigit() else plain(tgt)))]
    if base in ("pgrep", "pidof"):
        tgt = next((x for x in args if not x.startswith("-") and "$" not in x and "{" not in x), None)
        if tgt:
            return [action("agent", "probe", thing("process", plain(tgt), label=plain(tgt)))]
    if base in ("wait",):
        tgt = next((x for x in args if x.isdigit()), None)
        if tgt:
            return [action("agent", "probe", thing("process", tgt, label=f"pid {tgt}"))]
    if base in ("systemctl", "service", "launchctl", "sv", "rc-service"):
        a = [x for x in args if not x.startswith("-")]
        if base == "service" and len(a) >= 2:
            a = [a[1], a[0]]
        if len(a) >= 2:
            op, unit = a[0], a[1]
            verb = {"start": "launch", "restart": "launch", "reload": "launch", "stop": "stop",
                    "kill": "stop", "status": "probe", "is-active": "probe", "is-enabled": "probe",
                    "show": "probe", "cat": "read", "enable": "edit", "disable": "edit",
                    "mask": "edit", "unmask": "edit", "edit": "edit", "daemon-reload": "launch"}.get(op, "probe")
            ctx.remember("service", unit)
            return [action("agent", verb, thing("service", unit, label=unit))]
        return [action("agent", "probe", thing("host", "localhost", label="this host"))]
    if base == "journalctl":
        unit = next((args[i + 1] for i, a in enumerate(args) if a in ("-u", "--unit") and i + 1 < len(args)), None)
        return [action("agent", "watch", thing("service", unit, label=unit) if unit else thing("host", "localhost", label="this host"))]
    if base in HOST_PROBES:
        return [action("agent", "probe", thing("host", "localhost", label="this host"))]
    return []


def archive_actions(ctx, base, args):
    if base == "tar":
        mode = next((a for a in args if a.startswith("-") or re.fullmatch(r"[xcztjvfpC]+", a)), "")
        files = [a for a in args if is_pathish(a)]
        arch = next((f for f in files if file_role(f) == "archive"), files[0] if files else None)
        if not arch:
            return []
        t = file_thing(ctx, arch, role="archive")
        if "x" in mode:
            return [action("agent", "extract", t)]
        if "c" in mode:
            return [action("agent", "write", t)]
        return [action("agent", "read", t)]
    if base in ("unzip", "gunzip", "bunzip2", "unxz", "unzstd", "7z", "7za", "unrar", "cpio", "ar"):
        arch = next((a for a in args if is_pathish(a)), None)
        t = file_thing(ctx, arch, role="archive") if arch else None
        return [action("agent", "extract", t)] if t else []
    if base in ("zip", "gzip", "bzip2", "xz", "zstd"):
        arch = next((a for a in args if is_pathish(a)), None)
        t = file_thing(ctx, arch, role="archive") if arch else None
        return [action("agent", "write", t)] if t else []
    return []


def segment_actions(ctx, toks, flags):
    """One pipeline segment -> actions. The heart of the classifier."""
    acts = []
    toks = list(toks)
    # redirections
    k = 0
    while k < len(toks):
        if toks[k] in (">", ">>") and k + 1 < len(toks):
            t = file_thing(ctx, toks[k + 1])
            if t and t["kind"] == "file":
                acts.append(action("agent", "write", t))
            del toks[k:k + 2]; continue
        if toks[k] == "<" and k + 1 < len(toks):
            t = file_thing(ctx, toks[k + 1])
            if t:
                acts.append(action("agent", "read", t))
            del toks[k:k + 2]; continue
        k += 1
    # peel prefixes and assignments
    while toks:
        t0 = toks[0]
        if ASSIGN.match(t0):
            toks = toks[1:]; continue
        if t0 in PEEL:
            toks = toks[1:]
            if t0 in ("sudo", "doas", "env", "command", "builtin"):
                while toks and toks[0].startswith("-"):
                    if t0 == "command" and toks[0] in ("-v", "-V"):
                        return acts                     # `command -v ffmpeg`: a which, not a run
                    if toks[0] in ("-u", "-g", "-C", "-D", "-R", "-T", "-p", "-r", "-h", "--host", "--user", "--group", "--chdir") and len(toks) > 1:
                        toks = toks[2:]
                    else:
                        toks = toks[1:]
            continue
        if t0 in PEEL_ARG:
            toks = toks[1:]
            while toks and (toks[0].startswith("-") or re.fullmatch(r"\d+[smhd]?", toks[0])):
                toks = toks[1:]
            if t0 == "xargs" and toks and toks[0] in ("-I", "-n", "-P"):
                toks = toks[2:]
            continue
        break
    if not toks:
        return acts
    base, args = toks[0], toks[1:]
    bbase = os.path.basename(base)
    if base in ("[", "[[", "test"):
        for a in args:
            if is_pathish(a) and not a.startswith("-"):
                t = file_thing(ctx, a)
                if t:
                    acts.append(action("agent", "probe", t)); break
        return acts
    if len(args) == 1 and args[0] in ("--version", "--help", "-V", "-version", "-help"):
        return acts
    if base == "git" or bbase == "git":
        return acts + git_actions(ctx, args)
    if base == "gh":
        return acts + gh_actions(ctx, args)
    if bbase.startswith("qvm-") or bbase.startswith("qubes-") or bbase in ("docker", "podman", "nerdctl", "virsh", "vagrant", "multipass", "limactl", "colima", "minikube", "kind", "kubectl", "oc", "helm") or bbase.lower() == "vboxmanage":
        return acts + vm_actions(ctx, bbase, args, flags)
    if bbase in ("ssh", "scp", "rsync", "sftp", "ping", "ping6", "traceroute", "mtr", "dig", "nslookup", "nc", "ncat", "telnet", "nmap"):
        r = ssh_actions(ctx, bbase, args, flags)
        if r:
            return acts + r
    if bbase in NET:
        return acts + net_actions(ctx, bbase, args)
    if bbase in PKG_MANAGERS:
        r = package_actions(ctx, bbase, args)
        if r is not None:
            return acts + r
        # fall through: `cargo test`, `npm run build`, `go build` are instruments
        sub = next((x for x in args if not x.startswith("-")), "")
        return acts + instrument_actions(ctx, thing("prog", f"{bbase} {sub}".strip(), label=f"{bbase} {sub}".strip()), f"{bbase} {sub}", args[1:] if sub else args, flags)
    if bbase in ("kill", "pkill", "killall", "pgrep", "pidof", "wait", "systemctl", "service", "journalctl", "launchctl") or bbase in HOST_PROBES:
        return acts + process_actions(ctx, bbase, args)
    if bbase in ("tar", "unzip", "zip", "gunzip", "gzip", "bunzip2", "bzip2", "unxz", "xz", "zstd", "unzstd", "7z", "7za", "unrar", "cpio"):
        return acts + archive_actions(ctx, bbase, args)
    if bbase in AGENTS:
        return acts + [action("agent", "spawn", thing("agent", bbase, label=bbase + " cli"))]
    if bbase in INTERP:
        return acts + interp_actions(ctx, bbase, args, flags)
    if bbase in WRITERS or (bbase == "sed" and any(a.startswith("-i") for a in args)):
        verb = WRITERS.get(bbase, "edit")
        if bbase == "sed":
            args = [a for a in args if not a.startswith("-i")]
            if not any(a in ("-e", "-f", "--expression") or a.startswith("-e") for a in args):
                first = next((i for i, a in enumerate(args) if not a.startswith("-")), None)
                if first is not None:
                    args = args[:first] + args[first + 1:]          # the script is not a file
        recursive = any(a.startswith("-") and not a.startswith("--") and ("r" in a[1:] or "R" in a[1:]) for a in args)
        paths = [a for a in args if is_pathish(a, bare=recursive) and not a.startswith("-")]
        if bbase in ("cp", "mv", "ln", "install") and len(paths) >= 2:
            for p in paths[:-1]:
                t = file_thing(ctx, p)
                if t:
                    acts.append(action("agent", "read" if bbase != "mv" else "delete", t))
            t = file_thing(ctx, paths[-1])
            if t:
                acts.append(action("agent", "write", t))
            return acts
        for p in paths[:4]:
            t = file_thing(ctx, p, prefer_dir=recursive)
            if t:
                acts.append(action("agent", verb, t))
        return acts
    if bbase in SEARCHERS and bbase not in ("ls", "tree"):
        recursive = bbase in ("find", "fd", "fdfind", "rg", "ag", "ack") or any(
            a.startswith("-") and not a.startswith("--") and "r" in a[1:] for a in args) or "--recursive" in args
        paths = [a for a in args if is_pathish(a, bare=recursive) and not a.startswith("-")]
        if bbase in ("grep", "egrep", "fgrep", "rg", "ag", "ack") and paths and not any(a in ("-e", "--regexp", "-f") for a in args):
            first = next((i for i, a in enumerate(args) if not a.startswith("-")), None)
            if first is not None and args[first] in paths and not os.path.splitext(args[first])[1] and "/" not in args[first]:
                paths = [p for p in paths if p != args[first]]        # the pattern, not a path
        if bbase in ("find", "fd", "fdfind"):
            paths = []
            for a in args:
                if a.startswith("-"):
                    break
                if is_pathish(a, bare=True):
                    paths.append(a)
        if not paths and bbase in ("find", "fd", "fdfind", "rg", "ag") and ctx.cwd:
            paths = ["."]
        for p in paths[:3]:
            t = file_thing(ctx, p, prefer_dir=recursive)
            if t:
                verb = "watch" if t["kind"] == "file" and t["attrs"].get("role") == "log" else "search"
                acts.append(action("agent", verb, t))
        return acts
    if bbase in READERS or bbase in ("ls", "tree", "stat", "file"):
        if bbase in ("sed", "awk", "gawk", "mawk") and not any(a in ("-e", "-f", "--expression") or a.startswith("-e") for a in args):
            first = next((i for i, a in enumerate(args) if not a.startswith("-")), None)
            if first is not None:
                args = args[:first] + args[first + 1:]          # the script is not a file
        paths = [a for a in args if is_pathish(a, bare=bbase in ("ls", "tree", "du")) and not a.startswith("-")]
        for p in paths[:4]:
            t = file_thing(ctx, p, prefer_dir=bbase in ("ls", "tree", "du"))
            if not t:
                continue
            role = t["attrs"].get("role")
            verb = "read"
            if bbase in ("tail",) and any(a in ("-f", "-F") or a.startswith("-f") for a in args):
                verb = "watch"
            elif role == "log" and bbase in ("tail", "cat", "head", "stat", "ls", "wc", "grep"):
                verb = "watch"
            elif t["kind"] == "process":
                verb = "watch" if t["label"].startswith("task") else "probe"
            elif bbase in ("stat", "ls", "file", "du") and t["kind"] == "file":
                verb = "probe"
            acts.append(action("agent", verb, t))
        return acts
    if bbase in PLUMBING:
        return acts
    if bbase == "ffmpeg":
        ins = [args[i + 1] for i, a in enumerate(args) if a == "-i" and i + 1 < len(args)]
        out = args[-1] if args and is_pathish(args[-1]) else None
        for p in ins[:2]:
            t = file_thing(ctx, p)
            if t:
                acts.append(action("agent", "read", t))
        if out:
            t = file_thing(ctx, out)
            if t:
                acts.append(action("agent", "write", t))
        return acts
    # an instrument: a script by path, or a known/unknown program
    if "/" in base or base.startswith("."):
        p = ctx.resolve(base)
        if not p:
            return acts
        rel = os.path.relpath(p, ctx.cwd) if ctx.cwd and p.startswith(ctx.cwd + os.sep) else None
        inst = thing("file", p, label=file_label(p), role="tool")
        return acts + instrument_actions(ctx, inst, file_label(p), args, flags, rel)
    if bbase in EVALUATORS or bbase in BUILDERS or bbase in ("make",):
        sub = next((x for x in args if not x.startswith("-")), "")
        name = bbase if bbase not in ("make", "cargo", "go", "npm") else f"{bbase} {sub}".strip()
        inst = thing("prog", bbase, label=bbase)
        verb = "evaluate" if bbase in EVALUATORS else "build"
        if bbase == "make" and sub in ("test", "check", "lint", "selftest"):
            verb = "evaluate"
        if bbase == "make" and sub in ("clean", "distclean"):
            verb = "delete"
        if bbase == "make" and sub in ("install",):
            verb = "install"
        r = instrument_actions(ctx, inst, name, args, flags)
        for a in r:
            if a["src"] != "agent" or len(r) == 1:
                a["verb"] = verb if a["verb"] in ("run", "launch") or a["src"] != "agent" else a["verb"]
        if len(r) == 1 and ctx.cwd:
            r.append(action(inst, verb, thing("repo", ctx.cwd, label=file_label(ctx.cwd))))
        return acts + r
    if re.fullmatch(r"[\w.+-]{2,40}", bbase):
        inst = thing("prog", bbase, label=bbase)
        return acts + instrument_actions(ctx, inst, bbase, args, flags)
    return acts


def interp_actions(ctx, base, args, flags):
    """python3 x.py / python3 -c code / bash -c cmd / node script.js."""
    a = list(args)
    lang = "python" if base.startswith("py") else ("js" if base in ("node", "nodejs", "deno", "bun", "ts-node", "tsx") else base)
    code = None
    i = 0
    shellish = base in ("bash", "sh", "zsh", "dash", "ksh", "fish")
    while i < len(a):
        t = a[i]
        if t in ("-c", "-e", "--eval", "-Command", "-command", "-p", "--print") and i + 1 < len(a) and not (t == "-p" and not lang == "js"):
            code = a[i + 1]; break
        if shellish and re.fullmatch(r"-[a-z]*c[a-z]*", t) and i + 1 < len(a):     # bash -lc 'cmd'
            code = a[i + 1]; break
        if t in ("-W", "-X", "-Q", "-r", "--require", "--import", "--loader", "-I", "--inspect-port") and i + 1 < len(a) and not (t == "-I" and lang == "python"):
            i += 2; continue
        if t == "-m" and i + 1 < len(a):
            mod = a[i + 1]
            if mod in ("http.server", "SimpleHTTPServer"):
                port = next((x for x in a[i + 2:] if x.isdigit()), "8000")
                return [action("agent", "launch", thing("service", f"localhost:{port}", label=f"localhost:{port}"))]
            if mod in ("json.tool", "pprint"):
                return [action("agent", "read", t2) for t2 in pathlike_targets(ctx, a[i + 2:], 2)]
            name = mod.split(".")[-1]
            return instrument_actions(ctx, thing("prog", name, label=name), name, a[i + 2:], flags)
        if t == "-" or t == "--":
            code = flags.get("heredoc") or ""; break
        if t.startswith("-"):
            i += 1; continue
        if t == "run" and base in ("deno", "bun") and i + 1 < len(a):
            i += 1; continue
        # a script
        p = ctx.resolve(t)
        if not p:
            return []
        rel = os.path.relpath(p, ctx.cwd) if ctx.cwd and p.startswith(ctx.cwd + os.sep) else None
        inst = thing("file", p, label=file_label(p), role="tool")
        return instrument_actions(ctx, inst, file_label(p), a[i + 1:], flags, rel)
    if code is None and flags.get("heredoc") is not None:
        code = flags["heredoc"]
    if code is None:
        return []
    if base in ("bash", "sh", "zsh", "dash", "ksh", "fish"):
        return bash_actions(code, ctx)
    return code_actions(ctx, code, lang)


def bash_actions(command, ctx):
    """Every action a Bash tool call performed, de-duplicated, in order."""
    acts = []
    if not command or not command.strip():
        return acts
    home = ctx.cwd
    for toks, flags in split_shell(command):
        if flags.get("funcdef") or (toks and toks[0] == "function"):
            continue
        if toks and toks[0] in ("cd", "pushd"):              # the rest of the line lives there
            tgt = next((t for t in toks[1:] if not t.startswith("-")), None)
            if tgt in (None, "~", "-"):
                ctx.cwd = home if tgt != "-" else ctx.cwd
            elif "$" not in tgt:
                p = ctx.resolve(tgt)
                if p:
                    ctx.cwd = p
            continue
        if toks and toks[0] == "popd":
            ctx.cwd = home
            continue
        heredoc = flags.get("heredoc")
        if heredoc is not None and toks and toks[0] in ("cat", "tee") and not any(t in (">", ">>") for t in toks):
            # `cat <<EOF` with no target: a message, not a file
            if toks[0] == "tee":
                for t in pathlike_targets(ctx, toks[1:], 2):
                    acts.append(action("agent", "write", t))
            continue
        try:
            acts += segment_actions(ctx, toks, flags)
        except Exception:      # a heuristic must never take the parser down
            continue
    ctx.cwd = home
    # de-duplicate (same src, verb, dst), keep order
    seen, out = set(), []
    for a in acts:
        if not a.get("dst"):
            continue
        key = (json.dumps(a["src"], sort_keys=True) if isinstance(a["src"], dict) else a["src"],
               a["verb"], a["dst"]["kind"], a["dst"]["key"])
        if key in seen:
            continue
        seen.add(key); out.append(a)
    return out[:12]


# ----------------------------------------------------------- other tools --
def tool_actions(tool, inp, ctx):
    """Non-Bash tools are already one verb on one thing."""
    inp = inp if isinstance(inp, dict) else {}
    if tool == "Bash":
        return bash_actions(inp.get("command") or "", ctx)
    if tool in ("Read", "NotebookRead"):
        t = file_thing(ctx, inp.get("file_path") or inp.get("notebook_path"))
        return [action("agent", "read", t)] if t else []
    if tool == "Write":
        t = file_thing(ctx, inp.get("file_path"))
        return [action("agent", "write", t)] if t else []
    if tool in ("Edit", "NotebookEdit", "MultiEdit"):
        t = file_thing(ctx, inp.get("file_path") or inp.get("notebook_path"))
        return [action("agent", "edit", t)] if t else []
    if tool in ("Grep", "Glob"):
        p = inp.get("path") or ctx.cwd
        if not p:
            return []
        t = file_thing(ctx, p) if os.path.splitext(p)[1] and not p.endswith("/") else dir_thing(ctx, p)
        return [action("agent", "search", t, pattern=str(inp.get("pattern") or "")[:80])] if t else []
    if tool == "WebSearch":
        q = (inp.get("query") or "").strip()
        return [action("agent", "search", thing("search", q[:160], label=q[:60]))] if q else []
    if tool == "WebFetch":
        t = url_thing(inp.get("url") or "")
        return [action("agent", "fetch", t)] if t else []
    if tool in ("Agent", "Task"):
        a = inp.get("subagent_type") or "general-purpose"
        return [action("agent", "spawn", thing("agent", a, label=a))]
    if tool == "Workflow":
        return [action("agent", "spawn", thing("agent", "workflow", label="workflow"))]
    if tool == "Skill":
        s = inp.get("skill") or "?"
        return [action("agent", "skill", thing("skill", s, label=s))]
    if tool == "Artifact":
        act = inp.get("action") or "publish"
        return [action("agent", "push" if act == "publish" else "read", thing("service", "claude.ai/artifacts", label="artifacts"))]
    if tool == "SendUserFile":
        return [action("agent", "push", file_thing(ctx, f)) for f in (inp.get("files") or [])[:3] if file_thing(ctx, f)]
    if tool and tool.startswith("mcp__"):
        parts = tool.split("__")
        server = parts[1] if len(parts) > 1 else tool
        op = parts[2] if len(parts) > 2 else ""
        verb = "read" if re.match(r"(list|get|read|search|describe|fetch|query|show)", op) else "call"
        return [action("agent", verb, thing("service", server, label=server), op=op)]
    return []


# ---------------------------------------------------------------- outcomes --
FAIL_RE = re.compile(r"(?m)^(?:FAIL|FAILED|ERROR|E |Traceback|.*: command not found|.*No such file or directory|"
                     r"SELFTEST FAILED|.*\b(\d+) failed\b.*|rc=[1-9]\d*|exit (?:code|status) [1-9]\d*|"
                     r"✗|❌|panicked at|AssertionError|Segmentation fault|FATAL)")
PASS_RE = re.compile(r"(?m)^(?:PASS|PASSED|ok\b|OK\b|SELFTEST CLEAN|.*\b\d+ passed\b.*|rc=0|✓|✅|All checks passed|"
                     r".*\b0 failed\b.*|Build succeeded|BUILD SUCCESS|Finished `?(?:dev|release)|.*: running)")


def outcome_of(blob, is_error=False):
    """ok / fail / None from a tool result. `N passed, 0 failed` is ok; a
    non-zero rc, a FAIL line or a traceback is a fail; silence is neither."""
    if is_error:
        return "fail"
    if not blob:
        return None
    head = blob[:20000]
    signals = []                     # (position, verdict): the last word wins
    for m in re.finditer(r"\b(\d+) passed, (\d+) failed\b", head):
        signals.append((m.start(), "fail" if int(m.group(2)) else "ok"))
    for m in re.finditer(r"(?m)^(?:rc|exit(?: code| status)?)=(\d+)\s*$", head):
        signals.append((m.start(), "fail" if int(m.group(1)) else "ok"))
    for m in re.finditer(r"(?m)^(?:SELFTEST FAILED|FAIL(?:ED)?\b(?!\w)|Traceback \(most recent call last\)|.*: command not found\s*$|"
                         r"AssertionError\b|thread '.*' panicked at|FATAL:|ERROR:)", head):
        signals.append((m.start(), "fail"))
    for m in re.finditer(r"(?m)^(?:SELFTEST CLEAN|PASS(?:ED)?\b(?!\w)|ok\s*$|ok\s+[^:=]|OK\s*$|All checks passed|BUILD SUCCESS|Build succeeded)", head):
        signals.append((m.start(), "ok"))
    if not signals:
        return None
    return max(signals)[1]
