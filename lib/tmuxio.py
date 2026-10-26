#!/usr/bin/env python3
"""tmux, read and written in one place.

One `list-windows` per engine tick feeds every liveness question. Identity is
"<window_id> <pane_pid>": a window that merely wears a row's name is not that
row's session (v1 ORPHAN rule, kept).
"""
from __future__ import annotations

import os
import re
import signal
import subprocess
import threading
import time
from typing import Dict, List, Optional, Tuple

ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07")
FMT = "#{window_name}\t#{pane_dead}\t#{window_activity}\t#{window_id}\t#{pane_pid}\t#{pane_dead_status}"


def socket_args() -> List[str]:
    """`ORCH_TMUX_SOCKET=<name>` runs every tmux call of the framework on that private server (`tmux -L <name>`).
    Unset (the default, and every real rig) is the user's default server, unchanged. Tests and sandboxes set it
    (with a private TMUX_TMPDIR and no TMUX) so that nothing they do can reach a real rig's server."""
    name = os.environ.get("ORCH_TMUX_SOCKET", "").strip()
    return ["-L", name] if name else []


def _run(args: List[str], timeout: int = 15, data: Optional[str] = None) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(["tmux", *socket_args(), *args], capture_output=True, text=True, timeout=timeout, input=data)
    except Exception as e:  # tmux missing or wedged: callers treat it as "no windows"
        return subprocess.CompletedProcess(args, 99, "", str(e))


class Win(dict):
    @property
    def ident(self) -> str:
        return f"{self['wid']} {self['pid']}"


def windows(session: str) -> Dict[str, Win]:
    """name -> Win(alive, dead_status, activity, wid, pid). If two windows share a name,
    the live one wins (a dead pane left by remain-on-exit is never a candidate)."""
    p = _run(["list-windows", "-t", f"={session}", "-F", FMT])
    out: Dict[str, Win] = {}
    if p.returncode != 0:
        return out
    for line in p.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) < 5:
            continue
        name, dead, act, wid, pid = parts[:5]
        dstat = parts[5] if len(parts) > 5 else ""
        w = Win(alive=(dead != "1"), activity=int(act) if act.isdigit() else 0,
                wid=wid, pid=pid, dead_status=dstat)
        if name in out and out[name]["alive"] and not w["alive"]:
            continue
        out[name] = w
    return out


def has_session(session: str) -> bool:
    return _run(["has-session", "-t", f"={session}"]).returncode == 0


def ensure_session(session: str, cwd: str) -> None:
    if not has_session(session):
        _run(["new-session", "-d", "-s", session, "-n", "control", "-c", cwd])
    # dead panes stay so the engine can read their exit status; the reaper clears them
    _run(["set-option", "-t", f"={session}", "remain-on-exit", "on"])


def new_window(session: str, name: str, cwd: str, command: str) -> Optional[str]:
    """-> '<window_id> <pane_pid>' of the window created, or None."""
    p = _run(["new-window", "-d", "-P", "-F", "#{window_id} #{pane_pid}", "-t", f"={session}:",
              "-n", name, "-c", cwd, command])
    if p.returncode != 0:
        return None
    return p.stdout.strip() or None


def _stat(pid: int) -> Optional[Tuple[int, int, str]]:
    """(ppid, sid, starttime) from /proc/<pid>/stat, or None once the process is gone."""
    try:
        with open(f"/proc/{pid}/stat") as fh:
            f = fh.read().rsplit(")", 1)[1].split()
        return int(f[1]), int(f[3]), f[19]
    except (OSError, IndexError, ValueError):
        return None


def _detached(pid: int, sid: int) -> bool:
    """A job the session deliberately detached (`orch longrun`: setsid nohup, stdin /dev/null)
    outlives its window; a helper that merely leads its own session (stdin a pipe or socket) does not."""
    try:
        return sid == pid and os.readlink(f"/proc/{pid}/fd/0") == "/dev/null"
    except OSError:
        return False


def is_tmux(pid: int) -> bool:
    """A tmux server or client. Never a target of a tree kill: a pane's process tree, or a process that merely
    inherited a launch tag from the pane that started it (a test sandbox's tmux server!), must not take a whole
    tmux server — and with it every other session on it — down."""
    try:
        with open(f"/proc/{pid}/comm") as fh:
            return fh.read().strip().startswith("tmux")
    except OSError:
        return False


def tree(root: int) -> List[Tuple[int, str]]:
    """root and every descendant as (pid, starttime), skipping detached jobs and their subtrees, and any tmux
    process (see is_tmux)."""
    kids: Dict[int, List[int]] = {}
    info: Dict[int, Tuple[int, int, str]] = {}
    for d in os.listdir("/proc"):
        if d.isdigit() and (st := _stat(int(d))):
            info[int(d)] = st
            kids.setdefault(st[0], []).append(int(d))
    out, todo = [], [root]
    while todo:
        pid = todo.pop()
        if pid not in info or is_tmux(pid) or (pid != root and _detached(pid, info[pid][1])):
            continue
        out.append((pid, info[pid][2]))
        todo.extend(kids.get(pid, []))
    return out


def _lineage() -> List[int]:
    """This process and its ancestors: a window kill never signals the caller's own chain."""
    out, pid = [], os.getpid()
    while pid > 1 and (st := _stat(pid)):
        out.append(pid)
        pid = st[0]
    return out


def _signal_all(procs: List[Tuple[int, str]], sig: int) -> None:
    for pid, start in procs:
        if pid <= 1 or is_tmux(pid):       # last line of defence: never init, never a tmux server or client
            continue
        st = _stat(pid)
        if st and st[2] == start:          # the same process, never a reused pid
            try:
                os.kill(pid, sig)
            except OSError:
                pass


def kill(session: str, target: str, grace: float = 5.0) -> bool:
    """target: a window id (@12) or a name. Closing a window HUPs its pane, which a TUI such as
    OpenCode ignores (and its `serve` helper leads its own session), so the pane's process tree
    is ended too: SIGTERM now, SIGKILL after `grace` seconds. Detached jobs are left alone."""
    t = target if target.startswith("@") else f"={session}:={target}"
    # list-panes, never display-message: display-message falls back to the session's current
    # window when the target is gone, and that window may be the engine's own.
    p = _run(["list-panes", "-t", t, "-F", "#{pane_pid}"])
    pids = [int(x) for x in p.stdout.split() if x.isdigit()] if p.returncode == 0 else []
    mine = set(_lineage())
    procs = [pr for pid in pids for pr in tree(pid) if pr[0] not in mine]
    ok = _run(["kill-window", "-t", t]).returncode == 0
    if procs:
        _signal_all(procs, signal.SIGTERM)
        threading.Thread(target=_finish, args=(procs, grace)).start()
    return ok


def _finish(procs: List[Tuple[int, str]], grace: float) -> None:
    """SIGKILL whatever of `procs` is still alive after `grace` seconds; return early once all are gone."""
    end = time.time() + grace
    while time.time() < end:
        if not any((st := _stat(pid)) and st[2] == start for pid, start in procs):
            return
        time.sleep(0.2)
    _signal_all(procs, signal.SIGKILL)


def capture(session: str, target: str, lines: int = 60) -> str:
    t = target if target.startswith("@") else f"={session}:={target}"
    p = _run(["capture-pane", "-p", "-J", "-t", t, "-S", f"-{lines}"])
    return ANSI.sub("", p.stdout or "")


def tail_lines(text: str, n: int) -> List[str]:
    keep = [l.rstrip() for l in text.splitlines() if l.strip()]
    return keep[-n:]


def send_text(session: str, target: str, text: str, enter: bool = True) -> bool:
    """Type text into a pane as a paste (one turn), then Enter."""
    t = target if target.startswith("@") else f"={session}:={target}"
    buf = f"orch-{abs(hash((target, text))) % 10**8}"
    if _run(["load-buffer", "-b", buf, "-"], data=text).returncode != 0:
        return False
    if _run(["paste-buffer", "-d", "-b", buf, "-t", t]).returncode != 0:
        return False
    if enter:
        _run(["send-keys", "-t", t, "Enter"])
    return True


def send_keys(session: str, target: str, *keys: str) -> bool:
    t = target if target.startswith("@") else f"={session}:={target}"
    return _run(["send-keys", "-t", t, *keys]).returncode == 0


def respawn(session: str, name: str, command: str) -> bool:
    return _run(["respawn-window", "-k", "-t", f"={session}:={name}", command]).returncode == 0


def session_created(session: str) -> int:
    """Epoch the tmux session was (re)created; 0 when it does not exist. A session newer than a
    dispatch means the tmux server (or the host) went away under that dispatch."""
    p = _run(["list-sessions", "-F", "#{session_name}\t#{session_created}"])
    if p.returncode != 0:
        return 0
    for line in (p.stdout or "").splitlines():
        name, _, created = line.partition("\t")
        if name == session and created.strip().isdigit():
            return int(created.strip())
    return 0


def boot_id() -> str:
    """This host boot's id (Linux); '' elsewhere. ORCH_BOOT_ID overrides it (tests)."""
    import os
    if os.environ.get("ORCH_BOOT_ID"):
        return os.environ["ORCH_BOOT_ID"]
    try:
        return open("/proc/sys/kernel/random/boot_id").read().strip()
    except Exception:
        return ""


def tagged(tag: str) -> List[Tuple[int, str]]:
    """Processes whose environment carries ORCH_LAUNCH_TAG=<tag> (lib/launch.sh sets it for its adapter): the
    adapter's descendants that were orphaned (their parent died first) and so are no longer in `tree()`.
    Jobs the session deliberately detached are skipped, as in tree()."""
    if not tag:
        return []
    needle = f"ORCH_LAUNCH_TAG={tag}".encode() + b"\0"
    out = []
    for d in os.listdir("/proc"):
        if not d.isdigit():
            continue
        pid = int(d)
        st = _stat(pid)
        if not st or pid <= 1 or is_tmux(pid) or _detached(pid, st[1]):
            continue
        try:
            with open(f"/proc/{pid}/environ", "rb") as fh:
                if needle in fh.read():
                    out.append((pid, st[2]))
        except OSError:
            continue
    return out


def killtree(root: int, grace: float = 5.0, tag: str = "") -> None:
    """End `root`, its descendants and any orphaned process carrying `tag` (not this process's own chain):
    SIGTERM now, SIGKILL after `grace` seconds, waiting here. lib/launch.sh calls it when its window is closed
    from outside (a plain `tmux kill-window`, a lost tmux server)."""
    mine = set(_lineage())
    procs = {pr for pr in tree(root) + tagged(tag) if pr[0] not in mine}
    if not procs:
        return
    procs = sorted(procs)
    _signal_all(procs, signal.SIGTERM)
    _finish(procs, grace)
    # whatever was started or orphaned meanwhile
    late = [pr for pr in tagged(tag) if pr[0] not in mine and pr not in procs]
    if late:
        _signal_all(late, signal.SIGKILL)


if __name__ == "__main__":
    import sys
    if len(sys.argv) >= 3 and sys.argv[1] == "killtree":
        for _sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
            signal.signal(_sig, signal.SIG_IGN)     # a group-wide signal must not stop the clean-up
        killtree(int(sys.argv[2]), float(sys.argv[3]) if len(sys.argv) > 3 else 5.0,
                 sys.argv[4] if len(sys.argv) > 4 else "")
        sys.exit(0)
    print("usage: tmuxio.py killtree <pid> [grace-seconds] [tag]")
    sys.exit(2)
