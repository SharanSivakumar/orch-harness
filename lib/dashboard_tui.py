#!/usr/bin/env python3
"""Rich terminal dashboard for the orchestration framework — 100% READ-ONLY.

Reads through state_resolver only, so what it shows is what `orch status` and
the Telegram bot show; the three surfaces cannot disagree. It never writes a
file, never touches a sentinel, never sends a keystroke into a pane. Starting,
stopping, or leaving it running cannot affect a single executor.

Layout
  header    instance, tmux session, clock, global counters
  current   one scan-friendly row per lane (the default for small panes)
  pipeline  every lane's ladder in a width-adaptive grid (full/focused modes)
  panes     a live tail of each RUNNING lane's focus window, side by side (idle lanes hidden)
  attention halts, gates awaiting release, stalls, held leases
  events    merged chronological event stream across lanes

  orch dashboard [current|focused|full] [--lane F] [--interval 3] [--once]
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config_parser import load                 # noqa: E402
from state_resolver import Resolver            # noqa: E402

try:
    from rich import box
    from rich.console import Console, Group
    from rich.layout import Layout
    from rich.live import Live
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text
except ImportError:                             # pragma: no cover
    sys.stderr.write("orch: the dashboard needs `rich` (pip install --user rich)\n")
    raise SystemExit(2)

STATE_STYLE = {
    "CLOSED": "dim green", "RUNNING": "bold cyan", "UNBLOCKING": "bold magenta",
    "QUIET": "yellow", "ORPHAN": "bold red",
    "GATED": "bold yellow", "SPEND_WAIT": "bold yellow", "WAITING": "yellow", "PENDING": "white",
    "PAUSED": "dim yellow", "MANUAL": "bold blue",
    "BLOCKED": "bold red", "ROUTE_A": "bold red", "HALTED": "bold red",
}
ATTENTION = {"BLOCKED", "ROUTE_A", "HALTED", "ORPHAN", "GATED", "MANUAL", "SPEND_WAIT"}
LIVE = {"RUNNING", "UNBLOCKING", "QUIET"}


def style_for(state: str) -> str:
    return STATE_STYLE.get(state, "white")


def header(snap: Dict[str, Any], mode: str = "current",
           lanes_filter: List[str] | None = None) -> Panel:
    lanes = snap["lanes"]
    closed = sum(l["closed"] for l in lanes)
    total = sum(l["total"] for l in lanes)
    running = sum(1 for l in lanes for r in l["rows"] if r["state"] in ("RUNNING", "UNBLOCKING", "QUIET"))
    attn = sum(1 for l in lanes for r in l["rows"] if r["state"] in ATTENTION)
    watchers = sum(1 for l in lanes if l["watcher"])
    t = Text()
    t.append(f"{snap['name']}", style="bold white")
    view = mode
    if lanes_filter:
        view += ":" + ",".join(lanes_filter)
    t.append(f"   view:{view}", style="bold blue")
    t.append(f"   tmux:{snap['tmux']}", style="dim")
    t.append(f"   {closed}/{total} closed", style="green")
    t.append(f"   {running} running", style="cyan")
    t.append(f"   {attn} need attention", style="red" if attn else "dim")
    t.append(f"   {watchers}/{len(lanes)} watchers up", style="white" if watchers == len(lanes) else "yellow")
    t.append(f"   {datetime.now():%H:%M:%S}", style="dim")
    return Panel(t, box=box.HEAVY, border_style="blue")


def lane_current_row(lv: Dict[str, Any]) -> Dict[str, Any] | None:
    """Return live work, otherwise the first unfinished row in queue order."""
    for row in lv["rows"]:
        if row["state"] in LIVE:
            return row
    next_id = lv.get("next", "")
    for row in lv["rows"]:
        if row["id"] == next_id:
            return row
    focus = lv.get("focus", "")
    for row in lv["rows"]:
        if row["id"] == focus or row.get("unblock_window") == focus:
            return row
    return next((row for row in lv["rows"] if row["state"] != "CLOSED"), None)


def filtered_lanes(snap: Dict[str, Any], lanes_filter: List[str]) -> List[Dict[str, Any]]:
    return [lv for lv in snap["lanes"]
            if not lanes_filter or lv["lane"] in lanes_filter]


def current_pipeline(snap: Dict[str, Any], lanes_filter: List[str]) -> Panel:
    """Dense overview that keeps every lane visible in a small terminal."""
    lanes = filtered_lanes(snap, lanes_filter)
    if not lanes:
        return Panel(Text("no matching lanes"), title="pipeline · current",
                     box=box.ROUNDED, border_style="dim")

    table = Table(box=box.SIMPLE_HEAD, expand=True, pad_edge=False,
                  show_edge=False, collapse_padding=True)
    table.add_column("lane", style="bold", width=6, no_wrap=True)
    table.add_column("done", width=7, no_wrap=True)
    table.add_column("current", style="bold", width=13, no_wrap=True,
                     overflow="ellipsis")
    table.add_column("state", width=11, no_wrap=True)
    table.add_column("executor", width=10, no_wrap=True, overflow="ellipsis")
    table.add_column("why / next", ratio=1, no_wrap=True, overflow="ellipsis")

    for lv in lanes:
        row = lane_current_row(lv)
        watcher = "●" if lv["watcher"] else "○"
        lane = Text(f"{watcher} {lv['lane']}",
                    style="cyan" if lv["watcher"] else "yellow")
        progress = f"{lv['closed']}/{lv['total']}"
        if row is None:
            table.add_row(lane, progress, "—", Text("CLOSED", style=style_for("CLOSED")),
                          "—", Text("lane complete", style="green"))
            continue

        executor = row["executor"] + ("*" if row["exec_source"] == "override" else "")
        state = row["state"]
        if state == "WAITING" and row["unmet_deps"]:
            why = "needs " + ",".join(row["unmet_deps"])
        elif state == "WAITING":
            why = "waiting"
        elif state == "GATED":
            why = f"approve {row['id']}"
        elif state == "MANUAL":
            why = f"open prompts/{row['id']}.manual.md"
        elif state == "ORPHAN":
            why = f"close or adopt {row['id']}"
        elif state in {"BLOCKED", "ROUTE_A", "HALTED"}:
            why = row["note"] or lv["headline"]
        elif state == "QUIET":
            why = f"quiet {max(0, row['idle_secs']) // 60}m"
        elif state in LIVE:
            why = "live session"
        elif state == "PAUSED":
            why = "orch resume"
        elif state == "PENDING":
            why = "ready to dispatch"
        else:
            why = row["plan"]
        table.add_row(lane, progress, row["id"], Text(state, style=style_for(state)),
                      executor, Text(why, style="dim" if state not in ATTENTION else "yellow"))

    key = Text("● watcher up   ○ watcher down   * assigned executor", style="dim")
    return Panel(Group(table, key), title="pipeline · current",
                 box=box.ROUNDED, border_style="blue")


def pipeline_table(snap: Dict[str, Any], lanes_filter: List[str],
                   available_height: int | None = None) -> Panel:
    # Only lanes still in progress earn a panel — a lane whose every row is
    # CLOSED (resolver headline "LANE COMPLETE") contributes nothing but an
    # empty "── N earlier closed ──" stub, so drop it to free vertical space
    # for the lanes that are actually still moving.
    filtered = [lv for lv in snap["lanes"]
                if (not lanes_filter or lv["lane"] in lanes_filter)
                and any(r["state"] != "CLOSED" for r in lv["rows"])]
    if not filtered:
        msg = ("no lanes configured" if not snap["lanes"]
               else "all lanes complete — nothing in progress")
        return Panel(Text(msg), title="pipeline",
                     box=box.ROUNDED, border_style="dim")
    # Lanes are arranged in a horizontal grid (not a single tall stack) so a
    # rig with many lanes all fit on one screen and the live panes below stay
    # visible. Column count adapts to the terminal: 1 col on narrow TTYs,
    # 2 on normal, 3 on very wide with 5+ lanes. Each lane panel is sized to
    # its own share of the width and the note column is truncated to what the
    # panel can actually hold, so the state/executor/gate columns never get
    # squeezed away.
    term_cols = shutil.get_terminal_size((160, 40)).columns
    n = len(filtered)
    if n <= 1:
        cols = 1
    elif term_cols >= 196 and n >= 5:
        cols = 3
    elif term_cols >= 128:
        cols = 2
    else:
        cols = 1
    cols = min(cols, n)
    grid_rows = (n + cols - 1) // cols
    # Fit every lane into the height the layout actually gave the pipeline.
    # The outer panel, lane panel and table header consume roughly six lines;
    # the rest is row data. This stops the first one or two long ladders from
    # pushing every later lane below the terminal viewport.
    row_budget = None
    if available_height is not None:
        lane_height = max(5, (max(1, available_height - 2)) // grid_rows)
        row_budget = max(2, lane_height - 4)
        # If the final grid row contains a short odd lane, reuse its otherwise
        # blank height in the fuller rows above it.
        last_chunk = filtered[(grid_rows - 1) * cols:]
        last_open = max((sum(r["state"] != "CLOSED" for r in lv["rows"])
                         for lv in last_chunk), default=0)
        spare = max(0, row_budget - last_open)
        if grid_rows > 1:
            row_budget += spare // (grid_rows - 1)

    def lane_panel(lv: Dict[str, Any]) -> Panel:
        try:
            env_up = int(os.environ.get("ORCH_DASH_UPCOMING", "0"))
        except ValueError:
            env_up = 0
        # Default (0) adapts the "upcoming" cap to the vertical space the
        # pipeline section actually has, so a long ladder like lane E fills
        # the screen instead of being cut at a small fixed number. Set
        # ORCH_DASH_UPCOMING to pin it explicitly.
        term_lines = shutil.get_terminal_size((160, 40)).lines or 40
        avail = max(6, int((term_lines - 3 - 8) * 5 / 6) - 8)
        up_next = env_up if env_up > 0 else avail
        important, waiting = [], []
        closed_n, wait_hidden = 0, 0
        current = lane_current_row(lv)
        for r in lv["rows"]:
            if r["state"] == "CLOSED":
                closed_n += 1
                continue
            if r is current or r["state"] != "WAITING":
                important.append(r)
            else:
                waiting.append(r)
        # In a short focused pane, the operator must see the current row and
        # actionable states before ordinary dependency-blocked future work.
        if current in important:
            important.remove(current)
            important.insert(0, current)
        ordered = important + waiting[:up_next]
        hidden = max(0, len(waiting) - up_next)
        if row_budget is not None and len(ordered) + (1 if hidden else 0) > row_budget:
            # Reserve the last line for the omitted-row count. `current` is
            # first in `important`, so it can never be squeezed out.
            keep = max(1, row_budget - 1)
            hidden += max(0, len(ordered) - keep)
            ordered = ordered[:keep]
        visible = ordered
        wait_hidden = hidden
        t = Table(box=box.SIMPLE_HEAD, expand=True, pad_edge=False, show_edge=False)
        t.add_column("row", style="bold", width=14, no_wrap=True)
        t.add_column("state", width=11, no_wrap=True)
        t.add_column("executor", width=11, no_wrap=True)
        t.add_column("gate", width=6, no_wrap=True)
        t.add_column("deps", width=14, no_wrap=True)
        for r in visible:
            gate = "-" if not r["gate"] else ("✗" if r["state"] == "GATED" else "✓")
            ex = r["executor"] + ("*" if r["exec_source"] == "override" else "")
            deps = ",".join(r["unmet_deps"]) if r["unmet_deps"] else ("-" if not r["deps"] else "ok")
            t.add_row(r["id"], Text(r["state"], style=style_for(r["state"])), ex,
                      Text(gate, style="yellow" if gate == "✗" else "dim"),
                      Text(deps, style="yellow" if r["unmet_deps"] else "dim"))
        if wait_hidden:
            t.add_row(Text(f"… {wait_hidden} more upcoming", style="dim"),
                      *("",) * 4)
        if closed_n:
            t.add_row(Text(f"── {closed_n} earlier closed ──", style="dim"),
                      *("",) * 4)
        current = lane_current_row(lv)
        banner = Text(f"Lane {lv['lane']}  {lv['closed']}/{lv['total']}", style="bold")
        if current:
            banner.append(f" · {current['id']} ")
            banner.append(current["state"], style=style_for(current["state"]))
        if not lv["watcher"]:
            banner.append(" · watcher down", style="bold red")
        return Panel(t, title=banner, title_align="left",
                     box=box.ROUNDED, border_style="blue")

    panels = [lane_panel(lv) for lv in filtered]
    rows = []
    for i in range(0, len(panels), cols):
        chunk = panels[i:i + cols]
        if len(chunk) == 1:
            rows.append(chunk[0])
        else:
            gr = Table.grid(expand=True)
            for _ in chunk:
                gr.add_column(ratio=1)
            gr.add_row(*chunk)
            rows.append(gr)
    return Panel(Group(*rows), title="pipeline", box=box.ROUNDED,
                 border_style="dim")


def focus_lanes(snap: Dict[str, Any], lanes_filter: List[str]) -> List[str]:
    """Choose a useful single-lane focus when the operator did not choose one."""
    if lanes_filter:
        return lanes_filter
    lanes = snap["lanes"]
    for wanted in (LIVE, ATTENTION):
        for lv in lanes:
            row = lane_current_row(lv)
            if row and row["state"] in wanted:
                return [lv["lane"]]
    for lv in lanes:
        if lane_current_row(lv):
            return [lv["lane"]]
    return []


def pane_grid(res: Resolver, snap: Dict[str, Any], lanes_filter: List[str], height: int) -> Panel:
    panels = []
    for lv in snap["lanes"]:
        if lanes_filter and lv["lane"] not in lanes_filter:
            continue
        win = lv["focus"]
        # Only a lane with a REAL live tmux window deserves a pane. Idle lanes
        # that are merely waiting/blocked with nothing dispatched add nothing
        # but "no live window" noise — hide them so the grid shows exactly the
        # sessions actually running, and the freed width goes to those panes.
        if not (win and win in res.windows):
            continue
        body = Text(res.capture(win, height) or "(pane empty)", style="white")
        title = f"{lv['lane']} · {win}"
        border = "cyan"
        panels.append(Panel(body, title=title, title_align="left",
                            box=box.ROUNDED, border_style=border))
    if not panels:
        return Panel(Text("nothing running — every lane is idle, waiting, or blocked"),
                     title="live panes (read-only capture)", border_style="dim")
    row = Table.grid(expand=True)
    for _ in panels:
        row.add_column(ratio=1)
    row.add_row(*panels)
    return Panel(row, title="live panes (read-only capture)", box=box.ROUNDED, border_style="dim")


def attention_panel(snap: Dict[str, Any]) -> Panel:
    lines: List[Text] = []
    for lv in snap["lanes"]:
        for r in lv["rows"]:
            if r["state"] in ("BLOCKED", "ROUTE_A", "HALTED"):
                lines.append(Text(f"  ⛔ {r['id']} {r['state']} — {r['note'][:90]}", style="bold red"))
            elif r["state"] == "GATED":
                lines.append(Text(f"  ⏸  {r['id']} awaiting gate — orch approve {r['id']}", style="bold yellow"))
            elif r["state"] == "MANUAL":
                lines.append(Text(f"  👤 {r['id']} is a manual row — prompts/{r['id']}.manual.md", style="bold blue"))
            elif r["state"] == "ORPHAN":
                lines.append(Text(f"  👻 {r['id']} ORPHAN window — a session this rig did not "
                                  f"dispatch, not working the row. Close it, or: orch adopt {r['id']}",
                                  style="bold red"))
            elif r["state"] == "QUIET":
                lines.append(Text(f"  💤 {r['id']} no output for {r['idle_secs'] // 60} min "
                                  f"(warned, never killed)", style="yellow"))
    for name, info in snap["leases"].items():
        lines.append(Text(f"  🔒 lease {name} held by {info['owner']} since {info['since']}", style="magenta"))
    if not lines:
        lines = [Text("  nothing needs you — every lane is chaining", style="green")]
    return Panel(Group(*lines), title="attention", box=box.ROUNDED,
                 border_style="red" if any("⛔" in str(l) for l in lines) else "dim")


def events_panel(res: Resolver, snap: Dict[str, Any], n: int = 1) -> Panel:
    rows: List[tuple] = []
    for lv in snap["lanes"]:
        f = res.cfg.state / f"events_{lv['lane']}.log"
        if not f.is_file():
            continue
        try:
            for line in f.read_text(errors="replace").splitlines()[-n:]:
                rows.append((line[:19], lv["lane"], line[19:].strip()))   # ts sorts by full date
        except Exception:
            continue
    rows.sort(key=lambda r: r[0])
    t = Table(box=None, expand=True, show_header=False, pad_edge=False, padding=0)
    t.add_column(width=8, style="dim"); t.add_column(width=3)
    t.add_column(overflow="ellipsis", no_wrap=True)
    for ts, lane, msg in rows[-n:]:
        t.add_row(ts[11:], Text(lane, style="bold blue"), Text(msg, style="white"))
    return Panel(t, title="events", box=box.ROUNDED, border_style="dim")


def build(res: Resolver, lanes_filter: List[str], pane_height: int,
          mode: str = "current") -> Layout:
    snap = res.resolve()
    shown_lanes = focus_lanes(snap, lanes_filter) if mode == "focused" else lanes_filter
    term = shutil.get_terminal_size((120, 40))
    lanes = filtered_lanes(snap, shown_lanes)
    has_live_pane = any(lv["focus"] and lv["focus"] in res.windows for lv in lanes)
    foot_size = 3
    live_size = 7 if mode == "focused" else 5
    reserved = 3 + foot_size + (live_size if has_live_pane else 0)
    pipe_height = max(8, term.lines - reserved)
    lay = Layout()
    if mode == "current":
        # Short splits get the one-row-per-lane scan. Once there is room, the
        # default becomes the real lane ladders and they own the screen.
        pipeline = (current_pipeline(snap, shown_lanes)
                    if term.lines < 38
                    else pipeline_table(snap, shown_lanes, pipe_height))
    elif mode == "focused":
        pipeline = pipeline_table(snap, shown_lanes, pipe_height)
    else:
        pipeline = pipeline_table(snap, shown_lanes, pipe_height)

    sections = [
        Layout(header(snap, mode, shown_lanes), size=3, name="head"),
        Layout(pipeline, name="pipe"),
    ]
    if has_live_pane:
        sections.append(Layout(pane_grid(res, snap, shown_lanes, pane_height),
                               size=live_size, name="panes"))
    sections.append(Layout(name="foot", size=foot_size))
    lay.split_column(*sections)
    lay["foot"].split_row(Layout(attention_panel(snap), name="attn"),
                          Layout(events_panel(res, snap), name="ev"))
    return lay


def main() -> int:
    ap = argparse.ArgumentParser(prog="orch dashboard")
    ap.add_argument("view", nargs="?", choices=("current", "focused", "focus", "full"),
                    help="current (compact default), focused, or full")
    ap.add_argument("--mode", choices=("current", "focused", "focus", "full"),
                    help="named alternative to the positional view")
    ap.add_argument("--lane", action="append", default=[],
                    help="restrict to a lane; focused mode auto-picks when omitted (repeatable)")
    ap.add_argument("--interval", type=float, default=3.0)
    ap.add_argument("--pane-lines", type=int, default=8)
    ap.add_argument("--once", action="store_true", help="render one frame and exit")
    ap.add_argument("--alt-screen", action="store_true",
                    help="force the terminal alternate screen (disabled automatically in Ghostty)")
    a = ap.parse_args()
    if a.view and a.mode and a.view != a.mode:
        ap.error("choose the view once (positional or --mode), not both")
    mode = a.mode or a.view or "current"
    if mode == "focus":
        mode = "focused"
    cfg = load()
    console = Console()
    if a.once:
        console.print(build(Resolver(cfg), a.lane, a.pane_lines, mode))
        return 0
    # Ghostty can clear Rich's alternate screen while a long-lived Live render
    # is still active, leaving an apparently empty dashboard even though the
    # resolver and one-shot renderer are healthy. Keep the live render in the
    # normal screen there; operators can explicitly opt back in if desired.
    terminal = " ".join((os.environ.get("TERM", ""),
                         os.environ.get("TERM_PROGRAM", ""))).lower()
    use_alt_screen = a.alt_screen or "ghostty" not in terminal
    # Refresh only after a fresh resolver snapshot is ready. Rich's background
    # refresh thread otherwise repaints the same full-screen layout four times
    # per second; in Ghostty's normal-screen mode that becomes a continuous
    # stream of clear/redraw sequences and can leave the display apparently
    # blank. One atomic repaint per polling interval is sufficient.
    with Live(console=console, screen=use_alt_screen,
              auto_refresh=False) as live:
        try:
            while True:
                live.update(build(Resolver(cfg), a.lane, a.pane_lines, mode),
                            refresh=True)
                time.sleep(a.interval)
        except KeyboardInterrupt:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
