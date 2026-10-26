#!/usr/bin/env python3
"""Rounds, lane archive, and the optional goal metric (v2).

A ROUND is a tag, the lanes that carry it, an optional goal, an optional rules
delta and an optional preflight requirement:

    <instance>/rounds/<tag>/round.yaml
        tag: round-a
        title: "final push to 11/11"
        status: open | closed
        lanes: [A, B, C]
        goal: {metric_cmd: "...", name: planted, direction: up, target: 11,
               instrument: v6b, stall_after: 6, run_on: accept, auto_review: true}
        preflight: {required: true}
    <instance>/rounds/<tag>/RULES.md     the round's rules delta (work orders point at it)

Why: an earlier rig re-did by hand, every round, what a round object does — appending
lanes to orch.yaml, layering a new EXECUTION_RULES section that "wins over" the
last, re-placing judge/report rows, and comparing a metric whose scorer changed
under it. Closing a round archives its lanes (most lanes were finished and
still polled and drawn).

Goal metric: `metric_cmd` runs from the project root after an accepted row (or on
`orch score`). Its output names values as `METRIC <name>=<value>` lines (or a bare
number on the last line). Every run lands in <instance>/scoreboard.tsv. A run
whose `instrument` differs from the series' previous one starts a NEW series —
an instrument change is never read as movement. `stall_after: N` runs with no
improvement pause the round's lanes and page the operator.
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
import yaml  # noqa: E402
from config_parser import Config, load  # noqa: E402
from rows import active_lanes, archive_dir, load_rows, parse_file  # noqa: E402
from store import Store  # noqa: E402


def rounds_dir(cfg: Config) -> Path:
    return cfg.instance / "rounds"


def load_rounds(cfg: Config) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    d = rounds_dir(cfg)
    if not d.is_dir():
        return out
    for f in sorted(d.glob("*/round.yaml")):
        try:
            data = yaml.safe_load(f.read_text()) or {}
        except Exception:
            continue
        if not isinstance(data, dict):
            continue                      # a malformed round.yaml is no round (it never stops a caller)
        data.setdefault("tag", f.parent.name)
        data.setdefault("lanes", [])
        data.setdefault("status", "open")
        out[str(data["tag"])] = data
    return out


def lane_claims(cfg: Config, lanes: List[str], tag: str, st: Optional[Store] = None) -> List[str]:
    """Why a session may not list these lanes in round `tag`: a lane another round that is not closed
    already lists, or (with `st`) a lane whose rows have already run outside any round. A session (a
    planner, a course-loop row, a Steward) may not pull a live lane into its own round: what it may
    reset, approve and add rows to follows the round's lanes (v3, lib/rowadd.py)."""
    out = []
    for t, r in load_rounds(cfg).items():
        if t == tag or r.get("status") == "closed":
            continue
        out += [f"{l} (round {t})" for l in lanes if l in [str(x) for x in r.get("lanes", [])]]
    if st is not None:
        import rowadd
        for l in lanes:
            if any(l in o for o in out):
                continue
            ran = [x.id for x in parse_file(cfg.instance / f"queue_{l}.tsv", l) if rowadd.history(st, x.id)]
            if ran:
                out.append(f"{l} (its rows already ran: {', '.join(ran[:4])})")
    return out


def save_round(cfg: Config, data: Dict[str, Any]) -> Path:
    d = rounds_dir(cfg) / str(data["tag"])
    d.mkdir(parents=True, exist_ok=True)
    f = d / "round.yaml"
    f.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True))
    return f


def round_of_lane(cfg: Config, lane: str) -> str:
    for tag, r in load_rounds(cfg).items():
        if lane in [str(x) for x in r.get("lanes") or []]:
            return tag
    return ""


# -- executor policy per round (v3, drop D item 3) -----------------------------------------------------
POLICY_KEYS = ("retier_on_max_unblock", "dead_window_policy", "unblock_executors", "max_parallel")
_POLICY_CACHE: Dict[str, Any] = {}


def policy_problems(cfg: Config, pol: Any) -> List[str]:
    """What is wrong with a round.yaml `executors:` block (each bad key is ignored, never half-applied)."""
    if pol in (None, {}):
        return []
    if not isinstance(pol, dict):
        return ["executors: must be a mapping"]
    allowed = list(cfg.data["executors"].get("allowed") or [])
    out = [f"executors.{k}: not a per-round key (have: {', '.join(POLICY_KEYS)})" for k in pol if k not in POLICY_KEYS]
    if "retier_on_max_unblock" in pol and pol["retier_on_max_unblock"] not in allowed:
        out.append(f"executors.retier_on_max_unblock: '{pol['retier_on_max_unblock']}' is not in executors.allowed")
    elif "retier_on_max_unblock" in pol and not cfg.decides(pol["retier_on_max_unblock"]):
        out.append(f"executors.retier_on_max_unblock: '{pol['retier_on_max_unblock']}' is a junior tier — it must be a hard tier")
    if "dead_window_policy" in pol and pol["dead_window_policy"] not in ("halt", "retier"):
        out.append("executors.dead_window_policy: halt or retier")
    if "unblock_executors" in pol:
        u = pol["unblock_executors"]
        if not isinstance(u, list) or not u or any(x not in allowed for x in u):
            out.append("executors.unblock_executors: a non-empty list of executors in executors.allowed")
        elif any(not cfg.decides(x) for x in u):
            out.append("executors.unblock_executors: every unblocker must be a hard tier (never a junior one)")
    if "max_parallel" in pol:
        m = pol["max_parallel"]
        if not isinstance(m, dict) or any(not isinstance(v, int) or isinstance(v, bool) or v < 0 for v in m.values()):
            out.append("executors.max_parallel: {executor: whole number ≥ 0}")
    return out


def _bad_keys(cfg: Config, pol: Dict[str, Any]) -> List[str]:
    return [k for k in POLICY_KEYS if k in pol and policy_problems(cfg, {k: pol[k]})]


def exec_policy(cfg: Config, lane: str) -> Dict[str, Any]:
    """The executor settings for a row of `lane`: the rig's `executors:` with its round's `executors:` keys
    (retier_on_max_unblock, dead_window_policy, unblock_executors — only from a round the operator made, no `by:`)
    laid over them, plus `round_max_parallel`
    ({executor: n} — at most n live sessions of that round's rows and their sub-sessions on that executor; the
    rig's own caps still apply, so a round only ever narrows them) and `round` (the tag, '' for none). An open
    round that lists the lane wins over a closed one; invalid keys are ignored (`orch lint` names them). Cached
    on the round files' mtimes: the scheduler asks for every ready row."""
    d = rounds_dir(cfg)
    sig = tuple(sorted((str(f), f.stat().st_mtime_ns) for f in d.glob("*/round.yaml"))) if d.is_dir() else ()
    key = str(cfg.instance)
    c = _POLICY_CACHE.get(key)
    if not c or c[0] != sig or c[1] is not cfg.data:
        lanes: Dict[str, Tuple[str, Dict[str, Any]]] = {}
        rs = load_rounds(cfg)
        for closed in (False, True):
            for t, r in rs.items():
                if (r.get("status") == "closed") != closed:
                    continue
                pol = r.get("executors") if isinstance(r.get("executors"), dict) else {}
                bad = _bad_keys(cfg, pol)
                pol = {k: v for k, v in pol.items() if k in POLICY_KEYS and k not in bad}
                if r.get("by"):          # a round a session made only narrows (max_parallel): tiers stay the rig's
                    pol = {k: v for k, v in pol.items() if k == "max_parallel"}
                lanes_ = r.get("lanes") if isinstance(r.get("lanes"), list) else []
                for l in [str(x) for x in lanes_]:
                    lanes.setdefault(l, (t, pol))
        c = _POLICY_CACHE[key] = (sig, cfg.data, lanes)
    tag, pol = c[2].get(lane, ("", {}))
    out = dict(cfg.data["executors"])
    for k in ("retier_on_max_unblock", "dead_window_policy", "unblock_executors"):
        if k in pol:
            out[k] = pol[k]
    out["round_max_parallel"] = dict(pol.get("max_parallel") or {})
    out["round"] = tag
    return out


def review_key(cfg: Config, key: str) -> Tuple[str, str]:
    """A round review's key -> (round tag, checkpoint judge or ''). A course loop of a checkpoint judge (v3.1,
    round.yaml `judges:`) reviews as `rr-<tag>+<judge>`; a real round's tag is always just itself."""
    if "+" in key and not (rounds_dir(cfg) / key / "round.yaml").is_file():
        t, j = key.split("+", 1)
        return t, j
    return key, ""


def review_dir(cfg: Config, key: str) -> Path:
    """Where `rr-<key>` writes its brief: rounds/<tag>/review/, or rounds/<tag>/review+<judge>/ for a checkpoint."""
    t, j = review_key(cfg, key)
    return rounds_dir(cfg) / t / ("review" if not j else f"review+{j}")


def round_lanes(cfg: Config, tag: str) -> List[str]:
    lanes = (load_rounds(cfg).get(tag) or {}).get("lanes")
    return [str(x) for x in lanes] if isinstance(lanes, list) else []


def goal_for_lane(cfg: Config, lane: str) -> Optional[Dict[str, Any]]:
    tag = round_of_lane(cfg, lane)
    if tag:
        g = load_rounds(cfg)[tag].get("goal")
        if g:
            return dict(g, _series=tag)
    g = cfg.data.get("goal") or {}
    return dict(g, _series="rig") if g else None


def preflight_block(cfg: Config, st: Store, lane: str) -> str:
    tag = round_of_lane(cfg, lane)
    if not tag:
        return ""
    r = load_rounds(cfg)[tag]
    req = (r.get("preflight") or {}).get("required", (cfg.data.get("preflight") or {}).get("required", False))
    if not req:
        return ""
    if (cfg.control / f"GO_preflight_{tag}").exists():
        return ""
    pf = f"pf-{tag}"
    v, _, mt = st.status(pf)
    d = st.dispatch(pf)
    if v == "PASS" and d.get("ident") and mt + 1 >= float(d.get("epoch", 0) or 0):
        if d.get("plans_sha") == round_plans_sha(cfg, tag):
            return ""
        return f"round {tag}'s plans changed after their plan review PASSED — review again (orch preflight --round {tag} --review)"
    return f"round {tag} needs a passing plan review first (orch preflight --round {tag} --review)"


def round_plans_sha(cfg: Config, tag: str) -> str:
    """One digest over the round's queue lines and plan files: a plan review passes
    THESE plans, not whatever the folder holds later."""
    import hashlib
    r = load_rounds(cfg).get(tag) or {}
    lanes = [str(x) for x in r.get("lanes", [])]
    h = hashlib.sha256()
    rows, _ = load_rows(cfg, include_archived=False)
    for x in rows:
        if x.lane not in lanes:
            continue
        h.update(f"{x.lane}\t{x.id}\t{','.join(x.deps)}\t{x.gate}\t{x.queue_exec}\t{x.plan}\n".encode())
        try:
            h.update((cfg.workspace / x.plan).read_bytes())
        except Exception:
            h.update(b"<missing>")
    return h.hexdigest()


# -- scoreboard ----------------------------------------------------------------
METRIC_LINE = re.compile(r"^\s*METRIC\s+([A-Za-z0-9_.-]+)\s*[=:]\s*(-?\d+(?:\.\d+)?)")


def parse_metric(output: str, name: str) -> Optional[float]:
    vals: Dict[str, float] = {}
    for line in output.splitlines():
        m = METRIC_LINE.match(line)
        if m:
            vals[m.group(1)] = float(m.group(2))
    if name in vals:
        return vals[name]
    if vals and not name:
        return next(iter(vals.values()))
    lines = [l.strip() for l in output.splitlines() if l.strip()]
    if lines:
        m = re.fullmatch(r"-?\d+(?:\.\d+)?", lines[-1])
        if m:
            return float(lines[-1])
    return None


def scoreboard(cfg: Config) -> List[Dict[str, str]]:
    f = cfg.instance / "scoreboard.tsv"
    out: List[Dict[str, str]] = []
    if not f.is_file():
        return out
    lines = f.read_text().splitlines()
    if not lines:
        return out
    head = lines[0].split("\t")
    for line in lines[1:]:
        parts = line.split("\t")
        out.append(dict(zip(head, parts)))
    return out


def record_metric(cfg: Config, st: Store, rid: str, lane: str, rc: int, output: str, engine=None) -> None:
    g = goal_for_lane(cfg, lane) or {}
    name = str(g.get("name", "metric"))
    series = g.get("_series", "rig")
    inst = str(g.get("instrument", ""))
    val = parse_metric(output, name) if rc == 0 else None
    f = cfg.instance / "scoreboard.tsv"
    if not f.exists():
        f.write_text("ts\tseries\tinstrument\tname\tvalue\ttrigger\tdelta\tbest\tstall\n")
    prev = [r for r in scoreboard(cfg) if r.get("series") == series and r.get("name") == name
            and r.get("instrument") == inst and r.get("value") not in ("", "ERR")]
    up = str(g.get("direction", "up")) != "down"
    best = None
    for r in prev:
        v = float(r["value"])
        best = v if best is None else (max(best, v) if up else min(best, v))
    stall = int(prev[-1].get("stall", "0") or 0) if prev else 0
    delta = ""
    if val is None:
        line_val = "ERR"
    else:
        line_val = f"{val:g}"
        if best is None:
            stall = 0
            delta = "new series" if not prev else ""
        else:
            improved = val > best if up else val < best
            delta = f"{val - float(prev[-1]['value']):+g}"
            stall = 0 if improved else stall + 1
            best = val if improved else best
        best = val if best is None else best
    with open(f, "a") as fh:
        fh.write(f"{time.strftime('%F %T')}\t{series}\t{inst}\t{name}\t{line_val}\t{rid}\t{delta}\t"
                 f"{'' if best is None else f'{best:g}'}\t{stall}\n")
    st.event(rid, "metric", series=series, name=name, value=line_val, instrument=inst, stall=stall, rc=rc)
    msg = f"{name} {line_val} ({series}, instrument {inst or '-'}) after {rid}; best {best if best is not None else '-'}"
    if engine is not None:
        engine.log(lane, f"METRIC {msg}")
    target = g.get("target")
    if val is not None and target is not None:
        hit = val >= float(target) if up else val <= float(target)
        if hit and st.once(f"goal-hit-{series}-{name}-{inst}"):
            if engine is not None:
                engine.notify("normal", f"{cfg.name}: goal reached — {name} {line_val}", msg)
    sa = int(g.get("stall_after", 0) or 0)
    if sa and stall >= sa and st.once(f"goal-stall-{series}-{name}-{inst}-{len(prev)}"):
        lanes = load_rounds(cfg).get(series, {}).get("lanes", []) if series != "rig" else []
        for l in lanes:
            (cfg.control / f"PAUSE_{l}").write_text(f"goal stall: {stall} runs with no improvement\n")
        if series == "rig":
            (cfg.control / "PAUSE").write_text(f"goal stall: {stall} runs with no improvement\n")
        st.event(rid, "goal_stall", series=series, stall=stall)
        auto = bool(g.get("auto_review")) and series != "rig"
        if auto:
            try:
                dispatch_round_review(cfg, st, series, rig=engine)
            except Exception as e:
                auto = False
                st.engine_log(f"round review for {series} not dispatched: {e}")
        if engine is not None:
            engine.notify("critical", f"{cfg.name}: {name} has not improved in {stall} runs — round paused",
                          msg + (". Round review rr-" + series + " dispatched." if auto else
                                 f". Run the course-correction review: orch round review {series}"))


def dispatch_round_review(cfg: Config, st: Store, tag: str, reviewer: str = "", rig=None):
    """The course-correction review (COURSE_LOOP step 1) as a dispatched session:
    it reads the round's report, scoreboard and block sheet and writes a brief with
    the Owner's decisions into rounds/<tag>/review/. While the round's native course loop
    (lib/courseloop.py) runs, the work order says which loop and why."""
    import report as rep
    import template_engine as te
    from engine import Rig
    rig = rig or Rig(cfg)
    rtag, _ = review_key(cfg, tag)        # a checkpoint's loop reviews the same round into its own folder (v3.1)
    rpt = rounds_dir(cfg) / rtag / "REPORT.md"
    tmp = rpt.with_name(f".{rpt.name}.{os.getpid()}.{time.time_ns()}.tmp")   # two loops of one round may write it
    tmp.write_text(rep.render(cfg, round_tag=rtag))
    os.replace(tmp, rpt)
    rr = f"rr-{tag}"
    st.set_aside(rr, f"old{int(time.time())}")
    reviewer = reviewer or (cfg.data.get("preflight") or {}).get("reviewer", "opus")
    try:
        import courseloop
        loop = courseloop.loop_context(cfg, tag)
    except Exception:
        loop = ""
    order = te.write_roundreview(cfg, rr, rtag, reviewer, te.rel_to_root(cfg, rpt), loop=loop,
                                 briefdir=review_dir(cfg, tag))
    return rig.dispatch_sub(rr, reviewer, order, "", "", {"round": rtag})


# -- archive ----------------------------------------------------------------------
def lane_done(cfg: Config, st: Store, lane: str) -> bool:
    rows = parse_file(cfg.instance / f"queue_{lane}.tsv", lane)
    return bool(rows) and all(st.done(r.id) for r in rows)


def archive_lanes(cfg: Config, lanes: List[str], force: bool = False, dry: bool = False) -> List[str]:
    st = Store(cfg.state)
    out = []
    archive_dir(cfg).mkdir(exist_ok=True)
    for l in lanes:
        q = cfg.instance / f"queue_{l}.tsv"
        if not q.is_file():
            if (archive_dir(cfg) / q.name).is_file():
                print(f"  {l}: already archived (archive/{q.name})")
            else:
                print(f"  skip {l}: no active queue_{l}.tsv")
            continue
        if not force and not lane_done(cfg, st, l):
            open_rows = [r.id for r in parse_file(q, l) if not st.done(r.id)]
            print(f"  skip {l}: {len(open_rows)} row(s) not accepted ({', '.join(open_rows[:6])}) — --force to archive anyway")
            continue
        dst = archive_dir(cfg) / q.name
        if dst.exists():
            print(f"  skip {l}: archive/{q.name} already exists")
            continue
        if dry:
            print(f"  would archive {l}")
        else:
            import rowadd
            with rowadd.queue_lock(cfg):     # never between a `row add` and its record
                shutil.move(str(q), str(dst))
            for c in (f"RUN_{l}", f"PAUSE_{l}", f"STOP_{l}"):
                (cfg.control / c).unlink(missing_ok=True)
            st.lane_log(l, "lane archived (queue moved to archive/) — its rows still satisfy deps")
            print(f"  archived {l}")
        out.append(l)
    return out


# -- CLI ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(prog="orch round")
    sub = ap.add_subparsers(dest="cmd", required=True)
    n = sub.add_parser("new"); n.add_argument("tag"); n.add_argument("--title", default="")
    n.add_argument("--lanes", nargs="*", default=[]); n.add_argument("--preflight-required", action="store_true")
    n.add_argument("--phase", default="", help="the open phase the round belongs to (needed when the rig has phases)")
    a = sub.add_parser("add-lanes"); a.add_argument("tag"); a.add_argument("lanes", nargs="+")
    s = sub.add_parser("start"); s.add_argument("tag"); s.add_argument("--force", action="store_true")
    st_ = sub.add_parser("status"); st_.add_argument("tag", nargs="?")
    c = sub.add_parser("close"); c.add_argument("tag"); c.add_argument("--force", action="store_true")
    c.add_argument("--dry-run", action="store_true")
    sub.add_parser("list")
    ar = sub.add_parser("archive"); ar.add_argument("lanes", nargs="*"); ar.add_argument("--complete", action="store_true")
    ar.add_argument("--force", action="store_true"); ar.add_argument("--dry-run", action="store_true")
    rv = sub.add_parser("review"); rv.add_argument("tag"); rv.add_argument("--reviewer", default="")
    sc = sub.add_parser("score"); sc.add_argument("--lane", default=""); sc.add_argument("--show", action="store_true")
    args = ap.parse_args()
    cfg = load()
    st = Store(cfg.state)
    import capability
    sess, sess_kind = capability.identity(cfg)
    if sess and args.cmd in ("start", "close", "archive", "review", "new", "add-lanes"):
        # a planner/course-loop row whose dispatch snapshot carries rearm=round may write a round's
        # files itself; starting it stays the engine's act (`orch rearm round-start <tag>`)
        import rearm
        granted = (args.cmd in ("new", "add-lanes") and rearm.caller_ok(st, sess, sess_kind)[0]
                   and rearm.ROUND in rearm.snapshot_grants(st, sess))
        if not granted:
            hint = (f" A row with rearm=round asks the engine: `orch rearm round-start {args.tag} \"reason\"`."
                    if args.cmd == "start" else "")
            print(f"REFUSED: `orch round {args.cmd}` is the operator's (or the planner's) command, not session "
                  f"{sess}'s.{hint}")
            return 1
    rounds = load_rounds(cfg)
    actor = sess or os.environ.get("ORCH_ACTOR", "")    # a session, or the engine acting for one
    if actor and args.cmd in ("new", "add-lanes"):
        taken = lane_claims(cfg, list(args.lanes or []), args.tag, st)
        if taken:
            print(f"REFUSED: lane(s) {', '.join(taken)} belong to another open round or are already live; {actor} "
                  f"may not take them (the operator may)")
            return 1

    if args.cmd == "new":
        if args.tag in rounds:
            print(f"round {args.tag} already exists"); return 1
        import phases
        fence = phases.fence(cfg, list(args.lanes or []), new_round=True, phase=args.phase, tag=args.tag)
        if fence:               # the phase fence (drop D): a new round names an open phase; none opens here
            print(f"REFUSED: {fence}"); return 1
        data = {"tag": args.tag, "title": args.title, "status": "open", "created": time.strftime("%F %T"),
                "lanes": args.lanes}
        if actor:
            data["by"] = actor           # rows a session may add follow the rounds it created (lib/rowadd.py)
        if args.preflight_required:      # otherwise the rig's preflight.required applies (v2 pinned false here)
            data["preflight"] = {"required": True}
        if args.phase:
            data["phase"] = args.phase   # readable only; the fence trusts state/phases.json
        f = save_round(cfg, data)
        if args.phase:
            err = phases.add_round(cfg, args.phase, args.tag, by=actor or "owner")
            if err:                       # only what this call wrote goes: a folder's other files stay
                f.unlink(missing_ok=True)
                print(f"REFUSED: {err}"); return 1
        rules = f.parent / "RULES.md"
        if not rules.exists():
            rules.write_text(f"# Round {args.tag} — rules delta\n\nOnly what is specific to this round. "
                             f"Standing rules live in rules/BASE.md; rulings are cited by id.\n")
        print(f"round {args.tag} created: {f}")
        print(f"next: write its PLANs + queue_<L>.tsv, `orch round add-lanes {args.tag} <L>...`, "
              f"`orch preflight --round {args.tag}`, then `orch round start {args.tag}`")
        return 0
    if args.cmd == "add-lanes":
        r = rounds.get(args.tag)
        if not r:
            print(f"no round {args.tag}"); return 1
        r["lanes"] = list(dict.fromkeys([*r.get("lanes", []), *args.lanes]))
        save_round(cfg, r)
        print(f"round {args.tag} lanes: {' '.join(r['lanes'])}")
        return 0
    if args.cmd == "start":
        r = rounds.get(args.tag)
        if not r:
            print(f"no round {args.tag}"); return 1
        blk = ""
        for l in r.get("lanes", []):
            blk = blk or preflight_block(cfg, st, l)
        if blk and not args.force:
            print(f"REFUSED: {blk}  (or --force on the record)"); return 1
        import phases
        fence = phases.round_fence(cfg, args.tag)
        if fence:               # --force never crosses it: the phase is the Owner's
            print(f"REFUSED: {fence}"); return 1
        missing = [l for l in r.get("lanes", []) if not (cfg.instance / f"queue_{l}.tsv").is_file()]
        if missing:
            print(f"REFUSED: no queue file for lane(s) {' '.join(missing)}"); return 1
        for l in r.get("lanes", []):
            (cfg.control / f"PAUSE_{l}").unlink(missing_ok=True)
        # the same path as `orch start <lanes>`: framework checks, RUN_ markers, engine up
        import orchctl
        from engine import Rig
        orchctl.start(Rig(cfg), list(r.get("lanes", [])))
        r["started"] = time.strftime("%F %T")
        save_round(cfg, r)
        st.event(f"round:{args.tag}", "round_start", lanes=r.get("lanes"), by=os.environ.get("ORCH_ACTOR") or None)
        print(f"round {args.tag} started: lanes {' '.join(r.get('lanes', []))} enabled")
        return 0
    if args.cmd in ("status", "list"):
        tags = [args.tag] if getattr(args, "tag", None) else list(rounds)
        rows, _ = load_rows(cfg)
        for t in tags:
            r = rounds.get(t)
            if not r:
                print(f"no round {t}"); continue
            lanes = [str(x) for x in r.get("lanes", [])]
            rs = [x for x in rows if x.lane in lanes]
            done = sum(st.done(x.id) for x in rs)
            print(f"{t:<10} {r.get('status', 'open'):<7} {done}/{len(rs)} accepted   lanes {' '.join(lanes)}"
                  f"   {r.get('title', '')}")
            try:
                import courseloop
                for line in courseloop.status_lines(cfg, t):
                    print(line)
            except Exception:
                pass
            pol = r.get("executors")
            if pol:
                bad = policy_problems(cfg, pol)
                print(f"           executors: {', '.join(f'{k}={v}' for k, v in pol.items())}"
                      f"{'  (ignored: ' + '; '.join(bad) + ')' if bad else ''}")
            g = r.get("goal")
            if g:
                pts = [x for x in scoreboard(cfg) if x.get("series") == t]
                last = pts[-1] if pts else {}
                print(f"           goal {g.get('name')} → {g.get('target', '?')} ({g.get('direction', 'up')}); "
                      f"last {last.get('value', '-')} best {last.get('best', '-')} stall {last.get('stall', '-')}")
        if not tags:
            print("no rounds (orch round new <tag>)")
        return 0
    if args.cmd == "close":
        r = rounds.get(args.tag)
        if not r:
            print(f"no round {args.tag}"); return 1
        lanes = [str(x) for x in r.get("lanes", [])]
        done = archive_lanes(cfg, lanes, force=args.force, dry=args.dry_run)
        if args.dry_run:
            return 0
        # a lane counts as archived when this call archived it or its queue is already in archive/ (an earlier
        # `orch round archive`) and no active queue is left; a lane with neither keeps the round open
        pending = [l for l in lanes if l not in done and not (
            not (cfg.instance / f"queue_{l}.tsv").is_file() and (archive_dir(cfg) / f"queue_{l}.tsv").is_file())]
        if pending:
            print(f"some lanes were not archived ({', '.join(pending)}) — round stays open"); return 1
        r["status"] = "closed"
        r["closed"] = time.strftime("%F %T")
        save_round(cfg, r)
        try:
            import report
            (rounds_dir(cfg) / args.tag / "SUMMARY.md").write_text(report.render(cfg, round_tag=args.tag))
        except Exception as e:
            print(f"(summary not written: {e})")
        st.event(f"round:{args.tag}", "round_close")
        print(f"round {args.tag} closed; summary rounds/{args.tag}/SUMMARY.md")
        return 0
    if args.cmd == "archive":
        lanes = args.lanes
        if args.complete:
            lanes = [l for l in active_lanes(cfg) if lane_done(cfg, st, l)]
        if not lanes:
            print("nothing to archive"); return 0
        archive_lanes(cfg, lanes, force=args.force, dry=args.dry_run)
        return 0
    if args.cmd == "review":
        if args.tag not in rounds:
            print(f"no round {args.tag}"); return 1
        ok, how = dispatch_round_review(cfg, st, args.tag, args.reviewer)
        print(f"round review rr-{args.tag}: {'dispatched' if ok else 'NOT dispatched — ' + how}")
        return 0 if ok else 1
    if args.cmd == "score":
        if args.show:
            for r in scoreboard(cfg)[-30:]:
                print("  ".join(f"{k}={v}" for k, v in r.items()))
            return 0
        lane = args.lane or next(iter(active_lanes(cfg)), "")
        g = goal_for_lane(cfg, lane)
        if not g or not g.get("metric_cmd"):
            print("no goal.metric_cmd for this lane's round or the rig"); return 1
        p = subprocess.run(["bash", "-c", g["metric_cmd"]], cwd=str(cfg.root), capture_output=True, text=True)
        record_metric(cfg, st, "manual", lane, p.returncode, p.stdout + p.stderr)
        print(scoreboard(cfg)[-1])
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
