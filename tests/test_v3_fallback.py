#!/usr/bin/env python3
"""Executor variants and the rate-limit fallback (lib/fallback.py), in an isolated sandbox (mock executor only —
no model, no network, no spend). A real engine runs in a tmux session of its own; a "rate-limited" row is a mock
row whose pane prints the captured OpenCode provider message (tests/fixtures/opencode_rate_limit.txt) and then
stands still.

    python3 -m unittest tests/test_v3_fallback.py -v
    KEEP=1 python3 -m unittest tests/test_v3_fallback.py      # keep the sandbox
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
FIXTURE = HOME / "tests" / "fixtures" / "opencode_rate_limit.txt"
sys.path.insert(0, str(HOME / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import tmux_isolation  # noqa: E402,F401  — a PRIVATE tmux server for every session this suite starts

SBX = Path(tempfile.mkdtemp(prefix="orch-fb-sbx-"))
SESSION = f"orchfbt{os.getpid()}"
WS = SBX / "project" / "cycle"
INST = WS / "ORCH"
STUBS = SBX / "stubs"
STUBS.mkdir(parents=True)
for b in ("claude", "opencode", "codex", "gemini", "systemctl", "loginctl"):
    (STUBS / b).write_text("#!/bin/sh\necho \"test stub: $0 refused\"; exit 97\n")
    (STUBS / b).chmod(0o755)
ENV = dict(os.environ, ORCH_INSTANCE=str(INST), ORCH_ALLOW_DIRTY="1", ORCH_NO_SYSTEMD="1",
           ORCH_SPEND_DIR=str(SBX / "spend"), ORCH_SYSTEMD_USER_DIR=str(SBX / "units"),
           PATH=f"{STUBS}:{os.environ.get('PATH', '')}")
for k in ("ORCH_ROW", "ORCH_KIND", "ORCH_PLAN_DIR"):
    ENV.pop(k, None)
os.environ["PATH"] = ENV["PATH"]
os.environ["ORCH_INSTANCE"] = str(INST)
os.environ["ORCH_SPEND_DIR"] = str(SBX / "spend")

PLAN = "# PLAN — {id}\n\nRulings: -\n\n## Acceptance\n- done\n\n## Verification\n```bash\ntrue\n```\n"
PATTERNS = [r"Rate limit exceeded\. Please try again later", "FreeUsageLimitError", r"\b429\b"]   # the last one is loose on purpose


def orch(*args: str, check: bool = False, env: dict | None = None) -> subprocess.CompletedProcess:
    p = subprocess.run([str(ORCH), *args], capture_output=True, text=True, env=env or ENV, cwd=str(WS))
    if check and p.returncode != 0:
        raise AssertionError(f"orch {' '.join(args)} failed:\n{p.stdout}\n{p.stderr}")
    return p


def store():
    from store import Store
    return Store(INST / "state")


def cfg():
    from config_parser import load
    return load(str(INST))


def rig():
    from engine import Rig
    return Rig(cfg())


def wait(cond, timeout: float = 90, every: float = 0.5, what: str = "") -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            if cond():
                return True
        except Exception:
            pass
        time.sleep(every)
    f = INST / "state" / "engine.log"
    tail = "".join(f.read_text(errors="replace").splitlines(True)[-40:]) if f.exists() else "(no engine log)"
    raise AssertionError(f"timed out waiting for {what or cond}\n--- engine.log tail ---\n{tail}")


def ctl(name: str, text: str = "") -> None:
    (INST / "control" / name).write_text(text)


def add_lane(lane: str, rows: list) -> None:
    lines = []
    for rid, deps, x in rows:
        d = WS / f"PHASE_{lane}" / rid
        d.mkdir(parents=True, exist_ok=True)
        (d / "PLAN.md").write_text(PLAN.format(id=rid))
        lines.append(f"{rid}\t{deps}\t0\t{x}\tPHASE_{lane}/{rid}/PLAN.md")
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
    time.sleep(1.5)


def limited_pane(*sids: str, hang: bool = True, only: str = "") -> None:
    for s in sids:
        ctl(f"MOCK_PANE_{s}", FIXTURE.read_text())
        if hang:
            ctl(f"MOCK_HANG_{s}")
        if only:
            ctl(f"MOCK_ONLY_{s}", only)


def override(rid: str) -> str:
    f = INST / "control" / f"EXECUTOR_{rid}"
    return f.read_text().strip() if f.exists() else ""


def executor_of(rid: str) -> str:
    return store().dispatch(rid).get("executor", "")


def steward_log() -> str:
    f = INST / "steward" / "STEWARD_LOG.md"
    return f.read_text() if f.exists() else ""


def notifications(page_class: str = "", since: float = 0.0):
    out = []
    try:
        for line in (INST / "state" / "notify.jsonl").read_text().splitlines():
            rec = json.loads(line)
            if float(rec.get("epoch", 0)) >= since and (not page_class or rec.get("page_class") == page_class):
                out.append(rec)
    except FileNotFoundError:
        pass
    return out


def fb_incidents():
    import steward
    return [i for i in steward.incidents(cfg()) if i.get("kind") == "executor_fallback"]


def pane_tree(window: str):
    import tmuxio
    p = subprocess.run(["tmux", "list-panes", "-t", f"={SESSION}:={window}", "-F", "#{pane_pid}"],
                       capture_output=True, text=True)
    return [pid for r in p.stdout.split() if r.isdigit() for pid, _ in tmuxio.tree(int(r))]


def alive(pids) -> list:
    out = []
    for p in pids:
        try:
            os.kill(p, 0)
            if "Z" not in Path(f"/proc/{p}/stat").read_text().rsplit(")", 1)[1].split()[0]:
                out.append(p)
        except OSError:
            pass
    return out


def snapshot() -> dict:
    ctrl = {f.name: f.read_text() for f in sorted((INST / "control").glob("EXECUTOR_*"))}
    st = store()
    phases = {f.name[:-len(".result.json")]: st.phase(f.name[:-len(".result.json")])
              for f in (INST / "state").glob("*.result.json")}
    return {"control": ctrl, "phases": phases, "log": steward_log(), "fb": sorted(p.name for p in (INST / "state" / "fallback").glob("*"))
            if (INST / "state" / "fallback").is_dir() else []}


class FB(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tmux_isolation.assert_isolated()
        WS.mkdir(parents=True)
        (WS / "DECISIONS.md").write_text("# decisions\n")
        for a in (("init", "-q"), ("config", "user.email", "t@t"), ("config", "user.name", "t")):
            subprocess.run(["git", "-C", str(WS), *a], capture_output=True)
        subprocess.run(["git", "-C", str(WS), "add", "-A"], capture_output=True)
        subprocess.run(["git", "-C", str(WS), "commit", "-qm", "init"], capture_output=True)
        orch("init", str(INST), "--name", "sandboxfb", "--session", SESSION, check=True)
        import yaml
        f = INST / "orch.yaml"
        data = yaml.safe_load(f.read_text())
        data["executors"].update({
            "default": "mock", "allowed": ["mock", "manual"], "auto_unblock": ["mock"], "unblock_executor": "mock",
            "retier_on_max_unblock": "mock", "max_unblock_attempts": 1, "dead_window_policy": "retier",
            "variants": {"mockv": {"base": "mock", "env": {"MOCK_ENV": "variant"}, "max_parallel": 6},
                         "mockw": {"base": "mock"}},
            "fallback": {"mock": {"to": ["mockv", "mockw"], "limited_rows": 2, "limited_minutes": 10}},
            "limit_patterns": {"mock": PATTERNS}, "limit_confirm_secs": 4})
        data["scheduler"] = {"mode": "dag", "tick_seconds": 1, "max_parallel": 20, "min_free_mb": 0}
        data["review"] = {"policy": "none"}
        data["idle"] = {"nudge_after": 0}
        data["reaper"] = {"closed_grace_secs": 2}
        data["health"] = {"enabled": False}
        data["steward"] = {"enabled": True, "tiers": {"mechanical": "mock", "judgment": "mock"}, "gate_after": 3600,
                           "rest_after": 3600, "max_parallel": 1, "planner": "mock", "panel": {"enabled": False}}
        data["engine"] = {"self_update": False, "commit_bookkeeping": False}
        data["watchdog"] = {"enabled": False, "push_min_urgency": "critical"}
        data["framework"] = {"allow_dirty": True}
        f.write_text(yaml.safe_dump(data, sort_keys=False))
        (INST / "queue_A.tsv").unlink(missing_ok=True)
        add_lane("A", [("a1", "-", "mock")])
        p = orch("start")
        assert "lanes enabled" in p.stdout, p.stdout + p.stderr

    @classmethod
    def tearDownClass(cls):
        subprocess.run(["tmux", "kill-session", "-t", f"={SESSION}"], capture_output=True)
        if os.environ.get("KEEP") == "1":
            print(f"\nsandbox kept: {SBX}")
        else:
            shutil.rmtree(SBX, ignore_errors=True)

    # ---------------------------------------------------------------------------------------------------
    def test_00_engine_up_and_plain_row(self):
        wait(lambda: store().phase("a1") == "accepted", 60, what="a1 accepted")

    def test_01_variants_inherit_and_are_accepted_everywhere(self):
        c = cfg()
        self.assertEqual(c.base_of("mockv"), "mock")
        self.assertIn("mockv", c.data["executors"]["allowed"])
        self.assertEqual(c.adapter("mockv"), "mock")
        self.assertEqual(c.flavor("mockv"), c.flavor("mock"))
        self.assertEqual(c.data["executors"]["max_parallel"]["mockv"], 6)
        # inherited kick behaviour, auto-unblock membership, reviewer mapping, limit patterns, health remedies
        import yaml
        from config_parser import Config, DEFAULTS, _merge, launch_env
        over = {"executors": {"allowed": ["opencode", "sonnet", "manual"], "arg_kick": ["opencode"], "auto_unblock": ["opencode"],
                              "variants": {"opencode_gemini": {"base": "opencode", "model": "google/gemini-4-argon",
                                                               "env": {"ORCH_OPENCODE_FLAGS": "--standalone --auto"}}},
                              "env": {"opencode": {"OPENCODE_CONFIG": "/x/o.jsonc", "ORCH_OPENCODE_FLAGS": "--auto"}},
                              "limit_patterns": {"opencode": ["boom"]}},
                "review": {"reviewer_map": {"opencode": "sonnet"}},
                "health": {"remedies": {"opencode": ["echo fix"]}}}
        oc = Config(Path(tempfile.mkdtemp()), _merge(DEFAULTS, over))
        v = "opencode_gemini"
        self.assertEqual(oc.flavor(v), "jrdev")
        self.assertIn(v, oc.data["executors"]["arg_kick"])
        self.assertIn(v, oc.data["executors"]["auto_unblock"])
        self.assertEqual(oc.data["review"]["reviewer_map"][v], "sonnet")
        self.assertEqual(oc.pick(oc.data["executors"]["limit_patterns"], v), ["boom"])
        self.assertEqual(oc.pick(oc.data["health"]["remedies"], v, [], present=True), ["echo fix"])
        self.assertEqual(oc.adapter(v), "opencode")
        le = launch_env(oc, v)
        self.assertIn("ORCH_ADAPTER_X=opencode", le)
        self.assertIn("export ORCH_EXECUTOR=opencode_gemini", le)
        self.assertIn("export ORCH_VARIANT_MODEL=google/gemini-4-argon", le)
        self.assertIn("export OPENCODE_CONFIG=/x/o.jsonc", le, "the variant inherits the base's env")
        self.assertIn("ORCH_OPENCODE_FLAGS='--standalone --auto'", le.replace("export ", ""), "its own env wins")
        self.assertNotIn("ORCH_VARIANT_MODEL", launch_env(oc, "opencode"))
        # assign accepts a variant; an unknown name is refused
        add_lane("V", [("v1", "-", "mock")])
        orch("assign", "v1", "mockv", check=True)
        self.assertEqual(override("v1"), "mockv")
        self.assertNotEqual(orch("assign", "v1", "nope").returncode, 0)
        (INST / "control" / "EXECUTOR_v1").unlink()

    def test_02_variant_adapter_passes_the_model_as_inline_config(self):
        """The OpenCode TUI has no --model flag: a variant's model reaches it as OPENCODE_CONFIG_CONTENT."""
        out = SBX / "oc_env.txt"
        stub = SBX / "oc_stub"
        stub.write_text(f'#!/bin/sh\nprintf "%s\\n" "$OPENCODE_CONFIG_CONTENT" > {out}\nprintf "%s\\n" "$@" >> {out}\n')
        stub.chmod(0o755)
        order = SBX / "order.txt"
        order.write_text("hello")
        env = dict(os.environ, ORCH_BIN_OPENCODE=str(stub), ORCH_VARIANT_MODEL="google/gemini-4-argon",
                   ORCH_OPENCODE_FLAGS="--standalone --auto", ORCH_INSTANCE=str(INST), ORCH_ARG_KICK="")
        env.pop("OPENCODE_CONFIG_CONTENT", None)
        p = subprocess.run([str(HOME / "lib" / "adapters" / "opencode.sh"), "x1", "opencode", str(order)],
                           capture_output=True, text=True, env=env)
        self.assertEqual(p.returncode, 0, p.stderr)
        lines = out.read_text().splitlines()
        self.assertEqual(json.loads(lines[0]), {"model": "google/gemini-4-argon"})
        self.assertEqual(lines[1:], ["--standalone", "--auto", "--prompt", "hello"])
        env.pop("ORCH_VARIANT_MODEL")
        subprocess.run([str(HOME / "lib" / "adapters" / "opencode.sh"), "x1", "opencode", str(order)], env=env)
        self.assertEqual(out.read_text().splitlines()[0], "", "the base executor never sets a model")

    def test_03_a_429_inside_normal_output_does_not_trigger(self):
        """A row prints a '429' line once (the loose pattern matches it) and keeps working: the tail moves, the
        clock restarts, the row is never rate-limited."""
        ctl("MOCK_PANE_f1", "FAILED test_retry: HTTP 429 Too Many Requests (expected, mocked)\n")
        ctl("MOCK_TICKER_f1")
        add_lane("F", [("f1", "-", "mock")])
        orch("start", "F", check=True)
        wait(lambda: store().dispatch("f1").get("open"), 30, what="f1 running")
        time.sleep(11)               # > 2 x limit_confirm_secs of a moving pane
        rec = json.loads((INST / "state" / "f1.rl").read_text()) if (INST / "state" / "f1.rl").exists() else {}
        self.assertFalse(rec.get("confirmed"), rec)
        self.assertEqual(executor_of("f1"), "mock")
        self.assertFalse((INST / "state" / "fallback").is_dir() and any((INST / "state" / "fallback").glob("*.json")))
        # and once the 429 line has scrolled out of the 40-line tail the record is gone
        wait(lambda: not (INST / "state" / "f1.rl").exists(), 80, what="the stale match dropped")
        subprocess.run(["tmux", "kill-window", "-t", f"={SESSION}:=f1"], capture_output=True)
        orch("disable", "F")

    def test_04_dry_run_then_engagement(self):
        import fallback
        import steward
        for lane in ("F", "V"):      # earlier drills' leftover pending rows would be moved too (by design): clear them
            (INST / f"queue_{lane}.tsv").unlink(missing_ok=True)
        cfg_set("executors.fallback", {"mock": {"to": ["mockv", "mockw"], "limited_rows": 9, "limited_minutes": 600}})
        # a parked live row that shows the limit text: never touched, never counted
        limited_pane("pl", only="mock")
        add_lane("P", [("pl", "-", "mock")])
        orch("start", "P", check=True)
        wait(lambda: store().dispatch("pl").get("open"), 30, what="pl running")
        steward.park(rig(), "pl", "Owner park (drill)", by="owner")
        # a row working normally, two limited rows, pending rows (behind a manual row), a parked pending row
        ctl("MOCK_TICKER_w1")
        limited_pane("l1", "l2", only="mock")
        add_lane("L", [("hold", "-", "manual"), ("w1", "-", "mock"), ("l1", "-", "mock"), ("l2", "-", "mock"),
                       ("p1", "hold", "mock"), ("p2", "hold", "mock"), ("pk", "hold", "mock"), ("px", "hold", "opencode_x")])
        # px names an executor that is not allowed: it must not stop the plan
        (INST / "queue_L.tsv").write_text("\n".join(l for l in (INST / "queue_L.tsv").read_text().splitlines()
                                                   if not l.startswith("px")) + "\n")
        steward.park(rig(), "pk", "Owner park (drill)", by="owner")
        orch("start", "L", check=True)
        wait(lambda: all(store().dispatch(r).get("open") for r in ("w1", "l1", "l2")), 40, what="rows running")
        self.l_pids = pane_tree("l1") + pane_tree("l2")
        self.assertTrue(self.l_pids)

        def confirmed(r):
            return json.loads((INST / "state" / f"{r}.rl").read_text()).get("confirmed")
        wait(lambda: confirmed("l1") and confirmed("l2"), 40, what="l1, l2 confirmed rate-limited")
        time.sleep(2)
        self.assertEqual(executor_of("l1"), "mock", "9 rows needed: not engaged yet")
        # --- dry run: lists exactly what would move, changes nothing
        before = snapshot()
        p = orch("executor", "fallback", "mock", "--dry-run")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        out = p.stdout
        for r in ("l1", "l2", "p1", "p2"):
            self.assertIn(f"{r}  ", out)
            self.assertIn(f"revert: orch assign {r} mock", out)
        for r in ("w1", "pk", "pl"):
            self.assertNotRegex(out, rf"\n    {r}  ")
        self.assertIn("-> mockv", out)
        self.assertIn("nothing was changed", out)
        self.assertEqual(snapshot(), before, "a dry run changes nothing")
        self.assertIn("no executors.fallback entry", orch("executor", "fallback", "mockv", "--dry-run").stdout)
        # --- the thresholds are met: the engine acts
        t0 = time.time()
        cfg_set("executors.fallback", {"mock": {"to": ["mockv", "mockw"], "limited_rows": 2, "limited_minutes": 600},
                                       "mockv": {"to": ["mockw"], "limited_rows": 2, "limited_minutes": 600}})
        wait(lambda: fallback.in_fallback(cfg(), "mock"), 60, what="mock in fallback")
        wait(lambda: executor_of("l1") == "mockv" and executor_of("l2") == "mockv", 60, what="l1, l2 re-dispatched on mockv")
        # (a) killed + reset + assigned; (b) pending reassigned; (c) the working row untouched; parked rows untouched
        wait(lambda: not alive(self.l_pids), 15, what="the old l1/l2 process trees are gone")
        for r in ("l1", "l2", "p1", "p2"):
            self.assertEqual(override(r), "mockv", r)
        for r in ("w1", "pl", "pk"):
            self.assertEqual(override(r), "", f"{r} must not be touched")
        self.assertEqual(executor_of("w1"), "mock")
        self.assertTrue(store().dispatch("w1").get("open"))
        self.assertEqual(store().attempt("w1"), 1)
        self.assertEqual(store().phase("pl"), "parked")
        self.assertEqual(store().phase("pk"), "parked")
        self.assertTrue(store().dispatch("pl").get("open"), "the Owner-parked live row keeps running")
        self.assertEqual(store().attempt("pl"), 1)
        self.assertEqual(store().attempt("l1"), 2)
        self.assertEqual(store().phase("p1"), "", "pending rows stay pending (their dep is the manual row)")
        # (d) Steward acts with reverts, one incident closed resolved, one notification, a digest line
        log = steward_log()
        for r in ("l1", "l2", "p1", "p2"):
            self.assertRegex(log, rf"\| assign \| {r} \|.*\| orch assign {r} mock \|")
        for r in ("w1", "pl", "pk"):
            self.assertNotRegex(log, rf"\| assign \| {r} \|")
        (inc,) = fb_incidents()
        self.assertEqual((inc["status"], inc["outcome"], inc["subject"], inc["sessions"]),
                         ("closed", "resolved", "executor:mock", []))
        notes = notifications(PAGE_CLASS, t0)
        self.assertEqual(len(notes), 1, notes)
        self.assertEqual(notes[0]["asked"], "critical")
        self.assertEqual(notes[0]["urgency"], "normal", "the Steward lane is on and the class is not listed: digest only")
        import digest
        text = digest.render(rig(), t0 - 5)
        self.assertIn("## Executor fallback", text)
        self.assertIn("mock -> mockv", text)
        self.assertIn("IN FALLBACK now: mock", text)
        # (e) a row assigned to mock later is dispatched on the target, with no override of its own
        add_lane("N", [("n1", "-", "mock")])
        ctl("MOCK_TICKER_n1")
        orch("start", "N", check=True)
        wait(lambda: executor_of("n1") == "mockv", 40, what="n1 dispatched on the fallback target")
        self.assertEqual(override("n1"), "")
        st = orch("executor", "status").stdout
        self.assertRegex(st, r"mock\s+IN FALLBACK since \S+ \S+ -> mockv, 4 row\(s\) moved")
        self.assertIn("variant of mock", st)
        self.assertRegex(orch("assignments").stdout, r"n1\s+\S*\s+mockv\s+fallback")
        # the rows on mockv ran normally (the pane text is mock-only) and finish
        wait(lambda: store().phase("l1") == "accepted" and store().phase("l2") == "accepted", 60, what="l1, l2 accepted on mockv")
        subprocess.run(["tmux", "kill-window", "-t", f"={SESSION}:=n1"], capture_output=True)
        store().write_dispatch("n1", dict(store().dispatch("n1"), open=False))
        orch("disable", "N")

    def test_05_restore_puts_pending_rows_back(self):
        import fallback
        self.assertTrue(fallback.in_fallback(cfg(), "mock"))
        p = orch("executor", "restore", "mock", "the provider is back")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("2 pending row(s) given back", p.stdout)
        self.assertIn("2 keep their executor", p.stdout)             # l1, l2: already run on the target
        self.assertFalse(fallback.in_fallback(cfg(), "mock"))
        for r in ("p1", "p2"):
            self.assertEqual(override(r), "", f"{r} is back on its queue executor")
        self.assertEqual(executor_of("l1"), "mockv", "a row that ran on the target keeps its record")
        self.assertRegex(orch("executor", "status").stdout, r"mock\s+normal")
        self.assertIn("is not in fallback", orch("executor", "restore", "mock").stdout)
        # the pending rows now run on mock
        (WS / "PHASE_L" / "hold" / "PROGRESS.md").write_text("# PROGRESS\n\nreleased by the drill\n")
        orch("done", "hold", "released by the drill", check=True)
        wait(lambda: executor_of("p1") == "mock" and executor_of("p2") == "mock", 60, what="p1, p2 run on mock")
        self.assertEqual(store().phase("pk"), "parked")
        # kill the still-ticking rows of this scenario
        for r in ("w1", "pl"):
            subprocess.run(["tmux", "kill-window", "-t", f"={SESSION}:={r}"], capture_output=True)
        # a session may not restore
        p = orch("executor", "restore", "mock", env=dict(ENV, ORCH_ROW="a1", ORCH_KIND="row"))
        self.assertIn("REFUSED", p.stdout)

    def test_06_chain_and_target_in_fallback(self):
        """l3, l4 are limited on mock AND on mockv (not on mockw): mock falls back to mockv, mockv — itself limited —
        falls back by its own entry to mockw. The first executor in `to` that is not in fallback is chosen."""
        import fallback
        limited_pane("l3", "l4", only="mock mockv")
        add_lane("C", [("l3", "-", "mock"), ("l4", "-", "mock")])
        orch("start", "C", check=True)
        wait(lambda: fallback.in_fallback(cfg(), "mock") and fallback.in_fallback(cfg(), "mockv"), 90,
             what="mock and mockv in fallback")
        wait(lambda: store().phase("l3") == "accepted" and store().phase("l4") == "accepted", 90, what="l3, l4 accepted on mockw")
        for r in ("l3", "l4"):
            xs = [e.get("executor") for e in store().events(r) if e.get("event") == "dispatch"]
            self.assertEqual(xs, ["mock", "mockv", "mockw"], xs)
        log = steward_log()
        self.assertRegex(log, r"\| assign \| l3 \|.*mock -> mockv.*\| orch assign l3 mock \|")
        self.assertRegex(log, r"\| assign \| l3 \|.*mockv -> mockw.*\| orch assign l3 mockv \|")
        c = cfg()
        self.assertEqual(fallback.effective(c, "mock"), "mockw", "mockv is in fallback: the next free target")
        self.assertEqual(fallback.effective(c, "mockv"), "mockw")
        self.assertEqual(len(fb_incidents()), 3)                       # mock (test_04), mock again, mockv
        self.assertTrue(all(i["outcome"] == "resolved" and i["status"] == "closed" for i in fb_incidents()))
        st = orch("executor", "status").stdout
        self.assertRegex(st, r"mockv\s+IN FALLBACK since \S+ \S+ -> mockw")
        # every target in fallback with nothing behind it: nothing moves, the dry run says so
        orch("executor", "restore", "mockv", check=True)
        orch("executor", "restore", "mock", check=True)

    def test_07_lane_off_still_works_and_logs_the_same(self):
        import fallback
        cfg_set("steward.enabled", False)
        n0 = steward_log().count("| assign | o")
        limited_pane("o1", "o2", only="mock")
        add_lane("O", [("o1", "-", "mock"), ("o2", "-", "mock")])
        t0 = time.time()
        orch("start", "O", check=True)
        wait(lambda: fallback.in_fallback(cfg(), "mock"), 60, what="mock in fallback with the Steward lane off")
        wait(lambda: store().phase("o1") == "accepted" and store().phase("o2") == "accepted", 60, what="o1, o2 accepted")
        self.assertEqual(steward_log().count("| assign | o") - n0, 2)
        self.assertRegex(steward_log(), r"\| assign \| o1 \|.*\| orch assign o1 mock \|")
        self.assertEqual(fb_incidents()[-1]["subject"], "executor:mock")
        self.assertEqual(fb_incidents()[-1]["outcome"], "resolved")
        n = notifications(PAGE_CLASS, t0)
        self.assertEqual(len(n), 1)
        self.assertEqual(n[0]["urgency"], "critical", "no lane to downgrade it: the class is the Owner's to see")
        orch("executor", "restore", "mock", check=True)
        cfg_set("steward.enabled", True)

    def test_08_listing_the_class_makes_the_notification_critical(self):
        import fallback
        cfg_set("steward.page_classes", ["spend", "pii", "secret", "engine_down", PAGE_CLASS])
        limited_pane("q1", "q2", only="mock")
        add_lane("Q", [("q1", "-", "mock"), ("q2", "-", "mock")])
        t0 = time.time()
        orch("start", "Q", check=True)
        wait(lambda: len(notifications(PAGE_CLASS, t0)) == 1, 60, what="the notification")
        self.assertEqual(notifications(PAGE_CLASS, t0)[0]["urgency"], "critical")
        wait(lambda: store().phase("q1") == "accepted" and store().phase("q2") == "accepted", 60, what="q1, q2 accepted")
        orch("executor", "restore", "mock", check=True)
        cfg_set("steward.page_classes", ["spend", "pii", "secret", "engine_down"])

    def test_09_one_row_limited_for_limited_minutes(self):
        import fallback
        cfg_set("executors.fallback", {"mock": {"to": ["mockv"], "limited_rows": 5, "limited_minutes": 0.1}})
        limited_pane("m1", only="mock")
        add_lane("M", [("m1", "-", "mock")])
        orch("start", "M", check=True)
        wait(lambda: fallback.in_fallback(cfg(), "mock"), 60, what="mock in fallback after one row stood limited for limited_minutes")
        wait(lambda: store().phase("m1") == "accepted", 60, what="m1 accepted on mockv")
        self.assertEqual(executor_of("m1"), "mockv")
        orch("executor", "restore", "mock", check=True)

    def test_10_manual_engage_and_steward_restore_action(self):
        import fallback
        import steward
        add_lane("S", [("hold2", "-", "manual"), ("s1", "hold2", "mock"), ("s2", "hold2", "mock")])
        p = orch("executor", "fallback", "mock")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("mock is in fallback -> mockv", p.stdout)
        orch("start", "S", check=True)
        wait(lambda: store().dispatch("hold2").get("open"), 30, what="hold2")
        # s1, s2 were not in a started lane when the fallback engaged by hand, but are pending rows all the same
        self.assertTrue(fallback.in_fallback(cfg(), "mock"))
        r = rig()
        bad = steward.precheck(cfg(), "executor-restore", ["nope"])
        self.assertIn("not in executors.allowed", bad)
        d = {"incident": "-", "sid": "sw-9-1"}
        ok, detail, revert = steward.execute(r, "sw-9-1", "executor-restore", ["mock"], "the provider is back", d)
        self.assertTrue(ok, detail)
        self.assertIn("orch executor fallback mock", revert)
        self.assertFalse(fallback.in_fallback(cfg(), "mock"))
        self.assertIn("executor-restore", steward.STEWARD_ACTIONS)
        ok, detail, _ = steward.execute(r, "sw-9-1", "executor-restore", ["mock"], "again", d)
        self.assertFalse(ok)

    def test_11_lint_refuses_unknown_targets_and_cycles(self):
        import fallback
        from config_parser import Config, DEFAULTS, _merge

        def probs(ex):
            c = Config(Path(tempfile.mkdtemp()), _merge(DEFAULTS, {"executors": ex}))
            return fallback.problems(c)

        e, _ = probs({"fallback": {"opencode": {"to": ["nope"]}}})
        self.assertTrue(any("'nope' is not in executors.allowed" in m for m in e), e)
        e, _ = probs({"fallback": {"ghost": {"to": ["opencode"]}}})
        self.assertTrue(any("'ghost' is not in executors.allowed" in m for m in e), e)
        v = {"a_v": {"base": "opencode"}, "b_v": {"base": "opencode"}}
        e, _ = probs({"variants": v, "fallback": {"a_v": {"to": ["b_v"]}, "b_v": {"to": ["a_v"]}}})
        self.assertTrue(any("cycle" in m for m in e), e)
        e, _ = probs({"variants": v, "fallback": {"opencode": {"to": ["a_v"]}, "a_v": {"to": ["b_v"]}, "b_v": {"to": ["opencode"]}}})
        self.assertTrue(any("cycle" in m for m in e), e)
        e, _ = probs({"fallback": {"opencode": {"to": ["opencode"]}}})
        self.assertTrue(any("the executor itself" in m for m in e), e)
        e, _ = probs({"fallback": {"opencode": {"to": ["manual"]}}})
        self.assertTrue(any("manual" in m for m in e), e)
        e, _ = probs({"fallback": {"opencode": {"to": ["sonnet"], "limited_rows": 0}}})
        self.assertTrue(any("limited_rows" in m for m in e), e)
        e, _ = probs({"fallback": {"opencode": {"to": ["sonnet"], "limited_minutes": "soon"}}})
        self.assertTrue(any("limited_minutes" in m for m in e), e)
        e, _ = probs({"variants": {"v": {"base": "nothing_like_it"}}})
        self.assertTrue(any("not a known executor" in m for m in e), e)
        e, _ = probs({"variants": {"v": {"base": "opencode"}, "w": {"base": "v"}}})
        self.assertTrue(any("itself a variant" in m for m in e), e)
        e, w = probs({"variants": {"v": {"base": "opencode", "env": {"ORCH_OPENCODE_FLAGS": "--auto --model google/x"}}}})
        self.assertEqual(e, [])
        self.assertTrue(any("no --model flag" in m for m in w), w)
        e, _ = probs({"limit_patterns": {"opencode": ["(unclosed"]}})
        self.assertTrue(any("not a valid regex" in m for m in e), e)
        e, w = probs({"fallback": {"mock": {"to": ["sonnet"]}}})
        self.assertEqual(e, [])
        self.assertTrue(any("no executors.limit_patterns" in m for m in w), w)
        # a good config is clean; `orch lint` runs it
        e, w = probs({"variants": {"v": {"base": "opencode", "model": "p/m"}}, "fallback": {"opencode": {"to": ["v"]}}})
        self.assertEqual(e, [], e)
        cfg_set("executors.fallback", {"mock": {"to": ["mockv"]}, "mockv": {"to": ["mock"]}})
        p = orch("lint")
        self.assertEqual(p.returncode, 1, p.stdout)
        self.assertIn("executors.fallback has a cycle", p.stdout)
        cfg_set("executors.fallback", {"mock": {"to": ["mockv", "mockw"], "limited_rows": 2, "limited_minutes": 10}})
        self.assertNotIn("executors.fallback has a cycle", orch("lint").stdout)

    def test_12_no_process_left_and_no_stray_state(self):
        for _ in range(20):
            if not (INST / "state" / "fallback").is_dir() or not list((INST / "state" / "fallback").glob("*.json")):
                break
            time.sleep(0.5)
        self.assertEqual(list((INST / "state" / "fallback").glob("*.json")), [], "every drill restored what it moved")


PAGE_CLASS = "executor_fallback"

if __name__ == "__main__":
    unittest.main(verbosity=2)
