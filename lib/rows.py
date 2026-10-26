#!/usr/bin/env python3
"""The one queue parser (v2).

v1 parsed `queue_<L>.tsv` four times — the bash watcher, the resolver, the close
check and lint — and the copies drifted: a line with fewer than five fields was
skipped by the resolver and lint but dispatched with an empty plan by bash.
Everything now reads rows through `load_rows()`.

Queue line:  id  deps  gate  executor  plan_path  [extras…]

Extras containing `=` are row metadata; every other extra is an allowlist path
(comma or space separated), exactly as in v1:

    res=a,b          resource slots the engine takes all-or-nothing at dispatch
    review=0|1       force the post-close review off/on for this row
    reviewer=<exec>  the reviewer's executor
    timeout=<min>    wall-clock budget for one attempt
    prio=<n>         higher dispatches first among ready rows
    accept=<file>    acceptance probe (relative to the plan's folder)
    preflight=run    `orch preflight --run` executes this row's Verification block
    gate_kind=steward|owner|spend   who may release this row's gate (v3, lib/steward.py): `steward`
                     gates the Steward lane may approve; `owner` (the default, steward.default_gate_kind)
                     and `spend` gates only the Owner releases
    rearm=a,b,round  the row may ask the engine to reset rows a,b (`orch rearm <id>`) and, with
                     `round`, run `orch round new|add-lanes` and request `orch round start`
                     (lib/rearm.py; trusted only from the dispatch snapshot)
    needs=go         (v3) a dep this row names counts as met only when it is accepted with outcome `go`
                     or with no outcome; one closed `orch done --outcome nogo|held|fail|infeasible` holds the row

Archived lanes (`<instance>/archive/queue_<L>.tsv`) are parsed too: their rows
satisfy deps and appear in reports, but are never scheduled or drawn.
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config_parser import Config  # noqa: E402

META_KEYS = {"res", "review", "reviewer", "timeout", "prio", "accept", "preflight", "rearm", "gate_kind", "needs"}


@dataclass
class QRow:
    lane: str
    id: str
    deps: List[str]
    gate: bool
    queue_exec: str
    plan: str
    extra_allow: List[str] = field(default_factory=list)
    meta: Dict[str, str] = field(default_factory=dict)
    index: int = 0              # position in its lane
    archived: bool = False
    problems: List[str] = field(default_factory=list)

    @property
    def resources(self) -> List[str]:
        v = self.meta.get("res", "")
        return [x for x in re.split(r"[,\s]+", v) if x]

    @property
    def prio(self) -> int:
        try:
            return int(self.meta.get("prio", "0"))
        except ValueError:
            return 0

    @property
    def timeout_minutes(self) -> int:
        v = self.meta.get("timeout", "").strip().lower()
        m = re.fullmatch(r"(\d+)\s*([mh]?)", v)
        if not m:
            return 0
        n = int(m.group(1))
        return n * 60 if m.group(2) == "h" else n

    @property
    def needs_go(self) -> bool:
        return self.meta.get("needs", "").strip().lower() == "go"


def parse_line(line: str, lane: str, index: int, archived: bool = False) -> Optional[QRow]:
    s = line.strip()
    if not s or s.startswith("#"):
        return None
    parts = re.split(r"\s+", s)
    if len(parts) < 5:
        r = QRow(lane=lane, id=parts[0], deps=[], gate=False, queue_exec="", plan="",
                 index=index, archived=archived)
        r.problems.append(f"queue line has {len(parts)} fields, needs 5 (id deps gate executor plan)")
        return r
    rid, deps, gate, qexec, plan = parts[:5]
    extras: List[str] = []
    meta: Dict[str, str] = {}
    for tok in parts[5:]:
        if "=" in tok and tok.split("=", 1)[0] in META_KEYS:
            k, v = tok.split("=", 1)
            meta[k] = v
        else:
            extras += [e for e in tok.split(",") if e]
    r = QRow(lane=lane, id=rid, deps=[] if deps == "-" else [d for d in deps.split(",") if d],
             gate=(gate == "1"), queue_exec=qexec, plan=plan, extra_allow=extras, meta=meta,
             index=index, archived=archived)
    if gate not in ("0", "1"):
        r.problems.append(f"gate column is '{gate}', must be 0 or 1")
    if "needs" in meta and not r.needs_go:
        r.problems.append(f"needs={meta['needs']} is not a known requirement (needs=go)")
    return r


def parse_file(path: Path, lane: str, archived: bool = False) -> List[QRow]:
    rows: List[QRow] = []
    try:
        text = path.read_text(errors="replace")
    except Exception:
        return rows
    for line in text.splitlines():
        r = parse_line(line, lane, len(rows), archived)
        if r:
            rows.append(r)
    return rows


def archive_dir(cfg: Config) -> Path:
    return cfg.instance / "archive"


def archived_lanes(cfg: Config) -> List[str]:
    d = archive_dir(cfg)
    if not d.is_dir():
        return []
    return sorted(q.stem[len("queue_"):] for q in d.glob("queue_*.tsv"))


def active_lanes(cfg: Config) -> List[str]:
    """Lanes with a live queue file, in configured order; archived lanes are excluded
    even when orch.yaml still names them in lanes.order."""
    found = {q.stem[len("queue_"):] for q in cfg.instance.glob("queue_*.tsv")}
    return [l for l in cfg.lanes() if l in found]


def load_rows(cfg: Config, include_archived: bool = True) -> Tuple[List[QRow], Dict[str, QRow]]:
    """-> (rows in lane order then queue order, by_id). Active rows win an id clash."""
    rows: List[QRow] = []
    for lane in active_lanes(cfg):
        rows += parse_file(cfg.instance / f"queue_{lane}.tsv", lane)
    if include_archived:
        active_ids = {r.id for r in rows}
        for lane in archived_lanes(cfg):
            rows += [r for r in parse_file(archive_dir(cfg) / f"queue_{lane}.tsv", lane, True)
                     if r.id not in active_ids]
    by_id: Dict[str, QRow] = {}
    for r in rows:
        by_id.setdefault(r.id, r)
    return rows, by_id


def find_row(cfg: Config, rid: str) -> Optional[QRow]:
    return load_rows(cfg)[1].get(rid)


def plan_dir(cfg: Config, row: QRow) -> Optional[Path]:
    """The row's own folder, or None when the plan sits at the workspace top level
    (that would make the whole workspace the 'row folder')."""
    if not row.plan:
        return None
    d = (cfg.workspace / row.plan).parent
    try:
        if d.resolve() == cfg.workspace.resolve():
            return None
    except Exception:
        return None
    return d


def implicit_deps(cfg: Config, rows: List[QRow]) -> Dict[str, List[str]]:
    """serial lanes: each row also waits for EVERY row above it in its lane (v1 semantics:
    the lane's pointer is its first unfinished row, so a row closed by hand further down
    never lets the rows below it overtake an unfinished row above)."""
    out: Dict[str, List[str]] = {}
    above: Dict[str, List[str]] = {}
    for r in rows:
        if r.archived:
            continue
        if cfg.lane_mode(r.lane) == "serial" and above.get(r.lane):
            out[r.id] = list(above[r.lane])
        above.setdefault(r.lane, []).append(r.id)
    return out


def unmet_deps(st, r, implicit: Optional[Dict[str, List[str]]] = None) -> List[str]:
    """The deps that do not let `r` (a QRow or any row with .id/.deps/.meta) start: every dep not
    accepted, plus — when the row carries `needs=go` — every dep it names that was accepted with a
    negative outcome, as '<dep>:<outcome>'."""
    named = list(r.deps)
    needs_go = str((r.meta or {}).get("needs", "")).strip().lower() == "go"
    out: List[str] = []
    for d in named + [x for x in (implicit or {}).get(r.id, []) if x not in named]:
        if not st.done(d):
            out.append(d)
        elif needs_go and d in named and not st.go_ok(d):
            out.append(f"{d}:{st.outcome(d)}")
    return out
