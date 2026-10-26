#!/usr/bin/env python3
"""Protected paths (v3 drop B): what only the Owner may change.

Scorers, gates, keys, caps, phases and the rights table are the Owner's. A row, an unblocker,
a reviewer, a Steward or a planner may not change them; the only door is a ruling the Owner
signed and committed in the workspace DECISIONS.md that names the row or the path, cited in the
row's PROGRESS (`SIGNED_RULING <id>`).

The set (relative to the guard repo, `guard.git_repo`):

  * `guard.protected_paths` — globs. `*` and `**` cross `/` (over-matching is the safe
    direction; `**/x` also matches a top-level `x`); a pattern ending in `/`, or naming a
    directory, protects the whole tree;
  * the automatic set (`guard.protected_auto`, default on), relative to the instance:
    `SPEND_CAPS.yaml` (drop C), `phases.yaml` (drop D) and the rig's rights table
    (`steward.rights_file`, default `steward/RIGHTS.md`). Paths outside the guard repo
    cannot be committed through it and are not listed.

Two enforcement points:

  the guard (lib/parallel_guard_core.sh `commit`): refuses to stage a protected path for any
    id except a `plan:` commit from the operator's shell (no session identity; never
    `plan:engine`, the engine's bookkeeping). Every operator `plan:` commit is recorded in
    state/protected_commits.jsonl.
  close check `protected` (lib/closepipe.py, `close.protected`): against the row's BASE — the
    dispatch head and the protected paths already dirty at its first dispatch, kept across
    attempts and resets until the row is accepted (state/<id>.protected_base.json) — a claim
    fails when a commit since the base (`git log -m --no-renames`; a HEAD that no longer
    descends from it is compared by `git diff`) or an uncommitted change touches a protected
    path. Operator commits are not charged; a pending edit that predates the base is not
    charged while it stays byte for byte. The exception is an Owner ruling (see owner_ruling).
    A Steward ruling (`SW-<k>`) never counts here.

usage: protected.py list
       protected.py match <path>... | -              exit 0 none protected · 3 some (printed); - reads stdin
       protected.py record <sha> <id> [<path>...]    (the guard, after an operator plan: commit)
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))

AUTO = ("SPEND_CAPS.yaml", "phases.yaml")
LEDGER = "protected_commits.jsonl"
SIGNED = ("RULED", "RATIFIED")


def _norm(p: str) -> str:
    p = str(p).strip()
    if len(p) > 1 and p[0] == p[-1] == '"':   # git's C-quoted form
        import codecs
        p = codecs.escape_decode(p[1:-1].encode())[0].decode("utf-8", "replace")
    while p.startswith("./"):
        p = p[2:]
    return p


def _flag(v: Any, default: bool = True) -> bool:
    if v is None:
        return default
    if isinstance(v, str):
        return v.strip().lower() not in ("false", "off", "no", "0", "")
    return bool(v)


def enabled(cfg) -> bool:
    return _flag((cfg.data.get("close") or {}).get("protected", True))


def auto_paths(cfg) -> List[str]:
    if not _flag((cfg.data.get("guard") or {}).get("protected_auto", True)):
        return []
    rights = str((cfg.data.get("steward") or {}).get("rights_file") or "steward/RIGHTS.md")
    out = []
    for name in (*AUTO, rights):
        p = (cfg.instance / name).resolve()
        try:
            out.append(str(p.relative_to(cfg.git_repo.resolve())))
        except ValueError:
            continue
    return out


def patterns(cfg) -> List[str]:
    """The protected set as repo-relative globs: configured first, then the automatic set."""
    raw = (cfg.data.get("guard") or {}).get("protected_paths") or []
    if isinstance(raw, str):   # `protected_paths: gates/` — one pattern, never its characters
        raw = [raw]
    pats = []
    for g in raw:
        g = _norm(g)
        if g.startswith("/"):   # an absolute path counts only inside the guard repo
            try:
                g = str(Path(g).resolve().relative_to(cfg.git_repo.resolve())) + ("/" if g.endswith("/") else "")
            except ValueError:
                continue
        if g:
            pats.append(g)
    return list(dict.fromkeys(pats + auto_paths(cfg)))


def matches(rel: str, pats: List[str]) -> Optional[str]:
    """The first pattern that protects `rel` (repo-relative), else None."""
    rel = _norm(rel)
    for g in pats:
        if g.endswith("/"):
            if rel.startswith(g) or rel == g.rstrip("/"):
                return g
            continue
        if rel == g or fnmatch.fnmatchcase(rel, g) or rel.startswith(g + "/"):
            return g
        if g.startswith("**/") and matches(rel, [g[3:]]):
            return g
    return None


def _git(cfg, *args: str) -> Tuple[int, str]:
    try:   # no fsmonitor / ignoreStat: a stat cache must not hide an edit
        p = subprocess.run(["git", "-C", str(cfg.git_repo), "-c", "core.quotepath=false", "-c", "core.fsmonitor=false",
                            "-c", "core.ignoreStat=false", *args],
                           capture_output=True, text=True, timeout=60)
        return p.returncode, p.stdout
    except Exception as e:
        return 99, str(e)


def _prefix(cfg) -> str:
    """git_repo relative to its git toplevel ('' or 'sub/'): git prints toplevel-relative paths."""
    rc, out = _git(cfg, "rev-parse", "--show-prefix")
    return out.strip() if rc == 0 else ""


def _repo_rel(path: str, prefix: str) -> Optional[str]:
    path = _norm(path)
    if not prefix:
        return path
    return path[len(prefix):] if path.startswith(prefix) else None


def _sha(cfg, rel: str) -> str:
    f = cfg.git_repo / rel
    try:
        return hashlib.sha256(f.read_bytes()).hexdigest() if f.is_file() else "absent"
    except Exception:
        return "unreadable"


def writes_path(cfg) -> Path:
    return cfg.state / "protected_writes.json"


ORCH_WRITTEN = ("phases.yaml",)    # instance files orch itself rewrites while rows run (drop D)


def _orch_rels(cfg) -> List[str]:
    out = []
    for name in ORCH_WRITTEN:
        try:
            out.append(str((cfg.instance / name).resolve().relative_to(cfg.git_repo.resolve())))
        except ValueError:
            continue
    return out


def note_write(cfg, path: Path, sha: str) -> None:
    """orch itself wrote a protected path (the engine rests a phase in phases.yaml): its sha is recorded, so the
    close check does not charge that content to the rows running meanwhile. Only the latest sha per path counts,
    and only for ORCH_WRITTEN files (a forged entry for any other path is never honoured)."""
    try:
        rel = str(Path(path).resolve().relative_to(cfg.git_repo.resolve()))
    except ValueError:
        return
    if rel not in _orch_rels(cfg):
        return
    f = writes_path(cfg)
    try:
        cur = json.loads(f.read_text())
    except Exception:
        cur = {}
    cur[rel] = {"sha": sha, "ts": time.strftime("%F %T")}
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_name(f".{f.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(cur, indent=1, sort_keys=True))
    os.replace(tmp, f)


def orch_wrote(cfg, rel: str) -> bool:
    """The protected path's current content is exactly what orch last wrote there (ORCH_WRITTEN files only)."""
    if rel not in _orch_rels(cfg):
        return False
    try:
        rec = (json.loads(writes_path(cfg).read_text()) or {}).get(rel) or {}
    except Exception:
        return False
    return bool(rec.get("sha")) and _sha(cfg, rel) == rec["sha"]


def dirty(cfg, pats: Optional[List[str]] = None) -> List[str]:
    """Protected paths with uncommitted changes (staged, unstaged, untracked or deleted)."""
    pats = patterns(cfg) if pats is None else pats
    if not pats:
        return []
    rc, out = _git(cfg, "status", "--porcelain", "-z", "--untracked-files=all", "--no-renames")
    if rc != 0:
        raise RuntimeError(f"git status failed in {cfg.git_repo}")
    pre = _prefix(cfg)
    paths = [_repo_rel(rec[3:], pre) for rec in out.split("\0") if len(rec) > 3]
    return sorted({p for p in paths if p and matches(p, pats)})


def hidden(cfg, pats: List[str]) -> List[str]:
    """Protected paths git status would not report: assume-unchanged (lower-case tag) or
    skip-worktree (S) in `git ls-files -v`."""
    rc, out = _git(cfg, "ls-files", "-v", "-z")
    if rc != 0:
        return []
    pre = _prefix(cfg)
    res = []
    for rec in out.split("\0"):
        if len(rec) > 2 and (rec[0].islower() or rec[0] == "S"):
            p = _repo_rel(rec[2:], pre)
            if p and matches(p, pats):
                res.append(p)
    return sorted(set(res))


# -- the row's base -----------------------------------------------------------------------
def base_path(cfg, rid: str) -> Path:
    return cfg.state / f"{rid}.protected_base.json"


def load_base(cfg, rid: str) -> Optional[Dict[str, Any]]:
    try:
        return json.loads(base_path(cfg, rid).read_text())
    except Exception:
        return None


def make_base(cfg, rid: str, head: str) -> Dict[str, Any]:
    """At dispatch: the row's base, created at its first dispatch and KEPT across attempts and
    resets until it is accepted (clear_base), so a change one attempt left behind is still
    charged to the next. {head, dirty: {path: sha256}, set: [patterns] | None, ts}."""
    b = load_base(cfg, rid)
    if b is not None:
        return b
    on = enabled(cfg)
    pats = patterns(cfg) if on else []
    try:
        d = {p: _sha(cfg, p) for p in dirty(cfg, pats)}
    except Exception:
        d = {}
    unborn = not head and _git(cfg, "rev-parse", "--verify", "-q", "HEAD")[0] != 0 \
        and _git(cfg, "rev-parse", "--git-dir")[0] == 0
    b = {"head": head, "unborn": unborn, "dirty": d, "set": pats if on else None,
         "git_repo": str(cfg.git_repo), "ts": time.strftime("%F %T")}
    if not head and not unborn:
        return b   # the head could not be read: not kept, the next dispatch tries again
    try:
        cfg.state.mkdir(parents=True, exist_ok=True)
        tmp = base_path(cfg, rid).with_suffix(".tmp")
        tmp.write_text(json.dumps(b, indent=1))
        tmp.replace(base_path(cfg, rid))
    except Exception:
        pass
    return b


def clear_base(cfg, rid: str) -> None:
    try:
        base_path(cfg, rid).unlink()
    except FileNotFoundError:
        pass


# -- operator commits -------------------------------------------------------------------------
def operator_commits(cfg) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    try:
        for line in (cfg.state / LEDGER).read_text().splitlines():
            try:
                r = json.loads(line)
                out[str(r["sha"])] = r
            except Exception:
                continue
    except FileNotFoundError:
        pass
    return out


def record(cfg, sha: str, ident: str, paths: List[str]) -> None:
    cfg.state.mkdir(parents=True, exist_ok=True)
    rec = {"sha": sha, "id": ident, "protected": paths, "ts": time.strftime("%F %T")}
    with open(cfg.state / LEDGER, "a") as f:
        f.write(json.dumps(rec, sort_keys=True) + "\n")


def _commits(cfg, head: str) -> List[Tuple[str, List[str]]]:
    """[(sha, [repo-relative paths])] for head..HEAD (the whole history when head is '' — the repo
    had no commit at the base), merges split per parent."""
    if not head and _git(cfg, "rev-parse", "--verify", "-q", "HEAD")[0] != 0:
        return []
    rc, out = _git(cfg, "log", "-m", "--no-renames", "--name-only", "--format=@@%H", f"{head}..HEAD" if head else "HEAD")
    if rc != 0:   # the base commit is gone (history rewritten): nothing can be cleared
        raise RuntimeError(f"git log {head[:7]}..HEAD failed — the row's base commit is not in the history "
                           f"(after a history rewrite the operator removes state/<id>.protected_base.json)")
    pre = _prefix(cfg)
    res: List[Tuple[str, List[str]]] = []
    for line in out.splitlines():
        if line.startswith("@@"):
            res.append((line[2:].strip(), []))
        elif line.strip() and res:
            p = _repo_rel(line, pre)
            if p:
                res[-1][1].append(p)
    return res


def changes(cfg, base: Dict[str, Any], pats: List[str]) -> Tuple[List[str], List[str], int]:
    """-> (hits, operator commit shas since the base, pending edits that predate the base)."""
    head = str(base.get("head") or "")
    hits: List[str] = []
    ops: List[str] = []
    op_paths: set = set()
    if not head and not base.get("unborn"):
        hits.append("(the dispatch commit is unknown — commits since dispatch cannot be cleared)")
    else:
        ok = operator_commits(cfg)
        touched: Dict[str, str] = {}
        for sha, paths in _commits(cfg, head):
            if sha in ok:
                ops.append(sha)
                op_paths.update(paths)
                continue
            for p in paths:
                if matches(p, pats):
                    touched.setdefault(p, sha)
        # a path a session's commit touched is charged while it differs from the base (net):
        # a reverted change is no change
        net = set()
        if touched:
            rc, out = (_git(cfg, "diff", "--name-only", "--no-renames", head, "HEAD") if head else
                       _git(cfg, "ls-tree", "-r", "--name-only", "--full-tree", "HEAD"))
            pre = _prefix(cfg)
            net = {_repo_rel(l, pre) for l in out.splitlines()} if rc == 0 else set(touched)
        hits += [f"{p} (commit {sha[:7]})" for p, sha in touched.items() if p in net]
        if head and _git(cfg, "merge-base", "--is-ancestor", head, "HEAD")[0] != 0:
            rc, out = _git(cfg, "diff", "--name-only", "--no-renames", head, "HEAD")
            pre = _prefix(cfg)
            hits += [f"{p} (HEAD no longer descends from the dispatch commit {head[:7]})"
                     for p in (_repo_rel(l, pre) for l in out.splitlines()) if p and matches(p, pats)]
    snap = base.get("dirty")
    pre_n = 0
    now = dirty(cfg, pats)
    for p in now:
        if isinstance(snap, dict) and p in snap and snap[p] == _sha(cfg, p):
            pre_n += 1
            continue
        if orch_wrote(cfg, p):          # orch's own write (phases.yaml at a phase's rest), not the row's
            pre_n += 1
            continue
        hits.append(f"{p} (uncommitted)")
    hits += [f"{p} (hidden from git status: assume-unchanged/skip-worktree)" for p in hidden(cfg, pats)]
    for p in (snap or {}):
        if p not in now and p not in op_paths and matches(p, pats):
            hits.append(f"{p} (the pending edit present at dispatch was undone)")
    return list(dict.fromkeys(hits)), ops, pre_n


def check(cfg, rid: str, dispatch: Dict[str, Any], roots: List[Path]) -> Tuple[bool, str]:
    """The close check. -> (ok, detail). Runs when close.protected is on now or was on at the base
    (a row cannot switch it off, or narrow the set, during its own attempt)."""
    kept = load_base(cfg, rid)
    snapb = (dispatch.get("snapshot") or {}).get("protected") or {}
    base = kept or {"head": str(snapb.get("head", dispatch.get("head", ""))), "unborn": snapb.get("unborn"),
                    "dirty": snapb.get("dirty"), "set": snapb.get("set"), "git_repo": snapb.get("git_repo")}
    if base.get("git_repo") and Path(base["git_repo"]) != cfg.git_repo:   # a row cannot redirect the repo
        import copy
        cfg = copy.copy(cfg)
        cfg.git_repo = Path(base["git_repo"])
    pats = list(dict.fromkeys((patterns(cfg) if enabled(cfg) else []) + list(base.get("set") or [])))
    if not pats:
        return True, "no protected paths on this rig"
    if _git(cfg, "rev-parse", "--git-dir")[0] != 0:
        if kept:
            return False, f"{cfg.git_repo} is no longer a git repository — the row's base cannot be compared"
        return True, f"{cfg.git_repo} is not a git repository — nothing to compare"
    hits, ops, pre_n = changes(cfg, base, pats)
    if not hits:
        tail = f"; {pre_n} pending edit(s) predate the row" if pre_n else ""
        return True, f"{len(pats)} protected pattern(s), none touched{tail}"
    paths = sorted({h.split(" (", 1)[0] for h in hits if not h.startswith("(")})
    ruling, note = owner_ruling(cfg, rid, roots, str(base.get("head") or ""), ops, paths)
    if ruling:
        return True, f"{len(hits)} protected path change(s) covered by Owner ruling {ruling}: {', '.join(hits[:5])}"
    return False, (f"{len(hits)} change(s) to a protected path (the Owner's): {', '.join(hits[:5])}"
                   f"{' …' if len(hits) > 5 else ''}; {note}. Revert it, or cite an Owner ruling "
                   f"(SIGNED_RULING <id> in PROGRESS.md: RULED/RATIFIED in DECISIONS.md, committed by the "
                   f"operator, naming {rid} or the path)")


# -- Owner rulings --------------------------------------------------------------------------
OP_SCAN = 200   # newest operator commits searched for the lines that added a ruling


def owner_ruling(cfg, rid: str, roots: List[Path], head: str, ops: List[str],
                 paths: List[str]) -> Tuple[Optional[str], str]:
    """An Owner ruling covers a protected change when PROGRESS names it (SIGNED_RULING <id>, never
    SW-) and:
      * its entry line (the `### <id>` heading or the `| <id> |` row) was ADDED to DECISIONS.md by an
        operator `plan:` commit (state/protected_commits.jsonl, reachable from HEAD) — text a session
        wrote, committed or left in the working tree, signs nothing;
      * in DECISIONS.md as of the NEWEST operator commit it is signed (RULED/RATIFIED by its heading or
        status line / its leading table cell, first status word RULED/RATIFIED) and active (not
        superseded or withdrawn) — an Owner withdrawal wins;
      * it names the row, or every changed protected path (full path or file name)."""
    import close_check as cc
    import decisions
    named: List[str] = []
    for root in roots:
        p = root / "PROGRESS.md"
        if p.is_file():
            named += cc.SIGNED_LINE.findall(p.read_text(errors="replace"))
    named = list(dict.fromkeys(named))
    owner = [n for n in named if not n.startswith(cc.STEWARD_PREFIX)]
    if not named:
        return None, "no SIGNED_RULING line in PROGRESS.md"
    if not owner:
        return None, f"{', '.join(named)} is a Steward ruling — only an Owner ruling covers a protected path"
    dec = cfg.workspace / decisions.LEDGER
    try:
        rel = _prefix(cfg) + str(dec.resolve().relative_to(cfg.git_repo.resolve()))
    except ValueError:
        return None, "DECISIONS.md is outside the guard repo — no operator-committed ruling can be read"
    rc, out = _git(cfg, "log", "--format=%H", f"-{OP_SCAN * 5}", "HEAD", "--", f":(top){rel}")
    touching = out.split() if rc == 0 else []
    opset = operator_commits(cfg)
    op_shas = [sha for sha in touching if sha in opset][:OP_SCAN]   # newest first
    if not op_shas:
        return None, ("no operator plan: commit of DECISIONS.md is reachable from HEAD — an Owner ruling is "
                      "committed with `orch commit plan:<tag> -- DECISIONS.md` from the operator's shell")
    added: set = set()
    for sha in op_shas:
        rc, diff = _git(cfg, "show", "--format=", "--no-renames", "-U0", sha, "--", f":(top){rel}")
        added |= {l[1:].strip() for l in diff.splitlines() if l.startswith("+") and not l.startswith("+++")}
    rc, text = _git(cfg, "show", f"{op_shas[0]}:{rel}")
    if rc != 0:
        return None, f"DECISIONS.md cannot be read at operator commit {op_shas[0][:7]}"
    entries = decisions.parse(text)
    decisions.resolve(entries, {})
    found: Dict[str, str] = {}
    for n in owner:
        for e in decisions.find(entries, n):
            if e.head.strip() not in added:
                found.setdefault(n, "not added by an operator commit")
                continue
            if not (e.status in SIGNED and e.active) or \
                    (e.kind == "heading" and not cc.entry_signed(e.text, n)) or \
                    (e.kind == "table" and not cc.table_entry_signed(e)):
                found[n] = "not signed and active (RULED/RATIFIED, not superseded or withdrawn)"
                continue
            named_in = lambda x: bool(re.search(rf"(?<![\w./-]){re.escape(x)}(?![\w-])", e.text))
            if named_in(rid) or all(named_in(p) or named_in(Path(p).name) for p in paths):
                return n, f"{n} is signed in DECISIONS.md (operator commit {op_shas[0][:7]})"
            found[n] = f"names neither {rid} nor every changed path ({', '.join(paths[:3])})"
    why = "; ".join(f"{n}: {found.get(n, 'not in DECISIONS.md as the operator last committed it')}" for n in owner)
    return None, f"no Owner ruling covers it — {why}"


def main(argv: List[str]) -> int:
    from config_parser import load
    cfg = load()
    if argv[:1] == ["list"]:
        for g in patterns(cfg):
            print(g)
        return 0
    if argv[:1] == ["match"]:
        pats = patterns(cfg)
        pre = _prefix(cfg)
        hit = 0
        items = argv[1:] if argv[1:] != ["-"] else [l for l in sys.stdin.read().split("\n") if l]
        for p in items:
            rel = _repo_rel(p, pre) if pre and _norm(p).startswith(pre) else _norm(p)
            g = matches(rel, pats) if rel else None
            if g:
                hit = 1
                print(f"{p}\t{g}")
        return 3 if hit else 0
    if argv[:1] == ["record"] and len(argv) >= 3:
        import os
        import capability
        if os.environ.get("ORCH_ROW") or os.environ.get("ORCH_KIND") or capability.session(cfg):
            print("REFUSED: only the operator's own plan: commit is recorded")
            return 1
        record(cfg, argv[1], argv[2], argv[3:])
        return 0
    print(__doc__.split("usage:")[1].strip())
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
