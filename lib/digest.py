#!/usr/bin/env python3
"""The Owner's digest (v3): what happened since the last one, on the Owner's schedule.

`digest.times: ["08:00", "18:00"]` sends one digest per slot per day (as an ordinary
Telegram message — never a page) and writes the full text to
<instance>/digest/<YYYY-MM-DD>_<HHMM>.md. `orch digest [--send] [--since <epoch>]`
renders (and sends) one by hand.

What it reports, measured from state (never estimated):
  * rows accepted, parked (with the reason), halted or still open;
  * Steward incidents opened/closed and every Steward act with its revert;
  * Steward rulings (provisional) and the open Owner inbox (wall items);
  * HIGH first: class P rulings and panel outcomes (lib/panel.py);
  * executor health (breakers opened, open now);
  * executor fallback (lib/fallback.py): rows moved off a rate-limited executor, restores, executors in fallback now;
  * hours the rig sat idle while work was left (state/steward/rest.json);
  * human touches: operator commands seen in attempts.jsonl (reset/assign/approve/dispatch
    without a Steward or rearm author) — the target is 0;
  * spend against caps, when the spend governor is present (lib/spend.py);
  * queues edited outside `orch row add` (lib/rowadd.py, guard.queue_watch) and rows it added;
  * Steward acts the Owner reverted (lib/revert.py), each also a human touch.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parent))


def _jl(path: Path) -> List[Dict[str, Any]]:
    out = []
    try:
        for line in path.read_text().splitlines():
            try:
                out.append(json.loads(line))
            except Exception:
                pass
    except FileNotFoundError:
        pass
    return out


def human_touches(events: List[Dict[str, Any]]) -> List[str]:
    out = []
    for e in events:
        ev = e.get("event")
        by = str(e.get("by", "") or "")
        if ev == "reset" and not by:
            out.append(f"reset {e.get('id')}")
        elif ev in ("assign", "approve") and not by:
            out.append(f"{ev} {e.get('id')}")
        elif ev == "dispatch" and e.get("manual"):
            out.append(f"dispatch {e.get('id')}")
        elif ev == "round_start" and not by:
            out.append(f"round start {str(e.get('id', '')).replace('round:', '')}")
        elif ev == "park" and by == "owner":
            out.append(f"park {e.get('id')}")
        elif ev == "unpark" and by == "owner":
            out.append(f"unpark {e.get('id')}")
        elif ev == "steward_revert" and e.get("ok"):
            out.append(f"revert #{e.get('k')}")
        elif ev == "spend_answer":          # the one page's answer: money is the Owner's by design
            out.append(f"spend {e.get('req')} {e.get('letter')}")
        elif ev == "spend_caps_approved":
            out.append("spend caps approve")
        elif ev in ("phase_open", "phase_rest", "phase_close", "phase_approve", "phase_off") and by == "owner":
            out.append(f"phase {ev[6:]} {e.get('phase', '')}".rstrip())    # opening a phase is the Owner's by design
    return out


def render(rig, since: float) -> str:
    cfg, st = rig.cfg, rig.st
    import steward
    ev = [e for e in st.events() if float(e.get("epoch", 0) or 0) >= since]
    acc = [e["id"] for e in ev if e.get("event") == "accept"]
    parked = [e for e in ev if e.get("event") == "park"]
    opened = [e for e in ev if e.get("event") == "incident_open"]
    closed = [e for e in ev if e.get("event") == "incident_close"]
    acts = [a for a in _jl(cfg.state / "steward" / "log.jsonl") if float(a.get("epoch", 0) or 0) >= since]
    rulings = [r for r in _jl(cfg.state / "steward" / "rulings.jsonl") if float(r.get("epoch", 0) or 0) >= since]
    notes = [n for n in _jl(cfg.state / "steward" / "notes.jsonl")
             if time.mktime(time.strptime(n.get("ts", "1970-01-01 00:00:00"), "%Y-%m-%d %H:%M:%S")) >= since]
    inbox = steward.inbox_items(cfg)
    touches = human_touches(ev)
    breaker_open = [e for e in ev if e.get("event") == "breaker_open"]
    try:
        import health
        hl = health.Health(rig).summary_lines()
    except Exception:
        hl = []
    rest = {}
    try:
        rest = json.loads((cfg.state / "steward" / "rest.json").read_text()).get("by_day", {})
    except Exception:
        pass
    day0 = time.strftime("%F", time.localtime(since))
    idle_s = sum(v for d, v in rest.items() if d >= day0)
    from rows import load_rows
    rows, _ = load_rows(cfg, include_archived=False)
    phases: Dict[str, List[str]] = {}
    for r in rows:
        ph = st.phase(r.id) or "pending"
        phases.setdefault(ph, []).append(r.id)
    win = f"{time.strftime('%F %H:%M', time.localtime(since))} → {time.strftime('%F %H:%M')}"
    L = [f"# {cfg.name} — digest {win}", ""]
    L.append(f"**Accepted:** {len(acc)}" + (f" — {', '.join(acc[:25])}{' …' if len(acc) > 25 else ''}" if acc else ""))
    open_counts = ", ".join(f"{k} {len(v)}" for k, v in sorted(phases.items()) if k != "accepted")
    L.append(f"**Now:** {open_counts or 'every row accepted'}")
    if phases.get("parked"):
        L.append("**Parked (named carries):** " + ", ".join(
            f"{rid} ({str(st.result(rid).get('park_reason', ''))[:80]})" for rid in phases["parked"][:15]))
    L.append(f"**Idle while work was left:** {idle_s / 3600:.1f} h · **human touches:** {len(touches)}"
             + (f" ({', '.join(touches[:10])})" if touches else ""))
    added = [e for e in ev if e.get("event") == "row_add"]
    if added:
        L.append(f"**Rows added (orch row add):** {len(added)} — " + ", ".join(
            f"{e.get('id')} by {e.get('by')}" for e in added[:15]) + (" …" if len(added) > 15 else ""))
    L.append(f"**Steward:** {len(opened)} incident(s) opened, {len(closed)} closed, {len(acts)} act(s), "
             f"{len(rulings)} provisional ruling(s)")
    try:
        import spend
        L += spend.digest_lines(rig, since)
    except Exception:
        pass
    if hl or breaker_open:
        L.append(f"**Executor health:** {len(breaker_open)} breaker opening(s)" + (f"; open now: {'; '.join(hl)}" if hl else ""))
    high = [r for r in rulings if str(r.get("class", "")).upper() == "P"]
    panel_ev = [e for e in ev if e.get("event") in ("panel_confirm", "panel_object")]
    if high or panel_ev:
        # class P (new constructs, frozen schema/vocabulary changes, ADRs) is decided
        # provisionally by the panel and flagged HIGH; work builds on it until the Owner reverts it
        L += ["", "## HIGH — panel (class P) decisions: review these first", ""]
        for r in high[:20]:
            pn = r.get("panel") or {}
            L.append(f"- **{r['id']}** [P] {r.get('title')} — decider {pn.get('decider', '?')}, falsifier "
                     f"{pn.get('falsifier', '?')} confirmed · revert: `{r.get('revert')}`")
        for e in panel_ev[-20:]:
            what = (f"CONFIRMED, {e.get('executed')} staged action(s) executed"
                    + (" — one REFUSED, the rest held" if e.get("refused") else "")
                    if e["event"] == "panel_confirm" else f"OBJECTED ({e.get('verdict')}), nothing executed")
            L.append(f"- {e.get('id')}: {e.get('session')} {what} — {str(e.get('summary', ''))[:140]}")
    try:
        import phases as phasesmod
        L += phasesmod.digest_lines(cfg, ev)
    except Exception as e:
        L += ["", f"(phases: digest section failed — {type(e).__name__}: {e})"]
    try:
        import courseloop
        L += courseloop.digest_lines(cfg, ev)
    except Exception as e:
        L += ["", f"(course loops: digest section failed — {type(e).__name__}: {e})"]
    try:
        import rowadd
        L += rowadd.digest_lines(cfg, ev)
    except Exception as e:
        L += ["", f"(queue watch: digest section failed — {type(e).__name__}: {e})"]
    try:
        import fallback
        L += fallback.digest_lines(cfg, ev)
    except Exception as e:
        L += ["", f"(executor fallback: digest section failed — {type(e).__name__}: {e})"]
    reverts = [e for e in ev if e.get("event") in ("steward_revert", "steward_revert_refused")]
    if reverts:
        L += ["", "## Reverted by the Owner (orch revert / Telegram revert)", ""]
        for e in reverts[-20:]:
            if e["event"] == "steward_revert_refused":
                L.append(f"- #{e.get('k')} REFUSED (a forged or unproven revert request): {str(e.get('detail', ''))[:140]}")
            else:
                L.append(f"- #{e.get('k')} {e.get('action')} {e.get('target', '')} via {e.get('via')}: "
                         f"{'done' if e.get('ok') else 'FAILED'} — `{e.get('revert', '')}`")
    if inbox:
        L += ["", f"## Your inbox — {len(inbox)} open (nothing here paged you)", ""]
        for it in inbox[:20]:
            L.append(f"- **{it['k']}. {it['title']}** (row {it.get('row') or '-'}, {it['ts']}) — steward/INBOX.md")
    if rulings:
        L += ["", "## Steward rulings (provisional — revert any)", ""]
        for r in rulings[:20]:
            L.append(f"- {r['id']} [{r.get('class')}] {r.get('title')} — revert: `{r.get('revert')}`")
    if acts:
        L += ["", "## Steward acts (newest last; `orch revert <#>` undoes one)", ""]
        for a in acts[-30:]:
            L.append(f"- #{a['k']} {a['ts'][11:16]} incident {a.get('incident')} {a['action']} {a.get('target', '')} — "
                     f"{str(a.get('why', ''))[:120]} · revert: `{a.get('revert', '')}`")
    if closed:
        L += ["", "## Incidents closed", ""]
        for e in closed[-20:]:
            L.append(f"- #{e.get('n')} {e.get('outcome')}: {str(e.get('why', ''))[:160]}")
    if notes:
        L += ["", "## Notes from the Steward", ""] + [f"- {n['ts'][11:16]} {n['text'][:200]}" for n in notes[-10:]]
    return "\n".join(L) + "\n"


def phone_text(full: str, limit: int = 3500) -> str:
    """The Telegram copy: the summary lines plus as much of the rest as fits."""
    text = re.sub(r"\*\*", "", full)
    return text if len(text) <= limit else text[:limit].rsplit("\n", 1)[0] + "\n… (full digest in digest/)"


def write_and_send(rig, since: float, send: bool, slot: str = "") -> Path:
    cfg = rig.cfg
    d = cfg.instance / "digest"
    d.mkdir(exist_ok=True)
    text = render(rig, since)
    f = d / f"{time.strftime('%F')}_{(slot or time.strftime('%H:%M')).replace(':', '')}.md"
    f.write_text(text)
    keep = int((cfg.data.get("digest") or {}).get("keep_days", 30))
    for old in d.glob("*.md"):
        if time.time() - old.stat().st_mtime > keep * 86400:
            old.unlink(missing_ok=True)
    if send:
        subprocess.Popen([sys.executable, str(Path(__file__).resolve().parent / "telegram_bot.py"), "push", "--title",
                          "digest", "--body", phone_text(text), "--force"], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, env=dict(os.environ, ORCH_INSTANCE=str(cfg.instance)))
    rig.st.event("digest", "digest", file=f.name, sent=send)
    return f


def tick(rig) -> None:
    cfg = rig.cfg
    times = [str(t) for t in (cfg.data.get("digest") or {}).get("times") or []]
    if not times:
        return
    f = cfg.state / "digest.json"
    try:
        sent = json.loads(f.read_text())
    except Exception:
        sent = {}
    now_hm, today = time.strftime("%H:%M"), time.strftime("%F")
    for slot in sorted(times):
        if now_hm >= slot and sent.get(slot) != today:
            since = float(sent.get("_last_epoch", time.time() - 86400))
            write_and_send(rig, since, True, slot)
            sent[slot] = today
            sent["_last_epoch"] = time.time()
            f.write_text(json.dumps(sent, sort_keys=True))
            break


def main(argv: List[str]) -> int:
    from config_parser import load
    from engine import Rig
    rig = Rig(load())
    since = time.time() - 86400
    if "--since" in argv:
        since = float(argv[argv.index("--since") + 1])
    f = write_and_send(rig, since, "--send" in argv)
    print(f.read_text())
    print(f"(written {f})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
