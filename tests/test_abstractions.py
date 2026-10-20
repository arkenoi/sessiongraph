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
ok &= check("qvm-check is a probe of a VM, through dom0", acts("qvm-check --running win10-acc 2>&1"),
            ("agent", "call", "host", "dom0"), ("dom0", "probe", "vm", "win10-acc"))
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
ok &= check("a loop body runs once per item", acts('for v in build-vm test-vm; do qvm-check --running "$v"; done'),
            ("dom0", "probe", "vm", "build-vm"), ("dom0", "probe", "vm", "test-vm"))
ok &= check("qvm-start is dom0 launching the qube", acts("qvm-start build-vm"),
            ("agent", "call", "host", "dom0"), ("dom0", "launch", "vm", "build-vm"))
ok &= check("qvm-run in dom0 is a run of dom0", acts("qvm-run --pass-io dom0 true"), ("agent", "run", "host", "dom0"))
ok &= check("a dom0 screenshot service captures the qube it names",
            acts('qrexec-client-vm dom0 local.Screenshot+build-vm </dev/null > "$T"'),
            ("agent", "call", "host", "dom0"), ("dom0", "capture", "vm", "build-vm"))
ok &= check("a whole-desktop screenshot captures dom0", acts("qrexec-client-vm dom0 local.FullScreen > d.tar"),
            ("agent", "capture", "host", "dom0"))
ok &= check("a qrexec service in a qube goes through dom0", acts("qrexec-client-vm test-vm qubes.VMShell"),
            ("agent", "call", "host", "dom0"), ("dom0", "run", "vm", "test-vm"))
ok &= check("admin API names its verb", acts("qrexec-client-vm build-vm admin.vm.Start"),
            ("dom0", "launch", "vm", "build-vm"))
ok &= check("qrexec autostarts its target", A.vm_state_commands("qvm-run -p build-vm 'echo hi'; qvm-shutdown --wait test-vm"),
            ("build-vm", "running", "qvm-run"), ("test-vm", "halted", "qvm-shutdown"))
ok &= check("--no-autostart starts nothing", [A.vm_state_commands("qvm-run --no-autostart build-vm true")], [])
is_vm = lambda n: n in ("build-vm", "test-vm")
ok &= check("printed VM states, last word wins",
            A.vm_state_text("build-vm=halted test-vm=UP\n12:00:01 booting build-vm with the ISO\ntest-vm   Halted  AppVM", is_vm),
            ("build-vm", "halted", "build-vm=halted test-vm=UP"), ("test-vm", "running", "build-vm=halted test-vm=UP"),
            ("build-vm", "running", "12:00:01 booting build-vm with the ISO"), ("test-vm", "halted", "test-vm   Halted  AppVM"))
ok &= check("grep hits and comments are source, not state",
            [A.vm_state_text("tools/run.sh:12:  # build-vm up for the probe\n# test-vm is running", is_vm)], [])
V = A.load_vocab(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "examples", "vm-campaign.sessiongraph.json"))
ok &= check("project vm_states rules", A.vm_state_text("lock acquired vm=test-vm pid=1", is_vm, V["_vm_states"]),
            ("test-vm", "running", "lock acquired vm=test-vm pid=1"))
from campaign import Campaign
C, day = Campaign(V["campaign"]), 1792454400               # 2026-10-20 00:00Z
C.observe("suite pid=1 started 10:00:00Z\n[10:00:05] ### install (fresh)\n[10:30:00] ### upgrade\n[11:00:00] --- test: smoke-test", day + 11*3600)
C.observe("suite pid=2 started 12:00:00Z\n[12:00:05] ### install (fresh)\n[12:20:00] upgrade: still going", day + 12.4*3600)
P = C.project(day + 13*3600)                               # the earlier run reached smoke-test 60 min in
ok &= check("campaign projects the next phase on the earlier run's pace", [(P["phase"], P["vm"], P["how"])], ("smoke-test", "test-vm", "projected"))
ok &= check("campaign is over after its exit line", [C.project(day + 13*3600)["over"] if not C.observe("[12:50:00] SUITE EXIT rc=0", day + 12.9*3600) else None], True)
K = {"vm": set(), "host": set(), "service": set()}
acts('S=/tmp/scratch/patch; mkdir -p "$S/src"; cp vendor/lib/src/log.c "$S/src/log.c"', known=K)
ok &= check("a copy is its original: editing the copy edits log.c of the source tree",
            acts("sed -i 's/a/b/' /tmp/scratch/patch/src/log.c", known=K), ("agent", "edit", "file", "log.c"))
ok &= check("... and it is one node, keyed by the original",
            [a["dst"]["key"] for a in A.bash_actions("cat /tmp/scratch/patch/src/log.c", A.Context(cwd=CWD, known=K))],
            CWD + "/vendor/lib/src/log.c")
acts("cp -r vendor/lib /tmp/work", known=K)
ok &= check("a copied tree maps every file under it",
            [a["dst"]["key"] for a in A.bash_actions("cat /tmp/work/include/log.h", A.Context(cwd=CWD, known=K))],
            CWD + "/vendor/lib/include/log.h")
ok &= check("a grep pattern that looks like a file is still the pattern",
            [a for a in acts("grep -q 'wedge-lib.sh' tools/tests/list") if a[3] == "wedge-lib.sh"])
ok &= check("... and the file it searches is kept", acts("grep -q 'wedge-lib.sh' tools/tests/list"),
            ("agent", "search", "file", "list"))
ok &= check("grep -A 3 pattern file: the flag value is not the pattern", acts("grep -A 3 needle src/a.py"),
            ("agent", "search", "file", "a.py"))
C2 = Campaign(V["campaign"])
C2.observe("suite pid=3 started 14:00:00Z\n[14:00:05] ### install\n[14:20:00] --- test: smoke-test", day + 14.4*3600)
C2.observe("15:30:00 probe[test-vm]: round 2 still going", day + 15.5*3600)
P2 = C2.project(day + 16.2*3600)                           # 2h after smoke-test began, 42 min after test-vm last spoke
ok &= check("a phase whose machine still shows up in the log is not over", [(P2["over"], P2["vm"])], (False, "test-vm"))
ok &= check("a quiet campaign is over, and says when", [C2.project(day + 17*3600)["over"]], True)
ok &= check("outcome: the last signal wins", [A.outcome_of("3 passed, 0 failed\nlater\nrc=1")], "fail")
ok &= check("outcome: 'ok: false' is not ok", [A.outcome_of("ok: false")], None)
ok &= check("outcome: a mention of AssertionError mid-line is not a failure", [A.outcome_of("grep found AssertionError in 3 files")], None)
print("ALL OK" if ok else "FAILURES")
sys.exit(0 if ok else 1)
