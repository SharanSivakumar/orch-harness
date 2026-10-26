#!/usr/bin/env python3
"""The native course loop (v3, drop D item 2): a judge's negative outcome corrects the round by itself.

v2's course loop (docs/COURSE_LOOP.md) waited for a human at each step: start the review, answer it,
open a planner, start the round, re-run the judge. Here the engine walks the chain, inside an OPEN phase
and within its caps; opening or starting a new phase stays the Owner's.

    rounds/<tag>/round.yaml
        judge: <row>              the round's judge (a row of one of the round's lanes)
        on_fail: course_loop      off by default: no key, no loop (a v2 round)
        max_loops: 1              loops this round may run (default 1); the phase's caps.loops caps them too

The chain, one loop (state/courseloop/<tag>.json, event id `round:<tag>`):

  trigger   the judge is accepted with a negative outcome (nogo / held / fail / an unknown token — drop B).
            The scan opens no separate negative_outcome incident for it (an open one is closed: handed over).
            Held — and started later — while the round's phase is not open. At the round's max_loops or the
            phase's loops cap the PHASE RESTS (nothing new starts; only the Owner re-opens it) and one inbox
            item says why; no loop starts. Otherwise the loop count goes up (phases.yaml `loops`, the record).
  review    the engine dispatches `rr-<tag>` (the previous review/ is kept as review.loop<n-1>/).
  answer    the review is DONE: a `round_review` incident (judgment) — the Steward (or the panel for class P)
            writes ANSWERS.md within the rights, INBOXes the W questions, and runs `orch sw plan <next> "brief"`.
  plan      the planner `pl-<next>-<k>` creates round <next> in the same phase (`round-new --phase`), cuts the
            fix rows with `orch row add`, starts the round, and closes PASS.
  run       the rows that loop's planners added (`row_add` events) run.
  re-arm    when each of them is accepted or parked, the engine resets the judge (`course-loop:<tag>#<n>`);
            it re-runs; GO ends the loop (done), another negative outcome starts the next one.

A step that cannot go on (the Steward closed the incident without a planner, the planner failed and nothing
re-planned, the planner cut no rows or did not start its round, the round review failed twice) STOPS the loop:
an event, a normal notification and one inbox item. Nothing pages.

Only rounds the operator created count (round.yaml without `by`): a round a Steward or planner session made
cannot give itself a loop budget — since v3.1 it loops only inside a phase whose Owner set `session_loops: true`,
within caps.loops, and never on another round's judge or lane. The loop needs the Steward lane (steward.enabled) —
without it the round's config is reported once and nothing happens.

v3.1 — adaptive plans (docs/V3_DESIGN.md §15):

    judges: [<row>, {row: <row>, max_loops: <n>}]   checkpoint judges: each its own loop (a replan), keyed
                                                     `<tag>+<row>` (record, rr- session, review+<row>/ folder,
                                                     incident subject); the scalar `judge:` keeps the key `<tag>`
    bars: [<workspace path>, …]                      pinned with the judge's PLAN and the goal: block (bar pin)

  * `--outcome infeasible` ("the remaining plan cannot reach the goal") starts the loop as the replan: the held wave
    is recorded, the review proposes a new route to the SAME goal, the Steward parks what the new route makes moot,
    the planner cuts the new rows, the judge re-measures the same pinned bar. Any row accepted infeasible is never
    re-run by a session (owns_negative).
  * A checkpoint's loop counts in the phase's `replans` when the phase caps replans (else in `loops`).
  * A round a session created (`by:`) loops only in a phase whose Owner set `session_loops: true`, within caps.loops.
  * The bar pin (steward.bar_pin, default on): a loop whose judge PLAN, goal or listed bars changed since it started
    stops before the re-arm — a replan never moves a bar.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))

ON_FAIL = "course_loop"
DEFAULT_MAX_LOOPS = 1
ACTIVE = ("review", "answer", "plan", "run")
FINAL, CHECKPOINT = "final", "checkpoint"
KEY_SEP = "+"                 # never in a row id or round tag (rearm.ID_RE / TAG_RE): a key cannot be a round


def key_of(tag: str, judge: str, kind: str) -> str:
    """The loop's key: the round's tag for its `judge:` (every v3 name unchanged), `<tag>+<judge>` for a checkpoint."""
    return tag if kind == FINAL else f"{tag}{KEY_SEP}{judge}"


def split_key(key: str) -> Tuple[str, str]:
    """-> (round tag, checkpoint judge or '')."""
    if KEY_SEP in key:
        t, j = key.split(KEY_SEP, 1)
        return t, j
    return key, ""


def _who(conf: Dict[str, Any]) -> str:
    return (f"round {conf.get('tag', '?')}" if conf.get("kind", FINAL) == FINAL
            else f"checkpoint {conf['judge']} of round {conf.get('tag', '?')}")


def _full(key: str, conf: Dict[str, Any]) -> Dict[str, Any]:
    """A loop config with every v3.1 key: a v3-shaped {judge, max_loops, lanes} (a caller from before v3.1) reads
    as the round's own judge of the key's round."""
    if "tag" in conf and "kind" in conf and "key" in conf:
        return conf
    tag, cp = split_key(key)
    return dict({"key": key, "tag": tag, "kind": CHECKPOINT if cp else FINAL, "by": ""}, **conf)


# -- configuration --------------------------------------------------------------------------------
def _session_round_problem(cfg, tag: str) -> str:
    """'' when round `tag` (made by a session, `by:`) may loop: its phase is the Owner's, says `session_loops: true`
    and has a loops cap (v3.1, §15.5). A session never grants itself a loop budget."""
    import phases
    if not phases.active(cfg):
        return "this rig has no phases, and only a phase the Owner opened with `--session-loops on` lets it loop"
    f = phases.Fence(cfg)
    pid = f.round_phase.get(tag, "")
    if not pid:
        return "it belongs to no phase"
    ph = f.phases.get(pid) or {}
    if not ph.get("session_loops"):
        return f"phase {pid} does not allow session loops (the Owner's `orch phase open {pid} --session-loops on`)"
    if "loops" not in (ph.get("caps") or {}):
        return f"phase {pid} has no loops cap, and a session's round loops only within caps.loops"
    return ""


def _base_problem(cfg, tag: str, r: Dict[str, Any]) -> Optional[str]:
    """None when the round declares no loop (or is closed); '' when its declaration may loop; else why not."""
    if str(r.get("on_fail") or "").strip() != ON_FAIL:
        if r.get("on_fail"):
            return f"round {tag}: on_fail '{r.get('on_fail')}' is not '{ON_FAIL}' — no course loop"
        return None
    if r.get("status") == "closed":
        return None
    if KEY_SEP in tag:
        return f"round {tag}: a tag with '{KEY_SEP}' cannot loop ('{KEY_SEP}' separates a checkpoint's loop key)"
    if r.get("by"):
        why = _session_round_problem(cfg, tag)
        if why:
            return (f"round {tag} was created by session {r.get('by')}: {why} — only a round the operator created, or "
                    f"one in such a phase, may declare on_fail: {ON_FAIL} (a session gives itself no loop budget)")
    import steward
    if not steward.enabled(cfg):
        return f"round {tag} declares on_fail: {ON_FAIL}, but the Steward lane is off (steward.enabled)"
    return ""


def _max_loops(v: Any) -> Optional[int]:
    try:
        mx = int(v)
        return None if mx < 0 else mx
    except (TypeError, ValueError):
        return None


def _lanes(r: Dict[str, Any]) -> List[str]:
    return [str(x) for x in r.get("lanes")] if isinstance(r.get("lanes"), list) else []


def _judge_problem(cfg, tag: str, judge: str, lanes: List[str], what: str = "judge", by: str = "") -> str:
    from rows import find_row
    jr = find_row(cfg, judge)
    if jr is None:
        if what == "checkpoint":
            return (f"round {tag}: checkpoint {judge} is not a row yet — its loop switches on once a row with this id "
                    f"is added (declare every planned checkpoint id up front)")
        return f"round {tag}: {what} {judge} is not a row"
    if jr.lane not in lanes:
        return f"round {tag}: {what} {judge} is in lane {jr.lane}, not one of the round's lanes ({' '.join(lanes)})"
    if by:                       # a session's round loops only on its own lanes (v3.1, §15.5)
        import rounds
        other = [t for t, r in rounds.load_rounds(cfg).items() if t != tag and r.get("status") != "closed"
                 and jr.lane in [str(x) for x in (r.get("lanes") or [])]]
        if other:
            return (f"round {tag} (made by session {by}): {what} {judge} is in lane {jr.lane}, which round "
                    f"{other[0]} also lists — a session's round loops only on lanes of its own")
    return ""


def config(cfg, tag: str, r: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], str]:
    """The round's own judge (`judge:`, its FINAL judge): -> ({key, tag, judge, kind, max_loops, lanes, by}, '') for a
    valid course loop; (None, why) otherwise (why '' when the round declares none)."""
    base = _base_problem(cfg, tag, r)
    if base is None:
        return None, ""
    if base:
        return None, base
    judge = str(r.get("judge") or "").strip()
    if not judge:
        if r.get("judges"):
            return None, ""          # checkpoints only: no final judge is a valid round
        return None, f"round {tag} declares on_fail: {ON_FAIL} without a judge: <row>"
    lanes = _lanes(r)
    why = _judge_problem(cfg, tag, judge, lanes, by=str(r.get("by") or ""))
    if why:
        return None, why
    mx = _max_loops(r.get("max_loops", DEFAULT_MAX_LOOPS))
    if mx is None:
        return None, f"round {tag}: max_loops must be a whole number ≥ 0"
    return {"key": tag, "tag": tag, "judge": judge, "kind": FINAL, "max_loops": mx, "lanes": lanes,
            "by": str(r.get("by") or "")}, ""


def checkpoint_configs(cfg, tag: str, r: Dict[str, Any]) -> List[Tuple[Optional[Dict[str, Any]], str, str]]:
    """The round's checkpoint judges (`judges:`, v3.1): -> [(config | None, why, key)], one per entry ([] when the
    round declares none). A refused entry does nothing; the others loop."""
    raw = r.get("judges")
    if raw in (None, "", []):
        return []
    base = _base_problem(cfg, tag, r)
    if base is None:
        return []
    k0 = f"{tag}{KEY_SEP}judges"
    if base:
        return []                # config() reports the round's problem once, under the round's own key
    if not isinstance(raw, list):
        return [(None, f"round {tag}: judges: must be a list of row ids or {{row: <id>, max_loops: <n>}}", k0)]
    final = str(r.get("judge") or "").strip()
    lanes = _lanes(r)
    out: List[Tuple[Optional[Dict[str, Any]], str, str]] = []
    seen = set()
    for i, ent in enumerate(raw):
        if isinstance(ent, str) and ent.strip():
            j, mxv = ent.strip(), r.get("max_loops", DEFAULT_MAX_LOOPS)
        elif isinstance(ent, dict) and str(ent.get("row") or "").strip():
            j, mxv = str(ent["row"]).strip(), ent.get("max_loops", r.get("max_loops", DEFAULT_MAX_LOOPS))
        else:
            out.append((None, f"round {tag}: judges entry {ent!r} is not a row id or {{row: <id>, max_loops: <n>}}",
                        f"{k0}#{i}"))
            continue
        key = key_of(tag, j, CHECKPOINT)
        if KEY_SEP in j:
            out.append((None, f"round {tag}: checkpoint {j}: a row id with '{KEY_SEP}'", key))
            continue
        if j in seen:
            out.append((None, f"round {tag}: checkpoint {j} is listed twice in judges:", key))
            continue
        seen.add(j)
        if j == final:
            out.append((None, f"round {tag}: {j} is both the round's judge: and a checkpoint — the judge: wins", key))
            continue
        why = _judge_problem(cfg, tag, j, lanes, "checkpoint", by=str(r.get("by") or ""))
        if why:
            out.append((None, why, key))
            continue
        mx = _max_loops(mxv)
        if mx is None:
            out.append((None, f"round {tag}: checkpoint {j}: max_loops must be a whole number ≥ 0", key))
            continue
        out.append(({"key": key, "tag": tag, "judge": j, "kind": CHECKPOINT, "max_loops": mx, "lanes": lanes,
                     "by": str(r.get("by") or "")}, "", key))
    return out


def round_loops(cfg, tag: str, r: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], Dict[str, str]]:
    """Every loop one round declares: -> ([config], key -> why for each refused declaration)."""
    good: List[Dict[str, Any]] = []
    bad: Dict[str, str] = {}
    c, why = config(cfg, tag, r)
    if c:
        good.append(c)
    elif why:
        bad[tag] = why
    for c, why, key in checkpoint_configs(cfg, tag, r):
        if c:
            good.append(c)
        elif why:
            bad[key] = why
    return good, bad


def configs(cfg) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, str]]:
    """-> (key -> config for every loop that is on, key -> why for every declaration that is refused). The key is the
    round's tag for its `judge:` and `<tag>+<judge>` for a checkpoint. A judge two rounds name loops in neither (one
    claim would start two loops and count twice)."""
    import rounds
    out: Dict[str, Dict[str, Any]] = {}
    bad: Dict[str, str] = {}
    for tag, r in rounds.load_rounds(cfg).items():
        good, refused = round_loops(cfg, tag, r)
        for c in good:
            out[c["key"]] = c
        bad.update(refused)
        if (r.get("judges") not in (None, "", []) and str(r.get("on_fail") or "").strip() != ON_FAIL
                and r.get("status") != "closed"):
            bad[f"{tag}{KEY_SEP}judges"] = (f"round {tag} declares judges: without on_fail: {ON_FAIL} — its checkpoints "
                                            f"do not loop (their negative outcomes go to the Steward's incident)")
    by_judge: Dict[str, List[str]] = {}
    for key, c in out.items():
        by_judge.setdefault(c["judge"], []).append(key)
    for j, keys in by_judge.items():
        if len(keys) < 2:
            continue
        owners = [k for k in keys if not out[k].get("by")]
        if len(owners) == 1:          # the Owner's declaration wins: a session's round never takes it over (v3.1)
            own = out[owners[0]]
            for k in keys:
                if k != owners[0]:
                    out.pop(k, None)
                    bad[k] = (f"round {split_key(k)[0]}: judge {j} is round {own['tag']}'s (the Owner's round) — a "
                              f"session's round never takes another round's judge")
            continue
        tags = sorted({out[k]["tag"] for k in keys})
        for k in keys:
            out.pop(k, None)
            bad[k] = f"round {split_key(k)[0]}: judge {j} is the judge of rounds {', '.join(tags)} — one judge, one loop"
    return out, bad


def loops_of(cfg) -> Dict[str, Dict[str, Any]]:
    """key -> config, for every valid course loop."""
    return configs(cfg)[0]


def _declaring_key(cfg, rid: str) -> str:
    """The loop key of a round that declares `rid` as a judge (scalar or checkpoint) — even one refused as a
    duplicate: one judge, one loop switches the loop off, never the guard."""
    import rounds
    for t, r in rounds.load_rounds(cfg).items():
        if (config(cfg, t, r)[0] or {}).get("judge") == rid:
            return t
        for c, _, key in checkpoint_configs(cfg, t, r):
            if c and c["judge"] == rid:
                return key
    return ""


def owns_negative(cfg, rid: str, st=None) -> str:
    """'' unless no session may reset or re-arm `rid` — then why:
      * `rid` is accepted with outcome infeasible (v3.1, any row): the remaining plan cannot reach the goal, so the
        route is replanned; the measured result is never re-run to obtain a GO;
      * `rid` is a course-loop judge (a round's judge: or a checkpoint) accepted with a negative outcome: only the
        loop's own re-arm (after the correction's rows are done) runs it again."""
    from store import INFEASIBLE, Store, negative
    st = st or Store(cfg.state)
    if st.phase(rid) != "accepted":
        return ""
    oc = st.outcome(rid)
    if not negative(oc):
        return ""
    if oc == INFEASIBLE:
        return (f"{rid} was accepted with outcome infeasible — the remaining plan cannot reach the goal; a measured "
                f"result is never re-run to obtain a GO: replan the route toward the same goal (the course loop or "
                f"the Steward), never the bar")
    try:
        key = _declaring_key(cfg, rid)
    except Exception:
        return ""
    if not key:
        return ""
    tag, cp = split_key(key)
    who = f"round {tag}'s course-loop judge" if not cp else f"round {tag}'s checkpoint (course-loop judge)"
    return (f"{rid} is {who}, accepted with outcome {oc} — a measured negative result is never re-run to obtain a "
            f"GO; the engine re-arms it when the loop's correction rows are done")


def judges(cfg) -> Dict[str, str]:
    """judge row -> loop key, for every loop that is on (the Steward's scan hands these over)."""
    return {c["judge"]: key for key, c in loops_of(cfg).items()}


def final_judges(cfg) -> Dict[str, str]:
    """judge row -> round tag, for every round's own judge (`judge:`) declared with a valid loop — duplicates and a
    session round's claim included (one judge, one loop switches a loop off, never this guard): no session's
    `orch row add` may grant `rearm=` of one (vD2). A checkpoint may be granted while it holds no negative outcome
    (v3.1, §15.2)."""
    import rounds
    out: Dict[str, str] = {}
    for t, r in rounds.load_rounds(cfg).items():
        c = config(cfg, t, r)[0]
        if c:
            out.setdefault(c["judge"], t)
    return out


def declared_judges(cfg) -> List[str]:
    """Every row a round declares as a judge or checkpoint with a valid declaration (duplicates included)."""
    import rounds
    out: List[str] = []
    for t, r in rounds.load_rounds(cfg).items():
        c = config(cfg, t, r)[0]
        if c and c["judge"] not in out:
            out.append(c["judge"])
        for c, _, _ in checkpoint_configs(cfg, t, r):
            if c and c["judge"] not in out:
                out.append(c["judge"])
    return out


# -- the held wave and the bar pin (v3.1) ------------------------------------------------------------
def held_wave(cfg, judge: str, st=None) -> List[str]:
    """The rows a negative outcome of `judge` holds: its `needs=go` dependents not yet accepted, and every row whose
    deps reach one of them (not accepted), in queue order."""
    from rows import implicit_deps, load_rows
    from store import Store
    st = st or Store(cfg.state)
    rows, _ = load_rows(cfg, include_archived=False)
    imp = implicit_deps(cfg, rows)            # serial lanes: the rows below a held row wait too
    held = {x.id for x in rows if judge in x.deps and str(x.meta.get("needs", "")).lower() == "go"
            and st.phase(x.id) != "accepted"}
    grew = True
    while grew:
        grew = False
        for x in rows:
            if (x.id not in held and x.id != judge and st.phase(x.id) != "accepted"
                    and any(d in held for d in list(x.deps) + imp.get(x.id, []))):
                held.add(x.id)
                grew = True
    try:
        judges_ = set(declared_judges(cfg))   # a judge carries a bar: it is never offered for parking
    except Exception:
        judges_ = set()
    return [x.id for x in rows if x.id in held and x.id not in judges_]


def bar_pin_on(cfg) -> bool:
    v = (cfg.data.get("steward") or {}).get("bar_pin", True)
    return str(v).strip().lower() not in ("false", "off", "no", "0") if isinstance(v, str) else bool(v)


def _sha_path(p: Path) -> str:
    """sha256 of a file; of a folder's files (caches, dot files and *.pyc left out); 'absent'; or 'unreadable:<why>'
    — never an exception: a pin that cannot be read reads as moved, which stops the loop (fail safe)."""
    import hashlib
    try:
        if p.is_file():
            return hashlib.sha256(p.read_bytes()).hexdigest()
        if p.is_dir():
            h = hashlib.sha256()
            for f in sorted(x for x in p.rglob("*") if x.is_file()):
                rel = f.relative_to(p)
                if any(part.startswith(".") or part == "__pycache__" for part in rel.parts) or f.suffix == ".pyc":
                    continue
                h.update(str(rel).encode() + b"\0" + f.read_bytes() + b"\0")
            return "dir:" + h.hexdigest()
        return "absent"
    except OSError as e:
        return f"unreadable:{type(e).__name__}"


def _goal_sha(cfg, tag: str) -> str:
    import hashlib
    import rounds
    g = (rounds.load_rounds(cfg).get(tag) or {}).get("goal")
    return hashlib.sha256(json.dumps(g, sort_keys=True, default=str).encode()).hexdigest()


def pin(cfg, conf: Dict[str, Any]) -> Dict[str, str]:
    """What stays fixed while a loop runs: label -> sha256 of the judge's PLAN (`plan:<path>`), the round.yaml goal:
    block (`goal`) and every round.yaml `bars:` path (`bar:<path>`, workspace-relative; a folder hashes its files)."""
    import rounds
    from rows import find_row
    out: Dict[str, str] = {}
    jr = find_row(cfg, conf["judge"])
    if jr is not None:
        out[f"plan:{jr.plan}"] = _sha_path(cfg.workspace / jr.plan)
    out["goal"] = _goal_sha(cfg, conf.get("tag", ""))
    bars = (rounds.load_rounds(cfg).get(conf.get("tag", "")) or {}).get("bars") or []
    for b in (bars if isinstance(bars, list) else [bars]):
        b = str(b).strip()
        if b:
            out[f"bar:{b}"] = _sha_path(cfg.workspace / b)
    return out


def bar_moved(cfg, conf: Dict[str, Any], pinned: Dict[str, str]) -> List[str]:
    """The pinned labels whose content changed since the loop started (a removed `bars:` entry is still checked:
    the pin keeps its own list), and the judge's PLAN path itself when its queue line now points elsewhere."""
    from rows import find_row
    out = []
    jr = find_row(cfg, conf["judge"])
    for label, sha in pinned.items():
        kind, _, rel = label.partition(":")
        if kind == "plan":
            now = _sha_path(cfg.workspace / rel)
            if jr is None or jr.plan != rel:
                out.append(f"the judge's PLAN path ({rel} -> {jr.plan if jr else 'no row'})")
                continue
        elif kind == "goal":
            now = _goal_sha(cfg, conf.get("tag", ""))
        else:
            now = _sha_path(cfg.workspace / rel)
        if now != sha:
            out.append({"plan": f"the judge's PLAN {rel}", "goal": "round.yaml goal:"}.get(kind, f"bar {rel}"))
    return out


# -- the record -------------------------------------------------------------------------------------
def rdir(cfg) -> Path:
    d = cfg.state / "courseloop"
    d.mkdir(parents=True, exist_ok=True)
    return d


def load(cfg, tag: str) -> Dict[str, Any]:
    try:
        return json.loads((rdir(cfg) / f"{tag}.json").read_text())
    except Exception:
        return {"tag": tag, "loops": 0, "state": "", "history": []}


def save(cfg, rec: Dict[str, Any]) -> None:
    rec["updated"] = time.strftime("%F %T")
    p = rdir(cfg) / f"{rec['tag']}.json"
    tmp = p.with_name(f".{p.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(rec, indent=1, sort_keys=True))
    os.replace(tmp, p)


def _phase(cfg, tag: str) -> Tuple[bool, str, Dict[str, Any]]:
    """-> (phases on, the round's phase id, that phase)."""
    import phases
    if not phases.active(cfg):
        return False, "", {}
    f = phases.Fence(cfg)
    pid = f.round_phase.get(tag, "")
    return True, pid, f.phases.get(pid, {})


def _event(rig, tag: str, what: str, **kw) -> None:
    rig.st.event(f"round:{tag}", f"course_loop_{what}", **{k: v for k, v in kw.items() if v not in (None, "")})


def _log(rig, rec: Dict[str, Any], msg: str) -> None:
    rec.setdefault("history", []).append(f"{time.strftime('%F %T')} {msg}"[:400])
    rec["history"] = rec["history"][-40:]
    rig.st.engine_log(f"course loop {rec['tag']}: {msg}")


# -- the tick ------------------------------------------------------------------------------------------
def tick(rig) -> None:
    """Engine tick phase (before the Steward's): walk every course loop one step — each round's judge and each
    checkpoint has its own (keyed) loop."""
    import hashlib
    cfg = rig.cfg
    good, bad = configs(cfg)
    for key, why in bad.items():
        if rig.st.once(f"courseloop-cfg-{key}-{hashlib.sha1(why.encode()).hexdigest()[:10]}"):
            rig.st.engine_log(f"course loop: {why}")
            _event(rig, key, "config", detail=why[:300])
    for key, conf in good.items():
        rec = load(cfg, key)
        before = json.dumps(rec, sort_keys=True)
        rec["judge"] = conf["judge"]
        if conf["kind"] == CHECKPOINT:
            rec.update(round=conf["tag"], kind=CHECKPOINT)
        state = rec.get("state", "")
        try:
            if state in ACTIVE and _superseded(rig, key, conf, rec):
                pass
            elif state in ACTIVE and _bar_check(rig, key, conf, rec):
                pass
            elif state == "judging" and _judging_bar_check(rig, key, conf, rec):
                pass
            elif state == "review":
                _review(rig, key, conf, rec)
            elif state == "answer":
                _answer(rig, key, conf, rec)
            elif state == "plan":
                _plan(rig, key, conf, rec)
            elif state == "run":
                _run(rig, key, conf, rec)
            else:
                _trigger(rig, key, conf, rec)
        except Exception as e:                    # one loop's fault never starves the others (reported once)
            import traceback
            if rig.st.once(f"courseloop-error-{key}-{type(e).__name__}-{str(e)[:40]}"):
                rig.st.engine_log(f"course loop {key}: step failed — {type(e).__name__}: {e}\n{traceback.format_exc()}")
                _event(rig, key, "error", detail=f"{type(e).__name__}: {e}"[:300])
        finally:
            if json.dumps(rec, sort_keys=True) != before:
                save(cfg, rec)


def _sig(st, judge: str) -> str:
    """One accepted claim of the judge: its outcome, attempt and claim time."""
    res = st.result(judge)
    return f"{st.outcome(judge)}|{st.attempt(judge)}|{int(float(res.get('status_mtime', 0) or 0))}"


def counter_for(conf: Dict[str, Any], ph: Dict[str, Any]) -> str:
    """The phase counter a loop of `conf` uses (§15.4): a checkpoint's replan counts in `replans` when the phase caps
    replans; everything else — the round's judge, any loop of a session's round (§15.5) — in `loops`."""
    if conf.get("kind", FINAL) == CHECKPOINT and not conf.get("by") and "replans" in (ph.get("caps") or {}):
        return "replans"
    return "loops"


def _trigger(rig, key: str, conf: Dict[str, Any], rec: Dict[str, Any]) -> None:
    conf = _full(key, conf)
    cfg, st = rig.cfg, rig.st
    from store import negative
    judge = conf["judge"]
    if st.phase(judge) != "accepted":
        return
    oc = st.outcome(judge)
    if not negative(oc):
        if rec.get("state") == "judging":
            rec["state"] = "done"
            _log(rig, rec, f"{judge} accepted with outcome {oc or '(none)'} after loop {rec.get('loops')} — done")
            _event(rig, key, "done", judge=judge, outcome=oc or "none", loops=rec.get("loops"))
            rig.notify("low", f"{cfg.name}: course loop {key} done",
                       f"{judge} passed after {rec.get('loops')} course loop(s)")
        return
    sig = _sig(st, judge)
    if sig == rec.get("handled_sig"):
        return
    if _bar_held(rig, key, conf, rec, sig):
        return
    _hand_over_incident(rig, key, conf, judge)
    on, pid, ph = _phase(cfg, conf["tag"])
    n = int(rec.get("loops", 0) or 0)
    counter = counter_for(conf, ph) if on else "loops"
    cap = (ph.get("caps") or {}).get(counter) if on else None
    if n >= conf["max_loops"]:
        return _exhausted(rig, key, conf, rec, sig, oc, pid,
                          f"{_who(conf)} has run its max_loops ({n}/{conf['max_loops']})", counter)
    if cap is not None and int(ph.get(counter, 0) or 0) >= int(cap):
        return _exhausted(rig, key, conf, rec, sig, oc, pid,
                          f"phase {pid} has run its {counter} cap ({int(ph.get(counter, 0) or 0)}/{int(cap)})", counter)
    if on and not pid:
        return _hold(rig, key, rec, sig, f"round {conf['tag']} belongs to no phase")
    if on and ph.get("state") != "open":
        return _hold(rig, key, rec, sig, f"phase {pid} is {ph.get('state')}")
    _start(rig, key, conf, rec, sig, oc, pid, counter)


def _superseded(rig, key: str, conf: Dict[str, Any], rec: Dict[str, Any]) -> bool:
    """True (the loop ended) when the judge is no longer the claim the loop started from — reset or re-judged
    outside the loop. A GO ends it as done; anything else stops it, and the next negative claim starts a new one."""
    conf = _full(key, conf)
    from store import negative
    cur = rec.get("current") or {}
    judge = conf["judge"]
    ph = rig.st.phase(judge)
    if ph == "accepted" and _sig(rig.st, judge) == cur.get("sig"):
        return False
    oc = rig.st.outcome(judge) if ph == "accepted" else ""
    if ph == "accepted" and not negative(oc):
        rec["state"] = "done"
        _log(rig, rec, f"loop {cur.get('n')}: {judge} was accepted with outcome {oc or '(none)'} outside the loop — done")
        _event(rig, key, "done", judge=judge, outcome=oc or "none", loops=rec.get("loops"), why="judged outside the loop")
        return True
    if ph in ("", "retry", "claimed") or rig.st.dispatch(judge).get("open"):
        why = f"{judge} was reset under the loop (phase {ph or 'pending'}) — this loop ends; its next negative claim starts one"
    else:
        why = f"{judge} is no longer the claim the loop started from (phase {ph}, outcome {oc or '-'})"
    _stop(rig, key, rec, why, impact=f"{judge} runs or waits outside the loop; the loop starts again on its next "
                                     f"negative claim (within the limits).")
    return True


def _bar_check(rig, key: str, conf: Dict[str, Any], rec: Dict[str, Any]) -> bool:
    """The bar pin (v3.1, §15.6): True (the loop stopped) when the judge's PLAN, the round's goal or a listed bar
    changed since this loop started. A replan changes the route, never the bar."""
    conf = _full(key, conf)
    cur = rec.get("current") or {}
    pinned = cur.get("pin")
    if not pinned or not bar_pin_on(rig.cfg):
        return False
    moved = bar_moved(rig.cfg, conf, pinned)
    if not moved:
        return False
    rig.st.event(f"round:{key}", "course_loop_bar_moved", n=cur.get("n"), moved="; ".join(moved)[:300])
    _stop(rig, key, rec, f"the bar moved under the loop: {'; '.join(moved)} changed since loop {cur.get('n')} started "
                         f"— a replan changes the route, never the bar, so {conf['judge']} is not re-armed",
          impact=f"{conf['judge']} keeps its {cur.get('outcome')} outcome and the rows that need its GO stay held. If "
                 f"the change is the Owner's, record the ruling and `orch reset {conf['judge']}` (the judge then "
                 f"measures the new bar); otherwise restore the file(s) from git and reset the judge.")
    return True


def _owner_adopted(st, judge: str, since: float) -> bool:
    """The Owner reset (or unparked) the judge after `since`: the Owner's way to adopt a changed bar."""
    for e in st.events(judge):
        if e.get("event") == "reset" and float(e.get("epoch", 0) or 0) > since and (
                not e.get("by") or str(e.get("by")).startswith("unpark by owner")):
            return True
    return False


def _bar_held(rig, key: str, conf: Dict[str, Any], rec: Dict[str, Any], sig: str) -> bool:
    """True while a bar this loop chain pinned stays moved and the Owner has not reset the judge since: no loop
    starts on a moved bar. A claim right after the loop's own re-arm is checked against the chain's first pin."""
    if not bar_pin_on(rig.cfg):
        return False
    hold = rec.get("bar_hold") or {}
    if not hold and rec.get("state") == "judging":
        cur = rec.get("current") or {}
        pinned = cur.get("pin0") or cur.get("pin")
        if pinned:
            hold = {"pin": pinned, "epoch": float(cur.get("started_epoch", 0) or 0), "n": cur.get("n")}
    if not hold:
        return False
    if _owner_adopted(rig.st, conf["judge"], float(hold.get("epoch", 0))):
        rec.pop("bar_hold", None)
        _log(rig, rec, f"{conf['judge']} was reset by the Owner after the bar moved — the next loop pins the bar anew")
        return False
    moved = bar_moved(rig.cfg, conf, hold["pin"])
    if not moved:
        rec.pop("bar_hold", None)
        return False
    if rec.get("bar_hold_sig") != sig:
        rec.update(bar_hold=hold, bar_hold_sig=sig, handled_sig=sig)
        if rec.get("state") != "stopped":
            _stop(rig, key, rec, f"the bar moved since loop {hold.get('n')} started: {'; '.join(moved)} — no loop starts on "
                                 f"a moved bar", impact=f"{conf['judge']}'s outcome stands and the rows that need its GO "
                                 f"stay held; restore the bar from git, or record the ruling and `orch reset "
                                 f"{conf['judge']}` to adopt the new one")
        rig.st.event(f"round:{key}", "course_loop_bar_moved", moved="; ".join(moved)[:300], held="no new loop")
    return True


def _judging_bar_check(rig, key: str, conf: Dict[str, Any], rec: Dict[str, Any]) -> bool:
    """The re-armed judge measures the bar the loop pinned: when it moved after the re-arm, the loop stops; a judge
    not yet dispatched is parked (it never measures a moved bar); one already running is flagged (its outcome is on
    a moved bar — the Owner's inbox says so)."""
    conf = _full(key, conf)
    cur = rec.get("current") or {}
    pinned = cur.get("pin0") or cur.get("pin")
    if not pinned or not bar_pin_on(rig.cfg):
        return False
    moved = bar_moved(rig.cfg, conf, pinned)
    if not moved:
        return False
    import steward
    judge = conf["judge"]
    ph = rig.st.phase(judge)
    parked = False
    if ph in ("", "retry") and not rig.st.dispatch(judge).get("open"):
        parked = steward.park(rig, judge, f"course loop {key}: the bar moved after the re-arm ({'; '.join(moved)})"[:300],
                              by=f"course-loop:{key}", carry="bar-moved")
    rig.st.event(f"round:{key}", "course_loop_bar_moved", n=cur.get("n"), moved="; ".join(moved)[:300],
                 judge_parked=parked or None)
    rec["bar_hold"] = {"pin": pinned, "epoch": float(cur.get("started_epoch", 0) or 0), "n": cur.get("n")}
    what = (f"{judge} is parked before it ran, so it never measures a moved bar" if parked else
            f"{judge} was already {ph or 'running'}: its outcome is measured on a moved bar — do not trust a GO from it")
    _stop(rig, key, rec, f"the bar moved after the re-arm: {'; '.join(moved)} changed since loop {cur.get('n')} "
                         f"started — {what}",
          impact=f"restore the bar from git and `orch unpark`/`orch reset {judge}`, or record the ruling and reset it to "
                 f"adopt the new bar. No loop starts while the bar stays moved.")
    return True


def _phase_open(cfg, tag: str) -> str:
    """'' when the round's phase is open (or the rig has no phases); else why the loop step waits."""
    on, pid, ph = _phase(cfg, tag)
    if not on:
        return ""
    if not pid:
        return f"round {tag} belongs to no phase"
    if ph.get("state") != "open":
        return f"phase {pid} is {ph.get('state')}"
    return ""


def _wait_note(rig, key: str, rec: Dict[str, Any], why: str) -> None:
    cur = rec.get("current") or {}
    if cur.get("waiting") != why:
        cur["waiting"] = why
        _log(rig, rec, f"loop {cur.get('n')}: {rec.get('state')} waits — {why}")
        _event(rig, key, "held", n=cur.get("n"), why=why, step=rec.get("state"))


def _hand_over_incident(rig, key: str, conf: Dict[str, Any], judge: str) -> None:
    """A negative_outcome incident opened before the round declared its loop is the loop's now."""
    import steward
    inc = steward.open_for(rig.cfg, judge, "negative_outcome")
    if inc and not inc.get("sessions"):
        steward.close(rig, inc, "course_loop", f"handed to the native course loop of {_who(conf)}")


def _hold(rig, key: str, rec: Dict[str, Any], sig: str, why: str) -> None:
    if rec.get("held") != f"{sig}|{why}":
        rec["held"] = f"{sig}|{why}"
        rec["state"] = "held"
        _log(rig, rec, f"{rec.get('judge')} closed negative; the loop waits: {why} — it starts when the phase is open")
        _event(rig, key, "held", judge=rec.get("judge"), why=why)


def _exhausted(rig, key: str, conf: Dict[str, Any], rec: Dict[str, Any], sig: str, oc: str, pid: str, why: str,
               counter: str = "loops") -> None:
    """No loop is left: the phase rests (once per judge claim) and the Owner's inbox says why."""
    if rec.get("exhausted_sig") == sig:
        return
    rec.update(exhausted_sig=sig, state="rested", held="")
    judge = conf["judge"]
    tag = conf["tag"]
    n = int(rec.get("loops", 0) or 0)
    text = f"{judge} closed with outcome {oc} after {n} course loop(s): {why}"
    rested = ""
    if pid:
        import phases
        ok, out = phases.set_state(rig.cfg, pid, "rest", by="engine", why=f"course loop {key}: {why}"[:200],
                                   only_from="open")
        if ok:
            rested = pid
            rig.st.event("phases", "phase_rest", phase=pid, cap=f"course loop {key}: {why}"[:200])
    _log(rig, rec, f"{text} — {'phase ' + rested + ' rests' if rested else 'no loop left'}")
    _event(rig, key, "rest", judge=judge, outcome=oc, loops=n, phase=rested or None, why=why)
    import steward
    _, cp = split_key(key)
    more = (f"raise `max_loops` in rounds/{tag}/round.yaml" if not cp else
            f"raise checkpoint {cp}'s `max_loops` (its judges: entry) in rounds/{tag}/round.yaml")
    more += f" and `orch phase open {pid} --caps {counter}=<n>`" if pid else ""
    steward.add_inbox(rig, f"courseloop:{key}", f"course loop {key}: {judge} still {oc} — no loop left",
                      f"What happened: {text}.{' Phase ' + rested + ' rests: running rows finish, nothing new starts.' if rested else ''}\n\n"
                      f"Impact: the rows that need a GO from {judge} stay held.\n\n"
                      f"Options:\n- A) more loops: {more} — the loop then starts on its own for this claim.\n"
                      f"- B) stop here (recommended: the judge measured the bar as not met {n + 1} time(s); read "
                      f"{_rel_review(rig.cfg, key)}/BRIEF.md before paying for another loop) — close or re-scope the "
                      f"round.\n\nEvidence: state/courseloop/{key}.json, the judge's PROGRESS.",
                      row=judge)
    rig.notify("normal", f"{rig.cfg.name}: course loop {key} has no loop left", text[:400])


def _rel_review(cfg, key: str) -> str:
    import rounds
    try:
        return str(rounds.review_dir(cfg, key).relative_to(cfg.instance))
    except ValueError:
        return str(rounds.review_dir(cfg, key))


def _start(rig, key: str, conf: Dict[str, Any], rec: Dict[str, Any], sig: str, oc: str, pid: str,
           counter: str = "loops") -> None:
    cfg = rig.cfg
    import rounds
    from store import INFEASIBLE
    judge = conf["judge"]
    n = int(rec.get("loops", 0) or 0) + 1
    res = rig.st.result(judge)
    cur = {"n": n, "outcome": oc, "sig": sig, "started": time.strftime("%F %T"),
           "started_epoch": int(time.time()), "judge_summary": str(res.get("summary", ""))[:300],
           "rr_epoch": 0, "rr_tries": 0, "counted": not pid}
    if counter != "loops":
        cur["counter"] = counter
    if bar_pin_on(cfg):
        cur["pin"] = pin(cfg, conf)
        prev = rec.get("current") or {}             # a claim right after this chain's own re-arm keeps its first pin
        cur["pin0"] = (prev.get("pin0") or prev.get("pin")) if rec.get("state") == "judging" and prev.get("pin") \
            else cur["pin"]
    if oc == INFEASIBLE:
        cur["wave"] = held_wave(cfg, judge, rig.st)
    rec.update(loops=n, state="review", handled_sig=sig, held="", phase=pid, current=cur)
    rec.pop("bar_hold", None)
    save(cfg, rec)                                # the loop exists before anything is counted or dispatched
    _count(rig, key, rec)
    rv = rounds.review_dir(cfg, key)
    if rv.is_dir() and any(rv.iterdir()):        # loop n's review starts clean; the last one is kept
        dst = rv.with_name(f"{rv.name}.loop{n - 1}")
        if dst.exists():
            dst = rv.with_name(f"{rv.name}.loop{n - 1}.{int(time.time())}")
        shutil.move(str(rv), str(dst))
    _log(rig, rec, f"loop {n}/{conf['max_loops']} starts — {judge} closed {oc}"
                   + (f"; held wave: {', '.join(cur['wave'][:12]) or 'none'}" if oc == INFEASIBLE else ""))
    _event(rig, key, "start", n=n, judge=judge, outcome=oc, phase=pid or None,
           counter=counter if counter != "loops" else None,
           wave=",".join(cur.get("wave") or [])[:300] or None)
    from rows import find_row
    jr = find_row(cfg, judge)
    rig.log(jr.lane if jr else "", f"COURSE LOOP {key} #{n}: {judge} closed {oc} — round review rr-{key} next")
    _dispatch_review(rig, key, rec)


def _count(rig, key: str, rec: Dict[str, Any]) -> None:
    """The phase's loop count (phases.yaml), once per loop — re-tried after a crash between the two writes."""
    cur = rec["current"]
    if cur.get("counted") or not rec.get("phase"):
        return
    import phases
    phases.record_loop(rig.cfg, rec["phase"], by=f"course-loop:{key}", counter=cur.get("counter", "loops"))
    cur["counted"] = True
    save(rig.cfg, rec)


def _dispatch_review(rig, key: str, rec: Dict[str, Any]) -> None:
    import rounds
    cur = rec["current"]
    reviewer = (rig.cfg.data.get("preflight") or {}).get("reviewer", "opus")
    ok, why = rig.capacity_ok(reviewer, steward=True)
    if not ok:
        rig.sched_notes[f"rr-{key}"] = f"course loop review waiting for capacity: {why}"
        return
    try:
        ok, how = rounds.dispatch_round_review(rig.cfg, rig.st, key, rig=rig)
    except Exception as e:
        ok, how = False, f"{type(e).__name__}: {e}"
    if ok:
        cur["rr_epoch"] = int(time.time())
        cur["rr_tries"] = int(cur.get("rr_tries", 0)) + 1
        _event(rig, key, "review", n=cur["n"], session=f"rr-{key}")
    else:
        cur["rr_fails"] = int(cur.get("rr_fails", 0)) + 1
        rig.st.engine_log(f"course loop {key}: rr-{key} not dispatched — {how}")
        if cur["rr_fails"] >= 10:
            _stop(rig, key, rec, f"rr-{key} could not be dispatched ({how})")


def _review(rig, key: str, conf: Dict[str, Any], rec: Dict[str, Any]) -> None:
    conf = _full(key, conf)
    import rounds
    cur = rec["current"]
    rr = f"rr-{key}"
    _count(rig, key, rec)
    if not cur.get("rr_epoch"):
        return _dispatch_review(rig, key, rec)
    v, summary = rig.sub_verdict(rr, float(cur["rr_epoch"]) - 1)
    if v == "DONE":
        import steward
        _, _, mt = rig.st.status(rr)
        q = (rounds.review_dir(rig.cfg, key) / "QUESTIONS.md").is_file()
        nxt = next_tag(key, int(cur["n"]))
        if cur.get("outcome") == "infeasible":
            ask = (f"park the held rows the new route makes moot (`orch sw park <row> replan-{key}-{cur['n']} \"why\"`), "
                   f"then plan the new route to the same goal: `orch sw plan {nxt} \"brief\"`")
        else:
            ask = f"then plan the correction: `orch sw plan {nxt} \"brief\"`"
        summ = (f"course loop {key} #{cur['n']}: {conf['judge']} closed {cur['outcome']}; round review {rr} "
                f"{'wrote QUESTIONS.md' if q else 'asked the Owner nothing'} — answer within the rights "
                f"(ANSWERS.md), INBOX the Owner's items, {ask}")
        inc = steward.open_incident(rig, "round_review", f"round:{key}", summ, klass="judgment", sig=str(int(mt)))
        if inc is None:
            return _stop(rig, key, rec, "the Steward lane is off — nobody answers the review")
        cur["incident"] = inc["n"]
        rec["state"] = "answer"
        _log(rig, rec, f"loop {cur['n']}: {rr} DONE — incident {inc['n']} (round_review) for the Steward")
        _event(rig, key, "answer", n=cur["n"], incident=inc["n"])
        return
    d = rig.st.dispatch(rr)
    if v == "NONE" and (d.get("open") or float(d.get("epoch", 0) or 0) + 1 < float(cur["rr_epoch"])):
        return                                     # still working (or a re-dispatch after an interruption)
    if int(cur.get("rr_tries", 0)) < 2:
        _log(rig, rec, f"loop {cur['n']}: {rr} ended with {v if v != 'NONE' else 'no verdict'} — dispatched once more")
        cur["rr_epoch"] = 0
        return _dispatch_review(rig, key, rec)
    _stop(rig, key, rec, f"the round review {rr} ended twice without DONE ({v if v != 'NONE' else 'no verdict'})")


def _planners(cfg) -> Dict[str, Dict[str, Any]]:
    import steward
    return steward._jread(steward.sdir(cfg) / "planners.json", {}) or {}


def _answer(rig, key: str, conf: Dict[str, Any], rec: Dict[str, Any]) -> None:
    conf = _full(key, conf)
    import steward
    cfg = rig.cfg
    cur = rec["current"]
    n = cur.get("incident")
    pls = _planners(cfg)
    mine = sorted((sid for sid, p in pls.items()
                   if str(p.get("incident")) == str(n) and float(p.get("epoch", 0) or 0) >= float(cur["started_epoch"]) - 1),
                  key=lambda x: pls[x].get("epoch", 0))
    if mine:
        p = pls[mine[0]]
        nxt = str(p.get("round"))
        if nxt == conf["tag"]:
            return _stop(rig, key, rec, f"incident {n} planned round {nxt}, which holds the judge's own lanes — a "
                                        f"correction lives in a round of its own (`plan {next_tag(key, int(cur['n']))}`)")
        cur.update(next=nxt, planner=mine[0])
        rec["state"] = "plan"
        _log(rig, rec, f"loop {cur['n']}: incident {n} dispatched planner {mine[0]} for round {nxt}")
        _event(rig, key, "plan", n=cur["n"], planner=mine[0], next=nxt)
        return
    inc = steward.load_incident(cfg, int(n)) if str(n).isdigit() else {}
    if inc and inc.get("status") != "closed":
        return
    wait = _phase_open(cfg, conf["tag"])
    if wait:                    # the Steward could not plan in a resting phase: ask again once it is open
        return _wait_note(rig, key, rec, wait)
    if cur.get("waiting") and _ask_again(rig, key, rec):
        return
    _stop(rig, key, rec, f"incident {n} closed ({inc.get('outcome', '?')}: {str(inc.get('close_why', ''))[:160]}) "
                         f"without dispatching a planner")


def _ask_again(rig, key: str, rec: Dict[str, Any]) -> bool:
    """The phase is open again after a step waited on it: a fresh round_review incident asks the Steward for the plan
    (state `answer`). Once per wait: a second close without a planner stops the loop."""
    import steward
    cur = rec["current"]
    cur["waiting"] = ""
    new = steward.open_incident(rig, "round_review", f"round:{key}",
                                f"course loop {key} #{cur['n']}: the phase is open again — plan the correction "
                                f"(`orch sw plan {next_tag(key, int(cur['n']))} \"brief\"`)", klass="judgment",
                                sig=f"reopen-{int(time.time())}")
    if not new:
        return False
    cur["incident"] = new["n"]
    rec["state"] = "answer"
    _log(rig, rec, f"loop {cur['n']}: the phase is open again — incident {new['n']} asks for the plan")
    _event(rig, key, "answer", n=cur["n"], incident=new["n"])
    return True


def loop_rows(rig, rec: Dict[str, Any]) -> List[str]:
    """The rows this loop's planners added with `orch row add` (round <next>), in order."""
    cur = rec.get("current") or {}
    nxt = str(cur.get("next") or "")
    since = float(cur.get("started_epoch", 0) or 0)
    sids = {sid for sid, p in _planners(rig.cfg).items()
            if str(p.get("round")) == nxt and float(p.get("epoch", 0) or 0) >= since - 1}
    out = []
    for e in rig.st.events():
        if (e.get("event") == "row_add" and str(e.get("by", "")) in sids
                and float(e.get("epoch", 0) or 0) >= since - 1 and e.get("id") not in out):
            out.append(str(e.get("id")))
    return out


def _plan(rig, key: str, conf: Dict[str, Any], rec: Dict[str, Any]) -> None:
    conf = _full(key, conf)
    import rounds
    import steward
    cfg = rig.cfg
    cur = rec["current"]
    nxt = cur["next"]
    since = float(cur["started_epoch"]) - 1
    ps = {sid: p for sid, p in _planners(cfg).items()
          if str(p.get("round")) == nxt and float(p.get("epoch", 0) or 0) >= since}
    if any(p.get("status") == "pass" for p in ps.values()):
        rows = loop_rows(rig, rec)
        if not rows:
            return _stop(rig, key, rec, f"the planner of round {nxt} closed PASS but added no row with `orch row add`")
        from rows import find_row
        jr = find_row(cfg, conf["judge"])
        if jr is not None and jr.lane in rounds.round_lanes(cfg, nxt):
            return _stop(rig, key, rec, f"round {nxt} lists the judge's lane {jr.lane} — a correction lives in lanes "
                                        f"of its own (rows below the judge's needs=go dependents would wait on them)")
        on, pid, _ = _phase(cfg, nxt)
        if on and pid != rec.get("phase"):
            return _stop(rig, key, rec, f"round {nxt} is in phase {pid or 'none'}, not {rec.get('phase')} — a course "
                                        f"loop stays inside its phase")
        r = rounds.load_rounds(cfg).get(nxt) or {}
        if not r.get("started") and nxt != conf["tag"]:
            return _stop(rig, key, rec, f"the planner closed PASS but round {nxt} was not started")
        cur["rows"] = rows
        rec["state"] = "run"
        _log(rig, rec, f"loop {cur['n']}: round {nxt} runs {len(rows)} row(s): {', '.join(rows[:12])}")
        _event(rig, key, "run", n=cur["n"], next=nxt, rows=",".join(rows[:40]))
        return
    if any(p.get("status") == "running" for p in ps.values()):
        return
    if steward.open_for(cfg, f"round:{nxt}", "planner"):
        return                                    # the Steward handles the failed planner (it may plan again)
    wait = _phase_open(cfg, conf["tag"])
    if wait:
        return _wait_note(rig, key, rec, wait)
    if cur.get("waiting") and _ask_again(rig, key, rec):
        return
    _stop(rig, key, rec, f"the planner(s) of round {nxt} ({', '.join(sorted(ps)) or 'none'}) did not PASS and "
                         f"nothing planned again")


def _run(rig, key: str, conf: Dict[str, Any], rec: Dict[str, Any]) -> None:
    conf = _full(key, conf)
    from rows import find_row
    import rearm
    cur = rec["current"]
    rows = list(cur.get("rows") or [])
    for x in loop_rows(rig, rec):                 # a later planner of the same round may add more
        if x not in rows:
            rows.append(x)
    cur["rows"] = rows
    gone = [x for x in rows if find_row(rig.cfg, x) is None]
    if gone:
        return _stop(rig, key, rec, f"row(s) {', '.join(gone[:8])} the planner added are no longer in any queue — the "
                                    f"judge is not re-armed without its correction")
    left = [x for x in rows if rig.st.phase(x) not in ("accepted", "parked")]
    if left:
        return
    accepted = [x for x in rows if rig.st.phase(x) == "accepted"]
    if not accepted:
        return _stop(rig, key, rec, f"every row of the correction was parked ({', '.join(rows[:8])}) — nothing was "
                                    f"corrected, so the judge is not re-armed")
    judge = conf["judge"]
    busy = rearm.target_busy(rig, judge)
    if busy:
        return
    import orchctl
    import steward
    ok, out = steward._quiet(orchctl.reset, rig, judge, by=f"course-loop:{key}#{cur['n']}")
    if not ok:
        return _stop(rig, key, rec, f"re-arming {judge} failed: {out}")
    parked = [x for x in rows if rig.st.phase(x) == "parked"]
    cur["rearmed"] = time.strftime("%F %T")
    wave = list(cur.get("wave") or [])
    if cur.get("outcome") == "infeasible":        # what the replan did with the old route's held rows
        cur["wave_parked"] = [x for x in wave if rig.st.phase(x) == "parked"]
        cur["wave_kept"] = [x for x in wave if rig.st.phase(x) not in ("parked", "accepted") and find_row(rig.cfg, x)]
    rec["state"] = "judging"
    _log(rig, rec, f"loop {cur['n']}: rows done ({len(accepted)} accepted"
                   f"{', parked: ' + ','.join(parked) if parked else ''}) — {judge} re-armed"
                   + (f"; held wave parked: {','.join(cur['wave_parked']) or 'none'}, kept: "
                      f"{','.join(cur['wave_kept']) or 'none'}" if cur.get("outcome") == "infeasible" else ""))
    _event(rig, key, "rearm", n=cur["n"], judge=judge, parked=",".join(parked) or None,
           wave_parked=",".join(cur.get("wave_parked") or []) or None,
           wave_kept=",".join(cur.get("wave_kept") or []) or None)
    jr = find_row(rig.cfg, judge)
    rig.log(jr.lane if jr else "", f"REARM {judge} (course loop {key} #{cur['n']}: the correction's rows are done)")


def _stop(rig, key: str, rec: Dict[str, Any], why: str, impact: str = "") -> None:
    import steward
    cur = rec.get("current") or {}
    rec["state"] = "stopped"
    cur["stopped"] = why[:400]
    _log(rig, rec, f"loop {cur.get('n')} STOPS: {why}")
    _event(rig, key, "stop", n=cur.get("n"), why=why[:300])
    steward.add_inbox(rig, f"courseloop:{key}", f"course loop {key} #{cur.get('n')} stopped",
                      f"What happened: {why}.\n\nImpact: "
                      f"{impact or str(rec.get('judge')) + ' keeps its negative outcome; the rows that need its GO stay held. The loop starts again on the judge' + chr(39) + 's next negative claim.'}\n\n"
                      f"Options:\n- A) finish the step by hand (the brief: {_rel_review(rig.cfg, key)}/), then `orch reset "
                      f"{rec.get('judge')}` once the correction's rows are done (recommended when the review's brief "
                      f"is sound).\n- B) leave the round where it is and re-scope it.\n\nEvidence: "
                      f"state/courseloop/{key}.json, steward/STEWARD_LOG.md.", row=str(rec.get("judge") or ""))
    rig.notify("normal", f"{rig.cfg.name}: course loop {key} stopped", why[:400])


# -- what sessions read ---------------------------------------------------------------------------------
def next_tag(key: str, n: int) -> str:
    """The suggested tag of loop n's correction round: `<tag>-l<n>`, or `<tag>-<checkpoint>-l<n>`."""
    tag, cp = split_key(key)
    return f"{tag}-l{n}" if not cp else f"{tag}-{cp}-l{n}"


def fixed_bars(cfg, key: str, rec: Optional[Dict[str, Any]] = None) -> List[str]:
    """The bars that stay fixed in this loop, as the reader should name them (the pin's labels)."""
    rec = rec or load(cfg, key)
    pinned = (rec.get("current") or {}).get("pin") or {}
    out = []
    for label in pinned:
        kind, _, rel = label.partition(":")
        out.append({"plan": f"the judge's PLAN `{rel}`", "goal": "the round's `goal:` (round.yaml)"}.get(kind, f"`{rel}`"))
    return out


def loop_context(cfg, key: str, rec: Optional[Dict[str, Any]] = None) -> str:
    """One paragraph for the round review's work order ('' when no loop runs)."""
    rec = rec or load(cfg, key)
    cur = rec.get("current") or {}
    if rec.get("state") not in ACTIVE or not cur:
        return ""
    c = loops_of(cfg).get(key)
    mx = c["max_loops"] if c else "?"
    tag, cp = split_key(key)
    who = f"the judge `{rec.get('judge')}`" if not cp else f"the checkpoint judge `{rec.get('judge')}` (a replan)"
    bars = fixed_bars(cfg, key, rec)
    fixed = (f" The bars stay fixed: {', '.join(bars)}, and every protected path — never propose to move a bar, "
             f"threshold, denominator, definition, key, scorer or test to reach the goal." if bars else
             " The bars stay fixed: never propose to move a bar, threshold, denominator, definition, key, scorer or "
             "test to reach the goal.")
    if cur.get("outcome") == "infeasible":
        wave = cur.get("wave") or []
        return (f"this review is loop {cur.get('n')} of at most {mx} of {_who(c) if c else 'the round'}'s native course "
                f"loop: {who} closed with outcome infeasible — the remaining plan cannot reach the goal "
                f"({cur.get('judge_summary', '')[:200]}). Propose a NEW ROUTE to the SAME goal for the next round "
                f"({next_tag(key, int(cur.get('n') or 1))}), and name which of the held rows ({', '.join(wave[:20]) or 'none'}) "
                f"the new route makes moot — they are parked so they never run on the old route's say-so.{fixed} If no "
                f"route within the phase's caps can reach the goal, say so: that is the Owner's question")
    return (f"this review is loop {cur.get('n')} of at most {mx} of the round's native course loop: {who} "
            f"closed with outcome {cur.get('outcome')} ({cur.get('judge_summary', '')[:200]}). "
            f"Say why the bar was not met and what correction the next round ({next_tag(key, int(cur.get('n') or 1))}) "
            f"should make; a Steward answers your questions within the decision rights and a planner cuts the rows.{fixed}")


def bundle_lines(rig, key: str) -> List[str]:
    """The round_review incident's bundle section while a loop waits for its answers."""
    cfg = rig.cfg
    rec = load(cfg, key)
    cur = rec.get("current") or {}
    if rec.get("state") not in ACTIVE or not cur:
        return []
    c = loops_of(cfg).get(key)
    tag, cp = split_key(key)
    on, pid, ph = _phase(cfg, tag)
    nxt = next_tag(key, int(cur.get("n") or 1))
    qd = _rel_review(cfg, key)
    caps = (ph.get("caps") or {}) if on else {}
    counter = cur.get("counter", "loops")
    infeasible = cur.get("outcome") == "infeasible"
    bars = fixed_bars(cfg, key, rec)
    L = ["", "## The native course loop (round.yaml on_fail: course_loop)", "",
         f"- {'checkpoint ' if cp else ''}judge `{rec.get('judge')}` closed with outcome **{cur.get('outcome')}**: "
         f"{cur.get('judge_summary', '')}",
         f"- loop {cur.get('n')} of at most {c['max_loops'] if c else '?'}"
         + (f"; phase {pid}: {ph.get(counter, 0)} {'loop(s)' if counter == 'loops' else 'replan(s)'} used, cap "
            f"{caps.get(counter, 'none')}" if on else ""),
         f"- the review: `{qd}/BRIEF.md`, `{qd}/QUESTIONS.md` (if it asked anything)",
         f"- the bars that stay fixed (pinned: a change stops this loop): {', '.join(bars) or 'the judge PLAN and the goal'}"
         f"; protected paths: `./orch guard protected`. Replan the route, never the goal."]
    if infeasible:
        wave = cur.get("wave") or []
        L += [f"- **infeasible: the remaining plan cannot reach the goal.** The rows it holds (the old route's wave): "
              f"{', '.join(wave) or 'none'}. Park each one the new route makes moot — they cannot produce a result — and "
              f"keep only those the new route still needs (they wait for the judge's next GO). Never park a judge: its "
              f"bar stays."]
    L += ["- your steps:",
          f"  1. answer each question within the decision rights in `{qd}/ANSWERS.md`, one line per question "
          f"(`Q1: B — <why>`); a W question: `./orch sw inbox …` and the line `Q<k>: INBOX #<item>`; class P: "
          f"`./orch sw escalate-panel` and close ESCALATE (the panel answers and plans);"]
    if infeasible:
        L += [f"  2. park the held rows the new route makes moot: `./orch sw park <row> replan-{key}-{cur.get('n')} "
              f"\"why\"` (one per row; a row the Owner parked stays as it is);"]
    L += [f"  {3 if infeasible else 2}. `./orch sw plan {nxt} \"<brief: the {'new route to the same goal' if infeasible else 'correction'}, "
          f"the rows, their tiers, the caps>\"` — its planner creates round {nxt}"
          + (f" in phase {pid} (`round-new {nxt} --phase {pid} <new lane>`)" if pid else "")
          + ", adds the rows with `orch row add`, starts the round;",
          f"  {4 if infeasible else 3}. close RESOLVED. Never reset the judge: the engine re-arms it when the planner's rows are accepted "
          "(or parked), and it measures the same bar. Nothing here opens or starts another phase."]
    return L


def status_lines(cfg, tag: str) -> List[str]:
    good_all, bad_all = configs(cfg)
    good = [c for c in good_all.values() if c["tag"] == tag]
    mine_bad = {k: w for k, w in bad_all.items() if split_key(k)[0] == tag}
    out: List[str] = []
    for c in sorted(good, key=lambda c: (c["kind"] != FINAL, c["judge"])):
        rec = load(cfg, c["key"])
        cur = rec.get("current") or {}
        what = f"judge {c['judge']}" if c["kind"] == FINAL else f"checkpoint {c['judge']}"
        out.append(f"           course loop: {what}, loops {rec.get('loops', 0)}/{c['max_loops']}, "
                   f"state {rec.get('state') or 'idle'}"
                   f"{' (' + str(cur.get('stopped'))[:100] + ')' if rec.get('state') == 'stopped' else ''}")
    for k, why in mine_bad.items():
        out.append(f"           course loop: OFF — {why}")
    return out


def digest_lines(cfg, ev: List[Dict[str, Any]]) -> List[str]:
    loops = loops_of(cfg)
    mine = [e for e in ev if str(e.get("event", "")).startswith("course_loop_")]
    if not loops and not mine:
        return []
    L = ["", "## Course loops (inside the open phase)", ""]
    for key, c in loops.items():
        rec = load(cfg, key)
        what = f"round {key}: judge" if c["kind"] == FINAL else f"round {c['tag']}: checkpoint"
        L.append(f"- {what} {c['judge']}, {rec.get('loops', 0)}/{c['max_loops']} loop(s), "
                 f"{rec.get('state') or 'idle'}")
    for e in mine:
        what = str(e.get("event"))[len("course_loop_"):]
        detail = ", ".join(f"{k} {e[k]}" for k in ("n", "outcome", "incident", "planner", "next", "why", "moved",
                                                   "wave", "wave_parked", "wave_kept") if e.get(k))
        L.append(f"- {e.get('ts', '')[5:16]} {str(e.get('id', '')).replace('round:', 'round ')}: {what}"
                 f"{' — ' + detail[:220] if detail else ''}")
    return L
