#!/usr/bin/env python3
"""The panel tier (v3 drop B): class P decisions are made by two sessions, never one.

Class P: new constructs, frozen schema or vocabulary changes and ADRs. A single
Steward session may not take them; it asks for the panel (`orch sw escalate-panel "why"`, then
closes ESCALATE), or the engine opens an incident of class `panel` directly.

  decider    `sw-<n>-d<k>` on steward.panel.decider (default opus). Its `orch sw` actions are
             STAGED, not executed (state/steward/staged/<n>.json); it writes its proposal to
             steward/incidents/<n>-panel.md and closes RESOLVED (proposal ready), PARKED or
             INBOX (it chose the safe option itself: nothing staged runs) or ESCALATE (it
             cannot decide: park + inbox).
  falsifier  `sw-<n>-f<k>` on steward.panel.falsifier (default sonnet; must differ from the
             decider's executor). It reads the proposal and the staged actions
             (steward/incidents/<n>-staged.md), tries to show the proposal is wrong, and closes
             `orch verdict sw-<n>-f<k> CONFIRM|OBJECT "…"`.

  CONFIRM    the engine executes the staged actions in order through the same checks as a live
             request; each is logged with its revert (STEWARD_LOG.md). A staged `rule P` becomes
             `SW-<k>` class P. The first refusal stops the rest: park + inbox.
  OBJECT     the safe option: the subject row is parked, one inbox item, nothing executed. Any
             falsifier verdict other than CONFIRM (an idle or launch-failure verdict too) is OBJECT.

`note` and `page` are never staged: they move no row, gate or lane, and a page is already
limited to the Owner's page classes. Panel sessions have their own budget
(steward.panel.max_sessions); past it, park + inbox. The digest lists class P rulings and panel
outcomes under HIGH.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))

import steward  # noqa: E402

DECIDER_RE = re.compile(r"sw-(\d+)-d(\d+)")
FALSIFIER_RE = re.compile(r"sw-(\d+)-f(\d+)")
FALSIFIER_VERDICTS = ("CONFIRM", "OBJECT")
IMMEDIATE = {"note", "page"}
DEFAULTS = {"enabled": True, "decider": "opus", "falsifier": "sonnet", "max_sessions": 4}


def pcfg(cfg) -> Dict[str, Any]:
    return dict(DEFAULTS, **(steward.scfg(cfg).get("panel") or {}))


def ready(cfg) -> Tuple[bool, str]:
    """-> (True, '') when class P can be decided on this rig; else (False, why)."""
    if not steward.enabled(cfg):
        return False, "the Steward lane is off"
    p = pcfg(cfg)
    if not p.get("enabled", True):
        return False, "steward.panel.enabled is false"
    allowed = list(cfg.data["executors"]["allowed"])
    for role in ("decider", "falsifier"):
        if p.get(role) not in allowed:
            return False, f"steward.panel.{role} '{p.get(role)}' is not in executors.allowed"
    if p["decider"] == p["falsifier"]:
        return False, "steward.panel.falsifier must differ from steward.panel.decider"
    return True, ""


def is_decider(sid: str) -> bool:
    return bool(DECIDER_RE.fullmatch(sid))


def is_falsifier(sid: str) -> bool:
    return bool(FALSIFIER_RE.fullmatch(sid))


def is_panel(sid: str) -> bool:
    return is_decider(sid) or is_falsifier(sid)


# -- files ------------------------------------------------------------------------------------
def proposal_file(cfg, n: Any) -> Path:
    return steward.hdir(cfg) / "incidents" / f"{n}-panel.md"


def staged_view(cfg, n: Any) -> Path:
    return steward.hdir(cfg) / "incidents" / f"{n}-staged.md"


def staged_file(cfg, n: Any) -> Path:
    d = steward.sdir(cfg) / "staged"
    d.mkdir(exist_ok=True)
    return d / f"{n}.json"


def load_staged(cfg, n: Any) -> Dict[str, Any]:
    return steward._jread(staged_file(cfg, n), {})


def save_staged(cfg, n: Any, data: Dict[str, Any]) -> None:
    steward._atomic(staged_file(cfg, n), json.dumps(data, indent=1, sort_keys=True))
    render_staged(cfg, n, data)


def render_staged(cfg, n: Any, data: Dict[str, Any]) -> None:
    acts = data.get("actions") or []
    L = [f"# Incident {n} — the panel's staged actions ({data.get('status', 'staged')})", "",
         f"Decider `{data.get('decider', '?')}`. Nothing here has run unless its outcome says so; on CONFIRM the "
         f"engine runs them in this order, each through the same checks as a live request.", ""]
    if not acts:
        L.append("(no actions staged — a CONFIRM executes nothing and only closes the incident)")
    for a in acts:
        line = f"{a['i']}. `orch sw {a['action']} {' '.join(json.dumps(x) for x in a.get('args') or [])}` — {a.get('why', '')}"
        if a.get("outcome"):
            line += f" → **{a['outcome']}**: {a.get('detail', '')}"
            if a.get("revert"):
                line += f" · revert: `{a['revert']}`"
        L.append(line)
    steward._atomic(staged_view(cfg, n), "\n".join(L) + "\n")


def staged_hash(data: Dict[str, Any]) -> str:
    """The identity of a staged set: what the falsifier checked is what CONFIRM may execute."""
    core = [[a.get("i"), a.get("action"), list(a.get("args") or []), a.get("why", "")] for a in data.get("actions") or []]
    return hashlib.sha256(json.dumps([data.get("decider"), core], sort_keys=True).encode()).hexdigest()


def end_window(rig, sid: str) -> None:
    """Close a panel session's window once its verdict is consumed: a decider must not write
    after the falsifier starts (the reaper's grace would otherwise leave it running)."""
    import tmuxio
    d = rig.st.dispatch(sid)
    w = tmuxio.windows(rig.session).get(sid)
    if w is not None and d.get("ident") and getattr(w, "ident", None) == d.get("ident"):
        tmuxio.kill(rig.session, w["wid"])


def panel_sessions(inc: Dict[str, Any]) -> List[str]:
    return [s for s in inc.get("sessions") or [] if is_panel(s)]


def next_role(inc: Dict[str, Any]) -> str:
    return "falsifier" if (inc.get("panel") or {}).get("stage") == "falsify" else "decider"


def executor_for(cfg, inc: Dict[str, Any]) -> str:
    return str(pcfg(cfg)[next_role(inc)])


# -- staging (a decider's `orch sw` request) ----------------------------------------------------
def stage(rig, sid: str, d: Dict[str, Any], action: str, args: List[str], why: str) -> Tuple[bool, str]:
    cfg = rig.cfg
    n = d.get("incident")
    inc = steward.load_incident(cfg, int(n)) if str(n).isdigit() else {}
    p = (inc or {}).get("panel") or {}
    if d.get("panel") != "decider" or p.get("stage") != "decide" or p.get("decider") != sid:
        return False, f"{sid} is not the deciding session of incident {n}'s panel"
    if action == "escalate-panel":
        return False, ("you are the panel's decider: decide, write the proposal, close RESOLVED "
                       "(or PARKED / INBOX for the safe option, ESCALATE if it cannot be decided)")
    data = load_staged(cfg, n) or {}
    if data.get("decider") != sid:
        data = {"incident": n, "decider": sid, "actions": [], "status": "staged"}
    i = len(data["actions"]) + 1
    data["actions"].append({"i": i, "action": action, "args": list(args), "why": why, "ts": time.strftime("%F %T")})
    save_staged(cfg, n, data)
    rig.st.event(f"steward:{n}", "panel_staged", session=sid, i=i, action=action, target=" ".join(args)[:120])
    return True, (f"STAGED #{i} — not executed; it runs only if the independent falsifier CONFIRMs your proposal "
                  f"({steward.te_rel(cfg, staged_view(cfg, n))})")


# -- dispatch -------------------------------------------------------------------------------------
def blocked(rig, inc: Dict[str, Any]) -> bool:
    """Before a panel session is dispatched: True when the incident was settled instead (no panel on
    this rig, the panel's budget spent, or no independent falsifier) — always park + inbox."""
    cfg = rig.cfg
    ok, why = ready(cfg)
    if not ok:
        discard(cfg, inc["n"], f"no panel tier: {why}")
        steward.finalize_unresolved(rig, inc, f"class P needs the panel tier ({why}); without it P is a wall")
        return True
    mx = int(pcfg(cfg).get("max_sessions", 4))
    if len(panel_sessions(inc)) >= mx:
        discard(cfg, inc["n"], "the panel's session budget ran out")
        steward.finalize_unresolved(rig, inc, f"{len(panel_sessions(inc))} panel sessions did not settle it "
                                              f"(steward.panel.max_sessions) — nothing staged was executed")
        return True
    if next_role(inc) == "falsifier":
        dx = str((inc.get("panel") or {}).get("decider_executor", ""))
        if executor_for(cfg, inc) == dx:
            discard(cfg, inc["n"], "no independent falsifier")
            steward.finalize_unresolved(rig, inc, f"the falsifier would run on the decider's executor ({dx}); "
                                                  f"no independent check — nothing staged was executed")
            return True
    return False


def dispatch(rig, inc: Dict[str, Any]) -> Tuple[bool, str]:
    cfg = rig.cfg
    import template_engine as te
    n = inc["n"]
    p = inc.setdefault("panel", {})
    p.setdefault("why", inc.get("summary", ""))
    role = next_role(inc)
    x = executor_for(cfg, inc)
    sessions = inc.get("sessions") or []
    if role == "decider":
        k = sum(1 for s in sessions if is_decider(s)) + 1
        sid = f"sw-{n}-d{k}"
        prop = proposal_file(cfg, n)
        if prop.exists():   # a fresh decider writes its own proposal, never inherits one
            os.replace(prop, prop.with_name(f"{n}-panel.d{k - 1}.md"))
        save_staged(cfg, n, {"incident": n, "decider": sid, "actions": [], "status": "staged"})
        p.update(stage="decide", decider=sid, decider_executor=x)
    else:
        k = sum(1 for s in sessions if is_falsifier(s)) + 1
        sid = f"sw-{n}-f{k}"
        ev = steward.evidence_file(cfg, sid)
        if ev.exists():     # a re-dispatched falsifier writes its own findings, never inherits them
            os.replace(ev, ev.with_name(f"{ev.stem}.prev{int(time.time())}.md"))
        p.update(falsifier=sid, falsifier_executor=x)
    steward.save_incident(cfg, inc)
    b = steward.bundle(rig, inc)
    order = te.write_panel(cfg, sid, inc, x, te.rel_to_root(cfg, b), role)
    ok, how = rig.dispatch_sub(sid, x, order, "SW", "", {"incident": n, "tier": x, "panel": role})
    if ok:
        inc.setdefault("sessions", []).append(sid)
        inc.setdefault("session_days", []).append(time.strftime("%F"))
        inc["status"] = "working"
        inc["session_epoch"] = int(time.time())
        steward.save_incident(cfg, inc)
        rig.st.event(f"steward:{n}", "panel_dispatch", session=sid, role=role, executor=x)
    return ok, how


# -- verdicts ---------------------------------------------------------------------------------------
def discard(cfg, n: Any, why: str) -> None:
    data = load_staged(cfg, n)
    if data and data.get("status") == "staged":
        data.update(status="discarded", discarded=time.strftime("%F %T"), discard_why=why)
        save_staged(cfg, n, data)


def on_decider_verdict(rig, inc: Dict[str, Any], sid: str, v: str, summary: str) -> bool:
    """RESOLVED -> the falsifier runs; ESCALATE -> park + inbox. -> True when handled here; PARKED and
    INBOX return False after discarding the staged actions (the normal safe paths run)."""
    cfg = rig.cfg
    n = inc["n"]
    p = inc.setdefault("panel", {})
    end_window(rig, sid)
    if v == "RESOLVED":
        data = load_staged(cfg, n) or {"incident": n, "decider": sid, "actions": [], "status": "staged"}
        if data.get("decider") != sid:
            data = {"incident": n, "decider": sid, "actions": [], "status": "staged"}
        save_staged(cfg, n, data)
        p.update(stage="falsify", proposal=steward.te_rel(cfg, proposal_file(cfg, n)), decided=time.strftime("%F %T"),
                 staged_sha=staged_hash(data))
        inc["status"] = "open"
        steward.save_incident(cfg, inc)
        rig.st.event(f"steward:{n}", "panel_proposal", session=sid, staged=len(data.get("actions") or []),
                     summary=steward.cell(summary, 300))
        rig.st.engine_log(f"steward: incident {n} panel proposal by {sid} ({len(data.get('actions') or [])} staged "
                          f"action(s)) — the falsifier runs next")
        return True
    discard(cfg, n, f"the decider closed {v}")
    p["stage"] = "done"
    steward.save_incident(cfg, inc)
    if v == "ESCALATE":
        steward.finalize_unresolved(rig, inc, f"the panel's decider could not decide: {summary}")
        return True
    return False


def on_falsifier_verdict(rig, inc: Dict[str, Any], sid: str, v: str, summary: str) -> None:
    end_window(rig, sid)
    inc.setdefault("verdicts", []).append(f"{sid}: {v} — {steward.cell(summary, 300)}")
    if v == "CONFIRM":
        confirmed(rig, inc, sid, summary)
    else:
        objected(rig, inc, sid, v, summary)


def _subject_row(rig, inc: Dict[str, Any]) -> str:
    from rows import find_row
    subj = str(inc.get("subject", ""))
    return subj if find_row(rig.cfg, subj) is not None else ""


def _hold(rig, inc: Dict[str, Any], by: str, title: str, body: str) -> None:
    """The safe option: park the subject row (if any), one inbox item."""
    row = _subject_row(rig, inc)
    if row and rig.st.phase(row) not in ("parked", "accepted"):
        steward.park(rig, row, title, by=by, carry=f"incident-{inc['n']}")
    steward.add_inbox(rig, inc["n"], title, body, row=row, by=by)


def objected(rig, inc: Dict[str, Any], sid: str, v: str, summary: str) -> None:
    cfg = rig.cfg
    n = inc["n"]
    p = inc.setdefault("panel", {})
    discard(cfg, n, f"the falsifier {sid} did not confirm ({v})")
    data = load_staged(cfg, n)
    if data:
        data["status"] = "objected"
        save_staged(cfg, n, data)
    p.update(stage="done", outcome="objected", falsifier=sid)
    steward.save_incident(cfg, inc)
    rig.st.event(f"steward:{n}", "panel_object", session=sid, verdict=v, summary=steward.cell(summary, 300))
    reason = summary if v == "OBJECT" else f"no CONFIRM ({v}: {summary})"
    _hold(rig, inc, sid, f"panel OBJECTED on {inc.get('subject')}: nothing was executed",
          f"Incident {n} ({inc.get('kind')}, class P): {inc.get('summary', '')}\n\n"
          f"The decider `{p.get('decider', '?')}` proposed (`{steward.te_rel(cfg, proposal_file(cfg, n))}`); the "
          f"independent falsifier `{sid}` objected: {reason}\n\n"
          f"Staged and NOT executed: `{steward.te_rel(cfg, staged_view(cfg, n))}` "
          f"({len((data or {}).get('actions') or [])} action(s)).\n\n"
          f"A) decide it yourself and apply what you choose, then `orch unpark` (recommended: the falsifier found a "
          f"reason the proposal may be wrong)  B) adopt the proposal as written (run its staged actions by hand)")
    steward.close(rig, inc, "panel-objected", f"falsifier {sid}: {reason}")


def confirmed(rig, inc: Dict[str, Any], sid: str, summary: str) -> None:
    cfg = rig.cfg
    n = inc["n"]
    p = inc.setdefault("panel", {})
    decider = str(p.get("decider", ""))
    data = load_staged(cfg, n) or {"incident": n, "decider": decider, "actions": [], "status": "staged"}
    if (data.get("status") not in ("staged", "executing") or data.get("decider") != decider
            or staged_hash(data) != p.get("staged_sha")):
        objected(rig, inc, sid, "STALE", f"the staged actions are not the set the falsifier checked "
                                         f"({data.get('status')}, hash {'ok' if staged_hash(data) == p.get('staged_sha') else 'changed'})")
        return
    d = rig.st.dispatch(decider)
    ctx = {"decider": decider, "falsifier": sid}
    done: List[Dict[str, Any]] = [a for a in data.get("actions") or [] if a.get("outcome") == "done"]
    refused: Optional[Dict[str, Any]] = None
    data.update(status="executing", confirmed_by=sid)
    save_staged(cfg, n, data)
    for a in data.get("actions") or []:
        if a.get("outcome") == "done":
            continue        # an engine restart mid-CONFIRM: what already ran never runs twice
        ok, detail, revert = steward.execute(rig, decider, a["action"], [str(x) for x in a.get("args") or []],
                                             str(a.get("why", "")), d, panel_ctx=ctx)
        a.update(outcome="done" if ok else "refused", detail=steward.cell(detail, 300), revert=revert or "")
        if ok:
            a["log"] = steward.log_act(rig, n, decider, a["action"], " ".join(str(x) for x in a.get("args") or []),
                                       f"{a.get('why', '')} [panel: CONFIRMed by {sid}]", revert or "none")
            done.append(a)
        save_staged(cfg, n, data)      # each outcome on disk before the next action runs
        if not ok:
            refused = a
            break
    data.update(status="partial" if refused else "executed", executed=time.strftime("%F %T"))
    save_staged(cfg, n, data)
    p.update(stage="done", outcome="partial" if refused else "confirmed", falsifier=sid)
    steward.save_incident(cfg, inc)
    rig.st.event(f"steward:{n}", "panel_confirm", session=sid, executed=len(done), refused=bool(refused),
                 summary=steward.cell(summary, 300))
    if refused:
        ran = "; ".join(f"#{a['i']} {a['action']} (revert: {a.get('revert')})" for a in done) or "none"
        _hold(rig, inc, sid, f"panel CONFIRMED on {inc.get('subject')} but staged action #{refused['i']} was refused",
              f"Incident {n}: the falsifier `{sid}` confirmed `{steward.te_rel(cfg, proposal_file(cfg, n))}`. "
              f"Executed: {ran}. Refused: #{refused['i']} {refused['action']} — {refused.get('detail')}. "
              f"The rest did not run (`{steward.te_rel(cfg, staged_view(cfg, n))}`).\n\n"
              f"A) revert the executed actions and decide it yourself (recommended)  B) finish the remaining "
              f"actions by hand")
        steward.close(rig, inc, "panel-partial", f"staged #{refused['i']} refused: {refused.get('detail')}")
        return
    row = _subject_row(rig, inc)
    if (inc.get("kind") == "halt" and row and rig.st.phase(row) in steward.HALTED
            and steward.halt_sig(rig, row) == inc.get("sig")):
        steward.finalize_unresolved(rig, inc, f"the panel confirmed {len(done)} action(s) but {row} is still halted "
                                              f"with the same claim")
        return
    steward.close(rig, inc, "panel-confirmed", f"falsifier {sid} confirmed; {len(done)} staged action(s) executed")
