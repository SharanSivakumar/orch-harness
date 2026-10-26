#!/usr/bin/env python3
"""The spend governor (v3, drop C): money is the only thing that pages.

    orch spend run --provider P --est USD [--row id] [--envelope id] [--model M] -- <cmd…>
    orch spend caps                 the approved caps, and whether SPEND_CAPS.yaml was edited since
    orch spend caps approve         the Owner approves <instance>/SPEND_CAPS.yaml (operator-only)
    orch spend answer <id> A|B      the Owner answers a spend request at the shell (operator-only)
    orch spend status               spend against each approved cap, open requests
    orch spend ledger [n]           the last n ledger lines of this rig

Ledger. One host-wide JSON-lines file, `<dir>/spend.jsonl` (`spend.dir`, default ~/.local/state/orch;
ORCH_SPEND_DIR overrides it outside session panes — tests), one line per cost: ts, rig, instance, row,
session, provider, model, usd, source (metered | estimated | billing), envelope, phase. Caps count this
rig's lines only (its instance path).

Caps. `<instance>/SPEND_CAPS.yaml` holds {daily_usd, monthly_usd, per_phase: {P2: …}, per_provider:
{opencode: …}, per_row_default, per_row: {<row>: …}}. It is a protected path (lib/protected.py). Nothing
reads it for limits: `orch spend caps approve` (operator-only) validates it, copies it to
state/spend_caps.approved.yaml and records both shas in state/spend_caps.approved.json. An edit after that
is ignored (the approved copy stays in force) and the engine opens a Steward incident `spend_caps`.
With the governor on and nothing approved, every cap is closed (daily_usd 0): money waits for the Owner.

Windows: daily_usd = today, monthly_usd and per_provider = this calendar month, per_phase = the phase's
whole life (drop D's phases.yaml maps rounds to phases), per_row / per_row_default = the row's whole life.

`orch spend run` reserves the estimate against every applicable cap under a host-wide lock (live
reservations count as spent), runs the command with $ORCH_SPEND_OUT (it may write
`ORCH_SPEND_ACTUAL=<usd>` and `ORCH_SPEND_MODEL=<m>`), and records the actual (source metered) or the
estimate (source estimated). A reservation whose process died is recorded at its estimate. When a cap
would be crossed it runs nothing, files state/spend/requests/<id>.json, prints `SPEND_WAIT <id>` and
exits 75. The row claims BLOCKED with that line; the engine puts it in phase `spend_wait` (no unblocker, no
Steward), sends ONE critical page (page_class=spend) per request, and re-dispatches the row when the
request is approved.

Answers. A raises the approved copy by exactly the shortfall the request names (per cap), records it in
state/spend/answers.jsonl as an Owner ruling (SPEND-<id>) and wakes the row; B holds (the row stays in
spend_wait; a later A still releases it). Telegram `spend <id> A|B` (or the letter as a reply to the page)
travels like `revert <k>` (lib/revert.py): the request sealed in a memfd the rig's bot holds, a doorbell
control/SPEND_<id>.json, proved by the engine; its answer goes back sealed by the engine.

Meters (drop C item 3, vC2). The OpenCode meter (lib/meter.py, spend.opencode.meter) books what the cheap tier
spent from OpenCode's own db (read-only; cost/tokens/model/time keys only). A metered crossing files ONE request per
(row or rig, opencode) with source `meter` — each cap's shortfall plus spend.opencode.headroom_usd — pages once, and
opens the OpenCode spend hold (state/spend/hold.json, kind spend): nothing new dispatches to the metered executors
(a per-row crossing: to that row) until the request is approved or its caps no longer cross (`lapsed`).
Billing reconcile (spend.reconcile: [{provider, cmd, every}], cmd prints `SPEND <provider> <usd_month_to_date>`)
compares the host-wide month-to-date ledger of that provider; a gap beyond spend.reconcile_tolerance_usd opens a
Steward incident `spend_reconcile`. At spend.economy_pct (80) of any approved cap: state/spend/economy and a digest
line (the Steward prefers cheaper tiers).
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import re
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))

EXIT_WAIT = 75                      # EX_TEMPFAIL: the command did not run; a cap would be crossed
ID_RE = re.compile(r"^sp([0-9]+)$")
WAIT_RE = re.compile(r"\bSPEND_WAIT\s+(sp[0-9]+)\b")
DOOR_RE = re.compile(r"^SPEND_(sp[0-9]+)\.json$")
REQ_MEMFD, ANS_MEMFD = "orch-spend", "orch-spend-answer"
TOKEN = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
SCALARS = ("daily_usd", "monthly_usd", "per_row_default")
MAPS = ("per_phase", "per_provider", "per_row")
EPS = 1e-9
# nothing approved: closed caps. An A then grants the shortfall on each, so it is a one-off for that row
# (per_row) inside a rig still closed for every other row and capped for the month
CLOSED = {"daily_usd": 0.0, "monthly_usd": 0.0, "per_row_default": 0.0}
_HELD: Dict[str, Tuple[int, float]] = {}       # engine side: sealed answers kept open for the bot
_LAST: Dict[str, float] = {}                    # engine side: when each periodic spend job last ran (per rig)
SPEND_LINE = re.compile(r"^SPEND\s+([A-Za-z0-9_.:-]{1,64})\s+([0-9]+(?:\.[0-9]+)?)\s*$")


# -- config, paths --------------------------------------------------------------------------------
def scfg(cfg) -> Dict[str, Any]:
    return cfg.data.get("spend") or {}


def _flag(v: Any, default: bool = False) -> bool:
    if v is None:
        return default
    if isinstance(v, str):
        return v.strip().lower() not in ("false", "off", "no", "0", "")
    return bool(v)


def enabled(cfg) -> bool:
    return _flag(scfg(cfg).get("enabled"), False)


def _inside(cfg) -> str:
    try:
        import capability
        s = capability.inside(cfg) if capability.enabled(cfg) else None
        return str(s.get("sid")) if s else ""
    except Exception:
        return ""


def spend_dir(cfg, make: bool = True) -> Path:
    """The host-wide ledger directory: spend.dir, else ~/.local/state/orch. ORCH_SPEND_DIR overrides it for
    the operator's shell and the engine (tests); inside a session pane it is ignored unless it names the same
    directory (a session never chooses a fresh, empty ledger to dodge a cap)."""
    import pwd
    home = pwd.getpwuid(os.getuid()).pw_dir     # never $HOME: a session could point it at an empty ledger
    conf = str(scfg(cfg).get("dir") or "").strip()
    if conf.startswith("~/") or conf == "~":
        conf = home + conf[1:]
    base = Path(conf) if conf else Path(home) / ".local" / "state" / "orch"
    if not base.is_absolute():
        base = cfg.instance / base
    env = os.environ.get("ORCH_SPEND_DIR", "").strip()
    if env:
        e = Path(home + env[1:] if env.startswith("~/") else env)
        if e.resolve() != base.resolve() and _inside(cfg):
            print(f"[orch] ORCH_SPEND_DIR ignored inside a session (the rig's ledger is {base})", file=sys.stderr)
        else:
            base = e
    if make:
        base.mkdir(parents=True, exist_ok=True)
    return base


def ledger_path(cfg) -> Path:
    return spend_dir(cfg) / "spend.jsonl"


def sdir(cfg) -> Path:
    d = cfg.state / "spend"
    for sub in ("requests", "answers"):
        (d / sub).mkdir(parents=True, exist_ok=True)
    return d


def caps_file(cfg) -> Path:
    return cfg.instance / "SPEND_CAPS.yaml"


def approved_yaml(cfg) -> Path:
    return cfg.state / "spend_caps.approved.yaml"


def approved_rec(cfg) -> Path:
    return cfg.state / "spend_caps.approved.json"


def _inst(cfg) -> str:
    return str(Path(cfg.instance).resolve())


def _atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def _jread(path: Path, default=None):
    try:
        v = json.loads(path.read_text())
        return v if isinstance(v, dict) else ({} if default is None else default)
    except Exception:
        return {} if default is None else default


def _sha_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _sha_file(p: Path) -> str:
    try:
        return _sha_bytes(p.read_bytes())
    except FileNotFoundError:
        return ""


class _Lock:
    """The host-wide lock: every check + reserve, request, answer and caps write runs under it."""

    def __init__(self, cfg):
        self.f = open(spend_dir(cfg) / "spend.lock", "a")

    def __enter__(self):
        fcntl.flock(self.f, fcntl.LOCK_EX)
        return self

    def __exit__(self, *a):
        fcntl.flock(self.f, fcntl.LOCK_UN)
        self.f.close()


def usd(x: Any) -> str:
    return f"USD {float(x):,.2f}"


def _cents_up(x: float) -> float:
    return math.ceil(round(x * 100, 6) - EPS) / 100.0


# -- caps -----------------------------------------------------------------------------------------
def validate(data: Any) -> Tuple[Dict[str, Any], List[str]]:
    """-> (normalised caps, problems). Unknown keys, negative or non-numeric limits are problems."""
    probs: List[str] = []
    caps: Dict[str, Any] = {}
    if data is None:
        data = {}
    if not isinstance(data, dict):
        return {}, ["SPEND_CAPS.yaml must be a mapping"]
    for k, v in data.items():
        if k in SCALARS:
            if v is None:
                continue
            try:
                f = float(v)
            except (TypeError, ValueError):
                probs.append(f"{k}: {v!r} is not a number")
                continue
            if f < 0 or math.isnan(f) or math.isinf(f):
                probs.append(f"{k}: {v!r} must be a finite number >= 0")
                continue
            caps[k] = f
        elif k in MAPS:
            if v is None:
                continue
            if not isinstance(v, dict):
                probs.append(f"{k} must be a mapping of name -> USD")
                continue
            sub = {}
            for n, x in v.items():
                if not TOKEN.match(str(n)):
                    probs.append(f"{k}.{n}: not a plain name")
                    continue
                try:
                    f = float(x)
                except (TypeError, ValueError):
                    probs.append(f"{k}.{n}: {x!r} is not a number")
                    continue
                if f < 0 or math.isnan(f) or math.isinf(f):
                    probs.append(f"{k}.{n}: {x!r} must be a finite number >= 0")
                    continue
                sub[str(n)] = f
            caps[k] = sub
        else:
            probs.append(f"unknown key {k!r} (known: {', '.join(SCALARS + MAPS)})")
    return caps, probs


def _dump(caps: Dict[str, Any]) -> str:
    import yaml
    return ("# The Owner-approved spend caps (orch spend caps approve; raised by spend answers A). The engine\n"
            "# reads only this copy; <instance>/SPEND_CAPS.yaml is the Owner's draft.\n"
            + yaml.safe_dump(caps, sort_keys=True))


def approved(cfg) -> Tuple[Optional[Dict[str, Any]], str]:
    """-> (the caps in force, note). None = nothing approved yet. The copy is used when its sha is the one
    recorded; otherwise the caps embedded in the record (the last approved state) are."""
    rec = _jread(approved_rec(cfg))
    if not rec or not isinstance(rec.get("caps"), dict):
        return None, "no approved caps (orch spend caps approve)"
    p = approved_yaml(cfg)
    if p.is_file() and _sha_file(p) == rec.get("copy_sha"):
        try:
            import yaml
            caps, probs = validate(yaml.safe_load(p.read_text()))
            if not probs:
                return caps, ""
        except Exception:
            pass
    caps, _ = validate(rec["caps"])
    return caps, "the approved copy changed outside orch — the recorded approval is used"


def tamper(cfg) -> str:
    """'' when SPEND_CAPS.yaml and the approved copy are as approved; else what changed."""
    rec = _jread(approved_rec(cfg))
    if not rec:
        return ""
    out = []
    cur = _sha_file(caps_file(cfg))
    if rec.get("approved_at") and cur != str(rec.get("from_sha", "")):   # grants only: the draft is just unapproved
        out.append("SPEND_CAPS.yaml " + ("removed" if not cur else "edited") + " since it was approved "
                   f"({str(rec.get('approved_at', '?'))}) — the approved copy stays in force")
    if _sha_file(approved_yaml(cfg)) != str(rec.get("copy_sha", "")):
        out.append("state/spend_caps.approved.yaml changed outside orch — the recorded approval stays in force")
    return "; ".join(out)


def _write_approved(cfg, caps: Dict[str, Any], from_sha: str, by: str, extra: Dict[str, Any]) -> None:
    text = _dump(caps)
    _atomic(approved_yaml(cfg), text)
    rec = _jread(approved_rec(cfg))
    rec.update(caps=caps, from_sha=from_sha, copy_sha=_sha_bytes(text.encode()), by=by)
    rec.update(extra)
    _atomic(approved_rec(cfg), json.dumps(rec, indent=1, sort_keys=True))


def approve(cfg, by: str = "owner") -> Tuple[bool, str]:
    f = caps_file(cfg)
    if not f.is_file():
        return False, f"no {f} — write the caps first"
    raw = f.read_bytes()
    try:
        import yaml
        data = yaml.safe_load(raw.decode())
    except Exception as e:
        return False, f"SPEND_CAPS.yaml does not parse: {e}"
    caps, probs = validate(data)
    if probs:
        return False, "SPEND_CAPS.yaml refused: " + "; ".join(probs)
    with _Lock(cfg):
        rec = _jread(approved_rec(cfg))
        hist = list(rec.get("history") or [])[-20:]
        hist.append({"ts": time.strftime("%F %T"), "by": by, "from_sha": _sha_bytes(raw)})
        _write_approved(cfg, caps, _sha_bytes(raw), by,
                        {"approved_at": time.strftime("%F %T"), "approved_epoch": int(time.time()), "history": hist})
    return True, f"approved: {describe(caps)}"


def describe(caps: Optional[Dict[str, Any]]) -> str:
    if caps is None:
        return "none (every cap closed)"
    parts = [f"{k} {usd(caps[k])}" for k in SCALARS if k in caps]
    for m in MAPS:
        for n, v in sorted((caps.get(m) or {}).items()):
            parts.append(f"{m}.{n} {usd(v)}")
    return ", ".join(parts) or "empty (no cap: unlimited)"


# -- ledger and reservations ----------------------------------------------------------------------
def _append(path: Path, rec: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.write(json.dumps(rec, sort_keys=True) + "\n")
        fcntl.flock(f, fcntl.LOCK_UN)


def record(cfg, row: str, session: str, provider: str, model: str, amount: float, source: str,
           envelope: str = "", phase: str = "", **extra: Any) -> Dict[str, Any]:
    rec = {"ts": time.strftime("%F %T"), "epoch": round(time.time(), 3), "rig": cfg.name, "instance": _inst(cfg),
           "row": row, "session": session, "provider": provider, "model": model, "usd": round(float(amount), 6),
           "source": source, "envelope": envelope, "phase": phase}
    rec.update({k: v for k, v in extra.items() if v not in (None, "")})
    _append(ledger_path(cfg), rec)
    return rec


def lines(cfg, all_rigs: bool = False) -> List[Dict[str, Any]]:
    out = []
    me = _inst(cfg)
    try:
        with open(spend_dir(cfg, make=False) / "spend.jsonl") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if isinstance(r, dict) and (all_rigs or r.get("instance") == me):
                    out.append(r)
    except FileNotFoundError:
        pass
    return out


def _resdir(cfg) -> Path:
    d = spend_dir(cfg) / "reservations"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _alive(pid: Any, start: Any) -> bool:
    try:
        pid = int(pid)
        os.kill(pid, 0)
    except (OSError, TypeError, ValueError):
        return False
    try:
        import capability
        st = capability.started(pid)
        return not start or not st or abs(float(st) - float(start)) < 2.0
    except Exception:
        return True


def reservations(cfg) -> List[Dict[str, Any]]:
    """This rig's live reservations. Call under the lock: a reservation whose process died is recorded in
    the ledger at its estimate (the money may have been spent) and removed."""
    out = []
    me = _inst(cfg)
    for p in sorted(_resdir(cfg).glob("*.json")):
        r = _jread(p)
        if not r:
            p.unlink(missing_ok=True)
            continue
        if _alive(r.get("pid"), r.get("start")):
            if r.get("instance") == me:
                out.append(r)
            continue
        try:
            if not any(x.get("res") == p.name for x in lines(cfg, all_rigs=True)):   # booked before it was removed
                at = float(r.get("epoch", 0) or 0) or time.time()
                rec = {k: r.get(k, "") for k in ("rig", "instance", "row", "session", "provider", "envelope", "phase")}
                rec.update(ts=time.strftime("%F %T", time.localtime(at)), epoch=round(at, 3), res=p.name,
                           usd=float(r.get("usd", 0) or 0), source="estimated", model=r.get("model", ""),
                           est=r.get("usd"), note="orch spend run ended without recording its cost — the estimate is booked")
                _append(spend_dir(cfg) / "spend.jsonl", rec)
        finally:
            p.unlink(missing_ok=True)
    return out


def _window(rec: Dict[str, Any], kind: str, now: float) -> bool:
    try:
        t = time.localtime(float(rec.get("epoch") or 0))
    except Exception:
        return False
    n = time.localtime(now)
    if kind == "day":
        return (t.tm_year, t.tm_yday) == (n.tm_year, n.tm_yday)
    if kind == "month":
        return (t.tm_year, t.tm_mon) == (n.tm_year, n.tm_mon)
    return True


def applicable(caps: Dict[str, Any], row: str, provider: str, phase: str) -> List[Dict[str, Any]]:
    """The caps a cost of (row, provider, phase) counts against: name, window, filter, limit."""
    out = []
    if "daily_usd" in caps:
        out.append({"cap": "daily_usd", "window": "day", "limit": caps["daily_usd"]})
    if "monthly_usd" in caps:
        out.append({"cap": "monthly_usd", "window": "month", "limit": caps["monthly_usd"]})
    if provider in (caps.get("per_provider") or {}):
        out.append({"cap": f"per_provider.{provider}", "window": "month", "provider": provider,
                    "limit": caps["per_provider"][provider]})
    if phase and phase in (caps.get("per_phase") or {}):
        out.append({"cap": f"per_phase.{phase}", "window": "all", "phase": phase, "limit": caps["per_phase"][phase]})
    if row:
        if row in (caps.get("per_row") or {}):
            out.append({"cap": f"per_row.{row}", "window": "all", "row": row, "limit": caps["per_row"][row]})
        elif "per_row_default" in caps:
            out.append({"cap": f"per_row.{row}", "window": "all", "row": row, "limit": caps["per_row_default"],
                        "default": True})
    return out


def _counts(c: Dict[str, Any], r: Dict[str, Any]) -> bool:
    return all(str(r.get(k, "")) == str(c[k]) for k in ("provider", "phase", "row") if k in c)


def standing(cfg, caps: Dict[str, Any], row: str, provider: str, phase: str,
             res: Optional[List[Dict[str, Any]]] = None, now: Optional[float] = None) -> List[Dict[str, Any]]:
    """Each applicable cap with what is spent (ledger) and reserved (live runs) in its window."""
    now = now or time.time()
    led = lines(cfg)
    res = reservations(cfg) if res is None else res
    out = []
    for c in applicable(caps, row, provider, phase):
        spent = sum(float(r.get("usd", 0) or 0) for r in led if _window(r, c["window"], now) and _counts(c, r))
        resv = sum(float(r.get("usd", 0) or 0) for r in res if _counts(c, r))
        out.append(dict(c, spent=round(spent, 6), reserved=round(resv, 6)))
    return out


def crossing(cfg, caps: Dict[str, Any], row: str, provider: str, phase: str, est: float,
             res: Optional[List[Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
    """The caps this estimate would cross, each with the shortfall `need` (whole cents, rounded up)."""
    out = []
    for c in standing(cfg, caps, row, provider, phase, res):
        total = c["spent"] + c["reserved"] + est
        if total > c["limit"] + EPS:
            out.append(dict(c, need=_cents_up(total - c["limit"])))
    return out


def est_floor(cfg, provider: str) -> float:
    per = scfg(cfg).get("min_est") or {}
    try:
        return max(0.0, float(per.get(provider, scfg(cfg).get("min_est_usd", 0.01)) or 0))
    except (TypeError, ValueError):
        return 0.01


def grants(cfg, now: Optional[float] = None) -> List[Dict[str, Any]]:
    """Approved requests their row has not used yet: the raised headroom is held for that row and provider
    (other callers count it as running) until the row's next run takes it or spend.grant_secs pass."""
    now = now or time.time()
    keep = float(scfg(cfg).get("grant_secs", 86400) or 0)
    out = []
    for r in requests(cfg, ("approved",)):
        if r.get("consumed") or not r.get("row") or r.get("source") == "meter":   # a meter's raise is no run's
            continue
        try:
            at = time.mktime(time.strptime(str(r.get("answered")), "%Y-%m-%d %H:%M:%S"))
        except Exception:
            at = 0
        if keep and now - at > keep:
            continue
        out.append({"id": r["id"], "row": r["row"], "provider": r["provider"], "phase": r.get("phase", ""),
                    "usd": r["est"]})
    return out


def consume_grant(cfg, rid: str) -> None:
    rec = load_request(cfg, rid)
    if rec and not rec.get("consumed"):
        rec["consumed"] = time.strftime("%F %T")
        save_request(cfg, rec)


def phase_of(cfg, row: str) -> str:
    """The phase a row's round belongs to (drop D: the trusted phases record, lib/phases.py), else ''."""
    if not row:
        return ""
    try:
        import phases
        if not phases.active(cfg):
            return ""
        from rows import find_row
        r = find_row(cfg, row)
        return phases.Fence(cfg).of_lane(r.lane)[1] if r else ""
    except Exception:
        return ""


# -- requests -------------------------------------------------------------------------------------
def req_path(cfg, rid: str) -> Path:
    return sdir(cfg) / "requests" / f"{rid}.json"


def load_request(cfg, rid: str) -> Optional[Dict[str, Any]]:
    if not ID_RE.match(str(rid)):
        return None
    return next((r for r in requests(cfg) if r["id"] == rid), None)


def save_request(cfg, rec: Dict[str, Any]) -> None:
    _atomic(req_path(cfg, rec["id"]), json.dumps(rec, indent=1, sort_keys=True))


def requests(cfg, status: Optional[Tuple[str, ...]] = None) -> List[Dict[str, Any]]:
    out = []
    for p in (cfg.state / "spend" / "requests").glob("sp*.json"):
        r = _jread(p)
        if not (ID_RE.match(p.stem) and r.get("id") == p.stem):
            continue
        try:   # a malformed request never crashes a tick: it is skipped
            r["est"] = float(r.get("est", 0) or 0)
            r["provider"] = str(r.get("provider", "") or "?")
            r["crossed"] = [c for c in (r.get("crossed") or []) if isinstance(c, dict) and c.get("cap")]
            for c in r["crossed"]:
                c["need"] = float(c.get("need", 0) or 0)
                c["limit"] = float(c.get("limit", 0) or 0)
        except (TypeError, ValueError, AttributeError):
            continue
        if status is None or r.get("status") in status:
            out.append(r)
    return sorted(out, key=lambda r: int(ID_RE.match(r["id"]).group(1)))


def content_sha(rec: Dict[str, Any]) -> str:
    """What the Owner decides on: the row, provider, estimate and the caps with their shortfalls."""
    core = {k: rec.get(k) for k in ("id", "row", "provider", "est", "envelope", "phase")}
    core["crossed"] = [{k: c.get(k) for k in ("cap", "limit", "need", "default")} for c in rec.get("crossed") or []]
    return _sha_bytes(json.dumps(core, sort_keys=True).encode())[:16]


def _next_id(cfg) -> str:
    f = sdir(cfg) / "counter"
    try:
        n = int(f.read_text().strip() or "0")
    except Exception:
        n = 0
    n = max([n] + [int(ID_RE.match(r["id"]).group(1)) for r in requests(cfg)]) + 1
    _atomic(f, f"{n}\n")
    return f"sp{n}"


def open_request(cfg, row: str, session: str, provider: str, est: float, envelope: str, phase: str,
                 crossed: List[Dict[str, Any]], what: str, source: str = "run") -> Tuple[Dict[str, Any], bool]:
    """File (or find) the request for this need. Call under the lock. -> (request, new). A re-run of the
    same need (row, provider, estimate, envelope, caps) while its request is open or held gets the same
    id — one page per request id, never one per attempt."""
    caps_set = sorted(c["cap"] for c in crossed)
    fresh = [{k: c.get(k) for k in ("cap", "limit", "spent", "reserved", "need", "default", "window")} for c in crossed]
    for r in requests(cfg, ("open", "held")):
        if (r.get("row"), r.get("provider"), float(r.get("est", -1)), r.get("envelope", ""), r.get("source", "run")) == \
                (row, provider, float(est), envelope, source) and sorted(c["cap"] for c in r.get("crossed") or []) == caps_set:
            if not r.get("paged") and r.get("crossed") != fresh:   # not shown yet: show the current shortfall
                r["crossed"] = fresh
                save_request(cfg, r)
            return r, False
    rec = {"id": _next_id(cfg), "status": "open", "ts": time.strftime("%F %T"), "epoch": int(time.time()),
           "rig": cfg.name, "row": row, "session": session, "provider": provider, "est": round(float(est), 6),
           "envelope": envelope, "phase": phase, "what": what, "source": source,
           "crossed": [{k: c.get(k) for k in ("cap", "limit", "spent", "reserved", "need", "default", "window")}
                       for c in crossed]}
    save_request(cfg, rec)
    return rec, True


def wait_request(cfg, row: str, summary: str) -> Optional[Dict[str, Any]]:
    """The request a BLOCKED claim names (`SPEND_WAIT <id>` as its summary), when it is this row's."""
    m = WAIT_RE.search(str(summary or ""))
    if not m or not enabled(cfg):
        return None
    rec = load_request(cfg, m.group(1))
    if not rec or rec.get("row") != row:
        return None
    return rec


def page_text(cfg, rec: Dict[str, Any]) -> Tuple[str, str]:
    """-> (title, body) in decision-brief form: what needs money, the cap, spent so far, the two options, one
    recommendation. A is recommended when the spend is planned (the command named its --envelope)."""
    who = f"row {rec['row']}" if rec.get("row") else "the rig"
    env = rec.get("envelope") or ""
    if rec.get("source") == "meter":
        return _meter_page(cfg, rec, who)
    title = f"money needed — spend {rec['id']} ({who}, {usd(rec['est'])})"
    L = [f"{who} wants {usd(rec['est'])} for {rec['provider']}"
         + (f" (envelope {env})" if env else " (no envelope named)") + f" — command {rec.get('what') or '?'}. "
         "It ran nothing and waits; the rest of the rig keeps running."]
    for c in rec.get("crossed") or []:
        L.append(f"Cap {c['cap']}{' (per_row_default)' if c.get('default') else ''} {usd(c['limit'])}: spent "
                 f"{usd(c.get('spent', 0))}, running {usd(c.get('reserved', 0))} -> short {usd(c['need'])}.")
    raise_s = " and ".join(f"{c['cap']} by {usd(c['need'])}" for c in rec.get("crossed") or [])
    a_rec = bool(env)
    L.append(f"A) raise {raise_s}{' (recommended: the plan names this spend)' if a_rec else ''}")
    L.append(f"B) hold — the row stays in spend_wait{'' if a_rec else ' (recommended: no envelope names this spend)'}")
    return title, "\n".join(L)


def _meter_page(cfg, rec: Dict[str, Any], who: str) -> Tuple[str, str]:
    """A metered crossing: the money is already spent; the hold stops more. B is the recommendation."""
    title = f"money needed — spend {rec['id']} (OpenCode meter, {who}, {usd(rec['est'])})"
    head = float(rec.get("headroom", 0) or 0)
    scope = who if rec.get("row") else (f"phase {rec['hold_phase']}" if rec.get("hold_phase") else "")
    L = [f"Spend is past the cap{(' of ' + scope) if scope else ''} and OpenCode is spending (metered from its own "
         f"db). Spend hold: nothing new dispatches to the metered executors{(' for ' + scope) if scope else ''}; "
         "sessions already running finish; the rest of the rig keeps running."]
    for c in rec.get("crossed") or []:
        L.append(f"Cap {c['cap']}{' (per_row_default)' if c.get('default') else ''} {usd(c['limit'])}: spent "
                 f"{usd(c.get('spent', 0))}, running {usd(c.get('reserved', 0))} -> short {usd(c['need'])}"
                 + (f" (incl. headroom {usd(head)})" if head else "") + ".")
    raise_s = " and ".join(f"{c['cap']} by {usd(c['need'])}" for c in rec.get("crossed") or [])
    L.append(f"A) raise {raise_s} — OpenCode runs again")
    L.append("B) hold — OpenCode stays held until the cap's window resets (recommended: the cap is the plan; "
             "other tiers keep running)")
    return title, "\n".join(L)


# -- meters, the spend hold, economy, reconcile (vC2) ---------------------------------------------
def _ocfg(cfg) -> Dict[str, Any]:
    return scfg(cfg).get("opencode") or {}


def _headroom(cfg) -> float:
    try:
        return max(0.0, round(float(_ocfg(cfg).get("headroom_usd", 1.0) or 0), 2))
    except (TypeError, ValueError):
        return 1.0


def meter_scopes(cfg, caps: Dict[str, Any], rows: List[str], res: List[Dict[str, Any]],
                 now: Optional[float] = None) -> Dict[str, Dict[str, Any]]:
    """-> {scope: {row, phase, crossed}} for metered OpenCode spend. Scope '' = the rig-wide caps (daily, monthly,
    per_provider.opencode), crossed only when OpenCode itself spent in the cap's window; 'row:<id>' = that row's
    per_row cap; 'phase:<P>' = that phase's per_phase cap. Crossed = spent + running > limit."""
    now = now or time.time()
    out: Dict[str, Dict[str, Any]] = {}
    oc = [r for r in lines(cfg) if r.get("meter") == "opencode"]
    over = lambda st: [c for c in st if c["spent"] + c["reserved"] > c["limit"] + EPS]
    cr = [c for c in over(standing(cfg, caps, "", "opencode", "", res, now))
          if any(_window(r, c["window"], now) for r in oc)]   # another provider's overrun is not OpenCode's to hold
    if cr:
        out[""] = {"row": "", "phase": "", "crossed": cr}
    phases = set()
    for row in sorted({r for r in rows if r}):
        ph = phase_of(cfg, row)
        st = standing(cfg, caps, row, "opencode", ph, res, now)
        cr = over([c for c in st if c["cap"].startswith("per_row.")])
        if cr:
            out[f"row:{row}"] = {"row": row, "phase": ph, "crossed": cr}
        if ph and ph not in phases:
            phases.add(ph)
            cr = over([c for c in st if c["cap"] == f"per_phase.{ph}"])
            if cr:
                out[f"phase:{ph}"] = {"row": "", "phase": ph, "crossed": cr}
    return out


def _scope(r: Dict[str, Any]) -> str:
    if r.get("row"):
        return f"row:{r['row']}"
    return f"phase:{r['hold_phase']}" if r.get("hold_phase") else ""


def meter_request(cfg, scope: str, sc: Dict[str, Any]) -> Tuple[Dict[str, Any], bool]:
    """ONE request per scope (rig, row or phase) from the meter, whatever the amount (call under the lock).
    Each cap's shortfall (whole cents, up) plus the headroom, so an A does not re-page on the next cent."""
    head = _headroom(cfg)
    fresh = []
    for c in sc["crossed"]:
        need = round(_cents_up(c["spent"] + c["reserved"] - c["limit"]) + head, 2)
        fresh.append(dict({k: c.get(k) for k in ("cap", "limit", "spent", "reserved", "default", "window")}, need=need))
    est = max([c["need"] for c in fresh] or [0.0])
    for r in requests(cfg, ("open", "held")):
        if r.get("source") == "meter" and _scope(r) == scope and r.get("provider") == "opencode":
            if not r.get("paged") and (r.get("crossed") != fresh or r.get("est") != est):
                r.update(crossed=fresh, est=est)
                save_request(cfg, r)
            return r, False
    rec = {"id": _next_id(cfg), "status": "open", "ts": time.strftime("%F %T"), "epoch": int(time.time()),
           "rig": cfg.name, "row": sc["row"], "session": "", "provider": "opencode", "est": est, "envelope": "",
           "phase": sc["phase"], "what": "the OpenCode meter", "source": "meter", "headroom": head, "crossed": fresh}
    if scope.startswith("phase:"):
        rec["hold_phase"] = sc["phase"]
    save_request(cfg, rec)
    return rec, True


def hold_path(cfg) -> Path:
    return cfg.state / "spend" / "hold.json"


def update_hold(rig, booked_rows: Optional[List[str]] = None, now: Optional[float] = None) -> Dict[str, Any]:
    """Metered crossings -> requests; the OpenCode spend hold follows the open/held meter requests. A request whose
    caps no longer cross (a new day or month, a higher approval) lapses and lifts its part of the hold. Every row
    this rig metered this month is checked (another rig's poll may have booked it)."""
    cfg = rig.cfg
    now = now or time.time()
    with _Lock(cfg):
        caps, _ = approved(cfg)
        caps = caps if caps is not None else dict(CLOSED)
        res = reservations(cfg) + grants(cfg)
        pending = [r for r in requests(cfg, ("open", "held")) if r.get("source") == "meter"]
        rows = set(booked_rows or []) | {r.get("row", "") for r in pending}
        rows |= {str(r.get("row")) for r in lines(cfg) if r.get("meter") == "opencode" and r.get("row")
                 and _window(r, "month", now)}
        scopes = meter_scopes(cfg, caps, sorted(rows), res, now)
        new = []
        for r in pending:
            names = {c["cap"] for c in r.get("crossed") or []}
            if not names & {c["cap"] for c in (scopes.get(_scope(r)) or {}).get("crossed", [])}:
                r.update(status="lapsed", lapsed=time.strftime("%F %T"))
                save_request(cfg, r)
                rig.st.event(r.get("row") or "spend", "spend_lapsed", req=r["id"])
                rig.st.engine_log(f"spend {r['id']}: its caps no longer cross ({', '.join(sorted(names))}) — lapsed")
        for scope, sc in scopes.items():
            rec, isnew = meter_request(cfg, scope, sc)
            if isnew:
                new.append(rec)
        held = [r for r in requests(cfg, ("open", "held")) if r.get("source") == "meter"]
    for rec in new:
        rig.st.event(rec.get("row") or "spend", "spend_meter_crossing", req=rec["id"], est=rec["est"],
                     caps=",".join(c["cap"] for c in rec["crossed"]))
        rig.log("", f"spend {rec['id']}: the OpenCode meter crossed {', '.join(c['cap'] for c in rec['crossed'])}"
                    f"{' (row ' + rec['row'] + ')' if rec.get('row') else ''} — spend hold")
    return set_hold(rig, held)


def set_hold(rig, held: List[Dict[str, Any]]) -> Dict[str, Any]:
    cfg = rig.cfg
    old = _jread(hold_path(cfg)).get("opencode") or {}
    rig_reqs = sorted(r["id"] for r in held if not _scope(r))
    rows = sorted({r["row"] for r in held if r.get("row")})
    phases = sorted({r["hold_phase"] for r in held if r.get("hold_phase") and not r.get("row")})
    st = "open" if held else "closed"
    cur = {"state": st, "kind": "spend", "rig": rig_reqs, "rows": rows, "phases": phases,
           "reqs": sorted(r["id"] for r in held),
           "reason": ("; ".join(f"spend {r['id']} ({r['status']}): " + ", ".join(c["cap"] for c in r.get("crossed") or [])
                                + (f" of row {r['row']}" if r.get("row") else "") for r in held)),
           "since": old.get("since") if old.get("state") == "open" and st == "open" else (int(time.time()) if held else 0)}
    if cur != old:
        sdir(cfg)
        _atomic(hold_path(cfg), json.dumps({"opencode": cur}, indent=1, sort_keys=True))
        if old.get("state", "closed") != st:
            rig.st.event("spend", "spend_hold_open" if held else "spend_hold_close", reqs=",".join(cur["reqs"]) or None)
            rig.st.engine_log(f"spend: OpenCode spend hold {'OPEN — ' + cur['reason'] if held else 'CLOSED'}")
    return cur


def hold_blocks(cfg, executor: str, row: str = "") -> Tuple[bool, str]:
    """(True, why) while the OpenCode spend hold keeps executor (for row) from new dispatches."""
    f = hold_path(cfg)
    if not f.is_file() or not enabled(cfg):
        return False, ""
    h = _jread(f).get("opencode") or {}
    if h.get("state") != "open":
        return False, ""
    try:
        import meter
        if not meter.metered_executor(cfg, executor):
            return False, ""
    except Exception:
        return False, ""
    if h.get("rig"):
        return True, f"executor {executor} spend hold (kind spend) — {h.get('reason', '')}"
    if row and row in (h.get("rows") or []):
        return True, f"row {row} on {executor}: spend hold (kind spend) — {h.get('reason', '')}"
    if row and h.get("phases") and phase_of(cfg, row) in h["phases"]:
        return True, f"row {row} (phase {phase_of(cfg, row)}) on {executor}: spend hold (kind spend) — {h.get('reason', '')}"
    return False, ""


def hold_lines(cfg) -> List[str]:
    h = _jread(hold_path(cfg)).get("opencode") or {}
    if h.get("state") != "open":
        return []
    return [f"OpenCode spend hold OPEN since {time.strftime('%F %T', time.localtime(int(h.get('since', 0) or 0)))}"
            f" — {h.get('reason', '')}"]


def cap_uses(cfg, caps: Dict[str, Any], now: Optional[float] = None,
             led: Optional[List[Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
    """Every approved cap with what this rig spent in its window: the scalars, each per_provider / per_phase /
    per_row entry, and per_row_default for each row that spent without a per_row entry of its own."""
    now = now or time.time()
    led = lines(cfg) if led is None else led
    out = []

    def add(name, window, limit, key=None, val=None, default=False):
        spent = sum(float(r.get("usd", 0) or 0) for r in led
                    if _window(r, window, now) and (key is None or str(r.get(key, "")) == val))
        out.append({"cap": name, "window": window, "limit": float(limit), "spent": round(spent, 6), "default": default})
    for k, w in (("daily_usd", "day"), ("monthly_usd", "month")):
        if k in caps:
            add(k, w, caps[k])
    for n, v in sorted((caps.get("per_provider") or {}).items()):
        add(f"per_provider.{n}", "month", v, "provider", n)
    for n, v in sorted((caps.get("per_phase") or {}).items()):
        add(f"per_phase.{n}", "all", v, "phase", n)
    own = caps.get("per_row") or {}
    for n, v in sorted(own.items()):
        add(f"per_row.{n}", "all", v, "row", n)
    if "per_row_default" in caps:
        for n in sorted({str(r.get("row")) for r in led if r.get("row")} - set(own)):
            add(f"per_row.{n}", "all", caps["per_row_default"], "row", n, default=True)
    return out


def economy(rig, now: Optional[float] = None) -> List[Dict[str, Any]]:
    """Caps at spend.economy_pct (80 %) or more of an approved limit: state/spend/economy (the Steward prefers
    cheaper tiers while it exists) and an event once per cap and window. -> the marked caps."""
    cfg = rig.cfg
    f = sdir(cfg) / "economy"
    caps, _ = approved(cfg)
    try:
        pct = float(scfg(cfg).get("economy_pct", 80) or 0)
    except (TypeError, ValueError):
        pct = 80.0
    marks = []
    if caps and pct > 0:
        now = now or time.time()
        for c in cap_uses(cfg, caps, now):
            # day/month caps only: a lifetime per_row/per_phase cap an A raised to its spend would mark it for ever
            if c["window"] != "all" and c["limit"] > 0 and c["spent"] >= c["limit"] * pct / 100.0 - EPS:
                marks.append(dict(c, pct=round(100.0 * c["spent"] / c["limit"], 1)))
    if not marks:
        if f.exists():
            f.unlink(missing_ok=True)
            rig.st.event("spend", "spend_economy_off")
        return []
    text = (f"# economy: at least {pct:g} % of an approved spend cap is used — prefer the cheaper tier "
            f"(written by the engine; it goes when no cap is at the mark)\n"
            + "".join(f"{c['cap']} {c['spent']:.2f} of {c['limit']:.2f} USD ({c['pct']:g} %)\n" for c in marks))
    if not f.exists() or f.read_text() != text:
        _atomic(f, text)
    lt = time.localtime(now)
    for c in marks:
        wkey = {"day": time.strftime("%F", lt), "month": time.strftime("%Y-%m", lt)}.get(c["window"], "all")
        if rig.st.once(f"spend-economy-{c['cap']}-{wkey}-{c['limit']:.2f}"):
            rig.st.event("spend", "spend_economy", cap=c["cap"], pct=c["pct"], limit=c["limit"])
            rig.st.engine_log(f"spend: economy — {c['cap']} at {c['pct']:g} % of {usd(c['limit'])}")
    return marks


def _secs(v: Any, default: float) -> float:
    m = re.match(r"^\s*([0-9]+(?:\.[0-9]+)?)\s*([smhd]?)\s*$", str(v if v is not None else ""))
    if not m:
        return default
    return float(m.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]


def reconcile_entries(cfg) -> List[Dict[str, Any]]:
    out = []
    for i, e in enumerate(scfg(cfg).get("reconcile") or []):
        if isinstance(e, dict) and TOKEN.match(str(e.get("provider", ""))) and str(e.get("cmd", "")).strip():
            out.append({"i": i, "provider": str(e["provider"]), "cmd": str(e["cmd"]),
                        "every": _secs(e.get("every"), 86400.0)})
    return out


def reconcile_tick(rig, now: Optional[float] = None) -> None:
    """Start each spend.reconcile command when it is due (an engine job: spendrec:<i>)."""
    cfg = rig.cfg
    ents = reconcile_entries(cfg)
    if not ents:
        return
    now = now or time.time()
    f = sdir(cfg) / "reconcile.json"
    last = _jread(f)
    t = int(_secs(scfg(cfg).get("reconcile_timeout"), 120))
    for e in ents:
        k = str(e["i"])
        if now - float(last.get(k, 0) or 0) < e["every"] or rig.job_running(f"spendrec:{k}"):
            continue
        last[k] = now
        _atomic(f, json.dumps(last, sort_keys=True))
        rig.start_job(f"spendrec:{k}", ["bash", "-c", f"timeout {t} bash -c {shlex.quote(e['cmd'])}"],
                      sdir(cfg) / f"reconcile_{k}.out")


def on_reconcile_done(rig, ident: str, rc: int) -> Optional[Dict[str, Any]]:
    """Compare the provider's billing month-to-date with the host-wide ledger; a gap beyond the tolerance opens
    a Steward incident `spend_reconcile` (no Steward: a notification). Nothing is booked. -> the record."""
    cfg = rig.cfg
    e = next((x for x in reconcile_entries(cfg) if str(x["i"]) == str(ident)), None)
    if not e:
        return None
    out = sdir(cfg) / f"reconcile_{ident}.out"
    try:
        text = out.read_text(errors="replace")
    except Exception:
        text = ""
    billing = None
    for line in text.splitlines():
        m = SPEND_LINE.match(line.strip())
        if m and m.group(1) == e["provider"]:
            billing = float(m.group(2))
    now = time.time()
    rec: Dict[str, Any] = {"ts": time.strftime("%F %T"), "epoch": int(now), "provider": e["provider"], "rc": rc}
    if rc != 0 or billing is None:
        rec.update(ok=False, why=f"exit {rc}" if rc != 0 else f"no `SPEND {e['provider']} <usd>` line")
        _append(sdir(cfg) / "reconcile.jsonl", rec)
        rig.st.event("spend", "spend_reconcile_failed", provider=e["provider"], why=rec["why"])
        if rig.st.once(f"spend-reconcile-failed-{e['provider']}-{time.strftime('%F')}"):
            rig.st.engine_log(f"spend: reconcile {e['provider']} failed ({rec['why']}; state/spend/reconcile_{ident}.out)")
        return rec
    ledger = sum(float(r.get("usd", 0) or 0) for r in lines(cfg, all_rigs=True)
                 if r.get("provider") == e["provider"] and _window(r, "month", now))
    try:
        tol = float(scfg(cfg).get("reconcile_tolerance_usd", 1.0))
    except (TypeError, ValueError):
        tol = 1.0
    gap = round(billing - ledger, 6)
    rec.update(billing=billing, ledger=round(ledger, 6), gap=gap, tolerance=tol, ok=abs(gap) <= tol + EPS)
    _append(sdir(cfg) / "reconcile.jsonl", rec)
    rig.st.event("spend", "spend_reconcile", provider=e["provider"], billing=billing, ledger=rec["ledger"], gap=gap,
                 ok=rec["ok"])
    if rec["ok"]:
        return rec
    why = (f"{e['provider']} billing says {usd(billing)} month to date, the host ledger {usd(ledger)} — a gap of "
           f"{usd(gap)} beyond the tolerance {usd(tol)}"
           + (" (spend nobody booked: a paid command outside `orch spend run`, or a meter that missed it)" if gap > 0
              else " (the ledger books more than billing: estimates above the real cost)"))
    rig.st.engine_log(f"spend: reconcile mismatch — {why}")
    sig = f"{time.strftime('%Y-%m', time.localtime(now))}|x{int(math.log2(max(abs(gap) / max(tol, 0.01), 1.0)))}"   # again when it doubles
    subj = f"rig:spend_reconcile:{e['provider']}"
    try:
        import steward
        if steward.enabled(cfg):
            if not steward.open_for(cfg, subj, "spend_reconcile") and not steward.was_handled(cfg, "spend_reconcile", subj, sig):
                steward.open_incident(rig, "spend_reconcile", subj,
                                      f"{why}. Find the untracked spend (orch spend ledger, the provider's console) "
                                      f"and INBOX the Owner; never raise a cap", klass="judgment", sig=sig)
        elif rig.st.once(f"spend-reconcile-{e['provider']}-{sig}"):
            rig.notify("normal", f"{cfg.name}: spend reconcile mismatch ({e['provider']})", why[:400])
    except Exception:
        pass
    return rec


def meter_tick(rig, now: Optional[float] = None) -> None:
    """The OpenCode meter: sessions published every tick, the db polled every spend.opencode.every seconds."""
    import meter
    cfg = rig.cfg
    now = now or time.time()
    key = f"meter:{_inst(cfg)}"
    try:
        meter.track(rig)
        if now - _LAST.get(key, 0) < _secs(_ocfg(cfg).get("every"), 300.0):
            return
        _LAST[key] = now
        res = meter.poll(rig, now)
    except Exception as e:
        msg = f"{type(e).__name__}: {e}"
        if rig.st.once(f"meter-error-{time.strftime('%F')}-{msg[:40]}"):
            rig.st.event("spend", "meter_error", meter="opencode", detail=msg[:300])
            rig.st.engine_log(f"spend: the OpenCode meter could not read its db — {msg}")
        return
    if res.get("why") and not res.get("booked") and "no OpenCode db" in res["why"] \
            and rig.st.once(f"meter-nodb-{time.strftime('%F')}"):
        rig.st.engine_log(f"spend: {res['why']} — the meter books nothing")
    mine = [r for r in res.get("booked") or [] if r.get("instance") == _inst(cfg)]
    if enabled(cfg):
        update_hold(rig, [r.get("row", "") for r in mine], now)


# -- engine side ----------------------------------------------------------------------------------
def send_pages(rig) -> None:
    """One critical page (page_class=spend) per request id, the first tick it is seen."""
    cfg = rig.cfg
    for r in requests(cfg, ("open",)):
        if r.get("paged"):
            continue
        with _Lock(cfg):
            rec = load_request(cfg, r["id"])
            if not rec or rec.get("paged") or rec.get("status") != "open":
                continue
            rec.update(paged=time.strftime("%F %T"), paged_sha=content_sha(rec))
            save_request(cfg, rec)
        title, body = page_text(cfg, rec)
        rig.st.event(rec.get("row") or "spend", "spend_page", req=rec["id"], est=rec.get("est"),
                     caps=",".join(str(c.get("cap")) for c in rec.get("crossed") or []))
        rig.log("", f"spend {rec['id']}: {title} — paged the Owner (page_class spend)")
        rig.notify("critical", f"{cfg.name} — {title}", body, row=f"spend:{rec['id']}", page_class="spend")


def on_wait(rig, row, base: Dict[str, Any], rec: Dict[str, Any]) -> None:
    """A BLOCKED claim `SPEND_WAIT <id>`: phase spend_wait — no unblocker, no Steward, no halt page."""
    res = dict(base, phase="spend_wait", spend_req=rec["id"],
               why=f"waiting for money: spend {rec['id']} ({rec.get('status')}) — the Owner answers `spend {rec['id']} A|B`")
    rig.st.write_result(row.id, res)
    rig.st.event(row.id, "spend_wait", req=rec["id"], status=rec.get("status"))
    rig.log(row.lane, f"{row.id} waits for money: spend {rec['id']} ({usd(rec.get('est', 0))} {rec.get('provider')}) — spend_wait")


def wake(rig, notes: Optional[Dict[str, str]] = None) -> None:
    """spend_wait rows whose request is approved are re-dispatched (the reservation runs again, now inside
    the raised cap); held or open ones get a scheduler note."""
    from rows import load_rows
    rows, _ = load_rows(rig.cfg, include_archived=False)
    for r in rows:
        if rig.st.phase(r.id) != "spend_wait":
            continue
        res = rig.st.result(r.id)
        rid = str(res.get("spend_req", ""))
        rec = load_request(rig.cfg, rid)
        st = (rec or {}).get("status", "missing")
        if st == "approved":
            rig.st.set_aside(r.id, f"spendwait{int(time.time())}")
            rig.st.write_result(r.id, {"phase": "retry", "status_mtime": 0, "spend_req": rid,
                                       "why": f"spend {rid} approved by the Owner — re-dispatched"})
            rig.st.event(r.id, "spend_resume", req=rid)
            rig.log(r.lane, f"{r.id}: spend {rid} approved — re-dispatched")
            if notes is not None:
                notes.pop(r.id, None)
        elif notes is not None:
            notes[r.id] = (f"spend_wait: spend {rid} held by the Owner (B) — `spend {rid} A` releases it, "
                           f"orch reset {r.id} re-runs it" if st == "held" else
                           f"spend_wait: spend {rid} {st} — the Owner answers `spend {rid} A|B`")


def release_all(rig) -> None:
    """The governor switched off: rows still in spend_wait are re-dispatched (off = no caps)."""
    if not (rig.cfg.state / "spend" / "requests").is_dir():    # a v2 rig never filed one: nothing to do
        return
    from rows import load_rows
    for r in load_rows(rig.cfg, include_archived=False)[0]:
        if rig.st.phase(r.id) == "spend_wait":
            req = rig.st.result(r.id).get("spend_req")
            rig.st.set_aside(r.id, f"spendoff{int(time.time())}")
            rig.st.write_result(r.id, {"phase": "retry", "status_mtime": 0,
                                       "why": "spend governor switched off (spend.enabled false) — re-dispatched"})
            rig.st.event(r.id, "spend_resume", req=req, detail="governor off")
            rig.log(r.lane, f"{r.id}: the spend governor is off — re-dispatched")


def caps_watch(rig) -> None:
    """An edit of SPEND_CAPS.yaml (or of the approved copy) that was not approved: ignored — the approved
    copy stays in force — and recorded as a Steward incident (once per edit)."""
    cfg = rig.cfg
    why = tamper(cfg)
    if not why:
        if not _jread(approved_rec(cfg)).get("approved_at") and caps_file(cfg).is_file() \
                and rig.st.once("spend-caps-unapproved"):
            rig.log("", "spend: SPEND_CAPS.yaml is not approved yet — every cap is closed (orch spend caps approve)")
        return
    sig = f"{_sha_file(caps_file(cfg))[:16]}|{_sha_file(approved_yaml(cfg))[:16]}"
    if rig.st.once(f"spend-caps-tamper-{sig}"):
        rig.st.event("spend", "spend_caps_tamper", detail=why[:300])
        rig.log("", f"spend: {why}")
        try:
            import steward
            if not steward.enabled(cfg):     # no Steward lane: the Owner's digest carries it
                rig.notify("normal", f"{cfg.name}: spend caps edited without approval", why[:400])
        except Exception:
            pass
    try:
        import steward
        if steward.enabled(cfg) and not steward.open_for(cfg, "rig:spend_caps", "spend_caps") \
                and not steward.was_handled(cfg, "spend_caps", "rig:spend_caps", sig):
            steward.open_incident(rig, "spend_caps", "rig:spend_caps",
                                  f"{why}. Only the Owner approves caps (`orch spend caps approve`); the Steward "
                                  f"never does — INBOX the Owner with what changed", klass="judgment", sig=sig)
    except Exception:
        pass


def tick(rig, notes: Optional[Dict[str, str]] = None) -> None:
    """Engine tick phase (before the scheduler): answers, the caps watch, pages, wake-ups."""
    cfg = rig.cfg
    try:
        import meter
        metering = meter.on(cfg)
    except Exception:
        metering = False
    if metering:                 # the meters and reconcile measure, governor or not
        meter_tick(rig)
    reconcile_tick(rig)
    if not enabled(cfg):
        release_all(rig)
        if hold_path(cfg).is_file():
            set_hold(rig, [])
        (cfg.state / "spend" / "economy").unlink(missing_ok=True)
        return
    if any(cfg.control.glob("SPEND_*.json")):
        consume(rig)
    else:
        expire()
    caps_watch(rig)
    if not metering and any(r.get("source") == "meter" for r in requests(cfg, ("open", "held"))):
        update_hold(rig)         # the meter switched off: its requests still lapse or are answered
    elif metering or hold_path(cfg).is_file():
        set_hold(rig, [r for r in requests(cfg, ("open", "held")) if r.get("source") == "meter"])
    send_pages(rig)
    wake(rig, notes)
    key = f"economy:{_inst(cfg)}"
    sig = float(int(_sha_file(approved_rec(cfg))[:8] or "0", 16))     # a new approval or raise: at once
    if time.time() - _LAST.get(key, 0) >= 60 or _LAST.get(key + ":sig") != sig:
        _LAST[key], _LAST[key + ":sig"] = time.time(), sig
        economy(rig)


# -- answers --------------------------------------------------------------------------------------
def answer(rig, rid: str, letter: str, by: str, via: str, note: str = "", seen_sha: str = "",
           extra: Optional[Dict[str, Any]] = None, require_seen: bool = False) -> Tuple[bool, str]:
    """The Owner's answer to request rid. A raises the approved caps by exactly each shortfall; B holds.
    require_seen (Telegram): the answer is bound to the request content that was paged."""
    cfg = rig.cfg
    letter = str(letter).strip().upper()[:1]
    if letter not in ("A", "B"):
        return False, f"answer A or B, not {letter or '(nothing)'}"
    with _Lock(cfg):
        rec = load_request(cfg, rid)
        if not rec:
            return False, f"no spend request {rid}"
        if rec.get("status") == "approved":
            return False, f"spend {rid} was already approved ({rec.get('answered')})"
        if rec.get("status") == "lapsed":
            return False, f"spend {rid} lapsed ({rec.get('lapsed')}): its caps no longer cross — nothing to raise"
        if require_seen and not seen_sha:
            return False, f"spend {rid} has not been paged yet — answer it after the page (or orch spend answer)"
        if seen_sha and seen_sha != content_sha(rec):
            return False, f"spend {rid} changed since it was paged — not applied (orch spend status)"
        raised = []
        if letter == "A":
            caps, _ = approved(cfg)
            caps = json.loads(json.dumps(caps if caps is not None else CLOSED))
            prev = _jread(approved_rec(cfg))
            for c in rec.get("crossed") or []:
                need = float(c.get("need", 0) or 0)
                name = str(c.get("cap", ""))
                m, _, key = name.partition(".")
                if name in ("daily_usd", "monthly_usd"):
                    old = float(caps.get(name, c.get("limit", 0)) or 0)
                    caps[name] = round(old + need, 6)
                elif m in MAPS and key:
                    sub = caps.setdefault(m, {})
                    old = float(sub.get(key, c.get("limit", 0)) or 0)   # per_row: the default it was under
                    sub[key] = round(old + need, 6)
                else:
                    continue
                raised.append({"cap": name, "from": old, "to": round(old + need, 6), "by": need})
            raises = list(prev.get("raises") or [])[-50:]
            raises.append({"ts": time.strftime("%F %T"), "req": rid, "raised": raised, "by": by})
            _write_approved(cfg, caps, str(prev.get("from_sha", "")), str(prev.get("by") or by), {"raises": raises})
            rec["status"] = "approved"
        else:
            rec["status"] = "held"
        rec.update(answered=time.strftime("%F %T"), answer=letter, answered_by=by, answered_via=via)
        save_request(cfg, rec)
        out = {"ts": time.strftime("%F %T"), "epoch": int(time.time()), "req": rid, "letter": letter, "by": by,
               "via": via, "note": " ".join(str(note).split())[:500], "row": rec.get("row"), "raised": raised,
               "ruling": f"SPEND-{rid}", "kind": "owner ruling"}
        out.update(extra or {})
        _append(sdir(cfg) / "answers.jsonl", out)
    rig.st.event(rec.get("row") or "spend", "spend_answer", req=rid, letter=letter, via=via,
                 raised=", ".join(f"{x['cap']}+{x['by']:.2f}" for x in raised) or None)
    if letter == "A":
        msg = f"spend {rid} APPROVED — raised " + ", ".join(f"{x['cap']} {usd(x['from'])} -> {usd(x['to'])}" for x in raised)
    else:
        msg = f"spend {rid} HELD — {('row ' + rec['row']) if rec.get('row') else 'the request'} stays in spend_wait"
    rig.st.engine_log(f"{msg} (by {by} via {via})")
    return True, msg


def _answer_path(cfg, req: str) -> Path:
    return sdir(cfg) / "answers" / f"{req}.json"


def expire() -> None:
    import revert
    now = time.time()
    for req, (fd, at) in list(_HELD.items()):
        if now - at > revert.HOLD_SECS:
            try:
                os.close(fd)
            except OSError:
                pass
            _HELD.pop(req, None)


def proof(cfg, door: Dict[str, Any], rid: str) -> Tuple[Optional[Dict[str, Any]], str]:
    """-> (the Owner's sealed answer, '') when the doorbell points at a memfd the rig's bot sealed for
    request rid from the allowed user; else (None, why not). Without sealing the doorbell is the answer."""
    import revert
    allowed = str((cfg.data.get("watchdog") or {}).get("status_bot_allowed_user") or "")
    if revert.sealing():
        bad = revert._role_ok(cfg, int(door.get("pid") or 0), "telegram_bot.py", ("daemon", "poll")) \
            if str(door.get("pid", "")).isdigit() else "the doorbell names no bot process"
        if bad:
            return None, bad
        rec, bad = revert.read_sealed(door.get("pid"), door.get("fd"), REQ_MEMFD)
        if rec is None:
            return None, bad
        if str(rec.get("instance")) != _inst(cfg):
            return None, "the sealed answer is for another instance"
    else:
        rec = door
    if str(rec.get("id")) != rid:
        return None, f"the sealed answer is for {rec.get('id')}, the doorbell for {rid}"
    if not allowed or str(rec.get("from_id", "")) != allowed:
        return None, "the answer does not come from watchdog.status_bot_allowed_user"
    return rec, ""


def consume(rig) -> None:
    """control/SPEND_<id>.json doorbells from the bot: proved, applied, answered sealed. A bad one is
    answered refused and removed; it never stops the others."""
    import revert
    import shutil
    cfg = rig.cfg
    expire()
    for f in sorted(cfg.control.glob("SPEND_*.json")):
        m = DOOR_RE.match(f.name)
        door: Dict[str, Any] = {}
        try:
            if f.is_file() and not f.is_symlink():
                raw = json.loads(f.read_text())
                door = raw if isinstance(raw, dict) else {}
        except Exception:
            door = {}
        rid = m.group(1) if m else ""
        req = f"SPEND_{rid or 'x'}.{int(time.time() * 1000)}"
        try:
            if not m or not door:
                ok, detail = False, "malformed answer (not a plain file of JSON)"
            elif not _flag(scfg(cfg).get("telegram_answers"), True):
                ok, detail = False, "Telegram spend answers are off (spend.telegram_answers)"
            else:
                rec, why = proof(cfg, door, rid)
                if rec is None:
                    ok, detail = False, why
                    rig.st.event("spend", "spend_answer_refused", req=rid, detail=why[:300])
                    rig.st.engine_log(f"spend answer REFUSED (control/{f.name}) — {why}")
                else:
                    if re.match(r"^SPEND_sp[0-9]+\.[0-9]+$", str(rec.get("req", ""))):
                        req = str(rec["req"])
                    owner = (cfg.data.get("watchdog") or {}).get("owner_name") or "owner"
                    ok, detail = answer(rig, rid, str(rec.get("letter", "")), by=f"{owner} (Telegram)", via="telegram",
                                        note=str(rec.get("note", "")), seen_sha=str(rec.get("seen_sha", "")),
                                        extra={"message_id": str(rec.get("message_id", ""))[:20]}, require_seen=True)
        except Exception as e:
            ok, detail = False, f"could not be handled ({type(e).__name__}: {e})"
        try:
            ans = {"id": rid, "req": req, "outcome": "done" if ok else "refused", "why": detail,
                   "handled": time.strftime("%F %T")}
            out = dict(ans)
            if revert.sealing():
                fd = revert.sealed(ANS_MEMFD, ans)
                _HELD[req] = (fd, time.time())
                out.update(pid=os.getpid(), fd=fd)
            _atomic(_answer_path(cfg, req), json.dumps(out, indent=1, sort_keys=True, default=str))
        except Exception as e:
            rig.st.engine_log(f"spend: could not answer control/{f.name} ({type(e).__name__}: {e})")
        finally:
            try:
                if f.is_dir() and not f.is_symlink():
                    shutil.rmtree(f, ignore_errors=True)
                else:
                    f.unlink(missing_ok=True)
            except Exception:
                pass


def bot_request(cfg, rid: str, letter: str, msg: Dict[str, Any], note: str = ""):
    """The bot's side: the Owner's answer sealed in a memfd this process holds, and the doorbell
    control/SPEND_<id>.json. -> (answer path, fd, '') or (None, None, why not)."""
    import revert
    if not _flag(scfg(cfg).get("telegram_answers"), True):
        return None, None, "Telegram spend answers are off on this rig (spend.telegram_answers)"
    rec0 = load_request(cfg, rid)
    if not rec0:
        return None, None, f"no spend request {rid}"
    if rec0.get("status") in ("approved", "lapsed"):
        return None, None, f"spend {rid} was already {rec0.get('status')}"
    f = cfg.control / f"SPEND_{rid}.json"
    if f.exists():
        return None, None, f"an answer to spend {rid} is already pending"
    req = f"SPEND_{rid}.{int(time.time() * 1000)}"
    rec = {"id": rid, "letter": letter, "note": note[:500], "req": req,
           "from_id": str((msg.get("from") or {}).get("id", "")), "message_id": msg.get("message_id"),
           "epoch": int(time.time()), "instance": _inst(cfg), "seen_sha": rec0.get("paged_sha", "")}
    fd = None
    door: Dict[str, Any] = rec
    if revert.sealing():
        fd = revert.sealed(REQ_MEMFD, rec)
        door = {"id": rid, "pid": os.getpid(), "fd": fd}
    tmp = cfg.control / f".spend-{rid}-{os.getpid()}.tmp"     # never matches the engine's SPEND_* glob
    tmp.write_text(json.dumps(door, indent=1, sort_keys=True))
    os.replace(tmp, f)
    return _answer_path(cfg, req), fd, ""


# -- digest ---------------------------------------------------------------------------------------
def _sum(rs) -> float:
    return sum(float(r.get("usd", 0) or 0) for r in rs)


def digest_lines(rig, since: float) -> List[str]:
    """Spend in the window against each cap, the economy mark, open requests, the OpenCode share and hold,
    billing reconcile."""
    cfg = rig.cfg
    try:
        import meter
        metering = meter.on(cfg)
    except Exception:
        metering = False
    led_all = lines(cfg)
    if not enabled(cfg) and not led_all and not metering and not reconcile_entries(cfg):
        return []
    led = [r for r in led_all if float(r.get("epoch", 0) or 0) >= since]
    caps, note = approved(cfg)
    L = [f"**Spend:** {usd(_sum(led))} in this window ({len(led)} cost line(s)); caps: {describe(caps)}"
         + (f" — {note}" if note else "") + ("" if enabled(cfg) else " — governor off (no cap enforced)")]
    if caps:
        try:
            pct = float(scfg(cfg).get("economy_pct", 80) or 0)
        except (TypeError, ValueError):
            pct = 80.0
        active = {str(r.get("row")) for r in led if r.get("row")} | set((caps.get("per_row") or {}))
        for c in cap_uses(cfg, caps, led=led_all):
            if c["cap"].startswith("per_row.") and c["cap"][8:] not in active:
                continue                     # a row that spent nothing in this window and has no cap of its own
            share = (100.0 * c["spent"] / c["limit"]) if c["limit"] > 0 else 0.0
            mark = " — **economy**" if c["window"] != "all" and pct > 0 and c["limit"] > 0 and share >= pct - EPS else ""
            win = {"day": "today", "month": "this month", "all": "all time"}[c["window"]]
            L.append(f"- {c['cap']}: {usd(c['spent'])} of {usd(c['limit'])} {win} ({share:.0f} %){mark}")
    if (cfg.state / "spend" / "economy").is_file():
        L.append(f"- economy: at least {scfg(cfg).get('economy_pct', 80)} % of a cap is used — the Steward prefers "
                 "cheaper tiers (state/spend/economy)")
    t = tamper(cfg)
    if t:
        L.append(f"- caps edited without approval: {t}")
    oc = [r for r in led if r.get("meter") == "opencode"]
    if oc or metering:
        tot = _sum(led)
        mine = _sum(r for r in oc if r.get("row"))
        L.append(f"**OpenCode share:** {usd(_sum(oc))} metered in this window ({len(oc)} message(s); "
                 f"{(100.0 * _sum(oc) / tot) if tot > 0 else 0:.0f} % of the window's spend) — to rows {usd(mine)}, "
                 f"shared {usd(_sum(oc) - mine)}" + ("" if metering else " (meter off now)"))
    for h in hold_lines(cfg):
        L.append(f"- {h}")
    op = requests(cfg, ("open", "held"))
    if op:
        L.append("**Spend requests waiting:** " + ", ".join(
            f"{r['id']} ({r.get('row') or 'rig'}, {usd(r['est'])} {r['provider']}, {r['status']}"
            + (", meter" if r.get("source") == "meter" else "") + ")" for r in op[:15]))
    rec = []
    try:
        for line in (cfg.state / "spend" / "reconcile.jsonl").read_text().splitlines():
            try:
                x = json.loads(line)
            except Exception:
                continue
            if isinstance(x, dict) and float(x.get("epoch", 0) or 0) >= since:
                rec.append(x)
    except FileNotFoundError:
        pass
    if rec:
        last: Dict[str, Dict[str, Any]] = {}
        for x in rec:
            last[str(x.get("provider"))] = x
        parts = []
        for pv, x in sorted(last.items()):
            if x.get("billing") is None:
                parts.append(f"{pv} FAILED ({x.get('why', '?')})")
            else:
                parts.append(f"{pv} billing {usd(x['billing'])} vs ledger {usd(x.get('ledger', 0))}"
                             + (" ok" if x.get("ok") else f" — **gap {usd(x.get('gap', 0))}**"))
        L.append("**Billing reconcile:** " + "; ".join(parts))
    return L


# -- CLI ------------------------------------------------------------------------------------------
def _caller_row(cfg) -> Tuple[str, str]:
    """(session, row) of the caller: a row session charges its row; an unblocker/reviewer its parent row;
    a Steward or planner session the rig; the operator's shell whatever --row says."""
    try:
        import capability
        sid, kind = capability.identity(cfg)
    except Exception:
        sid, kind = os.environ.get("ORCH_ROW", ""), os.environ.get("ORCH_KIND", "")
    if not sid:
        return "", ""
    if kind in ("", "row"):
        return sid, sid
    m = re.match(r"^(?:ub|rv|pf)-(.+?)-[0-9]+(?:r[0-9]+)?$", sid)
    return sid, (m.group(1) if m else "")


def run_cmd(cfg, argv: List[str]) -> int:
    import argparse
    if "--" not in argv:
        print("usage: orch spend run --provider P --est USD [--row id] [--envelope id] [--model M] -- <cmd…>")
        return 2
    i = argv.index("--")
    opts, cmd = argv[:i], argv[i + 1:]
    ap = argparse.ArgumentParser(prog="orch spend run", add_help=False)
    ap.add_argument("--provider", required=True)
    ap.add_argument("--est", required=True)
    ap.add_argument("--row", default="")
    ap.add_argument("--envelope", default="")
    ap.add_argument("--model", default="")
    try:
        a = ap.parse_args(opts)
    except SystemExit:
        print("usage: orch spend run --provider P --est USD [--row id] [--envelope id] [--model M] -- <cmd…>")
        return 2
    if not cmd:
        print("orch spend run: no command after --")
        return 2
    try:
        est = float(a.est)
        assert est >= 0 and not math.isinf(est) and not math.isnan(est)
    except Exception:
        print(f"orch spend run: --est {a.est!r} is not a non-negative number of USD")
        return 2
    for name, v in (("provider", a.provider), ("envelope", a.envelope), ("model", a.model), ("row", a.row)):
        if v and not TOKEN.match(v):
            print(f"orch spend run: --{name} {v!r} is not a plain name")
            return 2
    sid, srow = _caller_row(cfg)
    if sid and a.row and a.row != srow:
        print(f"orch spend run REFUSED: session {sid} charges {srow or 'the rig'}, not {a.row}")
        return 2
    row = srow if sid else a.row
    if row:
        from rows import find_row
        if find_row(cfg, row) is None:
            print(f"orch spend run: no row {row} in any queue")
            return 2
    phase = phase_of(cfg, row)
    what = Path(cmd[0]).name[:60]
    on = enabled(cfg)
    resfile = None
    if on:
        floor = est_floor(cfg, a.provider)
        if est < floor:      # the caller's estimate is a claim: never below the rig's floor for the provider
            print(f"[orch] spend: --est {usd(est)} raised to the floor {usd(floor)} for {a.provider} "
                  f"(spend.min_est_usd / spend.min_est)", file=sys.stderr)
            est = floor
    with _Lock(cfg):
        if on:
            caps, _ = approved(cfg)
            if caps is None:
                caps = dict(CLOSED)           # nothing approved: every cap is closed
            res = reservations(cfg)
            gr = grants(cfg)
            mine = [g for g in gr if (g["row"], g["provider"]) == (row, a.provider)]
            crossed = crossing(cfg, caps, row, a.provider, phase, est, res + [g for g in gr if g not in mine])
            waiting = [r for r in requests(cfg, ("open", "held")) if row and (r.get("row"), r.get("provider")) == (row, a.provider)]
            if waiting:
                # the row already waits on the Owner for this provider: another estimate never slips past it,
                # and never files a second request (one page per need)
                crossed = waiting[0].get("crossed") or crossed or [{"cap": "?", "limit": 0, "need": 0}]
                rec, new = waiting[0], False
            elif crossed:
                rec, new = open_request(cfg, row, sid, a.provider, est, a.envelope, phase, crossed, what)
            if crossed:
                print(f"SPEND_WAIT {rec['id']}")
                print(f"[orch] spend {rec['id']} ({rec['status']}{', filed' if new else ', already filed'}): "
                      + "; ".join(f"{c['cap']} {usd(c['limit'])} would be crossed by {usd(c['need'])}" for c in crossed)
                      + f". Nothing ran. Claim BLOCKED with exactly this line: orch blocked "
                        f"{row or '<id>'} \"SPEND_WAIT {rec['id']}\" — the engine pages the Owner once and re-dispatches "
                        f"the row once the Owner approves.", file=sys.stderr)
                return EXIT_WAIT
            for g in mine:       # the approved headroom is this run's now
                consume_grant(cfg, g["id"])
        resfile = _resdir(cfg) / f"{hashlib.sha1(_inst(cfg).encode()).hexdigest()[:10]}-{os.getpid()}-{int(time.time() * 1000)}.json"
        try:
            import capability
            start = capability.started(os.getpid())
        except Exception:
            start = 0
        _atomic(resfile, json.dumps({"instance": _inst(cfg), "rig": cfg.name, "row": row, "session": sid,
                                     "provider": a.provider, "model": a.model, "envelope": a.envelope, "phase": phase,
                                     "usd": est, "pid": os.getpid(), "start": start, "epoch": time.time(),
                                     "ts": time.strftime("%F %T")}))
    outf = spend_dir(cfg) / "out" / f"{os.getpid()}-{int(time.time() * 1000)}"
    outf.parent.mkdir(parents=True, exist_ok=True)
    outf.write_text("")

    def _term(*_):
        raise SystemExit(143)
    old = signal.signal(signal.SIGTERM, _term)
    rc = 1
    try:
        rc = subprocess.run(cmd, env=dict(os.environ, ORCH_SPEND_OUT=str(outf))).returncode
    except FileNotFoundError:
        print(f"orch spend run: {cmd[0]}: not found")
        rc = 127
    except SystemExit as e:
        rc = int(e.code or 143)
    finally:
        signal.signal(signal.SIGTERM, old)
        actual, model = None, a.model
        try:
            for line in outf.read_text(errors="replace").splitlines():
                k, _, v = line.strip().partition("=")
                if k == "ORCH_SPEND_ACTUAL":
                    try:
                        f = float(v)
                        if f >= 0 and not math.isinf(f):
                            actual = f
                    except ValueError:
                        pass
                elif k == "ORCH_SPEND_MODEL" and TOKEN.match(v.strip()):
                    model = v.strip()
        except Exception:
            pass
        outf.unlink(missing_ok=True)
        with _Lock(cfg):
            if rc != 127 or actual is not None:     # a command that never started spent nothing
                record(cfg, row, sid, a.provider, model, actual if actual is not None else est,
                       "metered" if actual is not None else "estimated", a.envelope, phase, est=est, rc=rc,
                       res=resfile.name if resfile else "")
            if resfile:
                resfile.unlink(missing_ok=True)
        if on and actual is not None and actual > est + EPS:
            try:
                from store import Store
                Store(cfg.state).event(row or "spend", "spend_over_estimate", est=est, actual=actual,
                                       provider=a.provider)
            except Exception:
                pass
            print(f"[orch] spend: the actual {usd(actual)} exceeded the estimate {usd(est)}", file=sys.stderr)
    return rc


def status_text(cfg) -> str:
    caps, note = approved(cfg)
    L = [f"spend governor: {'ON' if enabled(cfg) else 'off'} — ledger {ledger_path(cfg)}",
         f"approved caps: {describe(caps)}" + (f" ({note})" if note else "")]
    t = tamper(cfg)
    if t:
        L.append(f"⚠ {t}")
    if caps:
        with _Lock(cfg):
            res = reservations(cfg)
        led = lines(cfg)
        now = time.time()
        for c in applicable(caps, "", "", ""):
            spent = sum(float(r.get("usd", 0) or 0) for r in led if _window(r, c["window"], now))
            L.append(f"  {c['cap']:<16} {usd(spent)} of {usd(c['limit'])}")
        for m in ("per_provider", "per_phase", "per_row"):
            for k, lim in sorted((caps.get(m) or {}).items()):
                key = {"per_provider": "provider", "per_phase": "phase", "per_row": "row"}[m]
                w = "month" if m == "per_provider" else "all"
                spent = sum(float(r.get("usd", 0) or 0) for r in led if _window(r, w, now) and str(r.get(key)) == k)
                L.append(f"  {m}.{k:<10} {usd(spent)} of {usd(lim)}")
        if res:
            L.append(f"  running now: {len(res)} reservation(s), {usd(sum(float(r.get('usd', 0)) for r in res))}")
    for h in hold_lines(cfg):
        L.append(f"⚠ {h}")
    if (cfg.state / "spend" / "economy").is_file():
        L.append("economy: " + "; ".join(l for l in (cfg.state / "spend" / "economy").read_text().splitlines()
                                        if l and not l.startswith("#")))
    try:
        import meter
        L.append(meter.status(cfg))
    except Exception:
        pass
    for e in reconcile_entries(cfg):
        L.append(f"reconcile {e['provider']}: every {int(e['every'])} s")
    rq = requests(cfg)
    if rq:
        L.append("requests:")
        for r in rq[-15:]:
            L.append(f"  {r['id']:<6} {r.get('status', ''):<9} {r.get('row') or 'rig'} {usd(r['est'])} {r['provider']}"
                     f" — {', '.join(c['cap'] + ' +' + format(float(c['need']), '.2f') for c in r.get('crossed') or [])}"
                     + (f" (answered {r.get('answer')} {r.get('answered')})" if r.get("answered") else ""))
    return "\n".join(L)


def _operator(cfg, what: str) -> bool:
    import capability
    s = capability.session(cfg)
    if s:
        print(f"REFUSED: {what} is the Owner's (operator shell only); session {s} may not")
        return False
    return True


def main(argv: List[str]) -> int:
    from config_parser import load
    cfg = load()
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__.split("\n\n")[1])
        return 0
    if argv[0] == "run":
        return run_cmd(cfg, argv[1:])
    if argv[0] == "status":
        print(status_text(cfg))
        return 0
    if argv[0] == "ledger":
        n = int(argv[1]) if len(argv) > 1 and argv[1].isdigit() else 20
        for r in lines(cfg)[-n:]:
            print(f"{r.get('ts')} {r.get('row') or 'rig'} {r.get('provider')} {usd(r.get('usd', 0))} {r.get('source')}"
                  f"{' envelope ' + r['envelope'] if r.get('envelope') else ''}")
        return 0
    if argv[0] == "caps":
        if argv[1:2] == ["approve"]:
            if not _operator(cfg, "orch spend caps approve"):
                return 1
            ok, msg = approve(cfg, by="owner")
            print(msg)
            if ok:
                from store import Store
                Store(cfg.state).event("spend", "spend_caps_approved", detail=msg[:300])
            return 0 if ok else 1
        caps, note = approved(cfg)
        print(f"approved: {describe(caps)}" + (f" ({note})" if note else ""))
        print(f"draft:    {caps_file(cfg)} {'present' if caps_file(cfg).is_file() else 'absent'}")
        t = tamper(cfg)
        print(f"⚠ {t}" if t else "no unapproved edit")
        return 0
    if argv[0] == "answer":
        if len(argv) < 3:
            print("usage: orch spend answer <id> A|B [\"note\"]")
            return 2
        if not _operator(cfg, "orch spend answer"):
            return 1
        from engine import Rig
        rid = argv[1] if argv[1].startswith("sp") else f"sp{argv[1].lstrip('#')}"
        ok, msg = answer(Rig(cfg), rid, argv[2], by="owner", via="shell", note=" ".join(argv[3:]))
        print(msg if ok else f"not applied: {msg}")
        return 0 if ok else 1
    print(f"unknown: orch spend {argv[0]} (run | status | ledger | caps [approve] | answer <id> A|B)")
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
