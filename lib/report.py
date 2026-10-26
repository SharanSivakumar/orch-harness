#!/usr/bin/env python3
"""`orch report [--round <tag>] [--json]` — what the rig actually spent its time on.

Built from state/attempts.jsonl (append-only, survives resets) and the rows'
result files. Answers the questions an audit used to reconstruct by hand:
attempts and wall-clock per row, hours spent on attempts that were later redone,
unblock / review-reject / check-failure counts, per-executor medians, rows that
were accepted but changed nothing, and the metric series if a goal is set.
"""
from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config_parser import Config, load  # noqa: E402
from rows import load_rows  # noqa: E402
from store import Store  # noqa: E402


def collect(cfg: Config, round_tag: str = "") -> Dict[str, Any]:
    st = Store(cfg.state)
    rows, by_id = load_rows(cfg)
    lanes = None
    if round_tag:
        import rounds
        lanes = set(str(x) for x in rounds.load_rounds(cfg).get(round_tag, {}).get("lanes", []))
    sel = {r.id: r for r in rows if lanes is None or r.lane in lanes}
    per: Dict[str, Dict[str, Any]] = {rid: {"attempts": 0, "secs": [], "unblocks": 0, "rejects": 0,
                                            "check_fails": 0, "dead": 0, "executors": [], "accepted": False}
                                      for rid in sel}
    open_d: Dict[str, int] = {}
    for e in st.events():
        rid = e.get("id")
        if rid not in per:
            continue
        p = per[rid]
        ev = e.get("event")
        if ev == "dispatch":
            p["attempts"] += 1
            p["executors"].append(e.get("executor", "?"))
            open_d[rid] = e.get("epoch", 0)
        elif ev in ("claim", "dead") and rid in open_d:
            p["secs"].append(max(0, e.get("epoch", 0) - open_d.pop(rid)))
        if ev == "unblock_dispatch":
            p["unblocks"] += 1
        elif ev == "review_verdict" and e.get("verdict") == "REJECT":
            p["rejects"] += 1
        elif ev == "check_failed":
            p["check_fails"] += 1
        elif ev == "dead":
            p["dead"] += 1
    noop_re = re.compile((cfg.data.get("progress") or {}).get("noop_pattern", r"$^"), re.I)
    noop = []
    for rid, p in per.items():
        res = st.result(rid)
        p["accepted"] = st.done(rid)
        p["phase"] = st.phase(rid) or "pending"
        p["commits"] = res.get("commits")
        if p["accepted"] and res and int(res.get("commits", 0) or 0) == 0 and noop_re.search(res.get("summary", "")):
            noop.append(rid)
    by_exec: Dict[str, List[int]] = {}
    redo = 0
    for rid, p in per.items():
        for i, s in enumerate(p["secs"]):
            x = p["executors"][i] if i < len(p["executors"]) else "?"
            by_exec.setdefault(x, []).append(s)
            if i < len(p["secs"]) - 1:
                redo += s
    return {"rows": per, "by_exec": by_exec, "redo_secs": redo, "noop": noop, "round": round_tag}


def render(cfg: Config, round_tag: str = "") -> str:
    c = collect(cfg, round_tag)
    rows = c["rows"]
    total = sum(sum(p["secs"]) for p in rows.values())
    acc = sum(1 for p in rows.values() if p["accepted"])
    out = [f"# Rig report — {cfg.name}{' — round ' + round_tag if round_tag else ''}", "",
           f"- rows: {len(rows)} · accepted {acc} · executor time {total / 3600:.1f} h "
           f"· redone attempts {c['redo_secs'] / 3600:.1f} h",
           f"- unblocks {sum(p['unblocks'] for p in rows.values())} · review rejects "
           f"{sum(p['rejects'] for p in rows.values())} · close-check refusals "
           f"{sum(p['check_fails'] for p in rows.values())} · dead sessions {sum(p['dead'] for p in rows.values())}",
           f"- accepted but changed nothing (no commits, no-op summary): {len(c['noop'])}"
           + (f" — {' '.join(c['noop'][:15])}" if c["noop"] else ""), "",
           "## Per executor (minutes per attempt)", "", "| executor | attempts | median | p90 |", "|---|---|---|---|"]
    for x, secs in sorted(c["by_exec"].items()):
        s = sorted(secs)
        p90 = s[min(len(s) - 1, int(len(s) * 0.9))] if s else 0
        out.append(f"| {x} | {len(s)} | {statistics.median(s) / 60:.0f} | {p90 / 60:.0f} |")
    out += ["", "## Rows with more than one attempt", "", "| row | attempts | unblocks | rejects | refusals | phase |",
            "|---|---|---|---|---|---|"]
    for rid, p in sorted(rows.items(), key=lambda kv: -kv[1]["attempts"]):
        if p["attempts"] > 1:
            out.append(f"| {rid} | {p['attempts']} | {p['unblocks']} | {p['rejects']} | {p['check_fails']} | {p['phase']} |")
    try:
        import rounds
        pts = [r for r in rounds.scoreboard(cfg) if not round_tag or r.get("series") == round_tag]
        if pts:
            out += ["", "## Metric", "", "| ts | series | instrument | name | value | after | best | stall |",
                    "|---|---|---|---|---|---|---|---|"]
            for r in pts[-20:]:
                out.append(f"| {r.get('ts')} | {r.get('series')} | {r.get('instrument')} | {r.get('name')} | "
                           f"{r.get('value')} | {r.get('trigger')} | {r.get('best')} | {r.get('stall')} |")
    except Exception:
        pass
    return "\n".join(out) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(prog="orch report")
    ap.add_argument("--round", default="")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    cfg = load()
    if a.json:
        print(json.dumps(collect(cfg, a.round), indent=1, default=str))
    else:
        print(render(cfg, a.round))
    return 0


if __name__ == "__main__":
    sys.exit(main())
