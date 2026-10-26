#!/usr/bin/env python3
"""Prompt/work-order rendering for the orchestration framework.

Templates are looked up instance-first, so a rig can override any work order
without forking the framework:

    <instance>/prompts/_TEMPLATE_HARD.md      (instance override)
    <framework>/templates/_TEMPLATE_HARD.md   (shipped default)

Variables available in every template and kick file:

    __ID__        row id                 __LANE__      lane letter
    __MODEL__     resolved executor      __FLAVOR__    hard|jrdev|codex|manual
    __PLAN__      plan path as the executor sees it, relative to project root
    __PLAN_WS__   plan path relative to the workspace (as written in the queue)
    __PLAN_DIR__  the plan's folder, relative to project root
    __WORKSPACE__ workspace path relative to project root ('' if identical)
    __ORCHDIR__   instance path relative to project root
    __REPORTS__   reports dir relative to project root
    __PROJECT__   project name           __TMUX__      tmux session
    __SENTINEL__  the exact path the row must write to close itself
    __CLOSECMD__  the exact command line that closes the row
    __BLOCKCMD__  the exact command line that blocks the row
    __ESCALATECMD__ the exact command line that stops the row ROUTE_A
    __ATTEMPT__   attempt number (1 on a first dispatch)
    __ROUND__     the row's round tag ('' when its lane is in no round)
    __BASE_RULES__   <workspace>/rules/BASE.md if it exists, else ''
    __ROUND_RULES__  <instance>/rounds/<round>/RULES.md if it exists, else ''
    __RULINGS_BLOCK__      the full text of the rulings the PLAN lists on a
                           `Rulings:` line (lib/decisions.py), else ''
    __PREVIOUS_ATTEMPTS__  what earlier attempts of this row ran into, else ''

Unblocker templates additionally get __UB__, __N__, __MAX__, __EXEC__,
__REASON__; reviewer templates __RV__, __CLAIM__, __CHECKS__, __DIFFCMD__,
__REVIEWFILE__, __VERDICTCMD__; plan-review templates __PF__, __SCOPE__,
__PLANS__, __PFREPORT__, __REVIEWFILE__, __VERDICTCMD__.

When a template does not place __RULINGS_BLOCK__ or __PREVIOUS_ATTEMPTS__ and
the block is non-empty, it is appended at the end — the executor always sees it.

Conditionals: `{{?__VAR__ text}}` keeps `text` only when __VAR__ is non-empty
(e.g. a "read the round rules" line in a rig that has none disappears).

Includes: a line `{{> name}}` is replaced by `_PART_<name>.md` (instance
prompts/ first, then the framework's templates/parts/<name>.md), so a clause
shared by every template (a never-open list, a long-jobs rule) lives once.

Generated work orders start with an `orch:generated` marker and are re-rendered
at every dispatch. A work order without the marker was hand-written
and is never overwritten.

__SUB__ is kept as an alias of __ID__ so older templates port verbatim.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config_parser import load, Config  # noqa: E402

FRAMEWORK = Path(__file__).resolve().parent.parent
MARKER = "<!-- orch:generated — re-rendered at every dispatch; delete this line to hand-edit -->"
INCLUDE = re.compile(r"^[ \t]*\{\{>\s*([A-Za-z0-9_.-]+)\s*\}\}[ \t]*$", re.M)
COND = re.compile(r"\{\{\?(__[A-Z][A-Z0-9_]+__)[ \t]+(.*?)\}\}", re.S)
LEFTOVER = re.compile(r"__[A-Z][A-Z0-9_]{2,}__")


def rel_to_root(cfg: Config, p: Path) -> str:
    try:
        return str(p.resolve().relative_to(cfg.root))
    except ValueError:
        return str(p.resolve())


def template_path(cfg: Config, stem: str) -> Optional[Path]:
    """Instance override wins over the shipped default; None if neither exists."""
    for cand in (cfg.instance / "prompts" / stem,
                 cfg.instance / "templates" / stem,
                 FRAMEWORK / "templates" / stem):
        if cand.is_file():
            return cand
    return None


def find_template(cfg: Config, stem: str) -> Path:
    p = template_path(cfg, stem)
    if p is None:
        raise SystemExit(f"orch: no template '{stem}' in instance prompts/ or {FRAMEWORK/'templates'}")
    return p


def expand_includes(cfg: Config, text: str, depth: int = 0) -> str:
    if depth > 5:
        return text

    def sub(m: re.Match) -> str:
        name = m.group(1)
        for cand in (cfg.instance / "prompts" / f"_PART_{name}.md",
                     FRAMEWORK / "templates" / "parts" / f"{name}.md"):
            if cand.is_file():
                return expand_includes(cfg, cand.read_text().rstrip("\n"), depth + 1)
        return f"<!-- orch: no part '{name}' -->"
    return INCLUDE.sub(sub, text)


def kick_stems(executor: str, flavor: str) -> List[str]:
    """An executor's own _KICK_<EXECUTOR>.txt wins over its flavor's
    _KICK_<FLAVOR>.txt, so two junior executors can carry different binding
    lines over one shared work order."""
    return [f"_KICK_{executor.upper()}.txt", f"_KICK_{flavor.upper()}.txt"]


def kicked(cfg: Config, executor: str) -> bool:
    """Pasted (paste_kick) or passed as the launch prompt (arg_kick)."""
    ex = cfg.data["executors"]
    return executor in (ex.get("paste_kick") or []) or executor in (ex.get("arg_kick") or [])


def round_of_lane(cfg: Config, lane: str) -> str:
    try:
        import rounds
        return rounds.round_of_lane(cfg, lane)
    except Exception:
        return ""


def base_vars(cfg: Config, rid: str, lane: str, executor: str, plan_ws: str) -> Dict[str, str]:
    ws = cfg.ws_prefix
    plan_root = f"{ws}/{plan_ws}" if ws else plan_ws
    orchdir = rel_to_root(cfg, cfg.instance)
    reports = f"{ws}/{cfg.reports_dir}" if ws else cfg.reports_dir
    sentinel = f"{orchdir}/state/{rid}.status"
    rnd = round_of_lane(cfg, lane) if lane and lane != "?" else ""
    base_rules = cfg.workspace / "rules" / "BASE.md"
    round_rules = cfg.instance / "rounds" / rnd / "RULES.md" if rnd else None
    plan_dir = str(Path(plan_root).parent) if plan_ws else ""
    return {
        "__ID__": rid, "__SUB__": rid, "__LANE__": lane,
        "__MODEL__": executor, "__FLAVOR__": cfg.flavor(executor),
        "__PLAN__": plan_root, "__PLAN_WS__": plan_ws, "__PLAN_DIR__": plan_dir,
        "__WORKSPACE__": ws, "__ORCHDIR__": orchdir, "__REPORTS__": reports,
        "__PROJECT__": cfg.name, "__TMUX__": cfg.tmux_session,
        "__SENTINEL__": sentinel,
        "__CLOSECMD__": f"cd {orchdir} && ./orch done {rid} \"<one-line summary>\"",
        "__BLOCKCMD__": f"cd {orchdir} && ./orch blocked {rid} \"<precise reason>\"",
        "__ESCALATECMD__": f"cd {orchdir} && ./orch escalate {rid} \"<why only the operator can decide this>\"",
        # spend.enabled (lib/spend.py): the paid-command paragraph of the close part
        "__SPENDCMD__": (f"cd {orchdir} && ./orch spend run --provider <provider> --est <USD> [--envelope <id>] -- <command…>"
                         if (cfg.data.get("spend") or {}).get("enabled") else ""),
        "__SPENDBLOCK__": f"cd {orchdir} && ./orch blocked {rid} \"SPEND_WAIT <id>\"",
        "__ATTEMPT__": "1", "__ROUND__": rnd,
        "__BASE_RULES__": rel_to_root(cfg, base_rules) if base_rules.is_file() else "",
        "__ROUND_RULES__": rel_to_root(cfg, round_rules) if round_rules and round_rules.is_file() else "",
        "__RULINGS_BLOCK__": "", "__PREVIOUS_ATTEMPTS__": "",
    }


def rulings_block(cfg: Config, plan_ws: str) -> str:
    """Full text of the rulings a PLAN lists on its `Rulings:` line."""
    try:
        import decisions
        plan = cfg.workspace / plan_ws
        if not plan.is_file():
            return ""
        ids = decisions.plan_rulings(plan.read_text(errors="replace"))
        if not ids:
            return ""
        return decisions.binding_block(cfg.workspace, ids)
    except Exception as e:  # never fail a dispatch over the digest
        return f"<!-- orch: rulings block unavailable ({type(e).__name__}) -->"


def previous_attempts(cfg: Config, rid: str) -> str:
    """What earlier attempts of this row ran into, from attempts.jsonl and the
    files the engine and its sub-sessions left behind."""
    try:
        from store import Store
        st = Store(cfg.state)
        lines: List[str] = []
        for e in st.events(rid):
            ev = e.get("event")
            if ev == "claim" and e.get("verdict") in ("BLOCKED", "ROUTE_A"):
                lines.append(f"- attempt {e.get('attempt')}: you (or a predecessor) stopped BLOCKED — {e.get('summary', '')}")
            elif ev == "check_failed":
                lines.append(f"- attempt {e.get('attempt')}: the close checks refused the CLOSED claim — {e.get('detail', '')}")
            elif ev == "review_verdict" and e.get("verdict") == "REJECT":
                lines.append(f"- attempt {e.get('attempt')}: the independent reviewer REJECTED the work — "
                             f"{e.get('summary', '')} (findings: {e.get('file', '')})")
            elif ev == "unblock_verdict":
                lines.append(f"- attempt {e.get('attempt')}: unblocker {e.get('session')} returned "
                             f"{e.get('verdict')} — {e.get('summary', '')} (analysis: {e.get('file', '')})")
            elif ev == "dead":
                lines.append(f"- attempt {e.get('attempt')}: the session died without a claim (exit {e.get('exit', '?')})")
            elif ev == "timeout":
                lines.append(f"- attempt {e.get('attempt')}: exceeded its time budget — {e.get('detail', '')}")
            elif ev == "retier":
                lines.append(f"- retiered to {e.get('to')} — {e.get('detail', '')}")
        if not lines:
            return ""
        return ("## Previous attempts — read these first\n\nThis row has run before. Do not repeat what "
                "failed; open the named files.\n\n" + "\n".join(lines[-12:]) + "\n")
    except Exception:
        return ""


def render(text: str, vars: Dict[str, str]) -> str:
    """One pass: a value is never itself rendered (a summary or ruling text that
    contains `__ID__` stays literal). Longest keys first in the alternation."""
    if not vars:
        return text
    pat = re.compile("|".join(re.escape(k) for k in sorted(vars, key=len, reverse=True)))
    return pat.sub(lambda m: vars[m.group(0)], text)


def conditionals(text: str, v: Dict[str, str]) -> str:
    """`{{?__VAR__ text}}` keeps `text` only when __VAR__ renders non-empty."""
    return COND.sub(lambda m: m.group(2) if v.get(m.group(1)) else "", text)


def finish(cfg: Config, text: str, v: Dict[str, str], marker: bool = True) -> str:
    raw = conditionals(expand_includes(cfg, text), v)
    out = render(raw, v)
    for key in ("__RULINGS_BLOCK__", "__PREVIOUS_ATTEMPTS__"):
        if v.get(key) and key not in raw:
            out = out.rstrip("\n") + "\n\n" + v[key].rstrip("\n") + "\n"
    return (MARKER + "\n" + out) if marker else out


def leftovers(text: str) -> List[str]:
    return sorted(set(LEFTOVER.findall(text)))


def is_generated(p: Path) -> bool:
    try:
        with open(p) as f:
            return "orch:generated" in f.readline()
    except Exception:
        return False


def write_prompt(cfg: Config, rid: str, lane: str, executor: str, plan_ws: str,
                 force: bool = False, attempt: int = 1) -> Path:
    flavor = cfg.flavor(executor)
    out = cfg.prompts / f"{rid}.{flavor}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    v = base_vars(cfg, rid, lane, executor, plan_ws)
    v["__ATTEMPT__"] = str(attempt)
    v["__RULINGS_BLOCK__"] = rulings_block(cfg, plan_ws)
    v["__PREVIOUS_ATTEMPTS__"] = previous_attempts(cfg, rid) if attempt > 1 else ""
    hand_written = out.is_file() and out.stat().st_size and not is_generated(out)
    if force or not hand_written:
        tpl = find_template(cfg, f"_TEMPLATE_{flavor.upper()}.md")
        out.write_text(finish(cfg, tpl.read_text(), v))
    elif v["__PREVIOUS_ATTEMPTS__"]:
        # a hand-written order is never rewritten, but the retry context still reaches the executor
        side = cfg.prompts / f"{rid}.previous.md"
        side.write_text(v["__PREVIOUS_ATTEMPTS__"])
    # kick-driven executors (pasted or passed as the launch prompt) need a short
    # kick text pointing at the work order; rewritten at every dispatch
    if kicked(cfg, executor):
        kick = cfg.prompts / f"{rid}.kick.txt"
        v2 = dict(v, __WORKORDER__=f"{rel_to_root(cfg, out)}", __RULINGS_BLOCK__="", __PREVIOUS_ATTEMPTS__="")
        stems = kick_stems(executor, flavor)
        ktpl = next((p for p in (template_path(cfg, s) for s in stems) if p), None)
        if ktpl is None:
            raise SystemExit(f"orch: no kick template {' or '.join(stems)} in instance prompts/ or {FRAMEWORK/'templates'}")
        kick.write_text(finish(cfg, ktpl.read_text(), v2, marker=False))
    return out


def _sub_order(cfg: Config, name: str, stem: str, kick_stem: str, v: Dict[str, str], executor: str) -> Path:
    flavor = cfg.flavor(executor)
    out = cfg.prompts / f"{name}.{flavor}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(finish(cfg, find_template(cfg, stem).read_text(), v))
    kt = template_path(cfg, kick_stem)
    if kt is not None:
        v2 = dict(v, __WORKORDER__=rel_to_root(cfg, out))
        (cfg.prompts / f"{name}.kick.txt").write_text(finish(cfg, kt.read_text(), v2, marker=False))
    return out


def unblock_executor(cfg: Config, n: int, lane: str = "") -> str:
    """Unblocker n's executor: the round's `executors.unblock_executors` (v3) for a row of `lane`, else the rig's."""
    if lane:
        import rounds
        ex = rounds.exec_policy(cfg, lane)
    else:
        ex = cfg.data["executors"]
    chain = [x for x in (ex.get("unblock_executors") or []) if x]
    if chain:
        return chain[min(n, len(chain)) - 1]
    return ex["unblock_executor"]


def write_unblock(cfg: Config, rid: str, lane: str, n: int, executor: str,
                  plan_ws: str, reason: str, ubx: str = "") -> Path:
    ub = f"ub-{rid}-{n}"
    ubx = ubx or unblock_executor(cfg, n)
    v = base_vars(cfg, rid, lane, executor, plan_ws)
    v.update({"__UB__": ub, "__N__": str(n),
              "__MAX__": str(cfg.data["executors"]["max_unblock_attempts"]),
              "__EXEC__": executor, "__REASON__": " ".join(reason.split()),
              "__VERDICTCMD__": f"cd {v['__ORCHDIR__']} && ./orch verdict {ub} RESOLVED|ESCALATE \"<one sentence>\"",
              "__PREVIOUS_ATTEMPTS__": previous_attempts(cfg, rid),
              "__RULINGS_BLOCK__": rulings_block(cfg, plan_ws)})
    return _sub_order(cfg, ub, "_TEMPLATE_UNBLOCK.md", "_KICK_UNBLOCK.txt", v, ubx)


def write_review(cfg: Config, rid: str, lane: str, rv: str, reviewer: str, executor: str,
                 plan_ws: str, claim: str, checks: str, diffcmd: str, attempt: int) -> Path:
    v = base_vars(cfg, rid, lane, executor, plan_ws)
    reviewfile = rel_to_root(cfg, cfg.instance / "reviews" / f"{rv[3:]}.md")
    (cfg.instance / "reviews").mkdir(exist_ok=True)
    v.update({"__RV__": rv, "__EXEC__": executor, "__MODEL__": reviewer, "__CLAIM__": " ".join(claim.split()),
              "__CHECKS__": checks, "__DIFFCMD__": diffcmd, "__REVIEWFILE__": reviewfile,
              "__ATTEMPT__": str(attempt),
              "__VERDICTCMD__": f"cd {v['__ORCHDIR__']} && ./orch verdict {rv} ACCEPT|REJECT \"<one sentence>\"",
              "__RULINGS_BLOCK__": rulings_block(cfg, plan_ws),
              "__PREVIOUS_ATTEMPTS__": previous_attempts(cfg, rid)})
    return _sub_order(cfg, rv, "_TEMPLATE_REVIEW.md", "_KICK_REVIEW.txt", v, reviewer)


def write_planreview(cfg: Config, pf: str, scope: str, reviewer: str, plans: List[str],
                     report: str) -> Path:
    v = base_vars(cfg, pf, "?", reviewer, "")
    reviewfile = rel_to_root(cfg, cfg.instance / "preflight" / f"{scope}.review.md")
    (cfg.instance / "preflight").mkdir(exist_ok=True)
    v.update({"__PF__": pf, "__SCOPE__": scope, "__PLANS__": "\n".join(f"- `{p}`" for p in plans),
              "__PFREPORT__": report, "__REVIEWFILE__": reviewfile,
              "__VERDICTCMD__": f"cd {v['__ORCHDIR__']} && ./orch verdict {pf} PASS|FIX \"<one sentence>\""})
    return _sub_order(cfg, pf, "_TEMPLATE_PLANREVIEW.md", "_KICK_PLANREVIEW.txt", v, reviewer)


def write_roundreview(cfg: Config, rr: str, tag: str, reviewer: str, report: str, loop: str = "",
                      briefdir: Optional[Path] = None) -> Path:
    """briefdir: where the review writes (default rounds/<tag>/review/; a checkpoint's loop: review+<judge>/)."""
    v = base_vars(cfg, rr, "?", reviewer, "")
    d = cfg.instance / "rounds" / tag
    bd = briefdir or (d / "review")
    bd.mkdir(parents=True, exist_ok=True)
    rnd = d / "RULES.md"
    v.update({"__RR__": rr, "__ROUND__": tag, "__REPORT__": report,
              "__ROUND_YAML__": rel_to_root(cfg, d / "round.yaml"),
              "__ROUND_RULES__": rel_to_root(cfg, rnd) if rnd.is_file() else "",
              "__BRIEFDIR__": rel_to_root(cfg, bd),
              "__SCOREBOARD__": rel_to_root(cfg, cfg.instance / "scoreboard.tsv"),
              "__BLOCKSHEET__": rel_to_root(cfg, cfg.instance / "BLOCK_SHEET.md"), "__LOOP__": loop,
              "__VERDICTCMD__": f"cd {v['__ORCHDIR__']} && ./orch verdict {rr} DONE \"<one sentence>\""})
    return _sub_order(cfg, rr, "_TEMPLATE_ROUNDREVIEW.md", "_KICK_ROUNDREVIEW.txt", v, reviewer)


def rights_file(cfg: Config) -> Path:
    """The rig's decision-rights table (steward.rights_file, relative to the instance), else the
    framework's generic default (everything non-mechanical is W)."""
    rel = str((cfg.data.get("steward") or {}).get("rights_file") or "steward/RIGHTS.md")
    p = cfg.instance / rel
    return p if p.is_file() else FRAMEWORK / "templates" / "steward" / "RIGHTS.md"


def write_steward(cfg: Config, sid: str, inc: Dict, executor: str, bundle: str) -> Path:
    import steward
    v = base_vars(cfg, sid, "SW", executor, "")
    v.update({"__SW__": sid, "__INCIDENT__": str(inc["n"]), "__KIND__": str(inc["kind"]),
              "__KLASS__": str(inc["klass"]), "__SUBJECT__": str(inc["subject"]),
              "__SUMMARY__": " ".join(str(inc.get("summary", "")).split()), "__BUNDLE__": bundle,
              "__RIGHTS__": rel_to_root(cfg, rights_file(cfg)),
              "__ANALYSIS__": rel_to_root(cfg, steward.evidence_file(cfg, sid)),
              "__PAGE_CLASSES__": ", ".join(str(c) for c in (cfg.data.get("steward") or {}).get("page_classes") or []),
              "__VERDICTCMD__": f"cd {v['__ORCHDIR__']} && ./orch verdict {sid} RESOLVED|PARKED|INBOX|ESCALATE \"<one sentence>\""})
    return _sub_order(cfg, sid, "_TEMPLATE_STEWARD.md", "_KICK_STEWARD.txt", v, executor)


def write_panel(cfg: Config, sid: str, inc: Dict, executor: str, bundle: str, role: str) -> Path:
    """The panel tier's work orders (lib/panel.py): role 'decider' or 'falsifier'."""
    import panel
    import steward
    n = inc["n"]
    v = base_vars(cfg, sid, "SW", executor, "")
    p = inc.get("panel") or {}
    verdicts = "CONFIRM|OBJECT" if role == "falsifier" else "RESOLVED|PARKED|INBOX|ESCALATE"
    v.update({"__SW__": sid, "__INCIDENT__": str(n), "__KIND__": str(inc["kind"]), "__SUBJECT__": str(inc["subject"]),
              "__SUMMARY__": " ".join(str(inc.get("summary", "")).split()), "__BUNDLE__": bundle,
              "__PANELWHY__": " ".join(str(p.get("why", "")).split()) or "an incident of class panel",
              "__RIGHTS__": rel_to_root(cfg, rights_file(cfg)),
              "__PROPOSAL__": rel_to_root(cfg, panel.proposal_file(cfg, n)),
              "__STAGED__": rel_to_root(cfg, panel.staged_view(cfg, n)),
              "__DECIDER__": str(p.get("decider", "")),
              "__ANALYSIS__": rel_to_root(cfg, steward.evidence_file(cfg, sid)),
              "__PAGE_CLASSES__": ", ".join(str(c) for c in (cfg.data.get("steward") or {}).get("page_classes") or []),
              "__VERDICTCMD__": f"cd {v['__ORCHDIR__']} && ./orch verdict {sid} {verdicts} \"<one sentence>\""})
    stem = "FALSIFIER" if role == "falsifier" else "DECIDER"
    return _sub_order(cfg, sid, f"_TEMPLATE_PANEL_{stem}.md", f"_KICK_PANEL_{stem}.txt", v, executor)


def write_planner(cfg: Config, sid: str, tag: str, executor: str, brief: str, inc_n) -> Path:
    import steward
    v = base_vars(cfg, sid, "SW", executor, "")
    v.update({"__PL__": sid, "__ROUND__": tag, "__BRIEF__": brief, "__INCIDENT__": str(inc_n),
              "__RIGHTS__": rel_to_root(cfg, rights_file(cfg)),
              "__PLANGUIDE__": str(FRAMEWORK / "docs" / "PLANNING_AGENT_GUIDE.md"),
              "__EVIDENCE__": rel_to_root(cfg, steward.planner_evidence(cfg, sid)),
              "__VERDICTCMD__": f"cd {v['__ORCHDIR__']} && ./orch verdict {sid} PASS|FAIL \"<one sentence>\""})
    return _sub_order(cfg, sid, "_TEMPLATE_PLANNER.md", "_KICK_PLANNER.txt", v, executor)


def nudge_text(cfg: Config, sid: str, kind: str, lane: str, executor: str, plan_ws: str) -> str:
    v = base_vars(cfg, sid, lane, executor, plan_ws)
    v["__VERDICTCMD__"] = f"cd {v['__ORCHDIR__']} && ./orch verdict {sid} <VERDICT> \"<one sentence>\""
    stem = {"unblock": "_NUDGE_UNBLOCK.txt", "review": "_NUDGE_REVIEW.txt",
            "preflight": "_NUDGE_REVIEW.txt", "steward": "_NUDGE_STEWARD.txt",
            "planner": "_NUDGE_STEWARD.txt"}.get(kind, "_NUDGE.txt")
    if kind == "steward" and re.fullmatch(r"sw-\d+-f\d+", sid):
        stem = "_NUDGE_FALSIFIER.txt"
    if kind == "steward" and re.fullmatch(r"sw-\d+-d\d+", sid):
        stem = "_NUDGE_DECIDER.txt"
    p = template_path(cfg, stem) or template_path(cfg, "_NUDGE.txt")
    if p is None:
        return ""
    return " ".join(finish(cfg, p.read_text(), v, marker=False).split())


def main() -> None:
    ap = argparse.ArgumentParser(prog="template_engine")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("prompt")
    p.add_argument("--id", required=True); p.add_argument("--lane", default="?")
    p.add_argument("--exec", dest="ex", required=True); p.add_argument("--plan", required=True)
    p.add_argument("--force", action="store_true"); p.add_argument("--attempt", type=int, default=1)
    u = sub.add_parser("unblock")
    u.add_argument("--id", required=True); u.add_argument("--lane", default="?")
    u.add_argument("--n", type=int, required=True); u.add_argument("--exec", dest="ex", required=True)
    u.add_argument("--plan", required=True); u.add_argument("--reason", default="")
    a = ap.parse_args()
    cfg = load()
    if a.cmd == "prompt":
        print(write_prompt(cfg, a.id, a.lane, a.ex, a.plan, a.force, a.attempt))
    else:
        print(write_unblock(cfg, a.id, a.lane, a.n, a.ex, a.plan, a.reason))


if __name__ == "__main__":
    main()
