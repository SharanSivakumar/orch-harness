#!/usr/bin/env python3
"""Read-only, deterministic quick-peek monitor for an orchestration instance.

The monitor has no control surface: it never writes state, sends tmux keys,
connects to a database or network service, or calls a model. It reconciles the
same Resolver snapshot used by status/dashboard and renders it once or once per
second until Ctrl-C.
"""
from __future__ import annotations

import argparse
import shutil
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config_parser import load                 # noqa: E402
from state_resolver import Resolver            # noqa: E402

try:
    from rich import box
    from rich.console import Console, Group
    from rich.live import Live
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text
except ImportError:                             # pragma: no cover
    sys.stderr.write("orch: the monitor needs `rich` (pip install --user rich)\n")
    raise SystemExit(2)

LIVE_STATES = {"RUNNING", "UNBLOCKING", "QUIET", "ORPHAN"}
HALT_STATES = {"BLOCKED", "ROUTE_A", "HALTED"}
STATE_STYLE = {
    "READY": "bold green", "RUNNING": "bold cyan", "UNBLOCKING": "bold magenta",
    "QUIET": "bold yellow", "ORPHAN": "bold red", "APPROVAL": "bold yellow",
    "MANUAL": "bold blue", "WAITING": "yellow", "AFTER": "cyan",
    "PAUSED": "dim yellow", "STOPPED": "bold red", "BLOCKED": "bold red",
    "ROUTE_A": "bold red", "HALTED": "bold red", "CLOSED": "dim green",
}


def selected_lanes(snap: Dict[str, Any], lane_filter: List[str]) -> List[Dict[str, Any]]:
    return [lv for lv in snap["lanes"]
            if not lane_filter or lv["lane"] in lane_filter]


def running_rows(lanes: Iterable[Dict[str, Any]]) -> List[Tuple[Dict[str, Any], Dict[str, Any]]]:
    return [(lv, row) for lv in lanes for row in lv["rows"]
            if row["state"] in LIVE_STATES]


def first_open(lv: Dict[str, Any]) -> Dict[str, Any] | None:
    return next((row for row in lv["rows"] if row["state"] != "CLOSED"), None)


def row_after(lv: Dict[str, Any], current: Dict[str, Any]) -> Dict[str, Any] | None:
    seen = False
    for row in lv["rows"]:
        if seen and row["state"] != "CLOSED":
            return row
        if row is current or row["id"] == current["id"]:
            seen = True
    return None


def gate_is_open(snap: Dict[str, Any], rid: str) -> bool:
    return f"GO_{rid}" in set(snap.get("gates", []))


def lane_candidate(lv: Dict[str, Any]) -> Dict[str, Any] | None:
    live = next((row for row in lv["rows"] if row["state"] in LIVE_STATES), None)
    return row_after(lv, live) if live else first_open(lv)


def candidate_status(lv: Dict[str, Any], row: Dict[str, Any], snap: Dict[str, Any],
                     running_ids: set[str]) -> Tuple[str, str]:
    if lv.get("stopped"):
        return "STOPPED", "orch unstop"
    if lv.get("paused"):
        return "PAUSED", "orch resume"
    if row["state"] in HALT_STATES:
        return row["state"], row.get("note") or "lane halted"
    unmet = row.get("unmet_deps") or []
    if unmet:
        if all(dep in running_ids for dep in unmet):
            return "AFTER", ",".join(unmet)
        return "WAITING", ",".join(unmet)
    if row.get("gate") and not gate_is_open(snap, row["id"]):
        return "APPROVAL", f"orch approve {row['id']}"
    if row.get("executor") == "manual":
        return "MANUAL", f"open prompts/{row['id']}.manual.md"
    return "READY", "can dispatch"


def next_candidates(lanes: List[Dict[str, Any]], snap: Dict[str, Any],
                    limit: int = 5) -> List[Tuple[Dict[str, Any], Dict[str, Any], str, str]]:
    running_ids = {row["id"] for _, row in running_rows(lanes)}
    priority = {
        "READY": 0, "APPROVAL": 1, "MANUAL": 1, "AFTER": 2,
        "WAITING": 3, "BLOCKED": 4, "ROUTE_A": 4, "HALTED": 4,
        "PAUSED": 5, "STOPPED": 5,
    }
    out = []
    for lane_index, lv in enumerate(lanes):
        row = lane_candidate(lv)
        if not row:
            continue
        status, reason = candidate_status(lv, row, snap, running_ids)
        out.append((priority.get(status, 9), lane_index, lv, row, status, reason))
    out.sort(key=lambda item: (item[0], item[1]))
    return [(lv, row, status, reason) for _, _, lv, row, status, reason in out[:limit]]


def actionable_items(lanes: List[Dict[str, Any]], snap: Dict[str, Any]) -> Tuple[List[Tuple[str, str, str]], int]:
    items: List[Tuple[str, str, str]] = []
    future_approvals = 0
    running_by_lane = {lv["lane"] for lv, _ in running_rows(lanes)}
    for lv in lanes:
        for row in lv["rows"]:
            if row["state"] == "CLOSED":
                continue
            unmet = row.get("unmet_deps") or []
            if row.get("gate") and not gate_is_open(snap, row["id"]):
                if unmet:
                    future_approvals += 1
                else:
                    items.append(("APPROVE", row["id"], f"orch approve {row['id']}"))
                continue
            if row["state"] in HALT_STATES:
                items.append((row["state"], row["id"], row.get("note") or "inspect the row"))
            elif row["state"] == "ORPHAN":
                items.append(("ORPHAN", row["id"], f"close it or: orch adopt {row['id']}"))
            elif row.get("executor") == "manual" and not unmet and row["state"] not in LIVE_STATES:
                items.append(("MANUAL", row["id"], f"open prompts/{row['id']}.manual.md"))

        head = first_open(lv)
        if (head and lv["lane"] not in running_by_lane and not lv.get("watcher")
                and not lv.get("stopped") and not lv.get("paused")):
            status, _ = candidate_status(lv, head, snap, set())
            if status in {"READY", "APPROVAL", "MANUAL"}:
                items.append(("WATCHER", lv["lane"], f"{head['id']} is ready; orch start {lv['lane']}"))
    return items, future_approvals


def blocker_groups(candidates: List[Tuple[Dict[str, Any], Dict[str, Any], str, str]]) -> List[str]:
    dep_lanes: Dict[str, set[str]] = defaultdict(set)
    for lv, row, _, _ in candidates:
        for dep in row.get("unmet_deps") or []:
            dep_lanes[dep].add(lv["lane"])
    same_lanes: Dict[Tuple[str, ...], List[str]] = defaultdict(list)
    for dep, lanes in dep_lanes.items():
        same_lanes[tuple(sorted(lanes))].append(dep)
    lines = []
    for lanes, deps in sorted(same_lanes.items(), key=lambda item: (-len(item[0]), item[1])):
        verb = "blocks" if len(lanes) == 1 else "block"
        lines.append(f"{','.join(sorted(deps))} {verb} {len(lanes)} lane head"
                     f"{'s' if len(lanes) != 1 else ''}: {','.join(lanes)}")
    return lines


def duration_since(value: str, now: float | None = None) -> str:
    try:
        then = datetime.strptime(value, "%Y-%m-%d %H:%M:%S").timestamp()
        seconds = max(0, int((now or time.time()) - then))
    except (TypeError, ValueError):
        return "?"
    if seconds >= 3600:
        return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"
    if seconds >= 60:
        return f"{seconds // 60}m"
    return f"{seconds}s"


def held_up_lines(lanes: List[Dict[str, Any]], snap: Dict[str, Any],
                  candidates: List[Tuple[Dict[str, Any], Dict[str, Any], str, str]],
                  future_approvals: int, limit: int = 6) -> List[str]:
    lines = blocker_groups(candidates)
    quiet = [(lv["lane"], row) for lv, row in running_rows(lanes) if row["state"] == "QUIET"]
    for lane, row in quiet:
        lines.append(f"lane {lane}: {row['id']} quiet {max(0, row['idle_secs']) // 60}m")
    paused = [lv["lane"] for lv in lanes if lv.get("paused") or lv.get("stopped")]
    if paused:
        lines.append("paused/stopped lanes: " + ",".join(paused))
    down = [lv["lane"] for lv in lanes if not lv.get("watcher") and first_open(lv)]
    if down:
        lines.append(f"{len(down)}/{len(lanes)} lane watchers down: {','.join(down)}")
    if future_approvals:
        lines.append(f"{future_approvals} future approval{'s' if future_approvals != 1 else ''} (not ready yet)")
    for name, info in sorted((snap.get("leases") or {}).items()):
        lines.append(f"lease {name}: {info['owner']} for {duration_since(info['since'])}")
    return lines[:limit] or ["nothing is holding progress"]


def started_seconds(res: Resolver, rid: str) -> int | None:
    try:
        parts = (res.cfg.state / f"{rid}.window").read_text().split()
        started = int(parts[2])
        return max(0, int(time.time()) - started)
    except (OSError, ValueError, IndexError):
        return None


def short_duration(seconds: int | None) -> str:
    if seconds is None:
        return "—"
    if seconds >= 3600:
        return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"
    if seconds >= 60:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    return f"{seconds}s"


def following_id(lv: Dict[str, Any], row: Dict[str, Any]) -> str:
    nxt = row_after(lv, row)
    return nxt["id"] if nxt else "lane complete"


def header_panel(snap: Dict[str, Any], lanes: List[Dict[str, Any]], needs_n: int,
                 interval: float = 1.0) -> Panel:
    closed = sum(lv["closed"] for lv in lanes)
    total = sum(lv["total"] for lv in lanes)
    running = len(running_rows(lanes))
    watchers = sum(bool(lv.get("watcher")) for lv in lanes)
    line = Text(snap["name"], style="bold white")
    line.append(f"   {closed}/{total} closed", style="green")
    line.append(f"   {running} running", style="cyan")
    line.append(f"   {needs_n} need you", style="bold yellow" if needs_n else "dim")
    line.append(f"   {watchers}/{len(lanes)} watchers", style="white" if watchers == len(lanes) else "yellow")
    line.append(f"   refresh {interval:g}s   {datetime.now():%H:%M:%S}", style="dim")
    return Panel(line, box=box.HEAVY, border_style="blue")


def running_panel(res: Resolver, lanes: List[Dict[str, Any]], width: int) -> Panel:
    rows = running_rows(lanes)
    if not rows:
        return Panel(Text("nothing running", style="dim"), title="RUNNING",
                     box=box.ROUNDED, border_style="dim")
    table = Table(box=None, expand=True, pad_edge=False)
    table.add_column("lane", width=5, no_wrap=True)
    table.add_column("row", style="bold", width=13, no_wrap=True, overflow="ellipsis")
    table.add_column("state", width=11, no_wrap=True)
    if width >= 100:
        table.add_column("executor", width=10, no_wrap=True, overflow="ellipsis")
        table.add_column("runtime", width=9, no_wrap=True)
    table.add_column("output", width=13, no_wrap=True)
    table.add_column("then", ratio=1, no_wrap=True, overflow="ellipsis")
    for lv, row in rows:
        output = (f"quiet {max(0, row['idle_secs']) // 60}m" if row["state"] == "QUIET"
                  else (f"active {row['idle_secs']}s" if row["idle_secs"] >= 0 else "active"))
        values: List[Any] = [lv["lane"], row["id"],
                             Text(row["state"], style=STATE_STYLE.get(row["state"], "white"))]
        if width >= 100:
            values.extend([row["executor"], short_duration(started_seconds(res, row["id"]))])
        values.extend([output, following_id(lv, row)])
        table.add_row(*values)
    return Panel(table, title="RUNNING", box=box.ROUNDED, border_style="cyan")


def next_panel(candidates: List[Tuple[Dict[str, Any], Dict[str, Any], str, str]], width: int) -> Panel:
    if not candidates:
        return Panel(Text("no unfinished lane heads", style="green"), title="NEXT ACROSS THE RIG",
                     box=box.ROUNDED, border_style="dim")
    table = Table(box=None, expand=True, pad_edge=False)
    table.add_column("#", width=3, style="dim")
    table.add_column("lane", width=5)
    table.add_column("row", width=13, style="bold", no_wrap=True, overflow="ellipsis")
    table.add_column("status", width=11, no_wrap=True)
    table.add_column("executor", width=10, no_wrap=True, overflow="ellipsis")
    table.add_column("reason", ratio=1, no_wrap=True, overflow="ellipsis")
    for i, (lv, row, status, reason) in enumerate(candidates, 1):
        values: List[Any] = [str(i), lv["lane"], row["id"],
                             Text(status, style=STATE_STYLE.get(status, "white")),
                             row.get("executor") or "—", reason]
        table.add_row(*values)
    return Panel(table, title=f"NEXT {len(candidates)} ACROSS THE RIG",
                 box=box.ROUNDED, border_style="blue")


def needs_panel(items: List[Tuple[str, str, str]]) -> Panel:
    lines = []
    for kind, target, instruction in items[:6]:
        line = Text(f"{kind:<9} {target:<12} ", style=STATE_STYLE.get(kind, "bold yellow"))
        line.append(instruction, style="white")
        lines.append(line)
    if len(items) > 6:
        lines.append(Text(f"… {len(items) - 6} more", style="dim"))
    if not lines:
        lines = [Text("nothing needs you", style="green")]
    return Panel(Group(*lines), title="NEEDS YOU", box=box.ROUNDED,
                 border_style="yellow" if items else "dim")


def held_panel(lines: List[str]) -> Panel:
    return Panel(Group(*(Text(line, style="yellow" if "block" in line or "down" in line else "dim")
                         for line in lines)), title="HELD UP",
                 box=box.ROUNDED, border_style="yellow" if lines != ["nothing is holding progress"] else "dim")


def lane_line(lv: Dict[str, Any]) -> Text:
    row = next((r for r in lv["rows"] if r["state"] in LIVE_STATES), None) or first_open(lv)
    watcher = "●" if lv.get("watcher") else "○"
    line = Text(f"{watcher} {lv['lane']:<3} {lv['closed']:>2}/{lv['total']:<2}  ",
                style="cyan" if lv.get("watcher") else "yellow")
    if row:
        line.append(f"{row['id']:<12} ", style="bold")
        line.append(row["state"], style=STATE_STYLE.get(row["state"], "white"))
    else:
        line.append("complete", style="green")
    return line


def lanes_panel(lanes: List[Dict[str, Any]], width: int) -> Panel:
    columns = 2 if width >= 72 and len(lanes) > 1 else 1
    grid = Table.grid(expand=True)
    for _ in range(columns):
        grid.add_column(ratio=1)
    lines = [lane_line(lv) for lv in lanes]
    for i in range(0, len(lines), columns):
        chunk = lines[i:i + columns]
        grid.add_row(*(chunk + [Text("")] * (columns - len(chunk))))
    return Panel(grid, title="LANES   ● watcher up  ○ watcher down",
                 box=box.ROUNDED, border_style="dim")


def build(res: Resolver, lane_filter: List[str], next_n: int,
          interval: float = 1.0) -> Group:
    snap = res.resolve()
    lanes = selected_lanes(snap, lane_filter)
    terminal = shutil.get_terminal_size((120, 40))
    width = terminal.columns
    candidates = next_candidates(lanes, snap, next_n)
    needs, future = actionable_items(lanes, snap)
    hold_limit = 4 if terminal.lines < 40 else 6
    held = held_up_lines(lanes, snap, next_candidates(lanes, snap, len(lanes)),
                         future, hold_limit)
    if width >= 100:
        pair = Table.grid(expand=True)
        pair.add_column(ratio=1)
        pair.add_column(ratio=1)
        pair.add_row(needs_panel(needs), held_panel(held))
        action_section: Any = pair
    else:
        action_section = Group(needs_panel(needs), held_panel(held))
    return Group(
        header_panel(snap, lanes, len(needs), interval),
        running_panel(res, lanes, width),
        next_panel(candidates, width),
        action_section,
        lanes_panel(lanes, width),
    )


def main() -> int:
    ap = argparse.ArgumentParser(prog="orch monitor")
    ap.add_argument("--interval", type=float, default=1.0,
                    help="refresh interval in seconds (default: 1)")
    ap.add_argument("--next", type=int, default=5, dest="next_n",
                    help="number of cross-rig next rows (default: 5)")
    ap.add_argument("--lane", action="append", default=[],
                    help="restrict the view to a lane (repeatable)")
    ap.add_argument("--once", action="store_true", help="render one snapshot and exit")
    ap.add_argument("--no-color", action="store_true", help="disable terminal colours")
    args = ap.parse_args()
    if args.interval <= 0:
        ap.error("--interval must be greater than zero")
    if args.next_n <= 0:
        ap.error("--next must be greater than zero")

    cfg = load()
    console = Console(no_color=args.no_color)
    if args.once:
        console.print(build(Resolver(cfg), args.lane, args.next_n, args.interval))
        return 0
    with Live(console=console, screen=True, refresh_per_second=8) as live:
        try:
            while True:
                live.update(build(Resolver(cfg), args.lane, args.next_n, args.interval))
                time.sleep(args.interval)
        except KeyboardInterrupt:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
