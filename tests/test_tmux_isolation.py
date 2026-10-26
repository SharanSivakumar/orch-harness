#!/usr/bin/env python3
"""Guard drills: the framework's tests run on a PRIVATE tmux server, and a tree kill can never take a tmux server,
the caller's own lineage, or a process outside the tagged set down.

A real rig lives on the user's default tmux server. These drills never touch it: they only ever ask it a
read-only question (`has-session` through its socket path) and run everything else on a private server.

    python3 -m unittest tests/test_tmux_isolation.py -v
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "lib"))
import tmux_isolation  # noqa: E402
import tmuxio  # noqa: E402


def tm(*args, env=None):
    a = list(args) if args and args[0] == "-L" else [*tmuxio.socket_args(), *args]
    return subprocess.run(["tmux", *a], capture_output=True, text=True, env=env or os.environ)


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return "Z" not in Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except OSError:
        return False


class Isolation(unittest.TestCase):
    def test_environment_is_private(self):
        sock = tmux_isolation.assert_isolated()
        self.assertTrue(sock.startswith(os.environ["TMUX_TMPDIR"]), sock)
        self.assertNotEqual(sock, tmux_isolation.default_socket())
        self.assertNotIn("TMUX", os.environ)

    def test_a_session_started_here_is_not_on_the_default_server(self):
        tm("new-session", "-d", "-s", "isodrill1", "sleep 60")
        self.addCleanup(tm, "kill-session", "-t", "=isodrill1")
        self.assertEqual(tm("has-session", "-t", "=isodrill1").returncode, 0)
        # the default server (read-only question, by socket path): no such session
        p = subprocess.run(["tmux", "-S", tmux_isolation.default_socket(), "has-session", "-t", "=isodrill1"],
                           capture_output=True, text=True, env={"PATH": os.environ["PATH"]})
        self.assertNotEqual(p.returncode, 0, "the drill session leaked onto the default tmux server")

    def test_guard_fails_a_test_that_would_act_on_the_default_server(self):
        base = dict(os.environ)
        bad_inside_pane = dict(base, TMUX="/tmp/tmux-1000/default,1,0")
        with self.assertRaises(AssertionError):
            tmux_isolation.assert_isolated(bad_inside_pane)          # raised before any tmux call
        no_private_dir = {k: v for k, v in base.items() if k != "TMUX_TMPDIR"}
        with self.assertRaises(AssertionError):
            tmux_isolation.assert_isolated(no_private_dir)
        home_dir = dict(base, TMUX_TMPDIR=str(Path.home()))
        with self.assertRaises(AssertionError):
            tmux_isolation.assert_isolated(home_dir)

    def test_tmuxio_runs_on_the_named_socket(self):
        self.assertEqual(tmuxio.socket_args(), [], "unset: the default server, unchanged")
        name = f"orchtest-sock-{os.getpid()}"
        with mock.patch.dict(os.environ, {"ORCH_TMUX_SOCKET": name}):
            self.assertEqual(tmuxio.socket_args(), ["-L", name])
            tmuxio.ensure_session("isodrill2", tempfile.gettempdir())
            self.addCleanup(tm, "-L", name, "kill-session", "-t", "=isodrill2")
            self.assertTrue(tmuxio.has_session("isodrill2"))
        self.assertEqual(tm("-L", name, "has-session", "-t", "=isodrill2").returncode, 0)
        self.assertNotEqual(tm("has-session", "-t", "=isodrill2").returncode, 0, "not on the private default socket")


class TreeKillSafety(unittest.TestCase):
    """killtree / tagged / tree / kill never select a tmux process, the caller's lineage, or a bystander."""

    def test_a_tmux_server_that_inherited_a_launch_tag_is_never_killed(self):
        tag = f"drill.{os.getpid()}.{time.time_ns()}"
        env = dict(os.environ, ORCH_LAUNCH_TAG=tag)
        # a second private tmux server whose environment carries the tag (it was started from a tagged pane)
        sock = f"orchtest-tag-{os.getpid()}"
        pid_of_server = [0]
        tm("-L", sock, "new-session", "-d", "-s", "tg", "sleep 120", env=env)
        self.addCleanup(lambda: [os.kill(int(x), signal.SIGTERM) for x in [pid_of_server[0]] if x and alive(int(x))])
        tm("-L", sock, "set-option", "-g", "exit-empty", "off")   # the server outlives its (tag-carrying) pane process
        pid = int(tm("-L", sock, "display-message", "-p", "-t", "tg", "#{pid}").stdout.strip())
        pid_of_server[0] = pid
        self.assertTrue(tmuxio.is_tmux(pid))
        # an ordinary orphan carrying the tag (the thing killtree is for), and a bystander without it
        orphan = subprocess.Popen(["sleep", "120"], env=env, start_new_session=True, stdin=subprocess.PIPE)
        bystander = subprocess.Popen(["sleep", "120"], start_new_session=True, stdin=subprocess.PIPE)
        self.addCleanup(lambda: [p.kill() for p in (orphan, bystander) if p.poll() is None])
        tagged = {p for p, _ in tmuxio.tagged(tag)}
        self.assertIn(orphan.pid, tagged)
        self.assertNotIn(pid, tagged, "a tmux server is never a tagged target")
        self.assertNotIn(bystander.pid, tagged)
        self.assertEqual(tmuxio.tree(pid), [], "a tmux server is not a tree")
        start = tmuxio._stat(pid)[2]
        tmuxio._signal_all([(pid, start)], signal.SIGTERM)
        time.sleep(0.5)
        self.assertTrue(alive(pid), "_signal_all refuses a tmux process")
        # killtree from a process that carries the tag itself: it ends the orphan, not itself, not tmux, not the bystander
        marker = Path(tempfile.mkdtemp()) / "done"
        code = (f"import sys; sys.path.insert(0, {str(HERE.parent / 'lib')!r}); import tmuxio, pathlib; "
                f"tmuxio.killtree({orphan.pid}, 1.0, {tag!r}); pathlib.Path({str(marker)!r}).write_text('ok')")
        p = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(marker.read_text(), "ok", "the caller survived its own killtree")
        orphan.wait(timeout=10)
        self.assertFalse(alive(orphan.pid))
        self.assertTrue(alive(bystander.pid), "a process outside the tag is untouched")
        self.assertTrue(alive(pid), "the tmux server survived")
        # (the pane's own process inherited the tag, so it was a legitimate target; the server was not)

    def test_window_kill_ends_the_pane_tree_but_not_the_server(self):
        tmuxio.ensure_session("isodrill3", tempfile.gettempdir())
        self.addCleanup(tm, "kill-session", "-t", "=isodrill3")
        ident = tmuxio.new_window("isodrill3", "w1", tempfile.gettempdir(), "sh -c 'sleep 120 & wait'")
        self.assertTrue(ident)
        time.sleep(0.5)
        pids = [p for p, _ in tmuxio.tree(int(ident.split()[1]))]
        self.assertGreaterEqual(len(pids), 2)
        server = int(tm("display-message", "-p", "-t", "isodrill3", "#{pid}").stdout.strip())
        self.assertNotIn(server, pids)
        self.assertTrue(tmuxio.kill("isodrill3", "w1", grace=1.0))
        end = time.time() + 8
        while time.time() < end and any(alive(p) for p in pids):
            time.sleep(0.2)
        self.assertFalse([p for p in pids if alive(p)])
        self.assertTrue(alive(server))
        self.assertTrue(tmuxio.has_session("isodrill3"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
