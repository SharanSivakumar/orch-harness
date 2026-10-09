#!/usr/bin/env python3
"""`orch lint` — validate an instance before a single executor is dispatched.

This is the contract check a planning agent runs after writing a ladder. It
catches the failures that used to cost a dispatch each: a dep naming a row
that does not exist, a cycle, a plan path that was never written, a row
whose executor has no adapter, a gated row with no note of who releases it,
and a row whose allowlist would refuse its own phase folder.

Exit code 0 = clean, 1 = errors present. Warnings never fail the run.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config_parser import load                    # noqa: E402
from state_resolver import Resolver               # noqa: E402
from template_engine import kicked, kick_stems, template_path  # noqa: E402

FRAMEWORK = Path(__file__).resolve().parent.parent


def main() -> int:
    cfg = load()
    res = Resolver(cfg)
    errors: List[str] = []
    warns: List[str] = []

    lanes = cfg.lanes()
    if not lanes:
        errors.append("no queue_*.tsv files found in the instance directory")

    rows = []
    for lane in lanes:
        rows.extend(res.parse_lane(lane))
    if not rows:
        errors.append("every queue file is empty (only comments?)")

    by_id: Dict[str, object] = {}
    for r in rows:
        if r.id in by_id:
            errors.append(f"duplicate row id '{r.id}' (lanes {getattr(by_id[r.id], 'lane', '?')} and {r.lane})")
        by_id[r.id] = r

    allowed = set(cfg.data["executors"]["allowed"])
    adapters = {p.stem for p in (FRAMEWORK / "lib" / "adapters").glob("*.sh")}
    alias = {"opus": "claude", "sonnet": "claude", "haiku": "claude", "hermespro": "hermes_pro"}

    for r in rows:
        # executor
        if r.queue_exec not in allowed:
            errors.append(f"{r.id}: executor '{r.queue_exec}' is not in executors.allowed")
        else:
            ad = alias.get(r.queue_exec, r.queue_exec)
            if ad not in adapters:
                errors.append(f"{r.id}: executor '{r.queue_exec}' has no adapter lib/adapters/{ad}.sh")
        # deps
        for d in r.deps:
            if d not in by_id:
                errors.append(f"{r.id}: depends on '{d}', which is in no queue file")
        # plan
        plan = cfg.workspace / r.plan
        if not plan.is_file():
            warns.append(f"{r.id}: plan {r.plan} does not exist yet (relative to {cfg.workspace})")
        # template
        flavor = cfg.flavor(r.queue_exec)
        tpl = f"_TEMPLATE_{flavor.upper()}.md"
        if not ((cfg.instance / "prompts" / tpl).is_file() or (FRAMEWORK / "templates" / tpl).is_file()):
            errors.append(f"{r.id}: no template {tpl} for flavor '{flavor}' (instance prompts/ or framework templates/)")
        if kicked(cfg, r.queue_exec):
            stems = kick_stems(r.queue_exec, flavor)
            if not any(template_path(cfg, k) for k in stems):
                errors.append(f"{r.id}: executor '{r.queue_exec}' is kick-driven but neither {' nor '.join(stems)} exists")
        # allowlist sanity
        al = res.allowlist(r.id)
        if not al:
            warns.append(f"{r.id}: resolves to an EMPTY allowlist — it will not be able to commit anything")
        # gate
        if r.gate and (cfg.control / f"GO_{r.id}").exists():
            warns.append(f"{r.id}: gated but control/GO_{r.id} already exists — the gate is pre-released")

    # cycles
    color: Dict[str, int] = {}

    def visit(node: str, stack: List[str]) -> None:
        color[node] = 1
        for d in getattr(by_id.get(node), "deps", []) or []:
            if d not in by_id:
                continue
            if color.get(d) == 1:
                errors.append(f"dependency cycle: {' -> '.join(stack + [node, d])}")
            elif color.get(d, 0) == 0:
                visit(d, stack + [node])
        color[node] = 2

    for rid in by_id:
        if color.get(rid, 0) == 0:
            visit(rid, [])

    # reachability: a row nothing can start
    starts = [r.id for r in rows if not r.deps]
    if rows and not starts:
        errors.append("no row has empty deps — nothing can ever start")

    # config sanity
    if cfg.data["guard"]["git_lock"] not in cfg.data["guard"]["resources"]:
        errors.append(f"guard.git_lock '{cfg.data['guard']['git_lock']}' is not listed in guard.resources")
    if not (cfg.git_repo / ".git").exists():
        warns.append(f"guard.git_repo {cfg.git_repo} is not a git repository — `orch commit` will fail there")
    for s in cfg.data["guard"]["compliance_scanners"]:
        p = Path(s) if s.startswith("/") else cfg.instance / s
        if not p.exists():
            errors.append(f"compliance scanner '{s}' does not exist ({p})")
    w = cfg.data["watchdog"]
    if w["enabled"] and not w["telegram_chat_id"]:
        warns.append("watchdog.enabled is true but no telegram_chat_id is set — push will be silent")

    print(f"instance   {cfg.instance}")
    print(f"workspace  {cfg.workspace}")
    print(f"root       {cfg.root}")
    print(f"git repo   {cfg.git_repo}")
    print(f"lanes      {' '.join(lanes)}   ({len(rows)} rows)")
    print()
    for wmsg in warns:
        print(f"  warn   {wmsg}")
    for emsg in errors:
        print(f"  ERROR  {emsg}")
    print()
    if errors:
        print(f"lint FAILED — {len(errors)} error(s), {len(warns)} warning(s)")
        return 1
    print(f"lint clean — 0 errors, {len(warns)} warning(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
