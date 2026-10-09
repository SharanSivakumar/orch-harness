#!/usr/bin/env python3
"""Unified state reconciliation for every orch surface (100% read-only).

An earlier rig had three independent readers of the same state — the bash
`status` subcommand, dashboard.py, and the Telegram poller — and they
disagreed. Two failure modes were routine:

  1. STALE SENTINEL. A row halted, the operator intervened by hand and re-dispatched
     it; state/<id>.status still said BLOCKED, so the banner read HALTED while
     the agent was visibly working. Rule here: a LIVE tmux window for <id>
     outranks any sentinel on disk. Live process > stale sentinel.
  2. STICKY PANE. `remain-on-exit on` leaves dead panes behind with the row's
     name, so pane pickers latched onto a corpse. Rule here: pane_dead == 1
     windows are excluded from every candidate pool, always.
  3. ORPHAN WINDOW. Rule 1 read a live window as proof of work, but a FINISHED
     session left open after `orch reset` wears the row's name too, so the lane
     reported RUNNING for hours with nothing running. Rule here: dispatch stamps
     the tmux identity it created in state/<id>.window; a live window that does
     not match the stamp is an ORPHAN, never RUNNING. Live process > stale
     sentinel still holds — but "live process" now means the one we started.
  4. QUIET. A stamped session can still be finished-and-idling (an interactive
     TUI that ended its turn without a sentinel). Nothing can distinguish that
     from deep work except silence, so past the configured stall threshold the
     state reads QUIET rather than RUNNING. Warned, never killed — the change is
     to what the surfaces SAY, not to what the watcher does.

This module never writes. It shells out to tmux for list-windows and
capture-pane only.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config_parser import Config, load  # noqa: E402

ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")

TERMINAL = {"CLOSED"}
HALTING = {"BLOCKED", "ESCALATED"}

# Display precedence, worst first — used to pick a lane's headline state.
SEVERITY = ["ESCALATED", "BLOCKED", "HALTED", "ORPHAN", "UNBLOCKING", "GATED",
            "MANUAL", "QUIET", "RUNNING", "PAUSED", "WAITING", "PENDING", "CLOSED"]


def _instance_of_pid(pid: str) -> Optional[str]:
    """ORCH_INSTANCE from a process's environment; None when it cannot be read.
    Only that one variable is extracted; nothing else is kept or printed."""
    try:
        raw = Path(f"/proc/{int(pid)}/environ").read_bytes()
    except Exception:
        return None
    for item in raw.split(b"\0"):
        if item.startswith(b"ORCH_INSTANCE="):
            return item[len(b"ORCH_INSTANCE="):].decode(errors="replace")
    return None


def _sh(cmd: List[str], timeout: int = 15) -> str:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return (p.stdout or "").strip()
    except Exception:
        return ""


@dataclass
class Row:
    lane: str
    id: str
    deps: List[str]
    gate: bool
    queue_exec: str
    plan: str
    extra_allow: List[str] = field(default_factory=list)
    sentinel: str = "NONE"
    note: str = ""
    executor: str = ""
    exec_source: str = "queue"
    state: str = "PENDING"
    live_window: bool = False
    window_ours: bool = False
    unblock_window: str = ""
    unblock_n: int = 0
    unmet_deps: List[str] = field(default_factory=list)
    idle_secs: int = -1


class Resolver:
    def __init__(self, cfg: Optional[Config] = None):
        self.cfg = cfg or load()
        self.windows = self._tmux_windows()

    # -- tmux ------------------------------------------------------------
    def _tmux_windows(self) -> Dict[str, Dict[str, Any]]:
        """name -> {alive, activity, index}. Dead panes are dropped entirely."""
        out = _sh(["tmux", "list-windows", "-t", self.cfg.tmux_session, "-F",
                   "#{window_name}\t#{pane_dead}\t#{window_activity}\t#{window_index}"
                   "\t#{window_id}\t#{pane_pid}"])
        wins: Dict[str, Dict[str, Any]] = {}
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) < 4:
                continue
            name, dead, act, idx = parts[0], parts[1], parts[2], parts[3]
            if dead == "1":
                continue          # anti-desync rule 2: corpses are never candidates
            wins[name] = {"alive": True,
                          "activity": int(act) if act.isdigit() else 0,
                          "index": idx,
                          "ident": " ".join(parts[4:6]) if len(parts) >= 6 else ""}
        return wins

    def alive(self, name: str) -> bool:
        return name in self.windows

    def window_is_ours(self, rid: str) -> bool:
        """True when the live window named <rid> is the session the rig dispatched.

        Bearing a row's name is not evidence of work — a finished session left
        open after `orch reset` bears it too. dispatch() writes the tmux
        window_id + pane_pid it created to state/<id>.window; anything else is
        an orphan. Both halves are compared because tmux reuses window ids
        across a server restart.
        """
        w = self.windows.get(rid)
        if not w:
            return False
        try:
            stamp = (self.cfg.state / f"{rid}.window").read_text().split("\n")[0]
        except Exception:
            return False
        parts = stamp.split()
        if len(parts) < 2:
            return False
        return w.get("ident") == " ".join(parts[:2])

    def capture(self, name: str, lines: int = 20) -> str:
        if not self.alive(name):
            return ""
        raw = _sh(["tmux", "capture-pane", "-t", f"{self.cfg.tmux_session}:{name}",
                   "-p", "-S", f"-{lines}"])
        keep = [l.rstrip() for l in ANSI.sub("", raw).splitlines() if l.strip()]
        return "\n".join(keep[-lines:])

    # -- disk ------------------------------------------------------------
    def sentinel(self, rid: str):
        f = self.cfg.state / f"{rid}.status"
        try:
            txt = f.read_text(errors="replace").splitlines()
        except Exception:
            return "NONE", ""
        if not txt or not txt[0].strip():
            return "NONE", ""
        verdict = re.sub(r"[^A-Z_]", "", txt[0].strip().upper()) or "NONE"
        return verdict, (txt[1].strip() if len(txt) > 1 else "")

    def gate_open(self, rid: str) -> bool:
        return (self.cfg.control / f"GO_{rid}").exists()

    def override(self, rid: str) -> str:
        f = self.cfg.control / f"EXECUTOR_{rid}"
        try:
            return re.sub(r"[^a-z]", "", f.read_text().strip().lower())
        except Exception:
            return ""

    def paused(self, lane: str) -> bool:
        c = self.cfg.control
        return (c / "PAUSE").exists() or (c / f"PAUSE_{lane}").exists()

    def stopped(self, lane: str) -> bool:
        c = self.cfg.control
        return (c / "STOP").exists() or (c / f"STOP_{lane}").exists()

    def watcher_running(self, lane: str) -> bool:
        # Scoped to THIS instance: lane letters repeat across rigs, so a
        # bare process-name match read another instance's watcher as ours. A
        # candidate counts only when the ORCH_INSTANCE in its environment resolves
        # to ours; an unreadable environment counts as ours (the conservative side,
        # the same rule as orchestrator_core.sh watcher_pids).
        pat = re.compile(rf"orchestrator_core\.sh run {re.escape(lane)}(\s|$)")
        mine = str(self.cfg.instance.resolve())
        for line in _sh(["ps", "-eo", "pid=,args="]).splitlines():
            pid, _, args = line.strip().partition(" ")
            if not pat.search(args):
                continue
            inst = _instance_of_pid(pid)
            if inst is None or str(Path(inst).resolve()) == mine:
                return True
        return False

    def leases(self) -> Dict[str, Dict[str, str]]:
        out: Dict[str, Dict[str, str]] = {}
        if not self.cfg.leases.is_dir():
            return out
        for d in sorted(self.cfg.leases.glob("*.lock")):
            try:
                owner = (d / "owner").read_text().splitlines()
            except Exception:
                owner = []
            out[d.stem] = {"owner": owner[0] if owner else "?",
                           "since": owner[1] if len(owner) > 1 else "?"}
        return out

    # -- queue -----------------------------------------------------------
    def parse_lane(self, lane: str) -> List[Row]:
        f = self.cfg.instance / f"queue_{lane}.tsv"
        rows: List[Row] = []
        if not f.is_file():
            return rows
        for line in f.read_text(errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = re.split(r"\s+", line, maxsplit=5)
            if len(parts) < 5:
                continue
            rid, deps, gate, qexec, plan = parts[:5]
            extra = re.split(r"[,\s]+", parts[5].strip()) if len(parts) > 5 else []
            rows.append(Row(lane=lane, id=rid,
                            deps=[] if deps == "-" else deps.split(","),
                            gate=(gate == "1"), queue_exec=qexec, plan=plan,
                            extra_allow=[e for e in extra if e]))
        return rows

    # -- resolution ------------------------------------------------------
    def resolve(self) -> Dict[str, Any]:
        lanes = self.cfg.lanes()
        all_rows: List[Row] = []
        for lane in lanes:
            all_rows.extend(self.parse_lane(lane))
        by_id = {r.id: r for r in all_rows}
        now = int(time.time())
        allowed = set(self.cfg.data["executors"]["allowed"])
        try:
            stall = int(self.cfg.data["lanes"]["stall_warning_seconds"])
        except Exception:
            stall = 2700
        default = self.cfg.data["executors"]["default"]

        for r in all_rows:
            r.sentinel, r.note = self.sentinel(r.id)
            ov = self.override(r.id)
            if ov and ov in allowed:
                r.executor, r.exec_source = ov, "override"
            else:
                r.executor = r.queue_exec if r.queue_exec in allowed else default
                r.exec_source = "queue"
            r.live_window = self.alive(r.id)
            try:
                r.unblock_n = int((self.cfg.state / f"{r.id}.unblock_n").read_text().strip())
            except Exception:
                r.unblock_n = 0
            for name in self.windows:
                if name.startswith(f"ub-{r.id}-"):
                    r.unblock_window = name
            r.unmet_deps = [d for d in r.deps if self.sentinel(d)[0] != "CLOSED"]
            if r.live_window:
                w = self.windows[r.id]
                r.idle_secs = max(0, now - w["activity"]) if w["activity"] else -1

            # ---- state precedence -------------------------------------
            # 1. CLOSED is TERMINAL and wins over any residual live window.
            #    A finished session left open (e.g. a Claude TUI that never
            #    /exit'd after writing its sentinel) is a zombie, not proof of
            #    work — the lane has already moved on. Reading it as RUNNING
            #    made every read-only surface show a closed row as running.
            # 2. otherwise a live window WE DISPATCHED is the row running,
            #    whatever the (non-terminal) sentinel says; a live window we
            #    did not is an orphan, and a silent one is QUIET, not work.
            r.window_ours = r.live_window and self.window_is_ours(r.id)
            if r.sentinel == "CLOSED":
                r.state = "CLOSED"
            elif r.unblock_window:
                r.state = "UNBLOCKING"
            elif r.live_window and not r.window_ours:
                r.state = "ORPHAN"
            elif r.live_window:
                r.state = "QUIET" if (0 <= stall < r.idle_secs) else "RUNNING"
            elif r.sentinel in HALTING:
                r.state = r.sentinel
            elif r.sentinel != "NONE":
                r.state = "HALTED"          # unrecognized sentinel
            elif r.gate and not self.gate_open(r.id):
                r.state = "GATED"
            elif r.unmet_deps:
                r.state = "WAITING"
            elif r.executor == "manual":
                r.state = "MANUAL"
            elif self.paused(r.lane):
                r.state = "PAUSED"
            else:
                r.state = "PENDING"

        # A gate far down a lane is not "before" anything yet: the watcher parks
        # on a gate only when that row is the lane's next row. Until then the
        # row waits on its deps like any other, so status/dashboard never say
        # "GATE HOLD before int1" while setup is running .
        for lane in lanes:
            first_open = next((r for r in all_rows if r.lane == lane and r.state != "CLOSED"), None)
            for r in all_rows:
                if r.lane == lane and r.state == "GATED" and r.unmet_deps and r is not first_open:
                    r.state = "WAITING"

        lane_view = []
        for lane in lanes:
            rows = [r for r in all_rows if r.lane == lane]
            open_rows = [r for r in rows if r.state != "CLOSED"]
            headline, focus = self._headline(lane, rows)
            lane_view.append({
                "lane": lane,
                "headline": headline,
                "focus": focus,
                "closed": sum(1 for r in rows if r.state == "CLOSED"),
                "total": len(rows),
                "watcher": self.watcher_running(lane),
                "paused": self.paused(lane),
                "stopped": self.stopped(lane),
                "next": open_rows[0].id if open_rows else "",
                "rows": [asdict(r) for r in rows],
            })

        return {
            "generated": time.strftime("%F %T"),
            "name": self.cfg.name,
            "tmux": self.cfg.tmux_session,
            "instance": str(self.cfg.instance),
            "lanes": lane_view,
            "live_windows": sorted(self.windows),
            "leases": self.leases(),
            "gates": sorted(p.name for p in self.cfg.control.glob("GO_*")) if self.cfg.control.is_dir() else [],
        }

    def _headline(self, lane: str, rows: List[Row]):
        """Lane banner + the row a viewer should be looking at."""
        if self.stopped(lane):
            return "STOPPED (control/STOP)", ""
        rank = {s: i for i, s in enumerate(SEVERITY)}
        open_rows = [r for r in rows if r.state != "CLOSED"]
        if not open_rows:
            return "LANE COMPLETE — every row CLOSED", ""
        # focus follows the live pane first (rule 1), else worst state
        live = [r for r in open_rows if r.state in ("RUNNING", "UNBLOCKING", "QUIET")]
        if live:
            r = live[0]
            what = r.unblock_window or r.id
            quiet = f" · no output for {r.idle_secs // 60} min" if r.state == "QUIET" else ""
            return (f"{r.state} {r.id} (executor {r.executor}"
                    f"{', assigned' if r.exec_source == 'override' else ''}){quiet}"), what
        worst = sorted(open_rows, key=lambda r: rank.get(r.state, 99))[0]
        msg = {
            "ESCALATED": f"HALTED — {worst.id} returned ESCALATED",
            "BLOCKED": f"HALTED — {worst.id} returned BLOCKED",
            "HALTED": f"HALTED — {worst.id} carries sentinel '{worst.sentinel}'",
            "ORPHAN": (f"ORPHAN WINDOW — a session this rig did not dispatch holds the "
                       f"name '{worst.id}' and is not working it. Close it "
                       f"(tmux kill-window -t {self.cfg.tmux_session}:{worst.id}) and the "
                       f"lane dispatches by itself · or claim it: orch adopt {worst.id}"),
            "GATED": f"GATE HOLD before {worst.id} — release: orch approve {worst.id}",
            "MANUAL": f"WAITING FOR OPERATOR — {worst.id} is a manual row",
            "PAUSED": f"PAUSED before {worst.id} (orch resume to continue)",
            "WAITING": f"waiting on deps [{','.join(worst.unmet_deps)}] for {worst.id}",
            "PENDING": (f"IDLE — next unstarted: {worst.id}"
                        if not self.watcher_running(lane) else f"dispatching {worst.id}"),
        }.get(worst.state, f"{worst.state} {worst.id}")
        # Any state whose exit needs the watcher must say so when there is none.
        # A halted lane whose watcher has exited used to read exactly like one
        # still being babysat.
        if not self.watcher_running(lane) and worst.state in (
                "PENDING", "WAITING", "GATED", "ORPHAN",
                "BLOCKED", "ESCALATED", "HALTED"):
            msg += "  [no watcher — 'orch start']"
        return msg, worst.id


    # -- guard allowlist (convention-based) -------------------------------
    def allowlist(self, rid: str) -> List[str]:
        """Every path row `rid` may stage, as REPO-relative paths.

        Convention replaces the 70-line `case` statement the earlier guard
        carried. Derived, in order:
          1. the row's own phase folder     — dirname(plan_path)
          2. its report pack prefix         — <reports_dir>/<id>_
          3. guard.shared_paths from orch.yaml
          4. the queue's optional 6th column (comma or space separated)
          5. an ALLOWLIST file inside the phase folder, one path per line
        Entries 1, 2, 4 and 5 are workspace-relative; they are re-based onto
        the guard's git repo, which is often a parent of the workspace.
        """
        row = None
        for lane in self.cfg.lanes():
            for r in self.parse_lane(lane):
                if r.id == rid:
                    row = r
                    break
            if row:
                break
        if row is None:
            return []
        pre = self.cfg.repo_prefix
        def rebase(p: str) -> str:
            p = p.strip().lstrip("./")
            return f"{pre}/{p}" if pre else p

        out: List[str] = []
        g = self.cfg.data["guard"]
        if g.get("auto_phase_isolation", True):
            phase_dir = str(Path(row.plan).parent)
            if phase_dir not in (".", ""):
                out.append(rebase(phase_dir))
            out.append(rebase(f"{self.cfg.reports_dir}/{rid}_"))
        out += [rebase(p) for p in (g.get("shared_paths") or [])]
        out += [rebase(p) for p in row.extra_allow]
        af = self.cfg.workspace / Path(row.plan).parent / "ALLOWLIST"
        if af.is_file():
            for line in af.read_text(errors="replace").splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    out.append(rebase(line))
        seen, uniq = set(), []
        for p in out:
            if p and p not in seen:
                seen.add(p)
                uniq.append(p)
        return uniq

    # -- convenience for the bot / CLI -----------------------------------
    def text_summary(self, full: bool = False) -> str:
        s = self.resolve()
        out = [f"=== {s['name']} — {s['generated']} ==="]
        for lv in s["lanes"]:
            out.append(f"Lane {lv['lane']}: {lv['headline']}"
                       f"   [{lv['closed']}/{lv['total']} closed]")
        if s["live_windows"]:
            out.append("Live windows: " + " ".join(s["live_windows"]))
        out.append("--- open rows ---")
        for lv in s["lanes"]:
            for r in lv["rows"]:
                if r["state"] == "CLOSED" and not full:
                    continue
                note = r["note"]
                if not full and len(note) > 100:
                    note = note[:100] + "…"
                gate = "-" if not r["gate"] else ("GO✓" if self.gate_open(r["id"]) else "GO✗")
                out.append(f"{r['id']:<12} {r['state']:<10} {r['executor']:<10}"
                           f" {gate:<4} {note}")
        if s["leases"]:
            out.append("--- leases ---")
            for k, v in s["leases"].items():
                out.append(f"{k} held by {v['owner']} since {v['since']}")
        ctl = sorted(p.name for p in self.cfg.control.iterdir()) if self.cfg.control.is_dir() else []
        if ctl:
            out.append("--- controls ---")
            out.append(" ".join(ctl))
        ev = []
        for lane in self.cfg.lanes():
            f = self.cfg.state / f"events_{lane}.log"
            if f.is_file():
                try:
                    ev += f.read_text(errors="replace").splitlines()[-6:]
                except Exception:
                    pass
        if ev:
            out.append("--- last events ---")
            ev.sort()
            for line in ev[-6:]:
                out.append(line if full else line[:200])
            if not full:
                out.append("(truncated — 'orch status full' for complete text)")
        return "\n".join(out)

    def focus_window(self) -> str:
        """The single pane a phone-sized status view should tail."""
        s = self.resolve()
        for lv in s["lanes"]:
            if lv["focus"] and lv["focus"] in self.windows:
                return lv["focus"]
        return ""


if __name__ == "__main__":
    r = Resolver()
    if "--allowlist" in sys.argv:
        i = sys.argv.index("--allowlist")
        print("\n".join(r.allowlist(sys.argv[i + 1])))
    elif "--json" in sys.argv:
        print(json.dumps(r.resolve(), indent=2))
    elif "--focus" in sys.argv:
        print(r.focus_window())
    else:
        print(r.text_summary(full="--full" in sys.argv))
