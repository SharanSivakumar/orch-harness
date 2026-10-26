#!/usr/bin/env python3
"""`orch` engine surfaces (v2): start/stop/pause/resume, assign, approve, dispatch,
done/blocked/verdict, reset, adopt, lease, prompt, engine, migrate.

Every command acts through the same `Rig` the engine uses, so a CLI action and an
engine action can never disagree about what a row is.
"""
from __future__ import annotations

import fcntl
import hashlib
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tmuxio  # noqa: E402
import template_engine as te  # noqa: E402
from config_parser import load  # noqa: E402
from engine import Rig, kind_of, parent_of, plan_sha, SUB_VERDICTS, LIB  # noqa: E402
from rows import active_lanes, find_row, load_rows  # noqa: E402

FRAMEWORK = LIB.parent


def die(msg: str, code: int = 1) -> None:
    print(msg)
    raise SystemExit(code)


# Every session the engine starts carries ORCH_ROW=<its own id> (and ORCH_KIND). The
# operator's shell does not. Commands that steer the rig are the operator's; a session
# may only claim ITSELF. v3 (lib/capability.py): a process that descends from a session's
# pane IS that session, with or without ORCH_ROW — unsetting the variable no longer reaches
# operator power. (Same-uid escapes remain; the review and close checks guard the rest.)
OPERATOR_ONLY = {"start", "night", "day", "pause", "resume", "stop", "unstop", "disable", "approve",
                 "dispatch", "reset", "adopt", "engine", "migrate", "service", "revert"}

_IDENT = None


def identity():
    """(session id, kind) of the caller — ('', '') for the operator's shell and the engine."""
    global _IDENT
    if _IDENT is None:
        import capability
        try:
            cfg = load()
        except (SystemExit, Exception):
            cfg = None
        _IDENT = (capability.identity(cfg) if cfg is not None
                  else (os.environ.get("ORCH_ROW", ""), os.environ.get("ORCH_KIND", "")))
    return _IDENT


def caller() -> str:
    return identity()[0]


def operator_only(cmd: str) -> None:
    if caller():
        hint = (" A course-loop/planner row asks the engine instead: `orch rearm <id> \"reason\"` "
                "(needs rearm=<id> on its queue line)." if cmd == "reset" else "")
        how = ("" if os.environ.get("ORCH_ROW") == caller() else
               f" (this process descends from {caller()}'s pane; unsetting or changing ORCH_ROW does not "
               f"make it the operator)")
        die(f"REFUSED: `orch {cmd}` is the operator's command; session {caller()} may not run it{how}.{hint} "
            f"If your work needs it, say so in PROGRESS.md and block.")


# -- engine liveness ---------------------------------------------------------------
def engine_running(rig: Rig) -> bool:
    f = rig.cfg.state / "engine.lock"
    if not f.exists():
        return False
    with open(f, "a") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fh, fcntl.LOCK_UN)
            return False
        except OSError:
            return True


def v1_watchers(rig: Rig) -> List[str]:
    """v1 lane watchers of THIS instance still running (they must stop before v2 runs)."""
    out = []
    mine = str(rig.cfg.instance.resolve())
    p = subprocess.run(["ps", "-eo", "pid=,args="], capture_output=True, text=True)
    for line in p.stdout.splitlines():
        pid, _, args = line.strip().partition(" ")
        m = re.search(r"orchestrator_core\.sh run (\S+)", args)
        if not m:
            continue
        try:
            env = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
            inst = next((e[len(b"ORCH_INSTANCE="):].decode() for e in env if e.startswith(b"ORCH_INSTANCE=")), "")
        except Exception:
            inst = ""
        if not inst or str(Path(inst).resolve()) == mine:
            out.append(f"{m.group(1)} (pid {pid})")
    return out


def framework_state() -> tuple:
    """-> (sha, dirty files, tested?)"""
    def git(*a):
        return subprocess.run(["git", "-C", str(FRAMEWORK), *a], capture_output=True, text=True).stdout.strip()
    sha = git("rev-parse", "--short", "HEAD")
    dirty = [l for l in git("status", "--porcelain").splitlines()
             if l.strip() and "__pycache__" not in l and ".tested/" not in l]
    tested = (FRAMEWORK / ".tested" / git("rev-parse", "HEAD")).exists()
    return sha, dirty, tested


def start(rig: Rig, lanes: List[str]) -> None:
    sha, dirty, tested = framework_state()
    allow = rig.cfg.data.get("framework", {}).get("allow_dirty") or os.environ.get("ORCH_ALLOW_DIRTY") == "1"
    if dirty and not allow:
        die(f"REFUSED: the framework tree {FRAMEWORK} has {len(dirty)} uncommitted change(s) "
            f"(first: {dirty[0].strip()}).\n  A rig runs on committed, tested framework code. Commit and run "
            f"tests/run_all.sh, or ORCH_ALLOW_DIRTY=1 on the record.")
    if not tested:
        print(f"WARNING: framework commit {sha} has not passed tests/run_all.sh on this host.")
    old = v1_watchers(rig)
    if old:
        die("REFUSED: v1 lane watchers of this rig are still running: " + ", ".join(old) +
            "\n  Stop them (old framework: `orch stop`, wait for HALTED (STOP), then `orch unstop`), then `orch migrate`.")
    rig.st.ensure_adopted()
    all_lanes = active_lanes(rig.cfg)
    want = lanes or all_lanes
    unknown = [l for l in want if l not in all_lanes]
    if unknown:
        die(f"no active queue for lane(s): {' '.join(unknown)}")
    for l in want:
        rig.ctl(f"RUN_{l}").write_text(time.strftime("%F %T\n"))
    print(f"lanes enabled: {' '.join(want)}")
    tmuxio.ensure_session(rig.session, str(rig.cfg.root))
    import service
    if engine_running(rig):
        print(f"engine already running ({'systemd unit ' + service.unit_name(rig.cfg, 'engine') if service.active(rig.cfg) else 'window orch-engine in tmux ' + rig.session})")
        service.ensure_viewer(rig)
    elif service.installed(rig.cfg):
        ok, out = service.start_unit(rig.cfg)
        for _ in range(40):
            time.sleep(0.25)
            if engine_running(rig):
                break
        service.ensure_viewer(rig)
        print(f"engine {'started' if engine_running(rig) else 'NOT confirmed'} as systemd unit "
              f"{service.unit_name(rig.cfg, 'engine')} (framework {sha}){'' if ok else ' — ' + out}")
    else:
        import shlex
        cmd = f"ORCH_INSTANCE={shlex.quote(str(rig.cfg.instance))} python3 {shlex.quote(str(LIB / 'engine.py'))}"
        wins = tmuxio.windows(rig.session)
        if "orch-engine" in wins:
            tmuxio.respawn(rig.session, "orch-engine", cmd)
        else:
            tmuxio.new_window(rig.session, "orch-engine", str(rig.cfg.root), cmd)
        for _ in range(20):
            time.sleep(0.25)
            if engine_running(rig):
                break
        print(f"engine {'started' if engine_running(rig) else 'NOT confirmed — check tmux window orch-engine'} "
              f"(framework {sha})")
    if rig.paused(""):
        print("note: control/PAUSE is set — nothing new dispatches until `orch resume` / `orch night`.")
    if rig.ctl("STOP").exists():
        print("note: control/STOP is set — nothing dispatches until `orch unstop`.")
    print(f"Watch live:  tmux attach -t {rig.session}     Status: orch status")


# -- row commands --------------------------------------------------------------------
def need_row(rig: Rig, rid: str):
    r = find_row(rig.cfg, rid)
    if r is None:
        die(f"unknown row '{rid}'")
    return r


def live_ours(rig: Rig, sid: str) -> bool:
    d = rig.st.dispatch(sid)
    w = tmuxio.windows(rig.session).get(sid)
    return bool(w and w["alive"] and d.get("open") and w.ident == d.get("ident"))


def assign(rig: Rig, rid: str, x: str, by: str = "operator") -> None:
    r = need_row(rig, rid)
    me = caller()
    if me and not (kind_of(me) == "unblock" and parent_of(me) == rid):
        die(f"REFUSED: session {me} may not retier {rid} (only the operator, or {rid}'s own unblocker)")
    if rig.st.done(rid):
        die(f"REFUSED: {rid} is accepted — 'orch reset {rid}' first to re-run under a new executor")
    if live_ours(rig, rid):
        die(f"REFUSED: {rid} has a LIVE session. A running session is never yanked.\n"
            f"To retier now: tmux kill-window -t {rig.session}:{rid} && orch reset {rid} && orch assign {rid} {x}")
    if x == "default":
        rig.ctl(f"EXECUTOR_{rid}").unlink(missing_ok=True)
        print(f"{rid} -> queue default ({r.queue_exec})")
    else:
        if x not in rig.cfg.data["executors"]["allowed"]:
            die(f"unknown executor '{x}' (have: {' '.join(rig.cfg.data['executors']['allowed'])}, or 'default')")
        rig.ctl(f"EXECUTOR_{rid}").write_text(f"{x}\n")
        print(f"{rid} -> {x}. Takes effect at its next dispatch.")
    rig.log(r.lane, f"ASSIGN: {rid} -> {x}{'' if by == 'operator' else ' (' + by + ')'}")
    rig.st.event(rid, "assign", to=x, by=None if by == "operator" else by)


def approve(rig: Rig, rid: str, by: str = "operator") -> None:
    r = need_row(rig, rid)
    if rig.st.phase(rid) == "parked" and not caller():
        import steward
        ok, how = steward.unpark(rig, rid, by="owner (approve)")
        print(f"{rid} was parked — {how}")
    sha = plan_sha(rig.cfg, r)
    rig.ctl(f"GO_{rid}").write_text(f"sha256 {sha}\napproved {time.strftime('%F %T')}\n" if sha else "")
    rig.st.forget(f"gate-{rid}")
    rig.st.event(rid, "approve", plan_sha=sha[:12] or None, by=None if by == "operator" else by)
    print(f"GO_{rid} set (bound to the plan as it stands: sha256 {sha[:12] or '?'} — an edited plan re-gates)")


def dispatch(rig: Rig, rid: str) -> None:
    r = need_row(rig, rid)
    if rig.st.done(rid):
        die(f"{rid} already accepted — 'orch reset {rid}' first")
    ph = rig.st.phase(rid)
    if ph not in ("", "retry"):
        die(f"{rid} is in phase '{ph}' — 'orch reset {rid}' first (a halted or in-flight row is never re-fired over its state)")
    if live_ours(rig, rid) or rid in rig.inflight():
        die(f"{rid} is already in flight — watch it instead")
    import phases
    blk = phases.Fence(rig.cfg).block(r.lane, ph, new_start=not rig.st.dispatch(rid))
    if blk:                     # FORCE=1 does not cross it: re-open the phase (`orch phase open`) instead
        die(f"REFUSED: {rid}: {blk[len('PHASE — '):]}")
    if r.gate:
        ok, why = rig.gate_open(r)
        if not ok:
            die(f"{rid}: {why}")
    from rows import implicit_deps, unmet_deps
    rows, _ = load_rows(rig.cfg)
    unmet = unmet_deps(rig.st, r, implicit_deps(rig.cfg, rows))
    if unmet:
        how = "not accepted" + (" (a dep:outcome is accepted with a negative outcome; the row has needs=go)"
                                if any(":" in u for u in unmet) else "")
        if os.environ.get("FORCE") == "1":
            print(f"WARNING: deps [{','.join(unmet)}] {how} — dispatching anyway (FORCE=1)")
        else:
            die(f"deps [{','.join(unmet)}] {how} — order is binding. Override: FORCE=1 orch dispatch {rid}")
    x, prob = rig.resolve_exec(r)
    if prob:
        die(prob)
    newly: List[str] = []
    if r.resources:
        ok, newly, why = rig.leases.acquire_all(rid, r.resources)
        if not ok:
            die(f"REFUSED: {why}")
    ok, how = rig.dispatch_row(r, x, manual=True)
    if not ok:
        rig.leases.release(rid, newly)
        die(f"not dispatched: {how}")
    print(f"dispatched {rid} (executor {x}){'' if x != 'manual' else ' — manual work order announced'}")


def split_outcome(args: List[str]) -> tuple:
    """`orch done <id> "…" --outcome go|nogo|held|fail|infeasible` (or --outcome=x, anywhere after the id)."""
    rest, outcome, i = [], "", 0
    while i < len(args):
        a = args[i]
        if a == "--outcome":
            if i + 1 >= len(args):
                die("usage: orch done <id> \"summary\" --outcome go|nogo|held|fail|infeasible")
            outcome, i = args[i + 1], i + 2
            continue
        if a.startswith("--outcome="):
            outcome = a.split("=", 1)[1]
        else:
            rest.append(a)
        i += 1
    if outcome:
        from store import OUTCOMES, norm_outcome
        if norm_outcome(outcome) not in OUTCOMES:
            die(f"REFUSED: --outcome '{outcome}' — one of {'|'.join(OUTCOMES)}")
        outcome = norm_outcome(outcome)
    return rest, outcome


def mark(rig: Rig, verdict: str, rid: str, summary: str, outcome: str = "") -> None:
    r = need_row(rig, rid)
    me = caller()
    word = {"CLOSED": "done", "BLOCKED": "blocked", "ROUTE_A": "escalate"}[verdict]
    if me and me != rid:
        die(f"REFUSED: session {me} may only claim itself, never {rid}")
    if rig.st.done(rid):
        die(f"{rid} is already accepted — nothing to do")
    ph = rig.st.phase(rid)
    if ph not in ("", "retry"):
        die(f"REFUSED: {rid} is in phase '{ph}'. A claim is already being judged or the row is halted — "
            f"the operator resets it first (orch reset {rid}).")
    if live_ours(rig, rid) and me != rid and os.environ.get("FORCE") != "1":
        die(f"REFUSED: {rid} has a live session that closes itself. If it is truly dead and you are closing it "
            f"yourself: FORCE=1 orch {word} {rid} \"…\"")
    if r.gate and not rig.gate_open(r)[0] and not me:
        die(f"REFUSED: {rid} is behind its gate — {rig.gate_open(r)[1]}")
    d = rig.st.dispatch(rid)
    if not d or not d.get("open"):
        if me:
            die(f"REFUSED: {rid} has no open session record — a session claims only the attempt it was dispatched for")
        from closepipe import git_head, probe_snapshot, protected_snapshot
        x, prob = rig.resolve_exec(r)
        if prob:
            die(f"REFUSED: {prob}")
        head = git_head(rig.cfg.git_repo)
        d = {"sid": rid, "kind": "row", "executor": x, "lane": r.lane, "attempt": rig.st.attempt(rid) + 1,
             "open": False, "ts": time.strftime("%F %T"), "epoch": int(time.time()),
             "head": head, "closed_by_cli": True,
             "snapshot": {"review": rig.review_needed(r, x), "probe": probe_snapshot(rig.cfg, r),
                          "protected": protected_snapshot(rig.cfg, rid, head)}}
        rig.st.write_dispatch(rid, d)
    if verdict == "CLOSED":
        import closepipe
        res = closepipe.check_claim(rig.cfg, r, summary, d)
        for c in res["checks"]:
            print(f"  {'ok  ' if c['ok'] else ('FAIL' if c['blocking'] else 'note')}  {c['name']}: {c['detail']}")
        if not res["ok"]:
            rig.log(r.lane, f"CLI close of {rid} REFUSED by the close checks")
            die(f"REFUSED: {rid} cannot be claimed CLOSED yet — fix the FAIL lines above, or close it BLOCKED "
                f"(orch blocked {rid} \"…\"). Nothing was written.", 3)
    rig.st.write_status(rid, verdict, summary, outcome if verdict == "CLOSED" else "")
    rig.log(r.lane, f"CLI wrote {verdict}{':' + outcome if outcome and verdict == 'CLOSED' else ''} for {rid} — {summary}")
    if verdict == "CLOSED":
        pol = rig.cfg.data.get("review", {}).get("policy", "all")
        print(f"{rid} claimed CLOSED. The engine re-runs these checks"
              f"{' and dispatches an independent review' if pol != 'none' else ''} before dependents start.")
    else:
        print(f"{rid} claimed {verdict}. The engine takes it from here.")
    if me == rid:
        print("Your work here is done: make no further tool calls.")


def verdict(rig: Rig, sid: str, v: str, summary: str) -> None:
    kind = kind_of(sid)
    if kind == "row":
        die(f"'{sid}' is a row — use orch done / orch blocked")
    v = v.upper()
    if caller() and caller() != sid:
        die(f"REFUSED: session {caller()} may record only its own verdict, never {sid}'s")
    if not caller() and os.environ.get("FORCE") != "1":
        die(f"REFUSED: verdicts come from the dispatched {kind} session. An operator override is "
            f"FORCE=1 orch verdict {sid} … (on the record).")
    ok = SUB_VERDICTS[kind]
    if kind == "steward":
        import steward
        ok = steward.verdicts_for(sid)
    if v not in ok:
        die(f"{sid} is a {kind} session: verdict must be one of {', '.join(ok)}")
    if kind == "review":
        f = rig.cfg.instance / "reviews" / f"{sid[3:]}.md"
        if not f.is_file() or f.stat().st_size < 40:
            die(f"REFUSED: write your findings to {te.rel_to_root(rig.cfg, f)} first — the verdict is only as good "
                f"as the evidence a reader can check.", 3)
    if kind == "preflight":
        scope = sid[3:]
        f = rig.cfg.instance / "preflight" / f"{scope}.review.md"
        if not f.is_file() or f.stat().st_size < 40:
            die(f"REFUSED: write your findings to {te.rel_to_root(rig.cfg, f)} first.", 3)
    if kind == "roundreview":
        import rounds
        d = rounds.review_dir(rig.cfg, sid[3:])
        if not d.is_dir() or not any(d.iterdir()):
            die(f"REFUSED: write the review brief into {te.rel_to_root(rig.cfg, d)}/ first.", 3)
    if kind in ("steward", "planner"):
        import steward
        f = steward.evidence_file(rig.cfg, sid) if kind == "steward" else steward.planner_evidence(rig.cfg, sid)
        if not f.is_file() or f.stat().st_size < 40:
            die(f"REFUSED: write your analysis to {te.rel_to_root(rig.cfg, f)} first — the Owner reads it in the digest.", 3)
    if summary.strip() == "" or te.LEFTOVER.search(summary) or re.search(r"<[A-Za-z][^<>]*>", summary):
        die("REFUSED: give a real one-sentence summary")
    rig.st.write_status(sid, v, summary)
    print(f"{sid}: {v} recorded. Make no further tool calls.")


def reset(rig: Rig, rid: str, by: str = "operator") -> None:
    r = need_row(rig, rid)
    st = rig.st
    st.set_aside(rid, f"reset{int(time.time())}")
    st.clear_result(rid)
    for n in ("unblock_n", "retiered", "check_fails", "review_rejects", "review_deaths", "idle_nudges",
              "idle_last", "limited", "rl", "probe_passed", "check.json", "orphan_notified", "fast_deaths"):
        st.rm(f"{rid}.{n}")
    stamp = time.strftime('%Y%m%dT%H%M%S')
    wins = tmuxio.windows(rig.session)
    for pre in (f"ub-{rid}-", f"rv-{rid}-"):
        for f in list(rig.cfg.state.glob(f"{pre}*.status")) + list(rig.cfg.state.glob(f"{pre}*.dispatch.json")):
            if re.fullmatch(rf"{re.escape(pre)}[0-9]+(r[0-9]+)?\.(status|dispatch\.json)", f.name):
                os.replace(f, f.with_name(f.name + f".reset-{stamp}"))
        for name, w in wins.items():   # sub-sessions of the old attempt stop working it
            if name.startswith(pre) and w["alive"]:
                tmuxio.kill(rig.session, w["wid"])
    d = st.dispatch(rid)
    if d.get("open"):
        d.update(open=False, end="reset", ended_epoch=int(time.time()))
        st.write_dispatch(rid, d)
    rig.leases.release(rid, r.resources)
    st.forget(f"timeout-{rid}")
    st.event(rid, "reset", by=None if by == "operator" else by)
    rig.log(r.lane, f"RESET {rid} ({by})")
    print(f"reset {rid} — the engine re-dispatches it when its deps, gate and capacity allow "
          f"(an executor override is KEPT; clear with 'orch assign {rid} default')")


def adopt(rig: Rig, rid: str) -> None:
    r = need_row(rig, rid)
    w = tmuxio.windows(rig.session).get(rid)
    if not (w and w["alive"]):
        die(f"no live window named '{rid}' in session {rig.session}")
    d = rig.st.dispatch(rid) or {"sid": rid, "kind": "row", "lane": r.lane, "attempt": rig.st.attempt(rid) + 1,
                                 "epoch": int(time.time()), "ts": time.strftime("%F %T")}
    d.update(ident=w.ident, open=True, adopted=True)
    d.setdefault("executor", rig.resolve_exec(r)[0])
    rig.st.write_dispatch(rid, d)
    (rig.cfg.state / f"{rid}.window").write_text(f"{w.ident} {int(time.time())}\n")
    import capability
    capability.register(rig.cfg, rid, "row", w.ident, adopted=True)
    rig.log(r.lane, f"operator ADOPTED the live window as {rid}'s session")
    print(f"adopted: window '{rid}' now counts as {rid}'s session. Only for a session genuinely working the row.")


def prompt(rig: Rig, ids: List[str], all_pending: bool) -> None:
    rows, by_id = load_rows(rig.cfg)
    if all_pending:
        ids = [r.id for r in rows if not r.archived and not rig.st.done(r.id) and r.id not in rig.inflight()]
    for rid in ids:
        r = by_id.get(rid)
        if not r:
            print(f"unknown row '{rid}'")
            continue
        x, prob = rig.resolve_exec(r)
        if prob:
            print(f"{rid}: {prob}")
            continue
        p = te.write_prompt(rig.cfg, rid, r.lane, x, r.plan, force=True, attempt=rig.st.attempt(rid) + 1)
        left = te.leftovers(p.read_text())
        print(f"{rid}: {p}" + (f"  (unfilled: {' '.join(left)})" if left else ""))


def lease(rig: Rig, op: str, res: str, owner: str) -> None:
    names = [x for x in res.split(",") if x]
    if op == "acquire":
        waited = 0
        while True:
            ok, newly, why = rig.leases.acquire_all(owner, names)
            if ok:
                print(f"lease {','.join(names)} ACQUIRED by {owner}" if newly else
                      f"lease {','.join(names)} already held by {owner} (re-entrant)")
                return
            if why.startswith("unknown"):
                die(why)
            if waited % 300 == 0:
                print(f"{time.strftime('%F %T')} {why} — {owner} waiting (nothing held while waiting)", flush=True)
            time.sleep(15)
            waited += 15
    elif op == "release":
        rel = rig.leases.release(owner, names)
        print(f"released {','.join(rel) or 'nothing'} for {owner}")
    else:
        die("usage: orch lease acquire|release <res>[,<res>] <owner>")


def assignments(rig: Rig, show_all: bool) -> None:
    rows, _ = load_rows(rig.cfg, include_archived=False)
    print(f"{'row':<14}{'phase':<12}{'executor':<12}{'source':<10}gate")
    for r in rows:
        ph = rig.st.phase(r.id) or "-"
        if ph == "accepted" and not show_all:
            continue
        x, prob = rig.resolve_exec(r)
        src = "override" if rig.ctl(f"EXECUTOR_{r.id}").exists() else "queue"
        if x and x != rig.resolve_exec_raw(r)[0]:
            src = "fallback"        # its executor is in rate-limit fallback (lib/fallback.py)
        gate = "-" if not r.gate else ("GO ✓" if rig.gate_open(r)[0] else "GO ✗")
        print(f"{r.id:<14}{ph:<12}{(x or 'INVALID'):<12}{src:<10}{gate}")


# -- engine and lane control -------------------------------------------------------
def engine_cmd(rig: Rig, sub: str) -> None:
    if sub == "status":
        hb = rig.cfg.state / "engine.heartbeat"
        age = int(time.time() - hb.stat().st_mtime) if hb.exists() else -1
        print(f"engine {'RUNNING' if engine_running(rig) else 'NOT running'}; last heartbeat "
              f"{age}s ago" if age >= 0 else "no heartbeat yet")
    elif sub == "stop":
        rig.ctl("ENGINE_STOP").write_text("")
        import service
        if service.active(rig.cfg):
            service.stop_unit(rig.cfg)
            (rig.cfg.state / "engine.stopped").write_text(time.strftime("%F %T\n"))
        print("engine asked to stop at its next tick (sessions keep running; `orch start` brings it back)")
    elif sub == "run":
        # the systemd unit's ExecStart: the engine in the foreground, waiting for the lock
        os.execv(sys.executable, [sys.executable, str(LIB / "engine.py"), "--wait-lock"])
    elif sub == "log":
        f = rig.cfg.state / "engine.log"
        print("".join(f.read_text(errors="replace").splitlines(True)[-60:]) if f.exists() else "(no engine log)")
    elif sub == "tick":
        subprocess.run([sys.executable, str(LIB / "engine.py"), "tick"])
    else:
        die("usage: orch engine status|stop|log|tick|run")


def migrate(rig: Rig, apply: bool, archive_complete: bool, retire_controls: bool) -> None:
    cfg, st = rig.cfg, rig.st
    print(f"migrate {cfg.instance} to v2 ({'APPLY' if apply else 'dry run — add --apply'})")
    old = v1_watchers(rig)
    if old:
        die("  REFUSED: v1 lane watchers still running: " + ", ".join(old))
    print(f"  v1 watchers: none running")
    ad = st.adopted_epoch()
    print(f"  adoption stamp: {'present' if ad else 'will be written now'} — CLOSED claims older than it count as accepted, "
          f"BLOCKED/ROUTE_A ones stay halted")
    if apply and not ad:
        st.ensure_adopted()
    rows, _ = load_rows(cfg, include_archived=False)
    phases = {}
    for r in rows:
        phases.setdefault(st.phase(r.id) or "pending", []).append(r.id)
    for ph, ids in sorted(phases.items()):
        print(f"  {ph:<12} {len(ids):>4}  {' '.join(ids[:10])}{' …' if len(ids) > 10 else ''}")
    import rounds
    done_lanes = [l for l in active_lanes(cfg) if rounds.lane_done(cfg, st, l)]
    print(f"  lanes fully done: {len(done_lanes)} of {len(active_lanes(cfg))}"
          + (" — archive with --archive-complete" if done_lanes and not archive_complete else ""))
    if archive_complete:
        rounds.archive_lanes(cfg, done_lanes, dry=not apply)
    stale = []
    for f in sorted(cfg.control.glob("EXECUTOR_*")) + sorted(cfg.control.glob("GO_*")):
        rid = f.name.split("_", 1)[1]
        if st.done(rid):
            stale.append(f)
    print(f"  controls of accepted rows (EXECUTOR_/GO_): {len(stale)}"
          + (" — retire with --retire-stale-controls" if stale and not retire_controls else ""))
    if retire_controls and stale:
        dst = cfg.control / ".retired"
        for f in stale:
            print(f"    {'retire' if apply else 'would retire'} {f.name}")
            if apply:
                dst.mkdir(exist_ok=True)
                shutil.move(str(f), str(dst / f.name))
    print("  leases:")
    for l in rig.leases.status_lines():
        print("  " + l)
    known = {p.name for n in rig.leases.res for p in rig.leases.slot_paths(n)}
    for d in sorted(cfg.leases.glob("*.lock")):
        if d.name not in known:
            info = rig.leases.read_owner(d)
            print(f"    undeclared lock {d.name} (owner {info.get('owner', '?')} since {info.get('since', '?')}) — "
                  f"not a guard.resources name; remove by hand once you know nothing holds it")
    print("  next: orch start <lanes>   (only the lanes you name are dispatched)")


def main(argv: List[str]) -> int:
    if not argv:
        die("usage: orch <command> …  (orch help)")
    cmd, args = argv[0], argv[1:]
    if cmd in OPERATOR_ONLY:
        operator_only(cmd)
    rig = Rig(load())

    def one(i: int, usage: str) -> str:
        if len(args) <= i:
            die(f"usage: {usage}")
        return args[i]

    if cmd == "start":
        start(rig, args)
    elif cmd == "night":
        for f in list(rig.cfg.control.glob("PAUSE*")):
            f.unlink()
        start(rig, [])
        print("NIGHT MODE — auto-chaining on.")
    elif cmd == "day":
        rig.ctl("PAUSE").write_text("day mode\n")
        print("DAY MODE — no new row dispatches; running sessions, reviews and unblockers continue. "
              "Fire rows yourself: orch dispatch <id>")
    elif cmd == "pause":
        rig.ctl(f"PAUSE{'_' + args[0] if args else ''}").write_text(time.strftime("%F %T\n"))
        print(f"paused {args[0] if args else 'ALL'} (running sessions continue)")
    elif cmd == "resume":
        if args:
            for l in args:
                rig.ctl(f"PAUSE_{l}").unlink(missing_ok=True)
            if rig.ctl("PAUSE").exists():
                print("note: the global PAUSE is still set (orch resume with no lane clears everything)")
            print(f"resumed {' '.join(args)}")
        else:
            for f in list(rig.cfg.control.glob("PAUSE*")):
                f.unlink()
            print("resumed ALL")
    elif cmd == "stop":
        rig.ctl(f"STOP{'_' + args[0] if args else ''}").write_text(time.strftime("%F %T\n"))
        print(f"STOP set for {args[0] if args else 'ALL'} — no new dispatch of any kind; running sessions finish")
    elif cmd == "unstop":
        for f in list(rig.cfg.control.glob("STOP*")):
            f.unlink()
        print("STOP cleared")
    elif cmd == "disable":
        for l in args:
            rig.ctl(f"RUN_{l}").unlink(missing_ok=True)
        print(f"lanes disabled: {' '.join(args)} (running sessions continue; nothing new dispatches there)")
    elif cmd == "assign":
        assign(rig, one(0, "orch assign <id> <executor|default>"), one(1, "orch assign <id> <executor|default>"))
    elif cmd == "assignments":
        assignments(rig, bool(args and args[0] == "all"))
    elif cmd == "approve":
        approve(rig, one(0, "orch approve <id>"))
    elif cmd == "dispatch":
        dispatch(rig, one(0, "orch dispatch <id>"))
    elif cmd == "done":
        rest, outcome = split_outcome(args)
        args = rest
        mark(rig, "CLOSED", one(0, "orch done <id> \"summary\" [--outcome go|nogo|held|fail|infeasible]"), " ".join(args[1:]),
             outcome)
    elif cmd == "blocked":
        mark(rig, "BLOCKED", one(0, "orch blocked <id> \"reason\""), " ".join(args[1:]) or "no reason given")
    elif cmd == "escalate":
        mark(rig, "ROUTE_A", one(0, "orch escalate <id> \"reason\""), " ".join(args[1:]) or "no reason given")
    elif cmd == "verdict":
        verdict(rig, one(0, "orch verdict <session> <VERDICT> \"summary\""),
                one(1, "orch verdict <session> <VERDICT> \"summary\""), " ".join(args[2:]))
    elif cmd == "reset":
        reset(rig, one(0, "orch reset <id>"))
    elif cmd == "adopt":
        adopt(rig, one(0, "orch adopt <id>"))
    elif cmd == "rearm":
        import rearm
        usage = 'orch rearm <id> "reason" | orch rearm round-start <tag> "reason"'
        target = one(0, usage)
        tag, rest = ("", args[1:]) if target != rearm.ROUND_START else (one(1, usage), args[2:])
        reason = " ".join(rest).strip()
        if not reason:
            die(f"usage: {usage} — give a reason")
        ok, how = rearm.request(rig, caller(), identity()[1], target, tag, reason)
        if not ok:
            die(f"REFUSED: {how}")
        print(f"re-arm requested ({how}); the engine handles it on its next tick — "
              f"see the lane log for REARM or REARM REFUSED")
    elif cmd == "prompt":
        prompt(rig, [a for a in args if not a.startswith("--")], "--all-pending" in args)
    elif cmd == "lease":
        lease(rig, one(0, "orch lease acquire|release <res> <owner>"), one(1, "…"), one(2, "…"))
    elif cmd == "engine":
        engine_cmd(rig, args[0] if args else "status")
    elif cmd == "migrate":
        migrate(rig, "--apply" in args, "--archive-complete" in args, "--retire-stale-controls" in args)
    else:
        die(f"orchctl: unknown command '{cmd}'")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
