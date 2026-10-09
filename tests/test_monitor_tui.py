#!/usr/bin/env python3
"""Pure state-selection tests for the read-only quick monitor."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from monitor_tui import (  # noqa: E402
    actionable_items,
    blocker_groups,
    next_candidates,
    running_rows,
)


def row(rid, state="WAITING", *, deps=None, gate=False, executor="opus"):
    return {
        "id": rid, "state": state, "unmet_deps": deps or [], "gate": gate,
        "executor": executor, "note": "", "plan": f"PHASE/{rid}_work/PLAN.md",
        "idle_secs": 0,
    }


def lane(name, rows, *, watcher=True, paused=False, stopped=False):
    return {
        "lane": name, "rows": rows, "watcher": watcher,
        "paused": paused, "stopped": stopped,
        "closed": sum(r["state"] == "CLOSED" for r in rows), "total": len(rows),
    }


class MonitorSelectionTest(unittest.TestCase):
    def test_running_and_next_use_the_successor_in_that_lane(self):
        a1 = row("a1", "RUNNING")
        a2 = row("a2", deps=["a1"])
        b1 = row("b1", "PENDING")
        lanes = [lane("A", [a1, a2]), lane("B", [b1])]
        snap = {"gates": []}
        self.assertEqual([("A", "a1")], [(lv["lane"], r["id"]) for lv, r in running_rows(lanes)])
        selected = next_candidates(lanes, snap, 5)
        self.assertEqual(["b1", "a2"], [item[1]["id"] for item in selected])
        self.assertEqual(["READY", "AFTER"], [item[2] for item in selected])

    def test_future_gate_is_not_an_actionable_approval(self):
        future = row("x2", deps=["x1"], gate=True, executor="manual")
        lanes = [lane("X", [future])]
        items, future_count = actionable_items(lanes, {"gates": []})
        self.assertEqual([], items)
        self.assertEqual(1, future_count)

    def test_ready_gate_needs_approval_and_open_manual_needs_operator(self):
        gate = row("x1", gate=True, executor="manual")
        lanes = [lane("X", [gate])]
        items, future_count = actionable_items(lanes, {"gates": []})
        self.assertEqual(("APPROVE", "x1", "orch approve x1"), items[0])
        self.assertEqual(0, future_count)

        items, _ = actionable_items(lanes, {"gates": ["GO_x1"]})
        self.assertEqual("MANUAL", items[0][0])

    def test_shared_dependencies_are_aggregated(self):
        candidates = [
            (lane("A", []), row("a1", deps=["c0", "ck0"]), "WAITING", "c0,ck0"),
            (lane("B", []), row("b1", deps=["c0", "ck0"]), "WAITING", "c0,ck0"),
        ]
        self.assertEqual(["c0,ck0 block 2 lane heads: A,B"], blocker_groups(candidates))


if __name__ == "__main__":
    unittest.main()
