"""Where a long-running campaign the agent launched probably is right now.

The agent starts a campaign (`nohup run-suite.sh &`) and from then on the
campaign drives the machines by itself; the transcript only holds the slices of
its log the agent happened to print. From those slices -- phase headers and
phase-prefixed lines with their clock times, across this run and earlier ones --
this keeps a timeline per run, and projects the current run forward on the most
recent earlier run's pace: which phase it is in, so which VM is up.

It is a projection, labelled as one. Configured per project in the vocabulary:

    "campaign": {
      "order": ["install", "upgrade", "smoke-test"],        # phases in run order
      "vm":    {"install": "build-vm", "upgrade": "build-vm", "smoke-test": "test-vm"},
      "start": "suite pid=\\d+ started (?P<time>\\d\\d:\\d\\d:\\d\\d)Z",   # a launch line
      "end":   "SUITE EXIT|ABORTED",
      "last_phase_minutes": 60,     # how long the last phase may run before "over"
      "serial": true                # one machine at a time: the others are down
    }
"""
import re
from datetime import datetime, timezone

TIME = re.compile(r"^\s*\[?(\d\d):(\d\d):(\d\d)(?=[\]\sZ])")


class Campaign:
    def __init__(self, cfg):
        self.cfg = cfg or {}
        self.order = list(self.cfg.get("order") or [])
        self.vm = dict(self.cfg.get("vm") or {})
        self.idx = {p.lower(): i for i, p in enumerate(self.order)}
        names = "|".join(re.escape(p) for p in sorted(self.order, key=len, reverse=True))
        # a phase named on its own, not inside a path: `### install (`, `upgrade-b1:`,
        # `test: smoke-test`; never `smoke-test.d/...`
        self.phase_rx = re.compile(r"(?<![\w./-])(%s)(?:-[a-z0-9]+)?(?=[:\s)]|$)" % names, re.I) if names else None
        self.start_rx = re.compile(self.cfg["start"]) if self.cfg.get("start") else None
        self.end_rx = re.compile(self.cfg["end"]) if self.cfg.get("end") else None
        self.marks = set()        # (abs ts, phase index)
        self.busy = {}            # machine -> last abs ts a log line named it: its phase still at work
        self.vm_rx = re.compile(r"(?<![\w-])(%s)(?![\w-])" % "|".join(
            re.escape(v) for v in sorted(set(self.vm.values()), key=len, reverse=True))) if self.vm else None
        self.starts = set()       # abs ts of campaign launches
        self.ends = set()         # abs ts of campaign exits

    @property
    def enabled(self):
        return bool(self.phase_rx)

    def _abs(self, h, m, s, seen_ts):
        """A log clock time, placed on the day it was printed (UTC)."""
        d = datetime.fromtimestamp(seen_ts, timezone.utc)
        t = d.replace(hour=int(h), minute=int(m), second=int(s), microsecond=0).timestamp()
        return t - 86400 if t > seen_ts + 300 else t

    def observe(self, text, seen_ts):
        if not self.enabled or not text or not seen_ts:
            return
        for line in text.splitlines():
            if self.start_rx:
                m = self.start_rx.search(line)
                if m:
                    hms = (m.groupdict().get("time") or "").split(":")
                    self.starts.add(self._abs(*hms, seen_ts) if len(hms) == 3 else seen_ts)
            tm = TIME.match(line)
            if not tm:
                continue
            t = self._abs(*tm.groups(), seen_ts)
            if self.end_rx and self.end_rx.search(line):
                self.ends.add(t)
            m = self.phase_rx.search(line[tm.end():])
            if m:
                self.marks.add((t, self.idx[m.group(1).lower()]))
            for v in (self.vm_rx.findall(line[tm.end():]) if self.vm_rx else ()):
                self.busy[v] = max(self.busy.get(v, 0), t)

    def _runs(self):
        """Marks split into runs: at a launch, or when the first phase begins again."""
        runs, cur, last_start = [], [], None
        starts = sorted(self.starts)
        for t, i in sorted(self.marks):
            launched = [s for s in starts if (last_start is None or s > last_start) and s <= t]
            if cur and (launched or (i == 0 and max(j for _, j in cur) > 0)):
                runs.append(cur); cur = []
            if launched:
                last_start = launched[-1]
            cur.append((t, i))
        if cur:
            runs.append(cur)
        return runs

    def project(self, now):
        runs = self._runs()
        if not runs:
            return None
        run = runs[-1]
        S = min(t for t, _ in run)
        launch = max([s for s in self.starts if s <= S + 600], default=None)
        S0 = launch if launch and launch <= S else S
        L = max(i for _, i in run)                        # furthest phase seen, and the latest line
        tL = max(t for t, _ in run)
        ends = [e for e in self.ends if e >= S0]
        if ends:
            return {"over": True, "since": min(ends), "basis": "the campaign log printed its exit",
                    "serial": bool(self.cfg.get("serial")), "vms": sorted(set(self.vm.values()))}
        # the pace: the most recent earlier run that got further than this one
        ref = next((r for r in reversed(runs[:-1]) if max(i for _, i in r) > L), None)
        expected = {}
        if ref:
            R0 = min(t for t, _ in ref)
            first = {}
            for t, i in sorted(ref):
                first.setdefault(i, t)
            for i, t in first.items():
                if i > L:
                    expected[i] = max(S + (t - R0), tL)
        phase, since, how = L, min(t for t, i in run if i == L), "seen"
        for i in sorted(expected):
            if expected[i] <= now:
                phase, since, how = i, expected[i], "projected"
        last = len(self.order) - 1
        limit = (self.cfg.get("last_phase_minutes") or 60) * 60
        name = self.order[phase]
        alive = max(since, tL, self.busy.get(self.vm.get(name), 0))    # the latest sign this phase was at work
        if phase == last and now - alive > limit:
            return {"over": True, "since": alive + limit, "serial": bool(self.cfg.get("serial")),
                    "vms": sorted(set(self.vm.values())),
                    "basis": f"nothing from {name} since ~{_hm(alive)}, longer than it ever runs"}
        if how == "seen" and ref is None and now - tL > limit * 2:
            return None                                   # nothing to pace it by, and long quiet
        basis = (f"last log line seen {_hm(max(tL, alive))} ({self.order[L]})"
                 + (f"; paced on the run of {_hm(min(t for t, _ in ref))}" if ref else ""))
        return {"over": False, "phase": name, "vm": self.vm.get(name), "since": since, "alive": alive,
                "how": how, "basis": basis, "serial": bool(self.cfg.get("serial")),
                "vms": sorted(set(self.vm.values()))}


def _hm(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%H:%M")
