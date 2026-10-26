#!/usr/bin/env python3
"""`orch why <id>` — in plain words, why a row is where it is (v3, drop B item 7). Read-only.

It walks the scheduler's own checks in the scheduler's order (Engine.schedule): queue problems, the
row's phase (running, halted, parked, accepted …), lane enabled, STOP, the phase fence (drop D), deps (with needs=go), PAUSE, the
gate and its kind, preflight, executor, the executor's breaker, capacity and resources. Then it adds the
open Steward incident, the engine's own last word on the row (state/sched.json, with its age) and the
last 5 events. The Telegram bot answers `why <row>` with the same text.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parent))

PHASE_WORDS = {
    "accepted": "accepted — done; its dependents may start",
    "claimed": "claimed — the session wrote its claim; the engine judges it on its next tick",
    "verifying": "being verified — the engine runs its close checks",
    "blocked_claim": "claimed BLOCKED — the engine routes it to an unblocker",
    "reviewing": "under independent review",
    "unblocking": "blocked, with an unblocker session working on it",
    "blocked": "BLOCKED and halted — its unblockers are spent",
    "route_a": "ROUTE_A — halted for a decision only the Owner (or, under the rights, the Steward) can make",
    "halted": "HALTED",
    "parked": "PARKED — a named carry: never dispatched, its dependents wait",
    "spend_wait": "waiting for money (spend_wait) — a paid command would cross an approved cap; the Owner answers its spend request",
    "retry": "waiting to be re-dispatched (retry)",
    "": "not started",
}


def _ago(epoch: float) -> str:
    s = max(0, int(time.time() - float(epoch or 0)))
    return f"{s}s" if s < 120 else f"{s // 60} min" if s < 7200 else f"{s // 3600} h"


def _event_line(e: Dict[str, Any]) -> str:
    skip = {"ts", "epoch", "id", "event"}
    rest = " ".join(f"{k}={str(v)[:60]}" for k, v in sorted(e.items()) if k not in skip)
    return f"{e.get('ts', '')} {e.get('event')}{(' ' + rest) if rest else ''}"


def explain(rig, rid: str) -> str:
    from rows import find_row, implicit_deps, load_rows, unmet_deps
    import steward
    cfg, st = rig.cfg, rig.st
    rows, by_id = load_rows(cfg)
    r = by_id.get(rid) or find_row(cfg, rid)
    if r is None:
        return f"{rid}: no such row in any queue of {cfg.name} (orch status lists them)"
    ph = st.phase(rid)
    res = st.result(rid)
    d = st.dispatch(rid)
    L = [f"{rid} (lane {r.lane}{', archived' if r.archived else ''}) is {PHASE_WORDS.get(ph, ph)}."]
    blockers: List[str] = []

    if r.problems:
        blockers.append("its queue line is broken: " + "; ".join(r.problems))
    if d.get("open"):
        L.append(f"Running: session {rid} on {d.get('executor', '?')}, dispatched {_ago(d.get('epoch', 0))} ago "
                 f"(attempt {st.attempt(rid)}).")
    subs = sorted(p.name.split(".")[0] for p in cfg.state.glob(f"[ru][vb]-{rid}-*.dispatch.json")
                  if st.dispatch(p.name.split(".")[0]).get("open"))
    if subs:
        L.append("Live helper session(s): " + ", ".join(subs) + ".")
    if ph in ("blocked", "route_a", "halted"):
        v, summ, _ = st.status(rid)
        why = res.get("reason") or summ
        L.append(f"Halted because: {str(why)[:300]}")
    if ph == "parked":
        L.append(f"Parked by {res.get('parked_by', '?')} at {res.get('parked_at', '?')}: "
                 f"{str(res.get('park_reason', ''))[:300]}"
                 + ("" if not __import__("steward").owner_held(res) else " — only the Owner unparks it")
                 + f". Release: orch unpark {rid}.")
    if ph == "spend_wait":
        try:
            import spend
            rq = spend.load_request(cfg, str(res.get("spend_req", ""))) or {}
        except Exception:
            rq = {}
        caps = ", ".join(f"{c.get('cap')} short {float(c.get('need', 0)):.2f}" for c in rq.get("crossed") or [])
        L.append(f"Spend request {res.get('spend_req', '?')}: {rq.get('status', 'missing')}"
                 + (f" — USD {float(rq.get('est', 0)):.2f} {rq.get('provider', '')}; {caps}" if rq else "")
                 + (f"; paged {rq.get('paged')}" if rq.get("paged") else "; not paged yet")
                 + f". Release: `spend {res.get('spend_req', '?')} A` (Telegram) or orch spend answer "
                   f"{res.get('spend_req', '?')} A; B holds. No unblocker or Steward acts on it.")
    if ph == "accepted":
        oc = st.outcome(rid)
        if oc:
            L.append(f"Outcome: {oc}" + (" (a needs=go dependent treats it as unmet)" if oc not in ("go",) else ""))

    if ph in ("", "retry") and not d.get("open") and not r.archived:
        implicit = implicit_deps(cfg, rows)
        try:
            import tmuxio
            w = tmuxio.windows(rig.session).get(rid)
            if w and w["alive"] and w.ident != d.get("ident"):
                blockers.append("a live window wears this row's name (orphan) — close it or orch adopt " + rid)
        except Exception:
            pass
        if not rig.lane_enabled(r.lane):
            blockers.append(f"lane {r.lane} is not started (orch start {r.lane})")
        if rig.stopped(r.lane):
            blockers.append("STOP is set (orch unstop)")
        try:
            import phases
            pf = phases.Fence(cfg).block(r.lane, ph)
        except Exception as e:
            pf = f"PHASE — the phase check failed ({type(e).__name__}: {e})"
        if pf:
            blockers.append("the phase fence: " + pf[len("PHASE — "):])
        unmet = unmet_deps(st, r, implicit)
        for u in unmet:
            if ":" in u:
                dep, oc = u.split(":", 1)
                blockers.append(f"dep {dep} was accepted with outcome {oc}, and this row needs=go")
            else:
                dph = st.phase(u)
                via = "" if u in r.deps else " (serial lane: every row above it)"
                blockers.append(f"waiting on {u}{via}, which is {PHASE_WORDS.get(dph, dph).split(' —')[0]}")
        if rig.paused(r.lane):
            blockers.append(f"{'PAUSE' if rig.ctl('PAUSE').exists() else 'PAUSE_' + r.lane} is set (orch resume)")
        if r.gate:
            ok, why = rig.gate_open(r)
            if not ok:
                kind = r.meta.get("gate_kind", steward.default_gate_kind(cfg))
                who = {"steward": "the Steward may release it", "owner": "only the Owner releases it",
                       "spend": "a spend gate: the Owner's"}.get(kind, f"kind {kind}")
                blockers.append(f"{why} — gate kind {kind}: {who}")
        try:
            import rounds as rmod
            pf = rmod.preflight_block(cfg, st, r.lane)
            if pf:
                blockers.append(pf)
        except Exception:
            pass
        x, prob = rig.resolve_exec(r)
        if prob:
            blockers.append(prob)
        elif x and x != "manual":
            try:
                import health
                blk, why = health.Health(rig).blocked(x)
                if blk:
                    blockers.append(why)
                import spend
                blk, why = spend.hold_blocks(cfg, x, r.id)
                if blk:
                    blockers.append(why)
            except Exception:
                pass
            if not blockers:
                try:
                    from engine import Engine
                    e = Engine(cfg)
                    ok, why = e.capacity_ok(x, row=r.id)
                    if not ok:
                        blockers.append(f"READY, but no capacity: {why}")
                except Exception:
                    pass
        if r.resources and not blockers:
            held = []
            for res_name in r.resources:
                try:
                    hs = rig.leases.holders(res_name)
                    full = len(hs) >= len(rig.leases.slot_paths(res_name))
                except Exception:
                    hs, full = [], False
                if full and not any(i.get("owner") == rid for _, i in hs):
                    held.append(f"{res_name} (held by {', '.join(i.get('owner', '?') for _, i in hs)})")
            if held:
                blockers.append("READY, but its resource(s) are held: " + ", ".join(held))
        if r.gate and not blockers and (cfg.state / f"{rid}.probe.out").exists():
            L.append(f"(gated row: the engine runs its pre-dispatch probe first — state/{rid}.probe.out)")
        if not blockers:
            blockers.append("nothing found — it should dispatch on the engine's next tick"
                            + ("" if _engine_up(cfg) else " (but the engine is NOT running: orch start)"))
    if blockers:
        L.append("Blocker" + ("s" if len(blockers) > 1 else "") + ":")
        L += [f"  - {b}" for b in blockers]

    inc = None
    for i in (steward.incidents(cfg) if (cfg.state / "steward").is_dir() else []):
        if i.get("subject") == rid and i.get("status") != "closed":
            inc = i
    if inc:
        L.append(f"Open Steward incident #{inc['n']} ({inc.get('kind')}, {inc.get('klass')}), sessions "
                 f"{', '.join(inc.get('sessions') or []) or 'none yet'}: {str(inc.get('summary', ''))[:200]}")
    try:
        sched = json.loads((cfg.state / "sched.json").read_text())
        mt = (cfg.state / "sched.json").stat().st_mtime
        if sched.get(rid):
            L.append(f"The engine's last note ({_ago(mt)} ago): {sched[rid]}")
    except Exception:
        pass
    ev = list(st.events(rid))[-5:]
    if ev:
        L.append("Last events:")
        L += [f"  {_event_line(e)}" for e in ev]
    return "\n".join(L)


def _engine_up(cfg) -> bool:
    try:
        hb = (cfg.state / "engine.heartbeat").read_text().split()
        return time.time() - float(hb[0]) < 300
    except Exception:
        return False


def main(argv: List[str]) -> int:
    if not argv or argv[0] in ("-h", "--help", "help"):
        print("usage: orch why <row-id>   — plain words on its phase, blocker and last 5 events (read-only)")
        return 0 if argv else 2
    from config_parser import load
    from engine import Rig
    print(explain(Rig(load()), argv[0]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
