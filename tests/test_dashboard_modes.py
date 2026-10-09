#!/usr/bin/env python3
"""Small, state-free checks for the dashboard's view selection and density."""
from __future__ import annotations

import io
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from dashboard_tui import current_pipeline, focus_lanes, lane_current_row  # noqa: E402
from rich.console import Console  # noqa: E402


def row(rid: str, state: str, *, deps=None):
    return {
        "id": rid, "state": state, "executor": "opus", "exec_source": "queue",
        "unmet_deps": deps or [], "note": "", "plan": f"PHASE/{rid}/PLAN.md",
        "unblock_window": "", "idle_secs": 0,
    }


def lane(name: str, state: str, *, watcher=False):
    current = row(name.lower() + "1", state, deps=["setup"] if state == "WAITING" else [])
    return {
        "lane": name, "headline": state, "focus": current["id"],
        "closed": 0, "total": 12, "watcher": watcher, "next": current["id"],
        "rows": [current],
    }


class DashboardModesTest(unittest.TestCase):
    def test_current_keeps_all_lanes_in_a_narrow_render(self):
        snap = {"lanes": [lane(name, "WAITING") for name in "MABESOX"]}
        console = Console(file=io.StringIO(), width=100, height=30,
                          record=True, color_system=None)
        console.print(current_pipeline(snap, []))
        rendered = console.export_text()
        for name in "MABESOX":
            self.assertIn(f"○ {name}", rendered)

    def test_focused_prefers_a_live_lane(self):
        snap = {"lanes": [lane("A", "GATED"), lane("B", "RUNNING", watcher=True)]}
        self.assertEqual(["B"], focus_lanes(snap, []))

    def test_focused_honours_explicit_lane(self):
        snap = {"lanes": [lane("A", "GATED"), lane("B", "RUNNING", watcher=True)]}
        self.assertEqual(["A"], focus_lanes(snap, ["A"]))

    def test_current_is_first_unfinished_not_a_future_gate(self):
        first = row("a1", "WAITING", deps=["setup"])
        future_gate = row("a9", "GATED")
        view = lane("A", "WAITING")
        view.update({"focus": "a9", "next": "a1", "rows": [first, future_gate]})
        self.assertIs(first, lane_current_row(view))


if __name__ == "__main__":
    unittest.main()
