#!/usr/bin/env python3
"""Does a shell line become the right actions? Run: python3 tests/test_abstractions.py"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import abstractions as A

CWD = "/home/u/proj"


def acts(cmd, vocab=None, known=None):
    ctx = A.Context(cwd=CWD, vocab=vocab, known=known)
    out = []
    for a in A.bash_actions(cmd, ctx):
        src = a["src"] if isinstance(a["src"], str) else a["src"]["label"]
        out.append((src, a["verb"], a["dst"]["kind"], a["dst"]["label"]))
    return out


def check(name, got, *want):
    missing = [w for w in want if w not in got]
    print(("ok   " if not missing else "FAIL ") + name)
    if missing:
        print("   wanted", missing); print("   got   ", got)
    return not missing


ok = True
ok &= check("git commit is a verb on the repo",
            acts("git add a.py && git commit -q -m 'x' && git push origin main"),
            ("agent", "commit", "repo", "proj"), ("agent", "push", "repo", "proj"))
ok &= check("no command node for git", [a for a in acts("git status --porcelain | cut -c1-70") if a[2] == "prog"])
ok &= check("tail of a log is a watch", acts("echo now; tail -3 /var/run/campaign/matrix.log | cut -c1-120"),
            ("agent", "watch", "file", "matrix.log"))
ok &= check("qvm-check is a probe of a VM", acts("qvm-check --running win10-acc 2>&1"),
            ("agent", "probe", "vm", "win10-acc"))
ok &= check("an instrument captures a VM it is told about by env",
            acts('S=/tmp/s; rm -f "$S/p.tar"; QTEST_VM=win10-acc ./tools/qtest shot "$S/p.tar" > "$S/o" 2>&1; echo rc=$?'),
            ("agent", "run", "file", "qtest"), ("qtest", "capture", "vm", "win10-acc"), ("qtest", "capture", "file", "p.tar"))
ok &= check("a known VM name is a VM anywhere later",
            acts("./tools/qtest shot win10-acc out.tar", known={"vm": {"win10-acc"}, "host": set(), "service": set()}),
            ("qtest", "capture", "vm", "win10-acc"))
ok &= check("python write_text is an edit", acts("python3 - <<'PY'\nimport pathlib\np=pathlib.Path('findings/issues.md')\nt=p.read_text()\np.write_text(t.replace('a','b'))\nPY"),
            ("agent", "edit", "file", "issues.md"))
ok &= check("python read only is a read", acts("python3 -c \"print(open('mgmt/x.py').read()[:10])\""),
            ("agent", "read", "file", "x.py"))
ok &= check("heredoc then a command on the next line",
            acts("cat > /tmp/s/dbg.py <<'EOF'\nprint(1)\nEOF\npython3 /tmp/s/dbg.py 2>&1 | tail -8"),
            ("agent", "write", "file", "dbg.py"), ("agent", "run", "file", "dbg.py"))
ok &= check("--selftest evaluates the harness itself", acts("python3 mgmt/harness/grade.py --selftest 2>&1 | tail -1"),
            ("agent", "evaluate", "file", "grade.py"))
ok &= check("a test script under tools/tests evaluates", acts("bash tools/tests/clock-selftest.sh"),
            ("agent", "evaluate", "file", "clock-selftest.sh"))
ok &= check("vocab: jev evaluates against an api",
            acts("tools/jev-artifact.sh rubric.json notes.txt -- 'git diff'",
                 vocab={"instruments": {"jev-*": {"verb": "evaluate", "against": "api:claude"}}, "entities": {}, "_compiled": {}}),
            ("jev-artifact.sh", "evaluate", "file", "notes.txt"), ("jev-artifact.sh", "against", "api", "claude"))
ok &= check("curl GET is a fetch of an api", acts("curl -sS https://api.github.com/repos/a/b -H 'x: y'"),
            ("agent", "fetch", "api", "api.github.com/repos/a"))
ok &= check("curl POST is a call", acts("curl -X POST -d '{}' http://127.0.0.1:8777/ingest"),
            ("agent", "call", "service", "localhost:8777"))
ok &= check("a server in the background is a launch of its port",
            acts("nohup python3 server.py --port 8782 > /tmp/s.log 2>&1 &"),
            ("agent", "launch", "file", "server.py"), ("server.py", "launch", "service", "localhost:8782"))
ok &= check("sed script is not a file", [a for a in acts("sed -n '/^HEREDOC/,/^def x/p' a.py") if a[3] != "a.py"])
ok &= check("function definitions are not calls",
            [a for a in acts("probe(){ curl -s \"$1\"; }\nprobe https://api.x.com/v1/y") if a[3] == "probe" and a[1] != "run"])
ok &= check("a /proc pid is a process probe", acts("cat /proc/2648758/cmdline | tr '\\0' ' '"),
            ("agent", "probe", "process", "pid 2648758"))
ok &= check("pip install is an install of a package", acts("pip install requests==2.0 rich"),
            ("agent", "install", "package", "requests==2.0"))
ok &= check("tar x is an extract", acts("tar xf shots/p5.tar -C /tmp/cap5 && find /tmp/cap5 -name '*.png'"),
            ("agent", "extract", "file", "p5.tar"), ("agent", "search", "dir", "cap5"))
ok &= check("pytest with no args evaluates the repo", acts("pytest -q 2>&1 | tail -3"),
            ("pytest", "evaluate", "repo", "proj"))
ok &= check("docker run launches a container", acts("docker run -d --name web nginx:1"),
            ("agent", "launch", "vm", "web"))
ok &= check("ssh with a command runs on the host", acts("ssh -p 22 me@rig 'ls /tmp'"),
            ("agent", "run", "host", "rig"))
ok &= check("plumbing alone is nothing", acts("echo \"now: $(date -u)\"; sleep 2; cd /tmp; export X=1"))
ok &= check("outcome: rc=1 fails", [A.outcome_of("citation corrected\npre-commit REFUSED\nrc=1\n")], "fail")
ok &= check("outcome: passed counts", [A.outcome_of("x: 13 passed, 0 failed")], "ok")
ok &= check("outcome: silence is neither", [A.outcome_of("hello\nworld")], None)
ok &= check("cd inside the line moves relative paths", acts("cd /opt/other && python3 run.py --out o.txt"),
            ("agent", "run", "file", "run.py"))
ok &= check("cd keys the script under the other tree",
            [a for a in A.bash_actions("cd /opt/other && python3 run.py", A.Context(cwd=CWD)) if a["dst"]["key"] == "/opt/other/run.py"],
            *[a for a in A.bash_actions("cd /opt/other && python3 run.py", A.Context(cwd=CWD)) if a["dst"]["key"] == "/opt/other/run.py"][:1])
ok &= check("-v is verbose, not --version", acts("pytest -v tests/"), ("pytest", "evaluate", "dir", "tests"))
ok &= check("flags do not name verbs: a watcher launch keeps its port",
            acts("python3 server.py --watch-dir /tmp/pump --port 8782 &"),
            ("agent", "launch", "file", "server.py"), ("server.py", "launch", "service", "localhost:8782"))
ok &= check("a no-target script is just run, whatever its name", acts("./clean.sh"), ("agent", "run", "file", "clean.sh"))
ok &= check("sed -i script is not a file", [a for a in acts("sed -i 's/foo/bar/g' x.py") if a[3] != "x.py"])
ok &= check("sed -i edits the file", acts("sed -i 's/foo/bar/g' x.py"), ("agent", "edit", "file", "x.py"))
ok &= check("sudo -u peels its value", acts("sudo -u postgres psql -c 'select 1'"), ("agent", "run", "prog", "psql"))
ok &= check("docker -v value is not the container", acts("docker run -v $PWD:/w -p 8080:80 --name web nginx"),
            ("agent", "launch", "vm", "web"))
ok &= check("docker flag value never becomes a VM", [a for a in acts("docker run -v /x:/w -e A=1 nginx:1") if a[3] in ("/x:/w", "A=1")])
ok &= check("gh --limit is not a PR number", acts("gh pr list --limit 5"), ("agent", "read", "repo", "proj"))
ok &= check("grep -r over . searches the repo", acts("grep -rn 'foo' ."), ("agent", "search", "repo", "proj"))
ok &= check("grep pattern word is not a path", [a for a in acts("grep -rn needle src") if a[3] == "needle"])
ok &= check("bash -lc runs the inner command", acts("bash -lc 'git status'"), ("agent", "read", "repo", "proj"))
ok &= check("python route literals are not files", [a for a in acts("python3 - <<'PY'\ns=open('server.py').read().replace('/select','/pick')\nopen('server.py','w').write(s)\nPY") if a[3] in ("select", "pick")])
ok &= check("here-string is not a heredoc", acts("grep -c x <<< 'x y' ; cat a.py"), ("agent", "read", "file", "a.py"))
ok &= check("outcome: the last signal wins", [A.outcome_of("3 passed, 0 failed\nlater\nrc=1")], "fail")
ok &= check("outcome: 'ok: false' is not ok", [A.outcome_of("ok: false")], None)
ok &= check("outcome: a mention of AssertionError mid-line is not a failure", [A.outcome_of("grep found AssertionError in 3 files")], None)
print("ALL OK" if ok else "FAILURES")
sys.exit(0 if ok else 1)
