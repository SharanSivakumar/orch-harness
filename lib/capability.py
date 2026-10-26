#!/usr/bin/env python3
"""Capability check by process ancestry (v3, drop B item 4).

Operator power — the OPERATOR_ONLY commands, `orch park|unpark`, `orch inbox done`, `orch service`,
`orch revert`, an operator `plan:` commit, `ORCH_SCANNER_OVERRIDE` / `ORCH_SHARED_WHOLE`,
`orch guard force-release` — used to be refused only while ORCH_ROW was set, and a session can
unset a variable. The engine starts every session as the first process of a tmux pane and records
that pane's pid (the dispatch record's `ident` = "<window_id> <pane_pid>", and state/<sid>.window).
A process whose parent chain (/proc/<pid>/status PPid, up to pid 1) passes through one of those
pane pids runs INSIDE that session, whatever its environment says.

  inside(cfg)    -> {"sid", "kind", "pid", "how"} of the session the caller runs in, or None
  identity(cfg)  -> (sid, kind): the session's own id when the caller is inside one (an ORCH_ROW
                    naming another session is ignored), else ORCH_ROW / ORCH_KIND as in v2
  python3 capability.py who    prints "<sid> <kind>" and exits 0 inside a session; exits 1 outside

Which pane pids count: every state/<sid>.dispatch.json and state/<sid>.window of the instance,
open or closed (a lingering window is still that session; a session that flips its own record to
closed does not leave it) — except an ADOPTED record once closed (`orch adopt` usually adopts a
window the operator made; it is the operator's again when the row ends). Plus the user's pane
registry (~/.local/state/orch/panes/<pid>.json, written by the engine at every dispatch): a pane of
ANY rig whose instance, state, control, leases or git repo this instance shares — a decoy
ORCH_INSTANCE built on the real rig's files is still that rig — with that rig's own off switch.
A pid counts only while the process holding it started no later than
the record was written (+ a few seconds): a pid the kernel reused for someone else — after the
pane ended, or after a reboot — started later and is a stranger, so the operator's shell is never
mistaken for a session.

Second signal (`guard.capability_cgroup`, on): a process that left the parent chain (double fork,
`setsid`, `nohup … &` whose parent exits) is re-parented to pid 1 but keeps its cgroup. When a
live pane sits in a cgroup of its own (tmux ≥ 3.2 on systemd puts each pane in a
`tmux-spawn-*.scope`) and the caller is in that same cgroup, the caller is inside that session.
Without per-pane cgroups the signal is silent (never a refusal on its own).

Off switch: `guard.capability_check: false` (v2 behaviour: the environment alone decides).

The engine is not inside a pane (a systemd unit or the orch-engine window), and neither is the
operator's shell, so both are untouched. Known limits (docs/V3_DESIGN.md §8): a session of a rig
that shares none of this instance's files is the operator here (sandboxes a session builds); a
process started through the tmux server or systemd (`tmux new-window|split-window|run-shell|
send-keys`, `systemd-run`, at/cron) is outside the pane's tree and cgroup; a same-uid session can
still edit orch.yaml or delete its records — the review and the close checks remain the guard.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

SLACK = 5          # seconds a pane may appear to start after its record (int epochs, slow tmux)
MAX_DEPTH = 256

_KINDS = (("ub-", "unblock"), ("rv-", "review"), ("pf-", "preflight"), ("rr-", "roundreview"),
          ("sw-", "steward"), ("pl-", "planner"))


def kind_of(sid: str) -> str:
    """Same answer as engine.kind_of, without importing the engine."""
    for p, k in _KINDS:
        if sid.startswith(p):
            return k
    return "row"


# -- /proc ---------------------------------------------------------------------------------------
def _ppid(pid: int) -> int:
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("PPid:"):
                return int(line.split()[1])
    except Exception:
        pass
    return 0


def ancestors(pid: Optional[int] = None) -> List[int]:
    """pid itself, its parent, … up to (not including) pid 1."""
    out, p = [], pid or os.getpid()
    while p > 1 and p not in out and len(out) < MAX_DEPTH:
        out.append(p)
        p = _ppid(p)
    return out


_BTIME: Optional[float] = None


def _btime() -> float:
    global _BTIME
    if _BTIME is None:
        _BTIME = 0.0
        try:
            for line in Path("/proc/stat").read_text().splitlines():
                if line.startswith("btime"):
                    _BTIME = float(line.split()[1])
        except Exception:
            pass
    return _BTIME


def started(pid: int) -> float:
    """Epoch the process holding `pid` started; 0 when unknown (no such process)."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        fields = stat.rsplit(")", 1)[1].split()   # comm may hold spaces and parens
        return _btime() + int(fields[19]) / os.sysconf("SC_CLK_TCK")
    except Exception:
        return 0.0


def cgroup(pid: int) -> str:
    try:
        return Path(f"/proc/{pid}/cgroup").read_text().strip()
    except Exception:
        return ""


# -- the instance's session panes ----------------------------------------------------------------
def _num(x: Any) -> float:
    try:
        return float(x or 0)
    except (TypeError, ValueError):
        return 0.0


def panes(state: Path) -> Dict[int, Dict[str, Any]]:
    """pane pid -> {sid, kind, epoch} for every session record of the instance (open or closed;
    an ADOPTED record counts only while open — the window adopted was usually the operator's own)."""
    out: Dict[int, Dict[str, Any]] = {}
    gone = set()

    def add(pid_s: Any, sid: str, kind: str, epoch: Any) -> None:
        try:
            pid = int(pid_s)
        except (TypeError, ValueError):
            return
        ep = _num(epoch)
        if pid <= 1 or not sid or sid in gone:
            return
        cur = out.get(pid)
        if cur is None or ep > cur["epoch"]:      # the newest record of a pid (pids recycle)
            out[pid] = {"sid": sid, "kind": kind or kind_of(sid), "epoch": ep}

    recs = []
    for f in state.glob("*.dispatch.json"):
        try:
            d = json.loads(f.read_text())
        except Exception:
            continue
        if not isinstance(d, dict):
            continue
        sid = f.name[: -len(".dispatch.json")]
        if d.get("adopted") and not d.get("open"):
            gone.add(sid)
            continue
        recs.append((sid, d))
    for sid, d in recs:
        parts = str(d.get("ident") or "").split()
        if len(parts) >= 2:
            add(parts[1], sid, str(d.get("kind") or ""), d.get("epoch"))
    for f in state.glob("*.window"):
        try:
            parts = f.read_text().split()
        except Exception:
            continue
        if len(parts) >= 3:
            add(parts[1], f.name[: -len(".window")], "", parts[2])
    return out


def _genuine(pid: int, rec: Dict[str, Any]) -> bool:
    """The process at `pid` is the pane that record describes (not a later reuse of the pid). A
    record with no epoch cannot be told from a reuse and does not count."""
    s = started(pid)
    return bool(s) and bool(rec.get("epoch")) and s <= rec["epoch"] + SLACK


# -- the user's pane registry (every rig of this user) ---------------------------------------------
# The engine also records each pane in one per-user directory that no environment variable moves
# (the home directory comes from the password database, not $HOME): a session that points
# ORCH_INSTANCE at a decoy instance sharing the real rig's state, control, leases or git repo is
# still that rig's session there, and the real rig's off switch is the one that counts.
REGISTRY: Optional[Path] = None      # tests set this in-process; never from the environment


def registry_dir() -> Path:
    if REGISTRY is not None:
        return REGISTRY
    import pwd
    return Path(pwd.getpwuid(os.getuid()).pw_dir) / ".local" / "state" / "orch" / "panes"


def _real(p: Any) -> str:
    try:
        return os.path.realpath(str(p)) if p else ""
    except Exception:
        return ""


def touches(cfg) -> List[str]:
    """The real paths an instance acts on: itself, state, control, leases, its git repo."""
    out = [_real(getattr(cfg, a, "")) for a in ("instance", "state", "control", "leases", "git_repo")]
    return sorted({p for p in out if p})


def register(cfg, sid: str, kind: str, ident: str, adopted: bool = False) -> None:
    """Called by the engine when it opens a session window (and by `orch adopt`). Never raises."""
    try:
        parts = str(ident or "").split()
        pid = int(parts[1])
        d = registry_dir()
        d.mkdir(parents=True, exist_ok=True)
        for f in d.glob("*.json"):                    # prune panes that are gone
            if f.stem.isdigit() and not Path(f"/proc/{f.stem}").exists():
                f.unlink(missing_ok=True)
        rec = {"pid": pid, "sid": sid, "kind": kind or kind_of(sid), "epoch": time.time(), "adopted": adopted,
               "instance": _real(cfg.instance), "touches": touches(cfg)}
        tmp = d / f".{pid}.{os.getpid()}.tmp"
        tmp.write_text(json.dumps(rec))
        os.replace(tmp, d / f"{pid}.json")
    except Exception:
        pass


def _owner_on(rec: Dict[str, Any]) -> bool:
    """The off switch and the adopted rule are read from the rig that owns the pane."""
    inst = Path(str(rec.get("instance") or ""))
    try:
        import yaml
        data = yaml.safe_load((inst / "orch.yaml").read_text()) or {}
        if not bool(((data.get("guard") or {}).get("capability_check", True))):
            return False
    except Exception:
        pass                                          # an owner we cannot read stays on
    if rec.get("adopted"):
        try:
            d = json.loads((inst / "state" / f"{rec.get('sid')}.dispatch.json").read_text())
            return isinstance(d, dict) and bool(d.get("open"))
        except Exception:
            return False
    return True


def registered(cfg) -> Dict[int, Dict[str, Any]]:
    """pane pid -> registry record, for the panes of every rig whose files this instance shares."""
    mine = set(touches(cfg))
    out: Dict[int, Dict[str, Any]] = {}
    try:
        files = list(registry_dir().glob("*.json"))
    except Exception:
        return out
    for f in files:
        try:
            rec = json.loads(f.read_text())
        except Exception:
            continue
        if not isinstance(rec, dict) or not f.stem.isdigit() or str(rec.get("pid")) != f.stem:
            continue
        if not mine & set(rec.get("touches") or []):
            continue
        rec["epoch"] = _num(rec.get("epoch"))
        rec["kind"] = str(rec.get("kind") or kind_of(str(rec.get("sid") or "")))
        if rec.get("sid"):
            out[int(f.stem)] = rec
    return out


def enabled(cfg) -> bool:
    g = (getattr(cfg, "data", {}) or {}).get("guard") or {}
    return bool(g.get("capability_check", True))


def _find(recs: Dict[int, Dict[str, Any]], chain: List[int], use_cgroup: bool, how: str):
    for p in chain:                                  # the nearest pane wins
        rec = recs.get(p)
        if rec and _genuine(p, rec):
            return dict(sid=rec["sid"], kind=rec["kind"], epoch=rec["epoch"], pid=p, how=how)
    if use_cgroup:
        mine = cgroup(chain[0] if chain else os.getpid())
        if mine:
            for p, rec in recs.items():
                if not _genuine(p, rec):
                    continue
                cg = cgroup(p)
                if cg and cg == mine and cg != cgroup(_ppid(p)):
                    return dict(sid=rec["sid"], kind=rec["kind"], epoch=rec["epoch"], pid=p, how=f"{how}-cgroup")
    return None


def inside(cfg, pid: Optional[int] = None) -> Optional[Dict[str, Any]]:
    g = (getattr(cfg, "data", {}) or {}).get("guard") or {}
    use_cg = bool(g.get("capability_cgroup", True))
    chain = ancestors(pid)
    if enabled(cfg):
        s = _find(panes(Path(cfg.state)), chain, use_cg, "ancestor")
        if s:
            return s
    reg = {p: r for p, r in registered(cfg).items() if _owner_on(r)}
    if reg:
        s = _find(reg, chain, use_cg, "registry")
        if s:
            return s
    return None


def identity(cfg) -> Tuple[str, str]:
    """(sid, kind) of the caller: the session it runs in, else the v2 environment."""
    try:
        s = inside(cfg)
    except Exception as e:      # a defect here must be seen, not silently become "the operator"
        print(f"[orch] ⚠  capability check failed ({type(e).__name__}: {e}) — the environment decides",
              file=sys.stderr)
        s = None
    if s:
        return s["sid"], s["kind"]
    return os.environ.get("ORCH_ROW", ""), os.environ.get("ORCH_KIND", "")


def session(cfg) -> str:
    """The caller's session id ('' for the operator's shell and the engine)."""
    return identity(cfg)[0]


def main(argv: List[str]) -> int:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from config_parser import load
    cfg = load()
    if argv[:1] == ["who"]:
        # exit 0 inside a session ("<sid> <kind> <how>"), 1 outside, 2 outside with the check off
        # (the guard then keeps v2's behaviour), 3 when the check itself failed
        try:
            s = inside(cfg)
        except Exception as e:
            print(f"[orch] ⚠  capability check failed ({type(e).__name__}: {e})", file=sys.stderr)
            return 3
        if s:
            print(f"{s['sid']} {s['kind']} {s['how']}")
            return 0
        return 1 if enabled(cfg) else 2
    print("usage: capability.py who")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
