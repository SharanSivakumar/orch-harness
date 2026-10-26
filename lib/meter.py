#!/usr/bin/env python3
"""The OpenCode meter (v3, drop C item 3): what the cheap tier spent, read from OpenCode's own database.

OpenCode keeps every assistant message in `~/.local/share/opencode/opencode.db`, table `message`, with a JSON
`data` column whose keys `cost`, `tokens`, `modelID`, `providerID` and `time` say what it cost. The same file holds
`account`, `credential` and `control_account` (secrets) and the message text (table `part`). This meter reads
ONLY the first set, and the restriction is enforced by SQLite, not by care:

  * the file is opened read-only (`mode=ro`, `PRAGMA query_only`);
  * an authorizer denies every read except table `message`, columns `id`, `time_created`, `data`, and every
    function except `json_extract` — `SELECT * FROM credential` fails, so does `SELECT lower(id) …`;
  * the one query extracts the numeric/name keys above; `data` is never selected whole.

Incremental: the cursor (`time_created`, then `id` for ties) lives host-wide in `<spend.dir>/meters/opencode.json`
under the spend lock, so a message is booked once on the host whichever rig polls. A first poll starts at the newest
message (no back-fill). An assistant message without `time.completed`, younger than `settle_secs`, stops the poll
(its cost is not final), and so does any message younger than 3 s (a concurrent insert may still commit behind it).
An unfinished message older than `settle_secs` (a long step) is passed and kept in the cursor's `pending` list,
re-read by id each poll and booked when it finishes (or after a day, at the cost it has then).

Attribution: each metering rig publishes the intervals of its OpenCode-family sessions to
`<spend.dir>/meters/sessions/<instance-hash>.json`. A message whose window [created, completed] overlaps exactly one
such session is that session's rig's and row's (a sub-session's parent row; a Steward/planner session's rig);
otherwise it is "shared": the rig whose sessions overlap it when there is one such rig, else the polling rig.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))

COLUMNS = ("id", "time_created", "data")
FUNCTIONS = ("json_extract",)
KEEP_SESSION_SECS = 8 * 86400
BATCH = 5000
LAG_MS = 3000          # messages newer than this are left for the next poll (concurrent inserts commit late)
QUERY = ("SELECT id, time_created, "
         "json_extract(data, '$.cost'), "
         "json_extract(data, '$.tokens.input'), json_extract(data, '$.tokens.output'), "
         "json_extract(data, '$.tokens.reasoning'), json_extract(data, '$.tokens.cache.read'), "
         "json_extract(data, '$.tokens.cache.write'), json_extract(data, '$.modelID'), "
         "json_extract(data, '$.providerID'), json_extract(data, '$.time.created'), "
         "json_extract(data, '$.time.completed') "
         "FROM message WHERE time_created > ? OR (time_created = ? AND id > ?) "
         "ORDER BY time_created, id LIMIT ?")
NEWEST = "SELECT time_created, id FROM message ORDER BY time_created DESC, id DESC LIMIT 1"
BY_ID = QUERY.split(" FROM message ")[0] + " FROM message WHERE id IN ({})"     # placeholders only, never data
PENDING_MAX, PENDING_SECS = 500, 86400   # unfinished messages past the cursor, re-read until they finish
FRESH_SECS = 120       # a sessions file not refreshed this long: its rig stopped (open sessions end at its mtime)
NAME = re.compile(r"^[A-Za-z0-9_.:/@+-]{1,96}$")


# -- config ---------------------------------------------------------------------------------------
def ocfg(cfg) -> Dict[str, Any]:
    return (cfg.data.get("spend") or {}).get("opencode") or {}


def on(cfg) -> bool:
    import spend
    return spend._flag(ocfg(cfg).get("meter"), False)


def db_path(cfg) -> Path:
    import pwd
    home = pwd.getpwuid(os.getuid()).pw_dir
    p = str(ocfg(cfg).get("db") or "~/.local/share/opencode/opencode.db").strip()
    if p == "~" or p.startswith("~/"):
        p = home + p[1:]
    return Path(p)


def metered_executor(cfg, x: str) -> bool:
    """Is executor x one the OpenCode meter measures (spend.opencode.executors, else its adapter family)?"""
    if not x:
        return False
    ex = ocfg(cfg).get("executors") or []
    if ex:
        return x in [str(e) for e in ex]
    return cfg.family(x) == str(ocfg(cfg).get("family") or "opencode")


def _num(v: Any) -> float:
    try:
        f = float(v)
        return f if f == f and f not in (float("inf"), float("-inf")) else 0.0
    except (TypeError, ValueError):
        return 0.0


# -- the database (read-only, whitelisted) --------------------------------------------------------
def _authorizer(action, a1, a2, _db, _trigger):
    if action == sqlite3.SQLITE_SELECT:
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_READ:
        return sqlite3.SQLITE_OK if (a1 == "message" and a2 in COLUMNS) else sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_FUNCTION:
        return sqlite3.SQLITE_OK if a2 in FUNCTIONS else sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_DENY


def connect(path: Path) -> sqlite3.Connection:
    """The db, read-only, behind the authorizer: only message.id/time_created/data and json_extract."""
    from urllib.parse import quote
    c = sqlite3.connect(f"file:{quote(str(path))}?mode=ro", uri=True, timeout=5)
    c.execute("PRAGMA query_only = 1")
    c.set_authorizer(_authorizer)
    return c


def _rows(c: sqlite3.Connection, sql: str, args: Tuple[Any, ...]) -> List[Dict[str, Any]]:
    out = []
    for r in c.execute(sql, args):
        # a model's answer carries tokens (a prompt has none): its cost is final once time.completed is set
        out.append({"id": str(r[0]), "tc": int(r[1] or 0), "answer": r[3] is not None, "cost": _num(r[2]),
                    "tokens": {"input": int(_num(r[3])), "output": int(_num(r[4])), "reasoning": int(_num(r[5])),
                               "cache_read": int(_num(r[6])), "cache_write": int(_num(r[7]))},
                    "model": str(r[8] or "") if NAME.match(str(r[8] or "")) else "?",
                    "upstream": str(r[9] or "") if NAME.match(str(r[9] or "")) else "?",
                    "created": _num(r[10]) or float(r[1] or 0), "completed": _num(r[11])})
    return out


def read(path: Path, cursor: Tuple[int, str], limit: int = BATCH) -> List[Dict[str, Any]]:
    """Messages after cursor (time_created, id), oldest first: only the cost/tokens/model/time keys."""
    c = connect(path)
    try:
        return _rows(c, QUERY, (int(cursor[0]), int(cursor[0]), str(cursor[1]), int(limit)))
    finally:
        c.close()


def read_ids(path: Path, ids: List[str]) -> List[Dict[str, Any]]:
    """The same keys for named messages (the unfinished ones past the cursor)."""
    if not ids:
        return []
    c = connect(path)
    try:
        return _rows(c, BY_ID.format(",".join("?" * len(ids))), tuple(str(i) for i in ids))
    finally:
        c.close()


def newest(path: Path) -> Tuple[int, str]:
    c = connect(path)
    try:
        r = c.execute(NEWEST).fetchone()
        return (int(r[0]), str(r[1])) if r else (0, "")
    finally:
        c.close()


# -- sessions and attribution ---------------------------------------------------------------------
def _ihash(inst: str) -> str:
    return hashlib.sha1(inst.encode()).hexdigest()[:12]


def sessions_dir(cfg) -> Path:
    import spend
    d = spend.spend_dir(cfg) / "meters" / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    return d


def parent_row(sid: str) -> str:
    """The row a session's cost belongs to: a row session's own id, a sub-session's parent row, else ''."""
    from capability import kind_of
    k = kind_of(sid)
    if k == "row":
        return sid
    m = re.match(r"^(?:ub|rv|pf)-(.+?)-[0-9]+(?:r[0-9]+)?$", sid)
    return m.group(1) if m else ""


def track(rig) -> None:
    """Publish this rig's OpenCode-family session intervals (one per dispatch: sid@start -> [start, end])."""
    import spend
    cfg = rig.cfg
    inst = spend._inst(cfg)
    f = sessions_dir(cfg) / f"{_ihash(inst)}.json"
    cur = spend._jread(f)
    ses = dict(cur.get("sessions") or {}) if cur.get("instance") == inst else {}
    now = time.time()
    changed = False
    try:
        from rows import load_rows
        known = {r.id for r in load_rows(cfg)[0]}
    except Exception:
        known = set()
    for p in cfg.state.glob("*.dispatch.json"):
        sid = p.name[: -len(".dispatch.json")]
        d = spend._jread(p)
        if not d or not metered_executor(cfg, str(d.get("executor", ""))):
            continue
        start = float(d.get("epoch", 0) or 0)
        if not start:
            continue
        # closed without an end time (a claim with no open window): it ended when it started, never "now"
        end = None if d.get("open") else float(d.get("ended_epoch", 0) or 0) or start
        key = f"{sid}@{int(start)}"
        row = parent_row(sid)
        rec = {"sid": sid, "row": row if row in known else "", "start": start, "end": end}
        if ses.get(key) != rec:
            ses[key] = rec
            changed = True
    for k in [k for k, v in ses.items() if (v.get("end") or now) < now - KEEP_SESSION_SECS]:
        ses.pop(k)
        changed = True
    try:
        stale = now - f.stat().st_mtime > FRESH_SECS / 2
    except OSError:
        stale = True
    if changed or stale:          # refreshed at least every minute: a silent file means a stopped rig
        spend._atomic(f, json.dumps({"instance": inst, "rig": cfg.name, "sessions": ses}, indent=1, sort_keys=True))


def intervals(cfg) -> List[Dict[str, Any]]:
    """Every metering rig's published session intervals on this host."""
    out = []
    now = time.time()
    for f in sessions_dir(cfg).glob("*.json"):
        d = {}
        try:
            d = json.loads(f.read_text())
            mt = f.stat().st_mtime
        except Exception:
            continue
        if not isinstance(d, dict):
            continue
        for v in (d.get("sessions") or {}).values():
            if isinstance(v, dict):
                v = dict(v, instance=str(d.get("instance", "")), rig=str(d.get("rig", "")))
                if v.get("end") is None and now - mt > FRESH_SECS:     # its rig stopped publishing
                    v["end"] = mt
                out.append(v)
    return out


def attribute(ivs: List[Dict[str, Any]], t0: float, t1: float, now: Optional[float] = None) -> Dict[str, Any]:
    """-> {instance, rig, row, session, attrib, n} for a message window [t0, t1] (seconds): exactly one overlapping
    session -> its rig and row; else shared — the one rig whose sessions overlap, or no instance (the caller's)."""
    now = now or time.time()
    t1 = max(t0, t1)
    hit = [s for s in ivs if float(s.get("start", 0) or 0) <= t1 and float(s.get("end") or now) >= t0]
    if len(hit) == 1:
        s = hit[0]
        return {"instance": s["instance"], "rig": s["rig"], "row": s.get("row", ""), "session": s.get("sid", ""),
                "attrib": "session", "n": 1}
    rigs = {(s["instance"], s["rig"]) for s in hit}
    if len(rigs) == 1:
        inst, name = next(iter(rigs))
        return {"instance": inst, "rig": name, "row": "", "session": "", "attrib": "shared", "n": len(hit)}
    return {"instance": "", "rig": "", "row": "", "session": "", "attrib": "shared", "n": len(hit)}


# -- the poll -------------------------------------------------------------------------------------
def cursor_path(cfg) -> Path:
    import spend
    d = spend.spend_dir(cfg) / "meters"
    d.mkdir(parents=True, exist_ok=True)
    return d / "opencode.json"


def poll(rig, now: Optional[float] = None) -> Dict[str, Any]:
    """Book the OpenCode messages since the host cursor. -> {booked: [ledger lines], skipped, why}."""
    import spend
    cfg = rig.cfg
    now = now or time.time()
    path = db_path(cfg)
    out: Dict[str, Any] = {"booked": [], "skipped": 0, "why": ""}
    if not path.is_file():
        out["why"] = f"no OpenCode db at {path}"
        return out
    track(rig)
    settle = float(ocfg(cfg).get("settle_secs", 900) or 0)
    key = str(path.resolve())
    with spend._Lock(cfg):
        cur_all = spend._jread(cursor_path(cfg))
        cur = cur_all.get(key) if isinstance(cur_all.get(key), dict) else None
        if cur is None:
            t, mid = newest(path)
            cur_all[key] = {"t": t, "id": mid, "since": time.strftime("%F %T"), "by": spend._inst(cfg)}
            spend._atomic(cursor_path(cfg), json.dumps(cur_all, indent=1, sort_keys=True))
            out["why"] = "cursor started at the newest message (no back-fill)"
            rig.st.event("spend", "meter_start", meter="opencode", db=str(path))
            return out
        pos = (int(cur.get("t", 0) or 0), str(cur.get("id", "")))
        pend: Dict[str, int] = {str(k): int(v) for k, v in (cur.get("pending") or {}).items()}
        ivs = intervals(cfg)
        me = spend._inst(cfg)
        todo = []
        for m in read_ids(path, sorted(pend)[:PENDING_MAX]):     # unfinished ones passed earlier
            if m["completed"] or now * 1000 - m["created"] > PENDING_SECS * 1000:
                pend.pop(m["id"], None)
                todo.append(m)
        for gone in [k for k, v in pend.items() if now * 1000 - v > PENDING_SECS * 1000 * 2]:
            pend.pop(gone, None)                                # deleted from the db: nothing to book
        for m in read(path, pos):
            if m["tc"] > now * 1000 - LAG_MS:      # a row inserted this instant may still have a twin committing
                out["why"] = "stopped at a message younger than the insert lag"
                break
            if m["answer"] and not m["completed"]:
                if (now * 1000 - m["created"]) < settle * 1000:
                    out["why"] = f"stopped at a message still running (created {int((now * 1000 - m['created']) / 1000)} s ago)"
                    break
                if len(pend) < PENDING_MAX:          # a long step: passed now, booked when it finishes
                    pend[m["id"]] = m["tc"]
                    pos = (m["tc"], m["id"])
                    continue
            pos = (m["tc"], m["id"])
            todo.append(m)
        for m in todo:
            if m["cost"] <= 0:
                out["skipped"] += 1
                continue
            t0 = m["created"] / 1000.0
            t1 = (m["completed"] or m["created"]) / 1000.0
            a = attribute(ivs, t0, t1, now)
            inst = a["instance"] or me
            name = a["rig"] if a["instance"] else cfg.name
            row = a["row"]
            rec = {"ts": time.strftime("%F %T", time.localtime(t1)), "epoch": round(t1, 3), "rig": name,
                   "instance": inst, "row": row, "session": a["session"], "provider": "opencode",
                   "model": m["model"], "upstream": m["upstream"], "usd": round(m["cost"], 6), "source": "metered",
                   "envelope": "", "phase": spend.phase_of(cfg, row) if inst == me else "", "meter": "opencode",
                   "attrib": a["attrib"], "overlap": a["n"], "tokens": m["tokens"],
                   "booked": time.strftime("%F %T")}
            spend._append(spend.ledger_path(cfg), rec)
            out["booked"].append(rec)
        cur_all[key] = dict(cur, t=pos[0], id=pos[1], polled=time.strftime("%F %T"), pending=pend)
        spend._atomic(cursor_path(cfg), json.dumps(cur_all, indent=1, sort_keys=True))
    if out["booked"]:
        mine = [r for r in out["booked"] if r["instance"] == spend._inst(cfg)]
        rig.st.event("spend", "meter_booked", meter="opencode", n=len(out["booked"]), mine=len(mine),
                     usd=round(sum(r["usd"] for r in out["booked"]), 6))
    return out


def status(cfg) -> str:
    if not on(cfg):
        return "OpenCode meter: off (spend.opencode.meter)"
    import spend
    cur = spend._jread(cursor_path(cfg)).get(str(db_path(cfg).resolve()) if db_path(cfg).exists() else str(db_path(cfg)))
    if not isinstance(cur, dict):
        return f"OpenCode meter: on, {db_path(cfg)} — not polled yet"
    return (f"OpenCode meter: on, {db_path(cfg)} — cursor {cur.get('t')} "
            f"(started {cur.get('since', '?')}, last poll {cur.get('polled', '-')})")
