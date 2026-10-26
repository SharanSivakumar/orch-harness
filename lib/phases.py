#!/usr/bin/env python3
"""The phase fence (v3, drop D item 1): the rig runs only inside a phase the Owner opened.

Opening a phase is the Owner's. No Steward, planner or row creates, opens or starts a
phase; at a phase's cap the phase rests and nothing new starts in it.

    <instance>/phases.yaml   the Owner's readable view (a protected path):
        phases:
          P1: {state: open|rest|closed, opened: "…", caps: {rows: 40, rulings: 10, days: 5, loops: 3, replans: 4},
               rounds: [p1, p1d], loops: 0, replans: 0, session_loops: false}
    state/phases.json        the record the fence trusts: the phases + the sha of the phases.yaml orch wrote

    orch phase open <P> [--caps rows=N,rulings=N,days=N,loops=N,replans=N] [--rounds a,b]
                        [--session-loops on|off] ["why"]                                   (operator only)
    orch phase rest|close <P> ["why"]                                                      (operator only)
    orch phase approve        adopt a hand edit of phases.yaml (prints what it changes)    (operator only)
    orch phase off            remove the fence (phases.yaml, the record, the marker)      (operator only)
    orch phase status

Only `orch phase` and the engine write phases.yaml, always together with the record. A phases.yaml that
differs from the recorded sha (a hand edit, a session) is IGNORED — the record stays in force — and is
reported once per content (event phases_tamper, a normal notification, the digest) until the Owner runs
`orch phase approve` or `orch phase …` rewrites it. No phases.yaml and no record: no fence (a v2 rig).
Deleting phases.yaml while the record exists keeps the fence.

The fence, when on:
  * fence(cfg, lanes)                      '' when every lane's round belongs to an OPEN phase;
  * fence(cfg, lanes, new_round=True, phase=P)  '' when P is open and below its caps (a new round names its
                                           phase: `orch round new <tag> --phase P`, `orch sw round-new <tag>
                                           --phase P …`); the round is recorded in P (add_round);
  * row_cap(cfg, lane, row)               '' while the lane's phase holds fewer rows than caps.rows
                                           (`orch row add`);
  * Fence(cfg).block(lane, row_phase)     the scheduler: open → ''; rest → a row not yet started ('' phase)
                                           waits, a retry finishes; closed → nothing; no round / no phase → waits.
  * tick(rig)                              at a cap — rows started ≥ caps.rows, Steward rulings since the
                                           latest open ≥ caps.rulings, days since the latest open ≥
                                           caps.days × 1.5, loops > caps.loops — the phase goes to rest
                                           (event phase_rest, a normal notification, the digest).
                                           caps.loops is the number of course loops the phase ALLOWS: the loop
                                           that brings the count to the cap still runs; the native course loop
                                           (lib/courseloop.py) rests the phase when its judge fails once more.
                                           v3.1 (docs/V3_DESIGN.md §15): with caps.replans set, a checkpoint
                                           judge's loops (round.yaml `judges:`) count in `replans` instead
                                           (same rule); without it every loop counts in `loops`.
  * session_loops: true                   (the Owner's, `--session-loops on`) a round a session created (`by:`)
                                           may loop in this phase, always within caps.loops (§15.5).
Nothing here ever opens a phase: only `orch phase open|approve` from the operator's shell does.

Known limits (docs/V3_DESIGN.md §12): same-uid state files (a session that writes state/phases.json
itself is the review's and the close checks' to catch); rulings are counted rig-wide since the latest open.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))

STATES = ("open", "rest", "closed")
CAP_KEYS = ("rows", "rulings", "days", "loops", "replans")
LOOP_COUNTERS = ("loops", "replans")      # counted as a loop STARTS: the cap counts loops allowed (`>` rests)
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,40}$")
DAYS_FACTOR = 1.5
HEADER = ("# phases.yaml — the rig's phases (orch v3, drop D). Written by `orch phase` and the engine; the fence\n"
          "# trusts state/phases.json. Opening a phase is the Owner's: `orch phase open|rest|close`, or edit this\n"
          "# file and run `orch phase approve` (an edit without it is ignored and reported). A protected path.\n")


# -- files ---------------------------------------------------------------------------------------
def yaml_path(cfg) -> Path:
    return cfg.instance / "phases.yaml"


def rec_path(cfg) -> Path:
    return cfg.state / "phases.json"


def marker_path(cfg) -> Path:
    return cfg.state / "phases.on"


def active(cfg) -> bool:
    """The fence is on when the rig has phases: phases.yaml, the record, or the marker orch leaves at every write
    (deleting both files does not turn it off; `orch phase off` — the Owner's — removes all three)."""
    return yaml_path(cfg).is_file() or rec_path(cfg).is_file() or marker_path(cfg).is_file()


def _sha_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _sha_file(p: Path) -> str:
    try:
        return _sha_bytes(p.read_bytes())
    except FileNotFoundError:
        return ""


def _atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def _record(cfg) -> Dict[str, Any]:
    try:
        v = json.loads(rec_path(cfg).read_text())
        return v if isinstance(v, dict) else {}
    except Exception:
        return {}


class _Lock:
    def __init__(self, cfg):
        cfg.state.mkdir(parents=True, exist_ok=True)
        self.f = open(cfg.state / "phases.lock", "a")

    def __enter__(self):
        fcntl.flock(self.f, fcntl.LOCK_EX)
        return self

    def __exit__(self, *a):
        fcntl.flock(self.f, fcntl.LOCK_UN)
        self.f.close()


# -- shape ---------------------------------------------------------------------------------------
def validate(data: Any) -> Tuple[Dict[str, Dict[str, Any]], List[str]]:
    """-> (phases, problems). Unknown per-phase keys are kept (opened, rested, rest_reason …)."""
    probs: List[str] = []
    if data is None:
        return {}, []
    if not isinstance(data, dict):
        return {}, ["phases.yaml must be a mapping with a `phases:` key"]
    raw = data.get("phases") or {}
    if not isinstance(raw, dict):
        return {}, ["`phases:` must map a phase id to its record"]
    out: Dict[str, Dict[str, Any]] = {}
    seen_rounds: Dict[str, str] = {}
    for pid, ph in raw.items():
        pid = str(pid)
        if not ID_RE.match(pid):
            probs.append(f"phase id '{pid}' is not [A-Za-z0-9][A-Za-z0-9_.-]*")
            continue
        ph = dict(ph or {})
        st = str(ph.get("state", "")).strip().lower()
        if st not in STATES:
            probs.append(f"{pid}: state '{ph.get('state')}' is not one of {'|'.join(STATES)}")
            continue
        ph["state"] = st
        caps = ph.get("caps") or {}
        if not isinstance(caps, dict):
            probs.append(f"{pid}: caps must be a mapping")
            continue
        good = {}
        for k, v in caps.items():
            if k not in CAP_KEYS:
                probs.append(f"{pid}: unknown cap '{k}' ({', '.join(CAP_KEYS)})")
                continue
            if v is None or v == "":
                continue
            try:
                n = float(v) if k == "days" else int(v)
            except (TypeError, ValueError):
                probs.append(f"{pid}: cap {k}={v!r} is not a number")
                continue
            if n < 0 or (k != "days" and float(v) != int(v)):
                probs.append(f"{pid}: cap {k}={v!r} must be a whole number ≥ 0" if k != "days" else
                             f"{pid}: cap days={v!r} must be ≥ 0")
                continue
            good[k] = n
        ph["caps"] = good
        rounds = ph.get("rounds") or []
        if not isinstance(rounds, list):
            probs.append(f"{pid}: rounds must be a list of round tags")
            continue
        ph["rounds"] = [str(r) for r in rounds]
        for r in ph["rounds"]:
            if r in seen_rounds and seen_rounds[r] != pid:
                probs.append(f"round {r} is in two phases ({seen_rounds[r]}, {pid}) — a round belongs to one phase")
            seen_rounds[r] = pid
        bad_count = ""
        for k in LOOP_COUNTERS:
            if k != "loops" and k not in ph:
                continue                 # v3.1 keys appear only once used: a v3 phases.yaml reads back unchanged
            try:
                ph[k] = int(ph.get(k, 0) or 0)
            except (TypeError, ValueError):
                bad_count = f"{pid}: {k} must be a whole number"
        if bad_count:
            probs.append(bad_count)
            continue
        if "session_loops" in ph and not isinstance(ph["session_loops"], bool):
            probs.append(f"{pid}: session_loops must be true or false")
            continue
        out[pid] = ph
    return out, probs


def _dump(phases: Dict[str, Dict[str, Any]]) -> str:
    import yaml
    clean = {pid: {k: v for k, v in ph.items() if not k.endswith("_epoch")} for pid, ph in phases.items()}
    return HEADER + yaml.safe_dump({"phases": clean}, sort_keys=False, allow_unicode=True)


def load(cfg) -> Dict[str, Dict[str, Any]]:
    """The phases the fence trusts (the record). {} when the rig has none — or none was ever approved."""
    ph, _ = validate({"phases": _record(cfg).get("phases") or {}})
    return ph


def tamper(cfg) -> str:
    """'' when phases.yaml is what orch last wrote; else what differs (the record stays in force)."""
    if not active(cfg):
        return ""
    rec = _record(cfg)
    cur = _sha_file(yaml_path(cfg))
    if not rec and marker_path(cfg).is_file():
        return ("state/phases.json was removed outside `orch phase` — the fence stays on and no phase is open "
                "(`orch phase open` or `orch phase approve` restores it; `orch phase off` turns the fence off)")
    if not rec:
        return ("phases.yaml was never approved — no phase is open until the Owner runs `orch phase approve` "
                "(or `orch phase open`)")
    if cur != str(rec.get("yaml_sha", "")):
        return ("phases.yaml " + ("removed" if not cur else "edited") + " outside `orch phase` — ignored; the "
                "recorded phases stay in force (`orch phase approve` adopts an Owner's edit)")
    return ""


def save(cfg, phases: Dict[str, Dict[str, Any]], by: str, what: str) -> None:
    """Write the record and phases.yaml together (callers hold _Lock)."""
    text = _dump(phases)
    rec = _record(cfg)
    hist = list(rec.get("history") or [])[-40:]
    hist.append({"ts": time.strftime("%F %T"), "by": by, "what": what[:300]})
    sha = _sha_bytes(text.encode())
    _atomic(yaml_path(cfg), text)        # the view first: a crash before the record leaves an edit to approve, not a stale view
    _atomic(rec_path(cfg), json.dumps({"phases": phases, "yaml_sha": sha, "history": hist,
                                       "written": time.strftime("%F %T"), "by": by}, indent=1, sort_keys=True))
    _atomic(marker_path(cfg), time.strftime("%F %T\n"))
    try:
        import protected
        protected.note_write(cfg, yaml_path(cfg), sha)
    except Exception:
        pass


# -- the map: lane -> round -> phase -------------------------------------------------------------
class Fence:
    """One read of the phases and the rounds' lanes (the scheduler builds one per pass)."""

    def __init__(self, cfg, phases: Optional[Dict[str, Dict[str, Any]]] = None, on: Optional[bool] = None):
        self.cfg = cfg
        self.on = active(cfg) if on is None else on
        self.error = ""
        self.phases = load(cfg) if phases is None else phases
        self.round_phase: Dict[str, str] = {}
        for pid, ph in self.phases.items():
            for r in ph.get("rounds") or []:
                self.round_phase[str(r)] = pid
        self.lane_rounds: Dict[str, List[str]] = {}
        self._started: Dict[str, int] = {}
        if self.on:
            self._map_lanes()

    @classmethod
    def failed(cls, cfg, why: str) -> "Fence":
        """A fence that could not be built: every row waits (fail closed), the scheduler goes on."""
        f = cls.__new__(cls)
        f.cfg, f.on, f.error, f.phases, f.round_phase, f.lane_rounds, f._started = cfg, True, why, {}, {}, {}, {}
        return f

    def _map_lanes(self) -> None:
        """lane -> every round that lists it: by folder name AND by the file's own tag (a round.yaml cannot
        borrow another round's tag); a malformed file maps no lane."""
        import yaml
        d = self.cfg.instance / "rounds"
        for f in sorted(d.glob("*/round.yaml")) if d.is_dir() else []:
            try:
                data = yaml.safe_load(f.read_text()) or {}
                if not isinstance(data, dict):
                    continue
                lanes = data.get("lanes") or []
                if not isinstance(lanes, list):
                    continue
                tags = [f.parent.name] + ([str(data["tag"])] if str(data.get("tag") or "") not in ("", f.parent.name) else [])
            except Exception:
                continue
            for l in lanes:
                lst = self.lane_rounds.setdefault(str(l), [])
                lst += [t for t in tags if t not in lst]

    def of_lane(self, lane: str) -> Tuple[str, str]:
        """-> (round tag, phase id) of a lane; '' for either that is missing or ambiguous."""
        tags = self.lane_rounds.get(lane, [])
        pids = {self.round_phase.get(t, "") for t in tags}
        if not tags or len(pids) != 1:
            return ",".join(tags), ""
        return tags[0], pids.pop()

    def lane_problem(self, lane: str) -> str:
        """'' when the lane's round is in an open phase."""
        if self.error:
            return f"the phase fence could not be read ({self.error}) — nothing dispatches until it can"
        tags = self.lane_rounds.get(lane, [])
        if not tags:
            return (f"lane {lane} belongs to no round, so to no phase — this rig runs only inside a phase the Owner "
                    f"opened (`orch phase status`)")
        pids = sorted({self.round_phase.get(t, "") or "no phase" for t in tags})
        if len(pids) > 1:
            return (f"lane {lane} is listed by rounds {', '.join(tags)} in different phases ({', '.join(pids)}) — "
                    f"it waits until one round lists it")
        tag, pid = self.of_lane(lane)
        if not pid:
            return (f"round {tag} belongs to no phase — only the Owner adds it to one "
                    f"(`orch phase open <P> --rounds {tag}`)")
        st = self.phases[pid]["state"]
        if st != "open":
            return f"phase {pid} is {st}{_why_rest(self.phases[pid])} — only the Owner re-opens it (`orch phase open {pid}`)"
        return ""

    def started(self, pid: str) -> int:
        if pid not in self._started:
            from store import Store
            st = Store(self.cfg.state)
            self._started[pid] = rows_started(self.cfg, pid, self.phases.get(pid) or {}, st, self)
        return self._started[pid]

    def at_rows_cap(self, lane: str) -> str:
        """'' while one more row of the lane's phase may START (the cap counts within one scheduler pass too)."""
        tag, pid = self.of_lane(lane)
        cap = (self.phases.get(pid, {}).get("caps") or {}).get("rows") if pid else None
        if cap is None or self.started(pid) < int(cap):
            return ""
        return (f"PHASE — phase {pid} is at its rows cap ({self.started(pid)}/{int(cap)} rows started) — it rests on "
                f"the engine's next tick; only the Owner raises it")

    def note_start(self, lane: str) -> None:
        tag, pid = self.of_lane(lane)
        if pid:
            self._started[pid] = self.started(pid) + 1

    def block(self, lane: str, row_phase: str, new_start: bool = False) -> str:
        """The scheduler's check for one row: '' when it may dispatch. new_start: the row has never been dispatched."""
        if not self.on:
            return ""
        tag, pid = self.of_lane(lane)
        if pid and not self.error and self.phases[pid]["state"] == "rest" and row_phase == "retry":
            return ""        # running rows finish
        why = self.lane_problem(lane)
        if why:
            return f"PHASE — {why}"
        return self.at_rows_cap(lane) if new_start else ""


def _why_rest(ph: Dict[str, Any]) -> str:
    r = str(ph.get("rest_reason") or ph.get("close_reason") or "")
    return f" ({r})" if r else ""


def fence(cfg, lanes: List[str], new_round: bool = False, phase: str = "", tag: str = "") -> str:
    """'' when the lanes may run (or, new_round, a new round `tag` with these lanes may be created in `phase`).
    No phases: ''."""
    if not active(cfg):
        if new_round and phase:
            return f"this rig has no phases (no phases.yaml) — drop --phase {phase}"
        return ""
    f = Fence(cfg)
    if new_round:
        open_ = [p for p, ph in f.phases.items() if ph["state"] == "open"]
        if not phase:
            return (f"a new round must name its phase (--phase <P>); open phase(s): {', '.join(open_) or 'none'} — "
                    f"only the Owner opens one")
        if phase not in f.phases:
            return f"no phase {phase} — only the Owner opens a phase (`orch phase open {phase}`)"
        ph = f.phases[phase]
        if ph["state"] != "open":
            return f"phase {phase} is {ph['state']}{_why_rest(ph)} — no new round in it; only the Owner re-opens it"
        cap = cap_reached(cfg, phase, ph)
        if cap:
            return f"phase {phase} is at its {cap} — no new round in it; it rests on the engine's next tick"
        if tag and f.round_phase.get(tag, phase) != phase:
            return f"round {tag} is already in phase {f.round_phase[tag]} — a round belongs to one phase"
        for l in lanes:
            other = sorted({f.round_phase.get(t, "") or "no phase" for t in f.lane_rounds.get(l, []) if t != tag}
                           - {phase})
            if other:
                return (f"lane {l} is already listed by a round in {', '.join(other)} — a lane of another phase is "
                        f"not taken into {phase}")
        return ""
    for l in lanes:
        why = f.lane_problem(l)
        if why:
            return why
    return ""


def round_fence(cfg, tag: str) -> str:
    """'' when round `tag` may start (its phase is open and below its caps)."""
    if not active(cfg):
        return ""
    f = Fence(cfg)
    pid = f.round_phase.get(tag, "")
    if not pid:
        return f"round {tag} belongs to no phase — only the Owner adds it to one (`orch phase open <P> --rounds {tag}`)"
    ph = f.phases[pid]
    if ph["state"] != "open":
        return f"phase {pid} is {ph['state']}{_why_rest(ph)} — round {tag} does not start; only the Owner re-opens it"
    cap = cap_reached(cfg, pid, ph)
    if cap:
        return f"phase {pid} is at its {cap} — round {tag} does not start"
    for l, tags in f.lane_rounds.items():
        if tag in tags:
            why = f.lane_problem(l)
            if why:
                return why
    return ""


def plan_fence(cfg, tag: str) -> str:
    """The Steward's `plan <tag>`: '' when a planner may plan round `tag` — its phase (an existing round's) or,
    for a new round, some phase is open and below its caps. A planner never plans outside an open phase."""
    if not active(cfg):
        return ""
    f = Fence(cfg)
    if tag in f.round_phase:
        return round_fence(cfg, tag)
    if not any(ph["state"] == "open" and not cap_reached(cfg, pid, ph, f=f) for pid, ph in f.phases.items()):
        return "no phase is open below its caps — nothing is planned until the Owner opens one (`orch phase open`)"
    return ""


def phase_rows(cfg, pid: str, f: Optional[Fence] = None) -> List[Any]:
    """The queue rows (archived too) of every lane of the phase's rounds."""
    from rows import load_rows
    f = f or Fence(cfg)
    rows, _ = load_rows(cfg)
    return [r for r in rows if f.of_lane(r.lane)[1] == pid]


def _rows_since(ph: Dict[str, Any]) -> float:
    """The rows cap counts from the phase's FIRST open (`rows_since`, kept across rest and re-open, so a
    re-open does not reset it); a record without it (written before vD3) counts every row of the phase."""
    try:
        return float(ph.get("rows_since") or 0)
    except (TypeError, ValueError):
        return 0.0


def _started_since(st, rid: str, since: float) -> bool:
    d = st.dispatch(rid)
    if not d:
        return False
    try:
        return float(d.get("epoch", 0) or 0) >= since if since else True
    except (TypeError, ValueError):
        return True


def rows_started(cfg, pid: str, ph: Dict[str, Any], st=None, f: Optional[Fence] = None) -> int:
    """Rows of the phase dispatched since `rows_since` (rows that ran before the phase existed are not its)."""
    if st is None:
        from store import Store
        st = Store(cfg.state)
    since = _rows_since(ph)
    return sum(1 for r in phase_rows(cfg, pid, f) if _started_since(st, r.id, since))


def row_cap(cfg, lane: str, row: Any = None) -> str:
    """`orch row add`: '' while the lane's phase holds fewer rows than its rows cap."""
    if not active(cfg):
        return ""
    f = Fence(cfg)
    tag, pid = f.of_lane(lane)
    if not pid:
        return ""            # fence() already refused it
    cap = (f.phases[pid].get("caps") or {}).get("rows")
    if cap is None:
        return ""
    from store import Store
    st, since = Store(cfg.state), _rows_since(f.phases[pid])
    # rows the cap holds: not yet dispatched, or dispatched since rows_since (a row that ran before the phase
    # was first opened is not the phase's — it counts again only if it is dispatched again)
    n = sum(1 for r in phase_rows(cfg, pid, f) if not st.dispatch(r.id) or _started_since(st, r.id, since))
    if n + 1 > int(cap):
        return (f"phase {pid} already holds {n} row(s), its rows cap is {int(cap)} — a row past the cap could never "
                f"run; only the Owner raises it (`orch phase open {pid} --caps rows=…`)")
    return ""


# -- caps ----------------------------------------------------------------------------------------
def usage(cfg, pid: str, ph: Dict[str, Any], now: Optional[float] = None, f: Optional[Fence] = None) -> Dict[str, Any]:
    """{rows: started rows, rulings: Steward rulings since the latest open, days: since the latest open, loops,
    replans}."""
    from store import Store
    now = time.time() if now is None else now
    st = Store(cfg.state)
    started = rows_started(cfg, pid, ph, st, f)
    since = float(ph.get("opened_epoch") or 0)
    if not since and ph.get("opened"):
        try:
            since = time.mktime(time.strptime(str(ph["opened"]), "%Y-%m-%d %H:%M:%S"))
        except Exception:
            since = 0.0
    n_rul = 0
    try:
        for line in (cfg.state / "steward" / "rulings.jsonl").read_text().splitlines():
            try:
                if float(json.loads(line).get("epoch", 0) or 0) >= since:
                    n_rul += 1
            except Exception:
                continue
    except FileNotFoundError:
        pass
    days = (now - since) / 86400.0 if since else 0.0
    return {"rows": started, "rulings": n_rul, "days": round(days, 3), "loops": int(ph.get("loops", 0) or 0),
            "replans": int(ph.get("replans", 0) or 0)}


def cap_reached(cfg, pid: str, ph: Dict[str, Any], now: Optional[float] = None, f: Optional[Fence] = None) -> str:
    """'' below every cap; else e.g. 'rows cap (3/3 rows started)'."""
    caps = ph.get("caps") or {}
    if not caps:
        return ""
    u = usage(cfg, pid, ph, now, f)
    for k in CAP_KEYS:
        if k not in caps:
            continue
        lim = float(caps[k]) * (DAYS_FACTOR if k == "days" else 1)
        # loops / replans: the cap counts loops allowed — the count reaches it when the last allowed loop STARTS, and
        # that loop must still run (its planner, round and rows); one more than the cap is a hand edit or a race
        if (u[k] > lim) if k in LOOP_COUNTERS else (u[k] >= lim):
            what = {"rows": "rows started", "rulings": "Steward rulings since it opened",
                    "days": f"days since it opened (cap {caps[k]} × {DAYS_FACTOR})", "loops": "course loops",
                    "replans": "checkpoint replans"}[k]
            return f"{k} cap ({u[k]:g}/{lim:g} {what})"
    return ""


# -- writers (the engine and the Owner's commands) -------------------------------------------------
def add_round(cfg, pid: str, tag: str, by: str) -> str:
    """Record round `tag` in phase `pid` (a new round names its phase). '' on success."""
    with _Lock(cfg):
        phases = load(cfg)
        if pid not in phases:
            return f"no phase {pid}"
        for other, ph in phases.items():
            if other != pid and tag in ph.get("rounds", []):
                return f"round {tag} is already in phase {other}"
        if tag not in phases[pid]["rounds"]:
            phases[pid]["rounds"].append(tag)
            save(cfg, phases, by, f"round {tag} recorded in {pid}")
    return ""


def record_loop(cfg, pid: str, by: str = "engine", counter: str = "loops") -> int:
    """Count one course loop of the phase as it starts (drop D item 2, lib/courseloop.py) in `counter` — `loops`, or
    `replans` for a checkpoint judge's loop when the phase caps replans (v3.1, §15.4). -> the new count."""
    if counter not in LOOP_COUNTERS:
        raise ValueError(f"not a loop counter: {counter}")
    with _Lock(cfg):
        phases = load(cfg)
        if pid not in phases:
            return 0
        phases[pid][counter] = int(phases[pid].get(counter, 0) or 0) + 1
        save(cfg, phases, by, f"{pid} {counter[:-1]} {phases[pid][counter]}")
        return phases[pid][counter]


def set_state(cfg, pid: str, state: str, by: str, why: str = "", only_from: str = "",
              recheck_cap: bool = False) -> Tuple[bool, str]:
    """only_from / recheck_cap (the engine): applied only while the phase is still in that state and, under the
    lock, still at a cap — an Owner's close or re-cap in between wins."""
    with _Lock(cfg):
        phases = load(cfg)
        if pid not in phases:
            return False, f"no phase {pid}"
        ph = phases[pid]
        prev = ph["state"]
        if only_from and prev != only_from:
            return False, f"phase {pid} is {prev}, not {only_from}"
        if recheck_cap:
            cap = cap_reached(cfg, pid, ph)
            if not cap:
                return False, f"phase {pid} is no longer at a cap"
            why = f"at its {cap}"
        ph["state"] = state
        ph[{"rest": "rested", "closed": "closed"}[state]] = time.strftime("%F %T")
        ph["rest_reason" if state == "rest" else "close_reason"] = why[:200]
        save(cfg, phases, by, f"{pid} {prev} -> {state}: {why[:200]}")
    return True, f"phase {pid}: {prev} -> {state}"


def _parse_caps(spec: str) -> Tuple[Dict[str, Any], str]:
    out: Dict[str, Any] = {}
    for part in [p for p in re.split(r"[,\s]+", spec or "") if p]:
        if "=" not in part:
            return {}, f"--caps wants key=value pairs ({', '.join(CAP_KEYS)}), got '{part}'"
        k, v = part.split("=", 1)
        out[k.strip()] = v.strip()
    ph, probs = validate({"phases": {"X": {"state": "open", "caps": out}}})
    return (ph["X"]["caps"], "") if not probs else ({}, "; ".join(probs))


def open_phase(cfg, pid: str, caps_spec: str, rounds_add: List[str], why: str, by: str = "owner",
               session_loops: Optional[bool] = None) -> Tuple[bool, str]:
    """The Owner opens (or re-opens, or re-caps) a phase. session_loops (v3.1): True/False sets whether rounds a
    session created may loop here (within caps.loops); None keeps it."""
    if not ID_RE.match(pid):
        return False, f"phase id '{pid}' is not [A-Za-z0-9][A-Za-z0-9_.-]*"
    caps, err = _parse_caps(caps_spec)
    if err:
        return False, err
    with _Lock(cfg):
        phases = load(cfg)
        ph = phases.get(pid) or {"state": "", "caps": {}, "rounds": [], "loops": 0}
        prev = ph.get("state") or "new"
        cand = dict(ph, caps=dict(ph.get("caps") or {}, **caps))
        if session_loops is not None:
            cand["session_loops"] = bool(session_loops)
        for tag in rounds_add:
            for other, o in phases.items():
                if other != pid and tag in o.get("rounds", []):
                    return False, f"round {tag} is already in phase {other} — a round belongs to one phase"
            if tag not in cand["rounds"]:
                cand["rounds"] = [*cand["rounds"], tag]
        if not cand.get("rows_since"):
            # the rows cap counts from the first open (a record written before vD3: from its latest open), in whole
            # seconds as dispatch records' epochs — a row dispatched in the opening second counts
            cand["rows_since"] = int(ph.get("opened_epoch") or time.time())
        if prev != "open":
            cand.update(state="open", opened=time.strftime("%F %T"), opened_epoch=time.time())
            cand.pop("rest_reason", None)
            cand.pop("close_reason", None)
        trial = dict(phases, **{pid: cand})
        cap = cap_reached(cfg, pid, cand, f=Fence(cfg, trial, on=True))   # on: the first open has no record yet
        if cap:
            return False, (f"phase {pid} would be at its {cap} at once and rest again on the next tick — raise it "
                           f"(--caps …)")
        phases[pid] = cand
        save(cfg, phases, by, f"open {pid} (was {prev}) caps {cand['caps']} rounds {cand['rounds']}"
                              f"{' session_loops ' + ('on' if cand.get('session_loops') else 'off') if session_loops is not None else ''}"
                              f": {why[:200]}")
    note = "" if cand["caps"] else "  ⚠ no caps: the phase runs until the Owner rests it"
    if cand.get("session_loops") and "loops" not in cand["caps"]:
        note += "  ⚠ session loops on, but no loops cap: a session's round loops only within caps.loops (none here)"
    return True, (f"phase {pid}: {prev} -> open · caps {_caps_text(cand['caps'])} · rounds "
                  f"{', '.join(cand['rounds']) or '-'}{' · session loops on' if cand.get('session_loops') else ''}{note}")


def approve(cfg, by: str = "owner") -> Tuple[bool, str]:
    """Adopt the Owner's hand edit of phases.yaml."""
    p = yaml_path(cfg)
    with _Lock(cfg):
        if not p.is_file():
            return False, f"no {p}"
        try:
            import yaml
            data = yaml.safe_load(p.read_text())
        except Exception as e:
            return False, f"phases.yaml does not parse: {e}"
        phases, probs = validate(data)
        if probs:
            return False, "phases.yaml refused: " + "; ".join(probs)
        old = load(cfg)
        for pid, ph in phases.items():
            o = old.get(pid) or {}
            if ph["state"] == "open" and (o.get("state") != "open" or not o.get("opened_epoch")):
                ph.setdefault("opened", time.strftime("%F %T"))
                ph["opened_epoch"] = time.time()
            elif o.get("opened_epoch"):
                ph["opened_epoch"] = o["opened_epoch"]
            if o.get("rows_since"):
                ph["rows_since"] = o["rows_since"]          # never moved by a hand edit
            elif ph["state"] == "open":
                ph["rows_since"] = int(o.get("opened_epoch") or ph["opened_epoch"])   # pre-vD3: its latest open
            else:
                ph.pop("rows_since", None)
        diff = describe_diff(old, phases)
        save(cfg, phases, by, "approve a hand edit of phases.yaml: " + "; ".join(diff))
    return True, "approved: " + ("; ".join(diff) or "no change")


def describe_diff(old: Dict[str, Dict[str, Any]], new: Dict[str, Dict[str, Any]]) -> List[str]:
    """What an approve changes, phase by phase (the Owner sees what a hand edit — anyone's — does)."""
    out = []
    for pid in sorted(set(old) | set(new)):
        a, b = old.get(pid), new.get(pid)
        if a is None:
            out.append(f"{pid} added ({b['state']})")
        elif b is None:
            out.append(f"{pid} removed (was {a['state']})")
        else:
            ch = [f"state {a['state']} -> {b['state']}"] if a["state"] != b["state"] else []
            ch += [f"caps {_caps_text(a.get('caps') or {})} -> {_caps_text(b.get('caps') or {})}"] \
                if (a.get("caps") or {}) != (b.get("caps") or {}) else []
            ch += [f"rounds {a.get('rounds')} -> {b.get('rounds')}"] if a.get("rounds") != b.get("rounds") else []
            ch += [f"loops {a.get('loops')} -> {b.get('loops')}"] if a.get("loops") != b.get("loops") else []
            ch += [f"replans {a.get('replans', 0)} -> {b.get('replans', 0)}"] \
                if int(a.get("replans", 0) or 0) != int(b.get("replans", 0) or 0) else []
            ch += [f"session_loops {'on' if a.get('session_loops') else 'off'} -> {'on' if b.get('session_loops') else 'off'}"] \
                if bool(a.get("session_loops")) != bool(b.get("session_loops")) else []
            if ch:
                out.append(f"{pid}: " + ", ".join(ch))
    return out


def turn_off(cfg) -> str:
    """`orch phase off` (the Owner's): remove the fence — phases.yaml, the record and the marker."""
    with _Lock(cfg):
        for p in (yaml_path(cfg), rec_path(cfg), marker_path(cfg)):
            p.unlink(missing_ok=True)
    return "the phase fence is off: phases.yaml, state/phases.json and state/phases.on removed (v2 behaviour)"


# -- the engine ------------------------------------------------------------------------------------
def tick(rig) -> None:
    """Engine tick phase: report an unapproved phases.yaml once per content; rest a phase at a cap."""
    cfg = rig.cfg
    if not active(cfg):
        if getattr(rig, "_phases_seen", False) and rig.st.once(f"phases-removed-{int(time.time() // 86400)}"):
            rig.st.event("phases", "phases_removed")
            rig.notify("normal", f"{cfg.name}: the phase fence is gone",
                       "phases.yaml, state/phases.json and state/phases.on were removed outside `orch phase off`: the "
                       "rig no longer runs inside a phase. `orch phase open …` restores the fence.")
        return
    rig._phases_seen = True
    with _Lock(cfg):                 # never between a writer's record and its phases.yaml
        t = tamper(cfg)
        sha = _sha_file(yaml_path(cfg))
    if t:
        key = f"phases-tamper-{sha[:16] or 'absent'}"
        if rig.st.once(key):
            rig.st.event("phases", "phases_tamper", detail=t[:300])
            rig.st.engine_log(f"phases: {t}")
            rig.notify("normal", f"{cfg.name}: phases.yaml changed outside orch phase", t[:600])
    f = Fence(cfg)
    for pid, ph in f.phases.items():
        if ph["state"] != "open":
            continue
        cap = cap_reached(cfg, pid, ph, f=f)
        if not cap:
            continue
        ok, _ = set_state(cfg, pid, "rest", by="engine", why=f"at its {cap}", only_from="open", recheck_cap=True)
        if ok:
            rig.st.event("phases", "phase_rest", phase=pid, cap=cap)
            rig.st.engine_log(f"phases: {pid} rests — at its {cap}; running rows finish, nothing new starts")
            rig.notify("normal", f"{cfg.name}: phase {pid} rests",
                       f"{pid} reached its {cap}. Running rows finish; nothing new starts. Only you re-open it "
                       f"(`orch phase open {pid} --caps …`) or open the next phase.")


def _caps_text(caps: Dict[str, Any]) -> str:
    return ", ".join(f"{k} {caps[k]:g}" for k in CAP_KEYS if k in caps) or "none"


def status_text(cfg) -> str:
    if not active(cfg):
        return "no phases (no phases.yaml): no fence — every started lane runs (v2 behaviour)"
    f = Fence(cfg)
    L = [f"phases: {len(f.phases)} (the fence is ON: rows run only in an open phase)"]
    t = tamper(cfg)
    if t:
        L.append(f"⚠ {t}")
    for pid, ph in f.phases.items():
        u = usage(cfg, pid, ph, f=f)
        caps = ph.get("caps") or {}
        use = ", ".join(f"{k} {u[k]:g}/{float(caps[k]) * (DAYS_FACTOR if k == 'days' else 1):g}" for k in CAP_KEYS
                        if k in caps) or "no caps"
        L.append(f"  {pid:<8} {ph['state']:<6} {use} · rounds {', '.join(ph.get('rounds') or []) or '-'}"
                 f"{' · session loops on' if ph.get('session_loops') else ''}{_why_rest(ph)}")
    return "\n".join(L)


def digest_lines(cfg, ev: List[Dict[str, Any]]) -> List[str]:
    if not active(cfg):
        return []
    L = ["", "## Phases (the Owner opens them)", ""]
    for line in status_text(cfg).splitlines()[1:]:
        L.append(f"- {line.strip()}")
    try:
        notes = json.loads((cfg.state / "sched.json").read_text())
    except Exception:
        notes = {}
    held: Dict[str, List[str]] = {}
    for rid, n in notes.items():
        if str(n).startswith("PHASE"):
            held.setdefault(str(n)[len("PHASE — "):][:140], []).append(rid)
    for why, ids in sorted(held.items()):
        L.append(f"- held by the fence: {len(ids)} row(s) ({', '.join(sorted(ids)[:8])}{' …' if len(ids) > 8 else ''})"
                 f" — {why}")
    for e in ev:
        if e.get("event") == "phase_rest":
            L.append(f"- {e.get('ts')}: phase {e.get('phase')} went to REST at its {e.get('cap')} — running rows "
                     f"finish, nothing new starts; only you re-open it")
        elif e.get("event") == "phases_tamper":
            L.append(f"- {e.get('ts')}: phases.yaml changed outside `orch phase` — ignored ({e.get('detail', '')[:120]})")
    return L


# -- CLI -------------------------------------------------------------------------------------------
def _operator(cfg, what: str) -> bool:
    import capability
    s = capability.session(cfg)
    if s:
        print(f"REFUSED: {what} is the Owner's (operator shell only) — opening, resting and closing a phase "
              f"stay the Owner's; session {s} may not")
        return False
    return True


def main(argv: List[str]) -> int:
    from config_parser import load as load_cfg
    cfg = load_cfg()
    sub = argv[0] if argv else "status"
    if sub in ("-h", "--help", "help"):
        print(__doc__.split("Only `orch phase` and the engine")[0].strip())
        return 0
    if sub == "status":
        print(status_text(cfg))
        return 0
    if sub not in ("open", "rest", "close", "approve", "off"):
        print(f"unknown: orch phase {sub} (open | rest | close | approve | off | status)")
        return 2
    if not _operator(cfg, f"`orch phase {sub}`"):
        return 1
    from store import Store
    st = Store(cfg.state)
    if sub == "off":
        print(turn_off(cfg))
        st.event("phases", "phase_off", by="owner")
        return 0
    if sub == "approve":
        ok, msg = approve(cfg)
        print(msg)
        if ok:
            st.event("phases", "phase_approve", by="owner")
        return 0 if ok else 1
    rest = argv[1:]
    if not rest or rest[0].startswith("-"):
        print(f'usage: orch phase {sub} <P>' + (' [--caps rows=N,rulings=N,days=N,loops=N,replans=N] [--rounds a,b]'
                                              ' [--session-loops on|off]' if sub == "open" else "") + ' ["why"]')
        return 2
    pid, rest = rest[0], rest[1:]
    caps, rounds_add, words, sloops = "", [], [], None
    i = 0
    while i < len(rest):
        if rest[i] in ("--caps", "--rounds", "--session-loops") and i + 1 < len(rest):
            if sub != "open":
                print(f"{rest[i]} is for `orch phase open`")
                return 2
            if rest[i] == "--caps":
                caps = rest[i + 1]
            elif rest[i] == "--session-loops":
                if rest[i + 1].lower() not in ("on", "off"):
                    print("--session-loops on|off")
                    return 2
                sloops = rest[i + 1].lower() == "on"
            else:
                rounds_add += [x for x in re.split(r"[,\s]+", rest[i + 1]) if x]
            i += 2
            continue
        words.append(rest[i])
        i += 1
    why = " ".join(words)
    if sub == "open":
        ok, msg = open_phase(cfg, pid, caps, rounds_add, why, session_loops=sloops)
    else:
        ok, msg = set_state(cfg, pid, "rest" if sub == "rest" else "closed", by="owner",
                            why=f"the Owner: {why}" if why else "the Owner")
    print(msg)
    if ok:
        st.event("phases", f"phase_{sub}", phase=pid, by="owner", why=why[:200])
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
