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
from store import Store                           # noqa: E402
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

    from rows import load_rows, active_lanes
    lanes = active_lanes(cfg)
    qrows, _ = load_rows(cfg, include_archived=True)
    archived_ids = {q.id for q in qrows if q.archived}
    for q in qrows:
        if q.problems and not q.archived:
            errors.append(f"queue_{q.lane}.tsv row '{q.id}': {'; '.join(q.problems)}")
    rows = [q for q in qrows if not q.archived and not q.problems]
    if not rows:
        errors.append("every queue file is empty (only comments?)")

    by_id: Dict[str, object] = {}
    for r in rows:
        if r.id in by_id:
            errors.append(f"duplicate row id '{r.id}' (lanes {getattr(by_id[r.id], 'lane', '?')} and {r.lane})")
        by_id[r.id] = r

    allowed = set(cfg.data["executors"]["allowed"])
    adapters = {p.stem for p in (FRAMEWORK / "lib" / "adapters").glob("*.sh")}
    resources = cfg.resources()

    for r in rows:
        if r.id.startswith(("ub-", "rv-", "pf-", "rr-")):
            errors.append(f"{r.id}: row ids may not start with ub-/rv-/pf-/rr- (reserved for sub-sessions)")
        # executor
        if r.queue_exec not in allowed:
            errors.append(f"{r.id}: executor '{r.queue_exec}' is not in executors.allowed")
        else:
            ad = cfg.adapter(r.queue_exec)
            if ad not in adapters:
                errors.append(f"{r.id}: executor '{r.queue_exec}' has no adapter lib/adapters/{ad}.sh")
        # deps
        for d in r.deps:
            if d not in by_id and d not in archived_ids:
                errors.append(f"{r.id}: depends on '{d}', which is in no queue file (active or archived)")
        for x in r.resources:
            if x not in resources:
                errors.append(f"{r.id}: res={x} is not in guard.resources")
        if "rearm" in r.meta:
            from rearm import parse_grants, ROUND
            grants, bad = parse_grants(r.meta["rearm"])
            if bad or not grants:
                errors.append(f"{r.id}: rearm={r.meta['rearm']} is malformed (row ids and/or 'round', comma separated)")
            for g in grants:
                if g == r.id:
                    errors.append(f"{r.id}: rearm= names the row itself; a row may never re-arm itself")
                elif g != ROUND and g not in by_id and g not in archived_ids:
                    warns.append(f"{r.id}: rearm={g} names no row yet (fine if a course loop adds it later)")
        rv = r.meta.get("reviewer")
        if rv and rv not in allowed:
            errors.append(f"{r.id}: reviewer={rv} is not in executors.allowed")
        elif rv and not cfg.decides(rv):
            errors.append(f"{r.id}: reviewer={rv} is a {cfg.flavor(rv)} tier — a reviewer needs a hard tier")
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
        if str(r.meta.get("needs", "")).lower() == "go" and not r.deps:
            warns.append(f"{r.id}: needs=go but its deps column names no row — it holds on nothing "
                         f"(a serial lane's order is not a named dep: name the judge in deps)")
        if r.gate and (cfg.control / f"GO_{r.id}").exists() and not Store(cfg.state).done(r.id):
            warns.append(f"{r.id}: hard-gated but control/GO_{r.id} already exists — the gate is pre-released")

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
    starts = [r.id for r in rows if all(d in archived_ids for d in r.deps)]
    if rows and not starts:
        errors.append("no row has empty deps — nothing can ever start")

    # templates: leftover placeholders after rendering are a defect
    try:
        import template_engine as te
        for stem in ("_TEMPLATE_HARD.md", "_TEMPLATE_JRDEV.md", "_TEMPLATE_UNBLOCK.md", "_TEMPLATE_REVIEW.md",
                     "_TEMPLATE_PLANREVIEW.md"):
            tp = te.template_path(cfg, stem)
            if tp is None:
                if stem in ("_TEMPLATE_REVIEW.md",) and (cfg.data.get("review") or {}).get("policy", "all") != "none":
                    errors.append(f"review.policy is on but no {stem} exists")
                continue
            v = te.base_vars(cfg, "x", "A", "opus", "PLAN.md")
            for k in ("__UB__", "__N__", "__MAX__", "__EXEC__", "__REASON__", "__RV__", "__CLAIM__", "__CHECKS__",
                      "__DIFFCMD__", "__REVIEWFILE__", "__VERDICTCMD__", "__PF__", "__SCOPE__", "__PLANS__",
                      "__PFREPORT__", "__WORKORDER__"):
                v.setdefault(k, "x")
            left = te.leftovers(te.finish(cfg, tp.read_text(), v))
            if left:
                warns.append(f"{tp.name} leaves unfilled placeholders: {' '.join(left)}")
            if "orch: no part" in te.finish(cfg, tp.read_text(), v):
                errors.append(f"{tp.name} includes a part that does not exist")
    except Exception as e:
        warns.append(f"template check skipped: {e}")
    for key, x in cfg.decision_roles():
        if not cfg.decides(x):
            errors.append(f"{key} is '{x}', a {cfg.flavor(x)} tier — a decision role needs a hard tier "
                          f"(a junior tier never unblocks, reviews, plans or stewards)")
    for x, fl in (cfg.data["executors"].get("flavors") or {}).items():
        if cfg.junior_only(x) and fl != "jrdev":
            errors.append(f"executors.flavors.{x}: {fl} — a Haiku tier is junior only; it never takes a judgment row")
    pinned = [k for k in ("opencode", "opencode_provider") if (cfg.data["executors"].get("models") or {}).get(k)]
    if pinned:
        warns.append(f"executors.models.{', '.join(pinned)} ignored — OpenCode runs its own configured default model")
    rp = (cfg.data.get("review") or {})
    for k in ("default_reviewer", "fallback_reviewer"):
        if rp.get(k) and rp[k] not in allowed:
            errors.append(f"review.{k} '{rp[k]}' is not in executors.allowed")

    # config sanity
    if cfg.data["guard"]["git_lock"] not in resources:
        errors.append(f"guard.git_lock '{cfg.data['guard']['git_lock']}' is not listed in guard.resources")
    if not (cfg.git_repo / ".git").exists():
        warns.append(f"guard.git_repo {cfg.git_repo} is not a git repository — `orch commit` will fail there")
    for s in cfg.data["guard"]["compliance_scanners"]:
        p = Path(s) if s.startswith("/") else cfg.instance / s
        if not p.exists():
            errors.append(f"compliance scanner '{s}' does not exist ({p})")
    # rounds (v3, drop D): the per-round executor policy and the native course loop
    try:
        import courseloop
        import rounds as rmod
        for tag, rd in rmod.load_rounds(cfg).items():
            for prob in rmod.policy_problems(cfg, rd.get("executors")):
                warns.append(f"round {tag}: {prob} — ignored")
            tiers = [k for k in (rd.get("executors") or {}) if k != "max_parallel"] if isinstance(rd.get("executors"), dict) else []
            if rd.get("by") and tiers:
                warns.append(f"round {tag} was made by session {rd.get('by')}: executors.{', '.join(tiers)} ignored "
                             f"(a session's round only narrows max_parallel)")
        for key, why in courseloop.configs(cfg)[1].items():    # every judge: and judges: entry (v3.1)
            warns.append(f"{why} — no course loop")
    except Exception as e:
        warns.append(f"rounds could not be checked ({type(e).__name__}: {e})")
    try:
        import fallback
        f_err, f_warn = fallback.problems(cfg)
        errors.extend(f_err)
        warns.extend(f_warn)
        if fallback.entries(cfg) and not (cfg.data.get("steward") or {}).get("enabled"):
            warns.append("executors.fallback is configured and the Steward lane is off: it still works (mechanical), "
                         "logged to steward/STEWARD_LOG.md the same way")
        sc = cfg.data.get("steward") or {}
        if fallback.entries(cfg) and sc.get("enabled") and fallback.PAGE_CLASS not in (sc.get("page_classes") or []):
            warns.append(f"executors.fallback is configured; steward.page_classes does not list '{fallback.PAGE_CLASS}', "
                         f"so its notification is a digest line, not a phone page")
    except Exception as e:
        warns.append(f"executors.fallback could not be checked ({type(e).__name__}: {e})")
    try:
        import steward
        d_ok, d_err = steward.duties(cfg)
        errors.extend(d_err)
        if d_ok and not steward.enabled(cfg):
            warns.append("steward.duties are configured but the Steward lane is off — no duty runs")
        sc = cfg.data.get("steward") or {}
        errors.extend(steward.cadence_validate(sc))
        if sc.get("enabled") and int(sc.get("idle_page_after") or 0) > 0:
            miss = [c for c in ("stall", "idle") if c not in (sc.get("page_classes") or [])]
            if miss:
                warns.append(f"steward.idle_page_after is set; steward.page_classes does not list {', '.join(miss)}, "
                             f"so that page is a digest line, not a phone page")
    except Exception as e:
        warns.append(f"steward.duties could not be checked ({type(e).__name__}: {e})")
    w = cfg.data["watchdog"]
    if w["enabled"] and not w["telegram_chat_id"]:
        warns.append("watchdog.enabled is true but no telegram_chat_id is set — push will be silent")

    print(f"instance   {cfg.instance}")
    print(f"workspace  {cfg.workspace}")
    print(f"root       {cfg.root}")
    print(f"git repo   {cfg.git_repo}")
    print(f"lanes      {len(lanes)} active ({len(rows)} rows), {len(archived_ids)} archived rows")
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
