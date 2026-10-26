#!/usr/bin/env python3
"""orch setup — turn a fresh instance into a configured rig, with the Owner's answers.

    orch setup questions [--preset P] [--json]     the behaviour switches to ask about
    orch setup apply [--answer id=opt[,opt…]]… [--set dotted.key=value]…
    orch setup defaults --preset P                 apply a preset's setup.defaults.yaml (init does this)
    orch setup rules [--preset P]                  write <workspace>/rules/BASE.md if it is missing
    orch setup card                                write <instance>/RIG.md (the hand-off to planning sessions)
    orch setup check                               is this rig ready for a planning session?

`apply` edits orch.yaml in place and keeps its comments: a key's value is replaced on its
own line, a missing key is inserted under its parent. Every write is re-read and checked.
Answers are recorded in <instance>/setup.answers.yaml, which the rig card reports.

The setup procedure for an agent is docs/SETUP_AGENT_GUIDE.md.
"""
from __future__ import annotations

import argparse
import json
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
from config_parser import CONFIG_NAME, DEFAULTS, Config, _merge, find_instance  # noqa: E402

HOME = Path(__file__).resolve().parent.parent
QUESTIONS = HOME / "templates" / "setup" / "questions.yaml"
ANSWERS = "setup.answers.yaml"
SKELETON_MARK = "TODO(setup)"


# -- comment-preserving YAML edits ------------------------------------------------
def _fmt(value: Any) -> str:
    """A value as one line of YAML (JSON is valid YAML flow style)."""
    return json.dumps(value, ensure_ascii=False)


def _split_comment(rest: str) -> Tuple[str, str]:
    """Split ' value  # comment' into (value, '  # comment'), ignoring '#' inside quotes."""
    q = None
    for i, ch in enumerate(rest):
        if q:
            if ch == q:
                q = None
        elif ch in "\"'":
            q = ch
        elif ch == "#" and (i == 0 or rest[i - 1] in " \t"):
            j = i
            while j > 0 and rest[j - 1] in " \t":
                j -= 1
            return rest[:j], rest[j:]
    return rest, ""


def _content(line: str) -> Optional[Tuple[int, str]]:
    s = line.lstrip(" ")
    if not s.strip() or s.startswith("#"):
        return None
    return len(line) - len(s), s


def _get(data: Any, parts: List[str]) -> Any:
    for p in parts:
        if not isinstance(data, dict) or p not in data:
            return KeyError
        data = data[p]
    return data


def set_key(text: str, dotted: str, value: Any) -> str:
    """Set dotted.key = value in YAML text, keeping every comment and every other line."""
    parts = dotted.split(".")
    lines = text.splitlines(keepends=True)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    start, end, parent_ind = 0, len(lines), -1
    for depth, key in enumerate(parts):
        last = depth == len(parts) - 1
        child_ind, found = None, None
        for i in range(start, end):
            c = _content(lines[i])
            if c is None:
                continue
            ind, s = c
            if child_ind is None:
                child_ind = ind
            if ind != child_ind:
                continue
            m = re.match(r"""(["']?)([^"':#][^:#]*?)\1\s*:(\s.*|)$""", s.rstrip("\n"))
            if m and m.group(2).strip() == key:
                found = i
                break
        if found is None:
            ci = child_ind if child_ind is not None else parent_ind + 2
            ins = end
            while ins > start and _content(lines[ins - 1]) is None:
                ins -= 1
            new: List[str] = []
            for k, p in enumerate(parts[depth:]):
                pad = " " * (ci + 2 * k)
                tail = f" {_fmt(value)}" if depth + k == len(parts) - 1 else ""
                new.append(f"{pad}{p}:{tail}\n")
            lines[ins:ins] = new
            return "".join(lines)
        ind, s = _content(lines[found])
        head, _, rest = lines[found].rstrip("\n").partition(":")
        val, comment = _split_comment(rest)
        # extent of this key's block (more-indented lines below it)
        blk = found + 1
        while blk < end:
            c = _content(lines[blk])
            if c is not None and c[0] <= ind:
                break
            blk += 1
        while blk > found + 1 and _content(lines[blk - 1]) is None:
            blk -= 1
        if last:
            lines[found] = f"{head}: {_fmt(value)}{comment}\n"
            del lines[found + 1:blk]
            return "".join(lines)
        if val.strip():   # an inline mapping: rewrite it as a merged flow mapping
            cur = yaml.safe_load(val) or {}
            if not isinstance(cur, dict):
                cur = {}
            node = cur
            for p in parts[depth + 1:-1]:
                if not isinstance(node.get(p), dict):
                    node[p] = {}
                node = node[p]
            node[parts[-1]] = value
            lines[found] = f"{head}: {_fmt(cur)}{comment}\n"
            return "".join(lines)
        start, end, parent_ind = found + 1, blk, ind
    return "".join(lines)


def write_settings(path: Path, settings: Dict[str, Any]) -> None:
    text = path.read_text()
    for k, v in settings.items():
        text = set_key(text, k, v)
    data = yaml.safe_load(text) or {}
    for k, v in settings.items():
        got = _get(data, k.split("."))
        if got != v:
            raise SystemExit(f"setup: writing {k} did not take (read back {got!r}); orch.yaml left unchanged")
    path.write_text(text)


# -- questions / answers -------------------------------------------------------------
def preset_dir(preset: str) -> Path:
    d = HOME / "templates" / "presets" / preset
    if not d.is_dir():
        raise SystemExit(f"setup: no preset '{preset}' in {d.parent}")
    return d


def preset_defaults(preset: str) -> Dict[str, Any]:
    f = preset_dir(preset) / "setup.defaults.yaml"
    return (yaml.safe_load(f.read_text()) or {}) if f.is_file() else {}


def mem_total_mb() -> int:
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal:"):
                return int(line.split()[1]) // 1024
    except OSError:
        pass
    return 0


def load_questions(preset: str = "") -> List[Dict[str, Any]]:
    qs = yaml.safe_load(QUESTIONS.read_text()) or []
    rec = (preset_defaults(preset).get("recommend") or {}) if preset else {}
    for q in qs:
        if q["id"] in rec:
            q["recommended"] = rec[q["id"]]
        if q.get("recommended") == "auto" and q["id"] == "capacity":
            mb = mem_total_mb()
            q["recommended"] = "large" if mb >= 24000 else "medium" if mb >= 12000 else "small"
    return qs


def instance_preset(inst: Path) -> str:
    f = inst / ANSWERS
    if f.is_file():
        return str((yaml.safe_load(f.read_text()) or {}).get("preset") or "")
    return ""


def settings_for(q: Dict[str, Any], choice: List[str], allowed: List[str]) -> Dict[str, Any]:
    by = {o["id"]: o for o in q["options"]}
    bad = [c for c in choice if c not in by]
    if bad:
        raise SystemExit(f"setup: '{q['id']}' has no option {', '.join(bad)} (options: {', '.join(by)})")
    if q.get("multi"):
        vals = [by[c]["value"] for c in choice]
        return {q["key"]: vals + [a for a in q.get("always") or [] if a not in vals]}
    if len(choice) != 1:
        raise SystemExit(f"setup: '{q['id']}' takes exactly one option")
    out = {}
    for k, v in (by[choice[0]].get("set") or {}).items():
        out[k] = [a for a in allowed if a not in ("manual", "mock")] if v == "@agents" else v
    return out


def parse_value(raw: str) -> Any:
    try:
        return yaml.safe_load(raw)
    except yaml.YAMLError:
        return raw


def cmd_questions(a) -> int:
    inst = None
    try:
        inst = find_instance()
    except SystemExit:
        pass
    preset = a.preset or (instance_preset(inst) if inst else "")
    qs = load_questions(preset)
    if a.json:
        print(json.dumps(qs, indent=1, ensure_ascii=False))
        return 0
    print(f"# setup questions{f' (preset {preset})' if preset else ''} — answer with: orch setup apply --answer <id>=<option>")
    for q in qs:
        rec = q.get("recommended")
        rec = ",".join(rec) if isinstance(rec, list) else rec
        print(f"\n[{q['id']}] {q['question']}{'  (several)' if q.get('multi') else ''}")
        print(f"    why: {q['why']}")
        for o in q["options"]:
            print(f"    - {o['id']:<16} {o['label']} — {o['description']}{'   ← recommended' if o['id'] in str(rec).split(',') else ''}")
    return 0


def cmd_apply(a) -> int:
    inst = find_instance()
    path = inst / CONFIG_NAME
    preset = instance_preset(inst)
    qs = {q["id"]: q for q in load_questions(preset)}
    data = yaml.safe_load(path.read_text()) or {}
    allowed = list(((data.get("executors") or {}).get("allowed")) or [])
    settings: Dict[str, Any] = {}
    record: Dict[str, Any] = {}
    # executors first, so "@agents" sees the new list
    answers = sorted(a.answer or [], key=lambda s: 0 if s.startswith("executors=") else 1)
    for ans in answers:
        qid, _, opt = ans.partition("=")
        if qid not in qs:
            raise SystemExit(f"setup: unknown question '{qid}' (questions: {', '.join(qs)})")
        choice = [c.strip() for c in opt.split(",") if c.strip()]
        s = settings_for(qs[qid], choice, allowed)
        if qs[qid].get("multi") and qs[qid]["key"] == "executors.allowed":
            allowed = s["executors.allowed"]
        settings.update(s)
        record[qid] = choice if qs[qid].get("multi") else choice[0]
    for kv in a.set or []:
        k, _, v = kv.partition("=")
        if not k or not _:
            raise SystemExit(f"setup: --set wants dotted.key=value, got '{kv}'")
        settings[k.strip()] = parse_value(v)
        record.setdefault("set", {})[k.strip()] = settings[k.strip()]
    if not settings:
        raise SystemExit("setup: nothing to apply (use --answer id=option or --set key=value)")
    write_settings(path, settings)
    rec_path = inst / ANSWERS
    old = (yaml.safe_load(rec_path.read_text()) or {}) if rec_path.is_file() else {}
    old.setdefault("answers", {})
    for k, v in record.items():
        if k == "set":
            old.setdefault("set", {}).update(v)
        else:
            old["answers"][k] = v
    old["updated"] = time.strftime("%F %T")
    rec_path.write_text(yaml.safe_dump(old, sort_keys=False, allow_unicode=True))
    for k, v in settings.items():
        print(f"set {k} = {_fmt(v)}")
    print(f"recorded in {rec_path.name}; run `orch lint` and `orch setup card`")
    return 0


def cmd_defaults(a) -> int:
    inst = find_instance()
    d = preset_defaults(a.preset)
    s = d.get("settings") or {}
    if s:
        write_settings(inst / CONFIG_NAME, s)
    rec_path = inst / ANSWERS
    old = (yaml.safe_load(rec_path.read_text()) or {}) if rec_path.is_file() else {}
    old["preset"] = a.preset
    old.setdefault("answers", {})
    old["created"] = old.get("created") or time.strftime("%F %T")
    rec_path.write_text(yaml.safe_dump(old, sort_keys=False, allow_unicode=True))
    print(f"applied preset '{a.preset}' defaults to orch.yaml ({len(s)} setting(s))")
    return 0


def load_cfg() -> Config:
    inst = find_instance()
    raw = yaml.safe_load((inst / CONFIG_NAME).read_text()) or {}
    return Config(inst, _merge(DEFAULTS, raw))


def cmd_rules(a) -> int:
    cfg = load_cfg()
    dest = cfg.workspace / "rules" / "BASE.md"
    if dest.exists():
        print(f"{dest} exists — not overwritten")
        return 0
    preset = a.preset or instance_preset(cfg.instance)
    src = HOME / "templates" / "rules_BASE.md"
    if preset and (preset_dir(preset) / "rules_BASE.md").is_file():
        src = preset_dir(preset) / "rules_BASE.md"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(src.read_text().replace("<PROJECT>", cfg.name))
    print(f"wrote {dest} (skeleton — fill every {SKELETON_MARK} with the Owner)")
    return 0


# -- rig card ----------------------------------------------------------------------------
def _rel(p: Path, base: Path) -> str:
    try:
        return str(p.resolve().relative_to(base.resolve())) or "."
    except ValueError:
        return str(p)


def card_text(cfg: Config) -> str:
    d, e = cfg.data, cfg.data["executors"]
    s, r, idle, reap = d.get("scheduler") or {}, d.get("review") or {}, d.get("idle") or {}, d.get("reaper") or {}
    w, cl, pf = d.get("watchdog") or {}, d.get("close") or {}, d.get("preflight") or {}
    fw = subprocess.run(["git", "-C", str(HOME), "rev-parse", "--short", "HEAD"], capture_output=True, text=True).stdout.strip()
    base = cfg.workspace / "rules" / "BASE.md"
    base_state = ("missing — `orch setup rules`" if not base.is_file() else
                  f"skeleton, {base.read_text().count(SKELETON_MARK)} TODO(setup) left" if SKELETON_MARK in base.read_text()
                  else f"{base.stat().st_size} bytes")
    try:
        import rounds as rmod
        rds = rmod.load_rounds(cfg)
    except Exception:
        rds = {}
    from rows import load_rows
    rows, _ = load_rows(cfg, include_archived=True)
    active = [x for x in rows if not x.archived]
    lanes: Dict[str, int] = {}
    for x in active:
        lanes[x.lane] = lanes.get(x.lane, 0) + 1
    ub = e.get("unblock_executors") or [e.get("unblock_executor")]
    flav = {x: cfg.flavor(x) for x in e.get("allowed") or []}
    ans_path = cfg.instance / ANSWERS
    ans = (yaml.safe_load(ans_path.read_text()) or {}) if ans_path.is_file() else {}
    L = [
        f"# Rig card — {cfg.name}",
        "",
        f"<!-- orch:generated by `orch setup card` on {time.strftime('%F %T')}; re-run it after any orch.yaml change. -->",
        "A planning session reads this first, then `rules/BASE.md`, then the framework's "
        "`docs/PLANNING_AGENT_GUIDE.md`. It describes how this rig behaves; the rules say what rows may do.",
        "",
        "## Where things are",
        "",
        "| what | path |",
        "|---|---|",
        f"| instance (`./orch`, state, queues, rounds) | `{_rel(cfg.instance, cfg.root)}` |",
        f"| project root (every executor's cwd) | `{cfg.root}` |",
        f"| workspace (plan paths, reports, DECISIONS.md) | `{_rel(cfg.workspace, cfg.root)}` |",
        f"| git repo the guard commits into | `{_rel(cfg.git_repo, cfg.root)}` |",
        f"| standing rules | `{_rel(base, cfg.root)}` ({base_state}) |",
        f"| round rules | `{_rel(cfg.instance, cfg.root)}/rounds/<tag>/RULES.md` |",
        f"| framework | `{HOME}` @ {fw or '?'} |",
        f"| tmux session | `{cfg.tmux_session}` |",
        "",
        "## Executors",
        "",
        f"- Allowed: {', '.join(f'`{x}` ({flav[x]})' for x in e.get('allowed') or [])}; default `{e.get('default')}`.",
        "- Launch args: " + (", ".join("`%s` %s" % (k, " ".join(map(str, v))) for k, v in (e.get("args") or {}).items()) or "none") + ";"
        f" environment set for: {', '.join(f'`{k}`' for k in (e.get('env') or {})) or 'none'}.",
        f"- Per-executor caps: {json.dumps(e.get('max_parallel') or {}) if e.get('max_parallel') else 'none'}.",
        "",
        "## Behaviour (orch.yaml)",
        "",
        "| switch | setting |",
        "|---|---|",
        f"| scheduling | `{s.get('mode')}` · max {s.get('max_parallel')} sessions · dispatch only while ≥ {s.get('min_free_mb')} MB free |",
        f"| unblocking | {' → '.join(f'`{x}`' for x in ub)} · {e.get('max_unblock_attempts')} attempt(s), then retier to `{e.get('retier_on_max_unblock')}`, then halt + page · auto-unblocked: {', '.join(e.get('auto_unblock') or []) or 'junior rows'} |",
        f"| dead window | `{e.get('dead_window_policy')}` |",
        f"| independent review | `{r.get('policy')}`{' (a row opts in with `review=1`)' if r.get('policy') == 'none' else ''} |",
        f"| close checks | PROGRESS.md {'required' if cl.get('require_progress') else 'optional'} · commit {'required' if cl.get('require_commit') else 'optional'} · on fail `{cl.get('on_fail')}` |",
        f"| idle sessions | {'nudged after ' + str(idle.get('nudge_after')) + ' s, max ' + str(idle.get('max_nudges')) + (', then BLOCKED after ' + str(idle.get('block_after')) + ' s' if idle.get('block_after') else '') if idle.get('nudge_after') else 'left alone'} |",
        f"| usage limits | the session waits; `continue` every 15 min until the limit lifts |",
        f"| window reaping | {'on, ' + str(reap.get('closed_grace_secs')) + ' s after accept' if reap.get('enabled', True) else 'off'} |",
        f"| plan review before a round | {'required' if pf.get('required') else 'optional'} (`orch preflight --round <tag> --review`) |",
        f"| phone | {'Telegram, ' + str(w.get('push_min_urgency', 'normal')) + ' and up' if w.get('enabled') else 'off'} |",
        "",
        "## Resources (use under `orch hold <id> <res> -- <cmd>`, declare on the row with `res=`)",
        "",
    ]
    for n, v in cfg.resources().items():
        L.append(f"- `{n}` — {v['slots']} slot(s), {v['scope']}")
    L += ["", "## Ladder now", ""]
    if lanes:
        L.append("Active lanes: " + ", ".join(f"`{k}` ({v} rows)" for k, v in lanes.items()) + f"; archived rows: {len(rows) - len(active)}.")
    else:
        L.append("No rows yet.")
    if rds:
        L.append("Rounds: " + ", ".join(f"`{t}` ({(v or {}).get('status', '?')}, lanes {' '.join((v or {}).get('lanes') or [])})" for t, v in rds.items()) + ".")
    L += ["", "## Setup answers", ""]
    if ans.get("preset"):
        L.append(f"Preset: `{ans['preset']}`.")
    for k, v in (ans.get("answers") or {}).items():
        L.append(f"- {k}: {', '.join(v) if isinstance(v, list) else v}")
    for k, v in (ans.get("set") or {}).items():
        L.append(f"- {k} = {_fmt(v)}")
    if not (ans.get("answers") or ans.get("set")):
        L.append("None recorded (`orch setup questions`, then `orch setup apply`).")
    L += [
        "",
        "## Next",
        "",
        "Plan a round in a fresh session: *“Read `" + _rel(cfg.instance, cfg.root) + "/RIG.md` and `" + str(HOME) +
        "/docs/PLANNING_AGENT_GUIDE.md`, then plan round `<tag>` for: <goal>.”* The planner ends at a clean "
        "`orch lint` + `orch preflight --round <tag>` and a commit; `./orch round start <tag>` starts it. "
        "Course corrections: `" + str(HOME) + "/docs/COURSE_LOOP.md`.",
        "",
    ]
    return "\n".join(L)


def cmd_card(a) -> int:
    cfg = load_cfg()
    out = cfg.instance / "RIG.md"
    out.write_text(card_text(cfg))
    print(f"wrote {out}")
    return 0


# -- readiness --------------------------------------------------------------------------
def adapter_binary(cfg: Config, family: str) -> str:
    """The CLI an adapter launches: executors.bin.<key> or the adapter's own ${ORCH_BIN_<KEY>:-default}."""
    f = HOME / "lib" / "adapters" / f"{family}.sh"
    m = re.search(r"\$\{ORCH_BIN_([A-Z_]+):-([^}]+)\}", f.read_text()) if f.is_file() else None
    if not m:
        return cfg.binary(family, family)
    return os.path.expandvars(cfg.binary(m.group(1).lower(), m.group(2)))


def cmd_check(a) -> int:
    cfg = load_cfg()
    errs: List[str] = []
    warns: List[str] = []
    ok: List[str] = []
    lint = subprocess.run([sys.executable, str(HOME / "lib" / "lint.py")], capture_output=True, text=True,
                          env=dict(os.environ, ORCH_INSTANCE=str(cfg.instance)))
    lint_errs = [l for l in lint.stdout.splitlines() if l.strip().startswith("ERROR")]
    if lint.returncode == 0:
        ok.append("orch lint clean")
    elif all("queue file is empty" in l for l in lint_errs) and lint_errs:
        ok.append("orch lint clean apart from: no rows yet (expected before the first round is planned)")
    else:
        errs.append(f"orch lint has {len(lint_errs) or 'some'} error(s) — run `orch lint`")
    for label, p in (("project root", cfg.root), ("workspace", cfg.workspace)):
        (ok if p.is_dir() else errs).append(f"{label} {p} {'exists' if p.is_dir() else 'does not exist'}")
    g = subprocess.run(["git", "-C", str(cfg.git_repo), "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    (ok if g.returncode == 0 else errs).append(f"guard git repo {cfg.git_repo} {'is a git repo' if g.returncode == 0 else 'is not a git repository'}")
    base = cfg.workspace / "rules" / "BASE.md"
    if not base.is_file():
        warns.append("rules/BASE.md missing — `orch setup rules`, then fill it with the Owner")
    elif SKELETON_MARK in base.read_text():
        warns.append(f"rules/BASE.md still has {base.read_text().count(SKELETON_MARK)} {SKELETON_MARK} slot(s)")
    else:
        ok.append("rules/BASE.md filled")
    # tmux session unique among rigs under the project root
    clash = []
    for y in cfg.root.glob("**/orch.yaml"):
        if y.parent == cfg.instance or ".worktree" in str(y) or "/tests/" in str(y):
            continue
        try:
            other = (yaml.safe_load(y.read_text()) or {}).get("project", {}).get("tmux_session")
        except Exception:
            continue
        if other == cfg.tmux_session:
            clash.append(str(y.parent))
    (errs if clash else ok).append(f"tmux session '{cfg.tmux_session}' " + (f"also used by {', '.join(clash)}" if clash else "is unique"))
    e = cfg.data["executors"]
    for x in e.get("allowed") or []:
        if x in ("manual", "mock"):
            continue
        b = adapter_binary(cfg, cfg.family(x))
        found = shutil.which(b) or (Path(b).is_file() and os.access(b, os.X_OK))
        (ok if found else warns).append(f"executor {x}: `{b}` {'found' if found else 'not found on PATH'}")
    if not (cfg.prompts / "_TEMPLATE_HARD.md").is_file():
        warns.append("no instance templates in prompts/ — the framework's generic templates will be used")
    w = cfg.data.get("watchdog") or {}
    if w.get("enabled") and not str(w.get("telegram_chat_id") or "").strip():
        errs.append("watchdog.enabled but no telegram_chat_id — `orch setup apply --set watchdog.telegram_chat_id=<id>`")
    sha = subprocess.run(["git", "-C", str(HOME), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    dirty = [l for l in subprocess.run(["git", "-C", str(HOME), "status", "--porcelain"], capture_output=True,
                                       text=True).stdout.splitlines() if "__pycache__" not in l and ".tested" not in l]
    if dirty:
        warns.append("framework tree is dirty — `orch start` refuses it")
    elif sha and not (HOME / ".tested" / sha).exists():
        warns.append("framework commit has not passed tests/run_all.sh")
    else:
        ok.append("framework commit tested and clean")
    if not (cfg.instance / "RIG.md").is_file():
        warns.append("no RIG.md — `orch setup card`")
    for x in ok:
        print(f"  ok    {x}")
    for x in warns:
        print(f"  warn  {x}")
    for x in errs:
        print(f"  ERROR {x}")
    print(f"\nsetup check: {len(errs)} error(s), {len(warns)} warning(s)")
    return 1 if errs else 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="orch setup", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("questions"); p.add_argument("--preset", default=""); p.add_argument("--json", action="store_true")
    p = sub.add_parser("apply"); p.add_argument("--answer", action="append"); p.add_argument("--set", action="append")
    p = sub.add_parser("defaults"); p.add_argument("--preset", required=True)
    p = sub.add_parser("rules"); p.add_argument("--preset", default="")
    sub.add_parser("card")
    sub.add_parser("check")
    a = ap.parse_args()
    if a.cmd in ("apply", "defaults"):          # they write orch.yaml — the operator's (v3, lib/capability.py)
        import capability
        from config_parser import load
        try:
            me = capability.session(load())
        except (SystemExit, Exception):
            me = ""
        if me:
            print(f"REFUSED: `orch setup {a.cmd}` changes the rig's configuration — the operator's command, "
                  f"not session {me}'s")
            return 1
    return {"questions": cmd_questions, "apply": cmd_apply, "defaults": cmd_defaults, "rules": cmd_rules,
            "card": cmd_card, "check": cmd_check}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
