#!/usr/bin/env python3
"""Prompt/work-order interpolation for the orchestration framework.

Replaces an earlier approach of a `sed -e s|__SUB__|...|g` chain plus two
1,500-character kick strings inlined in the watcher. Templates are looked up
instance-first, so a cycle can override any work order without forking the
framework:

    <instance>/prompts/_TEMPLATE_LEAD.md      (instance override)
    <framework>/templates/_TEMPLATE_LEAD.md   (shipped default)

Variables available in every template and kick file:

    __ID__        row id                 __LANE__      lane letter
    __MODEL__     resolved executor      __FLAVOR__    lead|scoped|codex|manual
    __PLAN__      plan path as the executor sees it, relative to project root
    __PLAN_WS__   plan path relative to the workspace (as written in the queue)
    __WORKSPACE__ workspace path relative to project root ('' if identical)
    __ORCHDIR__   instance path relative to project root
    __REPORTS__   reports dir relative to project root
    __PROJECT__   project name           __TMUX__      tmux session
    __SENTINEL__  the exact path the row must write to close itself
    __CLOSECMD__  the exact command line that closes the row
    __BLOCKCMD__  the exact command line that blocks the row

Unblocker templates additionally get __UB__, __N__, __MAX__, __EXEC__,
__REASON__.

__SUB__ is kept as an alias of __ID__ so older templates port verbatim.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config_parser import load, Config  # noqa: E402

FRAMEWORK = Path(__file__).resolve().parent.parent


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


def kick_stems(executor: str, flavor: str) -> List[str]:
    """An executor's own _KICK_<EXECUTOR>.txt wins over its flavor's
    _KICK_<FLAVOR>.txt, so two scoped executors can carry different binding
    lines over one shared work order."""
    return [f"_KICK_{executor.upper()}.txt", f"_KICK_{flavor.upper()}.txt"]


def kicked(cfg: Config, executor: str) -> bool:
    """Pasted (paste_kick) or passed as the launch prompt (arg_kick)."""
    ex = cfg.data["executors"]
    return executor in (ex.get("paste_kick") or []) or executor in (ex.get("arg_kick") or [])


def base_vars(cfg: Config, rid: str, lane: str, executor: str, plan_ws: str) -> Dict[str, str]:
    ws = cfg.ws_prefix
    plan_root = f"{ws}/{plan_ws}" if ws else plan_ws
    orchdir = rel_to_root(cfg, cfg.instance)
    reports = f"{ws}/{cfg.reports_dir}" if ws else cfg.reports_dir
    sentinel = f"{orchdir}/state/{rid}.status"
    return {
        "__ID__": rid, "__SUB__": rid, "__LANE__": lane,
        "__MODEL__": executor, "__FLAVOR__": cfg.flavor(executor),
        "__PLAN__": plan_root, "__PLAN_WS__": plan_ws,
        "__WORKSPACE__": ws, "__ORCHDIR__": orchdir, "__REPORTS__": reports,
        "__PROJECT__": cfg.name, "__TMUX__": cfg.tmux_session,
        "__SENTINEL__": sentinel,
        "__CLOSECMD__": f"cd {orchdir} && ./orch done {rid} \"one-line summary\"",
        "__BLOCKCMD__": f"cd {orchdir} && ./orch blocked {rid} \"precise reason\"",
    }


def render(text: str, vars: Dict[str, str]) -> str:
    for k, v in vars.items():
        text = text.replace(k, v)
    return text


def write_prompt(cfg: Config, rid: str, lane: str, executor: str, plan_ws: str,
                 force: bool = False) -> Path:
    flavor = cfg.flavor(executor)
    out = cfg.prompts / f"{rid}.{flavor}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    v = base_vars(cfg, rid, lane, executor, plan_ws)
    if force or not (out.is_file() and out.stat().st_size):
        tpl = find_template(cfg, f"_TEMPLATE_{flavor.upper()}.md")
        out.write_text(render(tpl.read_text(), v))
    # kick-driven executors (pasted or passed as the launch prompt) need a short
    # kick text pointing at the work order; rewritten at every dispatch
    if kicked(cfg, executor):
        kick = cfg.prompts / f"{rid}.kick.txt"
        v2 = dict(v, __WORKORDER__=f"{rel_to_root(cfg, out)}")
        stems = kick_stems(executor, flavor)
        ktpl = next((p for p in (template_path(cfg, s) for s in stems) if p), None)
        if ktpl is None:
            raise SystemExit(f"orch: no kick template {' or '.join(stems)} in instance prompts/ or {FRAMEWORK/'templates'}")
        kick.write_text(render(ktpl.read_text(), v2))
    return out


def write_unblock(cfg: Config, rid: str, lane: str, n: int, executor: str,
                  plan_ws: str, reason: str) -> Path:
    ub = f"ub-{rid}-{n}"
    flavor = cfg.flavor(cfg.data["executors"]["unblock_executor"])
    out = cfg.prompts / f"{ub}.{flavor}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    v = base_vars(cfg, rid, lane, executor, plan_ws)
    v.update({"__UB__": ub, "__N__": str(n),
              "__MAX__": str(cfg.data["executors"]["max_unblock_attempts"]),
              "__EXEC__": executor, "__REASON__": reason.replace("\n", " ")})
    out.write_text(render(find_template(cfg, "_TEMPLATE_UNBLOCK.md").read_text(), v))
    v["__WORKORDER__"] = rel_to_root(cfg, out)
    (cfg.prompts / f"{ub}.kick.txt").write_text(
        render(find_template(cfg, "_KICK_UNBLOCK.txt").read_text(), v))
    return out


def main() -> None:
    ap = argparse.ArgumentParser(prog="template_engine")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("prompt")
    p.add_argument("--id", required=True); p.add_argument("--lane", default="?")
    p.add_argument("--exec", dest="ex", required=True); p.add_argument("--plan", required=True)
    p.add_argument("--force", action="store_true")
    u = sub.add_parser("unblock")
    u.add_argument("--id", required=True); u.add_argument("--lane", default="?")
    u.add_argument("--n", type=int, required=True); u.add_argument("--exec", dest="ex", required=True)
    u.add_argument("--plan", required=True); u.add_argument("--reason", default="")
    a = ap.parse_args()
    cfg = load()
    if a.cmd == "prompt":
        print(write_prompt(cfg, a.id, a.lane, a.ex, a.plan, a.force))
    else:
        print(write_unblock(cfg, a.id, a.lane, a.n, a.ex, a.plan, a.reason))


if __name__ == "__main__":
    main()
