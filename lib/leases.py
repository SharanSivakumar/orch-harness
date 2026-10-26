#!/usr/bin/env python3
"""Resource leases with slots, scopes, all-or-nothing acquisition and stale reclaim.

v1 leases were bare `mkdir` locks keyed by row id: one slot each, waited on one
at a time while holding the earlier ones, invisible to other rigs, and
never reclaimed when the holder died (a stale lock could sit indefinitely).

Layout (compatible with v1 for 1-slot instance leases):

    <dir>/<name>.lock/owner          slot 1 (the v1 path, so v1 readers still see it)
    <dir>/<name>#<k>.lock/owner      slots 2..N

`<dir>` is `<instance>/leases` for scope `instance`, `~/.local/state/orch/leases`
for scope `host` (shared by every rig on the machine, e.g. one local Postgres).

owner file lines: owner id · time · pid (0 = a span with no process) · instance

A lease is STALE when its recorded pid is gone, or (pid 0) when the owner row is
not running in its instance (its window is not live and it has a claim on disk).
Stale leases are reclaimed by the next acquirer, and the reclaim is printed.

CLI (used by parallel_guard_core.sh and `orch lease`):
    leases.py acquire <owner> <res>[,<res>...] [--pid N] [--wait] [--span]
    leases.py release <owner> <res>[,<res>...]
    leases.py release-all <owner>
    leases.py status
Exit: 0 acquired/released · 10 every resource was already held by <owner> · 1 refused/busy
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config_parser import Config, load  # noqa: E402

HOST_DIR = Path(os.path.expanduser("~/.local/state/orch/leases"))


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


class Leases:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.res = cfg.resources()

    def dir_for(self, name: str) -> Path:
        scope = self.res.get(name, {}).get("scope", "instance")
        d = HOST_DIR if scope == "host" else self.cfg.leases
        d.mkdir(parents=True, exist_ok=True)
        return d

    def slot_paths(self, name: str) -> List[Path]:
        d = self.dir_for(name)
        n = self.res.get(name, {}).get("slots", 1)
        return [d / f"{name}.lock"] + [d / f"{name}#{k}.lock" for k in range(2, n + 1)]

    @staticmethod
    def read_owner(lock: Path) -> Dict[str, str]:
        try:
            lines = (lock / "owner").read_text().splitlines()
        except Exception:
            return {}
        keys = ["owner", "since", "pid", "instance"]
        return {k: (lines[i].strip() if i < len(lines) else "") for i, k in enumerate(keys)}

    def holders(self, name: str) -> List[Tuple[Path, Dict[str, str]]]:
        return [(p, self.read_owner(p)) for p in self.slot_paths(name) if p.is_dir()]

    def is_stale(self, info: Dict[str, str], row_running=None) -> Tuple[bool, str]:
        """row_running(owner, instance) -> bool lets the engine answer for span leases."""
        if not info:
            return True, "no owner file"
        try:
            pid = int(info.get("pid") or 0)
        except ValueError:
            pid = 0
        if pid > 0:
            return (not pid_alive(pid)), f"holder pid {pid} is gone"
        if row_running is not None:
            running = row_running(info.get("owner", ""), info.get("instance", ""))
            if running is False:
                return True, f"span owner {info.get('owner')} is not running"
        return False, ""

    def _take(self, name: str, owner: str, pid: int, row_running=None) -> Tuple[Optional[Path], str]:
        for p in self.slot_paths(name):
            info = self.read_owner(p) if p.is_dir() else None
            if info is not None:
                stale, why = self.is_stale(info, row_running)
                if not stale:
                    continue
                shutil.rmtree(p, ignore_errors=True)
                print(f"[lease] reclaimed stale '{p.name}' from {info.get('owner', '?')} ({why})")
            try:
                p.mkdir()
            except FileExistsError:
                continue
            (p / "owner").write_text(f"{owner}\n{time.strftime('%F %T')}\n{pid}\n{self.cfg.instance}\n")
            return p, ""
        held = ", ".join(i.get("owner", "?") for _, i in self.holders(name))
        return None, held

    def owned(self, name: str, owner: str) -> Optional[Path]:
        for p, info in self.holders(name):
            if info.get("owner") == owner and info.get("instance", str(self.cfg.instance)) in ("", str(self.cfg.instance)):
                return p
        return None

    def acquire_all(self, owner: str, names: List[str], pid: int = 0,
                    row_running=None) -> Tuple[bool, List[str], str]:
        """All-or-nothing. -> (ok, newly_taken, why_not). Already-owned leases are
        re-entrant: they count as held and are not in newly_taken."""
        unknown = [n for n in names if n not in self.res]
        if unknown:
            return False, [], f"unknown resource(s): {', '.join(unknown)} (have: {', '.join(self.res)})"
        taken: List[Path] = []
        newly: List[str] = []
        for n in sorted(set(names)):
            if self.owned(n, owner):
                continue
            p, held = self._take(n, owner, pid, row_running)
            if p is None:
                for t in taken:
                    shutil.rmtree(t, ignore_errors=True)
                return False, [], f"'{n}' busy (held by {held})"
            taken.append(p)
            newly.append(n)
        return True, newly, ""

    def release(self, owner: str, names: List[str]) -> List[str]:
        out = []
        for n in names:
            p = self.owned(n, owner)
            if p:
                shutil.rmtree(p, ignore_errors=True)
                out.append(n)
        return out

    def release_all(self, owner: str) -> List[str]:
        return self.release(owner, list(self.res))

    def status_lines(self, row_running=None) -> List[str]:
        out = []
        for n, spec in self.res.items():
            hs = self.holders(n)
            label = f"{n} [{spec['slots']} slot{'s' if spec['slots'] > 1 else ''}, {spec['scope']}]"
            if not hs:
                out.append(f"  {label:<36} free")
                continue
            for p, info in hs:
                stale, why = self.is_stale(info, row_running)
                inst = info.get("instance", "")
                other = f" (rig {Path(inst).name})" if inst and inst != str(self.cfg.instance) else ""
                out.append(f"  {label:<36} HELD by {info.get('owner', '?')}{other} since {info.get('since', '?')}"
                           + (f"  STALE: {why}" if stale else ""))
        return out


def main() -> int:
    ap = argparse.ArgumentParser(prog="leases")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("acquire")
    a.add_argument("owner"); a.add_argument("res")
    a.add_argument("--pid", type=int, default=0)
    a.add_argument("--wait", action="store_true")
    a.add_argument("--poll", type=int, default=15)
    r = sub.add_parser("release"); r.add_argument("owner"); r.add_argument("res")
    ra = sub.add_parser("release-all"); ra.add_argument("owner")
    sub.add_parser("status")
    args = ap.parse_args()
    cfg = load()
    L = Leases(cfg)

    def row_running(owner: str, inst: str) -> Optional[bool]:
        if inst and inst != str(cfg.instance):
            return None  # another rig's span: not ours to judge
        try:
            from store import Store
            import tmuxio
            st = Store(cfg.state)
            v, _, _ = st.status(owner)
            live = tmuxio.windows(cfg.tmux_session).get(owner, {}).get("alive", False)
            return bool(live or v == "NONE")
        except Exception:
            return None

    if args.cmd == "acquire":
        names = [x for x in args.res.split(",") if x]
        waited = 0
        while True:
            ok, newly, why = L.acquire_all(args.owner, names, args.pid, row_running)
            if ok:
                if not newly:
                    print(f"[lease] {','.join(names)} already held by {args.owner} — re-entrant")
                    return 10
                print(f"[lease] {','.join(newly)} ACQUIRED by {args.owner}")
                return 0
            if not args.wait or why.startswith("unknown"):
                print(f"[lease] REFUSED: {why}")
                return 1
            if waited % 300 == 0:
                print(f"{time.strftime('%F %T')} [lease] {why} — {args.owner} waiting {waited}s "
                      f"(all-or-nothing: nothing is held while waiting)", flush=True)
            time.sleep(args.poll)
            waited += args.poll
    if args.cmd == "release":
        rel = L.release(args.owner, [x for x in args.res.split(",") if x])
        print(f"[lease] released {','.join(rel) or 'nothing'} for {args.owner}")
        return 0
    if args.cmd == "release-all":
        rel = L.release_all(args.owner)
        print(f"[lease] released {','.join(rel) or 'nothing'} for {args.owner}")
        return 0
    print("\n".join(L.status_lines(row_running)) or "  (no resources declared)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
