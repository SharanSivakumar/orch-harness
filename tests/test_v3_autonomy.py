#!/usr/bin/env python3
"""v3 autonomy drills in an isolated sandbox (mock executor only — no model, no network, no spend).

Each scenario is one of the pre-registered drills of the autonomy plan (Docs/PlatformV2/
the autonomy drills): the rig must recover with ZERO operator
commands, and nothing may page except an allowed class.

    python3 -m unittest tests/test_v3_autonomy.py -v
    KEEP=1 python3 -m unittest tests/test_v3_autonomy.py      # keep the sandbox
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
from unittest import mock

HOME = Path(__file__).resolve().parent.parent
ORCH = HOME / "bin" / "orch"
sys.path.insert(0, str(HOME / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import tmux_isolation  # noqa: E402,F401  — a PRIVATE tmux server for every session this suite starts

SBX = Path(tempfile.mkdtemp(prefix="orch-v3-sbx-"))
SESSION = f"orchv3t{os.getpid()}"
WS = SBX / "project" / "cycle"
INST = WS / "ORCH"
STUBS = SBX / "stubs"
STUBS.mkdir(parents=True)
for b in ("claude", "opencode", "codex", "gemini", "systemctl", "loginctl"):
    (STUBS / b).write_text("#!/bin/sh\necho \"test stub: $0 refused\"; exit 97\n")
    (STUBS / b).chmod(0o755)
ENV = dict(os.environ, ORCH_INSTANCE=str(INST), ORCH_ALLOW_DIRTY="1", ORCH_NO_SYSTEMD="1",
           ORCH_SPEND_DIR=str(SBX / "spend"),
           ORCH_SYSTEMD_USER_DIR=str(SBX / "units"), PATH=f"{STUBS}:{os.environ.get('PATH', '')}")
for k in ("ORCH_ROW", "ORCH_KIND", "ORCH_PLAN_DIR"):
    ENV.pop(k, None)
os.environ["PATH"] = ENV["PATH"]
os.environ["ORCH_INSTANCE"] = str(INST)
os.environ["ORCH_SPEND_DIR"] = str(SBX / "spend")    # never the host's real ledger

PLAN = """# PLAN — {id}

Rulings: -

## Acceptance
- the thing is done

## Verification
```bash
test -f PROGRESS.md
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


def events(rid: str):
    return list(store().events(rid))


def all_events():
    return list(store().events())


def wait(cond, timeout: float = 90, every: float = 0.5, what: str = "") -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            if cond():
                return True
        except Exception:
            pass
        time.sleep(every)
    raise AssertionError(f"timed out waiting for {what or cond}\n--- engine.log tail ---\n" + engine_tail())


def engine_tail(n: int = 40) -> str:
    f = INST / "state" / "engine.log"
    return "".join(f.read_text(errors="replace").splitlines(True)[-n:]) if f.exists() else "(no engine log)"


def ctl(name: str, text: str = "") -> None:
    (INST / "control" / name).write_text(text)


def add_lane(lane: str, rows: list) -> None:
    lines = []
    for rid, deps, gate, x, extras in rows:
        d = WS / f"PHASE_{lane}" / rid
        d.mkdir(parents=True, exist_ok=True)
        if not (d / "PLAN.md").exists():
            (d / "PLAN.md").write_text(PLAN.format(id=rid))
        lines.append(f"{rid}\t{deps}\t{gate}\t{x}\tPHASE_{lane}/{rid}/PLAN.md\t{extras}".rstrip())
    (INST / f"queue_{lane}.tsv").write_text("\n".join(lines) + "\n")


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
    time.sleep(1.2)


def git(*args: str) -> str:
    return subprocess.run(["git", "-C", str(WS), *args], capture_output=True, text=True).stdout


def incidents():
    import steward
    from config_parser import load
    return steward.incidents(load(str(INST)))


def incident_for(subject: str, kind: str = ""):
    for i in incidents():
        if i.get("subject") == subject and (not kind or i.get("kind") == kind):
            return i
    return None


def incidents_of(kind: str):
    return [i for i in incidents() if i.get("kind") == kind]


T0 = [0.0]   # the running test's start (V3.setUp): a drill's "no page" covers its own window


def notifications(since: float = 0.0):
    out = []
    try:
        for line in (INST / "state" / "notify.jsonl").read_text().splitlines():
            rec = json.loads(line)
            if float(rec.get("epoch", 0)) >= since:
                out.append(rec)
    except FileNotFoundError:
        pass
    return out


def halt_downgraded(rid: str) -> bool:
    """The halt / ROUTE_A of `rid` was announced (asked critical) and the Steward lane downgraded it below the phone."""
    return any(n.get("asked") == "critical" and n.get("urgency") != "critical"
               and (f" {rid} HALTED" in n.get("title", "") or f" {rid} ROUTE_A" in n.get("title", ""))
               for n in notifications(T0[0]))


def no_halt_page_attempted(since: float | None = None) -> bool:
    """Nothing reached the phone's urgency during the test: every notification the engine made since the test
    started (state/notify.jsonl, written before any push) is below critical, and nothing was held for quiet hours."""
    crit = [n for n in notifications(T0[0] if since is None else since) if n.get("urgency") == "critical"]
    return not crit and not (INST / "state" / "held_pages.jsonl").exists()


class V3(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tmux_isolation.assert_isolated()
        WS.mkdir(parents=True)
        (WS / "DECISIONS.md").write_text("# decisions\n\n### DEC-1 — a ruling — RULED\nbody one\n")
        git("init", "-q")
        git("config", "user.email", "t@t")
        git("config", "user.name", "t")
        git("add", "-A")
        git("commit", "-qm", "init")
        orch("init", str(INST), "--name", "sandbox3", "--session", SESSION, check=True)
        import yaml
        f = INST / "orch.yaml"
        data = yaml.safe_load(f.read_text())
        data["executors"].update({"default": "mock", "allowed": ["mock", "mock2", "flaky", "manual"],
                                  "auto_unblock": ["mock", "flaky"], "unblock_executor": "mock",
                                  "retier_on_max_unblock": "mock2", "max_unblock_attempts": 1,
                                  "dead_window_policy": "retier",
                                  "flavors": {"mock2": "hard", "flaky": "hard"},
                                  "adapters": {"mock2": "mock", "flaky": "mock"}})
        data["scheduler"] = {"mode": "dag", "tick_seconds": 1, "max_parallel": 12, "min_free_mb": 0}
        data["review"] = {"policy": "none"}
        data["idle"] = {"nudge_after": 0}
        data["reaper"] = {"closed_grace_secs": 2}
        data["health"] = {"fast_death_secs": 30, "threshold": 2, "probe_every": 2, "open_secs": 600}
        data["steward"] = {"enabled": True, "tiers": {"mechanical": "mock", "judgment": "mock2"},
                           "gate_after": 3, "rest_after": 3600, "max_parallel": 2, "planner": "mock",
                           "panel": {"decider": "mock2", "falsifier": "mock"}}
        data["engine"] = {"self_update": False, "commit_bookkeeping": True, "bookkeeping_every": 3}
        data["watchdog"] = {"enabled": False, "push_min_urgency": "critical"}
        data["framework"] = {"allow_dirty": True}   # sessions run in tmux and do not inherit ORCH_ALLOW_DIRTY
        f.write_text(yaml.safe_dump(data, sort_keys=False))
        (INST / "queue_A.tsv").unlink(missing_ok=True)
        add_lane("A", [("a1", "-", 0, "mock", "")])
        p = orch("start")
        assert "lanes enabled" in p.stdout, p.stdout + p.stderr

    def setUp(self):
        T0[0] = time.time()

    @classmethod
    def tearDownClass(cls):
        subprocess.run(["tmux", "kill-session", "-t", f"={SESSION}"], capture_output=True)
        if os.environ.get("KEEP") == "1":
            print(f"\nsandbox kept: {SBX}")
        else:
            shutil.rmtree(SBX, ignore_errors=True)

    # ---------------------------------------------------------------------------------------------
    def test_00_engine_up_and_plain_row(self):
        wait(lambda: phase("a1") == "accepted", 60, what="a1 accepted")
        wait(lambda: any(" a1 accepted" in n.get("title", "") for n in notifications()), 30,
             what="the notification record works (the drills' page checks read it)")

    def test_01_interrupted_session_redispatched_not_charged(self):
        """Drill 1 (reboot): the tmux server dies under a running row; the engine comes back and
        re-dispatches it — no halt, no retier, no attempt charged."""
        ctl("MOCK_DELAY_r1", "20")
        add_lane("R", [("r1", "-", 0, "mock", "")])
        orch("start", "R", check=True)
        wait(lambda: store().dispatch("r1").get("open"), 30, what="r1 running")
        subprocess.run(["tmux", "kill-session", "-t", f"={SESSION}"], capture_output=True)   # engine dies too
        time.sleep(2)
        (INST / "control" / "MOCK_DELAY_r1").write_text("2")
        orch("start", check=True)                          # what the systemd unit / dead-man does
        wait(lambda: phase("r1") == "accepted", 90, what="r1 accepted after the interruption")
        evs = [e["event"] for e in events("r1")]
        self.assertIn("interrupted", evs)
        self.assertNotIn("halt", evs)
        self.assertNotIn("retier", evs)
        self.assertFalse((INST / "control" / "EXECUTOR_r1").exists())

    def test_02_boot_id_change_is_an_interruption(self):
        from engine import Engine
        from config_parser import load
        e = Engine(load(str(INST)))
        with mock.patch.dict(os.environ, {"ORCH_BOOT_ID": "boot-B"}):
            self.assertTrue(e.interrupted({"boot_id": "boot-A", "tmux_created": 0}))
            self.assertFalse(e.interrupted({"boot_id": "boot-B", "tmux_created": 0}))

    def test_03_breaker_opens_on_fast_deaths_and_closes_on_probe(self):
        """Drill 2 (broken CLI): two distinct sessions of one executor die at once — the breaker
        opens, nothing is retiered or halted, and the rows run once the probe passes."""
        cfg_set("health.probes", {"flaky": f"test -f {SBX}/flaky_healthy"})   # the probe the rig carries
        ctl("MOCK_EXIT_k1")
        ctl("MOCK_EXIT_k2")
        add_lane("K", [("k1", "-", 0, "flaky", ""), ("k2", "-", 0, "flaky", "")])
        orch("start", "K", check=True)
        import health
        from config_parser import load
        from engine import Rig
        h = lambda: health.Health(Rig(load(str(INST)))).state_of("flaky")
        wait(lambda: h() == "open", 60, what="flaky breaker open")
        (INST / "control" / "MOCK_EXIT_k1").unlink()
        (INST / "control" / "MOCK_EXIT_k2").unlink()
        time.sleep(4)
        self.assertEqual(h(), "open", "the breaker stays open while the probe fails")
        self.assertIn(phase("k1"), ("retry", ""), "a row is never halted for a CLI fault")
        (SBX / "flaky_healthy").write_text("ok")
        wait(lambda: phase("k1") == "accepted" and phase("k2") == "accepted", 90, what="k1,k2 after the probe passed")
        self.assertEqual(h(), "closed")
        for rid in ("k1", "k2"):
            evs = [e["event"] for e in events(rid)]
            self.assertNotIn("retier", evs, f"{rid} must not be retiered for an infra fault")
            self.assertNotIn("halt", evs)
            self.assertFalse((INST / "control" / f"EXECUTOR_{rid}").exists())
        hev = [e["event"] for e in events("health:flaky")]
        self.assertIn("breaker_open", hev)
        self.assertIn("breaker_close", hev)

    def test_04_dead_unblocker_steward_resets(self):
        """Drill 3: a row blocks, its unblocker dies twice, the row halts — the Steward resets it."""
        ctl("MOCK_s1", "BLOCKED\nneeds a mapping\n")
        ctl("MOCK_HANG_ub-s1-1")
        ctl("MOCKSW_s1", 'reset s1 "the unblocker died; the mapping is in the plan now"\n')
        add_lane("S", [("s1", "-", 0, "mock", "")])
        orch("start", "S", check=True)
        wait(lambda: store().dispatch("ub-s1-1").get("open"), 60, what="unblocker running")
        import tmuxio
        time.sleep(1)
        # the unblocker's pane dies by itself, after the fast window: a real death, not infra
        cfg_set("health.fast_death_secs", 1)
        time.sleep(2)
        subprocess.run(["tmux", "send-keys", "-t", f"={SESSION}:=ub-s1-1", "C-c", ""], capture_output=True)
        wait(lambda: phase("s1") in ("halted", "blocked"), 60, what="s1 halted after its unblocker died")
        (INST / "control" / "MOCK_s1").unlink()           # the 'fix' the Steward's reset picks up
        wait(lambda: phase("s1") == "accepted", 120, what="s1 accepted after the Steward's reset")
        inc = incident_for("s1", "halt")
        self.assertIsNotNone(inc)
        self.assertEqual(inc["outcome"], "resolved")
        log = (INST / "steward" / "STEWARD_LOG.md").read_text()
        self.assertIn("| reset | s1 |", log)
        self.assertTrue(any(e["event"] == "reset" and str(e.get("by", "")).startswith("sw-") for e in events("s1")))
        cfg_set("health.fast_death_secs", 30)

    def test_05_reserved_non_wall_question_ruled_provisionally(self):
        """Drill 4: a ROUTE_A the rights give the Steward — it rules (S), logs the revert, resets."""
        ctl("MOCK_q1", "ROUTE_A\nfence flags its own synthetic plant\n")
        ctl("MOCKSW_q1", 'rule S "synthetic plant exemption" "the hit is the row\'s own planted path" '
                         '"orch gd set-status SW-1 REVERTED" "precedent: synthetic plants are exempt"\n'
                         'reset q1 "re-run under SW-1"\n')
        add_lane("Q", [("q1", "-", 0, "mock", "")])
        orch("start", "Q", check=True)
        wait(lambda: incident_for("q1", "halt") is not None, 60, what="q1 incident")
        self.assertEqual(incident_for("q1", "halt")["klass"], "judgment", "ROUTE_A goes to the judgment tier")
        wait(lambda: store().dispatch(incident_for("q1", "halt")["sessions"][0]).get("epoch"), 60, what="steward dispatched")
        (INST / "control" / "MOCK_q1").unlink()
        wait(lambda: phase("q1") == "accepted", 120, what="q1 accepted")
        self.assertEqual(store().dispatch(incident_for("q1", "halt")["sessions"][0])["executor"], "mock2")
        rulings = (INST / "steward" / "RULINGS.md").read_text()
        self.assertIn("SW-1", rulings)
        self.assertIn("synthetic plant exemption", rulings)
        dg = orch("digest").stdout
        self.assertIn("SW-1 [S] synthetic plant exemption — revert: `orch gd set-status SW-1 REVERTED`", dg,
                      "drill 4: the ruling and its revert line show in the digest")
        self.assertTrue(halt_downgraded("q1"), "the halt was announced and kept off the phone")
        self.assertTrue(no_halt_page_attempted())

    def test_06_wall_parks_and_inboxes_never_pages(self):
        """Drill 5: a wall question — the Steward may not decide it: the row parks, one inbox item,
        the independent work keeps running, nothing pages."""
        ctl("MOCK_w1", "ROUTE_A\nthe row wants to delete a failing test\n")
        ctl("MOCKSWV_w1", "INBOX\ndeleting a test is the Owner's (wall)\n")
        ctl("MOCKSW_w1", 'inbox "w1 wants to delete a test" "A) keep the test (recommended) B) delete it" w1 '
                         '"a test deletion is a wall"\n')
        add_lane("W", [("w1", "-", 0, "mock", ""), ("w2", "w1", 0, "mock", ""), ("w3", "-", 0, "mock", "")])
        orch("start", "W", check=True)
        wait(lambda: phase("w1") == "parked", 120, what="w1 parked")
        wait(lambda: phase("w3") == "accepted", 60, what="independent w3 still runs")
        self.assertEqual(phase("w2"), "", "a dependent of a parked row waits")
        inbox = (INST / "steward" / "INBOX.md").read_text()
        self.assertIn("w1 wants to delete a test", inbox)
        self.assertEqual(incident_for("w1", "halt")["outcome"], "inbox")
        self.assertTrue(halt_downgraded("w1"), "the halt was announced and kept off the phone")
        self.assertTrue(no_halt_page_attempted())
        # the Owner answers later: unpark — the row runs again
        (INST / "control" / "MOCK_w1").unlink()
        orch("unpark", "w1", check=True)
        wait(lambda: phase("w2") == "accepted", 90, what="w1, w2 after the Owner's unpark")

    def test_07_gates_by_kind(self):
        """A steward-kind gate the Steward approves; an owner-kind gate it may not (it parks + inboxes)."""
        ctl("MOCKSW_gs1", 'approve gs1 "the gate condition (prerequisite rows accepted) holds"\n')
        ctl("MOCKSW_go1", 'approve go1 "trying"\n')
        ctl("MOCKSWV_go1", "INBOX\nan owner gate: the Owner releases it\n")
        add_lane("G", [("gs1", "-", 1, "mock", "gate_kind=steward"), ("go1", "-", 1, "mock", "")])
        orch("start", "G", check=True)
        wait(lambda: phase("gs1") == "accepted", 120, what="steward-kind gate approved by the Steward")
        wait(lambda: phase("go1") == "parked", 120, what="owner gate parked")
        self.assertFalse((INST / "control" / "GO_go1").exists(), "the Steward never opens an owner gate")
        refused = [e for e in all_events() if e.get("event") == "steward_refused" and e.get("action") == "approve"]
        self.assertTrue(refused, "the engine refused the owner-gate approval")
        orch("approve", "go1", check=True)                 # the Owner's act unparks it
        wait(lambda: phase("go1") == "accepted", 90, what="go1 after the Owner's approve")

    def test_08_page_classes(self):
        """Only an allowed class may page; a critical event without one is downgraded to the digest."""
        from engine import Rig
        from config_parser import load
        rig = Rig(load(str(INST)))
        rig.cfg.data["watchdog"]["push_min_urgency"] = "critical"
        with mock.patch("engine.subprocess.Popen") as pop, mock.patch("engine.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess([], 1)
            rig.notify("critical", "a halt", "body")
            self.assertFalse(any("telegram_bot.py" in " ".join(map(str, c.args[0])) for c in pop.call_args_list),
                             "a halt does not page with the Steward lane on")
            rig.notify("critical", "spend over cap", "body", page_class="spend")
            self.assertTrue(any("telegram_bot.py" in " ".join(map(str, c.args[0])) for c in pop.call_args_list),
                            "an allowed class pages")

    def test_09_rest_detector_steward_starts_lane(self):
        """The 'rig at rest' catch-all: nothing runs, work is left (a dependency lane never started)."""
        cfg_set("steward.rest_after", 4)
        add_lane("Y", [("y1", "-", 0, "mock", "")])        # not started
        add_lane("Z", [("z1", "y1", 0, "mock", "")])
        ctl("MOCKSW_rig", 'start Y "y1 is the only thing z1 waits for and its lane was never enabled"\n')
        (INST / "control" / "RUN_Y").unlink(missing_ok=True)
        orch("start", "Z", check=True)
        wait(lambda: phase("z1") == "accepted", 150, what="z1 after the Steward enabled lane Y")
        inc = incident_for("rig", "rest")
        self.assertIsNotNone(inc)
        cfg_set("steward.rest_after", 3600)
        (INST / "control" / "MOCKSW_rig").unlink()

    def test_10_planner_cuts_and_starts_a_round(self):
        """A Steward dispatches a planner; the planner creates and starts a round (rows written by it)."""
        add_lane("P", [("p1", "-", 0, "mock", "")])
        (INST / "control" / "RUN_P").unlink(missing_ok=True)
        # a steward session for a synthetic incident asks for a planner
        import steward
        from engine import Engine
        from config_parser import load
        e = Engine(load(str(INST)))
        inc = steward.open_incident(e, "progress", "round:tp", "the round needs one more row", klass="judgment")
        ctl(f"MOCK_SW_sw-{inc['n']}-1", 'plan tp "cut p1 in lane P and start round tp"\n')
        ctl("MOCK_SW_pl-tp-1", 'round-new tp P "one row for the course loop"\nround-start tp "rows cut"\n')
        wait(lambda: phase("p1") == "accepted", 150, what="p1 accepted in the planner's round")
        wait(lambda: json.loads((INST / "state" / "steward" / "planners.json").read_text())["pl-tp-1"]["status"] == "pass",
             60, what="planner PASS recorded")

    def test_11_sessions_cannot_reach_operator_power(self):
        row = dict(ENV, ORCH_ROW="a1", ORCH_KIND="row")
        p = orch("sw", "reset", "a1", "x", env=row)
        self.assertIn("REFUSED", p.stdout)
        p = orch("park", "a1", env=row)
        self.assertIn("REFUSED", p.stdout)
        p = orch("service", "install", env=row)
        self.assertIn("operator", p.stdout)
        sw = dict(ENV, ORCH_ROW="sw-999-1", ORCH_KIND="steward")
        p = subprocess.run([str(ORCH), "sw", "reset", "a1", "x"], capture_output=True, text=True, env=sw, cwd=str(WS),
                           timeout=180)
        self.assertIn("not a live session", p.stdout, "a forged Steward id is refused by the engine")

    def test_12_digest_and_bookkeeping_commit(self):
        p = orch("digest")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        for want in ("Accepted:", "Idle while work was left", "human touches", "Steward:", "Your inbox"):
            self.assertIn(want, p.stdout)
        self.assertIn("unpark w1", p.stdout, "an Owner act is counted as a human touch")
        wait(lambda: "engine bookkeeping" in git("log", "--oneline", "-20"), 30, what="bookkeeping commit")
        self.assertIn("ORCH/steward/STEWARD_LOG.md", git("show", "--stat", "--format=", "HEAD") + git("log", "--stat", "-5"))

    def test_13_service_units(self):
        p = orch("service", "install")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        unit = (SBX / "units" / f"orch-engine-{SESSION}.service").read_text()
        self.assertIn("KillMode=process", unit)
        self.assertIn("--wait-lock", unit)
        self.assertIn("Restart=on-failure", unit)
        self.assertTrue((SBX / "units" / f"orch-deadman-{SESSION}.timer").exists())
        (INST / "state" / "engine.stopped").write_text("x")
        p = subprocess.run([sys.executable, str(HOME / "lib" / "service.py"), "deadman"], capture_output=True,
                           text=True, env=ENV)
        self.assertEqual(p.returncode, 0)
        self.assertFalse((INST / "state" / "deadman.json").exists() and
                         json.loads((INST / "state" / "deadman.json").read_text()).get("restarts"),
                         "a deliberate stop is never undone by the dead-man")
        (INST / "state" / "engine.stopped").unlink()
        orch("service", "uninstall")

    def test_14_disk_admission(self):
        cfg_set("scheduler.min_free_disk_mb", 10 ** 9)
        add_lane("DK", [("dk1", "-", 0, "mock", "")])
        orch("start", "DK", check=True)
        time.sleep(4)
        self.assertEqual(phase("dk1"), "")
        notes = json.loads((INST / "state" / "sched.json").read_text())
        self.assertIn("free disk", notes.get("dk1", ""))
        cfg_set("scheduler.min_free_disk_mb", 0)
        wait(lambda: phase("dk1") == "accepted", 60, what="dk1 once the disk floor is lifted")

    def test_15_engine_restart_request(self):
        hb = INST / "state" / "engine.heartbeat"
        pid0 = hb.read_text().split()[1]
        ctl("ENGINE_RESTART", "test: re-exec")
        wait(lambda: "re-exec" in engine_tail(10), 30, what="engine re-exec logged")
        wait(lambda: hb.read_text().split()[1] == pid0 and "engine up" in engine_tail(10), 30, what="engine back")
        time.sleep(3)
        self.assertGreater(float(hb.read_text().split()[0]), time.time() - 10, "heartbeat continues after re-exec")

    def test_16_panel_confirm_executes_staged_actions(self):
        """Drill (drop B, class P): a judgment Steward hands a class-P question to the panel. The decider's
        acts are STAGED (nothing runs while it decides); the independent falsifier CONFIRMs; the engine then
        executes them in order, each logged with its revert, and the rule becomes SW-<k> class P (HIGH)."""
        ctl("MOCK_pc1", "ROUTE_A\nthe fix needs a new vocabulary term\n")
        ctl("MOCKSW_pc1", 'escalate-panel "a new vocabulary term is class P"\n')
        ctl("MOCKSWV_pc1", "ESCALATE\nclass P: the panel decides\n")
        ctl("MOCKPD_pc1", 'rule P "vocabulary term pc" "the row needs one new term; nothing it must prove changes" '
                          '"orch gd set-status SW-x REVERTED" "class P construct"\n'
                          'reset pc1 "re-run under the panel ruling"\n')
        ctl("MOCK_DELAY_pc1", "1")
        add_lane("PC", [("pc1", "-", 0, "mock", "")])
        orch("start", "PC", check=True)
        wait(lambda: incident_for("pc1", "halt") is not None, 60, what="pc1 incident")
        n = incident_for("pc1", "halt")["n"]
        wait(lambda: incident_for("pc1", "halt").get("klass") == "panel", 90, what="incident handed to the panel")
        # the falsifier's dispatch: the decider has staged both actions and nothing has run yet
        wait(lambda: store().dispatch(f"sw-{n}-f1").get("epoch"), 120, what="falsifier dispatched")
        staged = json.loads((INST / "state" / "steward" / "staged" / f"{n}.json").read_text())
        self.assertEqual([a["action"] for a in staged["actions"]], ["rule", "reset"])
        f_epoch = float(store().dispatch(f"sw-{n}-f1")["epoch"])
        (INST / "control" / "MOCK_pc1").unlink()
        wait(lambda: phase("pc1") == "accepted", 150, what="pc1 accepted after the panel's CONFIRM")
        inc = incident_for("pc1", "halt")
        self.assertEqual(inc["outcome"], "panel-confirmed")
        self.assertEqual(inc["sessions"], [f"sw-{n}-1", f"sw-{n}-d1", f"sw-{n}-f1"])
        self.assertEqual(store().dispatch(f"sw-{n}-d1")["executor"], "mock2")
        self.assertEqual(store().dispatch(f"sw-{n}-f1")["executor"], "mock", "the falsifier runs on another executor")
        resets = [e for e in events("pc1") if e.get("event") == "reset"]
        self.assertTrue(resets and all(float(e["epoch"]) >= f_epoch for e in resets),
                        "the staged reset ran only after the falsifier was dispatched (on its CONFIRM)")
        self.assertEqual(resets[0].get("by"), f"sw-{n}-d1")
        rul = [json.loads(l) for l in (INST / "state" / "steward" / "rulings.jsonl").read_text().splitlines()]
        p_rul = [r for r in rul if r.get("class") == "P" and r.get("incident") == n]
        self.assertEqual(len(p_rul), 1)
        self.assertEqual(p_rul[0]["panel"], {"decider": f"sw-{n}-d1", "falsifier": f"sw-{n}-f1"})
        log = (INST / "steward" / "STEWARD_LOG.md").read_text()
        self.assertIn(f"| {n} | sw-{n}-d1 | rule |", log)
        self.assertIn(f"| {n} | sw-{n}-d1 | reset | pc1 |", log)
        self.assertIn(f"CONFIRMed by sw-{n}-f1", log)
        done = json.loads((INST / "state" / "steward" / "staged" / f"{n}.json").read_text())
        self.assertEqual(done["status"], "executed")
        self.assertTrue(all(a["outcome"] == "done" and a["revert"] for a in done["actions"]), "each act carries its revert")
        p = orch("digest")
        self.assertIn("HIGH — panel (class P)", p.stdout)
        self.assertIn(p_rul[0]["id"], p.stdout.split("HIGH")[1])
        self.assertTrue(no_halt_page_attempted())

    def test_17_panel_object_parks_and_inboxes(self):
        """Drill (drop B): the falsifier OBJECTs — the safe option: the row parks, one inbox item, and
        not one staged action executes."""
        ctl("MOCK_po1", "ROUTE_A\nthe fix wants a schema change\n")
        ctl("MOCKSW_po1", 'escalate-panel "a frozen schema change is class P"\n')
        ctl("MOCKSWV_po1", "ESCALATE\nclass P: the panel decides\n")
        ctl("MOCKPD_po1", 'rule P "schema field po" "add the field" "orch gd set-status SW-x REVERTED" "class P"\n'
                          'reset po1 "re-run with the new field"\n')
        ctl("MOCKPFV_po1", "OBJECT\nthe proposal cites a schema file that does not exist\n")
        add_lane("PO", [("po1", "-", 0, "mock", ""), ("po2", "-", 0, "mock", "")])
        orch("start", "PO", check=True)
        wait(lambda: phase("po1") == "parked", 180, what="po1 parked after the panel's OBJECT")
        wait(lambda: phase("po2") == "accepted", 60, what="independent po2 still runs")
        inc = incident_for("po1", "halt")
        n = inc["n"]
        self.assertEqual(inc["outcome"], "panel-objected")
        self.assertIn(f"sw-{n}-f1", inc["sessions"])
        self.assertFalse([e for e in events("po1") if e.get("event") == "reset"], "nothing staged was executed")
        rul = [json.loads(l) for l in (INST / "state" / "steward" / "rulings.jsonl").read_text().splitlines()]
        self.assertFalse([r for r in rul if r.get("incident") == n], "no ruling recorded on OBJECT")
        staged = json.loads((INST / "state" / "steward" / "staged" / f"{n}.json").read_text())
        self.assertEqual(staged["status"], "objected")
        self.assertFalse(any(a.get("outcome") for a in staged["actions"]))
        self.assertNotIn(f"| sw-{n}-d1 |", (INST / "steward" / "STEWARD_LOG.md").read_text())
        inbox = (INST / "steward" / "INBOX.md").read_text()
        self.assertIn("panel OBJECTED on po1", inbox)
        self.assertIn("schema file that does not exist", inbox)
        self.assertTrue(no_halt_page_attempted())

    def test_18_class_p_needs_the_panel(self):
        """`rule P` from a single Steward session is refused (the panel's only); no panel = P is a wall."""
        import panel
        import steward
        from config_parser import load
        from engine import Rig
        rig = Rig(load(str(INST)))
        self.assertTrue(steward.panel_ready(rig.cfg))
        ok, detail, _ = steward.execute(rig, "sw-1-1", "rule", ["P", "t", "x", "r"], "why", {"incident": 1})
        self.assertFalse(ok)
        self.assertIn("escalate-panel", detail)
        rig.cfg.data["steward"]["panel"] = {"decider": "mock", "falsifier": "mock"}
        self.assertFalse(panel.ready(rig.cfg)[0], "a falsifier on the decider's executor is no panel")
        self.assertFalse(steward.panel_ready(rig.cfg))
        ok, detail, _ = steward.execute(rig, "sw-1-1", "escalate-panel", [], "why", {"incident": 1})
        self.assertFalse(ok)
        self.assertIn("wall", detail)
        self.assertEqual(steward.verdicts_for("sw-3-f1"), ("CONFIRM", "OBJECT"))
        self.assertNotIn("CONFIRM", steward.verdicts_for("sw-3-d1"))
        # the falsifier never acts; a decider's action is checked when staged, not only when it runs
        rig.st.write_dispatch("sw-999-f1", {"open": True, "ident": "@0 0", "incident": 999, "panel": "falsifier"})
        ok, detail, _ = steward.handle(rig, {"session": "sw-999-f1", "action": "reset", "args": ["a1"], "why": "x"})
        self.assertFalse(ok)
        self.assertIn("falsifier", detail)
        rig.st.write_dispatch("sw-999-f1", {"open": False})
        self.assertIn("unknown row", steward.precheck(rig.cfg, "reset", ["nope"]))
        self.assertIn("class S or P", steward.precheck(rig.cfg, "rule", ["W", "t", "x", "r"]))
        self.assertIn("no gate", steward.precheck(rig.cfg, "approve", ["a1"]))
        self.assertEqual(steward.precheck(rig.cfg, "reset", ["a1"]), "")

    def test_19_nogo_outcome_holds_needs_go_dependents(self):
        """Drill (drop B item 2, the evidence: NO-GO and HELD rows closed as plain CLOSED and their
        dependents ran). A judge accepted with outcome nogo (status line 3) or held (sealed CLOSED:held)
        holds every dependent that carries needs=go and opens a negative_outcome incident; a plain dependent
        still runs; an outcome-less CLOSED and an `orch done --outcome go` both let a needs=go dependent run."""
        ctl("MOCK_ng1", "CLOSED\nthe measured bar is missed: NO-GO\nnogo\n")
        ctl("MOCK_ng3", "CLOSED\nheld for evidence\nHELD\n")
        ctl("MOCK_SEAL_ng3")
        ctl("MOCK_ng4", "CLOSED\nthe bar is met\ngo\n")
        ctl("MOCK_VIA_CLI_ng4")
        ctl("MOCK_ng5", "CLOSED\nNO-GO: the bar is missed\nNO-GO\n")   # `orch done` without --outcome,
        ctl("MOCK_VIA_CLI_ng5")                                              # then ORCH_CLOSE ng5 CLOSED:NO-GO
        ctl("MOCK_LINE_OUTCOME_ng5")
        add_lane("NG", [("ng1", "-", 0, "mock", ""), ("nd1", "ng1", 0, "mock", "needs=go"),
                        ("nd2", "ng1", 0, "mock", ""),
                        ("ng2", "-", 0, "mock", ""), ("nd3", "ng2", 0, "mock", "needs=go"),
                        ("ng3", "-", 0, "mock", ""), ("nd4", "ng3", 0, "mock", "needs=go"),
                        ("ng4", "-", 0, "mock", ""), ("nd5", "ng4", 0, "mock", "needs=go"),
                        ("ng5", "-", 0, "mock", ""), ("nd6", "ng5", 0, "mock", "needs=go")])
        orch("start", "NG", check=True)
        wait(lambda: all(phase(x) == "accepted" for x in ("ng1", "ng2", "ng3", "ng4", "ng5", "nd2", "nd3", "nd5")), 180,
             what="judges, the plain dependent and the GO dependents accepted")
        st = store()
        self.assertEqual(st.result("ng1").get("outcome"), "nogo", "status line 3 is copied into the result")
        self.assertEqual(st.result("ng3").get("outcome"), "held", "the seal CLOSED:HELD carries the outcome")
        self.assertEqual(st.status("ng3")[:2], ("CLOSED", "held for evidence"))
        self.assertEqual(st.result("ng4").get("outcome"), "go", "orch done --outcome go")
        self.assertEqual(st.result("ng2").get("outcome"), "", "a plain CLOSED has no outcome")
        self.assertIn("OUTCOME nogo", st.status_path("ng1").read_text())
        self.assertEqual(st.result("ng5").get("outcome"), "nogo",
                         "a claim without --outcome whose own closing line says CLOSED:NO-GO records the NO-GO")
        self.assertTrue([e for e in events("ng5") if e.get("event") == "outcome_from_closing_line"])
        for j, held in (("ng1", "nd1"), ("ng3", "nd4"), ("ng5", "nd6")):
            wait(lambda: incident_for(j, "negative_outcome") is not None, 60, what=f"negative_outcome incident on {j}")
            inc = incident_for(j, "negative_outcome")
            self.assertEqual(inc["klass"], "judgment")
            self.assertIn(held, inc["summary"])
            self.assertTrue([e for e in events(j) if e.get("event") == "negative_outcome"])
        # the Steward handles the incidents (the mock resolves them); the needs=go rows stay held regardless
        wait(lambda: all(incident_for(j, "negative_outcome").get("status") == "closed" for j in ("ng1", "ng3", "ng5")), 150,
             what="negative_outcome incidents handled")
        bundle = (INST / "steward" / "incidents" / f"{incident_for('ng1', 'negative_outcome')['n']}.bundle.md").read_text()
        self.assertIn("Never reset or re-run it to obtain a GO", bundle)
        self.assertIn("held by it (queue extra needs=go): nd1", bundle)
        time.sleep(4)
        for rid in ("nd1", "nd4", "nd6"):
            self.assertEqual(phase(rid), "", f"{rid} (needs=go) never dispatched behind a negative outcome")
            self.assertFalse(st.dispatch(rid), f"{rid} has no dispatch record")
        sched = json.loads((INST / "state" / "sched.json").read_text())
        self.assertIn("ng1:nogo", sched.get("nd1", ""))
        self.assertIn("needs=go", sched.get("nd1", ""))
        self.assertIn("ng3:held", sched.get("nd4", ""))
        p = orch("dispatch", "nd1")
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("ng1:nogo", p.stdout + p.stderr)
        self.assertEqual(len(incidents_of("negative_outcome")), 3, "one incident per negative outcome, no re-open")
        self.assertTrue(no_halt_page_attempted())

    def test_20_outcome_cli_and_queue_checks(self):
        """`orch done --outcome` refuses an unknown outcome before anything is written; needs=<other> is a
        queue-line problem; an unknown hand-written outcome counts as negative (fail-safe)."""
        import rows
        from store import Store, negative, norm_outcome
        p = orch("done", "a1", "x", "--outcome", "maybe")
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("go|nogo|held|fail", p.stdout + p.stderr)
        self.assertEqual(norm_outcome("NO-GO"), "nogo")
        self.assertTrue(negative("maybe") and negative("fail") and not negative("go") and not negative(""))
        r = rows.parse_line("x1 a 0 mock P/x1/PLAN.md needs=all", "Z", 0)
        self.assertTrue(any("needs=all" in pr for pr in r.problems))
        self.assertTrue(rows.parse_line("x1 a 0 mock P/x1/PLAN.md needs=go", "Z", 0).needs_go)
        s = Store(Path(tempfile.mkdtemp(dir=SBX)))
        s.status_path("h").write_text("CLOSED\nby hand\nOUTCOME Maybe\n")
        s.write_result("h", {"phase": "accepted", "status_mtime": s.status("h")[2], "outcome": s.claim_outcome("h")})
        self.assertEqual(s.outcome("h"), "maybe")
        self.assertFalse(s.go_ok("h"))
        dep = rows.parse_line("y1 h 0 mock P/y1/PLAN.md needs=go", "Z", 0)
        self.assertEqual(rows.unmet_deps(s, dep), ["h:maybe"])
        plain = rows.parse_line("y2 h 0 mock P/y2/PLAN.md", "Z", 0)
        self.assertEqual(rows.unmet_deps(s, plain), [], "without needs=go an accepted dep is met, whatever its outcome")
        for text, oc in (("CLOSED\nx\nOUTCOME: nogo\n", "nogo"), ("CLOSED\nOUTCOME=held\n", "held"),
                         ("CLOSED\nx\n", ""), ("CLOSED\nx\nOUTCOME go\n", "go")):
            s.status_path("k").write_text(text)
            self.assertEqual(s.claim_outcome("k"), oc, text)

    def test_21_closing_line_outcome_parsers_agree(self):
        """The engine's seal (close_pattern) and the adapter's lib/closing.sh read the same outcome — and the
        closing lines of every older form still seal as before."""
        from engine import close_pattern
        from store import norm_outcome
        cases = [  # line, verdict, outcome, summary
            ("ORCH_CLOSE a1 CLOSED — all good", "CLOSED", "", "all good"),
            ("  ORCH_CLOSE a1 CLOSED:NO-GO — the bar is missed", "CLOSED", "nogo", "the bar is missed"),
            ("ORCH_CLOSE a1 CLOSED: held - waiting", "CLOSED", "held", "waiting"),
            ("ORCH_CLOSE a1 CLOSED:maybe7 — s", "CLOSED", "maybe7", "s"),
            ("ORCH_CLOSE a1 CLOSED: all checks pass", "CLOSED", "", ": all checks pass"),
            ("ORCH_CLOSE a1 CLOSED—x", "CLOSED", "", "x"),
            ("ORCH_CLOSE a1 BLOCKED — needs a key", "BLOCKED", "", "needs a key"),
            ("ORCH_CLOSE a1 CLOSEDX nope", "", "", ""),
            ("ORCH_CLOSE a1 CLOSED — <the same one-line summary>", "", "", ""),
        ]
        sh = HOME / "lib" / "closing.sh"
        for line, verd, oc, summ in cases:
            out = subprocess.run(["bash", "-c", '. "$1"; cl=$(closing_line a1 "$2"); v=$(closing_verdict "$cl"); '
                                  'printf "%s|%s|%s" "$v" "$(closing_outcome "$cl" "$v")" "$(closing_summary "$cl" "$v")"',
                                  "x", str(sh), line], capture_output=True, text=True).stdout
            self.assertEqual(out, f"{verd}|{oc}|{summ}", f"closing.sh on {line!r}")
            m = close_pattern("a1", "row").match(line)
            if not verd or "<" in line:
                self.assertTrue(m is None or "<" in line, line)
                continue
            self.assertEqual((m.group(1), norm_outcome(m.group(2) or "") if m.group(1) == "CLOSED" else "",
                              m.group(3).strip()), (verd, oc, summ), f"engine on {line!r}")
        m = close_pattern("rv-a1-1", "review").match("ORCH_CLOSE rv-a1-1 ACCEPT — holds")
        self.assertEqual((m.group(1), m.group(3)), ("ACCEPT", "holds"))

    def test_22_protected_paths_refused_by_guard_and_close_check(self):
        """Drop B item 3: a protected-path edit is refused by the guard (every session id and the engine's
        plan:engine; only the operator's own plan: commit passes) and by the close check (commits since
        dispatch and uncommitted changes); an SW- ruling never covers it; an uncommitted, unscoped or
        PROPOSED ruling signs nothing; a table-row Owner ruling the operator committed signs."""
        cfg_set("guard.protected_paths", ["scorers/", "PHASE_PR/pr1/SCORER.md"])
        listed = subprocess.run([sys.executable, str(HOME / "lib" / "protected.py"), "list"], capture_output=True,
                                text=True, env=ENV).stdout.split()
        for want in ("scorers/", "PHASE_PR/pr1/SCORER.md", "ORCH/SPEND_CAPS.yaml", "ORCH/phases.yaml",
                     "ORCH/steward/RIGHTS.md"):
            self.assertIn(want, listed, "configured globs + the automatic set (caps, phases, rights table)")
        (WS / "scorers").mkdir(exist_ok=True)
        (WS / "scorers" / "score.py").write_text("BAR = 1\n")
        p = orch("commit", "plan:setup", "-m", "operator: the scorer", "--", "scorers/score.py")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        op_sha = git("rev-parse", "HEAD").strip()
        ledger = (INST / "state" / "protected_commits.jsonl").read_text()
        self.assertIn(op_sha, ledger, "the operator's protected commit is recorded (never charged to a row)")
        (WS / "scorers" / "pending.txt").write_text("the Owner's own pending edit\n")   # dirty BEFORE dispatch
        (INST / "steward").mkdir(exist_ok=True)
        shutil.copy(HOME / "templates" / "steward" / "RIGHTS.md", INST / "steward" / "RIGHTS.md")   # untracked

        ctl("MOCK_HANG_pr1")
        add_lane("PR", [("pr1", "-", 0, "mock", "")])
        orch("start", "PR", check=True)
        wait(lambda: store().dispatch("pr1").get("open"), 60, what="pr1 dispatched")
        base = (store().dispatch("pr1").get("snapshot") or {}).get("protected") or {}
        self.assertIn("scorers/pending.txt", base.get("dirty", {}), "dirty protected paths are in the row's base")
        self.assertTrue((INST / "state" / "pr1.protected_base.json").is_file())
        row = dict(ENV, ORCH_ROW="pr1", ORCH_KIND="row")
        d = WS / "PHASE_PR" / "pr1"

        # the guard: a row, a planner session, and the engine's plan:engine are all refused
        (d / "SCORER.md").write_text("weights: 1\n")
        head0 = git("rev-parse", "HEAD")
        p = orch("commit", "pr1", "-m", "x", "--", "PHASE_PR/pr1/SCORER.md", env=row)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("protected path", p.stdout + p.stderr)
        pl = dict(ENV, ORCH_ROW="pl-pr-1", ORCH_KIND="planner")
        (WS / "scorers" / "score.py").write_text("BAR = 0\n")
        p = orch("commit", "plan:pr", "-m", "x", "--", "scorers/score.py", env=pl)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("protected path", p.stdout + p.stderr)
        p = orch("commit", "plan:engine", "-m", "x", "--", "scorers/score.py")
        self.assertNotEqual(p.returncode, 0, "the engine's bookkeeping id never stages the Owner's files")
        self.assertEqual(git("rev-parse", "HEAD"), head0, "nothing was committed")
        self.assertEqual(git("diff", "--cached", "--name-only").strip(), "", "the index was reset")
        (WS / "PHASE_PR" / "NOTES.md").write_text("planner notes\n")
        p = orch("commit", "plan:pr", "-m", "planner notes", "--", "PHASE_PR/NOTES.md", env=pl)
        self.assertEqual(p.returncode, 0, "a planner session commits its own round as plan:<tag>: " + p.stdout + p.stderr)
        (WS / "scorers" / "score.py").write_text("BAR = 1\n")
        # a rename out of a protected tree shows its source to the guard (--no-renames)
        git("mv", "scorers/score.py", "PHASE_PR/pr1/score.py")
        p = orch("commit", "pr1", "-m", "x", "--", "PHASE_PR/pr1/score.py", env=row)
        self.assertNotEqual(p.returncode, 0, "a git mv out of scorers/ is refused: " + p.stdout)
        git("reset", "-q")
        (WS / "PHASE_PR" / "pr1" / "score.py").rename(WS / "scorers" / "score.py")
        # an operator commit never sweeps in a protected file someone else left staged
        head1 = git("rev-parse", "HEAD")
        (WS / "scorers" / "score.py").write_text("BAR = -1\n")
        git("add", "scorers/score.py")
        (WS / "OPNOTES.md").write_text("operator notes\n")
        p = orch("commit", "plan:notes", "-m", "notes", "--", "OPNOTES.md")
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("did not name", p.stdout + p.stderr)
        self.assertEqual(git("rev-parse", "HEAD"), head1)
        git("reset", "-q")
        (WS / "scorers" / "score.py").write_text("BAR = 1\n")

        # the close check: an uncommitted change made during the attempt
        (d / "PROGRESS.md").write_text("# PROGRESS pr1\nchanged the scorer\n")
        p = orch("done", "pr1", "changed the scorer", env=row)
        self.assertEqual(p.returncode, 3, p.stdout + p.stderr)
        self.assertIn("protected", p.stdout)
        self.assertIn("SCORER.md (uncommitted)", p.stdout)
        self.assertNotIn("pending.txt", p.stdout, "a pending edit that predates the row is not charged to it")
        (d / "SCORER.md").unlink()

        # … and a commit that bypassed the guard
        (WS / "scorers" / "score.py").write_text("BAR = 0  # lowered\n")
        git("add", "scorers/score.py")
        git("commit", "-qm", "row: lower the bar")
        p = orch("done", "pr1", "lowered the bar", env=row)
        self.assertEqual(p.returncode, 3, p.stdout)
        self.assertIn("scorers/score.py (commit", p.stdout)

        # an SW- ruling never covers a protected path, even a valid class-S one on this row
        (INST / "state" / "steward").mkdir(parents=True, exist_ok=True)
        with open(INST / "state" / "steward" / "rulings.jsonl", "a") as fh:
            fh.write(json.dumps({"id": "SW-900", "class": "S", "title": "t", "text": "pr1 may lower the bar"}) + "\n")
        with open(d / "PROGRESS.md", "a") as fh:
            fh.write("SIGNED_RULING SW-900\n")
        p = orch("done", "pr1", "lowered the bar", env=row)
        self.assertEqual(p.returncode, 3, p.stdout)
        self.assertIn("Steward ruling", p.stdout)
        # a RULED entry the row wrote itself (uncommitted, or committed by the row) signs nothing
        with open(WS / "DECISIONS.md", "a") as fh:
            fh.write("\n| DEC-50 | RULED (the Owner, 2026-10-19) — pr1 may lower the bar in scorers/score.py |\n")
        with open(d / "PROGRESS.md", "a") as fh:
            fh.write("SIGNED_RULING DEC-50\n")
        p = orch("done", "pr1", "lowered the bar", env=row)
        self.assertEqual(p.returncode, 3, p.stdout)
        self.assertIn("operator plan: commit of DECISIONS.md", p.stdout)
        git("add", "DECISIONS.md")
        git("commit", "-qm", "row: a ruling of my own")
        self.assertEqual(orch("done", "pr1", "lowered the bar", env=row).returncode, 3,
                         "a ruling the row committed (no operator record) signs nothing")
        # the operator commits rulings: a PROPOSED one and an unscoped RULED one sign nothing
        with open(WS / "DECISIONS.md", "a") as fh:
            fh.write("\n| DEC-51 | PROPOSED — pr1 may lower the bar |\n\n| DEC-52 | RULED (the Owner) — "
                     "another row's matter |\n")
        self.assertEqual(orch("commit", "plan:rulings", "-m", "rulings", "--", "DECISIONS.md").returncode, 0)
        (d / "PROGRESS.md").write_text("# PROGRESS pr1\nSIGNED_RULING DEC-50\nSIGNED_RULING DEC-51\n"
                                       "SIGNED_RULING DEC-52\n")
        p = orch("done", "pr1", "lowered the bar", env=row)
        self.assertEqual(p.returncode, 3, p.stdout)
        self.assertIn("names neither pr1", p.stdout)
        self.assertIn("DEC-50: not added by an operator commit", p.stdout,
                      "a later operator commit of the ledger does not adopt a row-written entry")
        # the operator commits a RULED table row naming the row: it signs
        with open(WS / "DECISIONS.md", "a") as fh:
            fh.write("\n| DEC-53 | RULED (the Owner, 2026-10-19) — pr1 may lower the bar |\n")
        self.assertEqual(orch("commit", "plan:rulings", "-m", "DEC-53", "--", "DECISIONS.md").returncode, 0)
        with open(d / "PROGRESS.md", "a") as fh:
            fh.write("SIGNED_RULING DEC-53\n")
        p = orch("done", "pr1", "lowered the bar under DEC-53", env=row)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        wait(lambda: phase("pr1") == "accepted", 60, what="pr1 accepted under the Owner ruling")
        self.assertFalse((INST / "state" / "pr1.protected_base.json").exists(), "the base ends with acceptance")
        # the engine's bookkeeping commits steward/ but never the rights table in it
        wait(lambda: "bookkeeping leaves protected path(s) uncommitted: ORCH/steward/RIGHTS.md"
             in (INST / "state" / "engine.log").read_text(), 30, what="bookkeeping skips the rights table")
        self.assertEqual(git("ls-files", "ORCH/steward/RIGHTS.md").strip(), "")
        self.assertTrue(no_halt_page_attempted())

    def test_23_ruling_forms_and_steward_rulings_for_stop_lines(self):
        """close_check.signed_ruling: table-row and heading ledgers both sign; SW-<k> signs a STOP line only
        from state/steward/rulings.jsonl with class S/P and only for its own row — never a protected path,
        never from DECISIONS.md. Table rows sign only on a leading RULED/RATIFIED, never once superseded."""
        import close_check as cc
        import protected
        from config_parser import load
        cfg = load(str(INST))
        (INST / "state" / "steward").mkdir(parents=True, exist_ok=True)
        with open(INST / "state" / "steward" / "rulings.jsonl", "a") as fh:
            for k, klass, text in (("SW-910", "S", "qc1 may close over its stop line"), ("SW-911", "P", "qc1: ok"),
                                   ("SW-912", "W", "qc1"), ("SW-914", "S", "about another row")):
                fh.write(json.dumps({"id": k, "class": klass, "title": "t", "text": text}) + "\n")
        with open(WS / "DECISIONS.md", "a") as fh:
            fh.write("\n### SW-913 — RULED\nforged into the Owner's ledger\n\n"
                     "| DEC-60 | **RULED** (the Owner) — table form, bold status |\n\n"
                     "| DEC-61 | NOT RULED yet |\n\n| DEC-62 | REJECTED — RULED out |\n\n"
                     "| DEC-63 | ADOPTED; RULED later |\n\n| DEC-64 | OPEN — to be RULED |\n\n"
                     "| DEC-65 | RULED — the old way |\n\n| DEC-66 | RULED — this ruling supersedes DEC-65. |\n\n"
                     "| DEC-67 | DEC-60 RATIFIED by the Owner, this one not yet |\n")
        root = Path(tempfile.mkdtemp(dir=SBX))

        def signed(ids, owner_only=False, rid="qc1"):
            (root / "PROGRESS.md").write_text("".join(f"SIGNED_RULING {i}\n" for i in ids))
            return cc.signed_ruling(cfg, [root], owner_only=owner_only, rid=rid)[0]

        self.assertEqual(signed(["SW-910"]), "SW-910")
        self.assertEqual(signed(["SW-911"]), "SW-911")
        self.assertIsNone(signed(["SW-912"]), "a class outside S/P never signs")
        self.assertIsNone(signed(["SW-914"]), "a Steward ruling about another row never signs this one's stop line")
        self.assertIsNone(signed(["SW-910"], rid=None), "no row, no Steward signature")
        self.assertIsNone(signed(["SW-999"]), "an SW id the engine never recorded never signs")
        self.assertIsNone(signed(["SW-913"]), "an SW id written into DECISIONS.md is not a Steward record")
        self.assertIsNone(signed(["SW-910"], owner_only=True), "an SW ruling never covers a protected path")
        self.assertEqual(signed(["DEC-60"]), "DEC-60")
        self.assertEqual(signed(["DEC-1"]), "DEC-1", "heading entries still sign (v2)")
        for k in ("DEC-61", "DEC-62", "DEC-63", "DEC-64", "DEC-65", "DEC-67"):
            self.assertIsNone(signed([k]), f"{k} is not a signed, active table entry")
        self.assertEqual(signed(["DEC-66"]), "DEC-66")
        # end to end: `orch closecheck` over a STOP line, signed by a class-S Steward ruling on the row
        add_lane("QC", [("qc1", "-", 0, "manual", "")])
        dq = WS / "PHASE_QC" / "qc1"
        (dq / "run.out").write_text("STOP the heads moved\n")
        (dq / "PROGRESS.md").write_text("SIGNED_RULING SW-912\n")
        self.assertEqual(orch("closecheck", "qc1").returncode, 3)
        (dq / "PROGRESS.md").write_text("SIGNED_RULING SW-910\n")
        p = orch("closecheck", "qc1")
        self.assertEqual(p.returncode, 0, p.stdout)
        self.assertIn("signed=SW-910", p.stdout)
        # glob semantics: `*` crosses `/`, a trailing `/` or a directory protects its tree
        pats = ["keys/", "gates", "rules/*.md", "**/score*.py", "**/KEY"]
        for path, want in (("keys/a/b.txt", "keys/"), ("gates/g1.sh", "gates"), ("rules/BASE.md", "rules/*.md"),
                           ("rules/x/y.md", "rules/*.md"), ("a/b/scorer.py", "**/score*.py"),
                           ("score.py", "**/score*.py"), ("KEY", "**/KEY"),
                           ("gatesX/a", None), ("keysfile", None), ("README.md", None)):
            self.assertEqual(protected.matches(path, pats), want, path)

    def test_24_protected_base_survives_attempts_and_history_rewinds(self):
        """The row's base is kept across attempts (a change one attempt leaves is charged to the next);
        a HEAD rewound past an Owner commit is caught; a row cannot switch the check off mid-attempt."""
        import protected
        from types import SimpleNamespace
        repo = Path(tempfile.mkdtemp(dir=SBX))
        g = lambda *a: subprocess.run(["git", "-C", str(repo), *a], capture_output=True, text=True).stdout
        g("init", "-q"); g("config", "user.email", "t@t"); g("config", "user.name", "t")
        (repo / "gates").mkdir()
        (repo / "gates" / "g.sh").write_text("exit 0\n")
        (repo / "README").write_text("x\n")
        g("add", "-A"); g("commit", "-qm", "init")
        inst = repo / "ORCH"
        (inst / "state").mkdir(parents=True)
        cfg = SimpleNamespace(data={"guard": {"protected_paths": "gates/"}, "close": {"protected": True}, "steward": {}},
                              instance=inst, git_repo=repo, state=inst / "state", workspace=repo)
        self.assertIn("gates/", protected.patterns(cfg), "a string is one pattern, not its characters")
        head = g("rev-parse", "HEAD").strip()
        b1 = protected.make_base(cfg, "r1", head)
        (repo / "gates" / "g.sh").write_text("exit 1\n")          # attempt 1 leaves a change behind
        b2 = protected.make_base(cfg, "r1", g("rev-parse", "HEAD").strip())   # attempt 2
        self.assertEqual(b1, b2, "the base is kept until the row is accepted")
        ok, detail = protected.check(cfg, "r1", {}, [])
        self.assertFalse(ok, detail)
        self.assertIn("gates/g.sh (uncommitted)", detail)
        cfg.data["close"]["protected"] = False                       # a row switching the check off
        self.assertFalse(protected.check(cfg, "r1", {}, [])[0], "the base keeps the set it was made with")
        cfg.data["close"]["protected"] = True
        protected.clear_base(cfg, "r1")
        # an Owner commit, then a row that rewinds HEAD past it
        (repo / "gates" / "g.sh").write_text("exit 2\n")
        g("commit", "-qam", "owner: a stricter gate")
        protected.record(cfg, g("rev-parse", "HEAD").strip(), "plan:x", ["gates/g.sh"])
        protected.make_base(cfg, "r2", g("rev-parse", "HEAD").strip())
        self.assertTrue(protected.check(cfg, "r2", {}, [])[0])
        g("reset", "-q", "--hard", "HEAD~1")
        ok, detail = protected.check(cfg, "r2", {}, [])
        self.assertFalse(ok, "a rewound HEAD is not 'nothing touched'")
        self.assertIn("no longer descends", detail)
        # a session's committed change that it reverted again is no change (net); one it keeps is charged
        protected.make_base(cfg, "r3", g("rev-parse", "HEAD").strip())
        (repo / "gates" / "g.sh").write_text("exit 3\n")
        g("commit", "-qam", "row: change the gate")
        self.assertFalse(protected.check(cfg, "r3", {}, [])[0])
        g("revert", "--no-edit", "HEAD")
        ok, detail = protected.check(cfg, "r3", {}, [])
        self.assertTrue(ok, detail)
        # an edit hidden from git status by assume-unchanged is still charged
        g("update-index", "--assume-unchanged", "gates/g.sh")
        (repo / "gates" / "g.sh").write_text("exit 4\n")
        ok, detail = protected.check(cfg, "r3", {}, [])
        self.assertFalse(ok)
        self.assertIn("assume-unchanged", detail)
        g("update-index", "--no-assume-unchanged", "gates/g.sh")
        g("checkout", "--", "gates/g.sh")
        # a repo with no commit yet at the base: every later commit is since the base
        fresh = Path(tempfile.mkdtemp(dir=SBX))
        subprocess.run(["git", "init", "-q", str(fresh)])
        cfg2 = SimpleNamespace(data={"guard": {"protected_paths": ["gates/"]}, "close": {}, "steward": {}},
                               instance=fresh / "ORCH", git_repo=fresh, state=fresh / "ORCH" / "state", workspace=fresh)
        b = protected.make_base(cfg2, "u1", "")
        self.assertTrue(b.get("unborn"))
        self.assertTrue(protected.check(cfg2, "u1", {}, [])[0], "an empty repo with nothing touched passes")
        (fresh / "gates").mkdir()
        (fresh / "gates" / "g.sh").write_text("x\n")
        gf = lambda *a: subprocess.run(["git", "-C", str(fresh), *a], capture_output=True, text=True)
        gf("add", "-A"); gf("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "first")
        self.assertFalse(protected.check(cfg2, "u1", {}, [])[0], "the first commit is since the base")

    def test_25_ancestry_unset_orch_row_cannot_reach_operator_power(self):
        """Drill (drop B item 4): a process started inside a session's pane with ORCH_ROW unset (or
        borrowed) is still that session — orch reset, park/unpark, inbox done, service, a plan:
        commit, the operator's overrides and force-release all refuse; the operator's shell can."""
        add_lane("C", [("cap0", "-", 0, "mock", ""), ("cap1", "cap0", 0, "mock", "")])
        (WS / "PHASE_C" / "cap1" / "x.txt").write_text("x\n")
        script = r"""
O="$ORCH_INSTANCE/orch"
U="env -u ORCH_ROW -u ORCH_KIND -u ORCH_PLAN_DIR"
cd "$ORCH_INSTANCE/.."
pane=$(cut -d' ' -f2 "$ORCH_INSTANCE/state/cap1.window")
[[ $(cat /proc/$pane/cgroup) != "$(cat /proc/$(awk '/^PPid/{print $2}' /proc/$pane/status)/cgroup)" ]] \
  && echo "PANE_CGROUP=own" || echo "PANE_CGROUP=shared"
echo "== who"; $U python3 "@LIB@/capability.py" who; echo "rc=$?"
echo "== reset"; $U "$O" reset cap0; echo "rc=$?"
echo "== park"; $U "$O" park cap0 "x"; echo "rc=$?"
echo "== unpark"; $U "$O" unpark cap0; echo "rc=$?"
echo "== inbox"; $U "$O" inbox done 1; echo "rc=$?"
echo "== service"; $U "$O" service status; echo "rc=$?"
echo "== borrowed"; $U ORCH_ROW=cap0 "$O" done cap0 "not mine"; echo "rc=$?"
echo "== plan"; $U "$O" commit plan:x -m "m" -- PHASE_C/cap1/x.txt; echo "rc=$?"
echo "== scanner"; $U ORCH_SCANNER_OVERRIDE=1 "$O" commit cap1 -m "m" -- PHASE_C/cap1/x.txt; echo "rc=$?"
echo "== shared"; $U ORCH_SHARED_WHOLE=1 "$O" commit cap1 -m "m" -- PHASE_C/cap1/x.txt; echo "rc=$?"
echo "== force"; $U "$O" guard force-release git-lock; echo "rc=$?"
echo "== setup"; $U "$O" setup apply --set guard.capability_check=false; echo "rc=$?"
echo "== preflight"; $U "$O" preflight cap0 --review; echo "rc=$?"
echo "== decoy"; $U ORCH_INSTANCE="$ORCH_INSTANCE/../decoyA" "@ORCH@" pause C; echo "rc=$?"
echo "== decoyoff"; $U ORCH_INSTANCE="$ORCH_INSTANCE/../decoyB" "@ORCH@" reset cap0; echo "rc=$?"
( ( sleep 0.5; $U "$O" reset cap0 > "$ORCH_INSTANCE/mockrun/cap1.dfork" 2>&1; echo "rc=$?" >> "$ORCH_INSTANCE/mockrun/cap1.dfork" ) & )
setsid bash -c "sleep 0.5; $U '$O' reset cap0 > '$ORCH_INSTANCE/mockrun/cap1.setsid' 2>&1; echo rc=\$? >> '$ORCH_INSTANCE/mockrun/cap1.setsid'" &
sleep 4
"""
        # two decoy instances a session could point ORCH_INSTANCE at: A shares the rig's control/leases
        # (empty state: no records), B shares its state with the check switched off in its own orch.yaml
        for name, shared, off in (("decoyA", ("control", "leases"), False), ("decoyB", ("state", "control", "leases"), True)):
            d = WS / name
            d.mkdir()
            (d / "orch.yaml").write_text((INST / "orch.yaml").read_text())
            for s in shared:
                (d / s).symlink_to(INST / s)
            for q in INST.glob("queue_*.tsv"):
                (d / q.name).symlink_to(q)
            if off:
                import yaml
                data = yaml.safe_load((d / "orch.yaml").read_text())
                data.setdefault("guard", {})["capability_check"] = False
                (d / "orch.yaml").write_text(yaml.safe_dump(data, sort_keys=False))
        self.addCleanup(lambda: [shutil.rmtree(WS / n, ignore_errors=True) for n in ("decoyA", "decoyB")])
        ctl("MOCK_RUN_cap1", script.replace("@LIB@", str(HOME / "lib")).replace("@ORCH@", str(ORCH)))
        ctl("MOCK_DELAY_cap1", "0")
        orch("start", "C", check=True)
        wait(lambda: phase("cap1") == "accepted", 120, what="cap1 accepted")
        out = (INST / "mockrun" / "cap1.out").read_text()
        sect = {}
        for blk in out.split("== ")[1:]:
            name, _, body = blk.partition("\n")
            sect[name.strip()] = body
        self.assertIn("cap1 row ancestor", sect["who"], out)
        for name in ("reset", "park", "unpark", "inbox", "service", "borrowed", "plan", "scanner", "shared", "force",
                     "setup", "preflight", "decoy", "decoyoff"):
            self.assertIn("REFUSED", sect[name], f"{name} must refuse inside the session:\n{out}")
            self.assertNotIn("rc=0", sect[name], f"{name} exited 0 inside the session:\n{out}")
        self.assertIn("descends from cap1's pane", sect["reset"])
        self.assertIn("session cap1 may only claim itself", sect["borrowed"])
        self.assertIn("planning commits (plan:x)", sect["plan"])
        self.assertFalse((INST / "control" / "PAUSE_C").exists(), "the decoy did not pause the real rig")
        import yaml
        self.assertNotEqual((yaml.safe_load((INST / "orch.yaml").read_text()).get("guard") or {}).get("capability_check"),
                            False, "setup apply from inside did not switch the check off")
        if "PANE_CGROUP=own" in out:      # per-pane cgroups (tmux on systemd): leaving the tree does not help
            for f in ("cap1.dfork", "cap1.setsid"):
                txt = (INST / "mockrun" / f).read_text()
                self.assertIn("REFUSED", txt, f"{f}: a re-parented process is still the session:\n{txt}")
        self.assertNotIn("reset", [e["event"] for e in events("cap0") if not e.get("by")])
        self.assertEqual(phase("cap0"), "accepted")
        self.assertNotIn("m", git("log", "--format=%s", "-20").splitlines(), "nothing was committed from inside")
        # the operator's shell (this process: no pane of the rig among its ancestors) can
        p = orch("reset", "cap0")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("reset", [e["event"] for e in events("cap0") if not e.get("by")])
        wait(lambda: phase("cap0") == "accepted", 90, what="cap0 re-run after the operator's reset")

    def test_26_ancestry_records_pid_reuse_and_switch(self):
        """The check reads every session record of the instance; a pid reused by a later process is a
        stranger; guard.capability_check: false restores the v2 environment-only behaviour."""
        import capability
        from types import SimpleNamespace
        st = Path(tempfile.mkdtemp(dir=SBX))
        cfg = SimpleNamespace(data={"guard": {}}, state=st)
        me, parent = os.getpid(), os.getppid()
        self.assertIsNone(capability.inside(cfg), "no records: outside")
        (st / "zz1.window").write_text(f"@1 {parent} 1000\n")             # pid recorded long before it started
        self.assertIsNone(capability.inside(cfg), "a reused pid is a stranger")
        (st / "zz1.window").write_text(f"@1 {parent} {int(time.time())}\n")
        s = capability.inside(cfg)
        self.assertEqual((s["sid"], s["kind"], s["how"]), ("zz1", "row", "ancestor"))
        (st / "zz1.window").unlink()
        (st / "pl-t-1.dispatch.json").write_text(json.dumps({"ident": f"@2 {me}", "open": False,
                                                             "epoch": int(time.time()), "kind": "planner"}))
        s = capability.inside(cfg)
        self.assertEqual((s["sid"], s["kind"]), ("pl-t-1", "planner"), "a closed record still names its pane")
        with mock.patch.dict(os.environ, {"ORCH_ROW": "a1", "ORCH_KIND": "row"}):
            self.assertEqual(capability.identity(cfg), ("pl-t-1", "planner"), "the pane wins over a borrowed id")
            cfg.data["guard"]["capability_check"] = False
            self.assertIsNone(capability.inside(cfg))
            self.assertEqual(capability.identity(cfg), ("a1", "row"), "off: the v2 environment decides")
            cfg.data["guard"]["capability_check"] = True
        (st / "pl-t-1.dispatch.json").unlink()
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ORCH_ROW", None)
            os.environ.pop("ORCH_KIND", None)
            self.assertIsNone(capability.inside(cfg))
            self.assertEqual(capability.identity(cfg), ("", ""), "outside, check on: the operator")
        now = int(time.time())
        # a malformed record never switches the check off for the others
        (st / "bad.dispatch.json").write_text("[]")
        (st / "bad2.dispatch.json").write_text('{"ident": 5, "epoch": "x"}')
        (st / "zz2.window").write_text(f"@3 {parent} {now}\n")
        self.assertEqual(capability.inside(cfg)["sid"], "zz2", "malformed neighbours are skipped")
        # a record with no epoch cannot be told from a reused pid
        (st / "zz2.window").unlink()
        (st / "zz3.dispatch.json").write_text(json.dumps({"ident": f"@4 {parent}", "open": True}))
        self.assertIsNone(capability.inside(cfg))
        # an adopted record counts while open, and is the operator's window again once closed
        (st / "zz3.dispatch.json").write_text(json.dumps({"ident": f"@4 {parent}", "open": True, "adopted": True,
                                                          "epoch": now}))
        self.assertEqual(capability.inside(cfg)["sid"], "zz3")
        (st / "zz3.window").write_text(f"@4 {parent} {now}\n")
        (st / "zz3.dispatch.json").write_text(json.dumps({"ident": f"@4 {parent}", "open": False, "adopted": True,
                                                          "epoch": now}))
        self.assertIsNone(capability.inside(cfg), "a closed adoption is not a session (nor is its .window)")
        for f in st.iterdir():
            f.unlink()
        # the newest record of a recycled pid names the session
        (st / "old.window").write_text(f"@5 {parent} {now - 50}\n")
        (st / "new.window").write_text(f"@6 {parent} {now}\n")
        self.assertEqual(capability.inside(cfg)["sid"], "new")
        for f in st.iterdir():
            f.unlink()
        # cgroups: silent when the pane shares its parent's cgroup; a pane with its own claims its members
        other = subprocess.Popen(["sleep", "30"])
        self.addCleanup(other.kill)
        (st / "cg1.window").write_text(f"@7 {other.pid} {now + 1}\n")
        with mock.patch.object(capability, "cgroup", lambda p: "0::/same"):
            self.assertIsNone(capability.inside(cfg), "a shared cgroup never refuses on its own")
        real_ppid = capability._ppid
        with mock.patch.object(capability, "cgroup", lambda p: "0::/pane" if p in (other.pid, me) else "0::/server"), \
                mock.patch.object(capability, "_ppid", lambda p: 1 if p == other.pid else real_ppid(p)):
            s = capability.inside(cfg)
            self.assertEqual((s["sid"], s["how"]), ("cg1", "ancestor-cgroup"))
            cfg.data["guard"]["capability_cgroup"] = False
            self.assertIsNone(capability.inside(cfg))
            cfg.data["guard"]["capability_cgroup"] = True
        (st / "cg1.window").unlink()
        # the user's pane registry: a decoy instance sharing the rig's files is still the rig's session
        reg = Path(tempfile.mkdtemp(dir=SBX))
        real = Path(tempfile.mkdtemp(dir=SBX))
        for s in ("state", "control", "leases"):
            (real / s).mkdir()
        (real / "orch.yaml").write_text("guard: {}\n")
        rcfg = SimpleNamespace(data={"guard": {}}, instance=real, state=real / "state", control=real / "control",
                               leases=real / "leases", git_repo=real)
        with mock.patch.object(capability, "REGISTRY", reg):
            capability.register(rcfg, "r9", "row", f"@8 {parent}")
            decoy = Path(tempfile.mkdtemp(dir=SBX))
            (decoy / "state").mkdir()
            (decoy / "control").symlink_to(real / "control")
            dcfg = SimpleNamespace(data={"guard": {"capability_check": False}}, instance=decoy, state=decoy / "state",
                                   control=decoy / "control", leases=decoy / "leases", git_repo=decoy)
            s = capability.inside(dcfg)
            self.assertEqual((s["sid"], s["how"]), ("r9", "registry"), "the decoy's own switch does not count")
            lone = Path(tempfile.mkdtemp(dir=SBX))
            lcfg = SimpleNamespace(data={"guard": {}}, instance=lone, state=lone / "state", control=lone / "control",
                                   leases=lone / "leases", git_repo=lone)
            self.assertIsNone(capability.inside(lcfg), "a rig that shares nothing (a sandbox) is the caller's own")
            (real / "orch.yaml").write_text("guard: {capability_check: false}\n")
            self.assertIsNone(capability.inside(dcfg), "the owning rig's switch decides")

    def test_27_row_add_through_the_engine(self):
        """Drill (drop B item 5): a course-loop row adds a row with `orch row add` (the engine validates and
        appends); a gate removal, a gate-kind change and a rearm= grant wider than its own are refused, also
        when the request bypasses the CLI; a plain row may not add rows. With guard.queue_watch a hand edit is
        a digest line; an operator plan: commit and `orch row sync` bless the queues."""
        import rowadd
        from config_parser import load
        add_lane("QG", [("gq1", "-", 1, "mock", "")])
        add_lane("Q", [("cl1", "-", 0, "mock", "rearm=jd1,round"), ("jd1", "-", 0, "mock", "")])
        for rid in ("nq1", "nq2"):
            (WS / "PHASE_Q" / rid).mkdir(parents=True, exist_ok=True)
            (WS / "PHASE_Q" / rid / "PLAN.md").write_text(PLAN.format(id=rid))
        cfg_set("guard.queue_watch_grace", 6)
        cfg_set("guard.queue_watch", True)
        self.addCleanup(cfg_set, "guard.queue_watch", False)
        wait(lambda: (INST / "state" / "queues.sha").exists(), 60, what="the queue watch's baseline")
        script = r"""
O="$ORCH_INSTANCE/orch"
cd "$ORCH_INSTANCE/.."
echo "== ok"; "$O" row add Q "nq1 jd1 0 mock PHASE_Q/nq1/PLAN.md rearm=jd1" "a fix row for the judge"; echo "rc=$?"
echo "== gate"; "$O" row add QG "gq1 - 0 mock PHASE_QG/gq1/PLAN.md" "the gate is in the way"; echo "rc=$?"
echo "== kind"; "$O" row add QG "gq1 - 1 mock PHASE_QG/gq1/PLAN.md gate_kind=steward" "let the Steward pass it"; echo "rc=$?"
echo "== wide"; "$O" row add Q "nq2 - 0 mock PHASE_Q/nq2/PLAN.md rearm=jd1,cap0" "more power"; echo "rc=$?"
echo "== self"; "$O" row add Q "nq2 - 0 mock PHASE_Q/nq2/PLAN.md rearm=cl1" "re-arm me"; echo "rc=$?"
for _ in $(seq 60); do [[ -f "$ORCH_INSTANCE/mockrun/jd1.forged" ]] && break; sleep 1; done
echo "== forged"
python3 - <<'EOF'
import json, os, time
from pathlib import Path
d = Path(os.environ["ORCH_INSTANCE"]) / "state" / "rowadd"
(d / "requests").mkdir(parents=True, exist_ok=True)
stem = f"cl1.{int(time.time() * 1000)}"
(d / "requests" / f"{stem}.json").write_text(json.dumps({"session": "cl1", "lane": "Q", "why": "past the CLI",
    "line": "nq2 - 0 mock PHASE_Q/nq2/PLAN.md rearm=jd1,cap0"}))
for _ in range(60):
    for out in ("done", "refused"):
        f = d / "done" / f"{stem}.{out}.json"
        if f.exists():
            print(out.upper(), json.loads(f.read_text())["why"]); raise SystemExit(0)
    time.sleep(1)
print("NO ANSWER")
EOF
"""
        ctl("MOCK_RUN_cl1", script)
        # jd1, a plain row: refused by the CLI, and refused by the engine when it files a request in the name of
        # cl1 (live, allowed) from its own process — a request file proves nothing by its name alone
        ctl("MOCK_RUN_jd1", 'cd "$ORCH_INSTANCE/.."\n"$ORCH_INSTANCE/orch" row add Q '
                            '"nq2 - 0 mock PHASE_Q/nq2/PLAN.md" "a plain row cutting rows"; echo "rc=$?"\n'
                            'echo "== impersonate"\n'
                            "python3 - <<'EOF'\n"
                            "import json, os, sys, time\n"
                            f"sys.path.insert(0, {str(HOME / 'lib')!r})\n"
                            "import capability\n"
                            "from pathlib import Path\n"
                            "d = Path(os.environ['ORCH_INSTANCE']) / 'state' / 'rowadd'\n"
                            "(d / 'requests').mkdir(parents=True, exist_ok=True)\n"
                            "stem = f'cl1.{int(time.time() * 1000)}'\n"
                            "(d / 'requests' / f'{stem}.json').write_text(json.dumps({'session': 'cl1', 'lane': 'Q',\n"
                            "    'why': 'in cl1s name', 'line': 'nq2 - 0 mock PHASE_Q/nq2/PLAN.md',\n"
                            "    'pid': os.getpid(), 'start': capability.started(os.getpid())}))\n"
                            "for _ in range(60):\n"
                            "    for out in ('done', 'refused'):\n"
                            "        f = d / 'done' / f'{stem}.{out}.json'\n"
                            "        if f.exists():\n"
                            "            print(out.upper(), json.loads(f.read_text())['why']); raise SystemExit(0)\n"
                            "    time.sleep(1)\n"
                            "print('NO ANSWER')\n"
                            "EOF\n"
                            'touch "$ORCH_INSTANCE/mockrun/jd1.forged"\n')
        orch("start", "Q", check=True)
        wait(lambda: phase("cl1") == "accepted" and phase("jd1") == "accepted", 150, what="cl1 and jd1 accepted")
        out = (INST / "mockrun" / "cl1.out").read_text()
        sect = {}
        for blk in out.split("== ")[1:]:
            name, _, body = blk.partition("\n")
            sect[name.strip()] = body
        self.assertIn("DONE row add: nq1 appended to queue_Q.tsv", sect["ok"], out)
        self.assertIn("rc=0", sect["ok"], out)
        self.assertIn("REFUSED gate removal", sect["gate"], out)
        self.assertIn("REFUSED gate change", sect["kind"], out)
        self.assertIn("wider than cl1's own grants", sect["wide"], out)
        self.assertIn("re-arm its caller", sect["self"], out)
        self.assertIn("REFUSED", sect["forged"], out)
        self.assertIn("names no caller process", sect["forged"], "a hand-written request is not an orch row add")
        for name in ("gate", "kind", "wide", "self"):
            self.assertIn("rc=1", sect[name], f"{name}:\n{out}")
        jd = (INST / "mockrun" / "jd1.out").read_text()
        self.assertIn("may not add rows", jd)
        imp = jd.split("== impersonate")[1]
        self.assertIn("REFUSED", imp, jd)
        self.assertIn("not an `orch row add`", imp, jd)
        q = (INST / "queue_Q.tsv").read_text()
        self.assertIn("nq1\tjd1\t0\tmock\tPHASE_Q/nq1/PLAN.md\trearm=jd1\n", q)
        self.assertNotIn("nq2", q)
        self.assertIn("gq1\t-\t1\tmock", (INST / "queue_QG.tsv").read_text(), "the gate is still there")
        self.assertIn("cl1", [e.get("by") for e in events("nq1") if e["event"] == "row_add"])
        wait(lambda: phase("nq1") == "accepted", 120, what="the added row runs")
        edited = lambda: [e for e in all_events() if e.get("event") == "queue_edited"]
        self.assertEqual(edited(), [], "rows added by orch row add are not edits")
        # a hand edit that drops the gate: reported once, named, in the digest; `orch row diff` shows it
        add_lane("QG", [("gq1", "-", 0, "mock", "")])
        wait(lambda: edited(), 5, what="a gate removal reported at once (no grace)")
        self.assertIn("gq1: gate removed", edited()[0]["summary"])
        p = orch("digest")
        self.assertIn("Queue edited outside orch row add", p.stdout)
        self.assertIn("gq1: gate removed", p.stdout)
        p = orch("row", "diff")
        self.assertEqual(p.returncode, 1, p.stdout)
        self.assertIn("gq1: gate removed", p.stdout)
        add_lane("QG", [("gq1", "-", 1, "mock", "")])            # put back: the queue matches the record again
        wait(lambda: orch("row", "diff").returncode == 0, 30, what="the restored queue matches")
        reported = lambda: json.loads((INST / "state" / "queues.reported.json").read_text())
        wait(lambda: "QG" not in reported(), 30, what="the watch saw the restored queue")
        add_lane("QG", [("gq1", "-", 0, "mock", "")])            # the same edit again is reported again
        wait(lambda: len(edited()) == 2, 30, what="the repeated edit reported")
        add_lane("QG", [("gq1", "-", 1, "mock", "")])
        wait(lambda: orch("row", "diff").returncode == 0, 30, what="the restored queue matches")
        # comments and whitespace are not edits
        (INST / "queue_QG.tsv").write_text("# a note\n" + (INST / "queue_QG.tsv").read_text().replace("\t", "   "))
        p = orch("row", "diff")
        self.assertEqual(p.returncode, 0, p.stdout)
        time.sleep(8)
        self.assertEqual(len(edited()), 2)
        # the operator's shell: a hand edit committed with an operator plan: commit is blessed, not reported
        cfg = load(str(INST))
        (WS / "PHASE_QG" / "gq2").mkdir(parents=True, exist_ok=True)
        (WS / "PHASE_QG" / "gq2" / "PLAN.md").write_text(PLAN.format(id="gq2"))
        # (the watch sees the edit before the commit; guard.queue_watch_grace gives the operator time)
        with open(INST / "queue_QG.tsv", "a") as f:
            f.write("gq2\t-\t1\tmock\tPHASE_QG/gq2/PLAN.md\n")
        time.sleep(2)
        p = orch("commit", "plan:qg", "-m", "operator: gq2", "--", "ORCH/queue_QG.tsv", "PHASE_QG/gq2/PLAN.md")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        wait(lambda: any(e.get("event") == "queue_blessed" for e in events("queue:QG")), 30, what="operator commit blessed")
        time.sleep(8)
        self.assertEqual(len(edited()), 2, "an operator plan: commit is the operator's shell")
        # the operator's own row add: appended at once; append-only for the operator too
        (WS / "PHASE_QG" / "gq3").mkdir(parents=True, exist_ok=True)
        (WS / "PHASE_QG" / "gq3" / "PLAN.md").write_text(PLAN.format(id="gq3"))
        p = orch("row", "add", "QG", "gq3 gq2 1 mock PHASE_QG/gq3/PLAN.md gate_kind=steward", "operator adds")
        self.assertEqual(p.returncode, 0, p.stdout)
        p = orch("row", "add", "QG", "gq2 - 0 mock PHASE_QG/gq2/PLAN.md", "drop it")
        self.assertIn("REFUSED gate removal", p.stdout)
        # a hand edit the operator accepts with orch row sync (a session may not)
        with open(INST / "queue_QG.tsv", "a") as f:
            f.write("gq4\t-\t1\tmock\tPHASE_QG/gq3/PLAN.md\n")
        wait(lambda: len(edited()) == 3, 30, what="the next hand edit")
        self.assertIn("+1 (gq4)", edited()[-1]["summary"])
        # a later row add to that lane does not re-report the same hand edit, nor bless it
        (WS / "PHASE_QG" / "gq5").mkdir(parents=True, exist_ok=True)
        (WS / "PHASE_QG" / "gq5" / "PLAN.md").write_text(PLAN.format(id="gq5"))
        p = orch("row", "add", "QG", "gq5 - 1 mock PHASE_QG/gq5/PLAN.md", "operator adds")
        self.assertEqual(p.returncode, 0, p.stdout)
        time.sleep(8)
        self.assertEqual(len(edited()), 3)
        self.assertIn("gq4", orch("row", "diff").stdout)
        p = orch("row", "sync", "x", env=dict(ENV, ORCH_ROW="cl1", ORCH_KIND="row"))
        self.assertIn("REFUSED", p.stdout)
        p = orch("row", "sync", "reviewed by hand")
        self.assertEqual(p.returncode, 0, p.stdout)
        self.assertEqual(orch("row", "diff").returncode, 0)
        time.sleep(8)
        self.assertEqual(len(edited()), 3)
        self.assertNotIn("row add", " ".join(__import__("digest").human_touches(all_events())))

    def test_28_row_add_rules(self):
        """orch row add, unit: who may add, the planner's scope and grants, the operator, malformed lines,
        archived lanes, the drop-D phase hook, the keyed diff, and engine requests that are not a live session's."""
        import types
        import rowadd
        from config_parser import load
        cfg = load(str(INST))
        if not (INST / "queue_QG.tsv").exists():
            add_lane("QG", [("gq1", "-", 1, "mock", "")])
        if not (INST / "queue_Q.tsv").exists():
            add_lane("Q", [("cl1", "-", 0, "mock", "rearm=jd1,round"), ("jd1", "-", 0, "mock", "")])
        orch("round", "new", "zz", "--title", "unit", "--lanes", "QG", check=True)
        for d in ("PHASE_QG/zq1", "PHASE_Q/zq2", "PHASE_CY/cy1"):
            (WS / d).mkdir(parents=True, exist_ok=True)
            (WS / d / "PLAN.md").write_text(PLAN.format(id=d))

        class St:
            recs = {"pl-zz-1": {"open": True, "ident": "@1 1", "snapshot": {"round": "zz"}},
                    "cl1": {"open": True, "ident": "@2 1", "snapshot": {"meta": {"rearm": "jd1,round"}}},
                    "a9": {"open": True, "ident": "@3 1", "snapshot": {"meta": {}}},
                    "sw-1-1": {"open": True, "ident": "@4 1"},
                    "pl-zz-2": {"open": False, "ident": "@5 1", "snapshot": {"round": "zz"}}}

            def dispatch(self, sid):
                return self.recs.get(sid, {})

        st = St()
        pl, _ = rowadd.powers(cfg, st, "pl-zz-1")
        self.assertEqual((pl["kind"], pl["lanes"]), ("planner", ["QG"]))
        cl, _ = rowadd.powers(cfg, st, "cl1")
        self.assertEqual((cl["grants"], cl["lanes"]), (["jd1", "round"], ["Q"]), "its own lane only")
        for sid, why in (("a9", "may not add rows"), ("sw-1-1", "steward session"), ("pl-zz-2", "not a live"),
                         ("ghost", "not a live")):
            who, msg = rowadd.powers(cfg, st, sid)
            self.assertIsNone(who, sid)
            self.assertIn(why, msg, sid)
        op = {"sid": "", "kind": "operator"}
        plan, gplan, qplan = "PHASE_QG/zq1/PLAN.md", "PHASE_QG/gq1/PLAN.md", "PHASE_Q/zq2/PLAN.md"

        def v(who, lane, line):
            return rowadd.validate(cfg, st, who, lane, line)[:2]

        self.assertEqual(v(pl, "QG", f"zq1 gq1 0 mock {plan} rearm=gq1"), (True, ""))
        refused = [
            (pl, "Q", f"zq1 - 0 mock {plan}", "adds rows only to round zz's lanes"),
            (pl, "QG", f"zq1 - 0 mock {plan} rearm=round", "start any round"),
            (pl, "QG", f"zq1 - 0 mock {plan} rearm=jd1", "outside round zz's lanes"),
            (pl, "QG", f"zq1 - 0 mock {plan} review=0", "review=0"),
            (pl, "QG", f"ub-x - 0 mock {plan}", "session prefix"),
            (pl, "QG", f"zq1 nope 0 mock {plan}", "dep 'nope' is not a row"),
            (pl, "QG", "zq1 - 0 mock PHASE_QG/none/PLAN.md", "does not exist"),
            (pl, "QG", "zq1 - 0 mock ../x/PLAN.md", "without '..'"),
            (pl, "QG", f"zq1 - 0 bogus {plan}", "executors.allowed"),
            (pl, "QG", f"zq1 - 0 mock {plan} gate_kind=steward", "no gate"),
            (pl, "QG", f"zq1 - 1 mock {plan} gate_kind=boss", "not one of"),
            (pl, "QG", f"zq1 - 2 mock {plan}", "gate column"),
            (pl, "QG", "zq1 - 0", "needs 5"),
            (pl, "QG", f"zq1 - 0 mock {plan}\nzq2 - 0 mock {plan}", "one queue line"),
            (pl, "QG", f"zq1 - 0 mock {plan} rearm=zq1", "names the row itself"),
            (pl, "QG", f"zq1 - 0 mock {plan} rearm=a,b!", "malformed"),
            (pl, "QG", "# a comment", "comment"),
            (pl, "Q G", f"zq1 - 0 mock {plan}", "not a lane name"),
            (pl, "QG", f"zq1 - 1 mock {plan} gate_kind=steward", "a session never cuts a gate its own lane can pass"),
            (pl, "QG", f"zq1 - 0 mock {plan} reviewer=bogus", "reviewer=bogus"),
            (pl, "QG", f"zq1 - 0 mock {gplan}", "already row gq1's"),               # a twin around gq1's gate
            (cl, "Q", f"zq2 - 0 mock {qplan} rearm=cap0", "wider than cl1"),
            (cl, "QG", f"zq2 - 0 mock {qplan}", "only to its own lane"),
            (cl, "Q", f"zq2 - 0 mock {qplan} PHASE_QG", "outside cl1's own allowlist"),
            (op, "QG", f"gq1 - 0 mock {plan}", "REFUSED gate removal"),
            (op, "QG", f"jd1 - 0 mock {plan}", "already exists"),
            (op, "QG", "zq1 - 0 mock ORCH/orch.yaml", "in or above the instance"),
        ]
        for who, lane, line, why in refused:
            ok, msg = v(who, lane, line)
            self.assertFalse(ok, line)
            self.assertIn(why, msg, line)
        self.assertEqual(v(op, "QG", f"zq1 - 0 mock {plan} review=0 rearm=round,cap0"), (True, ""),
                         "the operator's power is not narrowed")
        self.assertEqual(v(cl, "Q", f"zq2 - 0 mock {qplan} rearm=round PHASE_Q/cl1/out"), (True, ""))
        self.assertEqual(v(op, "QG", f"zq1 - 1 mock {gplan} gate_kind=steward"), (True, ""), "the operator may")
        # an id with history in state/ is not new (an accepted result would make a gated row 'done' at once)
        real = store()
        real.write_result("gone1", {"phase": "accepted"})
        self.addCleanup(real.clear_result, "gone1")
        self.assertIn("used on this rig before (a result record", rowadd.validate(cfg, real, op, "QG", f"gone1 - 1 mock {plan}")[1])
        # a round a course-loop row created is its own; another open round's lane is not taken by a session
        p = orch("round", "new", "cy", "--title", "fix", "--lanes", "CY", env=dict(ENV, ORCH_ACTOR="cl1"))
        self.assertEqual(p.returncode, 0, p.stdout)
        cl, _ = rowadd.powers(cfg, st, "cl1")
        self.assertEqual(cl["lanes"], ["Q", "CY"])
        self.assertEqual(v(cl, "CY", "cy1 - 0 mock PHASE_CY/cy1/PLAN.md"), (True, ""))
        p = orch("round", "add-lanes", "zz", "CY", env=dict(ENV, ORCH_ACTOR="pl-zz-1"))
        self.assertIn("belong to another open round", p.stdout)
        p = orch("round", "new", "cz", "--title", "x", "--lanes", "QG", env=dict(ENV, ORCH_ACTOR="sw-1-1"))
        self.assertIn("belong to another open round", p.stdout)
        p = orch("round", "new", "cw", "--title", "x", "--lanes", "A", env=dict(ENV, ORCH_ACTOR="pl-cw-1"))
        self.assertIn("its rows already ran: a1", p.stdout, "a live lane no round lists is not a session's either")
        # archived lanes and ids
        (INST / "archive").mkdir(exist_ok=True)
        arch = INST / "archive" / "queue_ZA.tsv"
        arch.write_text(f"za1\t-\t0\tmock\t{plan}\n")
        self.addCleanup(arch.unlink, missing_ok=True)
        self.assertIn("archived", v(op, "ZA", f"zq1 - 0 mock {plan}")[1])
        self.assertIn("already exists (lane ZA, archived)", v(op, "QG", f"za1 - 0 mock {plan}")[1])
        # drop D's hook: a fence, a row cap and a failing check refuse (the real lib/phases.py: test_35/36)
        fake = types.ModuleType("phases")
        fake.fence = lambda cfg, lanes, new_round=False: "phase p1 is closed to lane QG" if "QG" in lanes else ""
        with mock.patch.object(rowadd, "PHASES", INST / "orch.yaml"), mock.patch.dict(sys.modules, {"phases": fake}):
            self.assertIn("phase p1 is closed", v(op, "QG", f"zq1 - 0 mock {plan}")[1])
            self.assertTrue(v(op, "Q", f"zq1 - 0 mock {plan}")[0], "no row_cap defined: the fence alone decides")
            fake.row_cap = lambda cfg, lane, row: "row cap 3 of phase p1 reached"
            self.assertIn("row cap 3", v(op, "Q", f"zq1 - 0 mock {plan}")[1])
            fake.row_cap = lambda cfg, lane, row: 1 / 0
            self.assertIn("refused, not passed", v(op, "Q", f"zq1 - 0 mock {plan}")[1])
        # the keyed diff names gate removals
        d = rowadd.diff(cfg, "QG", ["g1\t-\t1\tmock\tp", "g3\t-\t1\tmock\tp"], ["g1\t-\t0\tmock\tp", "g2\t-\t0\tmock\tp"])
        self.assertEqual((d["added"], d["removed"], d["changed"]), (["g2"], ["g3"], ["g1"]))
        self.assertEqual(d["gates"], ["g1: gate removed", "g3: gated row removed"])
        d.update(lane="QG", kind="edited")
        self.assertIn("GATES: g1: gate removed", rowadd.describe(d))
        # the engine's proof: a request is answered only for a live `orch row add` inside the named session's pane
        import capability
        me = os.getpid()
        rec = {"session": "cl1", "lane": "Q", "line": "x", "pid": me, "start": capability.started(me)}
        self.assertIn("not an `orch row add`", rowadd.proof(cfg, rec), "this test process is no orch row add")
        with mock.patch.object(rowadd, "_argv", lambda pid: ["python3", "/x/lib/rowadd.py", "add", "Q", "x", "why"]):
            with mock.patch.object(capability, "inside", lambda cfg, pid=None: {"sid": "jd1"}):
                self.assertIn("runs in jd1 session's pane, not cl1's", rowadd.proof(cfg, rec))
            with mock.patch.object(capability, "inside", lambda cfg, pid=None: {"sid": "cl1"}):
                self.assertEqual(rowadd.proof(cfg, rec), "")
                self.assertIn("gone", rowadd.proof(cfg, dict(rec, start=rec["start"] - 30)))
                self.assertIn("names no caller", rowadd.proof(cfg, {k: v for k, v in rec.items() if k != "pid"}))
                with mock.patch.dict(cfg.data["guard"], {"capability_check": False}):
                    self.assertEqual(rowadd.proof(cfg, {"session": "cl1"}), "", "check off: v2, the name decides")
        # engine requests: a session that is not live, and a file name that is not its session's
        reqd = rowadd.rdir(cfg) / "requests"
        stems = []
        for fname, sid in (("sw-999-1", "sw-999-1"), ("a1", "pl-zz-1")):
            stem = f"{fname}.{int(time.time() * 1000)}"
            (reqd / f"{stem}.json").write_text(json.dumps({"session": sid, "lane": "QG", "why": "x",
                                                           "line": f"zq9 - 0 mock {plan}"}))
            stems.append(stem)
            time.sleep(0.01)
        done = rowadd.rdir(cfg) / "done"
        wait(lambda: all((done / f"{s}.refused.json").exists() for s in stems), 30, what="the engine refuses both")
        self.assertIn("names no caller process", json.loads((done / f"{stems[0]}.refused.json").read_text())["why"])
        self.assertIn("does not match", json.loads((done / f"{stems[1]}.refused.json").read_text())["why"])
        self.assertNotIn("zq9", (INST / "queue_QG.tsv").read_text())
        # a request that is not a JSON object, or a directory, never wedges the phase
        bad = [f"x9.{int(time.time() * 1000)}"]
        (reqd / f"{bad[0]}.json").write_text("[]")
        (reqd / "zz-dir.json").mkdir()
        wait(lambda: (done / f"{bad[0]}.refused.json").exists() and not (reqd / "zz-dir.json").exists(), 30,
             what="malformed requests answered and moved")

    def test_29_revert_undoes_an_assign(self):
        """Drill (drop B item 6): the Owner undoes a Steward act with one command — `orch revert <k>` undoes
        an assign; a second revert of the line is refused; a session may not revert; a revert that is not one
        orch command is printed, never run; Telegram `revert <k>` runs only when the bot wrote the request."""
        import revert
        ctl("MOCKSW_rv1", 'assign rv1 mock2 "the gate waits; a harder tier for when it opens"\n'
                          'pause RV "nothing else in RV may start while rv1 waits"\n'
                          'note "rv1 waits for its owner gate"\n')
        ctl("MOCKSWV_rv1", "INBOX\nan owner gate: the Owner releases it\n")
        add_lane("RV", [("rv1", "-", 1, "mock", "")])
        orch("start", "RV", check=True)
        wait(lambda: phase("rv1") == "parked", 120, what="rv1 parked after the Steward's acts")
        from config_parser import load
        cfg = load(str(INST))
        acts = {a["action"]: k for k, a in revert.acts(cfg).items() if a.get("target", "").startswith(("rv1", "RV"))}
        self.assertTrue({"assign", "pause"} <= set(acts), acts)
        k_assign, k_pause = acts["assign"], acts["pause"]
        k_note = max(k for k, a in revert.acts(cfg).items() if a["action"] == "note")
        self.assertEqual((INST / "control" / "EXECUTOR_rv1").read_text().strip(), "mock2")
        self.assertTrue((INST / "control" / "PAUSE_RV").exists())
        # a session may not revert (v2 identity here; test_25's ancestry covers the pane case)
        p = orch("revert", str(k_assign), env=dict(ENV, ORCH_ROW="a1", ORCH_KIND="row"))
        self.assertIn("REFUSED", p.stdout)
        self.assertTrue((INST / "control" / "EXECUTOR_rv1").exists(), "a refused revert changes nothing")
        p = orch("revert", str(k_assign), "--dry-run")
        self.assertIn("would run: assign rv1 default", p.stdout)
        # the Owner reverts the assign
        p = orch("revert", str(k_assign))
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn(f"REVERTED #{k_assign}", p.stdout)
        self.assertFalse((INST / "control" / "EXECUTOR_rv1").exists(), "the assign is undone")
        self.assertTrue(any(e["event"] == "assign" and e.get("by") == f"owner (revert #{k_assign})" for e in events("rv1")))
        p = orch("revert", str(k_assign))
        self.assertIn("already reverted", p.stdout, "a line is reverted once")
        # a revert that is not one orch command is printed for the Owner, never run
        p = orch("revert", str(k_note))
        self.assertEqual(p.returncode, 3, p.stdout)
        self.assertIn("not run", p.stdout)
        self.assertIsNone(revert.reverted(cfg, k_note))
        # Telegram: a forged control/REVERT_<k>.json (no bot process) is refused and changes nothing
        cfg_set("watchdog.status_bot_allowed_user", "42")
        ctl(f"REVERT_{k_pause}.json", json.dumps({"k": k_pause, "req": "forged", "from_id": "42"}))
        wait(lambda: not (INST / "control" / f"REVERT_{k_pause}.json").exists(), 30,
             what="the engine's answer to a forged revert")
        ans = sorted((INST / "state" / "steward" / "reverts").glob(f"REVERT_{k_pause}.*.json"))
        self.assertTrue(ans, "the answer is filed under a name the engine chose, never the request's")
        self.assertEqual(json.loads(ans[-1].read_text())["outcome"], "refused")
        self.assertIn("names no bot process", json.loads(ans[-1].read_text())["why"])
        self.assertTrue((INST / "control" / "PAUSE_RV").exists(), "a forged revert never runs")
        self.assertFalse((INST / "control" / f"REVERT_{k_pause}.json").exists())
        # the bot (the real handler, in a process named like the bot) — the engine proves the writer and runs it
        fake = SBX / "fakebot"
        fake.mkdir(exist_ok=True)
        (fake / "telegram_bot.py").write_text(
            "import importlib.util, json, sys\n"
            f"spec = importlib.util.spec_from_file_location('tb', {str(HOME / 'lib' / 'telegram_bot.py')!r})\n"
            f"sys.path.insert(0, {str(HOME / 'lib')!r})\n"
            "tb = importlib.util.module_from_spec(spec); spec.loader.exec_module(tb)\n"
            "out = []\n"
            "tb.send_msg = lambda cfg, chat, text, reply_to=None: out.append(text)\n"
            "cfg = tb.load()\n"
            "tb.handle_revert(cfg, 1, {'message_id': 7, 'from': {'id': 42}}, int(sys.argv[2]))\n"
            "tb.handle_revert(cfg, 1, {'message_id': 8, 'from': {'id': 42}}, 99999)\n"
            f"open({str(fake / 'replies.json')!r}, 'w').write(json.dumps(out))\n")
        p = subprocess.run([sys.executable, str(fake / "telegram_bot.py"), "poll", str(k_pause)], capture_output=True,
                           text=True, env=ENV, timeout=120)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        replies = json.loads((fake / "replies.json").read_text())
        self.assertIn(f"REVERTED #{k_pause}", replies[0])
        self.assertIn("no line #99999", replies[1])
        self.assertFalse((INST / "control" / "PAUSE_RV").exists(), "Telegram revert resumed the lane")
        self.assertEqual(revert.reverted(cfg, k_pause).get("via"), "telegram")
        d = orch("digest")
        self.assertIn("Reverted by the Owner", d.stdout)
        self.assertIn(f"revert #{k_assign}", d.stdout, "a revert is a human touch")
        self.assertIn("REFUSED (a forged", d.stdout)
        orch("approve", "rv1", check=True)
        wait(lambda: phase("rv1") == "accepted", 90, what="rv1 after the Owner's approve")

    def test_30_revert_grammar_why_digest_and_bot_unit(self):
        """Units: what a revert line runs (closed set, never a shell), `orch why`, the digest's HIGH and queue
        sections, and the bot unit only with watchdog.answers (item 8b)."""
        import revert
        from config_parser import load
        cfg = load(str(INST))
        go = f"rm {(INST / 'control' / 'GO_a1').relative_to(cfg.root)}  (re-gates a1)"
        P = lambda t: revert.parse(cfg, t)[0]
        self.assertEqual(P(go), ["rm-go", "a1"])
        self.assertEqual(P("orch assign a1 default"), ["assign", "a1", "default"])
        self.assertEqual(P('orch park a1 "re-park"'), ["park", "a1", "re-park"])
        self.assertEqual(P("orch unpark a1"), ["unpark", "a1"])
        self.assertEqual(P("orch disable A"), ["disable", "A"])
        self.assertEqual(P("orch inbox done 3"), ["inbox-done", "3"])
        for bad in ("none — the re-run itself", "rm -r rounds/x", f"rm {INST}/orch.yaml", "rm /tmp/GO_a1",
                    "orch assign a1 default; rm -rf /", "orch assign a1 default && true", "orch reset a1",
                    "orch assign nope default", "orch assign a1 bogus", "orch start NOLANE", "$(id)",
                    "tmux kill-window -t x:y; git revert", "a new ruling reverting SW-1: x", ""):
            self.assertIsNone(P(bad), bad)
        # a revert must undo the act it is logged with; a k recorded twice never runs
        C = lambda **r: revert.check(cfg, dict({"k": 1, "session": "sw-1-1"}, **r))
        self.assertEqual(C(action="assign", target="a1 mock2", revert="orch assign a1 default")[0],
                         ["assign", "a1", "default"])
        self.assertEqual(C(action="pause", target="A", revert="orch resume A")[0], ["resume", "A"])
        self.assertIn("does not undo", C(action="assign", target="a1 mock2", revert="orch unpark a1")[1])
        self.assertIn("names row a1", C(action="assign", target="wy1 mock2", revert="orch assign a1 default")[1])
        self.assertIn("names lanes", C(action="pause", target="RV", revert="orch resume A")[1])
        self.assertIn("is not the one", C(action="inbox", target="t b", revert="orch inbox done 1")[1])
        self.assertIn("recorded 2 times", C(_dup=2)[1])
        logf = INST / "state" / "steward" / "log.jsonl"
        k1 = min(revert.acts(cfg))
        logf.write_text(logf.read_text() + json.dumps({"k": k1, "action": "unpark", "target": "a1",
                                                       "revert": "orch park a1"}) + "\n")
        self.assertTrue(revert.acts(cfg)[k1].get("_dup"), "a duplicated k is flagged")
        self.assertEqual(orch("revert", str(k1)).returncode, 3, "a duplicated k is printed, never run")
        lines = logf.read_text().splitlines()
        logf.write_text("\n".join(lines[:-1]) + "\n")
        self.assertEqual(orch("revert", "²").returncode, 2)
        # forged Telegram requests: naming a live bot-like process, a path in req, a directory, a sealed request
        # made by a process that is not the bot — none runs; a forged answer never reaches the Owner
        fb = SBX / "fakebot2"
        fb.mkdir(exist_ok=True)
        (fb / "telegram_bot.py").write_text("import time\ntime.sleep(120)\n")
        bot = subprocess.Popen([sys.executable, str(fb / "telegram_bot.py"), "daemon"])
        mine = revert.sealed(revert.REQ_MEMFD, {"k": k1, "from_id": "42", "instance": str(INST.resolve())})
        try:
            n_ok = len([r for r in revert.history(cfg) if r.get("ok")])
            refused = lambda: [e for e in all_events() if e.get("event") == "steward_revert_refused"]
            n_ref = len(refused())
            (INST / "control" / "REVERT_7.json").mkdir()
            ctl(f"REVERT_{k1}.json", json.dumps({"k": k1, "req": "../../zz.result", "from_id": "42", "phase": "accepted",
                                                  "pid": bot.pid, "fd": 1}))
            wait(lambda: not (INST / "control" / f"REVERT_{k1}.json").exists()
                 and not (INST / "control" / "REVERT_7.json").exists(), 30, what="forged requests consumed")
            self.assertFalse((INST / "state" / "zz.result.json").exists(), "a request never names a path")
            self.assertIn("not a orch-revert memfd", refused()[-1].get("detail", ""))
            ctl(f"REVERT_{k1}.json", json.dumps({"k": k1, "pid": os.getpid(), "fd": mine}))
            wait(lambda: not (INST / "control" / f"REVERT_{k1}.json").exists(), 30, what="second forged request")
            self.assertIn("is not the rig's telegram_bot.py", refused()[-1].get("detail", ""))
            self.assertEqual(len(refused()), n_ref + 2)
            self.assertEqual(len([r for r in revert.history(cfg) if r.get("ok")]), n_ok, "nothing ran")
            fake = INST / "state" / "steward" / "reverts" / "REVERT_5.123.json"
            ans_fd = revert.sealed(revert.ANS_MEMFD, {"req": "REVERT_5.123", "outcome": "done"})
            fake.write_text(json.dumps({"req": "REVERT_5.123", "outcome": "done", "pid": os.getpid(), "fd": ans_fd}))
            self.assertEqual(revert.wait_answer(cfg, fake, 1)["outcome"], "timeout", "only the engine answers")
            os.close(ans_fd)
        finally:
            bot.kill()
            os.close(mine)
        # orch why: lane not started, deps, an owner gate, parked rows, last events
        add_lane("WY", [("wy1", "-", 1, "mock", ""), ("wy2", "wy1", 0, "mock", "")])
        w2 = orch("why", "wy2").stdout
        self.assertIn("not started", w2)
        self.assertIn("lane WY is not started (orch start WY)", w2)
        self.assertIn("waiting on wy1", w2)
        w1 = orch("why", "wy1").stdout
        self.assertIn("orch approve wy1", w1)
        self.assertIn("only the Owner releases it", w1)
        self.assertIn("no such row", orch("why", "nope").stdout)
        a1 = orch("why", "a1").stdout
        self.assertIn("accepted", a1)
        self.assertIn("Last events:", a1)
        # digest item 8: HIGH (class P) and queue edits outside orch row add both render
        import digest
        from engine import Rig
        rig = Rig(cfg)
        text = digest.render(rig, 0)
        self.assertIn("HIGH — panel (class P)", text, "test_16's class P ruling is flagged HIGH")
        # item 8b: the bot unit only when the Owner answers by Telegram; an earlier install's unit is removed
        import service
        cfg.data["watchdog"].update(enabled=True, answers=False)
        self.assertNotIn(service.unit_name(cfg, "bot"), service.units(cfg))
        ud = SBX / "units8b"
        ud.mkdir(exist_ok=True)
        (ud / service.unit_name(cfg, "bot")).write_text("[Unit]\n")
        with mock.patch.object(service, "UNIT_DIR", ud), mock.patch.object(service, "_systemctl", lambda *a: (0, "")), \
                mock.patch.object(service, "linger_needed", lambda: False):
            service.install(cfg)
            self.assertFalse((ud / service.unit_name(cfg, "bot")).exists(), "a stale bot unit is removed")
            cfg.data["watchdog"]["answers"] = True
            self.assertIn(service.unit_name(cfg, "bot"), service.units(cfg))
            service.install(cfg)
            self.assertTrue((ud / service.unit_name(cfg, "bot")).exists())

    # -- drop C: the spend governor ----------------------------------------------------------------
    def _fake_bot(self, name: str, calls: str) -> list:
        """The real Telegram handlers in a process the engine can prove is the rig's bot (as in test_29)."""
        fake = SBX / f"fakebot-{name}"
        fake.mkdir(exist_ok=True)
        (fake / "telegram_bot.py").write_text(
            "import importlib.util, json, sys\n"
            f"spec = importlib.util.spec_from_file_location('tb', {str(HOME / 'lib' / 'telegram_bot.py')!r})\n"
            f"sys.path.insert(0, {str(HOME / 'lib')!r})\n"
            "tb = importlib.util.module_from_spec(spec); spec.loader.exec_module(tb)\n"
            "out = []\n"
            "tb.send_msg = lambda cfg, chat, text, reply_to=None: out.append(text)\n"
            "cfg = tb.load()\n" + calls +
            f"open({str(fake / 'replies.json')!r}, 'w').write(json.dumps(out))\n")
        p = subprocess.run([sys.executable, str(fake / "telegram_bot.py"), "poll"], capture_output=True, text=True,
                           env=ENV, timeout=150)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        return json.loads((fake / "replies.json").read_text())

    def test_31_spend_over_cap_one_page_spend_wait_then_A_resumes(self):
        """Drill (drop C items 1, 2, 4): an over-cap `orch spend run` runs nothing, the row waits in spend_wait
        (no unblocker, no Steward, no halt), exactly one page goes out for its request while the rest of the
        rig keeps running; the Owner's Telegram answer A raises the approved cap by exactly the shortfall and
        the row resumes and closes; a forged answer is refused."""
        import spend
        from config_parser import load
        cfg_set("spend.dir", str(SBX / "spend"))
        cfg_set("spend.enabled", True)
        cfg_set("watchdog.status_bot_allowed_user", "42")
        (INST / "SPEND_CAPS.yaml").write_text("daily_usd: 5\nper_row_default: 4\n")
        # only the Owner approves caps
        p = orch("spend", "caps", "approve", env=dict(ENV, ORCH_ROW="a1", ORCH_KIND="row"))
        self.assertIn("REFUSED", p.stdout)
        self.assertIsNone(spend.approved(load(str(INST)))[0], "a session's approve changes nothing")
        orch("spend", "caps", "approve", check=True)
        self.assertEqual(spend.approved(load(str(INST)))[0], {"daily_usd": 5.0, "per_row_default": 4.0})
        # USD 3 already spent today by the rig (an operator run, booked at its estimate)
        p = orch("spend", "run", "--provider", "testpaid", "--est", "3", "--", "true")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        run = ('"$ORCH_INSTANCE/orch" spend run --provider testpaid --est 3 --envelope E1 -- '
               'bash -c \'echo ORCH_SPEND_ACTUAL=2.75 >> "$ORCH_SPEND_OUT"; echo paid >> "$ORCH_INSTANCE/mockrun/m1.paid"\'\n')
        ctl("MOCK_RUN_m1", run)
        ctl("MOCK_DELAY_m2", "4")
        add_lane("SP", [("m1", "-", 0, "mock", ""), ("m2", "-", 0, "mock", ""), ("m3", "m1", 0, "mock", "")])
        orch("start", "SP", check=True)
        wait(lambda: phase("m1") == "spend_wait", 90, what="m1 in spend_wait")
        out = (INST / "mockrun" / "m1.out").read_text()
        rid = next(l.split()[1] for l in out.splitlines() if l.startswith("SPEND_WAIT "))
        self.assertFalse((INST / "mockrun" / "m1.paid").exists(), "an over-cap command never runs")
        cfg = load(str(INST))
        rq = spend.load_request(cfg, rid)
        self.assertEqual(rq["status"], "open")
        self.assertEqual([(c["cap"], c["need"]) for c in rq["crossed"]], [("daily_usd", 1.0)])
        wait(lambda: phase("m2") == "accepted", 90, what="m2 accepted while m1 waits for money")
        pages = lambda: [e for e in all_events() if e.get("event") == "spend_page" and e.get("req") == rid]
        wait(lambda: len(pages()) == 1, 30, what="the spend page")
        # the same need again (an operator re-run for m1) files no second request and no second page
        p = orch("spend", "run", "--row", "m1", "--provider", "testpaid", "--est", "3", "--envelope", "E1", "--", "true")
        self.assertEqual(p.returncode, 75, p.stdout + p.stderr)
        self.assertIn(f"SPEND_WAIT {rid}", p.stdout)
        time.sleep(4)
        self.assertEqual(len(pages()), 1, "exactly one page per request")
        self.assertEqual(phase("m1"), "spend_wait")
        self.assertNotIn(phase("m3"), ("accepted",), "its dependent waits")
        self.assertFalse(store().dispatch("m3").get("open"))
        evs = [e["event"] for e in events("m1")]
        for bad in ("halt", "retier"):
            self.assertNotIn(bad, evs)
        self.assertFalse(list((INST / "state").glob("ub-m1-*.dispatch.json")), "no unblocker for money")
        self.assertIsNone(incident_for("m1"), "no Steward incident for money")
        title, body = spend.page_text(cfg, rq)
        self.assertIn("A) raise daily_usd by USD 1.00 (recommended", body)
        self.assertIn("B) hold", body)
        import telegram_bot
        self.assertIn(f"orch spend answer {rid} A|B", telegram_bot.compose(cfg, title, body, f"spend:{rid}"),
                      "without a bot reading answers the page says how to answer on the host")
        cfg.data["watchdog"]["answers"] = True
        self.assertIn(f'"spend {rid} A"', telegram_bot.compose(cfg, title, body, f"spend:{rid}"))
        cfg.data["watchdog"]["answers"] = False
        self.assertIn("Spend request " + rid, orch("why", "m1").stdout)
        # a forged answer (no bot process) is refused and changes nothing
        ctl(f"SPEND_{rid}.json", json.dumps({"id": rid, "letter": "A", "from_id": "42"}))
        wait(lambda: not (INST / "control" / f"SPEND_{rid}.json").exists(), 30, what="the forged answer handled")
        self.assertEqual(spend.load_request(cfg, rid)["status"], "open")
        self.assertEqual(spend.approved(cfg)[0]["daily_usd"], 5.0)
        self.assertTrue(any(e.get("event") == "spend_answer_refused" for e in all_events()))
        # a session may not answer at the shell
        p = orch("spend", "answer", rid, "A", env=dict(ENV, ORCH_ROW="a1", ORCH_KIND="row"))
        self.assertIn("REFUSED", p.stdout)
        # the Owner replies A to the page (the real bot handler, a process the engine proves)
        with open(INST / "state" / "pages.jsonl", "a") as f:
            f.write(json.dumps({"message_id": 555, "row": f"spend:{rid}", "title": "t", "epoch": 1}) + "\n")
        replies = self._fake_bot("a",
            "import spend\n"
            f"rec = spend.load_request(cfg, {rid!r})\n"
            f"page = tb.compose(cfg, *spend.page_text(cfg, rec), 'spend:{rid}')\n"
            "tb.spend_reply(cfg, 1, {'message_id': 8, 'from': {'id': 42}, "
            "'reply_to_message': {'message_id': 555, 'text': 'A) raise daily_usd by USD 900.00'}}, 'A')\n"
            "tb.spend_reply(cfg, 1, {'message_id': 9, 'from': {'id': 42}, "
            "'reply_to_message': {'message_id': 555, 'text': page}}, 'A')\n")
        self.assertIn("not this request's current page", replies[0], "a reply to another text is refused")
        self.assertIn(f"spend {rid} APPROVED", replies[1])
        self.assertEqual(spend.approved(cfg)[0]["daily_usd"], 6.0, "raised by exactly the shortfall")
        ans = [json.loads(l) for l in (INST / "state" / "spend" / "answers.jsonl").read_text().splitlines()]
        self.assertEqual((ans[-1]["req"], ans[-1]["letter"], ans[-1]["ruling"], ans[-1]["via"]),
                         (rid, "A", f"SPEND-{rid}", "telegram"))
        wait(lambda: phase("m1") == "accepted" and phase("m3") == "accepted", 120, what="m1 resumed, then m3")
        self.assertTrue((INST / "mockrun" / "m1.paid").exists())
        self.assertIn("spend_resume", [e["event"] for e in events("m1")])
        led = [r for r in spend.lines(cfg) if r.get("row") == "m1"]
        self.assertEqual([(r["usd"], r["source"], r["envelope"]) for r in led], [(2.75, "metered", "E1")])
        self.assertEqual(len(pages()), 1)
        self.assertIn("spend " + rid + " A", " ".join(__import__("digest").human_touches(all_events())))

    def test_32_spend_caps_tamper_hold_and_units(self):
        """An unapproved caps edit is ignored (the approved copy stays in force) and opens an incident; B holds;
        the cap arithmetic, the per-row default, sessions charging another row, stale reservations, the off
        switch."""
        import spend
        from config_parser import load
        from engine import Rig
        if not spend.enabled(load(str(INST))):
            cfg_set("spend.dir", str(SBX / "spend"))
            cfg_set("spend.enabled", True)
        cfg = load(str(INST))
        before = spend.approved(cfg)[0]
        self.assertIsNotNone(before)
        (INST / "SPEND_CAPS.yaml").write_text("daily_usd: 100000\nper_row_default: 100000\n")
        wait(lambda: incident_for("rig:spend_caps", "spend_caps") is not None, 30, what="the tamper incident")
        self.assertEqual(spend.approved(cfg)[0], before, "an unapproved edit is ignored")
        self.assertIn("edited since it was approved", orch("spend", "caps").stdout)
        p = orch("spend", "run", "--provider", "testpaid", "--est", "500", "--", "true")
        self.assertEqual(p.returncode, 75, "the caps stay frozen at the approved copy")
        rid = p.stdout.split()[1]
        # B holds: the request stays, nothing is raised
        p = orch("spend", "answer", rid, "B")
        self.assertEqual(p.returncode, 0, p.stdout)
        self.assertEqual(spend.load_request(cfg, rid)["status"], "held")
        self.assertEqual(spend.approved(cfg)[0], before)
        self.assertEqual(orch("spend", "run", "--provider", "testpaid", "--est", "500", "--", "true").stdout.split()[1], rid,
                         "a held need is not filed again")
        # the approved copy edited behind orch: the recorded approval stays in force
        ap = INST / "state" / "spend_caps.approved.yaml"
        ap.write_text("daily_usd: 99999\n")
        self.assertEqual(spend.approved(cfg)[0], before)
        self.assertIn("spend_caps.approved.yaml changed outside orch", spend.tamper(cfg))
        # units in a private ledger
        u = SBX / "spend-units"
        with mock.patch.dict(os.environ, {"ORCH_SPEND_DIR": str(u)}):
            caps = {"daily_usd": 10.0, "per_row_default": 2.0, "per_provider": {"px": 1.5}}
            spend.record(cfg, "m2", "", "px", "", 1.0, "metered")
            cr = spend.crossing(cfg, caps, "m2", "px", "", 0.6)
            self.assertEqual(sorted((c["cap"], c["need"]) for c in cr), [("per_provider.px", 0.1)])
            cr = spend.crossing(cfg, caps, "m2", "py", "", 1.333)
            self.assertEqual([(c["cap"], c["need"], c.get("default")) for c in cr], [("per_row.m2", 0.34, True)])
            self.assertEqual(spend.crossing(cfg, caps, "m2", "py", "", 1.0), [], "exactly at the cap fits")
            # a reservation whose process died is booked at its estimate
            (u / "reservations").mkdir(parents=True, exist_ok=True)
            (u / "reservations" / "dead.json").write_text(json.dumps(
                {"instance": str(INST.resolve()), "row": "m2", "provider": "px", "usd": 0.4, "pid": 999999999}))
            with spend._Lock(cfg):
                self.assertEqual(spend.reservations(cfg), [])
            self.assertEqual([(r["usd"], r["source"]) for r in spend.lines(cfg)][-1], (0.4, "estimated"))
            self.assertFalse((u / "reservations" / "dead.json").exists())
            # validation
            self.assertTrue(spend.validate({"daily_usd": -1})[1])
            self.assertTrue(spend.validate({"weekly_usd": 3})[1])
            self.assertTrue(spend.validate({"per_provider": {"a b": 1}})[1])
            self.assertEqual(spend.validate({"per_phase": {"P2": "7"}})[0], {"per_phase": {"P2": 7.0}})
        # $HOME never moves the ledger; an estimate never goes below the floor; nothing approved = all closed
        with mock.patch.dict(os.environ, {"HOME": str(SBX / "fakehome"), "ORCH_SPEND_DIR": ""}):
            self.assertEqual(spend.spend_dir(cfg, make=False), SBX / "spend", "spend.dir decides, not $HOME")
            c2 = dict(cfg.data, spend=dict(cfg.data.get("spend") or {}, dir=""))
            import pwd
            self.assertEqual(spend.spend_dir(type(cfg)(cfg.instance, c2), make=False),
                             Path(pwd.getpwuid(os.getuid()).pw_dir) / ".local" / "state" / "orch")
        self.assertEqual(spend.est_floor(cfg, "px"), 0.01)
        with mock.patch.dict(os.environ, {"ORCH_SPEND_DIR": str(SBX / "spend-closed")}):
            self.assertTrue(spend.crossing(cfg, dict(spend.CLOSED), "m2", "px", "", 0.01), "closed caps refuse a cent")
        # a held request: the same row and provider wait on it whatever the estimate
        p = orch("spend", "run", "--row", "m2", "--provider", "py", "--est", "999", "--", "true")
        self.assertEqual(p.returncode, 75)
        rid2 = p.stdout.split()[1]
        orch("spend", "answer", rid2, "B", check=True)
        p = orch("spend", "run", "--row", "m2", "--provider", "py", "--est", "0.5", "--", "true")
        self.assertEqual((p.returncode, p.stdout.split()[1]), (75, rid2), "a smaller estimate never slips past a wait")
        # a per_row_default crossing raises per_row.<row>, not the default
        rig = Rig(cfg)
        rec, _ = spend.open_request(cfg, "m2", "", "pz", 1.0, "", "", [{"cap": "per_row.m2", "limit": 4.0, "need": 0.5,
                                                                        "default": True, "spent": 4.0, "reserved": 0}], "x")
        ok, msg = spend.answer(rig, rec["id"], "A", by="owner", via="test")
        self.assertTrue(ok, msg)
        caps = spend.approved(cfg)[0]
        self.assertEqual((caps["per_row"]["m2"], caps["per_row_default"]), (4.5, before["per_row_default"]))
        self.assertFalse(spend.answer(rig, rec["id"], "A", by="owner", via="test")[0], "approved once")
        # the approved headroom is held for that row: another caller counts it as running
        g = [x for x in spend.grants(cfg) if x["id"] == rec["id"]]
        self.assertEqual([(x["row"], x["provider"], x["usd"]) for x in g], [("m2", "pz", 1.0)])
        spend.consume_grant(cfg, rec["id"])
        self.assertFalse([x for x in spend.grants(cfg) if x["id"] == rec["id"]])
        # a session charges its own row only
        p = orch("spend", "run", "--row", "m2", "--provider", "px", "--est", "0", "--", "true",
                 env=dict(ENV, ORCH_ROW="a1", ORCH_KIND="row"))
        self.assertEqual(p.returncode, 2)
        self.assertIn("charges a1", p.stdout)
        # a claim naming another row's request is an ordinary BLOCKED (the unblocker path)
        self.assertIsNone(spend.wait_request(cfg, "a1", f"SPEND_WAIT {rec['id']}"))
        self.assertIsNone(spend.wait_request(cfg, "m2", "BLOCKED for another reason"))
        # off: the command runs and is booked, no cap
        cfg_set("spend.enabled", False)
        p = orch("spend", "run", "--provider", "testpaid", "--est", "500", "--", "true")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(spend.lines(load(str(INST)))[-1]["usd"], 500.0)
        d = orch("digest").stdout
        self.assertIn("**Spend:**", d)
        self.assertIn(f"{rid} (rig", d)

    # -- drop C items 3, 5 (vC2): the OpenCode meter, the spend hold, economy, reconcile, digest -------------
    @staticmethod
    def _oc_db(path: Path) -> None:
        """A fixture opencode.db holding only the columns the meter needs (and a secrets table it must never read)."""
        import sqlite3
        c = sqlite3.connect(str(path))
        c.execute("CREATE TABLE message (id text PRIMARY KEY, time_created integer NOT NULL, data text NOT NULL)")
        c.execute("CREATE TABLE credential (id text, secret text)")
        c.execute("INSERT INTO credential VALUES ('c1', 'CANARY-SECRET')")
        c.commit()
        c.close()

    _oc_n = 0

    def _oc_msg(self, path: Path, cost, t0: float, t1=None, answer: bool = True, mid: str = "") -> str:
        import sqlite3
        if not mid:
            V3._oc_n += 1
            mid = f"msg_{V3._oc_n:05d}"
        data = {"role": "assistant" if answer else "user", "summary": {"title": "CANARY-TEXT"},
                "time": {"created": int(t0 * 1000)}}
        if answer:
            data.update(cost=cost, modelID="cheap-model", providerID="upstream-x",
                        tokens={"input": 100, "output": 20, "reasoning": 0, "cache": {"read": 5, "write": 0}})
            if t1 is not None:
                data["time"]["completed"] = int(t1 * 1000)
        c = sqlite3.connect(str(path))
        c.execute("INSERT OR REPLACE INTO message VALUES (?, ?, ?)", (mid, int(t0 * 1000), json.dumps(data)))
        c.commit()
        c.close()
        return mid

    def test_33_opencode_meter_hold_one_page_then_A(self):
        """Drill (drop C items 3, 4, 6): the OpenCode meter on a fixture db books what the cheap tier spent —
        never history, never an unfinished message, a message to the one session that was open, else shared;
        crossing per_provider.opencode files ONE request and ONE page and opens the spend hold (an OpenCode row
        waits, a row on another executor runs); A raises the cap by the shortfall + headroom, the hold closes and
        the held row runs; 80 % of the raised cap is the economy mark; the digest shows the OpenCode share."""
        import spend
        import meter
        from config_parser import load
        db = SBX / "opencode.db"
        self._oc_db(db)
        now = time.time()
        self._oc_msg(db, 9.0, now - 3600, now - 3590)                      # history: never back-filled
        import yaml
        data = yaml.safe_load((INST / "orch.yaml").read_text())
        ex = data["executors"]
        ex["allowed"] = list(ex["allowed"]) + ["ocx"]
        ex["adapters"] = dict(ex.get("adapters") or {}, ocx="mock")
        (INST / "orch.yaml").write_text(yaml.safe_dump(data, sort_keys=False))
        (INST / "SPEND_CAPS.yaml").write_text("daily_usd: 100000\nmonthly_usd: 100000\nper_row_default: 100000\n"
                                              "per_provider:\n  opencode: 2.0\n")
        orch("spend", "caps", "approve", check=True)
        cfg_set("spend.dir", str(SBX / "spend"))
        cfg_set("spend.opencode", {"meter": True, "db": str(db), "every": 1, "settle_secs": 60,
                                   "executors": ["ocx"], "headroom_usd": 0.5})
        cfg_set("spend.enabled", True)
        cfg = load(str(INST))
        cur = lambda: spend._jread(meter.cursor_path(cfg)).get(str(db.resolve())) or {}
        wait(lambda: cur().get("since"), 30, what="the meter's cursor")
        oc = lambda: [r for r in spend.lines(cfg) if r.get("meter") == "opencode"]
        ctl("MOCK_DELAY_o1", "10")
        add_lane("OC", [("o1", "-", 0, "ocx", "")])
        orch("start", "OC", check=True)
        wait(lambda: store().dispatch("o1").get("open"), 60, what="o1 running on ocx")
        time.sleep(1.2)
        t = time.time()
        self._oc_msg(db, 0.75, t, t + 0.2)                                  # o1 is the only ocx session open
        self._oc_msg(db, None, t + 0.3, answer=False)                       # a prompt: no cost, never blocks
        run = self._oc_msg(db, 0.0, t + 0.4)                               # still running: the poll stops here
        self._oc_msg(db, 0.2, t + 0.5, t + 0.6)
        wait(lambda: len(oc()) == 1, 30, what="the o1 message booked")
        time.sleep(3)
        self.assertEqual(len(oc()), 1, "nothing after a message that is still running is booked yet")
        self._oc_msg(db, 0.3, t + 0.4, t + 0.9, mid=run)                   # it finishes with its final cost
        wait(lambda: len(oc()) == 3, 30, what="the finished message and the one after it")
        got = oc()
        self.assertEqual([(r["usd"], r["row"], r["attrib"], r["source"]) for r in got],
                         [(0.75, "o1", "session", "metered"), (0.3, "o1", "session", "metered"),
                          (0.2, "o1", "session", "metered")])
        self.assertEqual((got[0]["model"], got[0]["upstream"], got[0]["tokens"]["input"], got[0]["tokens"]["cache_read"]),
                         ("cheap-model", "upstream-x", 100, 5))
        self.assertNotIn(9.0, [r["usd"] for r in got], "history is never back-filled")
        led = (SBX / "spend" / "spend.jsonl").read_text()
        self.assertNotIn("CANARY", led, "no message text and no secret reaches the ledger")
        wait(lambda: phase("o1") == "accepted", 90, what="o1 accepted")
        wait(lambda: not store().dispatch("o1").get("open"), 30, what="o1 closed")
        time.sleep(1.5)
        t = time.time()
        self._oc_msg(db, 1.5, t, t + 0.1)                                   # no ocx session open: shared
        wait(lambda: len(oc()) == 4, 30, what="the shared message")
        self.assertEqual((oc()[-1]["row"], oc()[-1]["attrib"], oc()[-1]["rig"]), ("", "shared", cfg.name))
        # USD 2.75 metered against per_provider.opencode 2.00: one request, one page, the hold
        reqs = lambda: [r for r in spend.requests(cfg) if r.get("source") == "meter"]
        wait(lambda: len(reqs()) == 1, 30, what="the meter's request")
        rid = reqs()[0]["id"]
        pages = lambda: [e for e in all_events() if e.get("event") == "spend_page" and e.get("req") == rid]
        wait(lambda: len(pages()) == 1, 30, what="the meter's spend page")
        rq = spend.load_request(cfg, rid)
        self.assertEqual(([(c["cap"], c["need"]) for c in rq["crossed"]], rq["row"]), ([("per_provider.opencode", 1.25)], ""))
        title, body = spend.page_text(cfg, rq)
        self.assertIn("OpenCode meter", title)
        self.assertIn("A) raise per_provider.opencode by USD 1.25", body)
        self.assertIn("B) hold — OpenCode stays held until the cap's window resets (recommended", body)
        self.assertEqual(body.count("(recommended"), 1, "exactly one recommendation")
        wait(lambda: (spend._jread(spend.hold_path(cfg)).get("opencode") or {}).get("state") == "open", 30, what="the spend hold")
        ok, why = spend.hold_blocks(cfg, "ocx")
        self.assertTrue(ok and "spend hold" in why, why)
        self.assertFalse(spend.hold_blocks(cfg, "mock")[0], "the hold is the metered executors' only")
        # the rest of the rig keeps running; nothing new dispatches to OpenCode
        add_lane("OD", [("o2", "-", 0, "ocx", ""), ("o3", "-", 0, "mock", "")])
        orch("start", "OD", check=True)
        wait(lambda: phase("o3") == "accepted", 90, what="o3 (mock) accepted under the hold")
        self.assertFalse(store().dispatch("o2"), "no OpenCode dispatch under the spend hold")
        self.assertIn("spend hold", orch("why", "o2").stdout)
        t = time.time()
        self._oc_msg(db, 0.1, t, t + 0.1)                                   # more metered spend: same request
        wait(lambda: len(oc()) == 5, 30, what="a later message")
        time.sleep(3)
        self.assertEqual([r["id"] for r in reqs()], [rid], "one request per crossing, whatever the amount")
        self.assertEqual(len(pages()), 1, "exactly one page")
        self.assertIsNone(incident_for("o2"), "money is no Steward incident")
        self.assertIn("OpenCode spend hold OPEN", orch("spend", "status").stdout)
        # the Owner answers A: the cap rises by exactly the shortfall + headroom, the hold closes, o2 runs
        p = orch("spend", "answer", rid, "A")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(spend.approved(cfg)[0]["per_provider"]["opencode"], 3.25)
        wait(lambda: phase("o2") == "accepted", 120, what="o2 dispatched once the hold closed")
        self.assertEqual((spend._jread(spend.hold_path(cfg)).get("opencode") or {}).get("state"), "closed")
        self.assertFalse([g for g in spend.grants(cfg) if g["id"] == rid], "a meter's raise is no grant")
        hev = [e["event"] for e in all_events() if e.get("event", "").startswith("spend_hold")]
        self.assertEqual(hev[-2:], ["spend_hold_open", "spend_hold_close"])
        # USD 2.85 of 3.25 (88 %) is past the 80 % economy mark
        wait(lambda: (INST / "state" / "spend" / "economy").is_file(), 90, what="the economy mark")
        self.assertIn("per_provider.opencode", (INST / "state" / "spend" / "economy").read_text())
        self.assertTrue(any(e.get("event") == "spend_economy" and e.get("cap") == "per_provider.opencode"
                            for e in all_events()))
        d = orch("digest").stdout
        self.assertIn("**OpenCode share:** USD 2.85 metered", d)
        self.assertIn("to rows USD 1.25, shared USD 1.60", d)
        self.assertIn("per_provider.opencode: USD 2.85 of USD 3.25 this month (88 %) — **economy**", d)
        self.assertIn("economy", (HOME / "templates" / "_TEMPLATE_STEWARD.md").read_text())

    def test_34_meter_read_surface_attribution_reconcile_lapse(self):
        """The meter can read nothing but message.id/time_created/data through json_extract; attribution rules;
        billing reconcile opens a spend_reconcile incident on a gap and books nothing; a crossing lapses when a
        higher approval covers it; governor off clears the hold while the meter keeps booking."""
        import sqlite3
        import spend
        import meter
        from config_parser import load
        from engine import Rig
        db = SBX / "opencode.db"
        if not db.exists():
            self._oc_db(db)
        c = meter.connect(db)
        for q in ("SELECT * FROM credential", "SELECT secret FROM credential", "SELECT lower(id) FROM message",
                  "SELECT name FROM sqlite_master"):
            with self.assertRaises(sqlite3.DatabaseError, msg=q):
                c.execute(q).fetchall()
        with self.assertRaises(sqlite3.DatabaseError):
            c.execute("INSERT INTO message VALUES ('x', 1, '{}')")
        c.close()
        rows = meter.read(db, (0, ""))
        self.assertTrue(rows)
        self.assertEqual(set(rows[0]), {"id", "tc", "answer", "cost", "tokens", "model", "upstream", "created", "completed"})
        self.assertNotIn("CANARY", json.dumps(rows))
        # attribution: exactly one overlapping session -> its row; several -> shared (one rig: that rig)
        ivs = [{"instance": "/i/a", "rig": "a", "sid": "x1", "row": "x1", "start": 100, "end": 200},
               {"instance": "/i/a", "rig": "a", "sid": "ub-x2-1", "row": "x2", "start": 150, "end": None},
               {"instance": "/i/b", "rig": "b", "sid": "y1", "row": "y1", "start": 300, "end": 400}]
        self.assertEqual(meter.attribute(ivs, 110, 120, now=1000)["row"], "x1")
        a = meter.attribute(ivs, 160, 170, now=1000)
        self.assertEqual((a["row"], a["rig"], a["attrib"], a["n"]), ("", "a", "shared", 2))
        a = meter.attribute(ivs, 310, 320, now=1000)
        self.assertEqual((a["instance"], a["row"], a["n"]), ("", "", 2), "x2's open unblocker overlaps too: two rigs")
        self.assertEqual(meter.attribute(ivs[:1] + ivs[2:], 310, 320, now=1000)["row"], "y1")
        self.assertEqual(meter.attribute(ivs[:1], 500, 510, now=1000)["instance"], "", "none open: the polling rig's")
        self.assertEqual((meter.parent_row("ub-m1-2"), meter.parent_row("rv-m1-1"), meter.parent_row("m1"),
                          meter.parent_row("sw-3-1")), ("m1", "m1", "m1", ""))
        cfg = load(str(INST))
        cfg.data["spend"]["opencode"]["executors"] = []
        self.assertTrue(meter.metered_executor(cfg, "opencode") and not meter.metered_executor(cfg, "mock"),
                        "without a list the meter's executors are its adapter family")
        # a per-row hold holds that row only
        h = spend.hold_path(cfg)
        keep = h.read_text() if h.exists() else None
        h.write_text(json.dumps({"opencode": {"state": "open", "kind": "spend", "rig": [], "rows": ["zz"], "reason": "t"}}))
        cfg = load(str(INST))
        self.assertTrue(spend.hold_blocks(cfg, "ocx", "zz")[0])
        self.assertFalse(spend.hold_blocks(cfg, "ocx", "o9")[0])
        self.assertFalse(spend.hold_blocks(cfg, "ocx")[0])
        # a rig-wide hold also stops sub-sessions (planner, panel, preflight, review, unblocker) on the executor
        h.write_text(json.dumps({"opencode": {"state": "open", "kind": "spend", "rig": ["sp0"], "rows": [], "reason": "t"}}))
        from engine import Engine
        ok, why = Engine(load(str(INST))).dispatch_sub("pl-hold-1", "ocx", INST / "nowhere.md", "SW", "", {})
        self.assertFalse(ok)
        self.assertIn("spend hold", why)
        self.assertFalse(store().dispatch("pl-hold-1"), "nothing was opened")
        if keep is None:
            h.unlink()
        else:
            h.write_text(keep)
        # billing reconcile: a gap beyond the tolerance opens one incident and books nothing; a match is ok
        n0 = len(spend.lines(cfg, all_rigs=True))
        cfg_set("spend.reconcile", [{"provider": "opencode", "cmd": "echo SPEND opencode 50.00", "every": 3600},
                                    {"provider": "nothingpaid", "cmd": "echo SPEND nothingpaid 0.40", "every": 3600},
                                    {"provider": "brokenpoll", "cmd": "exit 3", "every": 3600}])
        wait(lambda: incident_for("rig:spend_reconcile:opencode", "spend_reconcile") is not None, 30,
             what="the reconcile incident")
        inc = incident_for("rig:spend_reconcile:opencode", "spend_reconcile")
        self.assertIn("billing says USD 50.00", inc["summary"])
        recf = INST / "state" / "spend" / "reconcile.jsonl"
        wait(lambda: len(recf.read_text().splitlines()) >= 3, 30, what="three reconcile records")
        rec = {r["provider"]: r for r in map(json.loads, recf.read_text().splitlines())}
        self.assertEqual((rec["nothingpaid"]["ok"], rec["nothingpaid"]["gap"]), (True, 0.4))
        self.assertFalse(rec["brokenpoll"]["ok"])
        self.assertIsNone(incident_for("rig:spend_reconcile:nothingpaid"), "inside the tolerance: no incident")
        self.assertEqual(len(spend.lines(cfg, all_rigs=True)), n0, "reconcile books nothing")
        self.assertIn("**Billing reconcile:**", orch("digest").stdout)
        cfg_set("spend.reconcile", [])
        # a crossing lapses when a higher approval covers it: the hold closes without an answer
        (INST / "SPEND_CAPS.yaml").write_text("daily_usd: 100000\nmonthly_usd: 100000\nper_row_default: 100000\n"
                                              "per_provider:\n  opencode: 1.0\n")
        orch("spend", "caps", "approve", check=True)
        cfg = load(str(INST))
        meters = lambda st: [r for r in spend.requests(cfg, st) if r.get("source") == "meter"]
        wait(lambda: meters(("open", "held")), 30, what="a crossing under the lower cap")
        rid = meters(("open", "held"))[0]["id"]
        wait(lambda: spend.hold_blocks(load(str(INST)), "ocx")[0], 30, what="the hold")
        (INST / "SPEND_CAPS.yaml").write_text("daily_usd: 100000\nmonthly_usd: 100000\nper_row_default: 100000\n"
                                              "per_provider:\n  opencode: 1000\n")
        orch("spend", "caps", "approve", check=True)
        wait(lambda: spend.load_request(cfg, rid)["status"] == "lapsed", 30, what="the request lapsed")
        ok, why = spend.answer(Rig(cfg), rid, "A", by="owner", via="test")
        self.assertFalse(ok, "a lapsed request raises nothing")
        self.assertIn("lapsed", why)
        wait(lambda: not spend.hold_blocks(load(str(INST)), "ocx")[0], 30, what="the hold closed")
        self.assertFalse((INST / "state" / "spend" / "economy").exists(), "no cap near its mark: no economy")
        # governor off: no hold; the meter still books
        cfg_set("spend.enabled", False)
        t = time.time()
        ocl = lambda: [r for r in spend.lines(cfg) if r.get("meter") == "opencode"]
        n = len(ocl())
        self._oc_msg(db, 0.05, t, t + 0.1)
        wait(lambda: len(ocl()) == n + 1, 30, what="the meter books with the governor off")
        # a long step (unfinished, older than settle_secs) is passed, kept pending and booked when it finishes
        import sqlite3
        long_id = "msg_long"
        time.sleep(2)                     # its time_created (now - 4 s) must lie after the cursor (>= 3 s old)
        c = sqlite3.connect(str(db))
        c.execute("INSERT INTO message VALUES (?, ?, ?)", (long_id, int((time.time() - 4) * 1000), json.dumps(
            {"cost": 0.0, "tokens": {"input": 1}, "time": {"created": int((time.time() - 120) * 1000)}})))
        c.commit()
        self._oc_msg(db, 0.07, time.time() - 3.5, time.time() - 3.4)          # after it: not blocked by it
        wait(lambda: len(ocl()) == n + 2, 30, what="the message after the long step")
        cur = spend._jread(meter.cursor_path(cfg)).get(str(db.resolve())) or {}
        self.assertIn(long_id, cur.get("pending") or {})
        c.execute("UPDATE message SET data = ? WHERE id = ?", (json.dumps(
            {"cost": 0.4, "tokens": {"input": 1}, "time": {"created": int((time.time() - 120) * 1000),
                                                         "completed": int(time.time() * 1000)}}), long_id))
        c.commit()
        c.close()
        wait(lambda: len(ocl()) == n + 3, 30, what="the long step booked when it finished")
        self.assertEqual(ocl()[-1]["usd"], 0.4)
        self.assertNotIn(long_id, (spend._jread(meter.cursor_path(cfg)).get(str(db.resolve())) or {}).get("pending") or {})
        self.assertFalse(spend.hold_blocks(load(str(INST)), "ocx")[0])
        cfg_set("spend.opencode.meter", False)

    # -- drop D item 1: the phase fence ------------------------------------------------------------
    def _phases_off(self) -> None:
        for f in (INST / "phases.yaml", INST / "state" / "phases.json", INST / "state" / "protected_writes.json",
                  INST / "state" / "phases.on"):
            f.unlink(missing_ok=True)

    def _sw_done(self, sid: str) -> list:
        out = []
        for f in sorted((INST / "state" / "steward" / "done").glob(f"{sid}.*.json")):
            r = json.loads(f.read_text())
            out.append((r.get("action"), r.get("args"), r.get("outcome"), r.get("why", "")))
        return out

    def test_34b_approved_and_dispatched_gate_opens_no_incident(self):
        """Live bug (a real incident): schedule() skipped a row that was live and so never cleared its old
        'gated' note; the stale note outlived the Owner's approval and the dispatch, and after steward.gate_after the
        Steward lane opened a `gate` incident for a row that was RUNNING. A row released and dispatched since its
        note was written has no gate incident, and its stale note is gone from the scheduler's list."""
        # sorts before test_35: from there on the sandbox has a phase fence and a new lane is held by it, not gated
        cfg_set("steward.gate_after", 6)
        ctl("MOCK_DELAY_sg1", "30")
        add_lane("SG", [("sg1", "-", 1, "mock", "")])
        orch("start", "SG", check=True)
        sched = lambda: json.loads((INST / "state" / "sched.json").read_text())
        wait(lambda: sched().get("sg1", "").startswith("gated"), 30, what="sg1 at its gate (the note is written)")
        orch("approve", "sg1", check=True)
        wait(lambda: store().dispatch("sg1").get("open"), 30, what="sg1 dispatched after the approval")
        time.sleep(6 + 6)                                   # past gate_after, with room for the scan ticks
        self.assertTrue(store().dispatch("sg1").get("open"), "sg1 must still be running (else the drill proves nothing)")
        self.assertIsNone(incident_for("sg1", "gate"), "no gate incident for a row released and dispatched")
        self.assertFalse(sched().get("sg1", "").startswith("gated"), "the stale gate note is cleared once the row ran")
        # the Steward-side guard alone: a stale note handed to scan() for a running / released row opens nothing
        import steward
        from config_parser import load
        from engine import Rig
        rig = Rig(load(str(INST)))
        waits = INST / "state" / "steward" / "waits.json"
        waits.write_text(json.dumps({"gate:sg1": time.time() - 3600}))
        steward.scan(rig, {"sg1": "gated — release with: orch approve sg1"})
        self.assertIsNone(incident_for("sg1", "gate"), "scan() ignores a gate note for a row with an open session")
        wait(lambda: phase("sg1") == "accepted", 90, what="sg1 accepted")
        cfg_set("steward.gate_after", 3)

    def test_35_phase_fence_steward_refused_rows_cap_rests(self):
        """Drop D item 1 (the phase rule): a Steward round-new outside an open phase is refused; a phase at its
        rows cap goes to rest — running rows finish, nothing new dispatches — and only the Owner re-opens it."""
        import phases
        import steward
        from config_parser import load
        from engine import Engine
        self.addCleanup(self._phases_off)
        cfg = load(str(INST))
        self.assertFalse(phases.active(cfg), "a rig with no phases.yaml has no fence")
        self.assertIn("no fence", orch("phase", "status").stdout)
        row_env = dict(ENV, ORCH_ROW="a1", ORCH_KIND="row")
        p = orch("phase", "open", "F1", "--caps", "rows=2", "a row tries", env=row_env)
        self.assertIn("REFUSED", p.stdout)
        self.assertFalse(phases.active(cfg), "a session opens no phase")
        p = orch("phase", "open", "F1", "--caps", "rows=2,loops=3", "drill phase", check=True)
        self.assertIn("-> open", p.stdout)
        self.assertEqual(phases.load(cfg)["F1"]["state"], "open")
        for rid in ("fa1", "fa2"):
            ctl(f"MOCK_DELAY_{rid}", "6")            # still running when the phase rests
        add_lane("FA", [("fa1", "-", 0, "mock", ""), ("fa2", "-", 0, "mock", ""), ("fa3", "-", 0, "mock", "")])
        # `orch row add` refuses a row past the rows cap (the phase already holds 3 rows, cap 2) — and outside any phase
        p = orch("row", "add", "FA", "fa4\t-\t0\tmock\tPHASE_FA/fa3/PLAN.md", "one more")
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("belongs to no round", p.stdout + p.stderr, "lane FA is in no round yet: the fence refuses")
        e = Engine(load(str(INST)))
        inc = steward.open_incident(e, "progress", "round:fa", "cut round fa inside phase F1", klass="judgment")
        sid = f"sw-{inc['n']}-1"
        ctl(f"MOCK_SW_{sid}", 'round-new fz FZ "no phase named"\n'
                              'round-new fz --phase F9 FZ "a phase the Owner never opened"\n'
                              'round-new fa --phase F1 FA "inside the open phase"\n')
        wait(lambda: len(self._sw_done(sid)) == 3, 120, what="the Steward's three round requests answered")
        done = self._sw_done(sid)
        self.assertEqual([d[2] for d in done], ["refused", "refused", "done"], done)
        self.assertIn("must name its phase", done[0][3])
        self.assertIn("no phase F9", done[1][3])
        self.assertFalse((INST / "rounds" / "fz").exists(), "no round outside an open phase")
        self.assertNotIn("F9", phases.load(cfg), "nothing opens a phase")
        self.assertEqual(phases.load(cfg)["F1"]["rounds"], ["fa"], "the new round is recorded in its phase")
        p = orch("row", "add", "FA", "fa4\t-\t0\tmock\tPHASE_FA/fa3/PLAN.md", "one more")
        self.assertIn("rows cap is 2", p.stdout + p.stderr)
        self.assertFalse(any(r.id == "fa4" for r in __import__("rows").load_rows(cfg)[0]))
        orch("round", "start", "fa", check=True)
        # three ready rows, cap 2: exactly two start in the pass (the cap holds within it) -> rest; they finish (their close checks pass although the engine rewrote
        # the protected phases.yaml under them); fa3 never starts
        wait(lambda: any(x["event"] == "phase_rest" for x in events("phases")), 90, what="phase F1 rests at its rows cap")
        self.assertEqual(phases.load(cfg)["F1"]["state"], "rest")
        self.assertIn("rows cap", phases.load(cfg)["F1"]["rest_reason"])
        wait(lambda: phase("fa1") == "accepted" and phase("fa2") == "accepted", 90, what="running rows finish")
        time.sleep(4)
        self.assertEqual(phase("fa3"), "", "nothing new dispatches in a resting phase")
        self.assertFalse(store().dispatch("fa3"))
        notes = json.loads((INST / "state" / "sched.json").read_text())
        self.assertTrue(notes.get("fa3", "").startswith("PHASE"), notes.get("fa3"))
        self.assertIn("the phase fence: phase F1 is rest", orch("why", "fa3").stdout)
        rig = Engine(load(str(INST)))
        rig.inflight = lambda: {}
        from rows import find_row
        self.assertEqual(steward.rest_state(rig, [find_row(cfg, "fa3")], notes), "",
                         "a resting phase is the Owner's: never 'work left' for the Steward's rest detector")
        # inside a resting phase a Steward starts nothing and cuts no round
        d = {"incident": inc["n"]}
        ok, why, _ = steward.execute(rig, sid, "round-new", ["fb", "--phase", "F1", "FB"], "x", d)
        self.assertFalse(ok)
        self.assertIn("rest", why)
        ok, why, _ = steward.execute(rig, sid, "start", ["FA"], "x", d)
        self.assertFalse(ok)
        self.assertIn("phase F1 is rest", why)
        ok, why, _ = steward.execute(rig, sid, "round-start", ["fa"], "x", d)
        self.assertFalse(ok)
        self.assertIn("rest", why)
        self.assertIn("rest", "\n".join(phases.digest_lines(cfg, all_events())))
        # the Owner re-opens: without a higher cap it would rest again at once (refused); with one fa3 runs
        p = orch("phase", "open", "F1")
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("rest again", p.stdout)
        orch("phase", "open", "F1", "--caps", "rows=3", "one more row", check=True)
        wait(lambda: phase("fa3") == "accepted", 90, what="fa3 after the Owner re-opened F1")

    def test_36_phase_fence_units_tamper_and_neutral_default(self):
        """The fence's rules on their own: neutral without phases, rest vs closed, days × 1.5, rulings, loops,
        an edit outside `orch phase` ignored and reported, `orch phase approve`, the operator-only commands."""
        import copy
        import phases
        import protected
        import spend
        from config_parser import load
        from engine import Engine
        self.addCleanup(self._phases_off)
        self._phases_off()
        cfg = load(str(INST))
        # no phases: no fence (v2), and --phase is refused rather than silently ignored
        self.assertEqual(phases.fence(cfg, ["A", "NOPE"]), "")
        self.assertEqual(phases.Fence(cfg).block("NOPE", ""), "")
        self.assertEqual(phases.row_cap(cfg, "A"), "")
        self.assertIn("drop --phase", phases.fence(cfg, [], new_round=True, phase="P1"))
        self.assertEqual(spend.phase_of(cfg, "fa1"), "")
        # shape
        _, probs = phases.validate({"phases": {"P1": {"state": "ajar"}, "P2": {"state": "open", "caps": {"rows": -1}},
                                               "P3": {"state": "open", "rounds": ["x"]}, "P4": {"state": "rest", "rounds": ["x"]}}})
        self.assertTrue(any("ajar" in x for x in probs) and any("rows" in x for x in probs)
                        and any("two phases" in x for x in probs), probs)
        # the Owner's phases: G1 open (round fa), G2 rest (round gr), G3 closed (round gc)
        orch("phase", "open", "G1", "--rounds", "fa", "--caps", "days=1", check=True)
        for pid, tag in (("G2", "gr"), ("G3", "gc")):
            orch("phase", "open", pid, "--rounds", tag, check=True)
        orch("phase", "rest", "G2", "pause it", check=True)
        orch("phase", "close", "G3", "done", check=True)
        for tag, lane in (("gr", "GR"), ("gc", "GC")):
            (INST / "rounds" / tag).mkdir(parents=True, exist_ok=True)
            (INST / "rounds" / tag / "round.yaml").write_text(f"tag: {tag}\nstatus: open\nlanes: [{lane}]\n")
        cfg = load(str(INST))
        f = phases.Fence(cfg)
        self.assertEqual(f.block("FA", ""), "")
        self.assertEqual(f.block("GR", "retry"), "", "rest: a running row finishes")
        self.assertIn("phase G2 is rest", f.block("GR", ""))
        self.assertIn("phase G3 is closed", f.block("GC", "retry"), "closed: nothing dispatches")
        self.assertIn("belongs to no round", f.block("A", ""), "fail closed: a lane outside every round")
        self.assertEqual(spend.phase_of(cfg, "fa1"), "G1", "spend's per-phase caps read the trusted record")
        # round new / start through the CLI
        p = orch("round", "new", "gz", "--lanes", "GZ")
        self.assertIn("must name its phase", p.stdout)
        p = orch("round", "new", "gz", "--lanes", "GZ", "--phase", "G3")
        self.assertIn("closed", p.stdout)
        self.assertFalse((INST / "rounds" / "gz").exists())
        p = orch("round", "start", "gr", "--force")
        self.assertIn("phase G2 is rest", p.stdout, "--force never crosses the phase fence")
        # days × 1.5 from the latest open; rulings since it; loops
        g1 = phases.load(cfg)["G1"]
        self.assertEqual(phases.cap_reached(cfg, "G1", dict(g1, opened_epoch=time.time() - 1.4 * 86400)), "")
        self.assertIn("days cap", phases.cap_reached(cfg, "G1", dict(g1, opened_epoch=time.time() - 1.6 * 86400)))
        c2 = copy.copy(cfg)
        c2.state = SBX / "rulings_state"
        (c2.state / "steward").mkdir(parents=True, exist_ok=True)
        (c2.state / "steward" / "rulings.jsonl").write_text(
            "".join(json.dumps({"id": f"SW-{k}", "epoch": time.time()}) + "\n" for k in range(2))
            + json.dumps({"id": "SW-0", "epoch": time.time() - 9 * 86400}) + "\n")
        self.assertIn("rulings cap (2/2", phases.cap_reached(c2, "G1", {"caps": {"rulings": 2}, "opened_epoch": time.time() - 60}))
        self.assertEqual(phases.cap_reached(c2, "G1", {"caps": {"rulings": 3}, "opened_epoch": time.time() - 60}), "")
        orch("phase", "open", "G1", "--caps", "loops=1", check=True)
        self.assertEqual(phases.record_loop(cfg, "G1"), 1)
        # vD2: caps.loops counts loops ALLOWED — the loop that brings the count to the cap still runs
        self.assertEqual(phases.cap_reached(cfg, "G1", phases.load(cfg)["G1"]), "")
        self.assertEqual(phases.record_loop(cfg, "G1"), 2)
        self.assertIn("loops cap (2/1", phases.cap_reached(cfg, "G1", phases.load(cfg)["G1"]))
        p = orch("round", "new", "gy", "--lanes", "GY", "--phase", "G1")
        self.assertIn("loops cap", p.stdout, "a phase at a cap takes no new round (or the engine already rested it)")
        self.assertFalse((INST / "rounds" / "gy").exists())
        orch("phase", "open", "G1", "--caps", "loops=5", check=True)
        # an edit outside `orch phase` (a session opening G3) is ignored and reported; deleting the file keeps the fence
        y = INST / "phases.yaml"
        rel = str(y.resolve().relative_to(cfg.git_repo.resolve()))
        self.assertTrue(protected.orch_wrote(cfg, rel))
        y.write_text(y.read_text().replace("state: closed", "state: open"))
        self.assertFalse(protected.orch_wrote(cfg, rel), "a hand edit is charged")
        self.assertEqual(phases.load(cfg)["G3"]["state"], "closed", "the record stays in force")
        self.assertIn("edited outside", phases.tamper(cfg))
        rig = Engine(cfg)
        phases.tick(rig)
        phases.tick(rig)
        self.assertEqual(sum(1 for x in events("phases") if x["event"] == "phases_tamper"), 1, "once per content")
        row_env = dict(ENV, ORCH_ROW="a1", ORCH_KIND="row")
        for args in (("approve",), ("rest", "G1"), ("close", "G1"), ("open", "G3")):
            p = orch("phase", *args, env=row_env)
            self.assertIn("REFUSED", p.stdout, args)
        self.assertEqual(phases.load(cfg)["G3"]["state"], "closed")
        orch("phase", "approve", check=True)          # the Owner adopts the edit
        self.assertEqual(phases.load(cfg)["G3"]["state"], "open")
        self.assertEqual(phases.tamper(cfg), "")
        y.unlink()
        self.assertTrue(phases.active(cfg), "the record keeps the fence when phases.yaml is deleted")
        self.assertIn("removed", phases.tamper(cfg))
        self.assertIn("G1", orch("phase", "status").stdout)

    def test_37_phase_fence_adversarial_fixes(self):
        """The fence's holes the adversarial pass found, closed: a lane listed by rounds of two phases, a borrowed
        tag, a malformed round.yaml, a fence that cannot be built, a round-new into a tag of another phase (the
        folder survives), both files deleted (the marker keeps the fence; `orch phase off` is the Owner's), `orch
        dispatch` and the Steward's `plan` outside an open phase, the engine never resting a phase the Owner
        closed, `approve` shows what it changes, the protected-write registry honours only phases.yaml."""
        import phases
        import protected
        import steward
        from config_parser import load
        from engine import Engine
        self.addCleanup(self._phases_off)
        self.addCleanup(lambda: (INST / "state" / "phases.on").unlink(missing_ok=True))
        self._phases_off()
        cfg = load(str(INST))
        for tag, lanes in (("hc", ["HC"]), ("c1", ["CL"]), ("c2", ["CL", "CL0"]), ("hb", ["HB"])):
            (INST / "rounds" / tag).mkdir(parents=True, exist_ok=True)
            (INST / "rounds" / tag / "round.yaml").write_text(f"tag: {tag}\nstatus: open\nlanes: [{', '.join(lanes)}]\n")
        (INST / "rounds" / "zborrow").mkdir(parents=True, exist_ok=True)
        (INST / "rounds" / "zborrow" / "round.yaml").write_text("tag: c1\nstatus: open\nlanes: [HB]\n")
        (INST / "rounds" / "zbad").mkdir(parents=True, exist_ok=True)
        (INST / "rounds" / "zbad" / "round.yaml").write_text("tag: zbad\nlanes: 5\n")
        for tag in ("zborrow", "zbad"):
            self.addCleanup(shutil.rmtree, INST / "rounds" / tag, True)
        orch("phase", "open", "H1", "--rounds", "c1,r5", check=True)
        orch("phase", "open", "H0", "--rounds", "c2,hc,hb", check=True)
        orch("phase", "close", "H0", check=True)
        f = phases.Fence(load(str(INST)))
        self.assertIn("different phases", f.block("CL", ""), "a lane two phases' rounds list waits")
        self.assertIn("different phases", f.block("HB", ""), "a round.yaml borrowing c1's tag cannot open lane HB")
        self.assertEqual(f.lane_rounds.get("5"), None, "a malformed round.yaml maps no lane (and does not crash)")
        self.assertIn("could not be read", phases.Fence.failed(cfg, "boom").block("A", "retry"))
        # a new round may not take a tag or a lane of another phase; a refused one deletes nothing it did not write
        (INST / "rounds" / "r5").mkdir(parents=True, exist_ok=True)
        (INST / "rounds" / "r5" / "NOTES.md").write_text("a planner's notes\n")
        self.addCleanup(shutil.rmtree, INST / "rounds" / "r5", True)
        orch("phase", "open", "H2", check=True)
        p = orch("round", "new", "r5", "--lanes", "L5", "--phase", "H2")
        self.assertIn("already in phase H1", p.stdout)
        self.assertTrue((INST / "rounds" / "r5" / "NOTES.md").exists())
        self.assertFalse((INST / "rounds" / "r5" / "round.yaml").exists())
        p = orch("round", "new", "c3", "--lanes", "CL0", "--phase", "H1")
        self.assertIn("lane CL0 is already listed by a round in H0", p.stdout)
        # `orch dispatch` (FORCE=1 too) and the Steward's `plan` stay inside an open phase
        add_lane("HC", [("hc1", "-", 0, "mock", "")])
        self.addCleanup((INST / "queue_HC.tsv").unlink, missing_ok=True)
        p = orch("dispatch", "hc1", env=dict(ENV, FORCE="1"))
        self.assertIn("phase H0 is closed", p.stdout + p.stderr)
        self.assertFalse(store().dispatch("hc1"))
        rig = Engine(load(str(INST)))
        ok, why, _ = steward.execute(rig, "sw-1-1", "plan", ["c2"], "x", {"incident": 1})
        self.assertFalse(ok)
        self.assertIn("phase H0 is closed", why)
        orch("phase", "rest", "H1", check=True)
        orch("phase", "rest", "H2", check=True)
        ok, why, _ = steward.execute(rig, "sw-1-1", "plan", ["brandnew"], "x", {"incident": 1})
        self.assertFalse(ok)
        self.assertIn("no phase is open", why)
        # the engine moves only open -> rest, and only while still at a cap under the lock
        self.assertFalse(phases.set_state(cfg, "H0", "rest", by="engine", only_from="open", recheck_cap=True)[0])
        self.assertEqual(phases.load(cfg)["H0"]["state"], "closed")
        orch("phase", "open", "H1", check=True)
        self.assertFalse(phases.set_state(cfg, "H1", "rest", by="engine", only_from="open", recheck_cap=True)[0],
                         "no cap reached: the Owner's re-open wins")
        # approve says what a hand edit changes
        y = INST / "phases.yaml"
        y.write_text(y.read_text().replace("state: closed", "state: open", 1))
        p = orch("phase", "approve", check=True)
        self.assertIn("H0: state closed -> open", p.stdout)
        # the protected-write registry honours only the files orch rewrites
        caps = INST / "SPEND_CAPS.yaml"
        had = caps.exists()
        if not had:
            caps.write_text("daily_usd: 1\n")
            self.addCleanup(caps.unlink, missing_ok=True)
        rel = str(caps.resolve().relative_to(cfg.git_repo.resolve()))
        import hashlib
        reg = INST / "state" / "protected_writes.json"
        cur = json.loads(reg.read_text())
        cur[rel] = {"sha": hashlib.sha256(caps.read_bytes()).hexdigest()}
        reg.write_text(json.dumps(cur))
        self.assertFalse(protected.orch_wrote(cfg, rel), "a forged entry for another protected path is ignored")
        protected.note_write(cfg, caps, "x")
        self.assertNotIn(rel, [k for k, v in json.loads(reg.read_text()).items() if v.get("sha") == "x"])
        # deleting both files keeps the fence (the marker) and is reported; `orch phase off` is the Owner's
        y.unlink()
        (INST / "state" / "phases.json").unlink()
        self.assertTrue(phases.active(cfg))
        self.assertIn("removed outside", phases.tamper(cfg))
        self.assertIn("PHASE", phases.Fence(cfg).block("A", ""))
        p = orch("phase", "off", env=dict(ENV, ORCH_ROW="a1", ORCH_KIND="row"))
        self.assertIn("REFUSED", p.stdout)
        self.assertTrue(phases.active(cfg))
        (INST / "state" / "phases.on").unlink()
        rig._phases_seen = True
        phases.tick(rig)
        self.assertTrue(any(x["event"] == "phases_removed" for x in events("phases")), "a silent removal is reported")
        orch("phase", "open", "H9", check=True)
        self.assertIn("fence is off", orch("phase", "off", check=True).stdout)
        self.assertFalse(phases.active(cfg))

    # -- drop D items 2 and 3: the native course loop and the executor policy per round ------------------
    @staticmethod
    def _round_yaml(tag: str, **keys) -> None:
        import yaml
        f = INST / "rounds" / tag / "round.yaml"
        data = yaml.safe_load(f.read_text())
        data.update(keys)
        f.write_text(yaml.safe_dump(data, sort_keys=False))

    @staticmethod
    def _loop(tag: str) -> dict:
        try:
            return json.loads((INST / "state" / "courseloop" / f"{tag}.json").read_text())
        except Exception:
            return {}

    @staticmethod
    def _loop_events(tag: str) -> list:
        return [e["event"][len("course_loop_"):] for e in store().events(f"round:{tag}")
                if str(e.get("event", "")).startswith("course_loop_")]

    def _course_loop_scripts(self, tag: str, nxt: str, lane: str, row: str, phase: str = "") -> None:
        """The review writes a question; the Steward answers it and plans; the planner cuts one row and starts."""
        (WS / f"PHASE_{lane}" / row).mkdir(parents=True, exist_ok=True)
        (WS / f"PHASE_{lane}" / row / "PLAN.md").write_text(PLAN.format(id=row))
        ctl(f"MOCK_RUN_rr-{tag}", f'mkdir -p "$ORCH_INSTANCE/rounds/{tag}/review"\n'
                                  f'printf "# Questions\\n\\nQ1: keep the bar? A) yes (recommended) B) lower it\\n" '
                                  f'> "$ORCH_INSTANCE/rounds/{tag}/review/QUESTIONS.md"\n')
        ctl(f"MOCKSWRUN_round:{tag}", f'printf "Q1: A — the bar is not the Steward\'s (W): kept\\n" '
                                      f'> "$ORCH_INSTANCE/rounds/{tag}/review/ANSWERS.md"\n')
        ctl(f"MOCKSW_round:{tag}", f'plan {nxt} "course loop: one correction row {row} in lane {lane}"\n')
        ph = f"--phase {phase} " if phase else ""
        ctl(f"MOCKPLRUN_{nxt}", f'O="$ORCH_INSTANCE/orch"\ncd "$ORCH_INSTANCE/.."\n'
                                f'"$O" sw round-new {nxt} {ph}{lane} "the correction round"\n'
                                f'"$O" row add {lane} "{row} - 0 mock PHASE_{lane}/{row}/PLAN.md" "the correction"\n'
                                f'"$O" sw round-start {nxt} "rows cut"\n')

    def test_38_course_loop_nogo_review_plan_rearm_then_phase_rests(self):
        """Drill (drop D item 2, drill 6): a judge closing `--outcome nogo` in an open phase gets
        a round review, a Steward's answers and plan, a planner's round and a re-armed judge with ZERO operator
        commands; when the re-armed judge closes nogo again at max_loops, the phase rests and the Owner's inbox
        says why — no second loop, no page, the needs=go dependent never runs."""
        import phases
        import digest
        from config_parser import load
        self.addCleanup(self._phases_off)
        self._phases_off()
        cfg_set("preflight.reviewer", "mock")
        self.addCleanup(cfg_set, "preflight.reviewer", "opus")
        cfg = load(str(INST))
        orch("phase", "open", "LPP", "--caps", "rows=30,loops=3", "course-loop drill phase", check=True)
        add_lane("LP", [("lw1", "-", 0, "mock", ""), ("lj", "lw1", 0, "mock", ""), ("ld", "lj", 0, "mock", "needs=go")])
        ctl("MOCK_lj", "CLOSED\nthe bar is not met\nnogo\n")
        ctl("MOCK_VIA_CLI_lj")                        # closes with `orch done lj … --outcome nogo`
        orch("round", "new", "lp", "--lanes", "LP", "--phase", "LPP", check=True)
        self._round_yaml("lp", judge="lj", on_fail="course_loop", max_loops=1)
        self.assertIn("course loop: judge lj, loops 0/1", orch("round", "status", "lp").stdout)
        self._course_loop_scripts("lp", "lp-l1", "LPF", "lf1", phase="LPP")
        orch("round", "start", "lp", check=True)
        wait(lambda: self._loop("lp").get("state") == "rested", 300, what="loop 1, the re-armed judge's nogo, the rest")
        ev = self._loop_events("lp")
        for step in ("start", "review", "answer", "plan", "run", "rearm", "rest"):
            self.assertIn(step, ev, f"course loop step {step}: {ev}")
        self.assertEqual(ev.count("start"), 1, "max_loops 1: one loop")
        self.assertEqual(ev.count("review"), 1, "no second review")
        self.assertEqual(store().attempt("lj"), 2, "the judge ran twice: once, then re-armed by the engine")
        self.assertTrue(any(e.get("event") == "reset" and e.get("by") == "course-loop:lp#1" for e in events("lj")))
        self.assertEqual(phase("lf1"), "accepted", "the planner's row ran before the re-arm")
        acc = [float(e["epoch"]) for e in events("lf1") if e.get("event") == "accept"]
        rst = [float(e["epoch"]) for e in events("lj") if e.get("event") == "reset" and e.get("by") == "course-loop:lp#1"]
        self.assertTrue(acc and rst and rst[0] >= acc[0], f"the re-arm came after the correction was accepted: {acc} {rst}")
        self.assertTrue((INST / "rounds" / "lp" / "review" / "ANSWERS.md").is_file(), "the Steward wrote ANSWERS.md")
        rec = self._loop("lp")
        self.assertEqual((rec["loops"], rec["current"]["next"], rec["current"]["rows"]), (1, "lp-l1", ["lf1"]))
        inc = incident_for("round:lp", "round_review")
        self.assertIsNotNone(inc, "the review's questions opened a round_review incident")
        self.assertIsNone(incident_for("lj", "negative_outcome"), "the judge's NO-GO is the course loop's, not an incident")
        ph = phases.load(cfg)["LPP"]
        self.assertEqual((ph["state"], ph["loops"]), ("rest", 1))
        self.assertIn("course loop lp", ph.get("rest_reason", ""))
        self.assertIn("lp-l1", ph["rounds"], "the correction round stayed inside the phase")
        self.assertEqual(store().dispatch("ld"), {}, "the needs=go dependent never ran")
        inbox = (INST / "steward" / "INBOX.md").read_text()
        self.assertIn("course loop lp: lj still nogo — no loop left", inbox)
        evs = all_events()
        i = max(k for k, e in enumerate(evs) if e.get("event") == "round_start" and e.get("id") == "round:lp")
        touches = digest.human_touches(evs[i:])
        self.assertEqual(touches, ["round start lp"], "zero operator commands after the round started")
        self.assertTrue(no_halt_page_attempted())
        d = orch("digest").stdout
        self.assertIn("Course loops", d)
        time.sleep(3)
        self.assertEqual(self._loop_events("lp").count("rest"), 1, "the phase rests once per judge claim")

    def test_39_course_loop_go_after_the_correction_without_phases(self):
        """The loop without a phases file (bounded by max_loops only): nogo → review (no questions) → the Steward
        plans → the planner's row → the re-armed judge closes GO → the loop is done and the needs=go dependent runs."""
        self._phases_off()
        cfg_set("preflight.reviewer", "mock")
        self.addCleanup(cfg_set, "preflight.reviewer", "opus")
        add_lane("KG", [("kw1", "-", 0, "mock", ""), ("kj", "kw1", 0, "mock", ""), ("kd", "kj", 0, "mock", "needs=go")])
        ctl("MOCK_kj", "CLOSED\nnot yet\nnogo\n")
        orch("round", "new", "kg", "--lanes", "KG", check=True)
        self._round_yaml("kg", judge="kj", on_fail="course_loop", max_loops=2)
        self._course_loop_scripts("kg", "kg-l1", "KGF", "kf1")
        ctl("MOCK_RUN_rr-kg", "true\n")              # this review asks the Owner nothing
        orch("round", "start", "kg", check=True)
        wait(lambda: "start" in self._loop_events("kg"), 120, what="the loop starting on kj's nogo")
        ctl("MOCK_kj", "CLOSED\nthe correction worked\ngo\n")
        wait(lambda: phase("kd") == "accepted", 300, what="kd after the re-armed judge's GO")
        rec = self._loop("kg")
        self.assertEqual((rec["state"], rec["loops"]), ("done", 1))
        self.assertFalse((INST / "rounds" / "kg" / "review" / "QUESTIONS.md").exists())
        self.assertIn("asked the Owner nothing", incident_for("round:kg", "round_review")["summary"])
        self.assertEqual(self._loop_events("kg")[-1], "done")
        self.assertEqual(store().outcome("kj"), "go")

    def test_40_course_loop_and_round_policy_units(self):
        """Neutral without on_fail; only an operator-made round with the Steward lane and a judge of its own lanes
        loops; the loops cap counts loops allowed; a session never re-runs a measured NO-GO; the round's
        executors: override the rig's in on_blocked, on_dead, the unblocker tier and capacity; lint names a bad
        key."""
        import copy
        import courseloop
        import phases
        import rounds
        import steward
        import template_engine as te
        from config_parser import load
        from engine import Engine
        from rows import find_row
        self.addCleanup(self._phases_off)
        self._phases_off()
        cfg = load(str(INST))
        e = Engine(cfg)
        # config: off by default, and every way it is refused
        r = {"tag": "u1", "lanes": ["UL"], "status": "open"}
        add_lane("UL", [("uj", "-", 0, "mock", "")])
        (INST / "control" / "RUN_UL").unlink(missing_ok=True)
        self.assertEqual(courseloop.config(cfg, "u1", r), (None, ""), "no on_fail: no loop, nothing said")
        good = dict(r, judge="uj", on_fail="course_loop")
        c, why = courseloop.config(cfg, "u1", good)
        self.assertEqual((c["judge"], c["max_loops"], why), ("uj", 1, ""), "max_loops defaults to 1")
        for bad, want in ((dict(good, by="pl-u1-1"), "created by session"), (dict(good, judge="a1"), "not one of"),
                          (dict(good, judge=""), "without a judge"), (dict(good, max_loops=-1), "max_loops"),
                          (dict(good, on_fail="retry"), "is not 'course_loop'"), (dict(good, judge="nope"), "not a row")):
            c, why = courseloop.config(cfg, "u1", bad)
            self.assertIsNone(c)
            self.assertIn(want, why)
        c2 = copy.copy(cfg)
        c2.data = dict(cfg.data, steward=dict(cfg.data["steward"], enabled=False))
        self.assertIn("Steward lane is off", courseloop.config(c2, "u1", good)[1])
        self.assertEqual(courseloop.config(cfg, "u1", dict(good, status="closed")), (None, ""))
        # the loops cap counts loops ALLOWED: the loop that reaches it still runs (its round may be created)
        orch("phase", "open", "U1", "--caps", "loops=1", check=True)
        self.assertEqual(phases.record_loop(cfg, "U1"), 1)
        self.assertEqual(phases.cap_reached(cfg, "U1", phases.load(cfg)["U1"]), "")
        self.assertEqual(phases.fence(cfg, ["UZ"], new_round=True, phase="U1", tag="uz"), "")
        self.assertEqual(phases.record_loop(cfg, "U1"), 2)
        self.assertIn("loops cap (2/1", phases.cap_reached(cfg, "U1", phases.load(cfg)["U1"]))
        self._phases_off()
        # a session never re-runs an accepted negative outcome (the course loop's re-arm is the engine's)
        self.assertEqual((phase("lj"), store().outcome("lj")), ("accepted", "nogo"))
        for act, args in (("reset", ["lj"]), ("retry", ["lj", "mock2"])):
            ok, why, _ = steward.execute(e, "sw-999-1", act, args, "get a GO", {})
            self.assertFalse(ok)
            self.assertIn("never re-run to obtain a GO", why)
        # the round's executors: laid over the rig's (bad keys ignored, named by lint)
        (INST / "rounds" / "ux").mkdir(parents=True, exist_ok=True)
        (INST / "rounds" / "ux" / "round.yaml").write_text(
            "tag: ux\nstatus: open\nlanes: [UL]\nexecutors:\n  retier_on_max_unblock: flaky\n  dead_window_policy: halt\n"
            "  unblock_executors: [mock2]\n  max_parallel: {mock: 0}\n  auto_unblock: [mock2]\n")
        self.addCleanup(shutil.rmtree, INST / "rounds" / "ux", True)
        cfg = load(str(INST))
        e = Engine(cfg)
        pol = rounds.exec_policy(cfg, "UL")
        self.assertEqual((pol["retier_on_max_unblock"], pol["dead_window_policy"], pol["unblock_executors"],
                          pol["round_max_parallel"], pol["round"]), ("flaky", "halt", ["mock2"], {"mock": 0}, "ux"))
        self.assertEqual(pol["auto_unblock"], cfg.data["executors"]["auto_unblock"], "not a per-round key: ignored")
        self.assertEqual(rounds.exec_policy(cfg, "A")["retier_on_max_unblock"], "mock2", "other lanes: the rig's")
        self.assertEqual((te.unblock_executor(cfg, 1, lane="UL"), te.unblock_executor(cfg, 1, lane="A")), ("mock2", "mock"))
        self.assertIn("round ux (its executors.max_parallel)", e.capacity_ok("mock", row="uj")[1])
        self.assertTrue(e.capacity_ok("mock", row="a1")[0], "a row of another round is not held by it")
        self.assertTrue(e.capacity_ok("mock2", row="uj")[0])
        self.assertIn("auto_unblock: not a per-round key", orch("lint").stdout)
        self.assertIn("ignored:", orch("round", "status", "ux").stdout)
        (INST / "rounds" / "ux" / "round.yaml").write_text(
            "tag: ux\nstatus: open\nlanes: [UL]\nexecutors: {retier_on_max_unblock: nope, dead_window_policy: maybe}\n")
        cfg = load(str(INST))
        pol = rounds.exec_policy(cfg, "UL")
        self.assertEqual((pol["retier_on_max_unblock"], pol["dead_window_policy"]), ("mock2", "retier"),
                         "an invalid value is ignored, never half-applied")
        (INST / "rounds" / "ux" / "round.yaml").write_text(
            "tag: ux\nstatus: open\nlanes: [UL]\nexecutors: {retier_on_max_unblock: flaky, dead_window_policy: halt}\n")
        cfg = load(str(INST))
        e = Engine(cfg)
        row = find_row(cfg, "uj")
        # on_blocked past max_unblock_attempts retiers to the ROUND's target
        e.st.write_int("uj.unblock_n", 1)
        e.on_blocked(row, "mock", "drill: blocked once more")
        self.assertEqual((INST / "control" / "EXECUTOR_uj").read_text().strip(), "flaky")
        # on_dead with the round's dead_window_policy halt: no retier although the rig says retier
        for f in ("EXECUTOR_uj",):
            (INST / "control" / f).unlink(missing_ok=True)
        e.st.rm("uj.retiered")
        d = {"sid": "uj", "kind": "row", "lane": "UL", "executor": "mock", "epoch": int(time.time()) - 300, "open": True}
        e.st.write_dispatch("uj", d)
        e.on_dead("uj", "row", d, "1", {"uj": row})
        self.assertEqual(phase("uj"), "halted")
        self.assertFalse((INST / "control" / "EXECUTOR_uj").exists())
        e.st.write_result("uj", {"phase": "parked", "parked_by": "owner", "park_reason": "unit drill"})

    def test_41_round_executor_policy_through_the_engine(self):
        """Drill (drop D item 3): a round's executors.max_parallel serialises its rows on one executor while the
        rig would run them together, and its unblock_executors pick the unblocker's tier."""
        add_lane("KX", [("kx1", "-", 0, "mock", ""), ("kx2", "-", 0, "mock", ""), ("kx3", "-", 0, "mock", ""),
                        ("ky1", "-", 0, "mock", "")])
        for rid in ("kx1", "kx2", "kx3"):
            ctl(f"MOCK_DELAY_{rid}", "4")
        ctl("MOCK_ky1", "BLOCKED\nneeds a hand once\n")
        ctl("MOCK_DELAY_ub-ky1-1", "4")
        orch("round", "new", "kx", "--lanes", "KX", check=True)
        self._round_yaml("kx", executors={"max_parallel": {"mock": 1}, "unblock_executors": ["mock2"]})
        orch("round", "start", "kx", check=True)
        wait(lambda: store().dispatch("ub-ky1-1").get("executor"), 200, what="ky1's unblocker")
        ctl("MOCK_ky1", "CLOSED\nunblocked\n")
        self.assertEqual(store().dispatch("ub-ky1-1")["executor"], "mock2", "the round's unblock tier, not the rig's mock")
        wait(lambda: all(phase(x) == "accepted" for x in ("kx1", "kx2", "kx3", "ky1")), 300, what="round kx")
        starts = sorted(float(e["epoch"]) for x in ("kx1", "kx2", "kx3") for e in events(x) if e.get("event") == "dispatch")
        self.assertGreaterEqual(starts[-1] - starts[0], 7, f"one mock session at a time in round kx: {starts}")

    def test_42_course_loop_holes_closed(self):
        """The adversarial pass's findings, closed: a row's `orch rearm` / a session's rearm= grant of a NO-GO loop
        judge; a correction that is all parked or gone never re-arms the judge; a judge reset or re-judged under the
        loop ends it (GO = done); a correction in the judge's own round stops the loop; only the loop's own planners'
        rows count; one judge, one loop; a session's round keeps the rig's tiers. Run on a copy of the state (the
        live engine never sees these records)."""
        import copy
        import courseloop
        import rearm
        import rounds
        import steward
        from config_parser import load
        from engine import Engine
        cfg = load(str(INST))
        e = Engine(cfg)
        # 1: the loop judge lj (accepted nogo, round lp) — no row may re-arm it, no session may grant it
        self.assertEqual((phase("lj"), store().outcome("lj")), ("accepted", "nogo"))
        e.st.write_dispatch("zzc", {"sid": "zzc", "snapshot": {"meta": {"rearm": "lj"}}})
        ok, why = rearm.validate(e, "zzc", "lj")
        self.assertFalse(ok)
        self.assertIn("course-loop judge", why)
        self.assertIn("course-loop judge", courseloop.owns_negative(cfg, "lj"))
        self.assertEqual(courseloop.owns_negative(cfg, "lf1"), "", "not a judge")
        import rowadd
        who = {"sid": "pl-lp-9", "kind": "planner", "lanes": ["LP"], "grants": [], "round": "lp"}
        (WS / "PHASE_LP" / "lz1").mkdir(parents=True, exist_ok=True)
        (WS / "PHASE_LP" / "lz1" / "PLAN.md").write_text(PLAN.format(id="lz1"))
        ok, why, _ = rowadd.validate(cfg, e.st, who, "LP", "lz1 - 0 mock PHASE_LP/lz1/PLAN.md rearm=lj")
        self.assertFalse(ok)
        self.assertIn("course-loop judge", why)
        # the state machine on a copy of the state
        c2 = copy.copy(cfg)
        c2.state = SBX / "cl_state"
        (c2.state / "steward").mkdir(parents=True, exist_ok=True)
        e2 = Engine(c2)
        add_lane("UQ", [("uqj", "-", 0, "mock", "")])
        add_lane("UQF", [("uqf1", "-", 0, "mock", ""), ("uqf2", "-", 0, "mock", "")])
        for l in ("UQ", "UQF"):
            (INST / "control" / f"RUN_{l}").unlink(missing_ok=True)
        (INST / "rounds" / "uq").mkdir(parents=True, exist_ok=True)
        (INST / "rounds" / "uq" / "round.yaml").write_text(
            "tag: uq\nstatus: closed\nlanes: [UQ]\njudge: uqj\non_fail: course_loop\n")
        self.addCleanup(shutil.rmtree, INST / "rounds" / "uq", True)
        conf = {"judge": "uqj", "max_loops": 1, "lanes": ["UQ"]}

        def judged(oc: str, mt: int) -> str:
            e2.st.write_result("uqj", {"phase": "accepted", "outcome": oc, "status_mtime": mt})
            return courseloop._sig(e2.st, "uqj")

        def rec(state: str, sig: str, **cur) -> dict:
            return {"tag": "uq", "judge": "uqj", "loops": 1, "state": state, "phase": "",
                    "current": dict({"n": 1, "outcome": "nogo", "sig": sig, "started_epoch": time.time() - 30,
                                     "next": "uq-l1", "rows": ["uqf1"]}, **cur)}

        def step(r: dict) -> dict:
            if not courseloop._superseded(e2, "uq", conf, r):
                getattr(courseloop, "_" + r["state"])(e2, "uq", conf, r)
            return r

        sig = judged("nogo", 100)
        e2.st.write_result("uqf1", {"phase": "parked"})
        r = step(rec("run", sig))
        self.assertEqual(r["state"], "stopped")
        self.assertIn("every row of the correction was parked", r["current"]["stopped"])
        r = step(rec("run", sig, rows=["ghost"]))
        self.assertIn("no longer in any queue", r["current"]["stopped"])
        e2.st.write_result("uqf1", {"phase": "accepted"})
        e2.st.write_result("uqf2", {"phase": "parked"})
        r = step(rec("run", sig, rows=["uqf1", "uqf2"]))
        self.assertEqual(r["state"], "judging", "one accepted row is a correction")
        self.assertEqual(e2.st.phase("uqj"), "", "re-armed")
        self.assertTrue(any(x.get("by") == "course-loop:uq#1" for x in e2.st.events("uqj") if x.get("event") == "reset"))
        # 3: reset or re-judged under the loop
        r = step(rec("plan", sig))
        self.assertEqual(r["state"], "stopped")
        self.assertIn("reset under the loop", r["current"]["stopped"])
        judged("go", 200)
        r = step(rec("review", sig))
        self.assertEqual(r["state"], "done", "a GO outside the loop ends it as done")
        # 5: a correction planned into the judged round itself stops the loop
        sig = judged("nogo", 300)
        pj = c2.state / "steward" / "planners.json"
        pj.write_text(json.dumps({"pl-uq-7": {"incident": 77, "round": "uq", "epoch": int(time.time()), "status": "running"}}))
        r = step(rec("answer", sig, incident=77))
        self.assertEqual(r["state"], "stopped")
        self.assertIn("a round of its own", r["current"]["stopped"])
        # 8: only the loop's own planners' rows count (pl-uq-l1-7 is not a planner of round uq)
        pj.write_text(json.dumps({"pl-uq-3": {"round": "uq", "epoch": int(time.time())},
                                  "pl-uq-l1-7": {"round": "uq-l1", "epoch": int(time.time())}}))
        e2.st.event("uqf1", "row_add", lane="UQF", by="pl-uq-3")
        e2.st.event("uqf2", "row_add", lane="UQF", by="pl-uq-l1-7")
        self.assertEqual(courseloop.loop_rows(e2, rec("run", sig, next="uq")), ["uqf1"])
        # 9: one judge, one loop
        for t in ("ud1", "ud2"):
            (INST / "rounds" / t).mkdir(parents=True, exist_ok=True)
            (INST / "rounds" / t / "round.yaml").write_text(
                f"tag: {t}\nstatus: open\nlanes: [UQ]\njudge: uqj\non_fail: course_loop\n")
            self.addCleanup(shutil.rmtree, INST / "rounds" / t, True)
        good, bad = courseloop.configs(cfg)
        self.assertNotIn("ud1", good)
        self.assertIn("one judge, one loop", bad["ud2"])
        for t in ("ud1", "ud2"):
            shutil.rmtree(INST / "rounds" / t, True)
        # a second round naming the loop judge switches its loop off — never the guard on its NO-GO
        (INST / "rounds" / "ud3").mkdir(parents=True, exist_ok=True)
        (INST / "rounds" / "ud3" / "round.yaml").write_text(
            "tag: ud3\nstatus: open\nlanes: [LP]\njudge: lj\non_fail: course_loop\n")
        self.addCleanup(shutil.rmtree, INST / "rounds" / "ud3", True)
        self.assertNotIn("lj", courseloop.judges(cfg))
        self.assertIn("course-loop judge", courseloop.owns_negative(cfg, "lj"))
        shutil.rmtree(INST / "rounds" / "ud3", True)
        # a plan step that waited on a resting phase asks again once the phase is open (back to answer)
        pj.write_text(json.dumps({"pl-uq-l1-1": {"incident": 5, "round": "uq-l1", "epoch": int(time.time()), "status": "fail"}}))
        r = step(rec("plan", sig, waiting="phase P is rest"))
        self.assertEqual(r["state"], "answer", r)
        self.assertNotEqual(r["current"].get("incident"), 77)
        r = step(r)
        self.assertEqual(r["state"], "answer", "its incident is open: the Steward plans")
        # 7: a session's round narrows max_parallel only; the tiers stay the rig's
        (INST / "rounds" / "uq" / "round.yaml").write_text(
            "tag: uq\nstatus: open\nby: pl-x-1\nlanes: [UQ]\nexecutors: {dead_window_policy: halt, "
            "retier_on_max_unblock: flaky, unblock_executors: [mock2], max_parallel: {mock: 1}}\n")
        pol = rounds.exec_policy(load(str(INST)), "UQ")
        self.assertEqual((pol["dead_window_policy"], pol["retier_on_max_unblock"], pol["round_max_parallel"]),
                         ("retier", "mock2", {"mock": 1}))
        self.assertIn("executors.dead_window_policy, retier_on_max_unblock, unblock_executors ignored", orch("lint").stdout)
        self.assertIn("created by session", courseloop.config(cfg, "uq", {"tag": "uq", "by": "pl-x-1", "lanes": ["UQ"],
                                                                          "judge": "uqj", "on_fail": "course_loop"})[1])

    def test_43_reboot_service_engine_comes_back_by_itself(self):
        """Drill 1 (reboot), with no operator command: the engine runs as the systemd unit runs it
        (`engine.py --wait-lock`, restarted on failure after the unit's RestartSec — emulated here by a
        respawn loop, since tests never touch the real systemd). The tmux server dies under a running
        row and the engine is SIGKILLed: it is back within 2 min, the row is re-dispatched as
        interrupted and accepted — no halt, no retier, no orch command."""
        import re
        import signal
        import threading
        import service
        from config_parser import load
        unit = service.units(load(str(INST)))[f"orch-engine-{SESSION}.service"]
        self.assertIn("Restart=on-failure", unit)
        restart_sec = int(re.search(r"^RestartSec=(\d+)$", unit, re.M).group(1))
        import shlex
        exec_start = shlex.split(re.search(r"^ExecStart=(.+)$", unit, re.M).group(1))   # the unit's own command
        self.assertLessEqual(restart_sec, 60, "the unit restarts a dead engine well inside the 2-min bar")
        hb = INST / "state" / "engine.heartbeat"
        hb_pid = lambda: int(hb.read_text().split()[1])
        ctl("MOCK_DELAY_rb1", "25")
        add_lane("RB", [("rb1", "-", 0, "mock", "")])
        orch("start", "RB", check=True)     # the test's own setup act (the round start an Owner made)
        wait(lambda: store().dispatch("rb1").get("open"), 60, what="rb1 running")
        procs, stop = [], threading.Event()
        log = open(SBX / "service_engine.log", "a")

        def supervisor():                   # what systemd does with Restart=on-failure
            while not stop.is_set():
                p = subprocess.Popen(exec_start, env=dict(ENV, ORCH_SERVICE="1"), cwd=str(WS), stdout=log, stderr=log)
                procs.append(p)
                while p.poll() is None and not stop.is_set():
                    time.sleep(0.2)
                if stop.is_set() or p.returncode == 0:
                    return
                stop.wait(restart_sec)

        t = threading.Thread(target=supervisor, daemon=True)
        t.start()
        try:
            time.sleep(1)
            subprocess.run(["tmux", "kill-session", "-t", f"={SESSION}"], capture_output=True)   # tmux engine + row die
            wait(lambda: procs and hb_pid() == procs[-1].pid, 60, what="the service engine took the lock")
            (INST / "control" / "MOCK_DELAY_rb1").write_text("2")
            killed = procs[-1]
            killed.send_signal(signal.SIGKILL)            # … and the engine itself dies
            t_kill = time.time()
            wait(lambda: len(procs) >= 2 and procs[-1].poll() is None and hb_pid() == procs[-1].pid,
                 120, what="the engine back within 2 min of the kill")
            wait(lambda: phase("rb1") == "accepted", 120, what="rb1 accepted after the reboot")
            evs = [e["event"] for e in events("rb1")]
            self.assertIn("interrupted", evs)
            for bad in ("halt", "retier"):
                self.assertNotIn(bad, evs)
            self.assertFalse((INST / "control" / "EXECUTOR_rb1").exists())
            self.assertTrue(any(" rb1 accepted" in n.get("title", "") for n in notifications(T0[0])),
                            "the record saw this drill's notifications")
            self.assertTrue(no_halt_page_attempted())
            self.assertEqual([x for x in __import__("digest").human_touches(events("rb1"))], [],
                             "no operator act on the row")
        finally:
            stop.set()
            for p in procs:
                if p.poll() is None:
                    p.terminate()
                    try:
                        p.wait(20)
                    except subprocess.TimeoutExpired:
                        p.kill()
            t.join(30)
            log.close()
        orch("start", "RB", check=True)     # the tmux engine back for the suite (RB is enabled already; no other lane)
        wait(lambda: hb_pid() not in [p.pid for p in procs], 60, what="the tmux engine back for the suite")

    def test_44_wall_row_deleting_a_test_refused_parked_inboxed(self):
        """Drill 5 end to end: a row deletes a (protected) test and claims CLOSED. The close check refuses
        the claim on every attempt, the row halts, the Steward may not decide a wall — it inboxes the
        Owner and the row parks; independent work keeps running; nothing pages."""
        import yaml
        cur = (yaml.safe_load((INST / "orch.yaml").read_text()).get("guard") or {}).get("protected_paths") or []
        cur = [cur] if isinstance(cur, str) else list(cur)
        cfg_set("guard.protected_paths", cur + ["PHASE_T5/t5/tests/"])   # inside the row's allowlist
        (WS / "PHASE_T5" / "t5" / "tests").mkdir(parents=True, exist_ok=True)
        (WS / "PHASE_T5" / "t5" / "tests" / "test_bar.py").write_text("def test_bar():\n    assert 2 + 2 == 4\n")
        self.assertEqual(orch("commit", "plan:setup", "-m", "operator: a test", "--", "PHASE_T5/t5/tests/test_bar.py").returncode, 0)
        (INST / "control" / "MOCK_RUN_t5").write_text(
            f"cd '{WS}'\nexec >> '{SBX}/t5_run.out' 2>&1\n"
            f"if [ -f PHASE_T5/t5/tests/test_bar.py ]; then\n"
            f"  rm PHASE_T5/t5/tests/test_bar.py\n"
            f"  '{INST}/orch' commit t5 -m 'drop a test' -- PHASE_T5/t5/tests/test_bar.py && echo GUARD_LET_IT_THROUGH\n"
            f"  git rm -q --cached --ignore-unmatch PHASE_T5/t5/tests/test_bar.py\n"
            f"  git commit -qm 'row: drop a failing test'   # around the guard\n"
            f"fi\n")
        ctl("MOCKSWV_t5", "INBOX\ndeleting a test is the Owner's (wall)\n")
        ctl("MOCKSW_t5", 'inbox "t5 wants to delete a test" "A) keep the test (recommended) B) delete it" t5 '
                         '"a test deletion is a wall"\n')
        add_lane("T5", [("t5", "-", 0, "mock", ""), ("t5b", "t5", 0, "mock", "")])
        orch("start", "T5", check=True)
        try:
            wait(lambda: phase("t5") == "parked", 240, what="t5 parked")
            # independent work dispatched after the bypass commit runs (a row that was already running when an
            # unattributable commit touched a protected path is held too — fail closed, V3_DESIGN §7 known limits)
            add_lane("T6", [("t6", "-", 0, "mock", "")])
            orch("start", "T6", check=True)
            wait(lambda: phase("t6") == "accepted", 60, what="independent t6 still runs")
            fails = [e for e in events("t5") if e["event"] == "check_failed"]
            self.assertTrue(fails, "the engine refused the CLOSED claim")
            self.assertTrue(all("PHASE_T5/t5/tests/test_bar.py" in str(e.get("detail", "")) for e in fails), fails)
            self.assertNotIn("accepted", [e["event"] for e in events("t5")])
            self.assertEqual(phase("t5b"), "", "a dependent of the parked row waits")
            self.assertIn("t5 wants to delete a test", (INST / "steward" / "INBOX.md").read_text())
            self.assertEqual(incident_for("t5", "halt")["outcome"], "inbox")
            self.assertTrue(halt_downgraded("t5"), "the halt was announced and kept off the phone")
            self.assertTrue(no_halt_page_attempted(), "a wall never pages")
            out = (SBX / "t5_run.out").read_text()
            self.assertIn("protected path", out, "the guard refused to stage the deletion")
            self.assertNotIn("GUARD_LET_IT_THROUGH", out)
            self.assertIn("row: drop a failing test", git("log", "--format=%s", "-30"), "the bypass commit happened")
        finally:
            cfg_set("guard.protected_paths", cur)

    def test_45_owner_park_holds_and_phase_rows_count_from_first_open(self):
        """vD3 adversarial-pass fixes. (1) An Owner park is lifted only by the Owner: a Steward reset, retry,
        assign or approve and a row's rearm grant are refused on it (they reset the row, which cleared the park).
        (2) A phase's rows cap counts rows started since its FIRST open: rows that ran before the phase existed
        neither rest it at once nor fill `orch row add`'s count; a re-open keeps the count (no reset by re-open)."""
        import phases
        import rearm
        import steward
        from config_parser import load
        from engine import Rig
        add_lane("OP", [("op1", "-", 1, "mock", ""), ("op2", "-", 0, "mock", "rearm=op1")])
        rig = Rig(load(str(INST)))
        self.assertTrue(steward.park(rig, "op1", "DEC-x: not to be reset", by="owner"))
        for action, args in (("reset", ["op1"]), ("retry", ["op1", "mock2"]), ("assign", ["op1", "mock2"]),
                             ("approve", ["op1"]), ("unpark", ["op1"])):
            ok, out, _ = steward.execute(rig, "sw-900-1", action, args, "try it", {"incident": 900})
            self.assertFalse(ok, f"{action} on an Owner-parked row: {out}")
            self.assertIn("only the Owner unparks it", out)
        self.assertEqual(phase("op1"), "parked")
        self.assertFalse((INST / "control" / "EXECUTOR_op1").exists())
        store().write_dispatch("op2", {"open": True, "epoch": int(time.time()), "snapshot": {"meta": {"rearm": "op1"}}})
        ok, why = rearm.validate(rig, "op2", "op1")
        self.assertFalse(ok)
        self.assertIn("parked by the Owner", why)
        self.assertTrue(steward.park(rig, "op2", "a Steward park", by="sw-901"))
        ok, out, _ = steward.execute(rig, "sw-900-1", "unpark", ["op2"], "a Steward park is the Steward's",
                                     {"incident": 900})
        self.assertTrue(ok, out)

        # (2) a lane whose rows all ran before its phase was opened
        cfg = load(str(INST))
        pre = [f"pr{i}" for i in range(3)]
        add_lane("PRE", [(r, "-", 0, "mock", "") for r in pre] + [("pr9", "-", 0, "mock", "")])
        old = int(time.time()) - 3600
        for r in pre:
            store().write_dispatch(r, {"open": False, "epoch": old})
            store().write_result(r, {"phase": "accepted"})
        (INST / "rounds" / "pre").mkdir(parents=True, exist_ok=True)
        (INST / "rounds" / "pre" / "round.yaml").write_text("tag: pre\nstatus: open\nlanes: [PRE]\n")
        ok, msg = phases.open_phase(cfg, "PRE", "rows=2", ["pre"], "pre-existing rows")
        self.assertTrue(ok, msg)
        rec = phases.load(load(str(INST)))["PRE"]
        self.assertTrue(rec.get("rows_since"))
        cfg = load(str(INST))
        self.assertEqual(phases.usage(cfg, "PRE", rec)["rows"], 0, "rows that ran before the first open are not the phase's")
        self.assertEqual(phases.cap_reached(cfg, "PRE", rec), "")
        self.assertEqual(phases.row_cap(cfg, "PRE"), "", "pr9 (never dispatched) is the only row the cap holds")
        store().write_dispatch("pr9", {"open": False, "epoch": int(time.time())})
        store().write_dispatch("pr0", {"open": False, "epoch": int(time.time())})   # an old row run again counts
        self.assertEqual(phases.usage(cfg, "PRE", rec)["rows"], 2)
        self.assertIn("rows cap", phases.cap_reached(cfg, "PRE", rec))
        self.assertIn("rows cap is 2", phases.row_cap(cfg, "PRE"))
        ok, msg = phases.set_state(cfg, "PRE", "rest", by="owner", why="the Owner")
        self.assertTrue(ok, msg)
        ok, msg = phases.open_phase(load(str(INST)), "PRE", "", [], "re-open at the cap")
        self.assertFalse(ok, "a re-open keeps the count: still at the cap")
        self.assertIn("rows cap", msg)
        self.assertEqual(phases.load(load(str(INST)))["PRE"]["rows_since"], rec["rows_since"])
        ok, msg = phases.open_phase(load(str(INST)), "PRE", "rows=5", [], "re-open with a higher cap")
        self.assertTrue(ok, msg)
        rec2 = phases.load(load(str(INST)))["PRE"]
        self.assertEqual(rec2["rows_since"], rec["rows_since"], "a re-open keeps the count")
        self.assertNotIn("rest_reason", rec2, "a re-open clears the old rest reason")
        self.assertEqual(phases.usage(load(str(INST)), "PRE", rec2)["rows"], 2)
        phases.set_state(load(str(INST)), "PRE", "closed", by="owner", why="test over")
        store().write_dispatch("op2", {"open": False, "epoch": int(time.time()), "snapshot": {"meta": {"rearm": "op1"}}})
        for lane in ("OP", "PRE"):
            (INST / "control" / f"RUN_{lane}").unlink(missing_ok=True)

    # -- v3.1: adaptive plans (docs/V3_DESIGN.md §15) ---------------------------------------------------
    def _loop_scripts(self, key: str, nxt: str, lane: str, row: str, phase: str = "", sw_pre: str = "",
                      plan_pre: str = "") -> None:
        """_course_loop_scripts for any loop key (`<tag>` or a checkpoint's `<tag>+<judge>`): the review asks one
        question; the Steward answers it (after `sw_pre`'s acts) and plans; the planner (after `plan_pre`) cuts one
        row in a lane of its own and starts its round."""
        tag, _, cj = key.partition("+")
        rd = f"rounds/{tag}/review" + (f"+{cj}" if cj else "")
        subj = f"round:{key}".replace("+", "_")       # the mock adapter's file-name form of the incident subject
        (WS / f"PHASE_{lane}" / row).mkdir(parents=True, exist_ok=True)
        (WS / f"PHASE_{lane}" / row / "PLAN.md").write_text(PLAN.format(id=row))
        ctl(f"MOCK_RUN_rr-{key}", f'mkdir -p "$ORCH_INSTANCE/{rd}"\n'
                                  f'printf "# Questions\\n\\nQ1: keep the bar? A) yes (recommended) B) lower it\\n" '
                                  f'> "$ORCH_INSTANCE/{rd}/QUESTIONS.md"\n')
        ctl(f"MOCKSWRUN_{subj}", f'printf "Q1: A — the bar is not the Steward\'s (W): kept\\n" '
                                 f'> "$ORCH_INSTANCE/{rd}/ANSWERS.md"\n')
        ctl(f"MOCKSW_{subj}", sw_pre + f'plan {nxt} "course loop: one row {row} in lane {lane}"\n')
        ph = f"--phase {phase} " if phase else ""
        ctl(f"MOCKPLRUN_{nxt}", f'O="$ORCH_INSTANCE/orch"\ncd "$ORCH_INSTANCE/.."\n{plan_pre}'
                                f'"$O" sw round-new {nxt} {ph}{lane} "the correction round"\n'
                                f'"$O" row add {lane} "{row} - 0 mock PHASE_{lane}/{row}/PLAN.md" "the correction"\n'
                                f'"$O" sw round-start {nxt} "rows cut"\n')

    @staticmethod
    def _mini(name: str, lanes: dict, rounds_: dict, serial: bool = False):
        """An instance of its own beside the sandbox (its own state, control, queues and rounds; the sandbox's
        workspace and orch.yaml): course-loop steps run on it without the live engine ever seeing them."""
        import copy
        from config_parser import load
        from engine import Rig
        cfg = load(str(INST))
        c = copy.copy(cfg)
        c.instance = SBX / name
        for a in ("state", "control", "leases", "prompts", "unblock"):
            setattr(c, a, c.instance / a)
            getattr(c, a).mkdir(parents=True, exist_ok=True)
        c.templates_local = c.prompts
        shutil.copy(INST / "orch.yaml", c.instance / "orch.yaml")
        if serial:
            c.data = dict(cfg.data, scheduler=dict(cfg.data.get("scheduler") or {}, mode="serial"))
        for lane, rows in lanes.items():
            lines = []
            for rid, deps, extras in rows:
                d = WS / f"PHASE_{lane}" / rid
                d.mkdir(parents=True, exist_ok=True)
                (d / "PLAN.md").write_text(PLAN.format(id=rid))
                lines.append(f"{rid}\t{deps}\t0\tmock\tPHASE_{lane}/{rid}/PLAN.md\t{extras}".rstrip())
            (c.instance / f"queue_{lane}.tsv").write_text("\n".join(lines) + "\n")
        for tag, text in rounds_.items():
            (c.instance / "rounds" / tag).mkdir(parents=True, exist_ok=True)
            (c.instance / "rounds" / tag / "round.yaml").write_text(text)
        return c, Rig(c)

    def _close_phase(self, pid: str, *lanes: str) -> None:
        import phases
        from config_parser import load
        phases.set_state(load(str(INST)), pid, "closed", by="owner", why="drill over")
        for lane in lanes:
            (INST / "control" / f"RUN_{lane}").unlink(missing_ok=True)

    def test_46_two_checkpoint_judges_loop_independently(self):
        """v3.1 (§15.2): a round declares two checkpoint judges (`judges:`). Each NO-GO runs its own course loop —
        its own record, review folder, incident, planner, correction round and re-arm — and one loop finishes while
        the other is still running. Both count in the phase's loops (no replans cap); the GO-only dependents run
        once each checkpoint passes."""
        import phases
        from config_parser import load
        cfg_set("preflight.reviewer", "mock")
        self.addCleanup(cfg_set, "preflight.reviewer", "opus")
        orch("phase", "open", "CPP", "--caps", "rows=40,loops=4", "two checkpoints", check=True)
        add_lane("CP", [("cw1", "-", 0, "mock", ""), ("cj1", "cw1", 0, "mock", ""), ("cj2", "cw1", 0, "mock", ""),
                        ("cd1", "cj1", 0, "mock", "needs=go"), ("cd2", "cj2", 0, "mock", "needs=go")])
        for j in ("cj1", "cj2"):
            ctl(f"MOCK_{j}", "CLOSED\nthe wave's bar is not met\nnogo\n")
            ctl(f"MOCK_VIA_CLI_{j}")
        orch("round", "new", "cp", "--lanes", "CP", "--phase", "CPP", check=True)
        self._round_yaml("cp", judges=["cj1", {"row": "cj2", "max_loops": 2}], on_fail="course_loop")
        st = orch("round", "status", "cp").stdout
        self.assertIn("course loop: checkpoint cj1, loops 0/1", st)
        self.assertIn("course loop: checkpoint cj2, loops 0/2", st)
        self._loop_scripts("cp+cj1", "cp-cj1-l1", "CPA", "ca1", phase="CPP")
        self._loop_scripts("cp+cj2", "cp-cj2-l1", "CPB", "cb1", phase="CPP")
        ctl("MOCK_DELAY_cb1", "40")                   # cj2's correction runs long; cj1's loop finishes meanwhile
        orch("round", "start", "cp", check=True)
        wait(lambda: self._loop("cp+cj1").get("loops") == 1 and self._loop("cp+cj2").get("loops") == 1, 120,
             what="both checkpoint loops started")
        for j in ("cj1", "cj2"):                       # the corrections work: the re-armed checkpoints pass
            ctl(f"MOCK_{j}", "CLOSED\nthe wave's bar is met after the correction\ngo\n")
        wait(lambda: self._loop("cp+cj1").get("state") == "done", 300, what="cj1's loop done")
        other = self._loop("cp+cj2").get("state")
        self.assertIn(other, ("plan", "run", "judging"), f"cj2's loop runs on its own clock (state {other})")
        wait(lambda: phase("cd1") == "accepted" and phase("cd2") == "accepted", 300, what="both GO-only dependents")
        for key, j, nxt in (("cp+cj1", "cj1", "cp-cj1-l1"), ("cp+cj2", "cj2", "cp-cj2-l1")):
            rec = self._loop(key)
            self.assertEqual((rec["loops"], rec["state"], rec["judge"], rec["round"], rec["kind"]),
                             (1, "done", j, "cp", "checkpoint"), key)
            self.assertEqual(rec["current"]["next"], nxt)
            ev = self._loop_events(key)
            for step in ("start", "review", "answer", "plan", "run", "rearm", "done"):
                self.assertIn(step, ev, f"{key}: step {step}: {ev}")
            self.assertTrue((INST / "rounds" / "cp" / f"review+{j}" / "BRIEF.md").is_file(), f"{j}'s own review folder")
            self.assertTrue((INST / "rounds" / "cp" / f"review+{j}" / "ANSWERS.md").is_file())
            self.assertIsNotNone(incident_for(f"round:{key}", "round_review"), f"{key}'s own incident")
            self.assertTrue(any(e.get("event") == "reset" and e.get("by") == f"course-loop:{key}#1" for e in events(j)))
            self.assertEqual(store().outcome(j), "go")
        self.assertFalse((INST / "state" / "courseloop" / "cp.json").exists(), "no round judge: no round-level loop")
        self.assertFalse((INST / "rounds" / "cp" / "review" / "BRIEF.md").exists(), "the round's own folder is untouched")
        ph = phases.load(load(str(INST)))["CPP"]
        self.assertEqual(ph["loops"], 2, "each checkpoint's loop counted once, in loops (no replans cap)")
        self.assertIn("cp-cj1-l1", ph["rounds"])
        self.assertIn("cp-cj2-l1", ph["rounds"])
        self.assertTrue(no_halt_page_attempted())
        self._close_phase("CPP", "CP", "CPA", "CPB")

    def test_47_infeasible_checkpoint_holds_its_wave_and_the_replan_parks_it(self):
        """v3.1 (§15.3): a checkpoint closes `--outcome infeasible` (the remaining plan cannot reach the goal). Its
        wave is held; no session re-runs it; its loop records the held wave; the Steward parks the rows the new route
        makes moot and plans the new route; the re-armed checkpoint passes the same bar; the kept row runs and the
        parked rows never do. A plain row closing infeasible gets the Steward's incident with the replan steps, and
        the Steward's reset of it is refused."""
        import rows as rowsmod
        import steward
        from config_parser import load
        from engine import Rig
        cfg_set("preflight.reviewer", "mock")
        self.addCleanup(cfg_set, "preflight.reviewer", "opus")
        orch("phase", "open", "IFP", "--caps", "rows=40,loops=2", "infeasible drill", check=True)
        add_lane("IF", [("iw1", "-", 0, "mock", ""), ("ij", "iw1", 0, "mock", ""),
                        ("ir1", "ij", 0, "mock", "needs=go"), ("ir2", "ir1", 0, "mock", ""),
                        ("ik", "ij", 0, "mock", "needs=go")])
        ctl("MOCK_ij", "CLOSED\nthe remaining plan cannot reach the goal\ninfeasible\n")
        ctl("MOCK_VIA_CLI_ij")                         # `orch done ij … --outcome infeasible`
        orch("round", "new", "if", "--lanes", "IF", "--phase", "IFP", check=True)
        self._round_yaml("if", judges=["ij"], on_fail="course_loop", max_loops=1)
        self._loop_scripts("if+ij", "if-ij-l1", "IFN", "in1", phase="IFP",
                           sw_pre='park ir1 replan-if "the new route makes it moot"\n'
                                  'park ir2 replan-if "it only runs on the old route"\n')
        ctl("MOCK_DELAY_rr-if+ij", "8")               # the hold is observable before the Steward acts
        orch("round", "start", "if", check=True)
        wait(lambda: self._loop("if+ij").get("loops") == 1, 120, what="ij's loop starts on infeasible")
        cfg = load(str(INST))
        rec = self._loop("if+ij")
        self.assertEqual((rec["current"]["outcome"], rec["current"]["wave"]), ("infeasible", ["ir1", "ir2", "ik"]),
                         "the held wave: the needs=go dependents and what depends on them")
        self.assertEqual(rowsmod.unmet_deps(store(), rowsmod.find_row(cfg, "ik")), ["ij:infeasible"], "ik is held")
        self.assertEqual(store().dispatch("ir1"), {})
        rig = Rig(cfg)
        ok, why, _ = steward.execute(rig, "sw-998-1", "reset", ["ij"], "get a GO", {"incident": 998})
        self.assertFalse(ok)
        self.assertIn("never re-run to obtain a GO", why)
        ctl("MOCK_ij", "CLOSED\nthe new route reaches the bar\ngo\n")
        wait(lambda: phase("ik") == "accepted", 300, what="the kept row after the re-armed checkpoint's GO")
        rec = self._loop("if+ij")
        self.assertEqual((rec["state"], rec["current"]["wave_parked"], rec["current"]["wave_kept"]),
                         ("done", ["ir1", "ir2"], ["ik"]))
        rearm_ev = [e for e in store().events("round:if+ij") if e.get("event") == "course_loop_rearm"]
        self.assertEqual((rearm_ev[-1].get("wave_parked"), rearm_ev[-1].get("wave_kept")), ("ir1,ir2", "ik"))
        for x in ("ir1", "ir2"):
            self.assertEqual(phase(x), "parked", f"{x} cannot produce a result: parked")
            self.assertEqual(store().dispatch(x), {}, f"{x} never ran")
        self.assertEqual(phase("in1"), "accepted", "the new route's row ran")
        inc = incident_for("round:if+ij", "round_review")
        self.assertIn("park the held rows", inc["summary"])
        bundle = (INST / "steward" / "incidents" / f"{inc['n']}.bundle.md").read_text()
        self.assertIn("infeasible: the remaining plan cannot reach the goal", bundle)
        self.assertIn("ir1, ir2, ik", bundle)
        self.assertIn("the bars that stay fixed", bundle)
        order = next((INST / "prompts").glob("rr-if+ij.*.md")).read_text()
        self.assertIn("NEW ROUTE to the SAME goal", order)
        self.assertIn("never propose to move a bar", order)
        # a plain row (no loop) closing infeasible: the Steward's incident with the replan steps; no reset of it
        add_lane("IG", [("gx", "-", 0, "mock", ""), ("gy", "gx", 0, "mock", "needs=go")])
        ctl("MOCK_gx", "CLOSED\nthe rest of this plan cannot show a result\ninfeasible\n")
        ctl("MOCK_VIA_CLI_gx")
        ctl("MOCKSW_gx", 'reset gx "run it once more"\n')
        ctl("MOCKSWV_gx", "INBOX\nno route inside the caps: the goal is the Owner's\n")
        orch("round", "new", "ig", "--lanes", "IG", "--phase", "IFP", check=True)
        orch("round", "start", "ig", check=True)
        wait(lambda: (incident_for("gx", "negative_outcome") or {}).get("status") == "closed", 200,
             what="gx's negative_outcome incident handled")
        inc = incident_for("gx", "negative_outcome")
        self.assertIn("the remaining plan cannot reach the goal", inc["summary"])
        bundle = (INST / "steward" / "incidents" / f"{inc['n']}.bundle.md").read_text()
        self.assertIn("## The remaining plan cannot reach the goal (outcome infeasible)", bundle)
        self.assertIn("the old route's wave: gy", bundle)
        sid = inc["sessions"][0]
        refused = [d for d in self._sw_done(sid) if d[0] == "reset"]
        self.assertTrue(refused and refused[0][2] == "refused" and "never re-run to obtain a GO" in refused[0][3],
                        f"the Steward's reset of an infeasible row: {refused}")
        self.assertEqual((phase("gx"), store().outcome("gx"), store().attempt("gx")), ("accepted", "infeasible", 1))
        self.assertEqual(store().dispatch("gy"), {}, "the GO-only dependent never ran")
        self.assertTrue(no_halt_page_attempted())
        self._close_phase("IFP", "IF", "IFN", "IG")

    def test_48_a_replan_never_moves_the_bar(self):
        """v3.1 (§15.6): (1) the loop's planner edits the judge's PLAN (the bar) — the loop stops before the re-arm,
        the judge is never re-run against the moved bar, the Owner's inbox says so; (2) a correction row that edits
        a protected key fails its close check every time, is parked, and the loop stops without re-arming; (3) the
        pin also covers the round's goal: and bars: files (a bars: entry removed later is still checked) and the
        judge's PLAN path; (4) `orch row add` cannot replace the judge's row."""
        import copy
        import courseloop
        import rowadd
        from config_parser import load
        from engine import Rig as Rig_
        cfg_set("preflight.reviewer", "mock")
        self.addCleanup(cfg_set, "preflight.reviewer", "opus")
        orch("phase", "open", "BMP", "--caps", "rows=40,loops=4", "bar drill", check=True)
        # (1) the planner moves the bar
        add_lane("BM", [("bw1", "-", 0, "mock", ""), ("bj", "bw1", 0, "mock", ""), ("bd", "bj", 0, "mock", "needs=go")])
        (WS / "PHASE_BM").mkdir(parents=True, exist_ok=True)
        (WS / "PHASE_BM" / "BARS.md").write_text("bar: 11 of 11 keyed lines verified\n")
        ctl("MOCK_bj", "CLOSED\n9 of 11: the bar is not met\nnogo\n")
        ctl("MOCK_VIA_CLI_bj")
        orch("round", "new", "bm", "--lanes", "BM", "--phase", "BMP", check=True)
        self._round_yaml("bm", judge="bj", on_fail="course_loop", max_loops=2, bars=["PHASE_BM/BARS.md"])
        self._loop_scripts("bm", "bm-l1", "BMF", "bf1", phase="BMP",
                           plan_pre='printf "\\n## Amendment\\n9 of 11 is enough\\n" >> PHASE_BM/bj/PLAN.md\n')
        orch("round", "start", "bm", check=True)
        wait(lambda: self._loop("bm").get("state") == "stopped", 300, what="the loop stops on the moved bar")
        rec = self._loop("bm")
        self.assertIn("the bar moved under the loop", rec["current"]["stopped"])
        self.assertIn("PHASE_BM/bj/PLAN.md", rec["current"]["stopped"])
        self.assertIn("plan:PHASE_BM/bj/PLAN.md", rec["current"]["pin"])
        self.assertIn("bar:PHASE_BM/BARS.md", rec["current"]["pin"])
        self.assertTrue(any(e.get("event") == "course_loop_bar_moved" for e in store().events("round:bm")))
        time.sleep(4)
        self.assertEqual(store().attempt("bj"), 1, "the judge is never re-armed against a moved bar")
        self.assertFalse(any(e.get("event") == "reset" for e in events("bj")))
        self.assertEqual(store().dispatch("bd"), {})
        self.assertIn("course loop bm #1 stopped", (INST / "steward" / "INBOX.md").read_text())
        # (3) the pin, unit by unit: the goal:, a bars: file (still checked once the entry is removed), the PLAN path
        cfg = load(str(INST))
        (WS / "PHASE_BM" / "bj" / "PLAN.md").write_text(PLAN.format(id="bj"))
        conf = courseloop.loops_of(cfg)["bm"]
        pinned = courseloop.pin(cfg, conf)
        self.assertEqual(courseloop.bar_moved(cfg, conf, pinned), [])
        (WS / "PHASE_BM" / "BARS.md").write_text("bar: 9 of 11\n")
        self.assertEqual(courseloop.bar_moved(cfg, conf, pinned), ["bar PHASE_BM/BARS.md"])
        self._round_yaml("bm", bars=[])
        self.assertEqual(courseloop.bar_moved(load(str(INST)), conf, pinned), ["bar PHASE_BM/BARS.md"],
                         "a bars: entry removed after the pin is still checked")
        (WS / "PHASE_BM" / "BARS.md").write_text("bar: 11 of 11 keyed lines verified\n")
        self._round_yaml("bm", goal={"name": "verified", "target": 9})
        self.assertEqual(courseloop.bar_moved(load(str(INST)), conf, pinned), ["round.yaml goal:"])
        self._round_yaml("bm", goal=None)
        c2 = dict(conf, judge="bw1")                   # the judge's queue line now names another PLAN
        self.assertTrue(any("PLAN path" in m for m in courseloop.bar_moved(cfg, c2, pinned)))
        # (4) the judge's row is never replaced through `orch row add`
        who = {"sid": "pl-bm-l1-9", "kind": "planner", "lanes": ["BM"], "grants": [], "round": "bm"}
        (WS / "PHASE_BM" / "bj2").mkdir(parents=True, exist_ok=True)
        (WS / "PHASE_BM" / "bj2" / "PLAN.md").write_text(PLAN.format(id="bj"))
        ok, why, _ = rowadd.validate(cfg, store(), who, "BM", "bj bw1 0 mock PHASE_BM/bj2/PLAN.md")
        self.assertFalse(ok, "a line never replaces the judge (and its bar)")
        # (5) the pin after the re-arm (state judging), between loops, the Owner's adoption, bar_pin off — on an
        # instance of its own
        c3, r3 = self._mini("u48", {"BJ": [("pj", "-", ""), ("pd", "pj", "needs=go")]},
                            {"pb": "tag: pb\nstatus: open\nlanes: [BJ]\njudge: pj\non_fail: course_loop\nmax_loops: 3\n"})
        conf3 = courseloop.loops_of(c3)["pb"]
        pinned = courseloop.pin(c3, conf3)
        plan = WS / "PHASE_BJ" / "pj" / "PLAN.md"
        rec = {"tag": "pb", "judge": "pj", "loops": 1, "state": "judging", "phase": "", "history": [],
               "handled_sig": "nogo|0|100",
               "current": {"n": 1, "outcome": "nogo", "sig": "nogo|0|100", "started_epoch": int(time.time()) - 60,
                           "pin": pinned, "pin0": pinned}}
        self.assertFalse(courseloop._judging_bar_check(r3, "pb", conf3, rec), "an unchanged bar: nothing happens")
        plan.write_text(plan.read_text() + "\n## Amendment\nthe bar is lowered\n")
        self.assertTrue(courseloop._judging_bar_check(r3, "pb", conf3, rec))
        self.assertEqual((rec["state"], r3.st.phase("pj")), ("stopped", "parked"),
                         "a re-armed judge not yet run is parked: it never measures a moved bar")
        self.assertIn("parked before it ran", rec["current"]["stopped"])
        import steward
        for act in ("unpark", "reset"):
            ok, why, _ = steward.execute(r3, "sw-996-1", act, ["pj"], "run it anyway", {"incident": 996})
            self.assertFalse(ok, f"a Steward's {act} of a bar-moved judge")
            self.assertIn("only the Owner unparks it", why)
        r3.st.write_result("pj", {"phase": "accepted", "outcome": "nogo", "status_mtime": 300})
        courseloop._trigger(r3, "pb", conf3, rec)
        self.assertEqual((rec["state"], rec["loops"]), ("stopped", 1), "no loop starts on a moved bar")
        r3.st.event("pj", "reset")                         # the Owner adopts the new bar: `orch reset pj`
        time.sleep(1.1)
        r3.st.write_result("pj", {"phase": "accepted", "outcome": "nogo", "status_mtime": 400})
        with mock.patch.object(courseloop, "_dispatch_review", lambda *a, **k: None):
            courseloop._trigger(r3, "pb", conf3, rec)
        self.assertEqual((rec["state"], rec["loops"]), ("review", 2), "after the Owner's reset the loop runs again")
        self.assertNotIn("bar_hold", rec)
        self.assertEqual(rec["current"]["pin0"], courseloop.pin(c3, conf3), "the adopted bar is the chain's new pin")
        rec["state"] = "judging"                           # loop 2's re-arm, then the bar moves before the next claim
        plan.write_text(plan.read_text() + "and lowered again\n")
        r3.st.write_result("pj", {"phase": "accepted", "outcome": "nogo", "status_mtime": 500})
        courseloop._trigger(r3, "pb", conf3, rec)
        self.assertEqual((rec["state"], rec["loops"]), ("stopped", 2), "a moved bar between loops starts no loop")
        c4 = copy.copy(c3)
        c4.data = dict(c3.data, steward=dict(c3.data["steward"], bar_pin=False))
        rec4 = dict(rec, state="judging", bar_hold=None)
        self.assertFalse(courseloop._judging_bar_check(Rig_(c4), "pb", conf3, rec4), "steward.bar_pin: false — no pin")
        # (2) a correction row edits a protected key: its claim fails the close check, it parks, nothing re-arms
        import yaml
        cur = (yaml.safe_load((INST / "orch.yaml").read_text()).get("guard") or {}).get("protected_paths") or []
        cur = [cur] if isinstance(cur, str) else list(cur)
        cfg_set("guard.protected_paths", cur + ["PHASE_BK/keys/"])
        try:
            (WS / "PHASE_BK" / "keys").mkdir(parents=True, exist_ok=True)
            (WS / "PHASE_BK" / "keys" / "answer.json").write_text('{"line": 1}\n')
            self.assertEqual(orch("commit", "plan:setup", "-m", "operator: a key", "--",
                                  "PHASE_BK/keys/answer.json").returncode, 0)
            add_lane("BK", [("bkw", "-", 0, "mock", ""), ("bkj", "bkw", 0, "mock", ""),
                            ("bkd", "bkj", 0, "mock", "needs=go")])
            ctl("MOCK_bkj", "CLOSED\nthe bar is not met\nnogo\n")
            ctl("MOCK_VIA_CLI_bkj")
            ctl("MOCK_RUN_bkf", f"cd '{WS}'\nprintf '{{\"line\": 2}}\\n' > PHASE_BK/keys/answer.json\n")
            ctl("MOCKSWV_bkf", "PARKED\na correction may not edit a key (W)\n")
            orch("round", "new", "bk", "--lanes", "BK", "--phase", "BMP", check=True)
            self._round_yaml("bk", judge="bkj", on_fail="course_loop", max_loops=1)
            self._loop_scripts("bk", "bk-l1", "BKF", "bkf", phase="BMP")
            orch("round", "start", "bk", check=True)
            wait(lambda: self._loop("bk").get("state") == "stopped", 400, what="the loop stops: its only row parked")
            fails = [e for e in events("bkf") if e["event"] == "check_failed"]
            self.assertTrue(fails and all("PHASE_BK/keys/answer.json" in str(e.get("detail", "")) for e in fails), fails)
            self.assertNotIn("accepted", [e["event"] for e in events("bkf")])
            self.assertEqual(phase("bkf"), "parked")
            self.assertIn("every row of the correction was parked", self._loop("bk")["current"]["stopped"])
            self.assertEqual(store().attempt("bkj"), 1)
            self.assertEqual(store().dispatch("bkd"), {})
        finally:
            cfg_set("guard.protected_paths", cur)
            git("checkout", "--", "PHASE_BK/keys/answer.json")
        self.assertTrue(no_halt_page_attempted())
        self._close_phase("BMP", "BM", "BMF", "BK", "BKF")

    def test_49_a_session_round_loops_only_within_the_phase_cap(self):
        """v3.1 (§15.5): a round a session created (`by:`) declares a checkpoint loop. While its phase does not allow
        session loops the declaration is refused (the NO-GO goes to the Steward's incident); only the Owner's
        `orch phase open --session-loops on` (refused from a session) allows it. The loop then counts in the phase's
        loops — never its replans — and at the loops cap the phase rests, whatever max_loops the round wrote."""
        import phases
        from config_parser import load
        cfg_set("preflight.reviewer", "mock")
        self.addCleanup(cfg_set, "preflight.reviewer", "opus")
        orch("phase", "open", "SLP", "--caps", "rows=40,loops=1,replans=3", "session-round drill", check=True)
        add_lane("SL", [("slw", "-", 0, "mock", ""), ("sj", "slw", 0, "mock", ""), ("sd", "sj", 0, "mock", "needs=go")])
        ctl("MOCK_sj", "CLOSED\nthe wave's bar is not met\nnogo\n")
        ctl("MOCK_VIA_CLI_sj")
        ctl("MOCKSWV_sj", "RESOLVED\nnoted: no loop is allowed for a session's round here\n")
        orch("round", "new", "sl", "--lanes", "SL", "--phase", "SLP", check=True)
        self._round_yaml("sl", by="pl-sl-1", judges=["sj"], on_fail="course_loop", max_loops=5)
        self.assertIn("does not allow session loops", orch("round", "status", "sl").stdout)
        self.assertIn("does not allow session loops", orch("lint").stdout)
        self._loop_scripts("sl+sj", "sl-sj-l1", "SLF", "sf1", phase="SLP")
        orch("round", "start", "sl", check=True)
        wait(lambda: incident_for("sj", "negative_outcome") is not None, 120,
             what="the session round's NO-GO goes to the Steward, not a loop")
        self.assertEqual(self._loop("sl+sj").get("loops", 0), 0)
        p = orch("phase", "open", "SLP", "--session-loops", "on", "a row tries", env=dict(ENV, ORCH_ROW="sj", ORCH_KIND="row"))
        self.assertIn("REFUSED", p.stdout, "a session never allows its own loops")
        self.assertFalse(phases.load(load(str(INST)))["SLP"].get("session_loops"))
        orch("phase", "open", "SLP", "--session-loops", "on", "the Owner allows session loops here", check=True)
        self.assertIn("session loops on", orch("phase", "status").stdout)
        wait(lambda: self._loop("sl+sj").get("state") == "rested", 300,
             what="loop 1, the re-armed checkpoint's nogo, the loops cap")
        rec = self._loop("sl+sj")
        self.assertEqual(rec["loops"], 1)
        self.assertNotIn("counter", rec["current"], "a session's round counts in loops, never replans")
        ph = phases.load(load(str(INST)))["SLP"]
        self.assertEqual((ph["state"], ph["loops"], ph.get("replans", 0)), ("rest", 1, 0))
        self.assertIn("loops cap (1/1)", ph.get("rest_reason", ""))
        self.assertIn("course loop sl+sj: sj still nogo — no loop left", (INST / "steward" / "INBOX.md").read_text())
        self.assertEqual(store().attempt("sj"), 2)
        self.assertEqual(store().dispatch("sd"), {})
        self.assertTrue(no_halt_page_attempted())
        self._close_phase("SLP", "SL", "SLF")

    def test_50_legacy_judge_round_and_record_unchanged(self):
        """v3.1 keeps v3's round judge as it was: a round with `judge:` keeps its record name, status line and
        events, and a record written by v3 loads and is left byte for byte while a checkpoint of the same round
        (`judges:`) loops beside it under its own key."""
        import courseloop
        from config_parser import load
        cfg_set("preflight.reviewer", "mock")
        self.addCleanup(cfg_set, "preflight.reviewer", "opus")
        orch("phase", "open", "LGP", "--caps", "rows=20,loops=3", "legacy drill", check=True)
        add_lane("LG", [("lgw", "-", 0, "mock", ""), ("lgj", "lgw", 0, "mock", ""), ("lgc", "lgw", 0, "mock", "")])
        ctl("MOCK_lgj", "CLOSED\nthe bar is met\ngo\n")
        ctl("MOCK_VIA_CLI_lgj")
        ctl("MOCK_lgc", "CLOSED\nthe wave's bar is not met\nnogo\n")
        ctl("MOCK_VIA_CLI_lgc")
        orch("round", "new", "lg", "--lanes", "LG", "--phase", "LGP", check=True)
        self._round_yaml("lg", judge="lgj", judges=["lgc"], on_fail="course_loop", max_loops=1)
        legacy = {"tag": "lg", "judge": "lgj", "loops": 1, "state": "done", "handled_sig": "nogo|1|1792400000",
                  "held": "", "phase": "LGP", "updated": "2026-10-19 10:00:00",
                  "current": {"n": 1, "outcome": "nogo", "sig": "nogo|1|1792400000", "started": "2026-10-19 09:00:00",
                              "started_epoch": 1792400000, "judge_summary": "the bar is not met", "rr_epoch": 1792400010,
                              "rr_tries": 1, "counted": True, "incident": 7, "next": "lg-l1", "planner": "pl-lg-l1-3",
                              "rows": ["lgf1"], "rearmed": "2026-10-19 09:40:00"},
                  "history": ["2026-10-19 09:00:00 loop 1/1 starts — lgj closed nogo"]}
        f = INST / "state" / "courseloop" / "lg.json"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(legacy, indent=1, sort_keys=True))
        before = f.read_bytes()
        self.assertIn("course loop: judge lgj, loops 1/1, state done", orch("round", "status", "lg").stdout)
        orch("round", "start", "lg", check=True)
        wait(lambda: self._loop("lg+lgc").get("loops") == 1, 120, what="the checkpoint's own loop")
        wait(lambda: phase("lgj") == "accepted", 120, what="the round judge's GO")
        time.sleep(4)
        self.assertEqual(f.read_bytes(), before, "the v3 record is left byte for byte")
        st = orch("round", "status", "lg").stdout
        self.assertIn("course loop: judge lgj, loops 1/1, state done", st)
        self.assertIn("course loop: checkpoint lgc, loops 1/1", st)
        self.assertFalse(any(e.get("event") == "course_loop_start" for e in store().events("round:lg")),
                         "the round judge's events are untouched")
        self.assertTrue(any(e.get("event") == "course_loop_start" for e in store().events("round:lg+lgc")))
        self.assertEqual(courseloop.judges(load(str(INST)))["lgj"], "lg")
        self.assertEqual(courseloop.judges(load(str(INST)))["lgc"], "lg+lgc")
        self.assertEqual(courseloop.next_tag("lg", 2), "lg-l2", "the v3 correction tag is unchanged")
        self.assertEqual(courseloop.next_tag("lg+lgc", 1), "lg-lgc-l1")
        # a v3 round whose judge is not a row yet reads exactly as before (the live PV2 p2 case)
        (INST / "rounds" / "lz").mkdir(parents=True, exist_ok=True)
        (INST / "rounds" / "lz" / "round.yaml").write_text(
            "tag: lz\nstatus: open\nlanes: [LZ]\njudge: lz4\non_fail: course_loop\nmax_loops: 2\n")
        self.addCleanup(shutil.rmtree, INST / "rounds" / "lz", True)
        self.assertIn("course loop: OFF — round lz: judge lz4 is not a row", orch("round", "status", "lz").stdout)
        self._close_phase("LGP", "LG")

    def test_51_the_loop_cap_still_rests_the_phase(self):
        """A checkpoint's loops count in caps.loops when the phase sets no replans cap: at the cap the phase rests and
        one inbox item says why (the round's max_loops left over does not matter). With caps.replans the checkpoint
        counts there instead: at the replans cap the phase rests, its loops count untouched; the round's judge still
        counts in loops. The cap counts loops ALLOWED (> rests), as in v3."""
        import copy
        import courseloop
        import phases
        from config_parser import load
        from engine import Rig
        cfg_set("preflight.reviewer", "mock")
        self.addCleanup(cfg_set, "preflight.reviewer", "opus")
        orch("phase", "open", "XCP", "--caps", "rows=40,loops=1", "loops cap drill", check=True)
        add_lane("XC", [("xw", "-", 0, "mock", ""), ("xj", "xw", 0, "mock", ""), ("xd", "xj", 0, "mock", "needs=go")])
        ctl("MOCK_xj", "CLOSED\nthe wave's bar is not met\nnogo\n")
        ctl("MOCK_VIA_CLI_xj")
        orch("round", "new", "xc", "--lanes", "XC", "--phase", "XCP", check=True)
        self._round_yaml("xc", judges=[{"row": "xj", "max_loops": 3}], on_fail="course_loop")
        self._loop_scripts("xc+xj", "xc-xj-l1", "XCF", "xf1", phase="XCP")
        orch("round", "start", "xc", check=True)
        wait(lambda: self._loop("xc+xj").get("state") == "rested", 300, what="loop 1, nogo again, the loops cap")
        ph = phases.load(load(str(INST)))["XCP"]
        self.assertEqual((ph["state"], ph["loops"]), ("rest", 1))
        self.assertIn("loops cap (1/1)", ph["rest_reason"])
        self.assertEqual(self._loop_events("xc+xj").count("start"), 1, "max_loops 3 left, but the phase cap is 1")
        self.assertIn("course loop xc+xj: xj still nogo — no loop left", (INST / "steward" / "INBOX.md").read_text())
        self.assertEqual(store().dispatch("xd"), {})
        self._close_phase("XCP", "XC", "XCF")
        # with caps.replans: units on an instance of their own (the live engine never sees these records)
        cfg = load(str(INST))
        c2 = copy.copy(cfg)
        c2.instance = SBX / "u51"
        for a in ("state", "control", "leases", "prompts", "unblock"):
            setattr(c2, a, c2.instance / a)
            getattr(c2, a).mkdir(parents=True, exist_ok=True)
        c2.templates_local = c2.prompts
        shutil.copy(INST / "orch.yaml", c2.instance / "orch.yaml")
        for rid in ("xrj", "xrf"):
            (WS / "PHASE_XR" / rid).mkdir(parents=True, exist_ok=True)
            (WS / "PHASE_XR" / rid / "PLAN.md").write_text(PLAN.format(id=rid))
        (c2.instance / "queue_XR.tsv").write_text("xrj\t-\t0\tmock\tPHASE_XR/xrj/PLAN.md\n"
                                                  "xrf\t-\t0\tmock\tPHASE_XR/xrf/PLAN.md\n")
        (c2.instance / "rounds" / "xr").mkdir(parents=True, exist_ok=True)
        (c2.instance / "rounds" / "xr" / "round.yaml").write_text(
            "tag: xr\nstatus: open\nlanes: [XR]\njudge: xrf\njudges: [xrj]\non_fail: course_loop\nmax_loops: 4\n")
        e2 = Rig(c2)
        ok, msg = phases.open_phase(c2, "XRP", "loops=5,replans=1", ["xr"], "replans drill")
        self.assertTrue(ok, msg)
        good = courseloop.loops_of(c2)
        ph = phases.load(c2)["XRP"]
        self.assertEqual(courseloop.counter_for(good["xr+xrj"], ph), "replans")
        self.assertEqual(courseloop.counter_for(good["xr"], ph), "loops", "the round's judge stays in loops")
        self.assertEqual(phases.record_loop(c2, "XRP", counter="replans"), 1)
        self.assertEqual(phases.cap_reached(c2, "XRP", phases.load(c2)["XRP"]), "", "the loop that reaches the cap runs")
        e2.st.write_result("xrj", {"phase": "accepted", "outcome": "nogo", "status_mtime": 500})
        rec = {"tag": "xr+xrj", "loops": 1, "state": "judging", "history": []}
        courseloop._trigger(e2, "xr+xrj", good["xr+xrj"], rec)
        self.assertEqual(rec["state"], "rested")
        ph = phases.load(c2)["XRP"]
        self.assertEqual((ph["state"], ph["replans"], ph["loops"]), ("rest", 1, 0))
        self.assertIn("replans cap (1/1)", ph["rest_reason"])
        self.assertEqual(phases.record_loop(c2, "XRP", counter="replans"), 2)
        self.assertIn("replans cap (2/1", phases.cap_reached(c2, "XRP", phases.load(c2)["XRP"]))
        # the real start path: a checkpoint's next negative claim, the phase re-capped by the Owner, counts in replans
        ok, msg = phases.open_phase(c2, "XRP", "replans=3", [], "the Owner raises the replans cap")
        self.assertTrue(ok, msg)
        e2.st.write_result("xrj", {"phase": "accepted", "outcome": "nogo", "status_mtime": 700})
        with mock.patch.object(courseloop, "_dispatch_review", lambda *a, **k: None):
            courseloop._trigger(e2, "xr+xrj", courseloop.loops_of(c2)["xr+xrj"], rec)
        ph = phases.load(c2)["XRP"]
        self.assertEqual((rec["state"], rec["loops"], rec["current"]["counter"]), ("review", 2, "replans"))
        self.assertEqual((ph["replans"], ph["loops"]), (3, 0), "the checkpoint's loop counted in replans, not loops")
        self.assertFalse((INST / "state" / "courseloop" / "xr+xrj.json").exists(), "the live rig saw none of it")

    def test_52_adaptive_plan_units(self):
        """v3.1 units: the infeasible outcome in the CLI, the seal and both close parsers; no session re-runs an
        infeasible row (Steward reset/retry, `orch rearm`, a `rearm=` grant); a session may hand on a checkpoint
        judge's grant while it holds no negative outcome but never the round judge's; refused `judges:` entries;
        the phase keys (validate, approve diff, session refusal); the review folder of a checkpoint key."""
        import copy
        import courseloop
        import phases
        import rearm
        import rounds
        import rowadd
        import steward
        from config_parser import load
        from engine import Rig, close_pattern
        from store import norm_outcome
        # the outcome word
        self.assertEqual(norm_outcome("PLAN_INFEASIBLE"), "infeasible")
        self.assertEqual(norm_outcome("Infeasible"), "infeasible")
        for line in ("ORCH_CLOSE a1 CLOSED:infeasible — no route left", "ORCH_CLOSE a1 CLOSED: infeasible - no route left"):
            m = close_pattern("a1", "row").match(line)
            self.assertEqual((m.group(1), norm_outcome(m.group(2)), m.group(3).strip()), ("CLOSED", "infeasible", "no route left"))
            out = subprocess.run(["bash", "-c", '. "$1"; cl=$(closing_line a1 "$2"); v=$(closing_verdict "$cl"); '
                                  'printf "%s|%s" "$v" "$(closing_outcome "$cl" "$v")"', "x",
                                  str(HOME / "lib" / "closing.sh"), line], capture_output=True, text=True).stdout
            self.assertEqual(out, "CLOSED|infeasible", line)
        add_lane("UV", [("ux1", "-", 0, "mock", ""), ("ux2", "ux1", 0, "mock", "needs=go"),
                        ("uxj", "-", 0, "mock", ""), ("uxf", "-", 0, "mock", "")])
        (INST / "control" / "RUN_UV").unlink(missing_ok=True)
        p = orch("done", "ux1", "the rest cannot show a result", "--outcome", "maybe")
        self.assertIn("REFUSED", p.stdout + p.stderr)
        cfg = load(str(INST))
        rig = Rig(cfg)
        rig.st.write_result("ux1", {"phase": "accepted", "outcome": "infeasible", "status_mtime": 100})
        self.assertIn("never re-run to obtain a GO", courseloop.owns_negative(cfg, "ux1"))
        for act, args in (("reset", ["ux1"]), ("retry", ["ux1", "mock2"])):
            ok, why, _ = steward.execute(rig, "sw-997-1", act, args, "get a GO", {"incident": 997})
            self.assertFalse(ok, act)
            self.assertIn("infeasible", why)
        rig.st.write_dispatch("ux2", {"sid": "ux2", "open": True, "snapshot": {"meta": {"rearm": "ux1,round"}}})
        ok, why = rearm.validate(rig, "ux2", "ux1")
        self.assertFalse(ok)
        self.assertIn("infeasible", why)
        # grants: a checkpoint judge may be handed on while it holds no negative outcome; the round judge never
        (INST / "rounds" / "uv").mkdir(parents=True, exist_ok=True)
        (INST / "rounds" / "uv" / "round.yaml").write_text(
            "tag: uv\nstatus: open\nlanes: [UV]\njudge: uxf\njudges: [uxj]\non_fail: course_loop\n")
        self.addCleanup(shutil.rmtree, INST / "rounds" / "uv", True)
        cfg = load(str(INST))
        self.assertEqual(courseloop.final_judges(cfg).get("uxf"), "uv")
        self.assertNotIn("uxj", courseloop.final_judges(cfg))
        for rid in ("uv1", "uv2", "uv3", "uv4"):
            (WS / "PHASE_UV" / rid).mkdir(parents=True, exist_ok=True)
            (WS / "PHASE_UV" / rid / "PLAN.md").write_text(PLAN.format(id=rid))
        who = {"sid": "ux2", "kind": "row", "grants": ["uxj", "uxf", "ux1", "round"], "lanes": ["UV"],
               "allow": [str((WS / "PHASE_UV").resolve())]}
        import phases as _ph
        with mock.patch.object(rowadd, "phase_check", return_value=""):
            ok, why, _ = rowadd.validate(cfg, rig.st, who, "UV", "uv1 - 0 mock PHASE_UV/uv1/PLAN.md rearm=uxj,round")
            self.assertTrue(ok, f"a checkpoint grant with no negative outcome is handed on: {why}")
            ok, why, _ = rowadd.validate(cfg, rig.st, who, "UV", "uv2 - 0 mock PHASE_UV/uv2/PLAN.md rearm=uxf")
            self.assertFalse(ok)
            self.assertIn("names a course-loop judge", why)
            ok, why, _ = rowadd.validate(cfg, rig.st, who, "UV", "uv3 - 0 mock PHASE_UV/uv3/PLAN.md rearm=ux1")
            self.assertFalse(ok)
            self.assertIn("infeasible", why)
            rig.st.write_result("uxj", {"phase": "accepted", "outcome": "nogo", "status_mtime": 100})
            ok, why, _ = rowadd.validate(cfg, rig.st, who, "UV", "uv4 - 0 mock PHASE_UV/uv4/PLAN.md rearm=uxj")
            self.assertFalse(ok, "never while the checkpoint holds a negative outcome")
            self.assertIn("never re-run to obtain a GO", why)
        rig.st.write_dispatch("ux2", {"sid": "ux2", "open": True, "snapshot": {"meta": {"rearm": "uxj"}}})
        ok, why = rearm.validate(rig, "ux2", "uxj")
        self.assertFalse(ok)
        self.assertIn("checkpoint", why)
        rig.st.write_dispatch("ux2", {"sid": "ux2", "open": False})
        # refused judges: entries — each alone; the others still loop
        bad_round = {"tag": "uv", "status": "open", "lanes": ["UV"], "judge": "uxf", "on_fail": "course_loop",
                     "judges": ["uxj", "uxj", "uxf", "a1", "nope", 7, {"row": "ux2", "max_loops": -1}]}
        res = courseloop.checkpoint_configs(cfg, "uv", bad_round)
        whys = [w for c, w, k in res if not c]
        self.assertEqual([c["judge"] for c, w, k in res if c], ["uxj"])
        for want in ("listed twice", "the judge: wins", "not one of the round's lanes", "not a row",
                     "is not a row id or", "max_loops must be"):
            self.assertTrue(any(want in w for w in whys), f"{want}: {whys}")
        self.assertIn("cannot loop", courseloop.config(cfg, "a+b", dict(bad_round, judges=[]))[1])
        self.assertEqual(courseloop.checkpoint_configs(cfg, "uv", dict(bad_round, judges="uxj"))[0][1].split(":")[0],
                         "round uv")
        # a checkpoint's review folder; a real round's tag is itself
        self.assertEqual(rounds.review_dir(cfg, "uv+uxj"), INST / "rounds" / "uv" / "review+uxj")
        self.assertEqual(rounds.review_dir(cfg, "uv"), INST / "rounds" / "uv" / "review")
        # the phase keys
        ph, probs = phases.validate({"phases": {"Z": {"state": "open", "caps": {"replans": 2}, "replans": "x"}}})
        self.assertTrue(any("replans must be a whole number" in p for p in probs))
        ph, probs = phases.validate({"phases": {"Z": {"state": "open", "session_loops": "yes"}}})
        self.assertTrue(any("session_loops must be true or false" in p for p in probs))
        ph, probs = phases.validate({"phases": {"Z": {"state": "open", "caps": {"loops": 2}, "loops": 1}}})
        self.assertEqual((probs, "replans" in ph["Z"], "session_loops" in ph["Z"]), ([], False, False),
                         "a v3 phase reads back with no v3.1 key added")
        self.assertIn("session_loops off -> on", " ".join(phases.describe_diff(
            {"Z": {"state": "open", "caps": {}}}, {"Z": {"state": "open", "caps": {}, "session_loops": True}})))
        self.assertIn("--session-loops on|off", orch("phase", "open", "Z9", "--session-loops", "maybe").stdout)
        rig.st.write_result("ux1", {"phase": "parked", "parked_by": "owner", "park_reason": "unit drill"})
        rig.st.write_result("uxj", {"phase": "parked", "parked_by": "owner", "park_reason": "unit drill"})
        # a session's round never takes the Owner's judge or lane; the final-judge guard follows every declaration
        c5, r5 = self._mini("u52a", {"OA": [("oaj", "-", ""), ("oaw", "-", "")], "OS": [("osj", "-", "")]},
                            {"oa": "tag: oa\nstatus: open\nlanes: [OA]\njudge: oaj\non_fail: course_loop\n",
                             "sx": "tag: sx\nstatus: open\nby: pl-sx-1\nlanes: [OA, OS]\njudge: oaj\njudges: [osj]\n"
                                   "on_fail: course_loop\n"})
        ok, msg = phases.open_phase(c5, "SP5", "loops=2", ["oa", "sx"], "session loops allowed", session_loops=True)
        self.assertTrue(ok, msg)
        good, bad = courseloop.configs(c5)
        self.assertEqual(sorted(good), ["oa", "sx+osj"], "the Owner's loop stays; the session's own checkpoint loops")
        self.assertIn("loops only on lanes of its own", bad["sx"])
        self.assertEqual(courseloop.final_judges(c5), {"oaj": "oa"})
        self.assertEqual(courseloop.judges(c5)["osj"], "sx+osj")
        # two Owner rounds naming one judge: neither loops, the grant guard stays
        c6, r6 = self._mini("u52b", {"DJ": [("dj", "-", "")]},
                            {"d1": "tag: d1\nstatus: open\nlanes: [DJ]\njudge: dj\non_fail: course_loop\n",
                             "d2": "tag: d2\nstatus: open\nlanes: [DJ]\njudge: dj\non_fail: course_loop\n"})
        self.assertEqual(courseloop.loops_of(c6), {})
        self.assertIn("dj", courseloop.final_judges(c6), "one judge, one loop switches the loop off, never the guard")
        # the held wave: never a judge; serial lanes' implicit deps count
        c7, r7 = self._mini("u52c", {"HW": [("hj", "-", ""), ("hd", "hj", "needs=go"), ("hx", "-", ""),
                                            ("hf", "hd", "")]},
                            {"hw": "tag: hw\nstatus: open\nlanes: [HW]\njudge: hf\njudges: [hj]\non_fail: course_loop\n"},
                            serial=True)
        r7.st.write_result("hj", {"phase": "accepted", "outcome": "infeasible", "status_mtime": 100})
        self.assertEqual(courseloop.held_wave(c7, "hj", r7.st), ["hd", "hx"],
                         "hx waits below hd in a serial lane; the final judge hf is never offered for parking")
        # judges: without on_fail is reported, not silent
        c8, r8 = self._mini("u52d", {"NJ": [("nj", "-", "")]}, {"nf": "tag: nf\nstatus: open\nlanes: [NJ]\njudges: [nj]\n"})
        self.assertIn("without on_fail: course_loop", courseloop.configs(c8)[1]["nf+judges"])
        # one loop's failure never starves the others
        c9, r9 = self._mini("u52e", {"TA": [("taj", "-", "")], "TB": [("tbj", "-", "")]},
                            {"ta": "tag: ta\nstatus: open\nlanes: [TA]\njudge: taj\non_fail: course_loop\n",
                             "tb": "tag: tb\nstatus: open\nlanes: [TB]\njudge: tbj\non_fail: course_loop\n"})
        real = courseloop._trigger

        def boom(rig_, key, conf, rec):
            if key == "ta":
                raise PermissionError("unreadable bar")
            return real(rig_, key, conf, rec)
        with mock.patch.object(courseloop, "_trigger", boom):
            courseloop.tick(r9)
        self.assertTrue(any(e.get("event") == "course_loop_error" for e in r9.st.events("round:ta")))
        self.assertTrue((c9.state / "courseloop" / "tb.json").is_file(), "round tb's loop was still walked")
        self.assertTrue(courseloop._sha_path(WS / "nope-not-here") == "absent")

    def test_53_a_parked_row_is_never_redispatched(self):
        """A row the Owner parks while it runs, whose window is then closed, is recorded dead and stays parked: the
        dead-window retier does not re-dispatch it (on any tier), nothing halts or pages. Every other re-dispatch
        path holds a parked row too (interruption, close-check refusal, review reject, max-unblock retier,
        unblocker RESOLVED), and a claim written after the park does not lift it. A retier's re-dispatch waits
        while the lane is paused (day mode), as any dispatch does."""
        import steward
        from config_parser import load
        from engine import Engine
        from rows import find_row
        self.addCleanup(self._phases_off)
        self._phases_off()
        # (1) the drill: park a running row, close its window
        ctl("MOCK_HANG_pk1")
        add_lane("PK", [("pk1", "-", 0, "mock", "")])
        self.addCleanup(lambda: (INST / "control" / "RUN_PK").unlink(missing_ok=True))
        orch("start", "PK", check=True)
        wait(lambda: store().dispatch("pk1").get("open"), 60, what="pk1 running")
        p = orch("park", "pk1", "drill: free its slot; it waits for a fix")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        t_park = time.time()
        subprocess.run(["tmux", "kill-window", "-t", f"={SESSION}:=pk1"], capture_output=True)
        wait(lambda: any(e.get("event") == "dead" for e in events("pk1")), 60, what="pk1's dead window recorded")
        time.sleep(6)                                       # several engine ticks: a retier would have dispatched
        evs = events("pk1")
        self.assertEqual(phase("pk1"), "parked")
        self.assertEqual([e for e in evs if e.get("event") == "dispatch" and float(e.get("epoch", 0)) >= t_park], [],
                         "no dispatch after the park")
        self.assertEqual(store().attempt("pk1"), 1)
        self.assertFalse(store().dispatch("pk1").get("open"))
        for ev in ("retier", "halt"):
            self.assertNotIn(ev, [e.get("event") for e in evs], f"no {ev} for a parked row")
        self.assertFalse((INST / "control" / "EXECUTOR_pk1").exists(), "no retier override")
        self.assertTrue([e for e in evs if e.get("event") == "dead" and e.get("parked")], "the death is recorded")
        self.assertFalse([n for n in notifications(t_park) if "pk1" in n.get("title", "")], "nothing pages")
        self.assertIn("while PARKED", engine_tail(200))

        # (2) every other re-dispatch path, on rows of a lane that never starts
        add_lane("PU", [(f"pu{i}", "-", 0, "mock", "") for i in range(1, 7)])
        cfg = load(str(INST))
        e = Engine(cfg)
        rows = {r: find_row(cfg, r) for r in (f"pu{i}" for i in range(1, 7))}
        for r in rows:
            e.st.write_dispatch(r, {"sid": r, "kind": "row", "lane": "PU", "executor": "mock", "attempt": 1,
                                    "epoch": int(time.time()) - 300, "open": False})
            self.assertTrue(steward.park(e, r, "unit drill", by="sw-953-1"))
        e.st.write_status("pu1", "CLOSED", "a claim written after the park")
        self.assertEqual(e.st.phase("pu1"), "parked", "a newer claim does not lift a park")
        e.on_dead("pu2", "row", e.st.dispatch("pu2"), "1", rows)                 # dead window (retier policy)
        e.on_interrupted("pu3", "row", e.st.dispatch("pu3"))                     # reboot / tmux restart
        e.on_review_verdict(rows["pu4"], dict(e.st.result("pu4"), attempt=1), "rv-pu4-1", "REJECT", "drill")
        e.st.write_int("pu5.unblock_n", 1)
        e.on_blocked(rows["pu5"], "mock", "drill: past max unblock")             # max-unblock retier
        e.on_unblock_verdict(rows["pu6"], e.st.result("pu6"), "ub-pu6-1", 1, "RESOLVED", "drill")
        res = e.st.result("pu1")
        (INST / "state" / "pu1.check.json").write_text(json.dumps(
            {"ok": False, "status_mtime": res.get("status_mtime"),
             "checks": [{"name": "drill", "ok": False, "blocking": True, "detail": "refused"}]}))
        e.collect_check(rows["pu1"])                                             # close-check redispatch
        for r in rows:
            self.assertEqual(e.st.result(r).get("phase"), "parked", f"{r} stays parked")
            self.assertFalse((INST / "control" / f"EXECUTOR_{r}").exists(), f"{r}: no retier override")
            self.assertNotIn("retier", [x.get("event") for x in events(r)])
        self.assertTrue([x for x in events("pu6") if x.get("event") == "held_parked"])

        # (3) day mode gates a retier's re-dispatch like any dispatch
        add_lane("PQ", [("pq1", "-", 0, "mock", "")])
        self.addCleanup(lambda: (INST / "control" / "RUN_PQ").unlink(missing_ok=True))
        ctl("PAUSE_PQ", "day mode\n")
        self.addCleanup(lambda: (INST / "control" / "PAUSE_PQ").unlink(missing_ok=True))
        store().write_dispatch("pq1", {"sid": "pq1", "kind": "row", "lane": "PQ", "executor": "mock", "attempt": 1,
                                       "epoch": int(time.time()) - 300, "open": False})
        store().write_result("pq1", {"phase": "retry", "status_mtime": 0, "why": "dead window, retiered"})
        orch("start", "PQ", check=True)
        time.sleep(5)
        self.assertEqual(store().attempt("pq1"), 1, "a paused lane gets no retier re-dispatch")
        (INST / "control" / "PAUSE_PQ").unlink()
        wait(lambda: store().attempt("pq1") == 2, 60, what="pq1 re-dispatched once the pause lifts")

    def test_54_operator_closes_an_incident(self):
        """`orch steward close <n> "why"`: the Owner's close, through close(): evented, logged, owner-closed, its
        signature marked handled so the next scan tick does not reopen the same halt. Refused inside any session,
        for unknown / closed incidents, an empty reason and a live session."""
        import steward
        c, rig = self._mini("u54", {"CL": [("cl1", "-", "")]}, {})
        env = dict(ENV, ORCH_INSTANCE=str(c.instance))

        def run(*a, e=None):
            return subprocess.run([str(ORCH), "steward", *a], capture_output=True, text=True, env=e or env, cwd=str(WS))
        rig.st.write_result("cl1", {"phase": "blocked", "why": "drill halt", "halted_at": "t1"})
        sig = steward.halt_sig(rig, "cl1")
        inc = steward.open_incident(rig, "halt", "cl1", "blocked: drill halt", sig=sig)
        n = inc["n"]
        # a session (any rig session, incl. sw-/pl-) is refused; the incident stays open
        for sid, kind in (("a1", "row"), ("sw-77-1", "steward"), ("pl-x-1", "planner")):
            p = run("close", str(n), "no", e=dict(env, ORCH_ROW=sid, ORCH_KIND=kind))
            self.assertIn("REFUSED", p.stdout, sid)
            self.assertEqual(steward.load_incident(c, n)["status"], "open")
        # missing / empty why, unknown number
        self.assertEqual(run("close", str(n)).returncode, 2)
        self.assertEqual(run("close", str(n), "  ").returncode, 2)
        self.assertEqual(steward.load_incident(c, n)["status"], "open")
        p = run("close", "999", "x")
        self.assertIn("REFUSED: no incident 999", p.stdout)
        # a live session on the incident
        inc["sessions"] = ["sw-5-1"]
        inc["status"] = "working"
        steward.save_incident(c, inc)
        rig.st.write_dispatch("sw-5-1", {"sid": "sw-5-1", "kind": "steward", "executor": "mock", "open": True,
                                         "epoch": int(time.time())})
        p = run("close", str(n), "x")
        self.assertIn("REFUSED", p.stdout)
        self.assertIn("live session (sw-5-1)", p.stdout)
        self.assertEqual(steward.load_incident(c, n)["status"], "working")
        # the session is dead: a working incident may be closed
        rig.st.write_dispatch("sw-5-1", {"sid": "sw-5-1", "kind": "steward", "executor": "mock", "open": False})
        p = run("close", str(n), "the halt was fixed by hand")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(p.stdout.strip(), f"incident {n} closed (owner-closed): the halt was fixed by hand")
        done = steward.load_incident(c, n)
        self.assertEqual((done["status"], done["outcome"]), ("closed", "owner-closed"))
        self.assertIn("incident_close", [e.get("event") for e in rig.st.events("cl1")])
        self.assertTrue((c.state / "engine.log").read_text().count(f"incident {n} CLOSED owner-closed"))
        self.assertTrue(steward.was_handled(c, "halt", "cl1", sig))
        # already closed
        p = run("close", str(n), "again")
        self.assertIn("REFUSED", p.stdout)
        self.assertIn("already closed", p.stdout)
        # the next scan tick (the row is still halted, same signature) does not reopen it
        steward.scan(rig, {})
        steward.scan(rig, {})
        self.assertEqual([i["n"] for i in steward.incidents(c) if i["subject"] == "cl1" and i["kind"] == "halt"], [n])
        # a new halt (another signature) is a new incident: the handled mark is per signature
        rig.st.write_result("cl1", {"phase": "blocked", "why": "drill halt", "halted_at": "t2"})
        steward.scan(rig, {})
        self.assertEqual(len([i for i in steward.incidents(c) if i["subject"] == "cl1" and i["kind"] == "halt"]), 2)

    def test_55_steward_duties(self):
        """steward.duties: periodic commands as async jobs; INCIDENT lines open `duty` incidents (deduped by open
        incident and handled signature), a failing duty opens duty:<name>; nothing runs while the lane is off; the
        last start survives an engine restart; `orch lint` refuses bad entries; the bundle names the duty."""
        import copy
        import yaml
        import steward
        from engine import Engine
        c, _ = self._mini("u55", {"DU": [("du1", "-", "")]}, {})
        root = Path(c.root)
        (root / "duty_one.sh").write_text("pwd\necho 'INCIDENT tok-a the thing is off 42'\necho noise\n")
        (root / "duty_fail.sh").write_text("echo 'boom happened here'; echo second >&2; exit 3\n")
        (root / "duty_slow.sh").write_text("sleep 20\n")
        (root / "duty_ok.sh").write_text("echo all good\n")
        duties_ = [{"name": "one", "every": 300, "cmd": "bash duty_one.sh", "klass": "mechanical"},
                   {"name": "fail", "every": 300, "cmd": "bash duty_fail.sh"},
                   {"name": "slow", "every": 300, "cmd": "bash duty_slow.sh", "timeout": 1},
                   {"name": "ok", "every": 300, "cmd": "bash duty_ok.sh"}]

        def conf(enabled=True, only=None):
            cc = copy.copy(c)
            cc.data = dict(c.data, steward=dict(c.data["steward"], enabled=enabled,
                                                duties=[d for d in duties_ if only is None or d["name"] in only]))
            return cc

        def drain(e, limit=30):
            end = time.time() + limit
            while e.jobs and time.time() < end:
                time.sleep(0.2)
                e.poll_jobs()
            self.assertFalse(e.jobs, "duty jobs finished")

        def age(*names):                      # as if `every` had passed
            st = steward._duty_state(c)
            for nm in names:
                st[nm]["start"] = float(st[nm]["start"]) - 400
            steward._duty_save(c, st)

        def duty_incs():
            return [i for i in steward.incidents(c) if i["kind"] == "duty"]

        # (1) the lane is off: nothing runs, nothing is recorded
        e0 = Engine(conf(enabled=False))
        steward.duty_tick(e0)
        steward.tick(e0, {})
        self.assertEqual(e0.jobs, {})
        self.assertFalse((c.state / "steward" / "duties.json").exists())

        # (2) a duty that prints one INCIDENT line: exactly one incident; the same line does not reopen it
        e = Engine(conf(only=["one"]))
        steward.duty_tick(e)
        self.assertIn("duty:one", e.jobs, "an async job: the tick did not wait for it")
        steward.duty_tick(e)
        self.assertEqual(len(e.jobs), 1, "at most one run per duty")
        drain(e)
        out = (c.state / "steward" / "duty_one.out").read_text()
        self.assertEqual(out.splitlines()[0], str(root), "cwd = the project root")
        (inc,) = duty_incs()
        self.assertEqual((inc["subject"], inc["klass"], inc["status"]), ("tok-a", "mechanical", "open"))
        self.assertEqual(inc["summary"], "the thing is off 42")
        self.assertEqual(inc["duty"]["name"], "one")
        self.assertEqual(inc["duty"]["line"], "INCIDENT tok-a the thing is off 42")
        self.assertEqual(len(inc["sig"]), 16)
        # (3) a restart: the last start survived, nothing fires until `every` has passed
        e2 = Engine(conf(only=["one"]))
        steward.duty_tick(e2)
        self.assertEqual(e2.jobs, {}, "every is respected across an engine restart")
        age("one")
        steward.duty_tick(e2)
        self.assertIn("duty:one", e2.jobs)
        drain(e2)
        self.assertEqual(len(duty_incs()), 1, "still open: not duplicated")
        # closed: its signature is handled, the same line stays quiet
        steward.close(e2, steward.load_incident(c, inc["n"]), "resolved", "drill")
        age("one")
        steward.duty_tick(e2)
        drain(e2)
        self.assertEqual(len(duty_incs()), 1, "a closed incident is not reopened by the same line")
        # a changed line is a new incident
        (root / "duty_one.sh").write_text("echo 'INCIDENT tok-a the thing is off 43'\n")
        age("one")
        steward.duty_tick(e2)
        drain(e2)
        self.assertEqual(len(duty_incs()), 2)
        # (4) bundle: the duty's name, the INCIDENT line, the .out path
        b = steward.bundle(e2, duty_incs()[-1]).read_text()
        self.assertIn("`one`", b)
        self.assertIn("INCIDENT tok-a the thing is off 43", b)
        self.assertIn("duty_one.out", b)
        # (5) a failing duty, a timeout, a clean duty
        e3 = Engine(conf(only=["fail", "slow", "ok"]))
        t0 = time.time()
        steward.duty_tick(e3)
        self.assertLess(time.time() - t0, 5, "a slow duty never blocks the tick")
        drain(e3)
        by = {i["subject"]: i for i in duty_incs()}
        self.assertEqual(by["duty:fail"]["summary"], "duty fail failed: exit 3: boom happened here")
        self.assertEqual(by["duty:fail"]["klass"], "judgment", "klass defaults to judgment")
        self.assertIn("duty slow failed: exit 124", by["duty:slow"]["summary"])
        self.assertNotIn("duty:ok", by, "exit 0 and no INCIDENT line: nothing")
        before = len(duty_incs())
        age("fail", "slow", "ok")
        steward.duty_tick(e3)
        drain(e3)
        self.assertEqual(len(duty_incs()), before, "an open duty:<name> incident is not duplicated")
        # (6) the lint refuses bad entries; status lists the duties
        bad = [{"name": "A b", "every": 10, "cmd": " "}, {"name": "x", "every": 300, "cmd": "true", "klass": "inbox"},
               {"name": "x", "every": 300, "cmd": "true"}]
        ok_, errs = steward.duty_validate(bad)
        self.assertEqual(ok_, [])
        self.assertTrue(any("name must match" in m for m in errs) and any("every must be" in m for m in errs)
                        and any("cmd must be" in m for m in errs) and any("klass must be" in m for m in errs)
                        and any("duplicate name" in m for m in errs), errs)
        self.assertEqual(steward.duty_validate(None), ([], []))
        self.assertEqual(steward.duty_validate(duties_)[1], [])
        f = c.instance / "orch.yaml"
        y = yaml.safe_load(f.read_text())
        y["steward"]["duties"] = duties_
        f.write_text(yaml.safe_dump(y, sort_keys=False))
        env = dict(ENV, ORCH_INSTANCE=str(c.instance))
        p = subprocess.run([str(ORCH), "steward", "status"], capture_output=True, text=True, env=env, cwd=str(WS))
        self.assertIn("Duties (4;", p.stdout)
        self.assertRegex(p.stdout, r"one\s+every 300s\s+class mechanical\s+last start \d{4}-\S+ \S+\s+last exit 0")
        self.assertIn("next due", p.stdout)
        y["steward"]["duties"] = bad
        f.write_text(yaml.safe_dump(y, sort_keys=False))
        p = subprocess.run([str(ORCH), "lint"], capture_output=True, text=True, env=env, cwd=str(WS))
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("steward.duties[0]", p.stdout)
        self.assertIn("duplicate name", p.stdout)

    def test_56_round_close_counts_already_archived_lanes(self):
        """`orch round close`: a lane whose queue is already in archive/ (an earlier `orch round archive`) counts
        as archived; a lane with neither an active nor an archived queue keeps the round open."""
        import yaml
        c, _ = self._mini("u56", {"RA": [("ra1", "-", "")], "RB": [("rb1", "-", "")], "RC": [("rc1", "-", "")]},
                          {"rcl": "tag: rcl\nstatus: open\nlanes: [RA, RB, RC, RD]\n"})
        env = dict(ENV, ORCH_INSTANCE=str(c.instance))

        def run(*a):
            return subprocess.run([str(ORCH), "round", *a], capture_output=True, text=True, env=env, cwd=str(WS))

        def status():
            return yaml.safe_load((c.instance / "rounds" / "rcl" / "round.yaml").read_text()).get("status")
        p = run("archive", "RA", "--force")
        self.assertIn("archived RA", p.stdout, p.stdout + p.stderr)
        shutil.move(str(c.instance / "queue_RB.tsv"), str(c.instance / "archive" / "queue_RB.tsv"))
        p = run("close", "rcl")
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("RA: already archived", p.stdout)
        self.assertIn("RB: already archived", p.stdout)
        self.assertNotIn("skip RA", p.stdout)
        self.assertNotIn("skip RB", p.stdout)
        self.assertIn("some lanes were not archived (RC, RD)", p.stdout)
        self.assertEqual(status(), "open", "RC (active, unfinished) and RD (no queue at all) keep it open")
        p = run("archive", "RC", "--force")
        self.assertIn("archived RC", p.stdout)
        p = run("close", "rcl")
        self.assertEqual(p.returncode, 1, "RD has neither an active nor an archived queue")
        self.assertEqual(status(), "open")
        (c.instance / "queue_RD.tsv").write_text("")
        shutil.move(str(c.instance / "queue_RD.tsv"), str(c.instance / "archive" / "queue_RD.tsv"))
        p = run("close", "rcl")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("round rcl closed", p.stdout)
        self.assertEqual(status(), "closed")

    def _cadence(self, name: str, **steward_extra):
        """A mini instance with the Steward lane on and the cadence switches given (F1–F4, (cadence))."""
        import copy
        from engine import Engine
        c, _ = self._mini(name, {name.upper(): [(f"{name}r1", "-", "")]}, {})
        cc = copy.copy(c)
        cc.data = dict(c.data, steward=dict(c.data["steward"], enabled=True, **steward_extra))
        return cc, Engine(cc)

    def test_57_answer_wakes_steward_once_per_batch(self):
        """steward.wake_on_answer: an inbox item the Owner closes opens ONE `answered` incident per batch (debounced),
        never on the answers that were already done when the switch went on, never twice for the same batch; a batch
        answered while that incident is open waits for it to close; the bundle carries the letters and the rig's
        steward.on_answer instruction; off by default."""
        import steward
        c, e = self._cadence("u57", wake_on_answer=True, answer_debounce=2,
                             on_answer="dispatch the lead for inbox {ks}")
        for t in ("first", "second", "third", "fourth", "fifth"):
            steward.add_inbox(e, 0, f"{t} wall", "A) yes\nB) no")
        steward.mark_inbox_done(c, 1)
        t0 = time.time()

        def answered():
            return [i for i in steward.incidents(c) if i["kind"] == "answered"]
        steward.answer_scan(e, now=t0)
        self.assertEqual(answered(), [], "answers done before the switch went on are the baseline, not a wake")
        steward.mark_inbox_done(c, 2, letter="A")
        steward.answer_scan(e, now=t0 + 1)
        steward.mark_inbox_done(c, 3, letter="b", note="hold it")
        steward.answer_scan(e, now=t0 + 2)
        self.assertEqual(answered(), [], "debounce: the Owner may still be answering")
        steward.answer_scan(e, now=t0 + 4.5)
        (inc,) = answered()
        self.assertEqual((inc["subject"], inc["klass"], inc["status"]), ("rig:answers", "mechanical", "open"))
        self.assertIn("inbox 2, 3", inc["summary"])
        self.assertIn("dispatch the lead for inbox 2,3", inc["summary"])
        self.assertEqual([a["k"] for a in inc["answers"]], [2, 3])
        self.assertEqual([a.get("answer") for a in inc["answers"]], ["A", "B"])
        b = steward.bundle(e, inc).read_text()
        self.assertIn("## The Owner answered", b)
        self.assertIn("2. second wall — answer A", b)
        self.assertIn("hold it", b)
        steward.answer_scan(e, now=t0 + 10)
        self.assertEqual(len(answered()), 1, "the same batch never wakes twice")
        steward.mark_inbox_done(c, 4)
        steward.answer_scan(e, now=t0 + 11)
        steward.answer_scan(e, now=t0 + 20)
        self.assertEqual(len(answered()), 1, "one wake at a time: the next batch waits for the open incident")
        steward.close(e, steward.load_incident(c, inc["n"]), "resolved", "drill")
        steward.answer_scan(e, now=t0 + 21)
        self.assertEqual(len(answered()), 2)
        self.assertEqual([a["k"] for a in answered()[-1]["answers"]], [4])
        # the debounce has a ceiling: a stream of answers still wakes within answer_max_wait
        steward.close(e, steward.load_incident(c, answered()[-1]["n"]), "resolved", "drill")
        c2, e2 = self._cadence("u57b", wake_on_answer=True, answer_debounce=100, answer_max_wait=5)
        for t in ("a", "b", "c"):
            steward.add_inbox(e2, 0, t, "A) x")
        steward.answer_scan(e2, now=t0)
        for i, k in enumerate((1, 2, 3)):
            steward.mark_inbox_done(c2, k)
            steward.answer_scan(e2, now=t0 + 2 * i)
        self.assertEqual(len([i for i in steward.incidents(c2) if i["kind"] == "answered"]), 0)
        steward.answer_scan(e2, now=t0 + 5.5)
        self.assertEqual(len([i for i in steward.incidents(c2) if i["kind"] == "answered"]), 1)
        # off by default
        c3, e3 = self._cadence("u57c")
        steward.add_inbox(e3, 0, "x", "A) x")
        steward.answer_scan(e3, now=t0)
        steward.mark_inbox_done(c3, 1)
        steward.answer_scan(e3, now=t0 + 1000)
        self.assertEqual([i for i in steward.incidents(c3) if i["kind"] == "answered"], [])
        self.assertFalse((c3.state / "steward" / "answers_seen.json").exists())

    def test_58_owner_wake_and_inbox_done_with_letter(self):
        """`orch steward wake "why"` (the Owner's) opens a `wake` incident on the next tick, refused from a session;
        `orch steward inbox done <k> [LETTER] ["note"]` records the letter."""
        import steward
        c, e = self._cadence("u58")
        env = dict(ENV, ORCH_INSTANCE=str(c.instance))

        def cli(*a, extra=None):
            return subprocess.run([str(ORCH), "steward", *a], capture_output=True, text=True,
                                  env=dict(env, **(extra or {})), cwd=str(WS))
        p = cli("wake")
        self.assertEqual(p.returncode, 2, "the reason is required")
        p = cli("wake", "go now", extra={"ORCH_ROW": "sw-9-1", "ORCH_KIND": "steward"})
        self.assertEqual(p.returncode, 1)
        self.assertIn("REFUSED", p.stdout)
        self.assertFalse((c.control / "STEWARD_WAKE").exists())
        p = cli("wake", "go", "now")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertTrue((c.control / "STEWARD_WAKE").exists())
        steward.answer_scan(e)
        (inc,) = [i for i in steward.incidents(c) if i["kind"] == "wake"]
        self.assertEqual((inc["subject"], inc["klass"]), ("rig:wake", "mechanical"))
        self.assertIn("go now", inc["summary"])
        self.assertFalse((c.control / "STEWARD_WAKE").exists(), "consumed")
        steward.answer_scan(e)
        self.assertEqual(len([i for i in steward.incidents(c) if i["kind"] == "wake"]), 1)
        steward.add_inbox(e, 0, "a wall", "A) go\nB) stop")
        p = cli("inbox", "done", "1", "b", "only", "the", "cheap", "part")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        (it,) = [i for i in steward.inbox_items(c, open_only=False) if i["k"] == 1]
        self.assertEqual((it["status"], it["answer"], it["note"], it["done_by"]), ("done", "B", "only the cheap part", "owner"))

    def test_59_idle_watchdog_pages_stall_or_idle(self):
        """steward.idle_page_after: nothing live of any kind (parked and accepted rows do not count) for that long ->
        ONE engine page, class `stall` when an inbox item is open (it names it and how to answer), else `idle`;
        a re-page at each steward.idle_repage mark; a dispatch ends the episode; a new episode soon after a page
        continues its sequence; STOP / PAUSE / no lane on -> no watch; steward.idle_pages_per_day caps it; off by
        default."""
        import steward
        c, e = self._cadence("u59", idle_page_after=100, idle_repage=[300], idle_pages_per_day=4)
        sent = []
        e.notify = lambda urgency, title, body, row="", page_class="": sent.append(
            dict(urgency=urgency, title=title, body=body, row=row, page_class=page_class))
        t0 = time.time()
        steward.idle_watch(e, now=t0)
        self.assertEqual(sent, [], "no lane on: nothing to watch")
        (c.control / "RUN_ALL").write_text("")
        steward.idle_watch(e, now=t0)
        steward.idle_watch(e, now=t0 + 99)
        self.assertEqual(sent, [])
        steward.idle_watch(e, now=t0 + 100)
        (pg,) = sent
        self.assertEqual((pg["urgency"], pg["page_class"]), ("critical", "idle"))
        self.assertIn("IDLE", pg["title"])
        self.assertIn("orch steward wake", pg["body"])
        self.assertLessEqual(len(pg["body"].splitlines()), 3)
        self.assertLessEqual(len(pg["title"]) + len(pg["body"]), 320)
        steward.idle_watch(e, now=t0 + 150)
        self.assertEqual(len(sent), 1, "one page per mark")
        steward.idle_watch(e, now=t0 + 300)
        self.assertEqual(len(sent), 2, "the re-page")
        # a live session (any kind) ends the episode; a new one soon after a page continues its sequence
        (c.state / "pl-x-1.dispatch.json").write_text(json.dumps({"open": True, "executor": "mock"}))
        steward.idle_watch(e, now=t0 + 310)
        (c.state / "pl-x-1.dispatch.json").unlink()
        steward.idle_watch(e, now=t0 + 320)
        steward.idle_watch(e, now=t0 + 500)
        self.assertEqual(len(sent), 2, "a flapping rig gets no fresh first page")
        steward.idle_watch(e, now=t0 + 5000)
        self.assertEqual(len(sent), 2, "no mark left in this episode")
        # a fresh episode long after the last page: an open inbox item makes it a stall
        c.data["watchdog"] = dict(c.data["watchdog"], inbox_answers=True)
        k = steward.add_inbox(e, 0, "P7 law also infeasible: which route now?", "A) drop it\nB) wait")
        (c.state / "pl-x-2.dispatch.json").write_text(json.dumps({"open": True, "executor": "mock"}))
        steward.idle_watch(e, now=t0 + 9000)
        (c.state / "pl-x-2.dispatch.json").unlink()
        steward.idle_watch(e, now=t0 + 9001)
        steward.idle_watch(e, now=t0 + 9101)
        pg = sent[-1]
        self.assertEqual(len(sent), 3)
        self.assertEqual(pg["page_class"], "stall")
        self.assertIn("STALLED", pg["title"])
        self.assertIn(f"inbox {k}", pg["body"])
        self.assertIn(f'reply "inbox {k} A"', pg["body"])
        self.assertEqual(pg["row"], f"inbox:{k}", "a reply to the page answers that item")
        # a manual row's open dispatch is not work
        (c.state / "m1.dispatch.json").write_text(json.dumps({"open": True, "executor": "manual"}))
        steward.idle_watch(e, now=t0 + 9401)
        self.assertEqual(len(sent), 4, "the re-page still comes: a manual row waits on a person")
        (c.state / "m1.dispatch.json").unlink()
        # the daily cap
        (c.state / "pl-x-3.dispatch.json").write_text(json.dumps({"open": True, "executor": "mock"}))
        steward.idle_watch(e, now=t0 + 20000)
        (c.state / "pl-x-3.dispatch.json").unlink()
        steward.idle_watch(e, now=t0 + 20001)
        steward.idle_watch(e, now=t0 + 20200)
        self.assertEqual(len(sent), 4, "idle_pages_per_day reached (a rolling 24 h)")
        st = json.loads((c.state / "steward" / "idle.json").read_text())
        self.assertGreater(sum(st["by_day"].values()), 0, "idle seconds are logged for the digest")
        # STOP / PAUSE: the rig was stopped on purpose
        for f in ("STOP", "PAUSE"):
            c4, e4 = self._cadence(f"u59{f.lower()}", idle_page_after=10)
            s4 = []
            e4.notify = lambda *a, **kw: s4.append(a)
            (c4.control / "RUN_ALL").write_text("")
            (c4.control / f).write_text("")
            steward.idle_watch(e4, now=t0)
            steward.idle_watch(e4, now=t0 + 100)
            self.assertEqual(s4, [], f"{f}: no page")
        # off by default
        c5, e5 = self._cadence("u59off")
        s5 = []
        e5.notify = lambda *a, **kw: s5.append(a)
        (c5.control / "RUN_ALL").write_text("")
        steward.idle_watch(e5, now=t0)
        steward.idle_watch(e5, now=t0 + 100000)
        self.assertEqual(s5, [])
        self.assertFalse((c5.state / "steward" / "idle.json").exists())
        # the engine's notify keeps the Owner's page_classes: a class not listed is a digest line
        c6, e6 = self._cadence("u59cls", page_classes=["spend"])
        e6.notify("critical", "x", "y", page_class="stall")
        rec = json.loads((c6.state / "notify.jsonl").read_text().splitlines()[-1])
        self.assertEqual((rec["asked"], rec["urgency"], rec["page_class"]), ("critical", "normal", "stall"))

    def test_60_cadence_lint(self):
        """`orch lint` refuses malformed cadence switches."""
        import steward
        self.assertEqual(steward.cadence_validate({}), [])
        self.assertEqual(steward.cadence_validate({"wake_on_answer": True, "answer_debounce": 120, "answer_max_wait": 600,
                                                   "on_answer": "x {ks}", "idle_page_after": 2700,
                                                   "idle_repage": [10800], "idle_pages_per_day": 4}), [])
        errs = steward.cadence_validate({"wake_on_answer": "yes", "answer_debounce": -1, "idle_page_after": 30,
                                         "idle_repage": "3h", "idle_pages_per_day": 0, "on_answer": 3})
        for key in ("wake_on_answer", "answer_debounce", "idle_page_after", "idle_repage", "idle_pages_per_day", "on_answer"):
            self.assertTrue(any(key in m for m in errs), (key, errs))

    def test_99_zero_operator_commands_in_drills(self):
        """Across the drills, the only operator acts are the ones the tests made on purpose
        (orch start, the Owner's unpark/approve)."""
        import digest
        touches = digest.human_touches(all_events())
        allowed = {"unpark w1", "approve go1", "reset cap0", "approve rv1", "spend caps approve",
                   "park op1", "approve sg1", "park pk1"}   # test_53 parks pk1; test_45's Owner park (the act whose hold it tests)
        self.assertTrue(set(touches) <= allowed | {t for t in touches if t.startswith(("round start", "revert #", "spend sp", "phase open ", "phase rest ", "phase close ",
                                                                  "phase approve", "phase off"))},
                        f"unexpected operator acts: {touches}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
