#!/usr/bin/env python3
"""Executor variants and the rate-limit fallback (v3.2).

A non-Claude executor (an OpenCode model on a provider plan, a Gemini key) can hit its provider's rate limit
and sit there for hours. The engine already waits out a Claude usage limit ("the session waits"). For an
executor that has a `fallback` entry it does something else: it moves the work to another executor, as
mechanical acts the Owner can read and revert, and moves it back on request.

Configuration (orch.yaml, all optional, all empty by default):

  executors:
    variants:                       a second executor on the same adapter
      opencode_gemini: {base: opencode, model: "google/gemini-4-argon", env: {…}, args: […], max_parallel: 3}
    limit_patterns:                 an executor name or an adapter family -> one regex or a list (matched, case
      opencode: ["Rate limit exceeded\\. Please try again later", "FreeUsageLimitError"]   insensitive, against
    limit_confirm_secs: 60          the last 40 lines of the pane)
    fallback:
      opencode: {to: [opencode_gemini], limited_rows: 2, limited_minutes: 10}

Detection (Rig.observe, once per engine tick per live row of an executor that has a fallback entry): the pane
tail matches a limit pattern and the tail does not otherwise change (digits inside the matching lines are
ignored, spinner glyphs are dropped) for `limited_confirm_secs`. Any tick on which the pattern is gone or other
pane text moved starts the clock again, so a "429" printed once inside ordinary row output never fires.

Trigger (tick): `limited_rows` confirmed rows of one executor at once, or one confirmed for `limited_minutes`
minutes. Once an executor is "in fallback" every later confirmed row of it moves at once.

The acts (each a Steward act in steward/STEWARD_LOG.md with its revert `orch assign <row> X`; one incident of
kind executor_fallback, closed `resolved`, no model session; one digest line; one Owner notification of page
class `executor_fallback`, critical only if the rig lists the class in steward.page_classes):
  a  every confirmed rate-limited live row of X: window and process tree killed (tmuxio.kill), row reset,
     assigned to the first executor in `to` that is not itself in fallback (a target that is in fallback
     yields to the target of its own entry — a chain; lint refuses a cycle);
  b  every pending (not yet dispatched) row whose executor is X: assigned to that target;
  c  live rows of X that work normally are left alone;
  d  X is marked in fallback (state/fallback/X.json): a row assigned to X later is dispatched on the target
     (Rig.resolve_exec), until `orch executor restore X`.
A parked row is never touched. A Claude executor's usage-limited row is untouched unless that executor has a
`fallback` entry of its own. The mechanism is mechanical, so it works with the Steward lane on or off; with the
lane off the log, the incident and the digest line are written the same way (only a lane-off incident is never
picked up by a session: it is closed when it is opened).

Restore: `orch executor restore X ["why"]` (the Owner) and `orch sw executor-restore X "why"` (a Steward):
the pending rows the fallback moved are given back to X (rows already running on the target keep it).
`orch executor status` shows each executor's state; `orch executor fallback X --dry-run` lists what an
engagement would do now and changes nothing; `orch executor fallback X` engages by hand.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))

TAIL_LINES = 40
PAGE_CLASS = "executor_fallback"
DEFAULT_ROWS = 2
DEFAULT_MINUTES = 10.0
SPINNER = re.compile("[⠀-⣿]")


def _now() -> float:
    return time.time()


def kind_of(sid: str) -> str:
    from engine import kind_of as k
    return k(sid)


# -- configuration -----------------------------------------------------------------------------
def entries(cfg) -> Dict[str, Dict[str, Any]]:
    """executors.fallback, normalised: {X: {to: [..], limited_rows: int, limited_minutes: float}}. Malformed
    entries are dropped here and named by problems() (`orch lint`)."""
    raw = cfg.data["executors"].get("fallback") or {}
    out: Dict[str, Dict[str, Any]] = {}
    if not isinstance(raw, dict):
        return out
    for x, e in raw.items():
        if not isinstance(e, dict):
            continue
        to = e.get("to")
        to = [to] if isinstance(to, str) else to
        if not isinstance(to, list) or not to or not all(isinstance(t, str) and t for t in to):
            continue
        try:
            rows = int(e.get("limited_rows", DEFAULT_ROWS))
            mins = float(e.get("limited_minutes", DEFAULT_MINUTES))
        except (TypeError, ValueError):
            continue
        if rows < 1 or mins <= 0:
            continue
        out[str(x)] = {"to": [str(t) for t in to], "limited_rows": rows, "limited_minutes": mins}
    return out


def has_entry(cfg, x: str) -> bool:
    return bool(x) and x in entries(cfg)


def patterns(cfg, x: str) -> List["re.Pattern"]:
    """The limit patterns of executor x (its own entry, its variant base's, its adapter family's)."""
    raw = cfg.pick(cfg.data["executors"].get("limit_patterns") or {}, x, None)
    raw = [raw] if isinstance(raw, str) else (raw if isinstance(raw, list) else [])
    out = []
    for p in raw:
        try:
            out.append(re.compile(str(p), re.I))
        except re.error:
            continue
    return out


def confirm_secs(cfg) -> float:
    try:
        return float(cfg.data["executors"].get("limit_confirm_secs", 60))
    except (TypeError, ValueError):
        return 60.0


def problems(cfg) -> Tuple[List[str], List[str]]:
    """-> (errors, warnings) for `orch lint`: variants, fallback entries, cycles, patterns."""
    errs: List[str] = []
    warns: List[str] = []
    e = cfg.data["executors"]
    allowed = set(e.get("allowed") or [])
    from config_parser import DEFAULT_FLAVORS
    builtin = set(DEFAULT_FLAVORS) | set((e.get("adapters") or {}).keys()) | set(e.get("flavors") or {})
    raw_v = e.get("variants") or {}
    if raw_v and not isinstance(raw_v, dict):
        errs.append("executors.variants must be a mapping {name: {base: <executor>, …}}")
        raw_v = {}
    adapters_dir = Path(__file__).resolve().parent / "adapters"
    for name, v in raw_v.items():
        k = f"executors.variants.{name}"
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", str(name)):
            errs.append(f"{k}: the name must be letters, digits, _ or - (it is a tmux-safe executor name)")
        if str(name) in builtin and str(name) not in (e.get("variants") or {}):
            errs.append(f"{k}: collides with a built-in executor name")
        if not isinstance(v, dict):
            errs.append(f"{k}: must be a mapping {{base: <executor>, …}}")
            continue
        b = v.get("base")
        if not isinstance(b, str) or not b:
            errs.append(f"{k}: needs base: <executor>")
            continue
        if b == name:
            errs.append(f"{k}: base is the variant itself")
        elif b in raw_v:
            errs.append(f"{k}: base '{b}' is itself a variant (one level only)")
        elif b not in allowed and b not in builtin:
            errs.append(f"{k}: base '{b}' is not a known executor")
        else:
            ad = cfg.adapter(name)
            if not (adapters_dir / f"{ad}.sh").is_file():
                errs.append(f"{k}: base '{b}' has no adapter lib/adapters/{ad}.sh")
        for key, typ in (("env", dict), ("model", str), ("max_parallel", int)):
            if key in v and v[key] is not None and not isinstance(v[key], typ):
                errs.append(f"{k}.{key}: must be a {typ.__name__}")
        if "args" in v and v["args"] is not None and not isinstance(v["args"], (list, str)):
            errs.append(f"{k}.args: must be a list or a string")
        flags = str(((v.get("env") or {}) if isinstance(v.get("env"), dict) else {}).get("ORCH_OPENCODE_FLAGS", ""))
        if re.search(r"(^|\s)(--model|-m)(\s|=)", flags):
            warns.append(f"{k}.env.ORCH_OPENCODE_FLAGS: the OpenCode TUI has no --model flag (it prints its help and "
                         f"exits); use `model: provider/model` on the variant")
        if v.get("model") and cfg.adapter(name) not in ("opencode", "claude"):
            warns.append(f"{k}.model: only the opencode and claude adapters read a variant's model; "
                         f"'{cfg.adapter(name)}' ignores it")
    fb = e.get("fallback") or {}
    if fb and not isinstance(fb, dict):
        errs.append("executors.fallback must be a mapping {executor: {to: [...], limited_rows: N, limited_minutes: M}}")
        fb = {}
    graph: Dict[str, List[str]] = {}
    for x, ent in fb.items():
        k = f"executors.fallback.{x}"
        if x not in allowed:
            errs.append(f"{k}: '{x}' is not in executors.allowed (or a declared variant)")
        if not isinstance(ent, dict):
            errs.append(f"{k}: must be a mapping {{to: [...], limited_rows: N, limited_minutes: M}}")
            continue
        to = ent.get("to")
        to = [to] if isinstance(to, str) else to
        if not isinstance(to, list) or not to or not all(isinstance(t, str) and t for t in to):
            errs.append(f"{k}.to: needs a non-empty list of executors")
            continue
        for t in to:
            if t == x:
                errs.append(f"{k}.to: '{t}' is the executor itself")
            elif t not in allowed:
                errs.append(f"{k}.to: '{t}' is not in executors.allowed (or a declared variant)")
            elif t == "manual":
                errs.append(f"{k}.to: 'manual' is not a fallback target (no session would run)")
            elif cfg.flavor(t) != cfg.flavor(x):
                warns.append(f"{k}.to: '{t}' is a {cfg.flavor(t)} tier, '{x}' a {cfg.flavor(x)} tier — a moved row "
                             f"is worked to a different standard")
        try:
            if int(ent.get("limited_rows", DEFAULT_ROWS)) < 1:
                raise ValueError
        except (TypeError, ValueError):
            errs.append(f"{k}.limited_rows: must be an integer >= 1")
        try:
            if float(ent.get("limited_minutes", DEFAULT_MINUTES)) <= 0:
                raise ValueError
        except (TypeError, ValueError):
            errs.append(f"{k}.limited_minutes: must be a number > 0")
        graph[x] = [t for t in to if isinstance(t, str)]
        if not patterns(cfg, x):
            warns.append(f"{k}: no executors.limit_patterns for '{x}' (or its family): its rows can never be seen as "
                         f"rate-limited, so this fallback never fires on its own")
    # cycles
    color: Dict[str, int] = {}

    def visit(n: str, stack: List[str]) -> None:
        color[n] = 1
        for t in graph.get(n, []):
            if color.get(t) == 1:
                errs.append(f"executors.fallback has a cycle: {' -> '.join(stack + [n, t])}")
            elif color.get(t, 0) == 0:
                visit(t, stack + [n])
        color[n] = 2

    for n in list(graph):
        if color.get(n, 0) == 0:
            visit(n, [])
    lp = e.get("limit_patterns") or {}
    for key, pat in (lp.items() if isinstance(lp, dict) else []):
        for p in ([pat] if isinstance(pat, str) else (pat if isinstance(pat, list) else [])):
            try:
                re.compile(str(p))
            except re.error as ex:
                errs.append(f"executors.limit_patterns.{key}: {p!r} is not a valid regex ({ex})")
    try:
        if float(e.get("limit_confirm_secs", 60)) < 0:
            raise ValueError
    except (TypeError, ValueError):
        errs.append("executors.limit_confirm_secs: must be a number >= 0")
    return errs, warns


# -- state ---------------------------------------------------------------------------------------
def sdir(cfg) -> Path:
    return cfg.state / "fallback"


def load_state(cfg, x: str) -> Dict[str, Any]:
    try:
        return json.loads((sdir(cfg) / f"{x}.json").read_text())
    except Exception:
        return {}


def save_state(cfg, x: str, data: Dict[str, Any]) -> None:
    d = sdir(cfg)
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / f".{x}.{os.getpid()}.tmp"
    tmp.write_text(json.dumps(data, indent=1, sort_keys=True))
    os.replace(tmp, d / f"{x}.json")


def in_fallback(cfg, x: str) -> bool:
    return (sdir(cfg) / f"{x}.json").is_file()


def states(cfg) -> Dict[str, Dict[str, Any]]:
    out = {}
    if sdir(cfg).is_dir():
        for f in sorted(sdir(cfg).glob("*.json")):
            out[f.stem] = load_state(cfg, f.stem)
    return out


def resolve(cfg, x: str, seen: Optional[set] = None) -> Optional[str]:
    """The executor a row of X goes to now: the first of X's `to` that is not itself in fallback; a target that
    is in fallback yields to the target of its own entry (the chain). None when nothing is available."""
    seen = set(seen or ()) | {x}
    for t in (entries(cfg).get(x) or {}).get("to", []):
        if t in seen:
            continue
        if not in_fallback(cfg, t):
            return t
        r = resolve(cfg, t, seen)
        if r:
            return r
    return None


def effective(cfg, x: str) -> str:
    """The executor a row resolved to X really dispatches on: X, unless X is in fallback and a target is free."""
    if not x or not sdir(cfg).is_dir() or not in_fallback(cfg, x):
        return x
    return resolve(cfg, x) or x


# -- per-row rate-limit detection ------------------------------------------------------------------
def _rl_file(rig, sid: str) -> Path:
    return rig.cfg.state / f"{sid}.rl"


def _rl_load(rig, sid: str) -> Dict[str, Any]:
    try:
        return json.loads(_rl_file(rig, sid).read_text())
    except Exception:
        return {}


def signature(lines: List[str], pats: List["re.Pattern"]) -> str:
    """The pane tail with what moves on a stuck screen removed: digits inside the matching lines (a retry
    countdown) and spinner glyphs. Any other change in the tail changes the signature."""
    out = []
    for l in lines:
        l = SPINNER.sub("", l)
        if any(p.search(l) for p in pats):
            l = re.sub(r"\d+", "#", l)
        out.append(" ".join(l.split()))
    return "\n".join(out)


def observe(rig, sid: str, d: Dict[str, Any], text: str) -> Dict[str, Any]:
    """One look at a live row's pane tail (text = its last lines). -> the row's record: {} when the tail does not
    match; else {since, sig, epoch, confirmed} — confirmed (an epoch) once the same matching tail has stood for
    limited_confirm_secs."""
    cfg = rig.cfg
    x = d.get("executor", "")
    pats = patterns(cfg, x)
    import tmuxio
    lines = tmuxio.tail_lines(text, TAIL_LINES)       # the last 40 non-blank lines
    f = _rl_file(rig, sid)
    if not pats or not any(p.search("\n".join(lines)) for p in pats):
        if f.exists():
            f.unlink(missing_ok=True)
        return {}
    now, sig = _now(), signature(lines, pats)
    rec = _rl_load(rig, sid)
    if not rec or rec.get("sig") != sig or rec.get("epoch") != d.get("epoch"):
        rec = {"since": now, "sig": sig, "epoch": d.get("epoch"), "confirmed": 0}
        f.write_text(json.dumps(rec))
        rig.st.engine_log(f"fallback: {sid} ({x}) pane matches a limit pattern — waiting {int(confirm_secs(cfg))}s "
                          f"of an unchanged tail before it counts as rate-limited")
        return rec
    if not rec.get("confirmed") and now - float(rec["since"]) >= confirm_secs(cfg):
        rec["confirmed"] = now
        f.write_text(json.dumps(rec))
        rig.st.event(sid, "rate_limited", executor=x)
        rig.log(d.get("lane", ""), f"{sid} is rate-limited on {x} (limit text unchanged for "
                                   f"{int(now - float(rec['since']))}s)")
    return rec


def limited_rows(rig, x: str) -> List[Tuple[str, Dict[str, Any], Dict[str, Any]]]:
    """Confirmed rate-limited live rows of X, as (sid, dispatch record, limit record); parked rows excluded."""
    out = []
    for sid, d in rig.inflight().items():
        if d.get("executor") != x or kind_of(sid) != "row":
            continue
        rec = _rl_load(rig, sid)
        if not rec.get("confirmed") or rec.get("epoch") != d.get("epoch"):
            continue
        if rig.parked(sid) or rig.st.status(sid)[0] != "NONE":
            continue
        out.append((sid, d, rec))
    return out


def matching_rows(rig, x: str) -> List[Tuple[str, Dict[str, Any]]]:
    """Live rows of X whose pane matches a limit pattern but is not (yet) confirmed."""
    out = []
    for sid, d in rig.inflight().items():
        if d.get("executor") == x and kind_of(sid) == "row":
            rec = _rl_load(rig, sid)
            if rec and not rec.get("confirmed") and rec.get("epoch") == d.get("epoch"):
                out.append((sid, rec))
    return out


# -- the plan and the acts ----------------------------------------------------------------------------
def pending_rows(rig, x: str) -> List[Any]:
    """Rows not yet dispatched (phase none/retry, no open session) whose own executor is X, not parked."""
    from rows import load_rows
    live = rig.inflight()
    out = []
    rows, _ = load_rows(rig.cfg)
    for r in rows:
        if r.archived or r.problems or r.id in live:
            continue
        if rig.st.phase(r.id) not in ("", "retry") or rig.st.done(r.id) or rig.parked(r.id):
            continue
        rx, prob = rig.resolve_exec_raw(r)
        if not prob and rx == x:
            out.append(r)
    return out


def plan(rig, x: str) -> Dict[str, Any]:
    cfg = rig.cfg
    ent = entries(cfg).get(x)
    if not ent:
        return {"entry": None}
    tgt = resolve(cfg, x)
    lim = limited_rows(rig, x)
    return {"entry": ent, "target": tgt, "in_fallback": in_fallback(cfg, x),
            "limited": [(s, int(_now() - float(rec["since"]))) for s, _, rec in lim],
            "matching": [s for s, _ in matching_rows(rig, x)],
            "pending": [r.id for r in pending_rows(rig, x)]}


def _prev_override(rig, rid: str) -> str:
    f = rig.ctl(f"EXECUTOR_{rid}")
    try:
        return f.read_text().split()[0] if f.exists() else "default"
    except Exception:
        return "default"


def _kill_window(rig, sid: str, d: Dict[str, Any]) -> None:
    import tmuxio
    w = tmuxio.windows(rig.session).get(sid)
    if w and w["alive"] and w.ident == d.get("ident"):
        tmuxio.kill(rig.session, w["wid"])


def engage(rig, x: str, why: str, by: str = "engine", manual: bool = False) -> Optional[Dict[str, Any]]:
    """Put X in fallback and do the acts. -> a summary dict, or None when nothing could be done (no target)."""
    import orchctl
    import steward
    cfg = rig.cfg
    tgt = resolve(cfg, x)
    if not tgt:
        if rig.st.once(f"fallback-notarget-{x}-{time.strftime('%F %H')}"):
            rig.log("", f"fallback: {x} wants to fall back but none of {entries(cfg).get(x, {}).get('to')} is free "
                        f"(every target is itself in fallback) — the rows wait as before")
        return None
    first = not in_fallback(cfg, x)
    st = load_state(cfg, x) if not first else {"since": _now(), "since_ts": time.strftime("%F %T"), "moved": {},
                                                "by": by, "why": why, "manual": manual}
    inc = None
    if first:
        inc = steward.open_incident(rig, "executor_fallback", f"executor:{x}", f"{x} fallback: {why}",
                                    klass="mechanical", force=True)
        st["incident"] = inc["n"] if inc else None
        save_state(cfg, x, dict(st, targets=[tgt]))     # marked first: a crash mid-way never leaves rows half moved unmarked
    inc_n = st.get("incident") or "-"
    lim = limited_rows(rig, x)
    pend = pending_rows(rig, x)
    killed: List[str] = []
    moved: List[str] = []
    refused: List[str] = []
    for sid, d, rec in lim:
        prev = _prev_override(rig, sid)
        _kill_window(rig, sid, d)
        ok, out = steward._quiet(orchctl.reset, rig, sid, by="fallback")
        if not ok:
            refused.append(f"{sid}: {out}")
            continue
        ok, out = steward._quiet(orchctl.assign, rig, sid, tgt, by="fallback")
        if not ok:
            refused.append(f"{sid}: {out}")
            continue
        _rl_file(rig, sid).unlink(missing_ok=True)
        killed.append(sid)
        st["moved"][sid] = {"to": tgt, "prev": prev, "kind": "limited", "at": int(_now())}
        steward.log_act(rig, inc_n, by, "assign", sid, f"{x} rate-limited ({why}): window killed, row reset, moved "
                        f"{x} -> {tgt}", f"orch assign {sid} {x}")
    for r in pend:
        prev = _prev_override(rig, r.id)
        ok, out = steward._quiet(orchctl.assign, rig, r.id, tgt, by="fallback")
        if not ok:
            refused.append(f"{r.id}: {out}")
            continue
        moved.append(r.id)
        st["moved"][r.id] = {"to": tgt, "prev": prev, "kind": "pending", "at": int(_now())}
        steward.log_act(rig, inc_n, by, "assign", r.id, f"{x} is in fallback ({why}): pending row moved {x} -> {tgt}",
                        f"orch assign {r.id} {x}")
    st["targets"] = [tgt]
    save_state(cfg, x, st)
    summary = (f"{x} -> {tgt}: {len(killed)} rate-limited row(s) killed, reset and moved"
               f"{' (' + ', '.join(killed) + ')' if killed else ''}; {len(moved)} pending row(s) reassigned"
               + (f"; REFUSED {len(refused)}: {'; '.join(refused)[:300]}" if refused else ""))
    rig.st.event(f"executor:{x}", "executor_fallback", to=tgt, killed=killed, pending=len(moved), first=first,
                 by=by, why=why[:200], refused=len(refused) or None)
    rig.log("", f"FALLBACK {summary}")
    if first and inc:
        inc["summary"] = steward.cell(f"{x} rate-limited ({why}); {summary}", 600)
        steward.save_incident(cfg, inc)
        steward.close(rig, inc, "resolved", summary)
        rig.notify("critical", f"{cfg.name}: executor {x} is rate-limited — rows moved to {tgt}",
                   f"{summary}. Revert one row: orch assign <row> {x}. Give the pending rows back: "
                   f"orch executor restore {x}", page_class=PAGE_CLASS)
    elif first:
        rig.notify("critical", f"{cfg.name}: executor {x} is rate-limited — rows moved to {tgt}",
                   f"{summary}. Give the pending rows back: orch executor restore {x}", page_class=PAGE_CLASS)
    return {"target": tgt, "killed": killed, "pending": moved, "refused": refused, "first": first}


def tick(rig) -> None:
    """Engine phase (after monitor): engage an executor whose confirmed rate-limited rows meet its entry's
    thresholds, or that is already in fallback and has a newly confirmed one."""
    cfg = rig.cfg
    ents = entries(cfg)
    if not ents:
        return
    now = _now()
    for x, ent in ents.items():
        lim = limited_rows(rig, x)
        if not lim:
            continue
        if in_fallback(cfg, x):
            why = "already in fallback; a later row is rate-limited too"
        elif len(lim) >= ent["limited_rows"]:
            why = f"{len(lim)} row(s) rate-limited at once (limited_rows {ent['limited_rows']})"
        elif any(now - float(rec["since"]) >= ent["limited_minutes"] * 60 for _, _, rec in lim):
            why = f"a row rate-limited for {int(max(now - float(r['since']) for _, _, r in lim) // 60)} min " \
                  f"(limited_minutes {ent['limited_minutes']:g})"
        else:
            continue
        engage(rig, x, why)


def restore(rig, x: str, why: str, by: str = "operator") -> Tuple[bool, str]:
    """Clear X's fallback and give the pending rows the fallback moved back to X. A row already running keeps its
    executor; a row someone has re-assigned since keeps that. -> (ok, one-line detail)."""
    import orchctl
    import steward
    cfg = rig.cfg
    st = load_state(cfg, x)
    if not st:
        return False, f"{x} is not in fallback"
    live = rig.inflight()
    back: List[str] = []
    kept: List[str] = []
    for rid, m in sorted((st.get("moved") or {}).items()):
        r = _find(rig, rid)
        cur = _prev_override(rig, rid)
        if (r is None or rid in live or rig.st.done(rid) or rig.parked(rid)
                or rig.st.phase(rid) not in ("", "retry") or cur != m.get("to")):
            kept.append(rid)
            continue
        prev = m.get("prev") or "default"
        ok, _ = steward._quiet(orchctl.assign, rig, rid, prev, by=by)
        (back if ok else kept).append(rid)
    try:
        (sdir(cfg) / f"{x}.json").unlink()
    except FileNotFoundError:
        pass
    rig.st.event(f"executor:{x}", "executor_restore", by=by, back=len(back), kept=len(kept), why=why[:200])
    msg = (f"{x} restored: {len(back)} pending row(s) given back"
           f"{' (' + ', '.join(back[:12]) + (' …' if len(back) > 12 else '') + ')' if back else ''}; "
           f"{len(kept)} keep their executor (running, done, parked or re-assigned since)")
    rig.log("", f"RESTORE {msg} — {why} ({by})")
    return True, msg


def _find(rig, rid: str):
    from rows import find_row
    return find_row(rig.cfg, rid)


# -- reports --------------------------------------------------------------------------------------------
def status_lines(rig) -> List[str]:
    cfg = rig.cfg
    ents = entries(cfg)
    names = list(cfg.data["executors"].get("allowed") or [])
    for x in list(ents) + list(states(cfg)):
        if x not in names:
            names.append(x)
    out = [f"{'executor':<18}{'state':<64}notes"]
    vs = cfg.variants()
    for x in names:
        st = load_state(cfg, x)
        if st:
            tgt = resolve(cfg, x) or "(no free target)"
            nm = len(st.get("moved") or {})
            state = f"IN FALLBACK since {st.get('since_ts', '?')} -> {tgt}, {nm} row(s) moved"
        else:
            state = "normal"
        notes = []
        if x in vs:
            notes.append(f"variant of {vs[x]['base']}" + (f" ({vs[x]['model']})" if vs[x].get("model") else ""))
        if x in ents:
            e = ents[x]
            notes.append(f"fallback to {', '.join(e['to'])} at {e['limited_rows']} row(s) or {e['limited_minutes']:g} min")
        lim = limited_rows(rig, x) if x in ents else []
        mt = matching_rows(rig, x) if x in ents else []
        if lim:
            notes.append(f"rate-limited now: {', '.join(s for s, _, _ in lim)}")
        if mt:
            notes.append(f"limit text on pane, unconfirmed: {', '.join(s for s, _ in mt)}")
        if x in ents or st or x in vs:
            out.append(f"{x:<18}{state:<64}{'; '.join(notes)}")
        elif not ents and not vs:
            pass
    if len(out) == 1:
        out.append("(no executor variants or fallback configured: every executor is normal)")
    return out


def dry_run_lines(rig, x: str) -> List[str]:
    cfg = rig.cfg
    p = plan(rig, x)
    if not p.get("entry"):
        return [f"{x}: no executors.fallback entry — nothing would move (configure executors.fallback.{x})"]
    e = p["entry"]
    out = [f"{x}: fallback to {', '.join(e['to'])} (limited_rows {e['limited_rows']}, limited_minutes "
           f"{e['limited_minutes']:g}); state: {'IN FALLBACK' if p['in_fallback'] else 'normal'}"]
    if not p["target"]:
        out.append(f"  no target is free (every one of {e['to']} is itself in fallback with nothing behind it): "
                   f"nothing would move")
        return out
    out.append(f"  target now: {p['target']}")
    out.append("  would kill, reset and move (confirmed rate-limited, live):" + ("" if p["limited"] else " none"))
    for s, secs in p["limited"]:
        out.append(f"    {s}  limited {secs}s  {x} -> {p['target']}   revert: orch assign {s} {x}")
    out.append("  would reassign (pending, not yet dispatched):" + ("" if p["pending"] else " none"))
    for rid in p["pending"]:
        out.append(f"    {rid}  {x} -> {p['target']}   revert: orch assign {rid} {x}")
    if p["matching"]:
        out.append(f"  limit text on the pane but not yet confirmed (left alone): {', '.join(p['matching'])}")
    out.append("  live rows working normally are left alone; parked rows are never touched")
    trig = len(p["limited"]) >= e["limited_rows"]
    out.append(f"  would the engine engage by itself now: "
               f"{'yes' if (p['in_fallback'] and p['limited']) or trig else 'no'} "
               f"({len(p['limited'])} confirmed of {e['limited_rows']} needed"
               f"{'; or one limited for ' + format(e['limited_minutes'], 'g') + ' min' if p['limited'] else ''})")
    out.append("  (dry run: nothing was changed)")
    return out


def digest_lines(cfg, ev: List[Dict[str, Any]]) -> List[str]:
    out: List[str] = []
    fb = [e for e in ev if e.get("event") == "executor_fallback"]
    rs = [e for e in ev if e.get("event") == "executor_restore"]
    now = states(cfg)
    if not (fb or rs or now):
        return out
    out += ["", "## Executor fallback", ""]
    for e in fb[-10:]:
        out.append(f"- {str(e.get('id', '')).replace('executor:', '')} -> {e.get('to')}: "
                   f"{len(e.get('killed') or [])} rate-limited row(s) killed and moved, {e.get('pending')} pending "
                   f"reassigned{' (first engagement)' if e.get('first') else ''} — {str(e.get('why', ''))[:120]}")
    for e in rs[-10:]:
        out.append(f"- {str(e.get('id', '')).replace('executor:', '')} restored by {e.get('by')}: "
                   f"{e.get('back')} pending row(s) given back")
    for x, st in now.items():
        out.append(f"- IN FALLBACK now: {x} since {st.get('since_ts', '?')}, {len(st.get('moved') or {})} row(s) moved "
                   f"— `orch executor restore {x}` when the provider is back")
    return out


# -- CLI ---------------------------------------------------------------------------------------------------
USAGE = ('usage: orch executor status\n'
         '       orch executor fallback <X> --dry-run     what would move where now (changes nothing)\n'
         '       orch executor fallback <X>               engage the fallback by hand (the Owner)\n'
         '       orch executor restore <X> ["why"]        clear X\'s fallback; pending rows go back (the Owner)')


def main(argv: List[str]) -> int:
    from config_parser import load
    from engine import Rig
    import orchctl
    cfg = load()
    rig = Rig(cfg)
    sub = argv[0] if argv else "status"
    args = argv[1:]
    if sub == "status":
        print("\n".join(status_lines(rig)))
        return 0
    if sub == "fallback":
        dry = "--dry-run" in args
        names = [a for a in args if not a.startswith("--")]
        if len(names) != 1:
            print(USAGE)
            return 2
        x = names[0]
        if x not in cfg.data["executors"].get("allowed", []):
            print(f"REFUSED: unknown executor '{x}'")
            return 1
        if dry:
            print("\n".join(dry_run_lines(rig, x)))
            return 0
        orchctl.operator_only("executor fallback")
        if not has_entry(cfg, x):
            print(f"REFUSED: no executors.fallback entry for {x}")
            return 1
        res = engage(rig, x, "engaged by the operator", by="operator", manual=True)
        if res is None:
            print(f"nothing done: no free target for {x}")
            return 1
        print(f"{x} is in fallback -> {res['target']}: {len(res['killed'])} limited row(s) moved, "
              f"{len(res['pending'])} pending row(s) reassigned"
              + (f", REFUSED: {'; '.join(res['refused'])}" if res["refused"] else ""))
        return 0
    if sub == "restore":
        orchctl.operator_only("executor restore")
        if not args:
            print(USAGE)
            return 2
        ok, msg = restore(rig, args[0], " ".join(args[1:]).strip() or "restored by the operator", by="operator")
        print(msg)
        return 0 if ok else 1
    print(USAGE)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
