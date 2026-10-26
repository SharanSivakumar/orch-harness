#!/usr/bin/env python3
"""`orch preflight` — review a round's PLANs BEFORE a single row is dispatched.

Retrospectives of long-running rigs found block-sheet stops caused by plan
defects that only surfaced at run time (sequencing gaps, wrong pins, exits a row
could not honestly reach), and others caused by tools keyed to one round's id
prefix. Each cost a dispatch and an unblocker. This catches the mechanical half before dispatch and hands the
judgment half to a plan-review session.

  orch preflight [--round <tag> | <ids…> | --pending]   static checks (always)
                 [--run]      also run each row's Verification block once, now,
                              before the row has done anything: a block that
                              already PASSES cannot tell done from not-done
                 [--review]   dispatch a plan-review session pf-<scope> (default
                              opus) over the PLANs and this report; a round with
                              preflight.required waits for its PASS

Static checks per row: the plan exists and is not marked DRAFT; it has an
Acceptance section and a Verification block; deps exist; `Rulings:` ids exist in
DECISIONS.md; every `orch hold <id> <res>` and `res=` names a declared resource;
no GO_ file is pre-released; the executor is allowed; paths the PLAN names under
the workspace exist (warning — a row may create them).

Report: <instance>/preflight/<scope>.md. Exit 0 clean · 1 errors.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, List, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config_parser import Config, load  # noqa: E402
from rows import QRow, load_rows  # noqa: E402
from store import Store  # noqa: E402

SECTION = re.compile(r"^#{1,4}\s*(?:\d+\s*[·.)-]?\s*)?(acceptance|verification|verify)\b", re.I | re.M)
DRAFT = re.compile(r"^\s*(?:\*\*)?status(?:\*\*)?\s*:?\s*(?:\*\*)?\s*draft\b", re.I | re.M)
HOLD = re.compile(r"orch\s+hold\s+\S+\s+([A-Za-z0-9_,.-]+)\s+--")
PATHISH = re.compile(r"`((?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.*-]+\.[A-Za-z0-9]{1,6})`")


def verification_block(text: str) -> str:
    """The first fenced code block under a Verification/Verify heading."""
    m = re.search(r"^#{1,4}[^\n]*\b(verification|verify)\b[^\n]*\n(.*?)(?=^#{1,4}\s|\Z)", text, re.I | re.M | re.S)
    if not m:
        return ""
    f = re.search(r"```(?:bash|sh|shell)?\n(.*?)```", m.group(2), re.S)
    return f.group(1).strip() if f else ""


def check_row(cfg: Config, st: Store, r: QRow, by_id: Dict[str, QRow], ledger_ids: set) -> Tuple[List[str], List[str]]:
    errs: List[str] = []
    warns: List[str] = list(r.problems)
    plan = cfg.workspace / r.plan
    if r.queue_exec not in cfg.data["executors"]["allowed"]:
        errs.append(f"executor '{r.queue_exec}' is not in executors.allowed")
    for d in r.deps:
        if d not in by_id:
            errs.append(f"depends on '{d}', which is in no queue")
    if r.gate and (cfg.control / f"GO_{r.id}").exists():
        errs.append(f"gated, but control/GO_{r.id} already exists — the gate is pre-released")
    res = cfg.resources()
    for x in r.resources:
        if x not in res:
            errs.append(f"res={x} is not a declared resource ({', '.join(res) or 'none'})")
    if not plan.is_file():
        errs.append(f"plan {r.plan} does not exist")
        return errs, warns
    text = plan.read_text(errors="replace")
    if DRAFT.search(text):
        errs.append("plan is marked DRAFT")
    secs = {m.group(1).lower() for m in SECTION.finditer(text)}
    if "acceptance" not in secs:
        errs.append("no Acceptance section — a reviewer cannot check what was never stated")
    if not ({"verification", "verify"} & secs):
        warns.append("no Verification section — the reviewer must invent the check")
    elif not verification_block(text):
        warns.append("Verification section has no fenced command block")
    for m in HOLD.finditer(text):
        for x in m.group(1).split(","):
            if x and x not in res:
                errs.append(f"`orch hold … {x}` names an undeclared resource")
    try:
        import decisions
        for rid in decisions.plan_rulings(text):
            if ledger_ids and rid not in ledger_ids:
                errs.append(f"Rulings: names {rid}, which is not in DECISIONS.md")
    except Exception:
        pass
    missing = []
    for m in PATHISH.finditer(text):
        p = m.group(1)
        if "*" in p or p.startswith(("http", "/")):
            continue
        if not ((cfg.workspace / p).exists() or (cfg.root / p).exists()):
            missing.append(p)
    if missing:
        warns.append(f"{len(missing)} named path(s) do not exist yet: {', '.join(missing[:5])}")
    if len(text) > 30000:
        warns.append(f"plan is {len(text) // 1024} KB — a lean row is code + test + short note")
    return errs, warns


def run_verification(cfg: Config, r: QRow, timeout: int) -> str:
    text = (cfg.workspace / r.plan).read_text(errors="replace") if (cfg.workspace / r.plan).is_file() else ""
    block = verification_block(text)
    if not block:
        return "no block"
    try:
        p = subprocess.run(["bash", "-c", block], cwd=str(cfg.root), capture_output=True, text=True,
                           timeout=timeout, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return f"timed out after {timeout}s"
    if p.returncode == 0:
        return "PASSES BEFORE THE ROW RAN — it may not be able to fail (make it falsifiable)"
    return f"fails now (exit {p.returncode}) — good, it can tell undone from done"


def main() -> int:
    ap = argparse.ArgumentParser(prog="orch preflight")
    ap.add_argument("ids", nargs="*")
    ap.add_argument("--round", default="")
    ap.add_argument("--pending", action="store_true")
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--review", action="store_true")
    a = ap.parse_args()
    cfg = load()
    import capability
    if a.review and capability.session(cfg):
        print(f"REFUSED: a session may not dispatch a plan review (session {capability.session(cfg)})")
        return 1
    st = Store(cfg.state)
    rows, by_id = load_rows(cfg)
    scope = a.round or ("pending" if a.pending or not a.ids else "rows-" + "-".join(a.ids[:3]))
    if a.round:
        import rounds
        r = rounds.load_rounds(cfg).get(a.round)
        if not r:
            print(f"no round {a.round}")
            return 1
        lanes = [str(x) for x in r.get("lanes", [])]
        sel = [x for x in rows if x.lane in lanes and not x.archived]
    elif a.ids:
        sel = [by_id[i] for i in a.ids if i in by_id]
    else:
        sel = [x for x in rows if not x.archived and not st.done(x.id)]
    ledger_ids: set = set()
    try:
        import decisions
        led = cfg.workspace / "DECISIONS.md"
        if led.is_file():
            ledger_ids = {e.id for e in decisions.load_entries(led)}
    except Exception:
        pass
    out = [f"# Preflight — {scope}", "", f"{time.strftime('%F %T')} · {len(sel)} row(s) · "
           f"{'with' if a.run else 'without'} verification runs", ""]
    n_err = 0
    timeout = int((cfg.data.get("preflight") or {}).get("run_timeout", 300))
    for r in sel:
        errs, warns = check_row(cfg, st, r, by_id, ledger_ids)
        line = f"## {r.id}  ({r.lane}, {r.queue_exec}) — `{r.plan}`"
        out.append(line)
        for e in errs:
            out.append(f"- **ERROR** {e}")
        for w in warns:
            out.append(f"- warn {w}")
        if a.run and (r.meta.get("preflight") == "run" or a.run):
            out.append(f"- verification now: {run_verification(cfg, r, timeout)}")
        if not errs and not warns:
            out.append("- clean")
        out.append("")
        n_err += len(errs)
    out.append(f"**{n_err} error(s)** across {len(sel)} row(s).")
    d = cfg.instance / "preflight"
    d.mkdir(exist_ok=True)
    rep = d / f"{scope}.md"
    rep.write_text("\n".join(out) + "\n")
    print("\n".join(out))
    print(f"\nreport: {rep}")
    if a.review:
        from engine import Rig
        import template_engine as te
        rig = Rig(cfg)
        pf = f"pf-{scope}"
        st.set_aside(pf, f"old{int(time.time())}")
        reviewer = (cfg.data.get("preflight") or {}).get("reviewer", "opus")
        plans = [str(Path(cfg.ws_prefix) / x.plan) if cfg.ws_prefix else x.plan for x in sel]
        order = te.write_planreview(cfg, pf, scope, reviewer, plans, te.rel_to_root(cfg, rep))
        extra = {"scope": scope}
        if a.round:
            import rounds as rmod
            extra["plans_sha"] = rmod.round_plans_sha(cfg, a.round)
        ok, how = rig.dispatch_sub(pf, reviewer, order, "", "", extra)
        print(f"plan review {pf}: {'dispatched (' + reviewer + ')' if ok else 'NOT dispatched — ' + how}")
    return 1 if n_err else 0


if __name__ == "__main__":
    sys.exit(main())
