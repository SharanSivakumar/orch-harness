#!/usr/bin/env python3
"""On-disk state for the v2 engine — the only module that writes under state/.

Files (all under <instance>/state/):

  <id>.status          the CLAIM: line 1 CLOSED|BLOCKED|ROUTE_A (rows), RESOLVED|ESCALATE
                       (unblockers), ACCEPT|REJECT (reviewers), PASS|FIX (plan reviews);
                       line 2 a one-line summary; optional line 3 `OUTCOME go|nogo|held|fail|infeasible`
                       (v3, `orch done --outcome`, a seal `CLOSED:<x>`). Written by the session,
                       `orch done`, `orch verdict` or an auto-seal. Lines 1-2 unchanged from v1.
  <id>.result.json     the engine's verdict on that claim: phase verifying|check_failed|
                       reviewing|rejected|accepted|blocked|route_a|halted, the checks it
                       ran, the review, timings. Dependents start on phase == accepted.
  <id>.dispatch.json   attempt n: executor, time, git heads at dispatch (for the diff a
                       reviewer reads and the commit count the close records).
  <id>.window          "<window_id> <pane_pid> <epoch>" — the tmux identity dispatched.
  attempts.jsonl       append-only event log; never truncated, survives `orch reset`.
  events_<L>.log       human-readable lane log (v1 format, kept for every reader).
  engine.log           the engine's own log.
  engine.adopted       epoch the instance moved to v2; rows CLOSED before it (no result)
                       are grandfathered as accepted.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

ROW_VERDICTS = ("CLOSED", "BLOCKED", "ROUTE_A")
SUB_VERDICTS = ("RESOLVED", "ESCALATE", "ACCEPT", "REJECT", "PASS", "FIX")
TERMINAL_OK = "accepted"
OUTCOMES = ("go", "nogo", "held", "fail", "infeasible")
INFEASIBLE = "infeasible"     # v3.1: "the remaining plan cannot reach the goal" — replan the route, never re-run it


def norm_outcome(x: str) -> str:
    """'NO-GO' -> 'nogo', 'PLAN_INFEASIBLE' -> 'infeasible'. Any other token is kept as written: it is not 'go',
    so it counts as negative."""
    v = re.sub(r"[\s_-]", "", str(x or "")).lower()
    return INFEASIBLE if v == "planinfeasible" else v


def negative(outcome: str) -> bool:
    return bool(outcome) and outcome != "go"


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


class Store:
    def __init__(self, state_dir: Path):
        self.dir = Path(state_dir)
        self.dir.mkdir(parents=True, exist_ok=True)

    # -- claims ------------------------------------------------------------
    def status_path(self, sid: str) -> Path:
        return self.dir / f"{sid}.status"

    def status(self, sid: str) -> Tuple[str, str, float]:
        """-> (VERDICT or NONE, summary, mtime). One parser for every surface:
        the verdict is line 1 upper-cased with everything but A-Z and _ removed."""
        f = self.status_path(sid)
        try:
            st = f.stat()
            lines = f.read_text(errors="replace").splitlines()
        except Exception:
            return "NONE", "", 0.0
        if not lines or not lines[0].strip():
            return "NONE", "", st.st_mtime
        v = re.sub(r"[^A-Z_]", "", lines[0].strip().upper()) or "NONE"
        return v, (lines[1].strip() if len(lines) > 1 else ""), st.st_mtime

    def write_status(self, sid: str, verdict: str, summary: str, outcome: str = "") -> None:
        summary = " ".join(str(summary).split())
        o = norm_outcome(outcome)
        _atomic_write(self.status_path(sid), f"{verdict}\n{summary}\n" + (f"OUTCOME {o}\n" if o else ""))

    def claim_outcome(self, sid: str) -> str:
        """Line 3 `OUTCOME <x>` of the claim, normalised; '' when absent. Fail-safe for a hand-written
        claim: any line after the verdict that starts OUTCOME counts (also `OUTCOME: x`, `OUTCOME=x`)."""
        try:
            lines = self.status_path(sid).read_text(errors="replace").splitlines()
        except Exception:
            return ""
        for line in lines[1:]:
            m = re.match(r"^\s*OUTCOME\s*[:=]?\s*([^\s:=]\S*)", line, re.I)
            if m:
                return norm_outcome(m.group(1))
        return ""

    def set_aside(self, sid: str, tag: str) -> Optional[Path]:
        """Move a claim aside (never delete it: it is the record of what was claimed)."""
        f = self.status_path(sid)
        if not f.exists():
            return None
        dst = self.dir / f"{sid}.{tag}.status"
        n = 1
        while dst.exists():
            n += 1
            dst = self.dir / f"{sid}.{tag}-{n}.status"
        os.replace(f, dst)
        return dst

    # -- engine verdicts ---------------------------------------------------
    def result(self, rid: str) -> Dict[str, Any]:
        try:
            return json.loads((self.dir / f"{rid}.result.json").read_text())
        except Exception:
            return {}

    def write_result(self, rid: str, data: Dict[str, Any]) -> None:
        data = dict(data, id=rid, updated=time.strftime("%F %T"))
        _atomic_write(self.dir / f"{rid}.result.json", json.dumps(data, indent=1, sort_keys=True))

    def clear_result(self, rid: str) -> None:
        f = self.dir / f"{rid}.result.json"
        if f.exists():
            os.replace(f, self.dir / f"{rid}.result.{int(time.time())}.json.old")

    # -- dispatch records --------------------------------------------------
    def dispatch(self, rid: str) -> Dict[str, Any]:
        try:
            return json.loads((self.dir / f"{rid}.dispatch.json").read_text())
        except Exception:
            return {}

    def write_dispatch(self, rid: str, data: Dict[str, Any]) -> None:
        _atomic_write(self.dir / f"{rid}.dispatch.json", json.dumps(data, indent=1, sort_keys=True))

    def attempt(self, rid: str) -> int:
        try:
            return int(self.dispatch(rid).get("attempt", 0))
        except Exception:
            return 0

    # -- small counters/flags ----------------------------------------------
    def flag(self, name: str) -> Path:
        return self.dir / name

    def has(self, name: str) -> bool:
        return (self.dir / name).exists()

    def touch(self, name: str, text: str = "") -> None:
        _atomic_write(self.dir / name, text or time.strftime("%F %T\n"))

    def rm(self, *names: str) -> None:
        for n in names:
            try:
                (self.dir / n).unlink()
            except FileNotFoundError:
                pass

    def read_int(self, name: str, default: int = 0) -> int:
        try:
            return int((self.dir / name).read_text().strip().split()[0])
        except Exception:
            return default

    def write_int(self, name: str, n: int) -> None:
        _atomic_write(self.dir / name, f"{n}\n")

    # -- logs --------------------------------------------------------------
    def event(self, rid: str, event: str, **detail: Any) -> None:
        """Append one line to attempts.jsonl under an exclusive lock."""
        rec = {"ts": time.strftime("%F %T"), "epoch": int(time.time()), "id": rid, "event": event}
        rec.update({k: v for k, v in detail.items() if v is not None})
        line = json.dumps(rec, sort_keys=True) + "\n"
        with open(self.dir / "attempts.jsonl", "a") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            f.write(line)
            fcntl.flock(f, fcntl.LOCK_UN)

    def events(self, rid: Optional[str] = None):
        try:
            with open(self.dir / "attempts.jsonl") as f:
                for line in f:
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue
                    if rid is None or rec.get("id") == rid:
                        yield rec
        except FileNotFoundError:
            return

    def lane_log(self, lane: str, msg: str) -> None:
        with open(self.dir / f"events_{lane}.log", "a") as f:
            f.write(f"{time.strftime('%F %T')} [{lane}] {msg}\n")

    def engine_log(self, msg: str) -> None:
        with open(self.dir / "engine.log", "a") as f:
            f.write(f"{time.strftime('%F %T')} {msg}\n")

    # -- once-only notifications -------------------------------------------
    def once(self, key: str) -> bool:
        """True the first time a key is seen (then remembered under state/notified/)."""
        d = self.dir / "notified"
        d.mkdir(exist_ok=True)
        f = d / re.sub(r"[^A-Za-z0-9_.-]", "_", key)
        if f.exists():
            return False
        f.write_text(time.strftime("%F %T\n"))
        return True

    def forget(self, prefix: str) -> None:
        d = self.dir / "notified"
        if not d.is_dir():
            return
        p = re.sub(r"[^A-Za-z0-9_.-]", "_", prefix)
        for f in d.glob(p + "*"):
            f.unlink(missing_ok=True)

    # -- adoption ----------------------------------------------------------
    def adopted_epoch(self) -> float:
        f = self.dir / "engine.adopted"
        try:
            return float(f.read_text().split()[0])
        except Exception:
            return 0.0

    def ensure_adopted(self) -> float:
        e = self.adopted_epoch()
        if e:
            return e
        e = time.time()
        _atomic_write(self.dir / "engine.adopted", f"{e:.0f} {time.strftime('%F %T')}\n")
        return e

    # -- the one 'done' question -------------------------------------------
    def phase(self, rid: str) -> str:
        """The row's phase as dependents see it.

        accepted   result.json says so, or a CLOSED claim older than the v2 adoption
                   with no result (grandfathered v1 close)
        claimed    a claim the engine has not judged yet
        ''         nothing
        """
        res = self.result(rid)
        v, _, mt = self.status(rid)
        if res:
            # a park holds against a claim written after it (a session still running when it was parked):
            # the claim waits on file for the unpark, which sets it aside with the reset
            if res.get("phase") == "parked":
                return "parked"
            # a result describes the claim it judged; a newer claim supersedes it
            if v != "NONE" and abs(float(res.get("status_mtime", 0)) - mt) > 1e-6 and mt > float(res.get("status_mtime", 0)):
                return "claimed"
            return str(res.get("phase", ""))
        if v == "NONE":
            return ""
        ad = self.adopted_epoch()
        if not self.dispatch(rid) and (not ad or mt <= ad):
            # a v1 claim from before this rig moved to v2: honoured as v1 left it —
            # CLOSED counts as done, BLOCKED/ROUTE_A stay halted until `orch reset`
            return {"CLOSED": TERMINAL_OK, "BLOCKED": "blocked", "ROUTE_A": "route_a"}.get(v, "halted")
        return "claimed"

    def done(self, rid: str) -> bool:
        return self.phase(rid) == TERMINAL_OK

    def outcome(self, rid: str) -> str:
        """The outcome of the accepted claim: the result's `outcome` (copied when the engine judged
        it), else line 3 of the claim (a grandfathered close). '' = no outcome."""
        res = self.result(rid)
        if "outcome" in res:
            return norm_outcome(res.get("outcome", ""))
        return self.claim_outcome(rid)

    def go_ok(self, rid: str) -> bool:
        """What a `needs=go` dependent asks of a dep: accepted with outcome go, or with none."""
        return self.done(rid) and not negative(self.outcome(rid))
