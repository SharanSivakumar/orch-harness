#!/usr/bin/env python3
"""Unified state reconciliation for every orch surface (100% read-only).

An earlier rig had three independent readers of the same state — the bash
`status` subcommand, dashboard.py, and the Telegram poller — and they
disagreed. Two failure modes were routine:

  1. STALE SENTINEL. A row halted, the Owner intervened by hand and re-dispatched
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
import tmuxio  # noqa: E402
from rows import active_lanes, archived_lanes, find_row, implicit_deps, load_rows, parse_file, unmet_deps  # noqa: E402
from store import Store  # noqa: E402

TERMINAL = {"CLOSED"}
HALTING = {"BLOCKED", "ROUTE_A"}

# Display precedence, worst first — used to pick a lane's headline state.
SEVERITY = ["ROUTE_A", "BLOCKED", "HALTED", "ORPHAN", "UNBLOCKING", "REJECTED", "GATED", "SPEND_WAIT",
            "MANUAL", "LIMITED", "QUIET", "RUNNING", "VERIFYING", "REVIEWING", "READY", "PAUSED",
            "DISABLED", "WAITING", "PENDING", "CLOSED"]


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
    meta: Dict[str, str] = field(default_factory=dict)
    sentinel: str = "NONE"
    note: str = ""
    phase: str = ""
    executor: str = ""
    exec_source: str = "queue"
    state: str = "PENDING"
    live_window: bool = False
    window_ours: bool = False
    unblock_window: str = ""
    review_window: str = ""
    unblock_n: int = 0
    attempt: int = 0
    unmet_deps: List[str] = field(default_factory=list)
    idle_secs: int = -1
    why: str = ""


class Resolver:
    def __init__(self, cfg: Optional[Config] = None, tmux: bool = True):
        self.cfg = cfg or load()
        self.st = Store(self.cfg.state)
        self.windows = self._tmux_windows() if tmux else {}

    # -- tmux ------------------------------------------------------------
    def _tmux_windows(self) -> Dict[str, Dict[str, Any]]:
        """name -> {alive, activity, ident}. Dead panes are dropped entirely."""
        return {n: {"alive": True, "activity": w["activity"], "ident": w.ident}
                for n, w in tmuxio.windows(self.cfg.tmux_session).items() if w["alive"]}

    def alive(self, name: str) -> bool:
        return name in self.windows

    def window_is_ours(self, rid: str) -> bool:
        w = self.windows.get(rid)
        if not w:
            return False
        d = self.st.dispatch(rid)
        if d.get("ident"):
            return w.get("ident") == d["ident"]
        try:   # v1 stamp
            parts = (self.cfg.state / f"{rid}.window").read_text().split()
        except Exception:
            return False
        return len(parts) >= 2 and w.get("ident") == " ".join(parts[:2])

    def capture(self, name: str, lines: int = 20) -> str:
        if not self.alive(name):
            return ""
        return "\n".join(tmuxio.tail_lines(tmuxio.capture(self.cfg.tmux_session, name, lines + 20), lines))

    # -- disk ------------------------------------------------------------
    def sentinel(self, rid: str):
        v, note, _ = self.st.status(rid)
        return v, note

    def gate_open(self, rid: str) -> bool:
        return (self.cfg.control / f"GO_{rid}").exists()

    def override(self, rid: str) -> str:
        f = self.cfg.control / f"EXECUTOR_{rid}"
        try:
            return re.sub(r"[^a-z0-9_-]", "", f.read_text().strip().split()[0].lower())
        except Exception:
            return ""

    def paused(self, lane: str) -> bool:
        c = self.cfg.control
        return (c / "PAUSE").exists() or (c / f"PAUSE_{lane}").exists()

    def stopped(self, lane: str) -> bool:
        c = self.cfg.control
        return (c / "STOP").exists() or (c / f"STOP_{lane}").exists()

    def enabled(self, lane: str) -> bool:
        c = self.cfg.control
        return (c / f"RUN_{lane}").exists() or (c / "RUN_ALL").exists()

    def engine_alive(self) -> bool:
        if getattr(self, "_engine", None) is None:
            hb = self.cfg.state / "engine.heartbeat"
            try:
                tick = float((self.cfg.data.get("scheduler") or {}).get("tick_seconds", 15))
                self._engine = time.time() - hb.stat().st_mtime < max(90.0, tick * 4)
            except Exception:
                self._engine = False
        return self._engine

    def watcher_running(self, lane: str) -> bool:
        """v2: the lane is babysat when the rig's engine is up and the lane is enabled."""
        return self.engine_alive() and self.enabled(lane)

    def leases(self) -> Dict[str, Dict[str, str]]:
        out: Dict[str, Dict[str, str]] = {}
        try:
            from leases import Leases
            L = Leases(self.cfg)
            for name in L.res:
                for p, info in L.holders(name):
                    out[p.name[:-5]] = {"owner": info.get("owner", "?"), "since": info.get("since", "?")}
        except Exception:
            pass
        return out

    # -- queue -----------------------------------------------------------
    def parse_lane(self, lane: str) -> List[Row]:
        return [self._row(q) for q in parse_file(self.cfg.instance / f"queue_{lane}.tsv", lane) if not q.problems]

    @staticmethod
    def _row(q) -> Row:
        return Row(lane=q.lane, id=q.id, deps=list(q.deps), gate=q.gate, queue_exec=q.queue_exec,
                   plan=q.plan, extra_allow=list(q.extra_allow), meta=dict(q.meta))

    # -- resolution ------------------------------------------------------
    def resolve(self) -> Dict[str, Any]:
        lanes = active_lanes(self.cfg)
        qrows, by_q = load_rows(self.cfg)
        implicit = implicit_deps(self.cfg, qrows)
        all_rows = [self._row(q) for q in qrows if not q.archived and not q.problems]
        now = int(time.time())
        allowed = set(self.cfg.data["executors"]["allowed"])
        try:
            stall = int(self.cfg.data["lanes"]["stall_warning_seconds"])
        except Exception:
            stall = 2700
        try:
            notes = json.loads((self.cfg.state / "sched.json").read_text())
        except Exception:
            notes = {}
        default = self.cfg.data["executors"]["default"]

        for r in all_rows:
            r.sentinel, r.note = self.sentinel(r.id)
            r.phase = self.st.phase(r.id)
            res = self.st.result(r.id)
            d = self.st.dispatch(r.id)
            r.attempt = int(d.get("attempt", 0) or 0)
            ov = self.override(r.id)
            if ov and ov in allowed:
                r.executor, r.exec_source = ov, "override"
            else:
                r.executor = r.queue_exec if r.queue_exec in allowed else default
                r.exec_source = "queue"
            r.live_window = self.alive(r.id)
            r.unblock_n = self.st.read_int(f"{r.id}.unblock_n", 0)
            for name in self.windows:
                if name.startswith(f"ub-{r.id}-"):
                    r.unblock_window = name
                if name.startswith(f"rv-{r.id}-"):
                    r.review_window = name
            r.unmet_deps = unmet_deps(self.st, r, implicit)
            if r.live_window:
                w = self.windows[r.id]
                r.idle_secs = max(0, now - w["activity"]) if w["activity"] else -1
            r.window_ours = r.live_window and self.window_is_ours(r.id)
            r.why = str(res.get("why", "") or notes.get(r.id, ""))
            ph = r.phase
            if ph == "accepted":
                r.state = "CLOSED"
            elif ph == "verifying" or ph == "claimed":
                r.state = "VERIFYING"
            elif ph == "reviewing":
                r.state = "REVIEWING"
            elif ph == "unblocking" or r.unblock_window:
                r.state = "UNBLOCKING"
            elif ph in ("blocked", "blocked_claim"):
                r.state = "BLOCKED"
            elif ph == "route_a":
                r.state = "ROUTE_A"
            elif ph == "halted":
                r.state = "HALTED"
            elif ph == "spend_wait":          # lib/spend.py: waits for the Owner's spend answer
                r.state = "SPEND_WAIT"
            elif r.live_window and not r.window_ours:
                r.state = "ORPHAN"
            elif r.live_window and d.get("open"):
                if self.st.has(f"{r.id}.limited"):
                    r.state = "LIMITED"
                else:
                    r.state = "QUIET" if (0 <= stall < r.idle_secs) else "RUNNING"
            elif d.get("open") and d.get("executor") == "manual":
                r.state = "MANUAL"
            elif r.gate and not self.gate_open(r.id) and not r.unmet_deps:
                r.state = "GATED"
            elif r.unmet_deps:
                r.state = "WAITING"
            elif not self.enabled(r.lane):
                r.state = "DISABLED"
            elif self.paused(r.lane):
                r.state = "PAUSED"
            elif r.executor == "manual":
                r.state = "MANUAL"
            elif str(notes.get(r.id, "")).startswith("READY"):
                r.state = "READY"
            else:
                r.state = "PENDING"
            if ph == "retry" and r.state == "PENDING":
                r.why = r.why or "re-dispatch pending"

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
                "enabled": self.enabled(lane),
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
            "engine": self.engine_alive(),
            "lanes": lane_view,
            "archived_lanes": archived_lanes(self.cfg),
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
        live = [r for r in open_rows if r.state in ("RUNNING", "UNBLOCKING", "QUIET", "LIMITED", "REVIEWING")]
        if live:
            r = live[0]
            what = r.unblock_window or r.review_window or r.id
            quiet = f" · no output for {r.idle_secs // 60} min" if r.state == "QUIET" else ""
            return (f"{r.state} {r.id} (executor {r.executor}"
                    f"{', assigned' if r.exec_source == 'override' else ''}){quiet}"), what
        worst = sorted(open_rows, key=lambda r: rank.get(r.state, 99))[0]
        msg = {
            "ROUTE_A": f"HALTED — {worst.id} returned ROUTE_A",
            "BLOCKED": f"HALTED — {worst.id} returned BLOCKED",
            "ORPHAN": (f"ORPHAN WINDOW — a session this rig did not dispatch holds the "
                       f"name '{worst.id}' and is not working it. Close it "
                       f"(tmux kill-window -t {self.cfg.tmux_session}:{worst.id}) and the "
                       f"lane dispatches by itself · or claim it: orch adopt {worst.id}"),
            "GATED": f"HARD PAUSE before {worst.id} — release: orch approve {worst.id}",
            "SPEND_WAIT": f"WAITING FOR MONEY — {worst.id}: {worst.why}",
            "HALTED": f"HALTED — {worst.id}: {worst.why or worst.sentinel}",
            "VERIFYING": f"verifying {worst.id}'s close claim",
            "READY": f"READY {worst.id} — {worst.why}",
            "DISABLED": f"lane not started — orch start {lane}",
            "MANUAL": f"WAITING FOR OPERATOR — {worst.id} is a manual row",
            "PAUSED": f"PAUSED before {worst.id} (orch resume to continue)",
            "WAITING": f"waiting on deps [{','.join(worst.unmet_deps)}] for {worst.id}",
            "PENDING": (f"IDLE — next unstarted: {worst.id}"
                        if not self.watcher_running(lane) else f"dispatching {worst.id}"
                        + (f" ({worst.why})" if worst.why else "")),
        }.get(worst.state, f"{worst.state} {worst.id}")
        # Any state whose exit needs the watcher must say so when there is none.
        # A halted lane whose watcher has exited used to read exactly like one
        # still being babysat.
        if not self.watcher_running(lane) and worst.state in (
                "PENDING", "WAITING", "GATED", "ORPHAN",
                "BLOCKED", "ROUTE_A", "HALTED", "READY"):
            msg += "  [engine down — 'orch start']" if not self.engine_alive() else "  [lane not started — 'orch start " + lane + "']"
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
        q = find_row(self.cfg, rid)
        if q is None:
            return []
        row = self._row(q)
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
        for lane in active_lanes(self.cfg):
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
