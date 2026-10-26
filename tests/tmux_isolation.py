"""Every framework test that starts tmux sessions runs them on a PRIVATE tmux server.

Import this module first (before anything starts tmux). It points TMUX_TMPDIR at a directory of its own, removes
$TMUX (a process started from inside a tmux pane has it set, and tmux then talks to THAT server whatever else is
asked). ORCH_TMUX_SOCKET (lib/tmuxio.py runs `tmux -L <name>`) is left unset here so that plain `tmux …` calls of a
test, the engine and the adapters all land on the same private server, never on the user's default server — where a real rig lives.

`assert_isolated()` is the guard each test class calls in setUpClass: it fails the test unless the tmux it would
use is not the default server.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile

_OWN = not os.environ.get("ORCH_TEST_TMUX_DIR")
_DIR = os.environ.get("ORCH_TEST_TMUX_DIR") or tempfile.mkdtemp(prefix="otm-")
os.environ["ORCH_TEST_TMUX_DIR"] = _DIR
os.environ["TMUX_TMPDIR"] = _DIR
os.environ.pop("TMUX", None)
os.environ.pop("ORCH_TMUX_SOCKET", None)   # the private TMUX_TMPDIR's "default" socket is shared by plain `tmux` and tmuxio


def default_socket() -> str:
    """The path of the user's default tmux server socket (as tmux would use it with a clean environment)."""
    return f"/tmp/tmux-{os.getuid()}/default"


def resolved_socket(env: dict | None = None) -> str:
    """Where `tmux` would put/find its server for this environment, asked of tmux itself."""
    env = dict(os.environ if env is None else env)
    args = ["tmux"]
    if env.get("ORCH_TMUX_SOCKET"):
        args += ["-L", env["ORCH_TMUX_SOCKET"]]
    probe = f"orchprobe{os.getpid()}"
    p = subprocess.run(args + ["new-session", "-d", "-s", probe, "-x", "20", "-y", "5", "sleep 30"],
                       capture_output=True, text=True, env=env)
    if p.returncode != 0:
        return f"?{p.stderr.strip()}"
    try:
        q = subprocess.run(args + ["display-message", "-p", "-t", probe, "#{socket_path}"], capture_output=True,
                           text=True, env=env)
        return q.stdout.strip()
    finally:
        subprocess.run(args + ["kill-session", "-t", f"={probe}"], capture_output=True, env=env)


def assert_isolated(env: dict | None = None) -> str:
    """Raise AssertionError unless a tmux started with `env` (default: this process) is on a private server."""
    e = dict(os.environ if env is None else env)
    if e.get("TMUX"):
        raise AssertionError("TMUX is set: tmux would use the server of the pane this runs in")
    if not (e.get("TMUX_TMPDIR") or "").startswith(tempfile.gettempdir()):
        raise AssertionError("TMUX_TMPDIR is not a private temp directory")
    sock = resolved_socket(e)
    if sock == default_socket() or not sock.startswith(e.get("TMUX_TMPDIR", "\0")):
        raise AssertionError(f"tmux would use {sock!r}, not a private server")
    return sock


def cleanup() -> None:
    if _OWN:
        shutil.rmtree(_DIR, ignore_errors=True)


import atexit  # noqa: E402
atexit.register(cleanup)
