#!/usr/bin/env python3
"""The Steward lane (v3): where the rig would page the Owner or go quiet, it acts.

Evidence from long-running rigs: sessions sat idle for long stretches while work
remained. Every halt waited for an operator `orch reset`; rounds and course loops
waited for a human to start a planner; most answers to pages were simply the
recommended option.

The engine opens an INCIDENT when it sees one of:

  halt        a row in phase blocked / route_a / halted (whatever put it there)
  gate        a row held only by its gate for steward.gate_after seconds
  manual      a manual row waiting for steward.gate_after seconds
  rest        "rig at rest": nothing running, work left, for steward.rest_after seconds
  breaker     an executor breaker open for health.steward_after seconds (lib/health.py)
  round_review  a round review (rr-<tag>) finished with QUESTIONS.md and no ANSWERS.md
  planner     a planner session the Steward dispatched failed or died
  progress    no-progress streak / goal stall
  undispatchable  a row the scheduler cannot dispatch (bad executor) for gate_after
  negative_outcome  a row accepted with outcome nogo / held / fail (`orch done --outcome`);
              its needs=go dependents are held (steward.negative_outcome: incident|off)
  executor_fallback  an executor was put in rate-limit fallback (lib/fallback.py): opened and closed `resolved` by
              the engine itself, no session (a record for the digest and the Owner)
  duty        a periodic command of the rig (steward.duties) printed `INCIDENT <subject> <summary>`, or
              failed (no INCIDENT line, non-zero exit / timeout): see "Duties" below
  answered    the Owner closed inbox items (steward.wake_on_answer): one incident per batch, debounced
  wake        the Owner asked the rig to wake (`orch steward wake "why"`)

and dispatches a Steward session `sw-<n>-<k>` (kind "steward") on the tier its class
needs — `mechanical` (default Sonnet) first, `judgment` (default Opus) on ESCALATE.
Incidents of class `inbox` (an Owner-only item, e.g. a CLI login) need no session: the
engine records them in the Owner's inbox and parks the row.

A Steward session acts ONLY through `orch sw <action>`: the CLI writes a request, the
engine validates it against the session's dispatch record, the incident and the caps,
runs the operator's own code path, and appends a line with the REVERT to
steward/STEWARD_LOG.md. It never pages except through `orch sw page <class>` with a
class in steward.page_classes (the Owner's ruling: spend beyond a cap, PII or secret
exposure, engine down). It closes with
`orch verdict sw-<n>-<k> RESOLVED|PARKED|INBOX|ESCALATE "…"` after writing its analysis
to steward/incidents/<n>-<k>.md.

  RESOLVED   it acted and the subject can proceed (checked: a still-halted row with the
             same halt reads as ESCALATE)
  PARKED     the subject row is parked as a named carry; the rig works around it
  INBOX      an Owner-only (wall) item: recorded in steward/INBOX.md, row parked
  ESCALATE   the next tier; past the last tier the engine parks the row and writes the
             inbox item itself. Nothing pages.

Class P (new constructs, frozen schema or vocabulary changes, ADRs) is never one
session's: `orch sw escalate-panel "why"` + ESCALATE hands the incident to the panel tier
(lib/panel.py: a decider whose actions are staged, an independent falsifier that CONFIRMs or
OBJECTs).

Caps: steward.max_sessions_per_incident, max_resets_per_row_day,
max_actions_per_session, sessions_per_day, max_parallel. A row parked is never
dispatched until `orch unpark` (Owner) or `orch sw unpark` (a Steward that parked it).

Duties (steward.duties; none by default): periodic commands run by the engine as async jobs while the lane is
enabled, cwd = the project root. Each stdout line `INCIDENT <subject> <summary…>` opens a `duty` incident (deduped
by open_for and the handled signature, so a closed one is not reopened by the same line). A failing duty with no
INCIDENT line opens `duty:<name>`. Output: state/steward/duty_<name>.out; last start/exit: state/steward/duties.json.

Cadence (README.md (Cadence); each off by default): steward.wake_on_answer turns the Owner's answers
(`orch steward inbox done <k> [LETTER]`, or Telegram `inbox <k> <LETTER>` with watchdog.inbox_answers) into an
`answered` incident within steward.answer_debounce seconds, whose bundle carries steward.on_answer (the rig's own
instruction: what the Steward does next); `orch steward wake "why"` opens a `wake` incident on the next tick;
steward.idle_page_after: when no session of any kind has been live that long, the ENGINE pages (class `stall` when an
inbox item is open, else `idle`; each must be in steward.page_classes to reach the phone), again at each
steward.idle_repage mark, at most steward.idle_pages_per_day in 24 h.

The operator closes an incident with `orch steward close <n> "why"` (never from inside a session).

Files: state/steward/ (engine-only: incidents, requests, rulings.jsonl, counters);
<instance>/steward/ (human view: STEWARD_LOG.md, INBOX.md, RULINGS.md, incidents/,
RIGHTS.md — the rig's decision-rights table).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))

KINDS_MECHANICAL = {"breaker", "undispatchable", "gate", "executor_fallback", "answered", "wake"}
KINDS_JUDGMENT = {"rest", "round_review", "planner", "progress", "manual", "negative_outcome", "duty"}
DUTY_KLASSES = ("mechanical", "judgment")
DUTY_MIN_EVERY = 300
DUTY_MAX_LINES = 20
VERDICTS = ("RESOLVED", "PARKED", "INBOX", "ESCALATE")
PLANNER_VERDICTS = ("PASS", "FAIL")
STEWARD_ACTIONS = {
    "reset", "retry", "assign", "approve", "park", "unpark", "start", "pause", "resume", "disable",
    "round-new", "round-add-lanes", "round-start", "round-close", "round-review", "plan",
    "rule", "inbox", "page", "remedy", "restart-engine", "note", "escalate-panel", "executor-restore",
}
PLANNER_ACTIONS = {"round-new", "round-add-lanes", "round-start", "reset", "approve", "note"}
HALTED = ("blocked", "route_a", "halted")


# -- paths ----------------------------------------------------------------------------
def sdir(cfg) -> Path:
    d = cfg.state / "steward"
    for sub in ("incidents", "requests", "done"):
        (d / sub).mkdir(parents=True, exist_ok=True)
    return d


def hdir(cfg) -> Path:
    d = cfg.instance / "steward"
    (d / "incidents").mkdir(parents=True, exist_ok=True)
    (d / "plans").mkdir(parents=True, exist_ok=True)
    return d


def scfg(cfg) -> Dict[str, Any]:
    return cfg.data.get("steward") or {}


def enabled(cfg) -> bool:
    return bool(scfg(cfg).get("enabled"))


def _atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def _jread(path: Path, default=None):
    try:
        return json.loads(path.read_text())
    except Exception:
        return {} if default is None else default


def _append(path: Path, line: str, header: str = "") -> None:
    import fcntl
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.seek(0)
        if header and not f.read().strip():
            f.write(header)
        f.write(line)
        fcntl.flock(f, fcntl.LOCK_UN)


def cell(s: Any, n: int = 240) -> str:
    return " ".join(str(s).replace("|", "/").split())[:n]


# -- incidents ------------------------------------------------------------------------
def incident_path(cfg, n: int) -> Path:
    return sdir(cfg) / "incidents" / f"{n}.json"


def load_incident(cfg, n: int) -> Dict[str, Any]:
    return _jread(incident_path(cfg, n))


def save_incident(cfg, inc: Dict[str, Any]) -> None:
    inc["updated"] = time.strftime("%F %T")
    _atomic(incident_path(cfg, int(inc["n"])), json.dumps(inc, indent=1, sort_keys=True))


def incidents(cfg, status: Optional[str] = None) -> List[Dict[str, Any]]:
    out = []
    for f in sorted((sdir(cfg) / "incidents").glob("*.json"), key=lambda p: int(p.stem) if p.stem.isdigit() else 0):
        inc = _jread(f)
        if inc and (status is None or inc.get("status") == status):
            out.append(inc)
    return out


def open_for(cfg, subject: str, kind: str = "") -> Optional[Dict[str, Any]]:
    for inc in incidents(cfg):
        if inc.get("status") != "closed" and inc.get("subject") == subject and (not kind or inc.get("kind") == kind):
            return inc
    return None


def next_n(cfg) -> int:
    import fcntl
    f = sdir(cfg) / "next_n"
    with open(f, "a+") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        fh.seek(0)
        try:
            n = int(fh.read().strip() or "0") + 1
        except ValueError:
            n = 1
        fh.seek(0)
        fh.truncate()
        fh.write(f"{n}\n")
        fcntl.flock(fh, fcntl.LOCK_UN)
    return n


def default_class(kind: str, rig, subject: str) -> str:
    if kind == "halt":
        ph = rig.st.phase(subject)
        why = str(rig.st.result(subject).get("why", ""))
        if ph == "route_a" or "ESCALATED" in why or "unrecognized claim" in why:
            return "judgment"
        return "mechanical"
    if kind in KINDS_MECHANICAL:
        return "mechanical"
    return "judgment"


def open_incident(rig, kind: str, subject: str, summary: str, klass: str = "", sig: str = "",
                  force: bool = False) -> Optional[Dict[str, Any]]:
    """Open (or return the already open) incident for subject/kind. -> the record. force: record it although the
    Steward lane is off (a mechanical act that is logged the same way either way — lib/fallback.py)."""
    cfg = rig.cfg
    if not enabled(cfg) and not force:
        return None
    cur = open_for(cfg, subject, kind)
    if cur:
        return cur
    n = next_n(cfg)
    inc = {"n": n, "kind": kind, "subject": subject, "summary": cell(summary, 600),
           "klass": klass or default_class(kind, rig, subject), "status": "open", "sig": sig,
           "opened": time.strftime("%F %T"), "opened_epoch": int(time.time()), "tier_idx": 0,
           "sessions": [], "verdicts": [], "actions": 0}
    save_incident(cfg, inc)
    rig.st.event(subject if not subject.startswith(("health:", "rig", "round:")) else f"steward:{n}",
                 "incident_open", n=n, kind=kind, klass=inc["klass"], summary=inc["summary"][:300])
    rig.st.engine_log(f"steward: incident {n} OPEN ({kind}, {inc['klass']}) {subject} — {inc['summary'][:200]}")
    return inc


def handled_key(kind: str, subject: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.:-]", "_", f"{kind}-{subject}")


def mark_handled(cfg, kind: str, subject: str, sig: str) -> None:
    d = sdir(cfg) / "handled"
    d.mkdir(exist_ok=True)
    _atomic(d / handled_key(kind, subject), sig + "\n")


def was_handled(cfg, kind: str, subject: str, sig: str) -> bool:
    try:
        return (sdir(cfg) / "handled" / handled_key(kind, subject)).read_text().strip() == sig
    except Exception:
        return False


def halt_sig(rig, rid: str) -> str:
    _, _, mt = rig.st.status(rid)
    res = rig.st.result(rid)
    return f"{rig.st.phase(rid)}|{int(mt)}|{res.get('halted_at', '')}|{rig.st.attempt(rid)}"


# -- parking --------------------------------------------------------------------------
def park(rig, rid: str, why: str, by: str, carry: str = "") -> bool:
    res = rig.st.result(rid)
    ph = rig.st.phase(rid)
    if ph == "parked":
        return False
    if ph == "accepted":
        return False
    res.update(phase="parked", prev_phase=ph, park_reason=cell(why, 500), parked_by=by,
               parked_at=time.strftime("%F %T"), carry=carry or res.get("carry", ""))
    _, _, mt = rig.st.status(rid)
    res["status_mtime"] = mt
    rig.st.write_result(rid, res)
    rig.st.event(rid, "park", by=by, why=cell(why, 300), carry=carry or None)
    from rows import find_row
    r = find_row(rig.cfg, rid)
    rig.log(r.lane if r else "", f"PARKED {rid} by {by} — {cell(why, 200)}")
    return True


def owner_held(res: Dict[str, Any]) -> bool:
    """A park only the Owner lifts: the Owner's own, or a judge the course loop parked because its bar moved."""
    return str(res.get("parked_by", "")).startswith("owner") or str(res.get("carry", "")) == "bar-moved"


def unpark(rig, rid: str, by: str) -> Tuple[bool, str]:
    if rig.st.phase(rid) != "parked":
        return False, f"{rid} is not parked"
    import orchctl
    try:
        orchctl.reset(rig, rid, by=f"unpark by {by}")
    except BaseException as e:
        return False, f"reset failed: {e}"
    rig.st.event(rid, "unpark", by=by)
    return True, "unparked (reset: it re-dispatches when its deps, gate and capacity allow)"


# -- ledger, inbox, rulings ---------------------------------------------------------------
LOG_HEADER = ("# STEWARD LOG — every act of the Steward lane, with its revert\n\n"
              "Newest last. The Owner reverts any line with `orch revert <#>` (or Telegram `revert <#>`).\n\n"
              "| # | when | incident | session | action | target | why | revert |\n"
              "|---|------|----------|---------|--------|--------|-----|--------|\n")


def log_act(rig, inc_n: Any, sid: str, action: str, target: str, why: str, revert: str) -> int:
    cfg = rig.cfg
    k = next_counter(cfg, "log_n")
    _append(hdir(cfg) / "STEWARD_LOG.md",
            f"| {k} | {time.strftime('%F %H:%M')} | {inc_n} | {sid} | {action} | {cell(target, 80)} | "
            f"{cell(why)} | {cell(revert, 200)} |\n", LOG_HEADER)
    rec = {"k": k, "ts": time.strftime("%F %T"), "epoch": int(time.time()), "incident": inc_n, "session": sid,
           "action": action, "target": target, "why": why[:600], "revert": revert}
    _append(sdir(cfg) / "log.jsonl", json.dumps(rec, sort_keys=True) + "\n")
    rig.st.event(f"steward:{inc_n}", "steward_act", session=sid, action=action, target=target[:120], k=k)
    return k


def next_counter(cfg, name: str) -> int:
    import fcntl
    f = sdir(cfg) / name
    with open(f, "a+") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        fh.seek(0)
        try:
            n = int(fh.read().strip() or "0") + 1
        except ValueError:
            n = 1
        fh.seek(0)
        fh.truncate()
        fh.write(f"{n}\n")
        fcntl.flock(fh, fcntl.LOCK_UN)
    return n


INBOX_HEADER = ("# OWNER INBOX — wall items only the Owner decides (nothing here pages)\n\n"
                "The rig parked the rows these items hold and keeps working around them. Answer an item in any\n"
                "session; `orch unpark <row>` releases a parked row once its item is decided.\n\n")


def add_inbox(rig, inc_n: Any, title: str, body: str, row: str = "", by: str = "engine") -> int:
    cfg = rig.cfg
    k = next_counter(cfg, "inbox_n")
    rec = {"k": k, "ts": time.strftime("%F %T"), "epoch": int(time.time()), "incident": inc_n, "row": row,
           "title": cell(title, 200), "body": body[:4000], "by": by, "status": "open"}
    _append(sdir(cfg) / "inbox.jsonl", json.dumps(rec, sort_keys=True) + "\n")
    _append(hdir(cfg) / "INBOX.md",
            f"## {k}. {cell(title, 200)}\n\n- when: {rec['ts']} · incident {inc_n} · row {row or '-'} · by {by}\n\n"
            f"{body.strip()}\n\n", INBOX_HEADER)
    rig.st.event(row or f"steward:{inc_n}", "inbox", k=k, title=rec["title"])
    rig.st.engine_log(f"steward: inbox item {k} — {rec['title']}")
    return k


def inbox_items(cfg, open_only: bool = True) -> List[Dict[str, Any]]:
    out: Dict[int, Dict[str, Any]] = {}
    try:
        for line in (sdir(cfg) / "inbox.jsonl").read_text().splitlines():
            try:
                r = json.loads(line)
            except Exception:
                continue
            if "k" in r:
                out[int(r["k"])] = {**out.get(int(r["k"]), {}), **r}
    except FileNotFoundError:
        pass
    items = [out[k] for k in sorted(out)]
    return [i for i in items if i.get("status") == "open"] if open_only else items


RULINGS_HEADER = ("# STEWARD RULINGS — provisional decisions the Steward lane took under the rig's decision rights\n\n"
                  "Each is revertable (its revert is in the entry). The Owner reviews them in the digest. A ruling of\n"
                  "a wall class is never accepted by the engine; these are S (Steward) and P (panel) classes only.\n\n")


def add_ruling(rig, inc_n: Any, sid: str, klass: str, title: str, text: str, revert: str,
               panel: Optional[Dict[str, str]] = None) -> str:
    cfg = rig.cfg
    k = next_counter(cfg, "ruling_n")
    rid = f"SW-{k}"
    rec = {"id": rid, "ts": time.strftime("%F %T"), "epoch": int(time.time()), "incident": inc_n,
           "session": sid, "class": klass, "title": cell(title, 200), "text": text[:4000], "revert": revert}
    if panel:
        rec["panel"] = dict(panel)
    who = (f"PANEL {klass} (decider {panel.get('decider')}, falsifier {panel.get('falsifier')} CONFIRMED)"
           if panel else f"STEWARD {klass}")
    _append(sdir(cfg) / "rulings.jsonl", json.dumps(rec, sort_keys=True) + "\n")
    _append(hdir(cfg) / "RULINGS.md",
            f"### {rid} — {cell(title, 200)} — {who} (provisional)\n\n"
            f"- when: {rec['ts']} · incident {inc_n} · session {sid}\n- revert: {revert}\n\n{text.strip()}\n\n",
            RULINGS_HEADER)
    return rid


def steward_ruling(cfg, rid: str) -> Optional[Dict[str, Any]]:
    """The engine-written record of a Steward ruling (the authority close checks trust)."""
    try:
        for line in (cfg.state / "steward" / "rulings.jsonl").read_text().splitlines():
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get("id") == rid:
                return r
    except FileNotFoundError:
        pass
    return None


# -- the incident bundle a Steward session reads ------------------------------------------
def bundle(rig, inc: Dict[str, Any]) -> Path:
    cfg = rig.cfg
    from rows import find_row, plan_dir
    import template_engine as te
    subj = str(inc.get("subject", ""))
    lines = [f"# Incident {inc['n']} — {inc['kind']} ({inc['klass']})", "",
             f"- subject: `{subj}`", f"- opened: {inc['opened']}", f"- summary: {inc['summary']}",
             f"- sessions so far: {', '.join(inc.get('sessions') or []) or 'none'}", ""]
    for v in inc.get("verdicts") or []:
        lines.append(f"- earlier Steward verdict: {v}")
    r = find_row(cfg, subj)
    if r is not None:
        res = rig.st.result(subj)
        v, summ, _ = rig.st.status(subj)
        d = plan_dir(cfg, r)
        lines += ["", "## The row", "",
                  f"- lane {r.lane}, executor (queue) {r.queue_exec}, deps {','.join(r.deps) or '-'}, gate {int(r.gate)}"
                  f"{' (' + r.meta.get('gate_kind', default_gate_kind(cfg)) + ')' if r.gate else ''}",
                  f"- plan: `{te.rel_to_root(cfg, cfg.workspace / r.plan)}`",
                  f"- phase: {rig.st.phase(subj) or 'pending'}; claim on disk: {v} — {summ}",
                  f"- engine's last word: {res.get('why', '')}",
                  f"- attempt {rig.st.attempt(subj)}; unblocks {rig.st.read_int(subj + '.unblock_n', 0)}; "
                  f"retiered {'yes' if rig.st.has(subj + '.retiered') else 'no'}"]
        if d is not None:
            for name in ("PAGE.txt", "PROGRESS.md"):
                if (d / name).is_file():
                    lines.append(f"- `{te.rel_to_root(cfg, d / name)}` exists — read it")
        ubs = sorted(cfg.unblock.glob(f"{subj}-*.md"))
        if ubs:
            lines.append("- unblocker analyses: " + ", ".join(f"`{te.rel_to_root(cfg, u)}`" for u in ubs[-4:]))
        dependents = [x.id for x in __import__("rows").load_rows(cfg, include_archived=False)[0] if subj in x.deps]
        if dependents:
            lines.append(f"- rows that depend on it: {', '.join(dependents[:20])}")
        ev = [e for e in rig.st.events(subj)][-15:]
        if ev:
            lines += ["", "## Its last events (state/attempts.jsonl)", ""]
            for e in ev:
                extra = {k: v for k, v in e.items() if k not in ("ts", "epoch", "id", "event")}
                lines.append(f"- {e.get('ts')} {e.get('event')} {cell(json.dumps(extra, sort_keys=True), 300)}")
    if inc["kind"] == "negative_outcome" and r is not None:
        rows_now = __import__("rows").load_rows(cfg, include_archived=False)[0]
        held = [x.id for x in rows_now if subj in x.deps and str(x.meta.get("needs", "")).lower() == "go"]
        lines += ["", "## A negative outcome", "",
                  f"- `{subj}` was accepted with outcome **{rig.st.outcome(subj)}**: its claim holds and it is a "
                  f"measured result, not a fault. Never reset or re-run it to obtain a GO.",
                  f"- held by it (queue extra needs=go): {', '.join(held) or 'none'}",
                  "- decide the course within the rights: park the held rows as a named carry, plan a correction "
                  "(`orch sw plan`), or INBOX the Owner when the next step is theirs (a W item)."]
        from store import INFEASIBLE
        if rig.st.outcome(subj) == INFEASIBLE:      # v3.1: replan the route toward the same goal (§15.3)
            import courseloop
            wave = courseloop.held_wave(cfg, subj, rig.st)
            lines += ["", "## The remaining plan cannot reach the goal (outcome infeasible)", "",
                      f"- the rows it holds — the old route's wave: {', '.join(wave) or 'none'}",
                      "- the goal and the bars stay fixed: the PLANs' bars, the round's goal, every protected path "
                      "(`./orch guard protected`). Replan the route, never the goal; never move a bar, threshold, "
                      "denominator, definition, key, scorer or test to get there.",
                      "- the steps, in order, within the decision rights (the rig's rights name the class of an "
                      "infeasible replan — when it is P, `./orch sw escalate-panel` and close ESCALATE):",
                      "  1. review: read the row's PROGRESS and verdict; say why the remaining plan cannot reach the "
                      "goal (`./orch sw round-review <tag>` when the round needs a full review);",
                      "  2. replan toward the same goal: `./orch sw plan <new round tag> \"<brief: the new route, the "
                      "rows, the tiers, the caps>\"`;",
                      f"  3. park each held row the new route makes moot: `./orch sw park <row> replan-{subj} \"why\"` "
                      "— they cannot produce a result on the old route;",
                      "  4. the planner cuts the new rows (`orch row add`). If no route inside the phase's caps reaches "
                      "the goal, INBOX the Owner (the goal is theirs) and park the wave."]
    if inc["kind"] == "duty":
        du = inc.get("duty") or {}
        dn = str(du.get("name", ""))
        dc = next((d for d in duties(cfg)[0] if d["name"] == dn), None)
        lines += ["", "## A duty's report", "",
                  f"- duty: `{dn}`" + (f" (every {dc['every']}s, timeout {dc['timeout']}s, class {dc['klass']}; "
                                         f"cmd run from the project root: `{dc['cmd']}`)" if dc else " (no longer configured)"),
                  f"- its line: `{cell(du.get('line', ''), 400)}`",
                  f"- full output of its last run: `{te.rel_to_root(cfg, duty_out(cfg, dn))}` (read it; it may hold more "
                  f"INCIDENT lines and the evidence)",
                  "- a duty reports a condition the rig cannot see itself. Judge it within the decision rights; "
                  "closing the incident marks this exact line handled (the same line does not reopen it)."]
    if inc["kind"] in ("answered", "wake"):
        if inc["kind"] == "answered":
            lines += ["", "## The Owner answered", ""]
            for a in inc.get("answers") or []:
                how = f"answer {a['answer']}" if a.get("answer") else "closed (no letter: the ruling is in the item, the ledger or the Owner's note)"
                lines.append(f"- {a['k']}. {a.get('title', '')} — {how}" + (f" (note: {a['note']})" if a.get("note") else "")
                             + (f" · by {a['by']}" if a.get("by") else ""))
            lines.append("- the items in full: `orch steward inbox` (all) or steward/INBOX.md")
        else:
            lines += ["", "## The Owner asked to wake the rig", "", f"- why: {inc.get('why', '')}"]
        lines += [f"- what this rig does next (steward.on_answer): {on_answer_text(cfg, inc.get('ks', '-'))}",
                  "- act within the decision rights: an answer releases only what it says; a letter is the option of "
                  "that item's decision brief with that letter. Record it where the rig records the Owner's rulings when the "
                  "item asks for one."]
    if inc["kind"] == "round_review" and subj.startswith("round:"):
        try:
            import courseloop
            lines += courseloop.bundle_lines(rig, subj[len("round:"):])
        except Exception as e:
            lines += ["", f"(course loop section unavailable: {type(e).__name__}: {e})"]
    try:
        notes = json.loads((cfg.state / "sched.json").read_text())
    except Exception:
        notes = {}
    if inc["kind"] in ("rest", "undispatchable", "gate"):
        lines += ["", "## Why nothing runs (the scheduler's notes)", ""]
        for k, v in sorted(notes.items())[:60]:
            lines.append(f"- {k}: {cell(v, 200)}")
    try:
        import health
        hl = health.Health(rig).summary_lines()
    except Exception:
        hl = []
    if hl:
        lines += ["", "## Executor health", ""] + [f"- {x}" for x in hl]
    open_items = inbox_items(cfg)
    if open_items:
        lines += ["", "## Owner inbox (open)", ""] + [f"- {i['k']}. {i['title']} (row {i.get('row') or '-'})"
                                                     for i in open_items[-10:]]
    out = hdir(cfg) / "incidents" / f"{inc['n']}.bundle.md"
    _atomic(out, "\n".join(lines) + "\n")
    return out


def default_gate_kind(cfg) -> str:
    return str(scfg(cfg).get("default_gate_kind", "owner"))


# -- dispatching Steward sessions ------------------------------------------------------------
def tiers_for(cfg, klass: str) -> List[str]:
    t = scfg(cfg).get("tiers") or {}
    mech, judg = t.get("mechanical", "sonnet"), t.get("judgment", "opus")
    if klass == "mechanical":
        return [mech, judg] if mech != judg else [judg]
    return [judg]


def sessions_today(cfg) -> int:
    day = time.strftime("%F")
    n = 0
    for inc in incidents(cfg):
        n += sum(1 for s in inc.get("session_days", []) if s == day)
    return n


def live_steward_sessions(rig) -> List[str]:
    return [sid for sid in rig.inflight() if sid.startswith("sw-")]


def dispatch_next(rig, inc: Dict[str, Any]) -> Tuple[bool, str]:
    cfg = rig.cfg
    import template_engine as te
    if inc.get("klass") == "panel":
        import panel
        return panel.dispatch(rig, inc)
    tiers = tiers_for(cfg, inc["klass"])
    idx = min(int(inc.get("tier_idx", 0)), len(tiers) - 1)
    x = tiers[idx]
    if x not in cfg.data["executors"]["allowed"]:
        return False, f"steward tier executor '{x}' is not in executors.allowed"
    import panel
    k = len([s for s in inc.get("sessions") or [] if not panel.is_panel(s)]) + 1
    sid = f"sw-{inc['n']}-{k}"
    b = bundle(rig, inc)
    order = te.write_steward(cfg, sid, inc, x, te.rel_to_root(cfg, b))
    ok, how = rig.dispatch_sub(sid, x, order, "SW", "", {"incident": inc["n"], "tier": x})
    if ok:
        inc.setdefault("sessions", []).append(sid)
        inc.setdefault("session_days", []).append(time.strftime("%F"))
        inc["status"] = "working"
        inc["session_epoch"] = int(time.time())
        save_incident(cfg, inc)
    return ok, how


# -- verdicts -----------------------------------------------------------------------------------
def evidence_file(cfg, sid: str) -> Path:
    """sw-<n>-<k> and the panel's falsifier sw-<n>-f<k>: steward/incidents/<n>-<k>.md; the panel's
    decider sw-<n>-d<k>: its proposal, steward/incidents/<n>-panel.md."""
    m = re.fullmatch(r"sw-(\d+)-d\d+", sid)
    if m:
        return hdir(cfg) / "incidents" / f"{m.group(1)}-panel.md"
    m = re.fullmatch(r"sw-(\d+)-(f?\d+)", sid)
    return hdir(cfg) / "incidents" / (f"{m.group(1)}-{m.group(2)}.md" if m else f"{sid}.md")


def verdicts_for(sid: str) -> Tuple[str, ...]:
    """The verdicts a Steward-kind session may record: a panel falsifier CONFIRM|OBJECT, the rest VERDICTS."""
    import panel
    return panel.FALSIFIER_VERDICTS if panel.is_falsifier(sid) else VERDICTS


def planner_evidence(cfg, sid: str) -> Path:
    return hdir(cfg) / "plans" / f"{sid}.md"


def finalize_unresolved(rig, inc: Dict[str, Any], why: str) -> None:
    """Past the last tier / session cap: park the subject row and write the inbox item. No page."""
    subj = str(inc.get("subject", ""))
    from rows import find_row
    if find_row(rig.cfg, subj) is not None:
        park(rig, subj, f"Steward could not resolve incident {inc['n']}: {why}", by="engine", carry=f"incident-{inc['n']}")
    add_inbox(rig, inc["n"], f"{inc['kind']} on {subj}: the Steward could not resolve it",
              f"Incident {inc['n']} ({inc['kind']}, {inc['klass']}): {inc['summary']}\n\nLast: {why}\n\n"
              f"Bundle: steward/incidents/{inc['n']}.bundle.md; analyses: steward/incidents/{inc['n']}-*.md",
              row=subj if find_row(rig.cfg, subj) else "")
    close(rig, inc, "unresolved", why)


def close(rig, inc: Dict[str, Any], outcome: str, why: str) -> None:
    inc.update(status="closed", outcome=outcome, closed=time.strftime("%F %T"), closed_epoch=int(time.time()),
               close_why=cell(why, 500))
    save_incident(rig.cfg, inc)
    if inc.get("sig"):
        mark_handled(rig.cfg, inc["kind"], inc["subject"], inc["sig"])
    rig.st.event(inc["subject"] if not str(inc["subject"]).startswith(("health:", "rig", "round:")) else f"steward:{inc['n']}",
                 "incident_close", n=inc["n"], outcome=outcome, why=cell(why, 300))
    rig.st.engine_log(f"steward: incident {inc['n']} CLOSED {outcome} — {cell(why, 200)}")


def on_verdict(rig, inc: Dict[str, Any], sid: str, v: str, summary: str) -> None:
    cfg = rig.cfg
    inc.setdefault("verdicts", []).append(f"{sid}: {v} — {cell(summary, 300)}")
    subj = str(inc.get("subject", ""))
    from rows import find_row
    import panel
    is_row = find_row(cfg, subj) is not None
    if panel.is_decider(sid) and panel.on_decider_verdict(rig, inc, sid, v, summary):
        return
    if (v == "ESCALATE" and not panel.is_panel(sid) and inc.get("klass") == "panel"
            and (inc.get("panel") or {}).get("stage") == "decide"):
        inc["status"] = "open"     # the session asked for the panel (escalate-panel): no tier change
        save_incident(cfg, inc)
        rig.st.engine_log(f"steward: incident {inc['n']} goes to the panel (decider + independent falsifier)")
        return
    if v == "RESOLVED":
        if inc["kind"] == "halt" and is_row and rig.st.phase(subj) in HALTED and halt_sig(rig, subj) == inc.get("sig"):
            v, summary = "ESCALATE", f"RESOLVED but {subj} is still halted with the same claim: act (reset, retry, park) or escalate"
        else:
            close(rig, inc, "resolved", summary)
            return
    if v == "PARKED":
        if is_row and rig.st.phase(subj) != "parked":
            park(rig, subj, summary, by=sid, carry=f"incident-{inc['n']}")
        close(rig, inc, "parked", summary)
        return
    if v == "INBOX":
        have = any(i.get("incident") == inc["n"] for i in inbox_items(cfg, open_only=False))
        if not have:
            add_inbox(rig, inc["n"], f"{inc['kind']} on {subj}", summary, row=subj if is_row else "", by=sid)
        if is_row and rig.st.phase(subj) not in ("parked", "accepted"):
            park(rig, subj, f"Owner inbox: {summary}", by=sid, carry=f"incident-{inc['n']}")
        close(rig, inc, "inbox", summary)
        return
    # ESCALATE (or a RESOLVED that did not resolve)
    tiers = tiers_for(cfg, inc["klass"])
    inc["tier_idx"] = int(inc.get("tier_idx", 0)) + 1
    inc["status"] = "open"
    save_incident(cfg, inc)
    if inc["tier_idx"] >= len(tiers):
        finalize_unresolved(rig, inc, f"last tier escalated: {summary}")
    else:
        rig.st.engine_log(f"steward: incident {inc['n']} escalates to {tiers[inc['tier_idx']]}")


# -- requests: `orch sw <action>` -----------------------------------------------------------------
def request(cfg, sid: str, action: str, args: List[str], why: str) -> Path:
    rec = {"session": sid, "action": action, "args": args, "why": " ".join(why.split()),
           "epoch": time.time(), "ts": time.strftime("%F %T")}
    f = sdir(cfg) / "requests" / f"{sid}.{int(time.time() * 1000)}.json"
    _atomic(f, json.dumps(rec, indent=1, sort_keys=True))
    return f


def wait_result(req: Path, timeout: float = 120) -> Dict[str, Any]:
    done = req.parent.parent / "done"
    stem = req.stem
    end = time.time() + timeout
    while time.time() < end:
        for suffix in ("done", "refused"):
            f = done / f"{stem}.{suffix}.json"
            if f.exists():
                return _jread(f)
        time.sleep(1)
    return {"outcome": "timeout", "why": f"the engine did not answer within {int(timeout)}s — is it running? "
                                          f"The request stays queued: {req.name}"}


def session_ok(rig, sid: str) -> Tuple[bool, str, Dict[str, Any]]:
    d = rig.st.dispatch(sid)
    if not d or not d.get("open") or not d.get("ident"):
        return False, f"{sid} is not a live session the engine dispatched", {}
    if not (sid.startswith("sw-") or sid.startswith("pl-")):
        return False, f"{sid} is not a Steward or planner session", {}
    return True, "", d


def _resets_today(rig, rid: str) -> int:
    day = time.strftime("%F")
    n = 0
    for e in rig.st.events(rid):
        if e.get("event") == "reset" and str(e.get("by", "")).startswith(("steward", "sw-", "pl-")) \
                and str(e.get("ts", "")).startswith(day):
            n += 1
    return n


def _round_cmd(rig, *args: str, actor: str = "steward") -> Tuple[bool, str]:
    exe = rig.cfg.instance / "orch"
    if not exe.exists():
        from engine import LIB
        exe = LIB.parent / "bin" / "orch"
    env = dict(os.environ, ORCH_INSTANCE=str(rig.cfg.instance))
    for k in ("ORCH_ROW", "ORCH_KIND", "ORCH_PLAN_DIR", "ORCH_TOKEN"):
        env.pop(k, None)
    env["ORCH_ACTOR"] = actor
    try:
        p = subprocess.run([str(exe), "round", *args], capture_output=True, text=True, env=env,
                           cwd=str(rig.cfg.instance), timeout=180)
    except Exception as e:
        return False, f"orch round {' '.join(args)} failed: {e}"
    return p.returncode == 0, " ".join((p.stdout + p.stderr).split())[-500:]


def _quiet(fn, *a, **kw) -> Tuple[bool, str]:
    """Run an orchctl function that prints and may die(); -> (ok, its output)."""
    import contextlib
    import io
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            fn(*a, **kw)
        return True, " ".join(buf.getvalue().split())[-400:]
    except SystemExit as e:
        return False, " ".join((buf.getvalue() + " " + str(e)).split())[-400:]
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def handle(rig, req: Dict[str, Any]) -> Tuple[bool, str, Optional[str]]:
    """Validate and execute one request (a panel decider's is staged instead). -> (ok, detail, revert);
    revert None = staged, nothing executed."""
    cfg = rig.cfg
    sid = str(req.get("session", ""))
    action = str(req.get("action", ""))
    args = [str(a) for a in req.get("args") or []]
    why = str(req.get("why", "")).strip()
    ok, msg, d = session_ok(rig, sid)
    if not ok:
        return False, msg, ""
    planner = sid.startswith("pl-")
    allowed = PLANNER_ACTIONS if planner else STEWARD_ACTIONS
    if action not in allowed:
        return False, f"'{action}' is not a {'planner' if planner else 'Steward'} action (have: {', '.join(sorted(allowed))})", ""
    if not why and action not in ("note",):
        return False, "every act needs a reason (the last argument)", ""
    cnt = rig.st.read_int(f"{sid}.sw_actions", 0)
    if cnt >= int(scfg(cfg).get("max_actions_per_session", 25)):
        return False, f"{sid} has used its {cnt} actions (steward.max_actions_per_session)", ""
    import panel
    if panel.is_falsifier(sid) and action != "note":
        return False, "the panel's falsifier only checks the proposal: CONFIRM or OBJECT, never act", ""
    if panel.is_decider(sid) and action not in panel.IMMEDIATE:
        bad = precheck(cfg, action, args)
        if bad:
            return False, f"not staged: {bad}", ""
        ok, detail = panel.stage(rig, sid, d, action, args, why)
        return ok, detail, (None if ok else "")
    return execute(rig, sid, action, args, why, d)


def precheck(cfg, action: str, args: List[str]) -> str:
    """The static checks of a request (shape, rows, lanes, rule class, gate kind) — what a staged action
    can be refused for later without the rig changing. '' when it passes."""
    from rows import find_row, active_lanes
    need = {"reset": 1, "retry": 2, "assign": 2, "approve": 1, "park": 1, "unpark": 1, "start": 1, "pause": 1,
            "resume": 1, "disable": 1, "round-new": 1, "round-add-lanes": 2, "round-start": 1, "round-close": 1,
            "round-review": 1, "plan": 1, "rule": 4, "inbox": 2, "page": 3, "remedy": 1, "executor-restore": 1}
    if len(args) < need.get(action, 0):
        return f"'{action}' needs {need[action]} argument(s) before the reason"
    if action in ("reset", "retry", "assign", "approve", "park", "unpark"):
        r = find_row(cfg, args[0])
        if r is None:
            return f"unknown row '{args[0]}'"
        if action == "approve":
            kind = r.meta.get("gate_kind", default_gate_kind(cfg))
            if not r.gate or kind != "steward":
                return f"{r.id} has no gate the Steward may release (gate {int(r.gate)}, kind {kind})"
        if action in ("retry", "assign") and args[1] != "default" and args[1] not in cfg.data["executors"]["allowed"]:
            return f"executor '{args[1]}' is not in executors.allowed"
    if action in ("start", "pause", "resume", "disable"):
        unknown = [l for l in args if l not in active_lanes(cfg)]
        if unknown:
            return f"no active queue for lane(s) {' '.join(unknown)}"
    if action == "executor-restore" and args[0] not in cfg.data["executors"]["allowed"]:
        return f"executor '{args[0]}' is not in executors.allowed"
    if action == "rule" and args[0].upper() not in ("S", "P"):
        return "a ruling is class S or P; W and $ are the Owner's"
    if action == "escalate-panel":
        return "the panel's own sessions do not escalate to the panel"
    return ""


def execute(rig, sid: str, action: str, args: List[str], why: str, d: Dict[str, Any],
            panel_ctx: Optional[Dict[str, str]] = None) -> Tuple[bool, str, str]:
    """Run one validated action as session `sid` (dispatch record `d`). panel_ctx: the panel's CONFIRM
    is executing the decider's staged action. -> (ok, detail, revert)."""
    cfg = rig.cfg
    planner = sid.startswith("pl-")
    from rows import find_row
    import orchctl
    import rearm

    def row_arg(i: int = 0):
        if len(args) <= i:
            raise ValueError(f"'{action}' needs a row id")
        r = find_row(cfg, args[i])
        if r is None:
            raise ValueError(f"unknown row '{args[i]}'")
        return r

    def planner_scope(rid: str) -> str:
        """A planner may only touch rows of its own round's lanes."""
        if not planner:
            return ""
        tag = str((d.get("snapshot") or {}).get("round") or d.get("round") or "")
        import rounds
        lanes = [str(x) for x in (rounds.load_rounds(cfg).get(tag) or {}).get("lanes", [])]
        r = find_row(cfg, rid)
        if r is None or r.lane not in lanes:
            return f"planner {sid} may only act on rows of round {tag}'s lanes ({' '.join(lanes) or 'none'})"
        return ""

    def owner_parked(rid: str) -> str:
        """An Owner park is lifted only by the Owner: no session act may re-run, re-tier or release the row. So is a
        judge the course loop parked because its bar moved (v3.1, carry `bar-moved`): adopting a new bar is the
        Owner's."""
        res = rig.st.result(rid)
        if rig.st.phase(rid) == "parked" and owner_held(res):
            return (f"{rid} was parked {'by the Owner' if str(res.get('parked_by', '')).startswith('owner') else 'because its bar moved'}"
                    f" ({cell(str(res.get('park_reason', '')), 120)}) — only the Owner unparks it; INBOX the question instead")
        return ""

    try:
        if action in ("reset", "retry", "assign", "approve") and args and owner_parked(args[0]):
            return False, owner_parked(args[0]), ""
        if action in ("reset", "retry"):
            r = row_arg(0)
            if planner_scope(r.id):
                return False, planner_scope(r.id), ""
            busy = rearm.target_busy(rig, r.id)
            if busy:
                return False, busy, ""
            import courseloop
            owned = courseloop.owns_negative(cfg, r.id, rig.st)
            if owned:
                return False, owned, ""
            if _resets_today(rig, r.id) >= int(scfg(cfg).get("max_resets_per_row_day", 3)):
                return False, (f"{r.id} was reset {_resets_today(rig, r.id)}x today by the Steward lane "
                               f"(steward.max_resets_per_row_day) — park it or escalate"), ""
            if action == "retry":
                if len(args) < 2:
                    return False, "retry <row> <executor> \"why\"", ""
                prev = (rig.ctl(f"EXECUTOR_{r.id}").read_text().strip() if rig.ctl(f"EXECUTOR_{r.id}").exists() else "default")
                ok, out = _quiet(orchctl.assign, rig, r.id, args[1], by=sid)
                if not ok:
                    return False, out, ""
            ok, out = _quiet(orchctl.reset, rig, r.id, by=sid)
            if not ok:
                return False, out, ""
            rv = (f"orch assign {r.id} {prev}" if action == "retry" else "none — the re-run itself; its PROGRESS records what it changed")
            return True, out, rv
        if action == "assign":
            r = row_arg(0)
            if len(args) < 2:
                return False, "assign <row> <executor|default> \"why\"", ""
            prev = (rig.ctl(f"EXECUTOR_{r.id}").read_text().strip() if rig.ctl(f"EXECUTOR_{r.id}").exists() else "default")
            ok, out = _quiet(orchctl.assign, rig, r.id, args[1], by=sid)
            return ok, out, f"orch assign {r.id} {prev}" if ok else ""
        if action == "approve":
            r = row_arg(0)
            if planner_scope(r.id):
                return False, planner_scope(r.id), ""
            if not r.gate:
                return False, f"{r.id} has no gate", ""
            kind = r.meta.get("gate_kind", default_gate_kind(cfg))
            if kind != "steward":
                return False, (f"{r.id}'s gate is kind '{kind}' — only gate_kind=steward gates are the Steward's; "
                               f"an '{kind}' gate is the Owner's (INBOX it)"), ""
            ok, out = _quiet(orchctl.approve, rig, r.id, by=sid)
            return ok, out, f"rm {te_rel(cfg, rig.ctl('GO_' + r.id))}  (re-gates {r.id})"
        if action == "park":
            r = row_arg(0)
            if not park(rig, r.id, why, by=sid, carry=args[1] if len(args) > 1 else ""):
                return False, f"{r.id} is already parked or accepted", ""
            return True, f"{r.id} parked", f"orch unpark {r.id}"
        if action == "unpark":
            r = row_arg(0)
            res = rig.st.result(r.id)
            if owner_held(res):
                return False, f"{r.id} was parked by the Owner (or because its bar moved) — only the Owner unparks it", ""
            ok, out = unpark(rig, r.id, by=sid)
            return ok, out, f"orch park {r.id} \"re-park\""
        if action in ("start", "pause", "resume", "disable"):
            lanes = [a for a in args if a]
            if not lanes:
                return False, f"{action} <lane>… \"why\"", ""
            from rows import active_lanes
            unknown = [l for l in lanes if l not in active_lanes(cfg)]
            if unknown:
                return False, f"no active queue for lane(s) {' '.join(unknown)}", ""
            fence = phase_fence(cfg, lanes)
            if action == "start" and fence:
                return False, fence, ""
            if action == "start":
                for l in lanes:
                    rig.ctl(f"RUN_{l}").write_text(time.strftime("%F %T\n"))
                return True, f"lanes enabled: {' '.join(lanes)}", f"orch disable {' '.join(lanes)}"
            if action == "disable":
                for l in lanes:
                    rig.ctl(f"RUN_{l}").unlink(missing_ok=True)
                return True, f"lanes disabled: {' '.join(lanes)}", f"orch start {' '.join(lanes)}"
            if action == "pause":
                for l in lanes:
                    rig.ctl(f"PAUSE_{l}").write_text(f"steward {sid}: {why}\n")
                return True, f"paused {' '.join(lanes)}", f"orch resume {' '.join(lanes)}"
            for l in lanes:
                rig.ctl(f"PAUSE_{l}").unlink(missing_ok=True)
            return True, f"resumed {' '.join(lanes)}", f"orch pause {' '.join(lanes)}"
        if action in ("round-new", "round-add-lanes", "round-start", "round-close", "round-review"):
            if not args:
                return False, f"{action} <tag> … \"why\"", ""
            tag = args[0]
            if planner and action != "round-new":
                mine = str((d.get("snapshot") or {}).get("round") or d.get("round") or "")
                if tag != mine:
                    return False, f"planner {sid} works round {mine}, not {tag}", ""
            if action == "round-new":
                lanes, ph = split_phase(args[1:])
                fence = phase_fence(cfg, lanes, new_round=True, phase=ph)
                if fence:
                    return False, fence, ""
                ok, out = _round_cmd(rig, "new", tag, "--title", why[:120], *(["--phase", ph] if ph else []),
                                     "--lanes", *lanes, actor=sid)
                return ok, out, f"rm -r {te_rel(cfg, cfg.instance / 'rounds' / tag)}  (before it starts)"
            if action == "round-add-lanes":
                if len(args) < 2:
                    return False, "round-add-lanes <tag> <lane>… \"why\"", ""
                ok, out = _round_cmd(rig, "add-lanes", tag, *args[1:], actor=sid)
                return ok, out, f"edit rounds/{tag}/round.yaml (remove {' '.join(args[1:])})"
            if action == "round-start":
                import rounds
                r = rounds.load_rounds(cfg).get(tag) or {}
                fence = phase_fence(cfg, [str(x) for x in r.get("lanes", [])]) or round_fence(cfg, tag)
                if fence:
                    return False, fence, ""
                ok, out = _round_cmd(rig, "start", tag, actor=sid)
                return ok, out, f"orch disable {' '.join(str(x) for x in r.get('lanes', []))}"
            if action == "round-close":
                ok, out = _round_cmd(rig, "close", tag, actor=sid)
                return ok, out, f"move rounds/{tag} lanes back from archive/ (manual)"
            ok, out = _round_cmd(rig, "review", tag, actor=sid)
            return ok, out, "none (a read-only review)"
        if action == "plan":
            if not args:
                return False, "plan <round-tag> \"brief\"", ""
            import phases
            try:
                fence = phases.plan_fence(cfg, args[0])
            except Exception as e:
                fence = f"the phase fence failed ({type(e).__name__}: {e}) — refused, not passed"
            if fence:
                return False, fence, ""
            ok, out = dispatch_planner(rig, d.get("incident"), args[0], why)
            return ok, out, (f"tmux kill-window -t {cfg.tmux_session}:{out}; git revert the planner's "
                             f"plan:{args[0]} commit") if ok else ""
        if action == "rule":
            # rule <S|P> <title> <text> <revert>
            if len(args) < 4:
                return False, 'rule <S|P> "<title>" "<text>" "<revert>" "why"', ""
            klass = args[0].upper()
            if klass not in ("S", "P"):
                return False, "a Steward ruling is class S or P; wall (W) and spend ($) items are the Owner's — INBOX or page them", ""
            if klass == "P" and not panel_ready(cfg):
                return False, ("class P needs the panel tier (decider + independent falsifier), which this rig "
                               "does not have (steward.panel) — it is a wall here: INBOX the question instead"), ""
            if klass == "P" and panel_ctx is None:
                return False, ("class P is the panel's, never one session's: `orch sw escalate-panel \"why\"`, then "
                               "close ESCALATE — a decider proposes, an independent falsifier confirms"), ""
            rid = add_ruling(rig, d.get("incident"), sid, klass, args[1], args[2], args[3], panel=panel_ctx)
            return True, f"{rid} recorded (steward/RULINGS.md)", f"a new ruling reverting {rid}: {args[3]}"
        if action == "escalate-panel":
            import panel
            if panel_ctx is not None or panel.is_panel(sid):
                return False, "the panel's own sessions do not escalate to the panel", ""
            ok, why_not = panel.ready(cfg)
            if not ok:
                return False, f"no panel tier on this rig ({why_not}) — class P is a wall here: INBOX it", ""
            n = d.get("incident")
            inc = load_incident(cfg, int(n)) if str(n).isdigit() else {}
            if not inc or inc.get("status") == "closed":
                return False, f"incident {n} is not open", ""
            inc["panel"] = {"stage": "decide", "why": cell(why, 600), "by": sid, "from_klass": inc.get("klass")}
            inc["klass"] = "panel"
            save_incident(cfg, inc)
            rig.st.event(f"steward:{n}", "panel_requested", session=sid, why=cell(why, 300))
            return True, (f"incident {n} goes to the panel after your verdict: close ESCALATE (a RESOLVED, PARKED or "
                          f"INBOX verdict settles it without the panel)"), \
                "none — nothing executes unless the panel's falsifier CONFIRMs"
        if action == "inbox":
            if len(args) < 2:
                return False, 'inbox "<title>" "<body: the decision, decision-brief form>" [row] "why"', ""
            k = add_inbox(rig, d.get("incident"), args[0], args[1], row=args[2] if len(args) > 2 else "", by=sid)
            return True, f"inbox item {k}", f"orch inbox done {k}"
        if action == "page":
            if len(args) < 3:
                return False, 'page <class> "<title>" "<body>" "why"', ""
            cls = args[0].lower()
            classes = [str(c).lower() for c in scfg(cfg).get("page_classes") or []]
            if cls not in classes:
                return False, (f"page class '{cls}' is not one the Owner allowed ({', '.join(classes)}); "
                               f"everything else goes to the digest or the inbox"), ""
            if not rig.st.once(f"sw-page-{cls}-{hashlib.sha1(args[1].encode()).hexdigest()[:10]}"):
                return False, "this page was already sent once", ""
            rig.notify("critical", f"{cfg.name}: {args[1]}", args[2][:700], page_class=cls)
            return True, f"paged ({cls})", "none"
        if action == "remedy":
            if not args:
                return False, "remedy <executor> \"why\"", ""
            import health
            h = health.Health(rig)
            h.remedy(args[0])
            h.probe(args[0])
            return True, f"remedies and probe started for {args[0]}", "none"
        if action == "executor-restore":
            if not args:
                return False, 'executor-restore <executor> "why"', ""
            import fallback
            ok, out = fallback.restore(rig, args[0], why, by=sid)
            return ok, out, (f"orch executor fallback {args[0]}  (engages the fallback again; the Owner's)" if ok else "")
        if action == "restart-engine":
            rig.ctl("ENGINE_RESTART").write_text(f"{sid}: {why}\n")
            return True, "the engine re-execs itself at the end of this tick", "none"
        if action == "note":
            text = " ".join(args) or why
            _append(sdir(cfg) / "notes.jsonl", json.dumps({"ts": time.strftime("%F %T"), "session": sid,
                                                          "text": text[:1000]}) + "\n")
            return True, "noted for the digest", "none"
    except ValueError as e:
        return False, str(e), ""
    return False, f"unhandled action {action}", ""


def te_rel(cfg, p: Path) -> str:
    import template_engine as te
    return te.rel_to_root(cfg, p)


def panel_ready(cfg) -> bool:
    """The panel tier (decider + independent falsifier, lib/panel.py) is configured on this rig."""
    try:
        import panel
    except Exception:
        return False
    return panel.ready(cfg)[0]


def phase_fence(cfg, lanes: List[str], new_round: bool = False, phase: str = "") -> str:
    """Drop D's phase boundary (lib/phases.py): '' when the lanes may run, or a new round may be created in
    `phase`. Without a phases file there is no fence; a fence that fails refuses (never an open phase)."""
    import phases
    try:
        return phases.fence(cfg, lanes, new_round=new_round, phase=phase)
    except Exception as e:
        return f"the phase fence failed ({type(e).__name__}: {e}) — refused, not passed"


def round_fence(cfg, tag: str) -> str:
    import phases
    try:
        return phases.round_fence(cfg, tag)
    except Exception as e:
        return f"the phase fence failed ({type(e).__name__}: {e}) — refused, not passed"


def split_phase(args: List[str]) -> Tuple[List[str], str]:
    """`round-new <tag> --phase P <lane>…`: -> (the lanes, P)."""
    out, ph, i = [], "", 0
    while i < len(args):
        if args[i] == "--phase" and i + 1 < len(args):
            ph = args[i + 1]
            i += 2
            continue
        if args[i].startswith("--phase="):
            ph = args[i].split("=", 1)[1]
        else:
            out.append(args[i])
        i += 1
    return out, ph


def consume_requests(rig) -> None:
    cfg = rig.cfg
    for f in sorted((sdir(cfg) / "requests").glob("*.json")):
        req = _jread(f)
        if not req:
            f.unlink(missing_ok=True)
            continue
        sid = str(req.get("session", ""))
        if not f.name.startswith(f"{sid}."):
            ok, detail, revert = False, "request file name does not match its session", ""
        else:
            ok, detail, revert = handle(rig, req)
        staged = ok and revert is None
        req.update(outcome="done" if ok else "refused", why=detail, revert=revert or "", handled=time.strftime("%F %T"),
                   staged=staged or None)
        if ok:
            rig.st.write_int(f"{sid}.sw_actions", rig.st.read_int(f"{sid}.sw_actions", 0) + 1)
        if staged:
            rig.st.engine_log(f"steward: {sid} {req.get('action')} STAGED — {detail}")
        elif ok:
            d = rig.st.dispatch(sid)
            k = log_act(rig, d.get("incident", "-"), sid, str(req.get("action")), " ".join(req.get("args") or []),
                        str(req.get("why", "")), revert)
            req["log"] = k
        else:
            rig.st.engine_log(f"steward: {sid} {req.get('action')} REFUSED — {detail}")
            rig.st.event(sid, "steward_refused", action=req.get("action"), detail=cell(detail, 300))
        out = sdir(cfg) / "done" / f"{f.stem}.{req['outcome']}.json"
        _atomic(out, json.dumps(req, indent=1, sort_keys=True))
        f.unlink(missing_ok=True)


# -- planner sessions ---------------------------------------------------------------------------------
def dispatch_planner(rig, inc_n: Any, tag: str, brief: str) -> Tuple[bool, str]:
    cfg = rig.cfg
    import template_engine as te
    x = str(scfg(cfg).get("planner", "opus"))
    k = next_counter(cfg, "planner_n")
    sid = f"pl-{tag}-{k}"
    bf = hdir(cfg) / "plans" / f"{sid}.brief.md"
    _atomic(bf, f"# Planner brief — {sid} (incident {inc_n})\n\n{brief.strip()}\n")
    order = te.write_planner(cfg, sid, tag, x, te.rel_to_root(cfg, bf), inc_n)
    ok, how = rig.dispatch_sub(sid, x, order, "SW", "", {"incident": inc_n, "round": tag, "snapshot": {"round": tag}})
    if ok:
        planners = _jread(sdir(cfg) / "planners.json", {})
        planners[sid] = {"incident": inc_n, "round": tag, "epoch": int(time.time()), "status": "running"}
        _atomic(sdir(cfg) / "planners.json", json.dumps(planners, indent=1, sort_keys=True))
        return True, sid
    return False, how


def drive_planners(rig) -> None:
    cfg = rig.cfg
    planners = _jread(sdir(cfg) / "planners.json", {})
    changed = False
    live = rig.inflight()
    for sid, p in planners.items():
        if p.get("status") != "running" or sid in live:
            continue
        v, summary = rig.sub_verdict(sid, float(p.get("epoch", 0)) - 1)
        if v in PLANNER_VERDICTS:
            rig.consume(sid)
            p.update(status=v.lower(), summary=cell(summary, 300), ended=time.strftime("%F %T"))
            changed = True
            rig.st.event(f"steward:{p.get('incident')}", "planner_verdict", session=sid, verdict=v, summary=cell(summary, 300))
            rig.st.engine_log(f"steward: planner {sid} {v} — {cell(summary, 200)}")
            if v == "FAIL":
                open_incident(rig, "planner", f"round:{p.get('round')}", f"planner {sid} FAILED: {summary}", klass="judgment")
            continue
        d = rig.st.dispatch(sid)
        if d.get("ended"):
            p.update(status="died", ended=time.strftime("%F %T"))
            changed = True
            open_incident(rig, "planner", f"round:{p.get('round')}", f"planner {sid} ended without a verdict", klass="judgment")
    if changed:
        _atomic(sdir(cfg) / "planners.json", json.dumps(planners, indent=1, sort_keys=True))


# -- the engine's scan ------------------------------------------------------------------------------------
def _waits(cfg) -> Dict[str, float]:
    return _jread(sdir(cfg) / "waits.json", {})


def scan(rig, notes: Dict[str, str]) -> None:
    """Open incidents for what the rig can no longer move by itself."""
    cfg = rig.cfg
    sc = scfg(cfg)
    from rows import load_rows
    rows, by_id = load_rows(cfg, include_archived=False)
    now = time.time()
    waits = _waits(cfg)
    seen = set()
    gate_after = float(sc.get("gate_after", 600))
    neg_on = str(sc.get("negative_outcome", "incident")).lower() not in ("off", "false", "0", "none")
    try:
        import courseloop
        loop_judges = courseloop.judges(cfg)     # their negative outcome is the native course loop's (drop D)
    except Exception:
        loop_judges = {}
    for r in rows:
        ph = rig.st.phase(r.id)
        if ph == "accepted" and neg_on:
            oc = rig.st.outcome(r.id)
            if oc and oc != "go" and r.id not in loop_judges:
                res = rig.st.result(r.id)
                sig = f"{oc}|{int(float(res.get('status_mtime', 0) or 0))}|{rig.st.attempt(r.id)}"
                if not open_for(cfg, r.id, "negative_outcome") and not was_handled(cfg, "negative_outcome", r.id, sig):
                    held = [x.id for x in rows if r.id in x.deps and str(x.meta.get("needs", "")).lower() == "go"]
                    open_incident(rig, "negative_outcome", r.id,
                                  f"{r.id} closed with outcome {oc}"
                                  f"{' (the remaining plan cannot reach the goal: replan the route)' if oc == 'infeasible' else ''}"
                                  f": {res.get('summary', '')}"
                                  f"{' — held (needs=go): ' + ','.join(held) if held else ''}", sig=sig)
            continue
        if ph in HALTED:
            if open_for(cfg, r.id, "halt"):
                continue
            sig = halt_sig(rig, r.id)
            if was_handled(cfg, "halt", r.id, sig):
                continue
            res = rig.st.result(r.id)
            v, summ, _ = rig.st.status(r.id)
            open_incident(rig, "halt", r.id, f"{ph}: {res.get('why') or summ}", sig=sig)
            continue
        note = str(notes.get(r.id, ""))
        key = None
        if ph in ("", "retry") and (note.startswith("gated") or note.startswith("plan changed since GO")):
            key = ("gate", note)
        elif ph in ("", "retry") and (note.startswith("control/EXECUTOR_") or note.startswith("queue executor")
                                      or note.startswith("work order could not")):
            key = ("undispatchable", note)
        elif rig.st.dispatch(r.id).get("open") and rig.st.dispatch(r.id).get("executor") == "manual" and ph in ("", "retry"):
            key = ("manual", "manual row waiting for its operator")
        if key and key[0] in ("gate", "undispatchable"):
            # a note outlives what it described (written in an approval window, the row since dispatched or its
            # gate released): only a row nothing is running for, still held, is stuck
            if rig.st.dispatch(r.id).get("open") or (key[0] == "gate" and note.startswith("gated") and rig.gate_open(r)[0]):
                key = None
        if key:
            wk = f"{key[0]}:{r.id}"
            seen.add(wk)
            first = waits.setdefault(wk, now)
            if now - first >= gate_after and not open_for(cfg, r.id, key[0]):
                import engine as eng
                sig = eng.plan_sha(cfg, r)[:16] + "|" + key[1][:40]
                if not was_handled(cfg, key[0], r.id, sig):
                    open_incident(rig, key[0], r.id, f"{key[1]} (for {int(now - first)}s)", sig=sig)
    for wk in list(waits):
        if wk not in seen and not wk.startswith("rest"):
            waits.pop(wk, None)
    # round reviews that wrote questions nobody answered
    import rounds
    for f in cfg.state.glob("rr-*.dispatch.json"):
        rr = f.name[: -len(".dispatch.json")]
        tag = rr[3:]                                  # a round's tag, or a checkpoint loop's <tag>+<judge> (v3.1)
        v, summ, mt = rig.st.status(rr)
        qd = rounds.review_dir(cfg, tag)
        if v == "DONE" and (qd / "QUESTIONS.md").is_file() and not (qd / "ANSWERS.md").is_file():
            try:
                import courseloop
                if courseloop.load(cfg, tag).get("state") == "review" and tag in courseloop.configs(cfg)[0]:
                    continue            # a course loop's review: the loop opens its own incident (its brief) next tick
            except Exception:
                pass
            sig = str(int(mt))
            if not open_for(cfg, f"round:{tag}", "round_review") and not was_handled(cfg, "round_review", f"round:{tag}", sig):
                open_incident(rig, "round_review", f"round:{tag}",
                              f"round review {rr} wrote QUESTIONS.md; nobody answered — answer within the decision "
                              f"rights (ANSWERS.md), INBOX the Owner's items, then plan the next round", sig=sig)
    # rig at rest: nothing running, work left
    rest = rest_state(rig, rows, notes)
    rk = "rest"
    if rest:
        first = waits.setdefault(rk, now)
        log_rest(cfg, now)
        if now - first >= float(sc.get("rest_after", 1200)) and not open_for(cfg, "rig", "rest"):
            sig = hashlib.sha1(rest.encode()).hexdigest()[:16]
            if not was_handled(cfg, "rest", "rig", sig):
                open_incident(rig, "rest", "rig", f"nothing has run for {int(now - first)}s and work is left: {rest[:400]}",
                              sig=sig)
    else:
        waits.pop(rk, None)
        log_rest(cfg, now, resting=False)
    _atomic(sdir(cfg) / "waits.json", json.dumps(waits, indent=1, sort_keys=True))


def rest_state(rig, rows, notes: Dict[str, str]) -> str:
    """'' unless the rig is at rest with work left: no live session of any kind, nothing READY,
    no STOP/PAUSE, and unfinished, unparked rows in enabled lanes. -> a short description."""
    if rig.stopped() or rig.paused(""):
        return ""
    if any(True for _ in rig.inflight()):
        return ""
    left = []
    for r in rows:
        if not rig.lane_enabled(r.lane) or rig.stopped(r.lane) or rig.paused(r.lane):
            continue
        ph = rig.st.phase(r.id)
        if ph in ("accepted", "parked", "spend_wait"):   # spend_wait: the Owner's money, never a Steward's
            continue
        n = str(notes.get(r.id, ""))
        if n.startswith("READY"):
            return ""
        if n.startswith("PHASE"):        # held by the phase fence: the Owner's phase, never a Steward's
            continue
        left.append(f"{r.id}({ph or 'pending'}: {n[:60]})")
    return "; ".join(left[:12]) + (f" … {len(left) - 12} more" if len(left) > 12 else "") if left else ""


def log_rest(cfg, now: float, resting: bool = True) -> None:
    """Accumulate 'idle while work was left' seconds for the digest (state/steward/rest.json)."""
    f = sdir(cfg) / "rest.json"
    r = _jread(f, {})
    last = float(r.get("last", 0) or 0)
    if resting and r.get("resting") and last:
        day = time.strftime("%F", time.localtime(now))
        r.setdefault("by_day", {})[day] = float(r.get("by_day", {}).get(day, 0)) + max(0.0, min(now - last, 120.0))
    r["resting"] = resting
    r["last"] = now
    _atomic(f, json.dumps(r, sort_keys=True))


# -- duties: periodic commands whose output can open incidents ---------------------------------------
DUTY_NAME = re.compile(r"^[a-z0-9_-]+$")
DUTY_LINE = re.compile(r"^INCIDENT\s+(\S+)(?:\s+(.*\S))?\s*$")


def duty_validate(raw: Any) -> Tuple[List[Dict[str, Any]], List[str]]:
    """steward.duties -> (the valid entries with defaults filled in, the errors). Shared with `orch lint`."""
    out: List[Dict[str, Any]] = []
    errs: List[str] = []
    if raw in (None, [], ()):
        return out, errs
    if not isinstance(raw, list):
        return out, ["steward.duties must be a list of {name, every, cmd, timeout, klass} entries"]
    seen = set()
    for i, d in enumerate(raw):
        tag = f"steward.duties[{i}]"
        if not isinstance(d, dict):
            errs.append(f"{tag}: not a mapping")
            continue
        name = d.get("name")
        if isinstance(name, str) and name:
            tag = f"steward.duties[{i}] ({name})"
        bad = []
        if not isinstance(name, str) or not DUTY_NAME.match(name):
            bad.append("name must match [a-z0-9_-]+")
        elif name in seen:
            bad.append("duplicate name")
        every = d.get("every")
        if isinstance(every, bool) or not isinstance(every, int) or every < DUTY_MIN_EVERY:
            bad.append(f"every must be an integer >= {DUTY_MIN_EVERY} (seconds between starts)")
        cmd = d.get("cmd")
        if not isinstance(cmd, str) or not cmd.strip():
            bad.append("cmd must be a non-empty string")
        timeout = d.get("timeout", 600)
        if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout < 1:
            bad.append("timeout must be a positive integer (seconds, default 600)")
        klass = d.get("klass", "judgment")
        if klass not in DUTY_KLASSES:
            bad.append(f"klass must be one of {'|'.join(DUTY_KLASSES)} (default judgment)")
        if isinstance(name, str):
            seen.add(name)
        if bad:
            errs.extend(f"{tag}: {b}" for b in bad)
            continue
        out.append({"name": name, "every": every, "cmd": cmd, "timeout": timeout, "klass": klass})
    return out, errs


def duties(cfg) -> Tuple[List[Dict[str, Any]], List[str]]:
    return duty_validate(scfg(cfg).get("duties"))


def duty_out(cfg, name: str) -> Path:
    return sdir(cfg) / f"duty_{name}.out"


def _duty_state(cfg) -> Dict[str, Any]:
    return _jread(sdir(cfg) / "duties.json", {})


def _duty_save(cfg, state: Dict[str, Any]) -> None:
    _atomic(sdir(cfg) / "duties.json", json.dumps(state, indent=1, sort_keys=True))


def duty_tick(rig) -> None:
    """Start the duties that are due (engine tick, Steward lane enabled only). Each runs as an async job
    (Engine.start_job) so a slow duty never blocks a tick; at most one run per duty; the last start is persisted
    (state/steward/duties.json) so an engine restart does not re-fire a duty before its `every` has passed."""
    cfg = rig.cfg
    if not enabled(cfg) or not hasattr(rig, "start_job"):
        return
    valid, errs = duties(cfg)
    for e in errs:
        if rig.st.once("duty-config-" + hashlib.sha1(e.encode()).hexdigest()[:12]):
            rig.st.engine_log(f"steward: duty config ignored — {e}")
    if not valid:
        return
    now = time.time()
    state = _duty_state(cfg)
    for d in valid:
        name = d["name"]
        key = f"duty:{name}"
        if rig.job_running(key):
            continue
        rec = state.get(name) or {}
        start = float(rec.get("start", 0) or 0)
        if now - start < d["every"]:
            continue
        if rec.get("running") and now - start < d["timeout"] + 30:
            continue        # a run the previous engine started may still be alive (its `timeout` bounds it)
        rec = {"start": now, "running": True, "rc": rec.get("rc"), "end": rec.get("end", 0)}
        state[name] = rec
        _duty_save(cfg, state)
        try:
            rig.start_job(key, ["bash", "-c", f"timeout {int(d['timeout'])} bash -c {shlex.quote(d['cmd'])}"],
                          duty_out(cfg, name))
        except Exception as e:
            rec.update(running=False, end=now, rc=-1)
            _duty_save(cfg, state)
            rig.st.engine_log(f"steward: duty {name} could not start ({type(e).__name__}: {e})")
            continue
        rig.st.engine_log(f"steward: duty {name} started (every {d['every']}s, timeout {d['timeout']}s)")


def on_duty_done(rig, name: str, rc: int) -> None:
    """A duty's job ended: record it, open the incidents its output reports (INCIDENT lines, else a failure)."""
    cfg = rig.cfg
    state = _duty_state(cfg)
    rec = state.setdefault(name, {})
    rec.update(running=False, end=time.time(), rc=rc)
    _duty_save(cfg, state)
    d = next((x for x in duties(cfg)[0] if x["name"] == name), None)
    if d is None or not enabled(cfg):
        rig.st.engine_log(f"steward: duty {name} ended (exit {rc}); not configured or the lane is off — output ignored")
        return
    try:
        text = duty_out(cfg, name).read_text(errors="replace")
    except Exception:
        text = ""
    reports: List[Tuple[str, str, str]] = []
    for ln in text.splitlines():
        m = DUTY_LINE.match(ln)
        if m:
            reports.append((m.group(1)[:120], m.group(2) or f"duty {name} reported {m.group(1)[:120]}", ln))
    if len(reports) > DUTY_MAX_LINES:
        rig.st.engine_log(f"steward: duty {name} printed {len(reports)} INCIDENT lines; only the first {DUTY_MAX_LINES} count")
        reports = reports[:DUTY_MAX_LINES]
    if not reports and rc != 0:
        first = next((x.strip() for x in text.splitlines() if x.strip()), "(no output)")
        summ = f"duty {name} failed: exit {rc}: {cell(first, 200)}"
        reports = [(f"duty:{name}", summ, f"(exit {rc}{', timeout' if rc == 124 else ''}) {cell(first, 200)}")]
    opened = 0
    for subject, summ, line in reports:
        sig = hashlib.sha1(f"{name}\n{subject}\n{summ}".encode()).hexdigest()[:16]
        if open_for(cfg, subject, "duty") or was_handled(cfg, "duty", subject, sig):
            continue
        inc = open_incident(rig, "duty", subject, summ, klass=d["klass"], sig=sig)
        if inc:
            inc["duty"] = {"name": name, "line": line, "out": f"duty_{name}.out", "rc": rc}
            save_incident(cfg, inc)
            opened += 1
    rig.st.engine_log(f"steward: duty {name} ended (exit {rc}); {len(reports)} report(s), {opened} incident(s) opened")


def duty_status_lines(cfg) -> List[str]:
    valid, errs = duties(cfg)
    if not valid and not errs:
        return []
    state = _duty_state(cfg)
    now = time.time()
    lines = [f"Duties ({len(valid)}; run only while the Steward lane is on, cwd = the project root):"]
    for d in valid:
        rec = state.get(d["name"]) or {}
        start = float(rec.get("start", 0) or 0)
        ls = time.strftime("%F %T", time.localtime(start)) if start else "never"
        if rec.get("running"):
            lx = "running"
        elif rec.get("rc") is None:
            lx = "-"
        else:
            lx = f"exit {rec['rc']}"
        due = start + d["every"]
        nxt = "now" if (not start or due <= now) else time.strftime("%F %T", time.localtime(due))
        lines.append(f"  {d['name']:<16} every {d['every']}s  class {d['klass']:<10} last start {ls}  last {lx}  next due {nxt}")
    lines += [f"  CONFIG ERROR: {e}" for e in errs]
    return lines


def tick(rig, notes: Dict[str, str]) -> None:
    """Engine tick phase: requests, scan, drive sessions, planners."""
    cfg = rig.cfg
    if not enabled(cfg):
        return
    import panel
    consume_requests(rig)
    scan(rig, notes)
    try:
        duty_tick(rig)
    except Exception as e:           # a duty never breaks the lane
        if rig.st.once(f"duty-tick-{type(e).__name__}"):
            rig.st.engine_log(f"steward: duty tick failed ({type(e).__name__}: {e})")
    try:
        answer_scan(rig)
    except Exception as e:           # nor does the cadence
        if rig.st.once(f"answer-scan-{type(e).__name__}"):
            rig.st.engine_log(f"steward: answer scan failed ({type(e).__name__}: {e})")
    drive_planners(rig)
    sc = scfg(cfg)
    live = rig.inflight()
    n_live = len([s for s in live if s.startswith("sw-")])
    rig.steward_wants = set()
    for inc in incidents(cfg):
        if inc.get("status") == "closed":
            continue
        sessions = inc.get("sessions") or []
        cur = sessions[-1] if sessions else ""
        if cur and cur in live:
            continue
        if cur and inc.get("status") == "working":
            v, summary = rig.sub_verdict(cur, float(inc.get("session_epoch", 0)) - 1)
            if panel.is_falsifier(cur) and v not in ("NONE", "FORGED"):
                # only CONFIRM executes; any other verdict (OBJECT, an idle or launch-failure verdict) is OBJECT
                rig.consume(cur)
                rig.st.event(f"steward:{inc['n']}", "steward_verdict", session=cur, verdict=v, summary=cell(summary, 300))
                panel.on_falsifier_verdict(rig, inc, cur, v, summary)
                continue
            if v in VERDICTS:
                rig.consume(cur)
                rig.st.event(f"steward:{inc['n']}", "steward_verdict", session=cur, verdict=v, summary=cell(summary, 300))
                on_verdict(rig, inc, cur, v, summary)
                continue
            if v not in ("NONE",):
                rig.st.set_aside(cur, "bad")
            d = rig.st.dispatch(cur)
            if d.get("interrupted"):
                # cut off by a reboot / tmux restart / infra: not charged, the same tier runs again
                inc["sessions"] = sessions[:-1]
                inc["session_days"] = (inc.get("session_days") or [])[:-1]
                inc["status"] = "open"
                save_incident(cfg, inc)
            elif d.get("ended") or not d.get("open"):
                inc.setdefault("verdicts", []).append(f"{cur}: ended without a verdict ({d.get('end', '?')})")
                inc["status"] = "open"
                save_incident(cfg, inc)
            else:
                continue
        inc = load_incident(cfg, int(inc["n"]))
        if inc.get("status") == "closed":
            continue
        if inc.get("klass") == "inbox":
            add_inbox(rig, inc["n"], f"{inc['kind']} on {inc['subject']}", inc["summary"],
                      row=inc["subject"] if not str(inc["subject"]).startswith(("health:", "rig", "round:")) else "")
            close(rig, inc, "inbox", "Owner-only item recorded in the inbox")
            continue
        own = [s for s in inc.get("sessions") or [] if not panel.is_panel(s)]
        if inc.get("klass") == "panel":
            if panel.blocked(rig, inc):          # no panel here / its budget spent: park + inbox
                continue
        elif len(own) >= int(sc.get("max_sessions_per_incident", 3)):
            finalize_unresolved(rig, inc, f"{len(own)} Steward sessions did not resolve it")
            continue
        if rig.stopped():
            continue
        if sessions_today(cfg) >= int(sc.get("sessions_per_day", 40)):
            if rig.st.once(f"steward-daycap-{time.strftime('%F')}"):
                rig.st.engine_log("steward: daily session budget used (steward.sessions_per_day) — incidents wait")
            continue
        if n_live >= int(sc.get("max_parallel", 1)):
            continue
        if inc.get("klass") == "panel":
            x = panel.executor_for(cfg, inc)
        else:
            tiers = tiers_for(cfg, inc["klass"])
            x = tiers[min(int(inc.get("tier_idx", 0)), len(tiers) - 1)]
        ok_cap, why = rig.capacity_ok(x, steward=True)
        if not ok_cap:
            rig.steward_wants.add(x)
            rig.sched_notes[f"sw-incident-{inc['n']}"] = f"Steward waiting for capacity: {why}"
            continue
        ok, how = dispatch_next(rig, inc)
        if ok:
            n_live += 1
        else:
            rig.st.engine_log(f"steward: incident {inc['n']} session not dispatched — {how}")
    try:
        idle_watch(rig)
    except Exception as e:
        if rig.st.once(f"idle-watch-{type(e).__name__}"):
            rig.st.engine_log(f"steward: idle watch failed ({type(e).__name__}: {e})")


# -- cadence: the Owner's answers wake the Steward; the idle watchdog pages (README.md (Cadence)) --
WAKE_FILE = "STEWARD_WAKE"
ANSWER_DEBOUNCE = 120
ANSWER_MAX_WAIT = 600
IDLE_FLAP = 1800            # a new idle episode this soon after a page continues that page's sequence
IDLE_MIN_AFTER = 300
DEFAULT_ON_ANSWER = ("act on the answers within the decision rights: unpark what they release, and plan the next "
                     "work (`orch sw plan <round> \"brief\"`) when they open it")


def cadence_validate(sc: Dict[str, Any]) -> List[str]:
    """steward cadence switches -> errors. Shared with `orch lint`."""
    errs: List[str] = []
    if "wake_on_answer" in sc and not isinstance(sc["wake_on_answer"], bool):
        errs.append("steward.wake_on_answer must be true or false")
    for key, lo in (("answer_debounce", 0), ("answer_max_wait", 1), ("idle_pages_per_day", 1)):
        v = sc.get(key)
        if key in sc and (isinstance(v, bool) or not isinstance(v, int) or v < lo):
            errs.append(f"steward.{key} must be an integer >= {lo}")
    after = sc.get("idle_page_after", 0)
    if isinstance(after, bool) or not isinstance(after, int) or (after != 0 and after < IDLE_MIN_AFTER):
        errs.append(f"steward.idle_page_after must be 0 (off) or an integer >= {IDLE_MIN_AFTER} (seconds)")
        after = 0
    rp = sc.get("idle_repage", [])
    if not isinstance(rp, list) or any(isinstance(x, bool) or not isinstance(x, int) for x in rp) \
            or any(b <= a for a, b in zip([after] + rp, rp)):
        errs.append("steward.idle_repage must be a list of integer seconds, increasing, each past idle_page_after")
    if "on_answer" in sc and not isinstance(sc["on_answer"], str):
        errs.append("steward.on_answer must be a string (the rig's instruction; {ks} = the answered items)")
    return errs


def on_answer_text(cfg, ks: str) -> str:
    return str(scfg(cfg).get("on_answer") or DEFAULT_ON_ANSWER).replace("{ks}", ks)


def mark_inbox_done(cfg, k: int, letter: str = "", note: str = "", by: str = "owner") -> None:
    rec: Dict[str, Any] = {"k": int(k), "status": "done", "done": time.strftime("%F %T"), "done_epoch": int(time.time()),
                           "done_by": by}
    if letter:
        rec["answer"] = letter.strip().upper()[:1]
    note = " ".join(str(note or "").split())[:1000]
    if note:
        rec["note"] = note
    _append(sdir(cfg) / "inbox.jsonl", json.dumps(rec, sort_keys=True) + "\n")


def item_letters(body: str) -> List[str]:
    """The option letters an inbox item offers ("A) …", "A. …", "**A** …", "(A) …" at a line start)."""
    return sorted(set(re.findall(r"(?m)^\s*[-*]*\s*(?:\*\*)?\(?([A-H])(?:[).:\]]|\*\*)", body or "")))


def consume_inbox_answers(rig) -> None:
    """watchdog.inbox_answers: control/INBOX_ANSWER_<k>.json (the bot, from the Owner's Telegram answer) closes the
    open inbox item k with that letter. Anything else is refused and set aside (state/answers/)."""
    cfg = rig.cfg
    if not (cfg.data.get("watchdog") or {}).get("inbox_answers"):
        return
    files = sorted(cfg.control.glob("INBOX_ANSWER_*.json"))
    if not files:
        return
    done = cfg.state / "answers"
    done.mkdir(exist_ok=True)
    open_ = {int(i["k"]): i for i in inbox_items(cfg)}
    for f in files:
        tag = f.stem[len("INBOX_ANSWER_"):]
        try:
            k = int(tag)
            rec = json.loads(f.read_text())
        except Exception:
            k, rec = -1, {}
        letter = str(rec.get("letter", "")).strip().upper()[:1]
        if k not in open_ or rec.get("k") != k or not letter:
            os.replace(f, done / f"inbox{tag}.{int(time.time())}.refused.json")
            rig.st.engine_log(f"steward: Telegram answer for inbox {tag} refused (no such open item, or no letter)")
            continue
        mark_inbox_done(cfg, k, letter=letter, note=str(rec.get("note", "")), by="telegram")
        open_.pop(k, None)
        os.replace(f, done / f"inbox{k}.{int(time.time())}.json")
        rig.st.event(f"steward:inbox-{k}", "inbox_answer", k=k, letter=letter, by="telegram")
        rig.st.engine_log(f"steward: inbox item {k} answered {letter} by Telegram")


def answer_scan(rig, now: Optional[float] = None) -> None:
    """The Owner's acts that must wake the rig within minutes, not at the next clock: a Telegram inbox answer, an
    `orch steward wake`, and (steward.wake_on_answer) every inbox item the Owner closes — batched while the Owner is
    still answering (steward.answer_debounce seconds of quiet, at most steward.answer_max_wait after the first)."""
    cfg = rig.cfg
    sc = scfg(cfg)
    now = time.time() if now is None else now
    consume_inbox_answers(rig)
    w = cfg.control / WAKE_FILE
    if w.exists():
        why = cell(w.read_text(errors="replace"), 300)
        w.unlink(missing_ok=True)
        inc = open_incident(rig, "wake", "rig:wake", f"the Owner asked to wake the rig: {why} — {on_answer_text(cfg, 'wake')}",
                            klass="mechanical", sig=str(int(now)))
        if inc and not inc.get("why"):
            inc.update(why=why, ks="wake")
            save_incident(cfg, inc)
    if not sc.get("wake_on_answer"):
        return
    f = sdir(cfg) / "answers_seen.json"
    done = {int(i["k"]): i for i in inbox_items(cfg, open_only=False) if i.get("status") == "done"}
    seen = _jread(f, {}) if f.exists() else None
    if not isinstance(seen, dict) or "seen" not in seen:   # the switch just went on: what is answered is the baseline
        _atomic(f, json.dumps({"seen": sorted(done), "pending": {}, "last_new": 0}, sort_keys=True))
        return
    pend = {int(k): float(v) for k, v in (seen.get("pending") or {}).items()}
    known = set(int(k) for k in seen.get("seen") or []) | set(pend)
    new = [k for k in sorted(done) if k not in known]
    for k in new:
        pend[k] = now
    last_new = now if new else float(seen.get("last_new", 0) or 0)
    if pend and not open_for(cfg, "rig:answers", "answered"):
        deb = float(sc.get("answer_debounce", ANSWER_DEBOUNCE))
        mx = float(sc.get("answer_max_wait", ANSWER_MAX_WAIT))
        if now - last_new >= deb or now - min(pend.values()) >= mx:
            ks = sorted(pend)
            answers = [{"k": k, "title": cell(done.get(k, {}).get("title", ""), 120), "answer": done.get(k, {}).get("answer", ""),
                        "note": cell(done.get(k, {}).get("note", ""), 300), "by": done.get(k, {}).get("done_by", "")}
                       for k in ks]
            what = "; ".join(f"{a['k']} {cell(a['title'], 50)}" + (f" = {a['answer']}" if a["answer"] else "") for a in answers)
            inc = open_incident(rig, "answered", "rig:answers",
                                f"the Owner answered inbox {', '.join(map(str, ks))} ({what}) — "
                                f"{on_answer_text(cfg, ','.join(map(str, ks)))}",
                                klass="mechanical", sig="+".join(map(str, ks)))
            if inc:
                inc.update(answers=answers, ks=",".join(map(str, ks)))
                save_incident(cfg, inc)
                seen["seen"] = sorted(set(int(k) for k in seen.get("seen") or []) | set(ks))
                pend = {}
    seen.update(pending={str(k): v for k, v in pend.items()}, last_new=last_new)
    _atomic(f, json.dumps(seen, sort_keys=True))


def cadence_status_lines(cfg) -> List[str]:
    sc = scfg(cfg)
    after = int(sc.get("idle_page_after") or 0)
    if not sc.get("wake_on_answer") and not after:
        return []
    st = _jread(sdir(cfg) / "idle.json", {})
    today = float((st.get("by_day") or {}).get(time.strftime("%F"), 0))
    since = float(st.get("since") or 0)
    return [f"Cadence: wake on answer {'on' if sc.get('wake_on_answer') else 'off'}"
            f" (debounce {sc.get('answer_debounce', ANSWER_DEBOUNCE)}s) · idle page "
            + (f"after {after}s, repage {sc.get('idle_repage') or []}, ≤{sc.get('idle_pages_per_day', 4)}/24h" if after else "off")
            + f" · idle today {int(today // 60)} min" + (f" · idle now {int((time.time() - since) // 60)} min" if since else "")]


def _phase_open(cfg) -> bool:
    try:
        import phases
        if not phases.active(cfg):
            return True
        return any(str(p.get("state")) == "open" for p in phases.load(cfg).values())
    except Exception:
        return True


def idle_page_text(rig, mins: int) -> Tuple[str, str, str, str]:
    """-> (class, title, body, row): the ELI5 page — what is wrong, what the Owner does. No row content beyond an
    inbox item's title."""
    cfg = rig.cfg
    items = inbox_items(cfg)
    tg = bool((cfg.data.get("watchdog") or {}).get("inbox_answers"))
    if items:
        it = items[-1]
        k = it["k"]
        more = f" (+{len(items) - 1} more open)" if len(items) > 1 else ""
        title = f"{cfg.name} STALLED {mins} min — nothing is running"
        how = f'reply "inbox {k} A" (your letter), or answer on the laptop' if tg else \
            f"answer on the laptop: orch steward inbox done {k} <LETTER>"
        body = f'Why: it needs your call on inbox {k} "{cell(it.get("title", ""), 70)}"{more}.\nDo: {how}.'
        return "stall", title, body, f"inbox:{k}"
    planners = _jread(sdir(cfg) / "planners.json", {})
    last = max(planners.items(), key=lambda kv: float(kv[1].get("epoch", 0) or 0), default=None)
    lead = f"last lead {last[0]} {last[1].get('status', '?')}" if last else "no lead dispatched yet"
    title = f"{cfg.name} IDLE {mins} min — nothing is running, nothing waits on you"
    body = (f"Why: the Steward did not set up work ({lead}).\n"
            f'Do: open tmux; run orch steward wake "why", or check the last lead.')
    return "idle", title, body, ""


def idle_watch(rig, now: Optional[float] = None) -> None:
    """steward.idle_page_after: no session of ANY kind live (rows, Steward, planners, reviews, unblocks, panel; a
    manual row's dispatch is a person, not work) while a lane is on, the phase open and no STOP/PAUSE -> the engine
    itself pages: the rig is stalled on the Owner, or the Steward failed to set up work. Parked and accepted rows do
    not count, unlike `rest`. One page per mark (idle_page_after, then each idle_repage), at most idle_pages_per_day
    in any 24 h; a new episode within IDLE_FLAP of a page continues its sequence. Idle seconds -> idle.json by_day."""
    cfg = rig.cfg
    sc = scfg(cfg)
    after = sc.get("idle_page_after", 0)
    if isinstance(after, bool) or not isinstance(after, int) or after <= 0:
        return
    now = time.time() if now is None else now
    f = sdir(cfg) / "idle.json"
    st = _jread(f, {})
    watching = (not rig.stopped() and not rig.paused("") and any(cfg.control.glob("RUN_*")) and _phase_open(cfg))
    live = [s for s, d in rig.inflight().items() if d.get("executor") != "manual"]
    idle = watching and not live
    last = float(st.get("last", 0) or 0)
    if idle and st.get("idle") and last:
        day = time.strftime("%F", time.localtime(now))
        bd = st.setdefault("by_day", {})
        bd[day] = float(bd.get(day, 0)) + max(0.0, min(now - last, 120.0))
    st.update(last=now, idle=idle)
    if not idle:
        st["since"] = 0
        _atomic(f, json.dumps(st, sort_keys=True))
        return
    if not st.get("since"):
        st["since"] = now
        if now - float(st.get("last_page", 0) or 0) > IDLE_FLAP:
            st["marks"] = 0
    dur = now - float(st["since"])
    marks = [after] + [int(x) for x in (sc.get("idle_repage") or []) if isinstance(x, int)]
    m = int(st.get("marks", 0) or 0)
    if m < len(marks) and dur >= marks[m]:
        st["marks"] = m + 1
        recent = [float(p) for p in st.get("pages") or [] if now - float(p) < 86400]
        cap = int(sc.get("idle_pages_per_day", 4))
        if len(recent) >= cap:
            rig.st.engine_log(f"steward: idle {int(dur)}s — no page (steward.idle_pages_per_day {cap} reached)")
        else:
            klass, title, body, row = idle_page_text(rig, int(dur // 60))
            st.update(pages=recent + [now], last_page=now)
            _atomic(f, json.dumps(st, sort_keys=True))
            rig.st.event("steward:idle", "idle_page", page_class=klass, idle_s=int(dur))
            rig.st.engine_log(f"steward: idle {int(dur)}s — page ({klass}): {title}")
            rig.notify("critical", title, body, row=row, page_class=klass)
            return
    _atomic(f, json.dumps(st, sort_keys=True))


# -- CLI ------------------------------------------------------------------------------------------------
def cli(argv: List[str]) -> int:
    from config_parser import load
    cfg = load()
    import capability
    sid = capability.session(cfg)
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__.split("A Steward session acts")[1].split("Caps:")[0].strip())
        print("\nactions: " + " ".join(sorted(STEWARD_ACTIONS)))
        return 0
    action, args = argv[0], argv[1:]
    if not sid or not (sid.startswith("sw-") or sid.startswith("pl-")):
        print(f"REFUSED: `orch sw` is for Steward (sw-) and planner (pl-) sessions; the operator uses the plain "
              f"commands (orch reset, orch approve, orch park …)")
        return 1
    if not args and action not in ("note", "restart-engine"):
        print(f'usage: orch sw {action} <args…> "<why>"  — the last argument is always the reason')
        return 2
    why = args[-1] if args else ""
    req = request(cfg, sid, action, args[:-1] if args else [], why)
    res = wait_result(req)
    out = res.get("outcome")
    if out == "done" and res.get("staged"):
        print(f"STAGED {action}: {res.get('why', '')}")
        return 0
    if out == "done":
        print(f"DONE {action}: {res.get('why', '')}  (STEWARD_LOG #{res.get('log', '?')}; revert: {res.get('revert', '')})")
        return 0
    print(f"{'REFUSED' if out == 'refused' else 'NO ANSWER'} {action}: {res.get('why', '')}")
    return 1 if out == "refused" else 4


def ops(argv: List[str]) -> int:
    """`orch steward status|incidents|log|inbox` — read-only views; `orch steward inbox done <k>` and `close <n> "why"`
    (the Owner's, refused inside any session); `orch park|unpark`."""
    from config_parser import load
    from engine import Rig
    cfg = load()
    import capability
    me = capability.session(cfg)           # the pane a caller descends from, or ORCH_ROW (v2)
    sub = argv[0] if argv else "status"
    if sub in ("status", "incidents"):
        incs = incidents(cfg)
        opn = [i for i in incs if i.get("status") != "closed"]
        print(f"Steward lane: {'ON' if enabled(cfg) else 'off'} · {len(opn)} open incident(s) · {len(incs)} total")
        for i in (incs if sub == "incidents" else opn):
            print(f"  #{i['n']:<4} {i.get('status'):<8} {i.get('kind'):<14} {i.get('klass'):<10} {i.get('subject'):<14} "
                  f"{(i.get('outcome') or ''):<10} {cell(i.get('summary', ''), 90)}")
        for ln in duty_status_lines(cfg):
            print(ln)
        for ln in cadence_status_lines(cfg):
            print(ln)
        items = inbox_items(cfg)
        if items:
            print(f"Owner inbox: {len(items)} open")
            for it in items:
                print(f"  {it['k']}. {it['title']} (row {it.get('row') or '-'})")
        return 0
    if sub == "log":
        f = hdir(cfg) / "STEWARD_LOG.md"
        print(f.read_text() if f.exists() else "(no Steward acts yet)")
        return 0
    if sub == "inbox":
        if len(argv) >= 3 and argv[1] == "done":
            if me:
                print(f"REFUSED: only the Owner closes an inbox item (session {me})")
                return 1
            k = int(argv[2])
            rest = argv[3:]
            letter = rest[0].upper() if rest and re.fullmatch(r"[A-Za-z]", rest[0]) else ""
            note = " ".join(rest[1:] if letter else rest)
            mark_inbox_done(cfg, k, letter=letter, note=note)
            print(f"inbox item {k} marked done" + (f" (answer {letter})" if letter else ""))
            return 0
        for it in inbox_items(cfg):
            print(f"{it['k']}. {it['title']}  (row {it.get('row') or '-'}, {it['ts']})\n{it.get('body', '')}\n")
        return 0
    if sub == "wake":
        if me:
            print(f"REFUSED: only the Owner wakes the rig (session {me})")
            return 1
        why = " ".join(argv[1:]).strip()
        if not why:
            print('usage: orch steward wake "why"  — the reason is required')
            return 2
        _atomic(cfg.control / WAKE_FILE, why[:300] + "\n")
        print("wake recorded: the engine opens a `wake` incident on its next tick")
        return 0
    if sub == "close":
        if me:
            print(f"REFUSED: only the Owner closes an incident by hand (session {me})")
            return 1
        why = " ".join(argv[2:]).strip()
        if len(argv) < 2 or not argv[1].isdigit() or not why:
            print('usage: orch steward close <n> "why"  — the reason is required')
            return 2
        n = int(argv[1])
        inc = load_incident(cfg, n)
        if not inc:
            print(f"REFUSED: no incident {n}")
            return 1
        if inc.get("status") == "closed":
            print(f"REFUSED: incident {n} is already closed ({inc.get('outcome', '?')})")
            return 1
        rig = Rig(cfg)
        live = rig.inflight()
        busy = [x for x in (inc.get("sessions") or []) if x in live]
        if busy:
            print(f"REFUSED: incident {n} has a live session ({busy[-1]}); it ends or is stopped first")
            return 1
        close(rig, inc, "owner-closed", why)
        print(f"incident {n} closed (owner-closed): {why}")
        return 0
    if sub in ("park", "unpark"):
        if me:
            print(f"REFUSED: `orch {sub}` is the operator's command (session {me}); a Steward uses `orch sw {sub}`")
            return 1
        if len(argv) < 2:
            print(f'usage: orch {sub} <row> ["why"]')
            return 2
        rig = Rig(cfg)
        if sub == "park":
            ok = park(rig, argv[1], " ".join(argv[2:]) or "parked by the Owner", by="owner")
            print(f"{argv[1]} {'parked' if ok else 'not parked (already parked or accepted)'}")
            return 0 if ok else 1
        ok, how = unpark(rig, argv[1], by="owner")
        print(f"{argv[1]}: {how}")
        return 0 if ok else 1
    print("usage: orch steward status|incidents|log|inbox [done <k> [LETTER] [\"note\"]]|wake \"why\"|close <n> \"why\" · "
          "orch park|unpark <row>")
    return 2


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "sw":
        sys.exit(cli(sys.argv[2:]))
    sys.exit(ops(sys.argv[1:]))
