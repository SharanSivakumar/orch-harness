#!/usr/bin/env python3
"""The close pipeline: what a CLOSED claim must survive before it counts (v2).

v1 ran its stop-line check only inside `orch done`; the watcher accepted any
CLOSED a session wrote itself, and the auto-seal skipped the check too
(a single judge). Here ONE function judges every claim, whichever path wrote
it, and `orch done` calls the same function synchronously so an executor sees
a refusal while it can still fix it.

Checks (each configurable under `close:` in orch.yaml):

  summary      the summary is not a template placeholder or empty
  stop_lines   no STOP / REFUSED / HEADS_UNCHANGED no in the row's evidence unless
               PROGRESS names a signed ruling (lib/close_check.py)
  protected    no commit since dispatch and no uncommitted change touches a protected path
               (guard.protected_paths + the automatic set) unless PROGRESS names an
               Owner-signed ruling (lib/protected.py, v3)
  progress     <plan dir>/PROGRESS.md exists, is non-empty and was written after
               this attempt was dispatched
  probe        <plan dir>/ACCEPT.sh (or accept=<file>) exits 0 from the project root
  commits      commits since dispatch that touch the row's allowlist; required only
               with close.require_commit
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config_parser import Config  # noqa: E402
from rows import QRow, plan_dir  # noqa: E402

PLACEHOLDER = re.compile(r"<[A-Za-z][^<>]*>|one-line summary|no summary given|^\s*$|^summary$", re.I)


def git_head(repo: Path) -> str:
    try:
        p = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True,
                           text=True, timeout=15)
        return p.stdout.strip() if p.returncode == 0 else ""
    except Exception:
        return ""


def allowlist(cfg: Config, rid: str) -> List[str]:
    from state_resolver import Resolver
    return Resolver(cfg, tmux=False).allowlist(rid)


def commits_since(cfg: Config, rid: str, head: str) -> Tuple[int, List[str]]:
    if not head:
        return 0, []
    paths = allowlist(cfg, rid)
    args = ["git", "-C", str(cfg.git_repo), "log", "--format=%h %s", f"{head}..HEAD"]
    if paths:
        args += ["--", *paths]
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=30)
        lines = [l for l in p.stdout.splitlines() if l.strip()] if p.returncode == 0 else []
    except Exception:
        lines = []
    return len(lines), lines[:20]


def probe_path(cfg: Config, row: QRow) -> Optional[Path]:
    d = plan_dir(cfg, row)
    if d is None:
        return None
    name = row.meta.get("accept", "ACCEPT.sh")
    p = (d / name).resolve()
    return p if p.is_file() else None


def protected_snapshot(cfg: Config, rid: str, head: str) -> Dict[str, Any]:
    """The row's protected-path base (lib/protected.py): made at its first dispatch, kept until it
    is accepted. Its copy rides in the dispatch snapshot for the record."""
    try:
        import protected
        return protected.make_base(cfg, rid, head)
    except Exception:
        return {}


def probe_snapshot(cfg: Config, row: QRow) -> Dict[str, str]:
    """{path, sha256} of the acceptance probe as it stood at dispatch ('' when none).
    The close compares against it: a row may not delete or rewrite its own probe."""
    import hashlib
    p = probe_path(cfg, row)
    if p is None:
        return {"path": "", "sha256": ""}
    return {"path": str(p), "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}


def _rel(cfg: Config, p: Path) -> str:
    try:
        return str(p.relative_to(cfg.workspace))
    except ValueError:
        return str(p)


def run_probe(cfg: Config, row: QRow, path: Path) -> Tuple[bool, str]:
    timeout = int(cfg.data["close"].get("probe_timeout", 900))
    out = cfg.state / f"{row.id}.accept.out"
    env = dict(os.environ, ORCH_ROW=row.id, ORCH_INSTANCE=str(cfg.instance),
               ORCH_WORKSPACE=str(cfg.workspace), ORCH_PLAN_DIR=str(path.parent))
    try:
        with open(out, "w") as f:
            os.chmod(out, 0o600)
            p = subprocess.run(["bash", str(path)], cwd=str(cfg.root), stdout=f, stderr=subprocess.STDOUT,
                               stdin=subprocess.DEVNULL, timeout=timeout, env=env)
        return p.returncode == 0, f"{path.name} exited {p.returncode} (output state/{row.id}.accept.out)"
    except subprocess.TimeoutExpired:
        return False, f"{path.name} timed out after {timeout}s"
    except Exception as e:
        return False, f"{path.name} could not run: {e}"


def check_claim(cfg: Config, row: QRow, summary: str, dispatch: Dict[str, Any]) -> Dict[str, Any]:
    """-> {ok, checks: [{name, ok, detail}], commits, commit_lines}. Never raises."""
    c = cfg.data["close"]
    checks: List[Dict[str, Any]] = []

    def add(name: str, ok: bool, detail: str, blocking: bool = True) -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": detail, "blocking": blocking})

    add("summary", not PLACEHOLDER.search(summary or ""),
        "summary present" if not PLACEHOLDER.search(summary or "") else
        f"summary is empty or a template placeholder: {summary[:80]!r}")

    if c.get("stop_lines", True):
        try:
            import close_check as cc
            roots = cc.evidence_roots(cfg, row.id)
            hits, files, skipped = cc.scan(roots)
            ruling, note = cc.signed_ruling(cfg, roots, rid=row.id) if hits else (None, "")
            ok = not hits or bool(ruling)
            detail = (f"{files} evidence files, no stop line" if not hits else
                      f"{len(hits)} stop line(s), first: {hits[0][:200]}; {note}" if not ruling else
                      f"{len(hits)} stop line(s) covered by signed ruling {ruling}")
            add("stop_lines", ok, detail)
        except Exception as e:
            add("stop_lines", False, f"the stop-line check could not run: {type(e).__name__}: {e}")

    try:   # on now, or on at the row's base (a row cannot switch it off mid-attempt): lib/protected.py
        import close_check as cc
        import protected
        if protected.enabled(cfg) or (protected.load_base(cfg, row.id) or {}).get("set") is not None:
            ok, detail = protected.check(cfg, row.id, dispatch, cc.evidence_roots(cfg, row.id))
            add("protected", ok, detail)
    except Exception as e:
        add("protected", False, f"the protected-path check could not run: {type(e).__name__}: {e}")

    d = plan_dir(cfg, row)
    since = float(dispatch.get("epoch", 0) or 0)
    if c.get("require_progress", True):
        if d is None:
            add("progress", True, "plan sits at the workspace top level — no row folder to check", False)
        else:
            p = d / "PROGRESS.md"
            if not p.is_file() or p.stat().st_size == 0:
                add("progress", False, f"{_rel(cfg, p)} is missing or empty")
            elif since and p.stat().st_mtime + 2 < since:
                add("progress", False, f"{_rel(cfg, p)} was last written "
                    f"{time.strftime('%F %T', time.localtime(p.stat().st_mtime))}, before this attempt was dispatched")
            else:
                add("progress", True, "PROGRESS.md written this attempt")

    pp = probe_path(cfg, row)
    snap = ((dispatch.get("snapshot") or {}).get("probe") or {})
    if snap.get("sha256"):
        import hashlib
        cur = Path(snap["path"])
        if not cur.is_file():
            add("probe_integrity", False, f"the acceptance probe {cur.name} existed at dispatch and is gone now")
            pp = None
        elif hashlib.sha256(cur.read_bytes()).hexdigest() != snap["sha256"]:
            add("probe_integrity", False, f"the acceptance probe {cur.name} was changed during the attempt")
            pp = None
        else:
            pp = cur
    if pp is not None:
        ok, detail = run_probe(cfg, row, pp)
        add("probe", ok, detail)
    elif row.meta.get("accept"):
        add("probe", False, f"accept={row.meta['accept']} names no file in the row folder")

    n, lines = commits_since(cfg, row.id, str(dispatch.get("head", "")))
    req = bool(c.get("require_commit", False))
    add("commits", n > 0 or not req, f"{n} commit(s) since dispatch touching the row's allowlist", req)

    ok = all(ch["ok"] for ch in checks if ch["blocking"])
    return {"ok": ok, "checks": checks, "commits": n, "commit_lines": lines}


def failures(result: Dict[str, Any]) -> str:
    return "; ".join(f"{c['name']}: {c['detail']}" for c in result.get("checks", [])
                     if c.get("blocking") and not c.get("ok"))


def judge(rid: str) -> int:
    """`closepipe.py judge <id>` — run by the engine as a job; writes state/<id>.check.json."""
    import json
    from config_parser import load
    from rows import find_row
    from store import Store
    cfg = load()
    st = Store(cfg.state)
    row = find_row(cfg, rid)
    v, summary, mt = st.status(rid)
    if row is None:
        res = {"ok": False, "checks": [{"name": "row", "ok": False, "detail": f"{rid} is in no queue", "blocking": True}]}
    else:
        res = check_claim(cfg, row, summary, st.dispatch(rid))
    res.update(status_mtime=mt, judged=time.strftime("%F %T"))
    out = cfg.state / f"{rid}.check.json"
    tmp = out.with_suffix(".tmp")
    tmp.write_text(json.dumps(res, indent=1))
    os.replace(tmp, out)
    print(json.dumps(res, indent=1))
    return 0


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "judge":
        sys.exit(judge(sys.argv[2]))
    print("usage: closepipe.py judge <row-id>")
    sys.exit(2)
