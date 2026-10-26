#!/usr/bin/env python3
"""End-to-end tests of the v2 engine in an isolated sandbox.

A throwaway project (own git repo, own tmux session, own instance) runs real
ladders through the real engine with the `mock` executor: no model, no network,
no spend, and never a real rig. Each scenario uses its own lane so they share
one engine.

    python3 -m unittest tests/test_v2_engine.py -v
    KEEP=1 python3 -m unittest tests/test_v2_engine.py   # keep the sandbox
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

HOME = Path(__file__).resolve().parent.parent
ORCH = HOME / "bin" / "orch"
sys.path.insert(0, str(HOME / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import tmux_isolation  # noqa: E402,F401  — a PRIVATE tmux server for every session this suite starts

SBX = Path(tempfile.mkdtemp(prefix="orch-v2-sbx-"))
SESSION = f"orchv2t{os.getpid()}"
WS = SBX / "project" / "cycle"
INST = WS / "ORCH"
# Defence in depth: no real agent CLI can ever start from this suite. Every model
# binary on PATH is shadowed by a stub that exits at once.
STUBS = SBX / "stubs"
STUBS.mkdir(parents=True)
for b in ("claude", "opencode", "codex", "gemini"):
    (STUBS / b).write_text("#!/bin/sh\necho \"test stub: $0 refused\"; exit 97\n")
    (STUBS / b).chmod(0o755)
ENV = dict(os.environ, ORCH_INSTANCE=str(INST), ORCH_ALLOW_DIRTY="1",
           PATH=f"{STUBS}:{os.environ.get('PATH', '')}")
ENV.pop("ORCH_ROW", None)
os.environ["PATH"] = ENV["PATH"]

PLAN = """# PLAN — {id}

Rulings: {rulings}

## Duties
1. do the thing

## Acceptance
- the thing is done

## Verification
```bash
{verify}
```
"""


def orch(*args: str, check: bool = False, env: dict | None = None) -> subprocess.CompletedProcess:
    p = subprocess.run([str(ORCH), *args], capture_output=True, text=True, env=env or ENV, cwd=str(WS))
    if check and p.returncode != 0:
        raise AssertionError(f"orch {' '.join(args)} failed:\n{p.stdout}\n{p.stderr}")
    return p


def store():
    from store import Store
    return Store(INST / "state")


def phase(rid: str) -> str:
    return store().phase(rid)


def wait(cond, timeout: float = 60, every: float = 0.5, what: str = "") -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(every)
    raise AssertionError(f"timed out waiting for {what or cond}")


def events(rid: str):
    return [e for e in store().events(rid)]


def ctl(name: str, text: str = "") -> None:
    (INST / "control" / name).write_text(text)


def add_lane(lane: str, rows: list, mode: str = "") -> None:
    """rows: (id, deps, gate, executor, extras)"""
    lines = []
    for rid, deps, gate, x, extras in rows:
        d = WS / f"PHASE_{lane}" / rid
        d.mkdir(parents=True, exist_ok=True)
        if not (d / "PLAN.md").exists():
            (d / "PLAN.md").write_text(PLAN.format(id=rid, rulings="-", verify="test -f PROGRESS.md"))
        lines.append(f"{rid}\t{deps}\t{gate}\t{x}\tPHASE_{lane}/{rid}/PLAN.md\t{extras}".rstrip())
    (INST / f"queue_{lane}.tsv").write_text("\n".join(lines) + "\n")
    if mode:
        cfg_set(f"scheduler.lane_modes.{lane}", mode)


def cfg_set(dotted: str, value) -> None:
    import yaml
    f = INST / "orch.yaml"
    data = yaml.safe_load(f.read_text())
    cur = data
    keys = dotted.split(".")
    for k in keys[:-1]:
        cur = cur.setdefault(k, {})
    cur[keys[-1]] = value
    f.write_text(yaml.safe_dump(data, sort_keys=False))
    time.sleep(1.2)   # mtime resolution + one engine tick to reload


def git(*args: str) -> str:
    return subprocess.run(["git", "-C", str(WS), *args], capture_output=True, text=True).stdout


class V2(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tmux_isolation.assert_isolated()
        WS.mkdir(parents=True)
        (WS / "DECISIONS.md").write_text("# decisions\n\n### DEC-1 — a ruling — RULED\nbody one\n\n"
                                         "### DEC-2 — a proposal (PROPOSED)\nquotes RULED but is not signed\n")
        git("init", "-q")
        git("config", "user.email", "t@t")
        git("config", "user.name", "t")
        git("add", "-A")
        git("commit", "-qm", "init")
        orch("init", str(INST), "--name", "sandbox", "--session", SESSION, check=True)
        import yaml
        f = INST / "orch.yaml"
        data = yaml.safe_load(f.read_text())
        data["executors"].update({"default": "mock", "allowed": ["mock", "mock2", "ghost", "manual"],
                                  "auto_unblock": ["mock"], "unblock_executor": "mock", "retier_on_max_unblock": "mock2",
                                  "max_unblock_attempts": 1, "flavors": {"mock2": "hard", "ghost": "hard"},
                                  "adapters": {"mock2": "mock"},
                                  "args": {"mock": ["--flag", "two words"]}, "env": {"mock": {"MOCK_ENV": "yes"}}})
        data["scheduler"] = {"mode": "serial", "tick_seconds": 1, "max_parallel": 12, "min_free_mb": 0}
        data["review"] = {"policy": "all", "reviewer_map": {"mock": "mock2", "mock2": "mock"},
                          "require_different_executor": True, "max_rejects": 2}
        data["close"] = {"max_fail": 1}
        data["preflight"] = {"reviewer": "mock2"}
        data["review"]["default_reviewer"] = "mock2"
        data["review"]["fallback_reviewer"] = "mock"
        data["idle"] = {"nudge_after": 0}
        data["reaper"] = {"closed_grace_secs": 2}
        data["guard"]["resources"] = ["git-lock", {"name": "db", "slots": 1}, {"name": "pool", "slots": 2}]
        data["guard"]["shared_paths"] = ["DECISIONS.md"]
        f.write_text(yaml.safe_dump(data, sort_keys=False))
        # mock2 is the mock adapter under another name, so a reviewer runs on a "different executor"
        (HOME / "lib" / "adapters" / "mock2.sh").exists() or None
        (INST / "queue_A.tsv").unlink(missing_ok=True)
        add_lane("A", [("a1", "-", 0, "mock", ""), ("a2", "-", 0, "mock", "")])
        add_lane("B", [("b1", "a1", 0, "mock", "")], mode="dag")
        cls.engine_started = False

    @classmethod
    def tearDownClass(cls):
        subprocess.run(["tmux", "kill-session", "-t", f"={SESSION}"], capture_output=True)
        if os.environ.get("KEEP") == "1":
            print(f"\nsandbox kept: {SBX}")
        else:
            shutil.rmtree(SBX, ignore_errors=True)

    # -- 00 ------------------------------------------------------------------------
    def test_00_lint_and_start(self):
        p = orch("lint")
        self.assertEqual(p.returncode, 0, p.stdout)
        p = orch("start", "A", "B")
        self.assertIn("lanes enabled: A B", p.stdout, p.stdout + p.stderr)
        wait(lambda: (INST / "state" / "engine.heartbeat").exists(), 20, what="engine heartbeat")
        p = orch("start")
        self.assertIn("engine already running", p.stdout)

    def test_01_chain_review_accept(self):
        wait(lambda: phase("a1") == "accepted", 60, what="a1 accepted")
        ev = [e["event"] for e in events("a1")]
        for want in ("dispatch", "claim", "review_dispatch", "review_verdict", "accept"):
            self.assertIn(want, ev)
        res = store().result("a1")
        self.assertEqual(res["review"]["verdict"], "ACCEPT")
        self.assertEqual(store().dispatch("rv-a1-1")["executor"], "mock2", "reviewer must differ from the executor")
        self.assertTrue((INST / "reviews" / "a1-1.md").exists())
        # serial lane: a2 dispatched only after a1 was ACCEPTED; dag lane b1 waited for a1 too
        d_a2 = store().dispatch("a2")
        wait(lambda: store().dispatch("a2").get("epoch"), 30, what="a2 dispatch")
        acc_a1 = next(e["epoch"] for e in events("a1") if e["event"] == "accept")
        self.assertGreaterEqual(store().dispatch("a2")["epoch"], acc_a1)
        wait(lambda: phase("b1") == "accepted" and phase("a2") == "accepted", 60, what="a2,b1 accepted")
        self.assertGreaterEqual(store().dispatch("b1")["epoch"], acc_a1)
        # executor args and env reached the adapter
        wo = (INST / "prompts" / "a1.hard.md").read_text()
        self.assertTrue(wo.startswith("<!-- orch:generated"))
        self.assertNotIn("__ID__", wo)

    def test_02_lane_complete_once_and_reaper(self):
        wait(lambda: (INST / "state" / "notified" / "lane-complete-A-2").exists(), 30, what="lane A complete")
        # finished windows are reaped after the grace period
        import tmuxio
        wait(lambda: "a1" not in tmuxio.windows(SESSION) and "rv-a1-1" not in tmuxio.windows(SESSION), 30,
             what="reaper")

    def test_03_review_reject_redispatch_with_context(self):
        ctl("MOCK_rv-r1-1", "REJECT\nthe verification was never run\n")
        add_lane("R", [("r1", "-", 0, "mock", "")])
        orch("start", "R", check=True)
        wait(lambda: phase("r1") == "accepted", 90, what="r1 accepted after a reject")
        self.assertEqual(store().dispatch("r1")["attempt"], 2)
        wo = (INST / "prompts" / "r1.hard.md").read_text()
        self.assertIn("Previous attempts", wo)
        self.assertIn("REJECTED", wo)
        self.assertTrue(any(e["event"] == "review_verdict" and e["verdict"] == "REJECT" for e in events("r1")))

    def test_04_close_checks_refuse_then_halt(self):
        ctl("MOCK_NOPROGRESS_c1")
        add_lane("C", [("c1", "-", 0, "mock", ""), ("c2", "-", 0, "mock", "")])
        orch("start", "C", check=True)
        wait(lambda: phase("c1") == "blocked", 90, what="c1 halted after repeated refusals")
        fails = [e for e in events("c1") if e["event"] == "check_failed"]
        self.assertEqual(len(fails), 2)
        self.assertIn("progress", fails[0]["detail"])
        self.assertEqual(phase("c2"), "", "a serial lane must not overtake a halted row")
        # operator fixes it and resets: the row runs again and is accepted
        (INST / "control" / "MOCK_NOPROGRESS_c1").unlink()
        orch("reset", "c1", check=True)
        wait(lambda: phase("c1") == "accepted" and phase("c2") == "accepted", 90, what="c1,c2 after reset")

    def test_05_blocked_unblock_resolved(self):
        ctl("MOCK_u1", "BLOCKED\nstep 2 needs a mapping the plan does not give\n")
        add_lane("U", [("u1", "-", 0, "mock", "")])
        orch("start", "U", check=True)
        wait(lambda: store().dispatch("ub-u1-1").get("epoch"), 60, what="unblocker dispatched")
        (INST / "control" / "MOCK_u1").unlink()   # the "plan fix"
        wait(lambda: phase("u1") == "accepted", 90, what="u1 accepted after unblock")
        self.assertIn("ub-u1-1", (INST / "BLOCK_SHEET.md").read_text().replace("| u1 | 1 |", "ub-u1-1"))
        wo = (INST / "prompts" / "ub-u1-1.hard.md").read_text()
        self.assertIn("step 2 needs a mapping", wo, "the unblocker must see the stop reason (__REASON__)")

    def test_06_escalate_halts(self):
        ctl("MOCK_e1", "BLOCKED\nneeds an owner decision\n")
        ctl("MOCK_ub-e1-1", "ESCALATE\nonly the operator can decide this\n")
        add_lane("E", [("e1", "-", 0, "mock", "")])
        orch("start", "E", check=True)
        wait(lambda: phase("e1") == "blocked", 60, what="e1 halted on ESCALATE")

    def test_07_dead_window_halts_and_seal(self):
        ctl("MOCK_EXIT_d1")
        ctl("MOCK_SEAL_s1")
        add_lane("D", [("d1", "-", 0, "mock", ""), ], mode="dag")
        add_lane("S", [("s1", "-", 0, "mock", "")])
        orch("start", "D", "S", check=True)
        wait(lambda: phase("d1") == "halted", 60, what="d1 halted (dead window)")
        self.assertTrue(any(e["event"] == "dead" for e in events("d1")))
        wait(lambda: phase("s1") == "accepted", 90, what="s1 sealed and accepted")
        self.assertTrue(any(e["event"] == "sealed" for e in events("s1")))

    def test_08_cli_done_refusals_and_signed_ruling(self):
        add_lane("M", [("m1", "-", 0, "manual", ""), ("m2", "-", 0, "manual", "")])
        orch("start", "M", check=True)
        wait(lambda: store().dispatch("m1").get("open"), 30, what="manual m1 announced")
        p = orch("done", "m1", "did it")
        self.assertEqual(p.returncode, 3, p.stdout)
        self.assertIn("PROGRESS.md", p.stdout)
        d = WS / "PHASE_M" / "m1"
        (d / "PROGRESS.md").write_text("done by hand\n")
        (d / "run.out").write_text("STOP the heads moved\n")
        p = orch("done", "m1", "did it")
        self.assertEqual(p.returncode, 3, p.stdout)
        self.assertIn("stop line", p.stdout)
        (d / "PROGRESS.md").write_text("done by hand\nSIGNED_RULING DEC-2\n")
        self.assertEqual(orch("done", "m1", "did it").returncode, 3, "a PROPOSED entry is not a signature")
        (d / "PROGRESS.md").write_text("done by hand\nSIGNED_RULING DEC-1\n")
        p = orch("done", "m1", "<one-line summary>")
        self.assertEqual(p.returncode, 3, "a placeholder summary is refused")
        p = orch("done", "m1", "did it by hand")
        self.assertEqual(p.returncode, 0, p.stdout)
        wait(lambda: phase("m1") == "accepted", 30, what="manual row accepted (manual rows are not reviewed)")

    def test_09_resources_slots_and_capacity(self):
        for i in range(1, 4):
            ctl(f"MOCK_DELAY_x{i}", "4")
            ctl(f"MOCK_DELAY_y{i}", "4")
        add_lane("X", [(f"x{i}", "-", 0, "mock", "res=db review=0") for i in range(1, 4)], mode="dag")
        add_lane("Y", [(f"y{i}", "-", 0, "mock", "res=pool review=0") for i in range(1, 4)], mode="dag")
        orch("start", "X", "Y", check=True)
        import tmuxio
        max_x = max_y = 0
        end = time.time() + 90
        # a resource is held from dispatch until the session's claim: count open sessions
        while time.time() < end and not all(phase(f"{l}{i}") == "accepted" for l in "xy" for i in range(1, 4)):
            st = store()
            max_x = max(max_x, sum(1 for i in range(1, 4) if st.dispatch(f"x{i}").get("open")))
            max_y = max(max_y, sum(1 for i in range(1, 4) if st.dispatch(f"y{i}").get("open")))
            time.sleep(0.2)
        self.assertTrue(all(phase(f"{l}{i}") == "accepted" for l in "xy" for i in range(1, 4)))
        self.assertEqual(max_x, 1, "a 1-slot resource never runs two rows at once")
        self.assertEqual(max_y, 2, "a 2-slot resource runs two rows at once")
        self.assertFalse(list((INST / "leases").glob("db*.lock")), "leases are released at session end")

    def test_10_gate_bound_to_plan(self):
        add_lane("G", [("g1", "-", 1, "mock", "review=0")])
        orch("start", "G", check=True)
        time.sleep(3)
        self.assertEqual(phase("g1"), "")
        orch("approve", "g1", check=True)
        plan = WS / "PHASE_G" / "g1" / "PLAN.md"
        plan.write_text(plan.read_text() + "\nedited after approval\n")
        time.sleep(3)
        self.assertEqual(phase("g1"), "", "an edited plan re-gates")
        self.assertIn("plan changed", json.loads((INST / "state" / "sched.json").read_text()).get("g1", ""))
        orch("approve", "g1", check=True)
        wait(lambda: phase("g1") == "accepted", 60, what="g1 after re-approval")

    def test_11_archive_and_archived_deps(self):
        p = orch("archive", "--complete")
        self.assertIn("archived A", p.stdout, p.stdout)
        self.assertTrue((INST / "archive" / "queue_A.tsv").exists())
        add_lane("H", [("h1", "a2", 0, "mock", "review=0")], mode="dag")
        orch("start", "H", check=True)
        wait(lambda: phase("h1") == "accepted", 60, what="h1 (dep on an archived row)")
        self.assertEqual(orch("lint").returncode, 0)

    def test_12_guard_boundary_and_planning_commit(self):
        for rid in ("k1", "k10"):
            (WS / "PHASE_K" / rid).mkdir(parents=True, exist_ok=True)
        add_lane("K", [("k1", "-", 0, "manual", ""), ("k10", "-", 0, "manual", "")])
        (WS / "PHASE_K" / "k10" / "out.txt").write_text("k10's file\n")
        p = orch("commit", "k1", "-m", "x", "--", "PHASE_K/k10/out.txt")
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("outside k1's allowlist", p.stdout + p.stderr)
        (WS / "PHASE_K" / "k1" / "mine.txt").write_text("mine\n")
        p = orch("commit", "k1", "-m", "k1 commit", "--", "PHASE_K/k1/mine.txt")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        (WS / "NOTES.md").write_text("planning notes\n")
        p = orch("commit", "plan:t", "-m", "planning", "--", "NOTES.md")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        p = orch("commit", "plan:t", "-m", "planning", "--", "ORCH/state/a1.status")
        self.assertNotEqual(p.returncode, 0, "planning commits never stage live rig state")

    def test_13_stale_lease_reclaimed(self):
        lock = INST / "leases" / "db.lock"
        lock.mkdir(parents=True)
        (lock / "owner").write_text("ghostrow\n2026-01-01 00:00:00\n999999\n" + str(INST) + "\n")
        p = subprocess.run([sys.executable, str(HOME / "lib" / "leases.py"), "acquire", "me", "db"],
                           capture_output=True, text=True, env=ENV)
        self.assertEqual(p.returncode, 0, p.stdout)
        self.assertIn("reclaimed stale", p.stdout)
        subprocess.run([sys.executable, str(HOME / "lib" / "leases.py"), "release", "me", "db"], env=ENV)

    def test_14_grandfathered_v1_claims(self):
        st = store()
        (INST / "queue_V.tsv").write_text("v1\t-\t0\tmock\tPHASE_V/v1/PLAN.md\nv2\t-\t0\tmock\tPHASE_V/v2/PLAN.md\n")
        for rid, v in (("v1", "CLOSED"), ("v2", "BLOCKED")):
            f = INST / "state" / f"{rid}.status"
            f.write_text(f"{v}\nfrom v1\n")
            os.utime(f, (1, 1))
        self.assertEqual(st.phase("v1"), "accepted")
        self.assertEqual(st.phase("v2"), "blocked")
        (INST / "queue_V.tsv").unlink()

    def test_15_preflight(self):
        d = WS / "PHASE_P" / "p1"
        d.mkdir(parents=True, exist_ok=True)
        (d / "PLAN.md").write_text("# PLAN p1\n\nRulings: DEC-1, DEC-99\n\n## Verification\n```bash\ntrue\n```\n")
        (INST / "queue_P.tsv").write_text("p1\t-\t0\tmock\tPHASE_P/p1/PLAN.md\tres=nope\n")
        p = orch("preflight", "p1", "--run")
        self.assertEqual(p.returncode, 1)
        for want in ("no Acceptance section", "DEC-99", "res=nope", "PASSES BEFORE THE ROW RAN"):
            self.assertIn(want, p.stdout)
        (INST / "queue_P.tsv").unlink()

    def test_16_rounds_goal_and_stall(self):
        orch("round", "new", "rz", "--title", "metric round", "--lanes", "Z", check=True)
        import yaml
        rf = INST / "rounds" / "rz" / "round.yaml"
        r = yaml.safe_load(rf.read_text())
        r["goal"] = {"metric_cmd": "echo METRIC score=3", "name": "score", "direction": "up",
                     "instrument": "v1", "stall_after": 2, "target": 10}
        rf.write_text(yaml.safe_dump(r))
        add_lane("Z", [(f"z{i}", "-", 0, "mock", "review=0") for i in range(1, 5)])
        orch("round", "start", "rz", check=True)
        wait(lambda: (INST / "control" / "PAUSE_Z").exists(), 120, what="round paused on metric stall")
        from rounds import scoreboard
        pts = [x for x in scoreboard(type("C", (), {"instance": INST})()) if x["series"] == "rz"]
        self.assertGreaterEqual(len(pts), 3)
        self.assertEqual(pts[-1]["stall"], "2")
        rnd = (WS / "PHASE_Z" / "z1" / "RULES.md")
        self.assertTrue((INST / "rounds" / "rz" / "RULES.md").exists())
        wo = (INST / "prompts" / "z1.hard.md").read_text()
        self.assertIn("rounds/rz/RULES.md", wo, "the work order points at the round's rules")

    def test_17_launch_failure_blocks(self):
        add_lane("F", [("f1", "-", 0, "ghost", "")])
        orch("start", "F", check=True)
        wait(lambda: phase("f1") in ("blocked", "halted"), 60, what="launch failure becomes BLOCKED")
        self.assertIn("launch failed", store().result("f1").get("why", "") + store().status("f1")[1])

    def test_18_report_and_gd(self):
        p = orch("report")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("Per executor", p.stdout)
        p = orch("gd", "next", "--reserve", "a test ruling")
        self.assertIn("DEC-3", p.stdout, p.stdout + p.stderr)
        p = orch("gd", "show", "DEC-1")
        self.assertIn("body one", p.stdout)

    def test_19_hot_reload_and_capacity(self):
        cfg_set("scheduler.max_parallel", 1)
        for i in (1, 2):
            ctl(f"MOCK_DELAY_w{i}", "3")
        add_lane("W", [("w1", "-", 0, "mock", "review=0"), ("w2", "-", 0, "mock", "review=0")], mode="dag")
        orch("start", "W", check=True)
        import tmuxio
        both = False
        end = time.time() + 60
        while time.time() < end and not (phase("w1") == "accepted" and phase("w2") == "accepted"):
            w = tmuxio.windows(SESSION)
            if w.get("w1", {}).get("alive") and w.get("w2", {}).get("alive"):
                both = True
            time.sleep(0.3)
        self.assertFalse(both, "max_parallel=1 (hot-reloaded) never runs two sessions")
        self.assertEqual(phase("w2"), "accepted")
        cfg_set("scheduler.max_parallel", 12)

    def test_20_idle_nudge(self):
        cfg_set("idle.nudge_after", 3)
        cfg_set("idle.max_nudges", 1)
        ctl("MOCK_HANG_n1")
        add_lane("N", [("n1", "-", 0, "mock", "review=0")])
        orch("start", "N", check=True)
        wait(lambda: any(e["event"] == "idle_nudge" for e in events("n1")), 60, what="idle nudge")
        cfg_set("idle.nudge_after", 0)
        import tmuxio
        tmuxio.kill(SESSION, "n1")

    def test_21_round_review_and_plan_review(self):
        p = orch("round", "review", "rz")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        wait(lambda: store().status("rr-rz")[0] == "DONE", 60, what="round review verdict")
        self.assertTrue((INST / "rounds" / "rz" / "REPORT.md").exists())
        self.assertTrue((INST / "rounds" / "rz" / "review" / "BRIEF.md").exists())
        wo = next((INST / "prompts").glob("rr-rz.*.md")).read_text()
        self.assertIn("QUESTIONS.md", wo)
        # a round that requires a plan review waits for its PASS
        orch("round", "new", "rq", "--lanes", "Q", "--preflight-required", check=True)
        add_lane("Q", [("q1", "-", 0, "mock", "review=0")])
        p = orch("round", "start", "rq")
        self.assertNotEqual(p.returncode, 0, "round start refuses before the plan review passes")
        orch("preflight", "--round", "rq", "--review")
        wait(lambda: store().status("pf-rq")[0] == "PASS", 60, what="plan review PASS")
        orch("round", "start", "rq", check=True)
        wait(lambda: phase("q1") == "accepted", 60, what="q1 after the plan review")

    def test_22_forged_verdicts_and_session_identity(self):
        ctl("MOCK_DELAY_fg1", "5")
        add_lane("O", [("fg1", "-", 0, "mock", "")])
        orch("start", "O", check=True)
        wait(lambda: store().dispatch("fg1").get("open"), 30, what="fg1 dispatched")
        # the row (or anything) writes its own reviewer's ACCEPT before claiming
        (INST / "reviews").mkdir(exist_ok=True)
        (INST / "reviews" / "fg1-1.md").write_text("# looks great, trust me\n" * 3)
        store().write_status("rv-fg1-1", "ACCEPT", "self-approved")
        wait(lambda: phase("fg1") == "accepted", 90, what="fg1 accepted by a REAL review")
        self.assertTrue(any(e["event"] == "forged_verdict" for e in events("fg1")))
        self.assertTrue(store().dispatch("rv-fg1-1").get("ident"), "a real reviewer was dispatched")
        # a session may only claim itself; operator commands refuse inside sessions
        sess = dict(ENV, ORCH_ROW="zz9")
        p = orch("done", "a2", "not mine", env=sess)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("may only claim itself", p.stdout)
        p = orch("approve", "g1", env=sess)
        self.assertIn("operator's command", p.stdout)
        p = orch("verdict", "rv-a1-1", "ACCEPT", "x", env=sess)
        self.assertIn("only its own verdict", p.stdout)
        p = orch("commit", "plan:t", "-m", "x", "--", "NOTES.md", env=sess)
        self.assertNotEqual(p.returncode, 0)

    def test_23_probe_integrity(self):
        d = WS / "PHASE_I" / "i1"
        d.mkdir(parents=True, exist_ok=True)
        (d / "PLAN.md").write_text(PLAN.format(id="i1", rulings="-", verify="true"))
        (d / "ACCEPT.sh").write_text("exit 1\n")
        ctl("MOCK_DELAY_i1", "4")
        add_lane("I", [("i1", "-", 0, "mock", "review=0")])
        orch("start", "I", check=True)
        wait(lambda: store().dispatch("i1").get("open"), 30, what="i1 dispatched")
        (d / "ACCEPT.sh").write_text("exit 0\n")      # the row 'fixes' its own probe
        wait(lambda: any(e["event"] == "check_failed" for e in events("i1")), 60, what="probe integrity refusal")
        fail = next(e for e in events("i1") if e["event"] == "check_failed")
        self.assertIn("changed during the attempt", fail["detail"])

    def test_24_push_filter(self):
        from engine import push_wanted
        self.assertTrue(push_wanted({}, "normal"))
        self.assertTrue(push_wanted({}, "critical"))
        self.assertFalse(push_wanted({}, "low"))
        crit = {"push_min_urgency": "critical"}
        self.assertFalse(push_wanted(crit, "normal"))
        self.assertTrue(push_wanted(crit, "critical"))
        self.assertFalse(push_wanted({"push_min_urgency": "low"}, "low"))   # low never pushes

    def test_25_unblock_ladder(self):
        cfg_set("executors.unblock_executors", ["mock", "mock2"])
        cfg_set("executors.max_unblock_attempts", 2)
        cfg_set("executors.unblock_ladder", True)
        ctl("MOCK_l1", "BLOCKED\nneeds a call the first tier cannot make\n")
        ctl("MOCK_ub-l1-1", "ESCALATE\nfirst tier lacks the delegation\n")
        ctl("MOCK_DELAY_ub-l1-2", "4")
        ctl("MOCK_l2", "BLOCKED\nneeds an owner decision\n")
        ctl("MOCK_ub-l2-1", "ESCALATE\nfirst tier lacks the delegation\n")
        ctl("MOCK_ub-l2-2", "ESCALATE\nhard stop: owner only\n")
        add_lane("L", [("l1", "-", 0, "mock", "review=0"), ("l2", "-", 0, "mock", "review=0")], mode="dag")
        orch("start", "L", check=True)
        wait(lambda: store().dispatch("ub-l1-2").get("epoch"), 60, what="second-tier unblocker dispatched")
        self.assertEqual(store().dispatch("ub-l1-2").get("executor"), "mock2", "the ladder climbs to the next tier")
        self.assertEqual(phase("l1"), "unblocking", "a first-tier ESCALATE must not halt while a higher tier remains")
        wo = (INST / "prompts" / "ub-l1-2.hard.md").read_text()
        self.assertIn("first tier lacks the delegation", wo, "the next tier sees the first tier's escalation")
        (INST / "control" / "MOCK_l1").unlink()   # the second tier's fix
        wait(lambda: phase("l1") == "accepted", 90, what="l1 accepted after the second tier resolved")
        wait(lambda: phase("l2") == "blocked", 90, what="l2 halts when the last tier escalates")
        self.assertEqual(len([e for e in events("l2") if e["event"] == "unblock_verdict"]), 2)
        self.assertTrue(any(e["event"] == "unblock_ladder" for e in events("l2")))
        cfg_set("executors.unblock_ladder", False)
        cfg_set("executors.max_unblock_attempts", 1)
        cfg_set("executors.unblock_executors", [])

    def test_26_engine_rearm(self):
        cfg_set("scheduler.rearm_max", 1)
        cfg_set("framework.allow_dirty", True)   # the engine's round start runs `orch start`'s tree check
        cfg_set("scheduler.max_parallel", 40)    # earlier scenarios leave sealed/hanging windows alive
        ctl("MOCK_DELAY_ra2", "75")          # the course-loop row stays live while it asks
        ctl("MOCK_HANG_ra3")                  # a running target
        add_lane("RA", [("ra1", "-", 0, "mock", "review=0"),
                       ("ra3", "-", 0, "mock", "review=0"),
                       ("ra2", "ra1", 0, "mock", "review=0 rearm=ra1,ra3,round")], mode="dag")
        p = orch("lint")   # other scenarios leave their own deliberate lint errors; none may concern lane RA
        self.assertFalse([l for l in p.stdout.splitlines() if "ERROR" in l and ("rearm" in l or " ra" in l)], p.stdout)
        orch("start", "RA", check=True)
        wait(lambda: phase("ra1") == "accepted", 60, what="ra1 accepted")
        wait(lambda: store().dispatch("ra2").get("open") and store().dispatch("ra3").get("open"), 120,
             what="ra2, ra3 live")
        self.assertEqual(store().dispatch("ra2")["snapshot"]["meta"]["rearm"], "ra1,ra3,round")
        row = lambda sid: dict(ENV, ORCH_ROW=sid, ORCH_KIND="row")
        # operator commands stay refused inside a session, with the hint
        p = orch("reset", "ra1", env=row("ra2"))
        self.assertIn("operator's command", p.stdout)
        self.assertIn("orch rearm", p.stdout)
        # no grant: ra3 is live but its snapshot carries no rearm=
        p = orch("rearm", "ra1", "loop 1", env=row("ra3"))
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("no rearm=ra1 grant", p.stdout)
        # editing the live queue line after dispatch grants nothing
        q = INST / "queue_RA.tsv"
        q.write_text(q.read_text().replace("ra3\t-\t0\tmock\tPHASE_RA/ra3/PLAN.md\treview=0",
                                           "ra3\t-\t0\tmock\tPHASE_RA/ra3/PLAN.md\treview=0 rearm=ra1"))
        self.assertIn("rearm=ra1", q.read_text())
        p = orch("rearm", "ra1", "loop 1", env=row("ra3"))
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("no rearm=ra1 grant", p.stdout)
        # self, sub-sessions, running target, no reason
        p = orch("rearm", "ra2", "me", env=row("ra2"))
        self.assertIn("may not re-arm itself", p.stdout)
        p = orch("rearm", "ra1", "x", env=dict(ENV, ORCH_ROW="ub-ra2-1", ORCH_KIND="unblock"))
        self.assertIn("not a row session", p.stdout)
        p = orch("rearm", "ra3", "x", env=row("ra2"))
        self.assertIn("is running", p.stdout)
        p = orch("rearm", "ra1", env=row("ra2"))
        self.assertNotEqual(p.returncode, 0)
        # a forged request file (caller without a grant) is refused by the engine
        (INST / "state" / "ra3.rearm.ra1.request").write_text(json.dumps(
            {"caller": "ra3", "target": "ra1", "tag": "", "reason": "forged"}))
        wait(lambda: list((INST / "state").glob("ra3.rearm.ra1.*.refused")), 30, what="forged request refused")
        # the granted row asks; the engine resets ra1, which runs again
        p = orch("rearm", "ra1", "loop 1: judge re-run", env=row("ra2"))
        self.assertEqual(p.returncode, 0, p.stdout)
        wait(lambda: any(e["event"] == "rearm" for e in events("ra1")), 30, what="engine re-armed ra1")
        self.assertIn("REARM ra1 by ra2", (INST / "state" / "events_RA.log").read_text())
        self.assertTrue(list((INST / "state").glob("ra1.reset*.status")))
        wait(lambda: store().attempt("ra1") == 2 and phase("ra1") == "accepted", 60, what="ra1 re-ran")
        # cap (rearm_max 1): the second request is refused by the engine
        p = orch("rearm", "ra1", "loop 2", env=row("ra2"))
        self.assertEqual(p.returncode, 0, p.stdout)
        wait(lambda: list((INST / "state").glob("ra2.rearm.ra1.*.refused")), 30, what="cap refusal")
        self.assertEqual(store().attempt("ra1"), 2)
        # rounds: new/add-lanes direct with the grant, start via the engine; never `round start` directly
        p = orch("round", "new", "trearm", "--lanes", "RB", env=row("ra3"))
        self.assertIn("REFUSED", p.stdout)
        p = orch("round", "new", "trearm", "--lanes", "RB", env=row("ra2"))
        self.assertEqual(p.returncode, 0, p.stdout)
        add_lane("RB", [("rb1", "-", 0, "mock", "review=0")])
        p = orch("round", "start", "trearm", env=row("ra2"))
        self.assertIn("rearm round-start", p.stdout)
        self.assertFalse((INST / "control" / "RUN_RB").exists())
        p = orch("rearm", "round-start", "trearm", "loop 1 rows", env=row("ra2"))
        self.assertEqual(p.returncode, 0, p.stdout)
        wait(lambda: phase("rb1") == "accepted", 60, what="round trearm started by the engine, rb1 accepted")
        self.assertIn("REARM round start trearm by ra2", (INST / "state" / "events_RA.log").read_text())
        wait(lambda: phase("ra2") == "accepted", 90, what="ra2 finished")
        # ra3 hangs by design: stop it
        import tmuxio
        w = tmuxio.windows(SESSION).get("ra3")
        if w:
            tmuxio.kill(SESSION, w["wid"])
        cfg_set("scheduler.rearm_max", 5)
        cfg_set("scheduler.max_parallel", 12)

    def test_99_status_surfaces(self):
        p = orch("status")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("Lane", p.stdout)
        p = orch("json")
        data = json.loads(p.stdout)
        self.assertTrue(data["engine"])
        self.assertIn("A", data["archived_lanes"])
        orch("engine", "stop")
        wait(lambda: not subprocess.run([sys.executable, "-c",
             f"import sys; sys.path.insert(0, '{HOME / 'lib'}'); import orchctl, engine; "
             f"sys.exit(0 if orchctl.engine_running(engine.Rig()) else 1)"], env=ENV).returncode == 0, 30,
             what="engine stop")



class KillEndsTheTree(unittest.TestCase):
    """Closing a window ends its process tree — a HUP-deaf TUI and a helper that leads its own
    session (OpenCode's `serve --stdio`) — but never a job the session detached (`orch longrun`)."""

    def test_kill_window_ends_the_tree_but_not_a_detached_job(self):
        import tmuxio
        sess = f"orchkill{os.getpid()}"
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)
        self.addCleanup(subprocess.run, ["tmux", "kill-session", "-t", f"={sess}"], capture_output=True)
        (d / "s.sh").write_text(
            "trap '' HUP\n"
            "setsid bash -c 'echo $$ > helper.pid; trap \"\" HUP; sleep 300' < <(sleep 300) &\n"
            "setsid nohup bash -c 'echo $$ > job.pid; exec sleep 300' > /dev/null 2>&1 < /dev/null &\n"
            "echo $$ > tui.pid; while :; do sleep 1; done\n")
        tmuxio.ensure_session(sess, str(d))
        self.assertTrue(tmuxio.new_window(sess, "w1", str(d), f"bash {d / 's.sh'}"))
        pids = {}
        for _ in range(50):
            pids = {k: int((d / f"{k}.pid").read_text()) for k in ("tui", "helper", "job")
                    if (d / f"{k}.pid").is_file() and (d / f"{k}.pid").read_text().strip()}
            if len(pids) == 3:
                break
            time.sleep(0.1)
        self.assertEqual(len(pids), 3, pids)
        self.addCleanup(lambda: subprocess.run(["kill", str(pids["job"])], capture_output=True))
        self.assertTrue(tmuxio.kill(sess, "w1", grace=1.0))
        alive = lambda pid: tmuxio._stat(pid) is not None
        deadline = time.time() + 5
        while time.time() < deadline and (alive(pids["tui"]) or alive(pids["helper"])):
            time.sleep(0.2)
        self.assertFalse(alive(pids["tui"]), "the HUP-deaf session process outlived its window")
        self.assertFalse(alive(pids["helper"]), "the own-session helper outlived its window")
        self.assertTrue(alive(pids["job"]), "a detached longrun job was killed with the window")
        # a window that is already gone: nothing is signalled (tmux display-message would have answered
        # with the session's current window — here the surviving `w0`)
        self.assertTrue(tmuxio.new_window(sess, "w0", str(d), "sleep 300"))
        w0 = int(tmuxio._run(["list-panes", "-t", f"={sess}:=w0", "-F", "#{pane_pid}"]).stdout.split()[0])
        tmuxio._run(["select-window", "-t", f"={sess}:=w0"])    # the window display-message would fall back to
        self.assertFalse(tmuxio.kill(sess, "w1", grace=0.5))
        time.sleep(1)
        self.assertTrue(alive(w0), "killing a missing window signalled another window's tree")


class LaunchShKillsTheTree(unittest.TestCase):
    """lib/launch.sh: a window closed from OUTSIDE tmuxio.kill (a plain `tmux kill-window`) ends the adapter's whole
    process tree — a HUP-deaf TUI, a helper leading its own session, an orphaned helper — after the grace, and
    writes no .exit record; a normal exit still records "<rc> <epoch>". Runs a fake adapter from a copy of lib/."""

    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.d, True)
        shutil.copytree(HOME / "lib", self.d / "fw" / "lib", ignore=shutil.ignore_patterns("__pycache__"))
        self.inst = self.d / "ws" / "ORCH"
        (self.inst / "state").mkdir(parents=True)
        (self.inst / "orch.yaml").write_text("name: lt\nexecutors: {default: fakex, allowed: [fakex], "
                                             "adapters: {fakex: fake}}\n")
        (self.d / "order.md").write_text("order\n")
        ad = self.d / "fw" / "lib" / "adapters" / "fake.sh"
        ad.write_text(
            "#!/usr/bin/env bash\n"
            "trap '' HUP\n"
            "if [[ -e $ORCH_INSTANCE/exit_now ]]; then exit 7; fi\n"
            "setsid bash -c 'echo $$ > \"$ORCH_INSTANCE/helper.pid\"; trap \"\" HUP; sleep 300' < <(sleep 300) &\n"
            "( setsid bash -c 'echo $$ > \"$ORCH_INSTANCE/orphan.pid\"; trap \"\" HUP; sleep 300' < <(sleep 300) & )\n"
            "echo $$ > \"$ORCH_INSTANCE/tui.pid\"\n"
            "while :; do sleep 1; done\n")
        ad.chmod(0o755)
        self.sess = f"orchlaunch{os.getpid()}"
        self.addCleanup(subprocess.run, ["tmux", "kill-session", "-t", f"={self.sess}"], capture_output=True)

    def _start(self):
        import tmuxio
        tmuxio.ensure_session(self.sess, str(self.d))
        cmd = (f"env ORCH_INSTANCE={self.inst} ORCH_KIND=row ORCH_ROW=s1 ORCH_KILL_GRACE=1 "
               f"bash {self.d / 'fw' / 'lib' / 'launch.sh'} s1 fakex {self.d / 'order.md'}")
        self.assertTrue(tmuxio.new_window(self.sess, "w1", str(self.d), cmd))

    def test_plain_kill_window_leaves_no_process_and_no_record(self):
        import tmuxio
        self._start()
        names = ("tui", "helper", "orphan")
        pids = {}
        for _ in range(100):
            pids = {k: int((self.inst / f"{k}.pid").read_text()) for k in names
                    if (self.inst / f"{k}.pid").is_file() and (self.inst / f"{k}.pid").read_text().strip()}
            if len(pids) == 3:
                break
            time.sleep(0.1)
        self.assertEqual(len(pids), 3, pids)
        for p in pids.values():
            self.addCleanup(lambda p=p: subprocess.run(["kill", "-9", str(p)], capture_output=True))
        alive = lambda pid: tmuxio._stat(pid) is not None
        self.assertEqual(tmuxio._run(["kill-window", "-t", f"={self.sess}:=w1"]).returncode, 0)   # NOT tmuxio.kill
        deadline = time.time() + 12
        while time.time() < deadline and any(alive(p) for p in pids.values()):
            time.sleep(0.2)
        for k, p in pids.items():
            self.assertFalse(alive(p), f"{k} outlived its window")
        script = str(self.d / "fw" / "lib" / "launch.sh").encode()

        def launchers():
            out = []
            for dd in os.listdir("/proc"):
                try:
                    if dd.isdigit() and script in Path(f"/proc/{dd}/cmdline").read_bytes():
                        out.append(int(dd))
                except OSError:
                    pass
            return out
        deadline = time.time() + 5
        while time.time() < deadline and launchers():
            time.sleep(0.1)
        self.assertEqual(launchers(), [], "launch.sh itself is gone")
        self.assertFalse((self.inst / "state" / "s1.exit").exists(), "killed from outside leaves no record")

    def test_normal_exit_still_records(self):
        (self.inst / "exit_now").write_text("")
        self._start()
        f = self.inst / "state" / "s1.exit"
        for _ in range(100):
            if f.is_file():
                break
            time.sleep(0.1)
        self.assertRegex(f.read_text(), r"^7 \d+\n$")


if __name__ == "__main__":
    unittest.main(verbosity=2)
