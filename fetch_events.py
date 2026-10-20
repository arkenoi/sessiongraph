#!/usr/bin/env python3
"""Fetch a Claude Code remote session's events with nothing but Python.

No Claude Code install, no MCP, no model turn. Talks to the sessions REST API
directly and drops pages where `server.py --watch-dir` is looking:

    export CLAUDE_SESSIONS_TOKEN=...            # or --token-file PATH
    python3 fetch_events.py session_01ABC... --out ~/.sessiongraph/dumps --every 60

The credential is the claude.ai login token Claude Code itself holds, scope
user:sessions:claude_code. Only that works on these routes — the CLI's own code
refuses API keys and says `claude setup-token` tokens are "limited to inference
only", and that has been checked against the bundle, not assumed.

  One-time, on any machine that can run it:   claude login
  Then the token is in:   ~/.claude/.credentials.json -> claudeAiOauth.accessToken
                          (macOS: Keychain item "Claude Code-credentials")

  python3 fetch_events.py --token-file ~/.claude/.credentials.json ...   # reads that key
  export CLAUDE_SESSIONS_TOKEN=sk-ant-oat...                            # or paste it

After that this script needs no Claude Code. The token expires (the CLI
refreshes it silently; a copied one does not), so when you start seeing 401,
copy it again. A `sk-ant-sid` browser cookie is accepted on the off chance it
works, but nothing in the bundle sends it on these routes: unverified.

The HUD de-duplicates by record uuid, so re-fetching the newest page each poll
is harmless; `--all` walks the whole history once first.

This script never prints the token. It reads it from the environment or a file
you name, sends it, and forgets it.
"""
import argparse, json, os, sys, time, urllib.error, urllib.parse, urllib.request

BASE = os.environ.get("CLAUDE_SESSIONS_BASE", "https://api.anthropic.com")
# On the wire, as the Claude Code client's own header builders do it:
#   Authorization: Bearer <login token>, anthropic-version: 2023-06-01,
#   Content-Type: application/json (even on GET). No beta header on these routes
#   (oauth-2025-04-20 is attached to inference calls only). x-organization-uuid
#   is optional here and required only on /teleport-events.
DEFAULT_VERSION = "2023-06-01"
MAX_LIMIT = 100                                   # 500 is rejected with 400


def load_token(a):
    if a.token_file:
        with open(os.path.expanduser(a.token_file)) as fh:
            raw = fh.read().strip()
        if raw[:1] == "{":                         # ~/.claude/.credentials.json
            try:
                return json.loads(raw)["claudeAiOauth"]["accessToken"]
            except Exception:
                sys.exit("token file is JSON but has no claudeAiOauth.accessToken")
        return raw
    tok = os.environ.get("CLAUDE_SESSIONS_TOKEN") or os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
    if not tok:
        sys.exit("no token: set CLAUDE_SESSIONS_TOKEN (or CLAUDE_CODE_OAUTH_TOKEN) or pass --token-file")
    return tok


def request(a, token, path, params=None):
    url = BASE.rstrip("/") + path
    if params:
        url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None}, doseq=True)
    headers = {"accept": "application/json", "anthropic-version": a.version,
               "content-type": "application/json", "user-agent": "sessiongraph-fetch/1"}
    org = a.org or os.environ.get("CLAUDE_CODE_ORGANIZATION_UUID") or os.environ.get("CLAUDE_ORG_UUID")
    if token.startswith("sk-ant-sid"):            # browser cookie: unverified on this host
        headers["Cookie"] = "sessionKey=" + token
    elif a.auth_header.lower() == "x-api-key":
        headers["x-api-key"] = token
    else:
        headers["Authorization"] = "Bearer " + token
    if org:
        headers["x-organization-uuid"] = org
    if a.beta:
        headers["anthropic-beta"] = ",".join(a.beta)
    req = urllib.request.Request(url, headers=headers)
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.status, json.loads(r.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            if e.code == 429 and attempt < 4:
                wait = float(e.headers.get("Retry-After") or 2 ** attempt)
                print(f"429, waiting {wait:.0f}s", file=sys.stderr)
                time.sleep(wait)
                continue
            try:
                msg = json.loads(body).get("error", {}).get("message")
            except Exception:
                msg = body[:200]
            return e.code, {"error": msg}
        except urllib.error.URLError as e:
            if attempt < 4:
                time.sleep(2 ** attempt)
                continue
            return 0, {"error": str(e)}
    return 0, {"error": "gave up"}


def id_forms(session):
    """The CLI rewrites session_<x> to cse_<x> for paths; the API may take either."""
    if session.startswith("session_"):
        return [session, "cse_" + session[len("session_"):]]
    if session.startswith("cse_"):
        return [session, "session_" + session[len("cse_"):]]
    return [session]


def get_either(a, token, path_fmt, params=None):
    """Try the id as given, and on 404 the other prefix form."""
    last = (404, {"error": "not found"})
    for sid in id_forms(a.session):
        code, doc = request(a, token, path_fmt.format(sid=sid), params)
        if code != 404:
            if sid != a.session:
                print(f"note: the API wanted the id as {sid}", file=sys.stderr)
                a.session = sid
            return code, doc
        last = (code, doc)
    return last


def events_page(a, token, session, cursor=None, asc=False):
    params = {"limit": min(a.limit, MAX_LIMIT)}
    if asc:
        params["sort_order"] = "asc"
    if cursor:
        params["cursor"] = cursor
    return get_either(a, token, "/v1/code/sessions/{sid}/events", params)


def next_cursor(doc, items):
    """The route's continuation is `next_cursor`; null or absent means last page."""
    if not isinstance(doc, dict):
        return None
    return doc.get("next_cursor") or None


def items_of(doc):
    if isinstance(doc, dict):
        return doc.get("data") or doc.get("events") or []
    return doc if isinstance(doc, list) else []


def write_page(a, doc, tag):
    os.makedirs(a.out, exist_ok=True)
    name = os.path.join(a.out, f"{a.session[:16]}-{tag}.json")
    with open(name, "w") as fh:
        json.dump(doc, fh)
    return name


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("session", nargs="?", help="session id, session_01...  (omit with --list)")
    ap.add_argument("--list", action="store_true", help="list your sessions (id, title, status) and exit")
    ap.add_argument("--out", default=os.path.expanduser("~/.sessiongraph/dumps"))
    ap.add_argument("--every", type=float, default=0, help="poll interval in seconds (0 = once)")
    ap.add_argument("--all", action="store_true", help="walk the full history first, paging backwards")
    ap.add_argument("--limit", type=int, default=100)

    ap.add_argument("--token-file")
    ap.add_argument("--auth-header", default="Authorization", help="Authorization (default) or x-api-key")
    ap.add_argument("--org", help="organization uuid (optional on these routes)")
    ap.add_argument("--beta", nargs="*", default=[], help="extra anthropic-beta values")
    ap.add_argument("--version", default=DEFAULT_VERSION)
    ap.add_argument("--probe", action="store_true", help="just GET the session and report the status")
    a = ap.parse_args()
    token = load_token(a)

    if a.list:
        cursor, shown = None, 0
        while True:
            code, doc = request(a, token, "/v1/code/sessions", {"limit": 50, "cursor": cursor})
            if code != 200:
                sys.exit(f"HTTP {code}: {doc.get('error')}")
            items = items_of(doc)
            for sdoc in items:
                if not isinstance(sdoc, dict):
                    continue
                status = str(sdoc.get('status') or sdoc.get('session_status') or '').replace('SESSION_STATUS_', '')
                when = sdoc.get('last_event_at') or sdoc.get('updated_at') or sdoc.get('created_at') or ''
                print(f"{sdoc.get('id','?'):<34} {status:<9} {str(when)[:16]:<17} "
                      f"{str(sdoc.get('environment_kind') or ''):<16} {str(sdoc.get('title',''))[:56]}")
                shown += 1
            nxt = next_cursor(doc, items)
            if not nxt or nxt == cursor or shown >= 200:
                break
            cursor = nxt
        sys.exit(0)
    if not a.session:
        ap.error("a session id is required unless --list is given")

    if a.probe:
        code, doc = get_either(a, token, "/v1/code/sessions/{sid}")
        print(code, (doc.get("error") if isinstance(doc, dict) and "error" in doc
                     else {k: doc[k] for k in list(doc)[:6]} if isinstance(doc, dict) else type(doc).__name__))
        sys.exit(0 if code == 200 else 1)

    if a.all:
        cursor, n, seen = None, 0, set()
        while True:
            code, doc = events_page(a, token, a.session, cursor=cursor)
            if code != 200:
                sys.exit(f"HTTP {code}: {doc.get('error')}")
            items = items_of(doc)
            if not items:
                break
            n += 1
            if n == 1 and isinstance(doc, dict):
                print("response keys:", list(doc.keys())[:8], file=sys.stderr)
            write_page(a, doc, f"hist{n:04d}")
            print(f"page {n}: {len(items)} events", file=sys.stderr)
            nxt = next_cursor(doc, items)
            if not nxt or nxt in seen or (isinstance(doc, dict) and doc.get("has_more") is False):
                break
            seen.add(nxt)
            cursor = nxt

    while True:
        code, doc = events_page(a, token, a.session)
        if code != 200:
            print(f"HTTP {code}: {doc.get('error')}", file=sys.stderr)
            if code == 401:
                sys.exit("401: the login token is missing, expired or revoked — copy a fresh "
                         "claudeAiOauth.accessToken from ~/.claude/.credentials.json")
            if code == 403:
                sys.exit("403: wrong kind of token for this route — it must be the claude.ai login "
                         "token (user:sessions:claude_code), not an API key or setup-token")
        else:
            f = write_page(a, doc, time.strftime("%Y%m%d-%H%M%S"))
            print(f"{time.strftime('%H:%M:%S')}  {len(items_of(doc))} events -> {os.path.basename(f)}", file=sys.stderr)
        if not a.every:
            break
        time.sleep(a.every)


if __name__ == "__main__":
    main()
