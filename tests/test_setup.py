#!/usr/bin/env python3
"""orch setup: the comment-preserving YAML setter, init with a preset, the question
catalogue, apply, the rig card and the readiness check. Sandbox only; no tmux, no model.

    python3 -m unittest tests/test_setup.py -v
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

HOME = Path(__file__).resolve().parent.parent
ORCH = HOME / "bin" / "orch"
sys.path.insert(0, str(HOME / "lib"))
import setup as S  # noqa: E402

DOC = """# top comment
project:
  name: "x"          # the name
  root: "../.."
executors:
  allowed: [opus, mock]   # who may run
  args: {}           # extra args
reaper: {closed_grace_secs: 300}   # inline
guard:
  resources:
    - "git-lock"
    - "db"
  git_lock: "git-lock"
"""


class SetKey(unittest.TestCase):
    def test_replace_keeps_comment(self):
        t = S.set_key(DOC, "project.name", "y")
        self.assertIn('  name: "y"          # the name\n', t)
        self.assertEqual(yaml.safe_load(t)["project"]["name"], "y")
        self.assertIn("# top comment", t)

    def test_nested_insert_and_new_top_level(self):
        t = S.set_key(DOC, "executors.unblock_executor", "opus")
        t = S.set_key(t, "review.policy", "none")
        t = S.set_key(t, "watchdog.quiet_hours.from", "22:00")
        d = yaml.safe_load(t)
        self.assertEqual(d["executors"]["unblock_executor"], "opus")
        self.assertEqual(d["review"]["policy"], "none")
        self.assertEqual(d["watchdog"]["quiet_hours"]["from"], "22:00")
        self.assertEqual(d["executors"]["allowed"], ["opus", "mock"])

    def test_inline_mapping_and_block_list(self):
        t = S.set_key(DOC, "reaper.halted_grace_secs", 60)
        t = S.set_key(t, "guard.resources", ["git-lock", {"name": "db", "slots": 3}])
        t = S.set_key(t, "executors.args", {"opus": ["--effort", "high"]})
        d = yaml.safe_load(t)
        self.assertEqual(d["reaper"], {"closed_grace_secs": 300, "halted_grace_secs": 60})
        self.assertEqual(d["guard"]["resources"], ["git-lock", {"name": "db", "slots": 3}])
        self.assertEqual(d["guard"]["git_lock"], "git-lock")
        self.assertEqual(d["executors"]["args"], {"opus": ["--effort", "high"]})
        self.assertIn("# inline", t)
        self.assertIn("# extra args", t)


class InitAndSetup(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sbx = Path(tempfile.mkdtemp(prefix="orch-setup-"))
        cls.ws = cls.sbx / "proj" / "Docs" / "CycleT"
        cls.ws.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(cls.ws)], check=True)
        cls.inst = cls.ws / "ORCH_T"
        cls.env = dict(os.environ, ORCH_INSTANCE=str(cls.inst))
        cls.env.pop("ORCH_ROW", None)
        p = subprocess.run([str(ORCH), "init", str(cls.inst), "--name", "Cycle T", "--session",
                            f"setuptest{os.getpid()}", "--preset", "strict"],
                           capture_output=True, text=True, cwd=str(cls.sbx / "proj"),
                           env={k: v for k, v in cls.env.items() if k != "ORCH_INSTANCE"})
        assert p.returncode == 0, p.stdout + p.stderr

    @classmethod
    def tearDownClass(cls):
        if not os.environ.get("KEEP"):
            shutil.rmtree(cls.sbx, ignore_errors=True)

    def orch(self, *a):
        return subprocess.run([str(ORCH), *a], capture_output=True, text=True, env=self.env, cwd=str(self.inst))

    def cfg(self):
        return yaml.safe_load((self.inst / "orch.yaml").read_text())

    def test_1_init_applies_preset(self):
        d = self.cfg()
        self.assertEqual(d["executors"]["unblock_executor"], "opus")
        self.assertEqual(d["review"]["policy"], "junior")
        self.assertEqual(d["watchdog"]["push_min_urgency"], "critical")
        self.assertEqual(d["executors"]["args"]["opus"], ["--effort", "high"])
        names = sorted(p.name for p in (self.inst / "prompts").iterdir())
        self.assertTrue(all(n.startswith("_") or n == "README.md" for n in names), names)
        self.assertNotIn("setup.defaults.yaml", names)
        base = (self.ws / "rules" / "BASE.md").read_text()
        self.assertIn("TODO(setup)", base)
        self.assertIn("Cycle T", base)
        self.assertTrue((self.inst / "RIG.md").is_file())
        self.assertEqual(yaml.safe_load((self.inst / "setup.answers.yaml").read_text())["preset"], "strict")

    def test_2_questions(self):
        p = self.orch("setup", "questions", "--json")
        self.assertEqual(p.returncode, 0, p.stderr)
        qs = {q["id"]: q for q in json.loads(p.stdout)}
        for k in ("executors", "unblocker", "review", "scheduler", "capacity", "notify", "idle"):
            self.assertIn(k, qs)
        self.assertIn(qs["capacity"]["recommended"], ("small", "medium", "large"))
        self.assertEqual(qs["review"]["recommended"], "junior")   # preset recommendation

    def test_3_apply_and_card(self):
        p = self.orch("setup", "apply", "--answer", "executors=opus,sonnet", "--answer", "unblock_scope=all",
                      "--answer", "unblocker=sonnet-then-opus", "--answer", "scheduler=serial",
                      "--set", "guard.resources=[git-lock, {name: db, slots: 2}]")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        d = self.cfg()
        self.assertEqual(d["executors"]["allowed"], ["opus", "sonnet", "manual", "mock"])
        self.assertEqual(d["executors"]["auto_unblock"], ["opus", "sonnet"])
        self.assertEqual(d["executors"]["unblock_executors"], ["sonnet", "opus"])
        self.assertEqual(d["scheduler"]["mode"], "serial")
        self.assertEqual(d["guard"]["resources"], ["git-lock", {"name": "db", "slots": 2}])
        self.assertIn("# dag: declared deps only", (self.inst / "orch.yaml").read_text())   # comment kept
        ans = yaml.safe_load((self.inst / "setup.answers.yaml").read_text())
        self.assertEqual(ans["answers"]["unblocker"], "sonnet-then-opus")
        bad = self.orch("setup", "apply", "--answer", "review=sometimes")
        self.assertNotEqual(bad.returncode, 0)
        self.assertEqual(self.orch("setup", "card").returncode, 0)
        card = (self.inst / "RIG.md").read_text()
        self.assertIn("`sonnet` → `opus`", card)
        self.assertIn("`db` — 2 slot(s)", card)
        self.assertIn("unblocker: sonnet-then-opus", card)

    def test_4_check(self):
        p = self.orch("setup", "check")
        self.assertIn("no rows yet", p.stdout)
        self.assertIn("TODO(setup)", p.stdout)
        self.assertIn("is unique", p.stdout)
        self.assertEqual(p.returncode, 0, p.stdout)

    def test_5_lint_refuses_a_junior_decision_role(self):
        before = (self.inst / "orch.yaml").read_text()
        self.addCleanup((self.inst / "orch.yaml").write_text, before)
        p = self.orch("setup", "apply", "--set", "executors.unblock_executors=[haiku, opus]")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        out = self.orch("lint").stdout
        self.assertIn("executors.unblock_executors[0] is 'haiku', a jrdev tier", out)
        self.assertNotIn("unblock_executors[1]", out)


class JuniorNeverDecides(unittest.TestCase):
    """Haiku is a junior tier like opencode: it may run a complete plan, never a decision role."""

    def cfg(self, over=None):
        import config_parser as C
        return C.Config(Path(tempfile.mkdtemp()), C._merge(C.DEFAULTS, over or {}))

    def test_tiers(self):
        c = self.cfg()
        self.assertEqual((c.flavor("haiku"), c.adapter("haiku")), ("jrdev", "claude"))
        self.assertEqual([x for x in ("opus", "sonnet", "haiku", "opencode", "gemini", "codex", "manual")
                          if c.decides(x)], ["opus", "sonnet"])
        self.assertEqual([k for k, x in c.decision_roles() if not c.decides(x)], [], "the defaults are all hard tiers")

    def test_every_decision_role_is_named(self):
        c = self.cfg({"executors": {"unblock_executor": "haiku", "retier_on_max_unblock": "opencode",
                                    "unblock_executors": ["sonnet", "haiku"]},
                      "review": {"reviewer_map": {"opencode": "haiku"}},
                      "preflight": {"reviewer": "haiku"},
                      "steward": {"enabled": True, "tiers": {"mechanical": "haiku", "judgment": "opus"},
                                  "planner": "haiku", "panel": {"falsifier": "haiku"}},
                      "watchdog": {"answers": True, "answer_executor": "haiku"}})
        bad = sorted(k for k, x in c.decision_roles() if not c.decides(x))
        self.assertEqual(bad, sorted(["executors.unblock_executor", "executors.retier_on_max_unblock",
                                      "executors.unblock_executors[1]", "review.reviewer_map.opencode",
                                      "preflight.reviewer", "steward.tiers.mechanical", "steward.planner",
                                      "steward.panel.falsifier", "watchdog.answer_executor"]))

    def test_a_round_cannot_hand_a_junior_a_decision(self):
        import rounds
        c = self.cfg({"executors": {"allowed": ["opus", "sonnet", "haiku"]}})
        probs = rounds.policy_problems(c, {"retier_on_max_unblock": "haiku", "unblock_executors": ["haiku"]})
        self.assertEqual(len(probs), 2, probs)
        self.assertEqual(rounds.policy_problems(c, {"retier_on_max_unblock": "opus",
                                                    "unblock_executors": ["sonnet", "opus"]}), [])

    def test_haiku_cannot_be_promoted(self):
        c = self.cfg({"executors": {"flavors": {"haiku": "hard", "cheap": "hard"}, "models": {"cheap": "claude-haiku-latest"}}})
        self.assertEqual((c.flavor("haiku"), c.flavor("cheap")), ("jrdev", "jrdev"))
        self.assertFalse(c.decides("haiku") or c.decides("cheap"))


if __name__ == "__main__":
    unittest.main()
