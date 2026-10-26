#!/usr/bin/env python3
"""Engine-mediated queue edits (v3, drop B item 5): `orch row add`, `orch row sync`, `orch row diff`.

v2 planners appended rows by editing `queue_<L>.tsv` by hand, so nothing stopped a planner from
dropping a gate, granting a row more re-arm power than it had itself, or cutting rows past a phase.
Now a row is added through one command the engine validates:

    orch row add <lane> "<queue line>" "why"

  the operator   (no session — capability.session(cfg), GDV3-2): validated and appended in its own
                 process, under the queue lock;
  a planner      pl-<tag>-<k>, lanes of its own round only;
  a course-loop  row whose DISPATCH SNAPSHOT carries rearm=…,round (the marker that already lets it
                 write a round's files): its own lane and the lanes of rounds it created;
  sessions write state/rowadd/requests/<sid>.<ms>.json with the CLI's pid; the engine (tick phase
  row_requests) answers only while that `orch row add` process runs inside the named session's pane,
  re-validates and appends, and answers in state/rowadd/done/. Every other session is refused.

Checks (one function, `validate`, for the CLI and the engine):
  * the line parses (rows.parse_line, the one parser) and is one row; the lane is not archived;
  * the id is NEW — no queue row and no history in state/; no line ever replaces a row, so no gate
    is removed or changed (a re-declared gated row is refused by name); no session prefix;
  * deps exist; executor and reviewer= allowed; the plan file exists inside the workspace, its folder
    not the instance's; gate_kind is steward|owner|spend and only on a gated row;
  * from a session: no twin of an existing row's plan/folder, no gate_kind=steward, no review=0, a
    row's extra allowlist inside its own;
  * rearm= grants never wider than the caller's own (a row: a subset of its snapshot grants, never
    itself; a planner: rows of its round's lanes, never `round`);
  * the phase fence and the phase's row cap (lib/phases.py, drop D: `phases.fence(cfg, [lane])`,
    `phases.row_cap(cfg, lane, row)`) — no phases.py: pass; a failing check: refused.

The queue watch (guard.queue_watch, default false): the engine keeps state/queues.sha (lane -> sha256
of the queue's row lines; comments and whitespace do not count) and the blessed copy
state/queues/<lane>.tsv. A lane whose rows differ is reported once per edit, after
guard.queue_watch_grace seconds — event queue_edited, the lane log, the digest ("queue edited
outside orch row add"). Blessed: by `row add`
(its line only), by `orch row sync` (the operator), and when the lane's file equals HEAD and the last
commit that touched it is an operator `plan:` commit (state/protected_commits.jsonl) — the operator's
shell. A lane archived unchanged is dropped silently.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))

LANE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
SESSION_PREFIXES = ("ub-", "rv-", "pf-", "rr-", "sw-", "pl-")
GATE_KINDS = ("steward", "owner", "spend")
PHASES = Path(__file__).resolve().parent / "phases.py"      # drop D
HEADER = ("# queue_{lane}.tsv — id deps gate executor plan_path [extras]; created by `orch row add`\n")


def watch_on(cfg) -> bool:
    return bool((cfg.data.get("guard") or {}).get("queue_watch", False))


def rdir(cfg) -> Path:
    d = cfg.state / "rowadd"
    for sub in ("requests", "done"):
        (d / sub).mkdir(parents=True, exist_ok=True)
    return d


@contextmanager
def queue_lock(cfg):
    cfg.state.mkdir(parents=True, exist_ok=True)
    with open(cfg.state / "queues.lock", "a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def one_line(line: str) -> str:
    return "\t".join(re.split(r"\s+", line.strip()))


# -- who is asking ------------------------------------------------------------------------------
def powers(cfg, st, sid: str) -> Tuple[Optional[Dict[str, Any]], str]:
    """-> (the caller's powers, '') or (None, why it may not add rows)."""
    if not sid:
        return {"sid": "", "kind": "operator"}, ""
    from engine import kind_of
    kind = kind_of(sid)
    d = st.dispatch(sid)
    if not d.get("open") or not d.get("ident"):
        return None, f"{sid} is not a live session the engine dispatched"
    if kind == "planner":
        import rounds
        tag = str((d.get("snapshot") or {}).get("round") or d.get("round") or "")
        lanes = [str(x) for x in (rounds.load_rounds(cfg).get(tag) or {}).get("lanes", [])]
        return {"sid": sid, "kind": "planner", "round": tag, "lanes": lanes}, ""
    if kind == "row":
        import rearm
        grants = rearm.snapshot_grants(st, sid)
        if rearm.ROUND not in grants:
            return None, (f"row {sid} may not add rows: only a planner (pl-) or a course-loop row whose queue line "
                          f"carried rearm=…,round at dispatch may (`orch row add`); say so in PROGRESS.md and block")
        from rows import find_row, plan_dir
        import rounds
        me = find_row(cfg, sid)
        lanes = [me.lane] if me else []
        for r in rounds.load_rounds(cfg).values():   # rounds the row created itself (round.yaml `by`)
            if str(r.get("by", "")) == sid and r.get("status") != "closed":
                lanes += [str(x) for x in r.get("lanes", []) if str(x) not in lanes]
        allow = ([str(plan_dir(cfg, me))] if me and plan_dir(cfg, me) else []) + \
            ([str((cfg.workspace / x).resolve()) for x in me.extra_allow] if me else [])
        return {"sid": sid, "kind": "row", "grants": grants, "lanes": lanes, "allow": allow}, ""
    return None, f"{sid} is a {kind} session; only planners, course-loop rows (rearm=round) and the operator add rows"


# -- the checks ----------------------------------------------------------------------------------
def _gate_kind(cfg, r) -> str:
    import steward
    return str(r.meta.get("gate_kind") or steward.default_gate_kind(cfg))


def history(st, rid: str) -> str:
    """'' when the id has never been used on this rig, else what is left of it (a result, a claim, a
    dispatch record, events) — a 'new' row with an old id would inherit them (an accepted result = done)."""
    try:
        if st.result(rid):
            return "a result record"
        if st.status(rid)[0] != "NONE":
            return "a claim"
        if st.dispatch(rid):
            return "a dispatch record"
        if any(True for _ in st.events(rid)):
            return "events in attempts.jsonl"
    except AttributeError:          # a store without that surface (unit tests): nothing to inherit
        return ""
    return ""


def _under(path: Path, roots: List[str]) -> bool:
    p = str(path.resolve())
    return any(p == r or p.startswith(r.rstrip("/") + "/") for r in roots)


def phase_check(cfg, lane: str, row) -> str:
    """Drop D's phase fence and row cap: '' when the row may be added. Without lib/phases.py both pass;
    a phases.py that fails refuses (a broken fence is not an open one)."""
    if not PHASES.exists():
        return ""
    try:
        import phases
        why = phases.fence(cfg, [lane]) or ""
        if why:
            return f"the phase fence: {why}"
        cap = getattr(phases, "row_cap", None)
        return str(cap(cfg, lane, row) or "") if cap else ""
    except Exception as e:
        return f"the phase check failed ({type(e).__name__}: {e}) — refused, not passed"


def grant_problem(who: Dict[str, Any], rid: str, grants: List[str], by_id: Dict[str, Any]) -> str:
    """rearm= on a new row is never wider than the caller's own re-arm power."""
    import rearm
    if rid in grants:
        return f"rearm={rid} names the row itself (a row never re-arms itself)"
    if who["kind"] == "operator":
        return ""
    if who["kind"] == "row":
        if who["sid"] in grants:
            return f"rearm={who['sid']} would let the new row re-arm its caller, which {who['sid']} may not do itself"
        wider = [g for g in grants if g not in who["grants"]]
        if wider:
            return (f"rearm={','.join(wider)} is wider than {who['sid']}'s own grants "
                    f"({','.join(who['grants']) or 'none'}, from its dispatch snapshot)")
        return ""
    # planner: what `orch sw reset` lets it reset — rows of its own round's lanes; never `round`
    if rearm.ROUND in grants:
        return (f"rearm=round would let the row start any round; planner {who['sid']} may start only round "
                f"{who.get('round') or '?'} (`orch sw round-start`)")
    out = [g for g in grants if g not in by_id or by_id[g].lane not in who["lanes"]]
    if out:
        return (f"rearm={','.join(out)} names rows outside round {who.get('round') or '?'}'s lanes "
                f"({' '.join(who['lanes']) or 'none'}) — wider than planner {who['sid']}'s own power")
    return ""


def validate(cfg, st, who: Dict[str, Any], lane: str, line: str):
    """-> (ok, why, the parsed row). Shared by the CLI and the engine."""
    from rows import parse_line, load_rows, active_lanes, archived_lanes
    import rearm
    if not LANE_RE.fullmatch(lane or ""):
        return False, f"lane '{lane}' is not a lane name ([A-Za-z0-9][A-Za-z0-9_.-]*)", None
    if "\n" in line or "\r" in line:
        return False, "one queue line per `orch row add`", None
    s = line.strip()
    if not s or s.startswith("#"):
        return False, "the queue line is empty or a comment", None
    r = parse_line(s, lane, 0)
    if r.problems:
        return False, "; ".join(r.problems), r
    if lane in archived_lanes(cfg) and lane not in active_lanes(cfg):
        return False, f"lane {lane} is archived (archive/queue_{lane}.tsv); a new round takes a new lane", r
    if not ID_RE.fullmatch(r.id):
        return False, f"row id '{r.id}' is not an id ([A-Za-z0-9][A-Za-z0-9_.-]*)", r
    if r.id.startswith(SESSION_PREFIXES):
        return False, f"row id '{r.id}' begins with a session prefix ({' '.join(SESSION_PREFIXES)})", r
    if not (cfg.data.get("lanes") or {}).get("auto_discover", True) and lane not in cfg.lanes():
        return False, f"lane {lane} is not in lanes.order and lanes.auto_discover is off — the row would be invisible", r
    rows, by_id = load_rows(cfg)
    old = [x for x in rows if x.id == r.id]
    if old:
        o = old[0]
        if o.gate and (not r.gate or _gate_kind(cfg, o) != _gate_kind(cfg, r)):
            now = "no gate" if not r.gate else f"gate kind {_gate_kind(cfg, r)}"
            return False, (f"REFUSED gate {'removal' if not r.gate else 'change'}: {r.id} (lane {o.lane}) has a gate of "
                           f"kind {_gate_kind(cfg, o)}; this line gives it {now}. `orch row add` never removes or "
                           f"changes a gate — only the Owner edits an existing row"), r
        return False, (f"row {r.id} already exists (lane {o.lane}{', archived' if o.archived else ''}); "
                       f"`orch row add` adds new rows only — never reuse an id"), r
    left = history(st, r.id)
    if left:
        return False, f"id {r.id} was used on this rig before ({left} in state/) — never reuse an id", r
    for dep in r.deps:
        if dep == r.id:
            return False, f"{r.id} depends on itself", r
        if dep not in by_id:
            return False, f"dep '{dep}' is not a row (add it first)", r
    if r.queue_exec not in cfg.data["executors"]["allowed"]:
        return False, f"executor '{r.queue_exec}' is not in executors.allowed", r
    p = Path(r.plan)
    if p.is_absolute() or ".." in p.parts:
        return False, f"plan path '{r.plan}' must be relative to the workspace, without '..'", r
    if not (cfg.workspace / p).is_file():
        return False, f"plan file {r.plan} does not exist (relative to the workspace) — write the PLAN first", r
    pdir = (cfg.workspace / p).parent.resolve()
    inst = cfg.instance.resolve()
    above = str(inst).startswith(str(pdir).rstrip("/") + "/") and pdir != cfg.workspace.resolve()  # the top level has no folder
    if pdir == inst or above or str(pdir).startswith(str(inst) + "/"):
        return False, f"plan {r.plan} sits in or above the instance ({inst.name}/): its folder would be the row's allowlist", r
    if who["sid"]:
        # a twin of an existing row (same PLAN or folder) would route around that row's gate
        twin = [x.id for x in rows if x.plan and ((cfg.workspace / x.plan).resolve() == (cfg.workspace / p).resolve()
                                                 or (cfg.workspace / x.plan).parent.resolve() == pdir)]
        if twin:
            return False, (f"plan {r.plan} (or its folder) is already row {twin[0]}'s — a session cuts a new row in a "
                           f"folder of its own"), r
        if who["kind"] == "row":
            bad = [x for x in r.extra_allow if not _under(cfg.workspace / x, who.get("allow") or [])]
            if bad:
                return False, (f"extra allowlist path(s) {', '.join(bad)} lie outside {who['sid']}'s own allowlist — "
                               f"a row never grants more than it may stage"), r
    if "gate_kind" in r.meta:
        if r.meta["gate_kind"] not in GATE_KINDS:
            return False, f"gate_kind={r.meta['gate_kind']} is not one of {'|'.join(GATE_KINDS)}", r
        if not r.gate:
            return False, "gate_kind= on a row with no gate (gate column 0)", r
        if who["sid"] and r.meta["gate_kind"] == "steward":
            return False, (f"gate_kind=steward is a gate the Steward lane (and a planner) may release; a session never "
                           f"cuts a gate its own lane can pass — leave the default (owner) or ask the Owner"), r
    if "reviewer" in r.meta and r.meta["reviewer"] not in cfg.data["executors"]["allowed"]:
        return False, f"reviewer={r.meta['reviewer']} is not in executors.allowed", r
    if "reviewer" in r.meta and not cfg.decides(r.meta["reviewer"]):
        return False, f"reviewer={r.meta['reviewer']} is a junior tier — a reviewer needs a hard tier", r
    if "review" in r.meta:
        if r.meta["review"] not in ("0", "1"):
            return False, f"review={r.meta['review']} must be 0 or 1", r
        if who["sid"] and r.meta["review"] == "0":
            return False, "review=0 switches off the independent review; only the operator cuts such a row", r
    if "rearm" in r.meta:
        grants, bad = rearm.parse_grants(r.meta["rearm"])
        if bad:
            return False, f"rearm= has malformed token(s): {','.join(bad)}", r
        why = grant_problem(who, r.id, grants, by_id)
        if why:
            return False, why, r
        if who["sid"]:                 # the native course loop's judge is re-armed by the engine only (drop D)
            try:
                import courseloop
                loop_j = [g for g in grants if g in courseloop.final_judges(cfg)]
                # v3.1 (§15.2): a checkpoint judge may be granted (a replan row chains the next wave's replan row)
                # while it holds no negative outcome; no row accepted infeasible is ever granted
                owned = [g for g in grants if g != rearm.ROUND and courseloop.owns_negative(cfg, g, st)]
            except Exception:
                loop_j, owned = [], []
            if loop_j:
                return False, (f"rearm={','.join(loop_j)} names a course-loop judge — the engine re-arms it when the "
                               f"loop's correction rows are done; no session's row may"), r
            if owned:
                return False, (f"rearm={','.join(owned)}: {courseloop.owns_negative(cfg, owned[0], st)} — no session's "
                               f"row may be granted it"), r
    if who["kind"] == "planner" and lane not in who["lanes"]:
        return False, (f"planner {who['sid']} adds rows only to round {who.get('round') or '?'}'s lanes "
                       f"({' '.join(who['lanes']) or 'none'}) — `orch sw round-add-lanes` first"), r
    if who["kind"] == "row" and lane not in who["lanes"]:
        return False, (f"row {who['sid']} adds rows only to its own lane and the lanes of rounds it created "
                       f"({' '.join(who['lanes']) or 'none'})"), r
    fence = phase_check(cfg, lane, r)
    if fence:
        return False, fence, r
    return True, "", r


# -- the queue watch -----------------------------------------------------------------------------
def norm_lines(text: str) -> List[str]:
    out = []
    for line in text.splitlines():
        s = line.strip()
        if s and not s.startswith("#"):
            out.append(one_line(s))
    return out


def _sha(lines: List[str]) -> str:
    return hashlib.sha256("\n".join(lines).encode()).hexdigest()


def queue_file(cfg, lane: str) -> Path:
    return cfg.instance / f"queue_{lane}.tsv"


def current(cfg, lane: str) -> List[str]:
    try:
        return norm_lines(queue_file(cfg, lane).read_text(errors="replace"))
    except FileNotFoundError:
        return []


def load_base(cfg) -> Optional[Dict[str, str]]:
    try:
        return dict(json.loads((cfg.state / "queues.sha").read_text()).get("lanes") or {})
    except FileNotFoundError:
        return None
    except Exception:
        return {}           # a damaged record: every lane differs until `orch row sync`


def blessed(cfg, lane: str) -> List[str]:
    try:
        return (cfg.state / "queues" / f"{lane}.tsv").read_text().splitlines()
    except FileNotFoundError:
        return []


def bless(cfg, lane: str, lines: List[str]) -> None:
    base = load_base(cfg) or {}
    _atomic(cfg.state / "queues" / f"{lane}.tsv", "".join(l + "\n" for l in lines))
    base[lane] = _sha(lines)
    _atomic(cfg.state / "queues.sha", json.dumps({"lanes": base, "ts": time.strftime("%F %T")}, indent=1, sort_keys=True))


def unbless(cfg, lane: str) -> None:
    base = load_base(cfg) or {}
    base.pop(lane, None)
    (cfg.state / "queues" / f"{lane}.tsv").unlink(missing_ok=True)
    _atomic(cfg.state / "queues.sha", json.dumps({"lanes": base, "ts": time.strftime("%F %T")}, indent=1, sort_keys=True))


def keyed(cfg, lane: str, lines: List[str]) -> Dict[str, Any]:
    from rows import parse_line
    out = {}
    for l in lines:
        r = parse_line(l, lane, 0)
        if r:
            out.setdefault(r.id, (l, r))
    return out


def diff(cfg, lane: str, old: List[str], new: List[str]) -> Dict[str, Any]:
    a, b = keyed(cfg, lane, old), keyed(cfg, lane, new)
    added = [k for k in b if k not in a]
    removed = [k for k in a if k not in b]
    changed = [k for k in b if k in a and a[k][0] != b[k][0]]
    gates = []
    for k in changed:
        o, n = a[k][1], b[k][1]
        if o.gate and not n.gate:
            gates.append(f"{k}: gate removed")
        elif o.gate and _gate_kind(cfg, o) != _gate_kind(cfg, n):
            gates.append(f"{k}: gate kind {_gate_kind(cfg, o)} -> {_gate_kind(cfg, n)}")
    gates += [f"{k}: gated row removed" for k in removed if a[k][1].gate]
    # what the edit IS (not the file's content): a later `row add` to the same lane leaves it unchanged
    sig = hashlib.sha256(json.dumps([sorted(b[k][0] for k in added), sorted(a[k][0] for k in removed),
                                     sorted([a[k][0], b[k][0]] for k in changed), len(new) - len(b),
                                     [l for l in new if l in old] != [l for l in old if l in new]]).encode()).hexdigest()
    return {"added": added, "removed": removed, "changed": changed, "gates": gates,
            "dup": len(new) - len(b), "sig": sig}


def describe(d: Dict[str, Any]) -> str:
    if d["kind"] == "removed":
        return f"queue_{d['lane']}.tsv removed (not archived with the content the engine recorded)"
    parts = []
    for key, sign in (("added", "+"), ("removed", "-"), ("changed", "~")):
        if d[key]:
            ids = d[key]
            parts.append(f"{sign}{len(ids)} ({', '.join(ids[:8])}{' …' if len(ids) > 8 else ''})")
    if d.get("dup"):
        parts.append(f"{d['dup']} duplicate id line(s)")
    if d["gates"]:
        parts.append("GATES: " + "; ".join(d["gates"][:6]))
    if not parts:
        parts.append("line order changed")
    return f"queue_{d['lane']}.tsv " + " ".join(parts)


def differences(cfg) -> List[Dict[str, Any]]:
    """What the queues hold that the engine did not record (empty when the watch has no baseline)."""
    from rows import active_lanes, archive_dir
    base = load_base(cfg)
    if base is None:
        return []
    out = []
    act = active_lanes(cfg)
    for lane in act:
        cur = current(cfg, lane)
        if lane not in base:
            d = diff(cfg, lane, [], cur)
            d.update(lane=lane, kind="new", lines=cur, sha=_sha(cur))
            out.append(d)
        elif _sha(cur) != base[lane]:
            d = diff(cfg, lane, blessed(cfg, lane), cur)
            d.update(lane=lane, kind="edited", lines=cur, sha=_sha(cur))
            out.append(d)
    for lane in sorted(set(base) - set(act)):
        arch = archive_dir(cfg) / f"queue_{lane}.tsv"
        same = arch.is_file() and _sha(norm_lines(arch.read_text(errors="replace"))) == base[lane]
        out.append({"lane": lane, "kind": "archived" if same else "removed", "lines": [], "sha": "removed",
                    "sig": "removed", "added": [], "removed": [], "changed": [], "gates": []})
    return out


SETTLE = 30     # seconds a commit is given for the guard to record it as the operator's


def head_state(cfg, lane: str) -> Tuple[Optional[List[str]], str, float]:
    """(the lane's row lines at HEAD or None, the last commit that touched the file or '', its epoch)."""
    q = queue_file(cfg, lane)
    repo = cfg.git_repo
    try:
        rel = os.path.relpath(q.resolve(), repo.resolve())
    except Exception:
        return None, "", 0.0
    if rel.startswith(".."):
        return None, "", 0.0

    def git(*a: str) -> Tuple[int, str]:
        try:
            p = subprocess.run(["git", "-C", str(repo), "-c", "core.quotepath=false", *a], capture_output=True,
                               timeout=30)
        except Exception:
            return 1, ""
        return p.returncode, p.stdout.decode(errors="replace")

    rc, out = git("log", "-1", "--format=%H %ct", "--", rel)
    sha, _, ct = out.strip().partition(" ") if rc == 0 else ("", "", "")
    rc, text = git("show", f"HEAD:./{rel}")
    try:
        when = float(ct)
    except ValueError:
        when = 0.0
    return (norm_lines(text) if rc == 0 else None), sha, when


def operator_commit(cfg, lane: str, lines: List[str]) -> str:
    """The operator's shell: the lane's file equals HEAD and the last commit that touched it is an
    operator `plan:` commit (recorded by the guard in state/protected_commits.jsonl). -> its sha or ''."""
    import protected
    head, sha, _ = head_state(cfg, lane)
    return sha if sha and head == lines and sha in protected.operator_commits(cfg) else ""


def grace(cfg) -> float:
    try:
        return float((cfg.data.get("guard") or {}).get("queue_watch_grace", 600))
    except (TypeError, ValueError):
        return 600.0


def _clear(cfg) -> None:
    import shutil
    (cfg.state / "queues.sha").unlink(missing_ok=True)
    (cfg.state / "queues.reported.json").unlink(missing_ok=True)
    shutil.rmtree(cfg.state / "queues", ignore_errors=True)


def watch(rig) -> None:
    """Engine tick phase (guard.queue_watch): report queue edits made outside `orch row add`.

    A difference is reported once per edit (its signature). An uncommitted edit that touches no gate
    waits guard.queue_watch_grace seconds from the moment the lane first differed — the operator's own
    hand edit is blessed by an operator `plan:` commit within that time; a gate removal or change, a
    committed edit and a removed lane are reported at once. A lane that matches the record again forgets its report, so
    the same edit made again is reported again. With the watch off the record is dropped: turning
    it on again takes a fresh baseline."""
    cfg, st = rig.cfg, rig.st
    if not watch_on(cfg):
        if (cfg.state / "queues.sha").exists():
            with queue_lock(cfg):
                _clear(cfg)
            st.engine_log("queue watch off: state/queues.sha dropped (a fresh baseline when it is turned on)")
        return
    from rows import active_lanes
    with queue_lock(cfg):
        if load_base(cfg) is None:
            lanes = active_lanes(cfg)
            for lane in lanes:
                bless(cfg, lane, current(cfg, lane))
            st.event("queues", "queue_baseline", lanes=lanes)
            st.engine_log(f"queue watch: baseline of {len(lanes)} lane(s) in state/queues.sha")
            return
        rf = cfg.state / "queues.reported.json"
        try:
            rep = dict(json.loads(rf.read_text()))
        except Exception:
            rep = {}
        seen = set()
        now = time.time()
        for d in differences(cfg):
            lane = d["lane"]
            if d["kind"] == "archived":
                unbless(cfg, lane)
                continue
            if d["kind"] == "new" and not d["lines"]:
                bless(cfg, lane, [])
                continue
            uncommitted = False
            if d["kind"] != "removed":
                import protected
                head, sha, when = head_state(cfg, lane)
                if sha and head == d["lines"] and sha in protected.operator_commits(cfg):
                    bless(cfg, lane, d["lines"])
                    st.event(f"queue:{lane}", "queue_blessed", by="operator commit", commit=sha[:12])
                    continue
                # a commit seconds old may be the operator's whose record the guard is still writing
                uncommitted = head != d["lines"] or now - when < SETTLE
            seen.add(lane)
            # the grace is for the operator's edit on its way to a plan: commit only: a gate removed or
            # changed, a committed edit (a session's commit) and a removed lane are reported at once; the
            # clock starts when the lane first differs and never restarts while it keeps differing
            e = rep.get(lane) if isinstance(rep.get(lane), dict) else None
            if not e or not isinstance(e.get("sigs"), list):
                e = rep[lane] = {"since": now, "sigs": []}
            due = bool(d["gates"]) or not uncommitted or now - float(e.get("since", now)) >= grace(cfg)
            if d["sig"] not in e["sigs"] and due:
                text = describe(d)
                st.event(f"queue:{lane}", "queue_edited", lane=lane, summary=text, added=d["added"],
                         removed=d["removed"], changed=d["changed"], gates=d["gates"])
                rig.log(lane, f"QUEUE EDITED OUTSIDE orch row add — {text} (orch row diff; the operator accepts "
                              f"it with orch row sync)")
                e["sigs"] = (e["sigs"] + [d["sig"]])[-50:]
        rep = {k: v for k, v in rep.items() if k in seen}
        _atomic(rf, json.dumps(rep, indent=1, sort_keys=True))


def digest_lines(cfg, events: List[Dict[str, Any]]) -> List[str]:
    edits = [e for e in events if e.get("event") == "queue_edited"]
    still = [d for d in differences(cfg) if d["kind"] != "archived"] if watch_on(cfg) else []
    if not edits and not still:
        return []
    L = ["", "## Queue edited outside orch row add", ""]
    L += [f"- {e['ts'][5:16]} queue edited outside orch row add: {e.get('summary', '')}" for e in edits[-20:]]
    if still:
        L.append(f"- still differs now: {'; '.join(describe(d) for d in still[:6])} — `orch row diff`, "
                 f"accept with `orch row sync`")
    return L


# -- adding -------------------------------------------------------------------------------------
def add(cfg, st, who: Dict[str, Any], lane: str, line: str, why: str) -> Tuple[bool, str]:
    """Validate and append under the queue lock (the engine for a session, the CLI for the operator)."""
    by = who["sid"] or "operator"
    with queue_lock(cfg):
        ok, msg, r = validate(cfg, st, who, lane, line)
        if not ok:
            st.event(f"queue:{lane}", "row_add_refused", by=by, line=one_line(line)[:300], detail=msg[:300])
            st.engine_log(f"row add REFUSED ({by}, lane {lane}) — {msg}")
            return False, msg
        text = one_line(line)
        q = queue_file(cfg, lane)
        watching = watch_on(cfg) and load_base(cfg) is not None
        old_blessed = blessed(cfg, lane)
        if not q.exists():
            q.write_text(HEADER.format(lane=lane))
        pre = q.read_text(errors="replace")
        with open(q, "a") as fh:                 # appended in place (a symlinked queue stays a symlink)
            fh.write(("" if not pre or pre.endswith("\n") else "\n") + text + "\n")
        if watching:
            # only this line is blessed: an earlier hand edit of the lane stays reported
            bless(cfg, lane, old_blessed + [text])
        st.event(r.id, "row_add", lane=lane, by=by, why=" ".join(why.split())[:300])
        st.lane_log(lane, f"ROW ADD {r.id} by {by} — {' '.join(why.split())[:200]}")
        st.engine_log(f"[{lane}] ROW ADD {r.id} by {by}")
    return True, f"{r.id} appended to queue_{lane}.tsv"


def request(cfg, sid: str, lane: str, line: str, why: str) -> Path:
    import capability
    rec = {"session": sid, "lane": lane, "line": line, "why": " ".join(why.split()), "epoch": time.time(),
           "ts": time.strftime("%F %T"), "pid": os.getpid(), "start": capability.started(os.getpid())}
    f = rdir(cfg) / "requests" / f"{sid}.{int(time.time() * 1000)}.json"
    _atomic(f, json.dumps(rec, indent=1, sort_keys=True))
    return f


def wait_result(req: Path, timeout: float = 120) -> Dict[str, Any]:
    done = req.parent.parent / "done"
    end = time.time() + timeout
    while time.time() < end:
        for suffix in ("done", "refused"):
            f = done / f"{req.stem}.{suffix}.json"
            if f.exists():
                try:
                    return json.loads(f.read_text())
                except Exception:
                    pass
        time.sleep(1)
    return {"outcome": "timeout", "why": f"the engine did not answer within {int(timeout)}s — is it running? "
                                          f"The request stays queued: {req.name}"}


def _argv(pid: int) -> List[str]:
    try:
        return [a.decode(errors="replace") for a in Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")]
    except Exception:
        return []


def proof(cfg, rec: Dict[str, Any]) -> str:
    """'' when the request was written by an `orch row add` process that still runs inside the named
    session's pane (it waits for the answer); else why not. A request file is plain JSON any same-uid
    process can write — without this, one session could ask in another's name. With the capability
    check off (guard.capability_check: false) or no /proc, the v2 rule applies: the name decides."""
    import capability
    if not capability.enabled(cfg) or not Path("/proc/self/status").exists():
        return ""
    sid = str(rec.get("session", ""))
    try:
        pid, start = int(rec.get("pid")), float(rec.get("start"))
    except (TypeError, ValueError):
        return "the request names no caller process (only `orch row add` writes a request)"
    if pid <= 1 or abs(capability.started(pid) - start) > 0.5:
        return "the process that wrote the request is gone (or its pid was reused)"
    argv = _argv(pid)
    at = next((i for i, a in enumerate(argv) if a.endswith("rowadd.py")), -1)
    if at < 0 or argv[at + 1:at + 4] != ["add", str(rec.get("lane", "")), str(rec.get("line", ""))]:
        return "the process that wrote the request is not an `orch row add` of that line"
    s = capability.inside(cfg, pid)
    if not s or s.get("sid") != sid:
        return f"the process that wrote the request runs in {s.get('sid') if s else 'no'} session's pane, not {sid}'s"
    return ""


def consume(rig) -> None:
    """Engine tick phase: every pending session request, validated again here. One bad request never
    stops the others: anything that fails is answered REFUSED and moved out of the queue."""
    cfg, st = rig.cfg, rig.st
    reqd = cfg.state / "rowadd" / "requests"
    if not reqd.is_dir():
        return
    for f in sorted(reqd.glob("*.json")):
        rec: Dict[str, Any] = {}
        try:
            raw = json.loads(f.read_text())
            rec = raw if isinstance(raw, dict) else {}
            sid = str(rec.get("session", ""))
            if not rec or not sid or not f.name.startswith(f"{sid}."):
                ok, why = False, "malformed request, or its file name does not match its session"
            else:
                why = proof(cfg, rec)
                who = None
                if not why:
                    who, why = powers(cfg, st, sid)
                if who is not None:
                    ok, why = add(cfg, st, who, str(rec.get("lane", "")), str(rec.get("line", "")),
                                  str(rec.get("why", "")))
                else:
                    ok = False
                    st.event(f"queue:{rec.get('lane', '-')}", "row_add_refused", by=sid, detail=why[:300])
                    st.engine_log(f"row add REFUSED ({sid}) — {why}")
        except Exception as e:
            ok, why = False, f"request could not be handled ({type(e).__name__}: {e})"
        out = dict(rec, outcome="done" if ok else "refused", why=why, handled=time.strftime("%F %T"))
        try:
            _atomic(cfg.state / "rowadd" / "done" / f"{f.stem}.{out['outcome']}.json",
                    json.dumps(out, indent=1, sort_keys=True, default=str))
        except Exception:
            pass
        try:
            if f.is_dir():
                os.replace(f, cfg.state / "rowadd" / "done" / f"{f.name}.{int(time.time())}.refused")
            else:
                f.unlink(missing_ok=True)
        except Exception as e:
            st.engine_log(f"row add: could not move request {f.name} out of the queue ({e})")


# -- CLI --------------------------------------------------------------------------------------------
USAGE = ('usage: orch row add <lane> "<id deps gate executor plan [extras]>" "why"\n'
         '       orch row diff                 what the queues hold that the engine did not record\n'
         '       orch row sync ["why"]         the operator accepts the queues as they are now')


def main(argv: List[str]) -> int:
    from config_parser import load
    from store import Store
    import capability
    cfg = load()
    st = Store(cfg.state)
    sid = capability.session(cfg)        # the pane a caller descends from, else ORCH_ROW (GDV3-2)
    sub = argv[0] if argv else ""
    if sub == "add":
        if len(argv) < 4 or not argv[3].strip():
            print(USAGE)
            return 2
        lane, line, why = argv[1], argv[2], " ".join(argv[3:])
        if not sid:
            ok, msg = add(cfg, st, {"sid": "", "kind": "operator"}, lane, line, why)
            print(f"{'DONE' if ok else 'REFUSED'} row add: {msg}")
            return 0 if ok else 1
        who, why_not = powers(cfg, st, sid)
        if who is None:
            print(f"REFUSED row add: {why_not}")
            return 1
        ok, msg, _ = validate(cfg, st, who, lane, line)        # a quick answer; the engine checks again
        if not ok:
            print(f"REFUSED row add: {msg}")
            return 1
        res = wait_result(request(cfg, sid, lane, line, why))
        out = res.get("outcome")
        print(f"{'DONE' if out == 'done' else 'REFUSED' if out == 'refused' else 'NO ANSWER'} row add: {res.get('why', '')}")
        return 0 if out == "done" else 1 if out == "refused" else 4
    if sub == "diff":
        if not watch_on(cfg):
            print("queue watch is off (guard.queue_watch: false) — nothing is recorded")
            return 0
        ds = [d for d in differences(cfg) if d["kind"] != "archived"]
        if load_base(cfg) is None:
            print("queue watch: no baseline yet (the engine takes one on its next tick)")
            return 0
        for d in ds:
            print(describe(d))
        if not ds:
            print("every queue matches the engine's record (state/queues.sha)")
        return 1 if ds else 0
    if sub == "sync":
        if sid:
            print(f"REFUSED: `orch row sync` is the operator's command (session {sid}); a session adds rows with "
                  f"`orch row add`")
            return 1
        from rows import active_lanes
        why = " ".join(argv[1:]) or "accepted by the operator"
        with queue_lock(cfg):
            ds = differences(cfg)
            for d in ds:
                if d["kind"] in ("archived", "removed"):
                    unbless(cfg, d["lane"])
            for lane in active_lanes(cfg):
                bless(cfg, lane, current(cfg, lane))
            st.event("queues", "queue_sync", by="operator", why=why[:300],
                     lanes=[d["lane"] for d in ds if d["kind"] != "archived"])
        print(f"queues accepted as they are ({len(ds)} difference(s) cleared) — state/queues.sha")
        return 0
    print(USAGE)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
