#!/usr/bin/env python3
"""Engine re-arm: a row asks, the engine resets.

Course-loop and planner rows must re-run a judge after they cut fix rows, and
must start the round that holds those rows. `orch reset` and `orch round start`
are the operator's commands and stay refused inside every session. Instead:

  queue line extra   rearm=<row-id>[,<row-id>...][,round]
                     written by the planner/operator. `round` lets the row run
                     `orch round new` / `orch round add-lanes` itself and request
                     `orch round start` from the engine.
  row command        orch rearm <row-id> "<reason>"
                     orch rearm round-start <tag> "<reason>"

The command only writes a request file `state/<caller>.rearm.<slug>.request`.
The engine re-validates it on its next tick and then runs exactly the operator's
code path (`orchctl.reset` / `orch round start`), logs `REARM …` to both lanes,
and moves the request to `.done` or `.refused`.

Authority comes from the engine's DISPATCH SNAPSHOT of the calling row
(`state/<caller>.dispatch.json` → snapshot.meta.rearm), never from the live queue
file: a session that edits its own queue line after dispatch gains nothing.
Only a live row session may ask (never ub-/rv-/pf-/rr-), never for itself, never
for a target that is running, and at most `scheduler.rearm_max` (default 5)
times per caller/target pair.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROUND = "round"
ROUND_START = "round-start"
ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
TAG_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")


def parse_grants(value: str) -> Tuple[List[str], List[str]]:
    """'p0J3,round' -> (grants, problems)."""
    out, bad = [], []
    for g in [x for x in re.split(r"[,\s]+", value or "") if x]:
        if g == ROUND or ID_RE.fullmatch(g):
            out.append(g)
        else:
            bad.append(g)
    return out, bad


def snapshot_grants(st, caller: str) -> List[str]:
    d = st.dispatch(caller)
    meta = ((d.get("snapshot") or {}).get("meta") or {})
    return parse_grants(str(meta.get("rearm", "")))[0]


def _kind(sid: str) -> str:
    from engine import kind_of
    return kind_of(sid)


def caller_ok(st, caller: str, kind_env: str) -> Tuple[bool, str]:
    if not caller:
        return False, "orch rearm is for row sessions; the operator uses orch reset / orch round start directly"
    if _kind(caller) != "row" or (kind_env or "row") != "row":
        return False, f"session {caller} is not a row session; only a row may ask for a re-arm"
    d = st.dispatch(caller)
    if not d.get("open"):
        return False, f"{caller} has no live dispatch; only a running row may ask for a re-arm"
    return True, ""


def target_busy(rig, target: str) -> str:
    """'' when the target may be reset now, else why not."""
    st = rig.st
    if st.dispatch(target).get("open"):
        return f"{target} is running (open dispatch)"
    for sid in rig.inflight():
        if sid.startswith((f"ub-{target}-", f"rv-{target}-")):
            return f"{target} has a live sub-session {sid}"
    if st.phase(target) == "claimed":
        return f"{target} has a claim the engine has not judged yet"
    return ""


def slug_for(target: str, tag: str = "") -> str:
    return f"{ROUND_START}.{tag}" if target == ROUND_START else target


def validate(rig, caller: str, target: str, tag: str = "") -> Tuple[bool, str]:
    """Shared by the CLI and the engine. The caller's grants come from its dispatch snapshot."""
    grants = snapshot_grants(rig.st, caller)
    if target == ROUND_START:
        if ROUND not in grants:
            return False, f"{caller}'s dispatch snapshot carries no rearm=round grant"
        if not TAG_RE.fullmatch(tag or ""):
            return False, f"round tag '{tag}' is malformed"
        return True, ""
    if target == caller:
        return False, f"a row may not re-arm itself ({caller})"
    if target not in grants:
        return False, (f"{caller}'s dispatch snapshot has no rearm={target} grant "
                       f"(grants: {','.join(grants) or 'none'}); the queue line is read at dispatch only")
    from rows import find_row
    if find_row(rig.cfg, target) is None:
        return False, f"unknown row '{target}'"
    import courseloop
    owned = courseloop.owns_negative(rig.cfg, target, rig.st)   # the native loop's judge (drop D)
    if owned:
        return False, owned
    res = rig.st.result(target)
    import steward
    if rig.st.phase(target) == "parked" and steward.owner_held(res):
        return False, f"{target} was parked by the Owner (or because its bar moved) — only the Owner unparks it"
    busy = target_busy(rig, target)
    if busy:
        return False, busy
    return True, ""


def request(rig, caller: str, kind_env: str, target: str, tag: str, reason: str) -> Tuple[bool, str]:
    ok, why = caller_ok(rig.st, caller, kind_env)
    if ok:
        ok, why = validate(rig, caller, target, tag)
    if not ok:
        return False, why
    slug = slug_for(target, tag)
    f = rig.cfg.state / f"{caller}.rearm.{slug}.request"
    rec = {"caller": caller, "target": target, "tag": tag, "reason": " ".join(reason.split()),
           "epoch": int(time.time()), "ts": time.strftime("%F %T")}
    tmp = f.with_name(f.name + ".tmp")
    tmp.write_text(json.dumps(rec, indent=1, sort_keys=True))
    os.replace(tmp, f)
    return True, f.name


def _cap(rig) -> int:
    try:
        return int((rig.cfg.data.get("scheduler") or {}).get("rearm_max", 5))
    except (TypeError, ValueError):
        return 5


def _finish(f: Path, rec: Dict[str, Any], outcome: str, why: str) -> None:
    rec.update(outcome=outcome, why=why, handled=time.strftime("%F %T"))
    dest = f.with_name(f.name[: -len(".request")] + f".{int(time.time())}.{outcome}")
    dest.write_text(json.dumps(rec, indent=1, sort_keys=True))
    f.unlink(missing_ok=True)


def consume(rig) -> None:
    """Engine tick phase: handle every pending request once."""
    from rows import find_row
    for f in sorted(rig.cfg.state.glob("*.rearm.*.request")):
        try:
            rec = json.loads(f.read_text())
        except Exception:
            _finish(f, {"raw": f.name}, "refused", "unreadable request")
            continue
        caller, target, tag = str(rec.get("caller", "")), str(rec.get("target", "")), str(rec.get("tag", ""))
        reason = str(rec.get("reason", ""))
        if not f.name.startswith(f"{caller}.rearm."):
            _finish(f, rec, "refused", "request file name does not match its caller")
            continue
        crow = find_row(rig.cfg, caller)
        clane = crow.lane if crow else "-"
        ok, why = (True, "") if _kind(caller) == "row" else (False, "caller is not a row")
        if ok:
            ok, why = validate(rig, caller, target, tag)
        slug = slug_for(target, tag)
        cnt_name = f"{caller}.rearm.{slug}.count"
        n = rig.st.read_int(cnt_name)
        if ok and n >= _cap(rig):
            ok, why = False, f"cap reached: {n} re-arms of {slug} by {caller} (scheduler.rearm_max {_cap(rig)})"
            rig.notify("critical", f"{rig.cfg.name}: re-arm cap {caller} → {slug}", why)
        if not ok:
            rig.log(clane, f"REARM REFUSED {slug} asked by {caller} — {why}")
            rig.st.event(caller, "rearm_refused", target=slug, detail=why)
            _finish(f, rec, "refused", why)
            continue
        if target == ROUND_START:
            done, out = _round_start(rig, tag)
            if not done:
                rig.log(clane, f"REARM REFUSED round start {tag} asked by {caller} — {out}")
                rig.st.event(caller, "rearm_refused", target=slug, detail=out)
                _finish(f, rec, "refused", out)
                continue
            rig.st.write_int(cnt_name, n + 1)
            rig.log(clane, f"REARM round start {tag} by {caller} — {reason}")
            rig.st.event(caller, "rearm", target=slug, reason=reason)
            _finish(f, rec, "done", out)
            continue
        trow = find_row(rig.cfg, target)
        import orchctl
        try:
            orchctl.reset(rig, target, by=f"rearm by {caller}")
        except BaseException as e:   # reset() dies via SystemExit on refusal
            why = f"reset failed: {e}"
            rig.log(clane, f"REARM REFUSED {target} asked by {caller} — {why}")
            _finish(f, rec, "refused", why)
            continue
        rig.st.write_int(cnt_name, n + 1)
        msg = f"REARM {target} by {caller} — {reason}"
        rig.log(trow.lane if trow else "-", msg)
        if trow is None or trow.lane != clane:
            rig.log(clane, msg)
        rig.st.event(target, "rearm", by=caller, reason=reason)
        _finish(f, rec, "done", "reset")


def _round_start(rig, tag: str) -> Tuple[bool, str]:
    exe = rig.cfg.instance / "orch"
    if not exe.exists():
        from engine import LIB
        exe = LIB.parent / "bin" / "orch"
    env = dict(os.environ, ORCH_INSTANCE=str(rig.cfg.instance))
    for k in ("ORCH_ROW", "ORCH_KIND", "ORCH_PLAN_DIR"):
        env.pop(k, None)
    try:
        p = subprocess.run([str(exe), "round", "start", tag], capture_output=True, text=True, env=env,
                           cwd=str(rig.cfg.instance), timeout=120)
    except Exception as e:
        return False, f"round start failed: {e}"
    out = " ".join((p.stdout + p.stderr).split())[-400:]
    return p.returncode == 0, out
