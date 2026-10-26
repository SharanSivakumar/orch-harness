#!/usr/bin/env python3
"""The Owner undoes a Steward act (v3, drop B item 6).

Every act of the Steward lane is a line of steward/STEWARD_LOG.md (state/steward/log.jsonl) with its
revert. The Owner undoes line k with

    orch revert <k>              (operator shell; a session may not — lib/capability.py, GDV3-2)
    revert <k>                   (Telegram, from watchdog.status_bot_allowed_user; the engine runs it)
    orch revert                  the last acts and whether each was reverted (read-only)
    orch revert <k> --dry-run    what `orch revert <k>` would do (read-only)

Only a revert that is ONE orch command of a closed set runs — exactly the forms the Steward's own acts
write (lib/steward.py execute):

    orch assign <row> <executor|default>    orch unpark <row>      orch park <row> ["why"]
    orch start|disable|pause|resume <lane>…  orch inbox done <k>    rm <instance>/control/GO_<row>

They run through the operator's own code paths, in-process (never a shell). Anything else ("none …",
"rm -r rounds/…", "edit …", a compound command, "a new ruling reverting …") is printed for the Owner and
never run. A line reverted successfully is refused a second time (state/steward/reverts.jsonl).

A revert must also undo the act it is logged with (INVERSE: the inverse action, on the same row, lanes or
inbox item); a k recorded twice in log.jsonl is never run.

Telegram: the bot seals the Owner's request in a memfd it holds and rings a doorbell,
control/REVERT_<k>.json {k, pid, fd}. The engine (phase owner_reverts) reads the request through
/proc/<pid>/fd/<fd> and runs it only when that process is the rig's `telegram_bot.py daemon|poll` outside
every session pane and the sealed request is for line k from the allowed user (GDV3-3 (2)); its answer
goes back the same way, sealed in the engine, and the bot trusts nothing else.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import shlex
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))

LANE_CMDS = ("start", "disable", "pause", "resume")
REVERT_RE = re.compile(r"^REVERT_([0-9]+)\.json$")
REQ_RE = re.compile(r"^REVERT_[0-9]+\.[0-9]+$")
# a revert must undo the act it is logged with: the Steward action -> the revert forms that undo it
INVERSE = {"assign": ("assign",), "retry": ("assign",), "park": ("unpark",), "unpark": ("park",),
           "start": ("disable",), "disable": ("start",), "pause": ("resume",), "resume": ("pause",),
           "approve": ("rm-go",), "inbox": ("inbox-done",)}


def scfg(cfg) -> Dict[str, Any]:
    return cfg.data.get("steward") or {}


def sdir(cfg, make: bool = True) -> Path:
    d = cfg.state / "steward"
    if make:
        d.mkdir(parents=True, exist_ok=True)
    return d


def acts(cfg) -> Dict[int, Dict[str, Any]]:
    """STEWARD_LOG lines by k. A k recorded twice is never run (a second record could swap the revert the
    Owner reads in STEWARD_LOG.md for another): it maps to {"k", "_dup": n}."""
    out: Dict[int, Dict[str, Any]] = {}
    seen: Dict[int, int] = {}
    try:
        for line in (sdir(cfg, False) / "log.jsonl").read_text().splitlines():
            try:
                r = json.loads(line)
                k = int(r["k"])
            except Exception:
                continue
            seen[k] = seen.get(k, 0) + 1
            out[k] = r if seen[k] == 1 else {"k": k, "_dup": seen[k], "action": "?", "revert": ""}
    except FileNotFoundError:
        pass
    return out


def history(cfg) -> List[Dict[str, Any]]:
    out = []
    try:
        for line in (sdir(cfg, False) / "reverts.jsonl").read_text().splitlines():
            try:
                out.append(json.loads(line))
            except Exception:
                continue
    except FileNotFoundError:
        pass
    return out


def reverted(cfg, k: int) -> Optional[Dict[str, Any]]:
    """The successful revert of line k, if any."""
    for r in history(cfg):
        if int(r.get("k", -1)) == k and r.get("ok"):
            return r
    return None


# -- what a revert line means --------------------------------------------------------------------
def parse(cfg, text: str) -> Tuple[Optional[List[str]], str]:
    """-> (argv, '') when `text` is one command of the closed set, else (None, why it is printed only).
    argv is normalised: ['assign', row, x] · ['unpark', row] · ['park', row, why] · [<lane cmd>, lane…] ·
    ['inbox-done', k] · ['rm-go', row]."""
    from rows import find_row, active_lanes
    s = " ".join(str(text or "").split("\n")).strip()
    s = re.sub(r"\s{2,}\([^()]*\)\s*$", "", s).strip()      # the trailing "  (re-gates x)" comment
    if not s:
        return None, "the line has no revert"
    if re.search(r"[;&|`$<>]", s):
        return None, "not a single command"
    try:
        w = shlex.split(s)
    except ValueError as e:
        return None, f"cannot parse it ({e})"
    if not w:
        return None, "the line has no revert"

    def row(rid: str) -> str:
        return "" if find_row(cfg, rid) else f"unknown row '{rid}'"

    if w[0] == "rm" and len(w) == 2:
        name = Path(w[1]).name
        m = re.fullmatch(r"GO_(.+)", name)
        p = Path(w[1]) if Path(w[1]).is_absolute() else cfg.root / w[1]
        if not m or p.resolve() != (cfg.control / name).resolve():
            return None, "rm of anything but this rig's control/GO_<row> is the Owner's to do by hand"
        bad = row(m.group(1))
        return (None, bad) if bad else (["rm-go", m.group(1)], "")
    if w[0] != "orch" or len(w) < 2:
        return None, "not an orch command"
    cmd, a = w[1], w[2:]
    if cmd == "assign" and len(a) == 2:
        if a[1] != "default" and a[1] not in cfg.data["executors"]["allowed"]:
            return None, f"executor '{a[1]}' is not in executors.allowed"
        bad = row(a[0])
        return (None, bad) if bad else (["assign", a[0], a[1]], "")
    if cmd == "unpark" and len(a) == 1:
        bad = row(a[0])
        return (None, bad) if bad else (["unpark", a[0]], "")
    if cmd == "park" and len(a) in (1, 2):
        bad = row(a[0])
        return (None, bad) if bad else (["park", a[0], a[1] if len(a) > 1 else ""], "")
    if cmd in LANE_CMDS and a:
        unknown = [l for l in a if l not in active_lanes(cfg)]
        if unknown:
            return None, f"no active queue for lane(s) {' '.join(unknown)}"
        return [cmd, *a], ""
    if cmd == "inbox" and len(a) == 2 and a[0] == "done" and a[1].isdigit():
        return ["inbox-done", a[1]], ""
    return None, f"`orch {cmd}` with these arguments is not a revert orch runs for you"


def check(cfg, rec: Dict[str, Any]) -> Tuple[Optional[List[str]], str]:
    """parse() plus: the revert undoes the act it is logged with (its inverse action, on the same row,
    lanes or inbox item). -> (argv, '') or (None, why it is printed only)."""
    if rec.get("_dup"):
        return None, f"STEWARD_LOG line #{rec.get('k')} is recorded {rec['_dup']} times — check it by hand"
    argv, why = parse(cfg, str(rec.get("revert", "")))
    if argv is None:
        return None, why
    action = str(rec.get("action", ""))
    target = str(rec.get("target", "")).split()
    if argv[0] not in INVERSE.get(action, ()):
        return None, f"`{argv[0]}` does not undo a `{action}` act"
    if argv[0] in LANE_CMDS:
        if sorted(argv[1:]) != sorted(target):
            return None, f"the revert names lanes {' '.join(argv[1:])}, the act {' '.join(target)}"
    elif argv[0] == "inbox-done":
        import steward
        item = next((i for i in steward.inbox_items(cfg, open_only=False) if str(i.get("k")) == argv[1]), None)
        if not item or str(item.get("by", "")) != str(rec.get("session", "")):
            return None, f"inbox item {argv[1]} is not the one session {rec.get('session')} filed"
    elif not target or argv[1] != target[0]:
        return None, f"the revert names row {argv[1]}, the act {target[0] if target else 'none'}"
    return argv, ""


def _quiet(fn, *a, **kw) -> Tuple[bool, str]:
    import steward
    return steward._quiet(fn, *a, **kw)


def apply(rig, argv: List[str], k: int) -> Tuple[bool, str]:
    """Run one parsed revert through the operator's own code path."""
    import orchctl
    import steward
    cfg = rig.cfg
    by = f"owner (revert #{k})"
    op = argv[0]
    if op == "assign":
        return _quiet(orchctl.assign, rig, argv[1], argv[2], by=by)
    if op == "unpark":
        return steward.unpark(rig, argv[1], by=by)
    if op == "park":
        ok = steward.park(rig, argv[1], argv[2] or f"re-parked: the Owner reverted STEWARD_LOG #{k}", by=by)
        return ok, (f"{argv[1]} parked (by the Owner: only the Owner unparks it)" if ok
                    else f"{argv[1]} is already parked or accepted")
    if op in LANE_CMDS:
        lanes = argv[1:]
        for l in lanes:
            if op == "start":
                rig.ctl(f"RUN_{l}").write_text(time.strftime("%F %T\n"))
            elif op == "disable":
                rig.ctl(f"RUN_{l}").unlink(missing_ok=True)
            elif op == "pause":
                rig.ctl(f"PAUSE_{l}").write_text(f"owner: revert of STEWARD_LOG #{k}\n")
            else:
                rig.ctl(f"PAUSE_{l}").unlink(missing_ok=True)
        for l in lanes:
            rig.log(l, f"{op.upper()} lane {l} — {by}")
        return True, {"start": "lanes enabled", "disable": "lanes disabled", "pause": "paused",
                      "resume": "resumed"}[op] + f": {' '.join(lanes)}"
    if op == "inbox-done":
        steward._append(steward.sdir(cfg) / "inbox.jsonl",
                        json.dumps({"k": int(argv[1]), "status": "done", "done": time.strftime("%F %T"), "by": by}) + "\n")
        return True, f"inbox item {argv[1]} marked done"
    if op == "rm-go":
        rid = argv[1]
        go = rig.ctl(f"GO_{rid}")
        had = go.exists()
        go.unlink(missing_ok=True)
        rig.st.forget(f"gate-{rid}")
        rig.st.event(rid, "ungate", by=by)
        ph = rig.st.phase(rid) or "pending"
        note = ("" if ph in ("", "pending", "retry", "parked") else
                f" — {rid} is already {ph}: this re-gates its next run only, it does not undo the run")
        return True, (f"GO_{rid} removed: {rid} is gated again{note}" if had else f"GO_{rid} was already absent{note}")
    return False, f"unknown revert '{op}'"


def _lock(cfg):
    fh = open(sdir(cfg) / "reverts.lock", "a")
    fcntl.flock(fh, fcntl.LOCK_EX)
    return fh


def run(rig, k: int, via: str = "shell", extra: Optional[Dict[str, Any]] = None) -> Tuple[str, str, str]:
    """Revert STEWARD_LOG line k. -> (outcome, detail, the revert text); outcome done | refused | manual
    (printed for the Owner, nothing ran)."""
    cfg = rig.cfg
    rec = acts(cfg).get(k)
    if rec is None:
        return "refused", f"STEWARD_LOG has no line #{k}", ""
    text = str(rec.get("revert", ""))
    with _lock(cfg):
        prev = reverted(cfg, k)
        if prev:
            return "refused", (f"line #{k} was already reverted ({prev.get('ts')}, via {prev.get('via')}): "
                               f"{prev.get('detail', '')}"), text
        argv, why = check(cfg, rec)
        if argv is None:
            return "manual", f"not run ({why}) — do it by hand if you want it: {text or '(none)'}", text
        try:
            ok, detail = apply(rig, argv, k)
        except Exception as e:
            ok, detail = False, f"{type(e).__name__}: {e}"
        out = {"k": k, "ts": time.strftime("%F %T"), "epoch": int(time.time()), "via": via, "ok": bool(ok),
               "action": rec.get("action"), "target": rec.get("target"), "session": rec.get("session"),
               "revert": text, "detail": " ".join(str(detail).split())[:400]}
        out.update(extra or {})
        with open(sdir(cfg) / "reverts.jsonl", "a") as f:
            f.write(json.dumps(out, sort_keys=True) + "\n")
    rig.st.event(f"steward:{rec.get('incident')}", "steward_revert", k=k, via=via, ok=bool(ok),
                 action=rec.get("action"), target=str(rec.get("target", ""))[:120], revert=text[:200])
    rig.st.engine_log(f"revert #{k} via {via}: {'done' if ok else 'FAILED'} — {text} — {out['detail']}")
    return ("done" if ok else "refused"), out["detail"], text


# -- Telegram: control/REVERT_<k>.json ----------------------------------------------------------------
# A file under control/ or state/ is plain data any same-uid process can write, rename or rewrite, and a
# pid written in it proves nothing. So the request and the answer each travel in a SEALED memfd (no
# write, no resize, no unseal) held open by the process that made it; the file is only a doorbell naming
# that pid and fd. A reader opens /proc/<pid>/fd/<fd>: only that process can have put a memfd there, and
# nobody can change its content afterwards. The engine trusts a request only from the rig's Telegram bot,
# and the bot trusts an answer only from the rig's engine (GDV3-3 (2)).
REQ_MEMFD, ANS_MEMFD = "orch-revert", "orch-revert-answer"
_HELD: Dict[str, Tuple[int, float]] = {}       # engine side: answer memfds kept open for the bot to read
HOLD_SECS = 300


def sealing() -> bool:
    return hasattr(os, "memfd_create") and hasattr(fcntl, "F_ADD_SEALS") and Path("/proc/self/fd").is_dir()


def sealed(name: str, rec: Dict[str, Any]) -> int:
    fd = os.memfd_create(name, os.MFD_ALLOW_SEALING | os.MFD_CLOEXEC)
    os.write(fd, json.dumps(rec, sort_keys=True, default=str).encode())
    fcntl.fcntl(fd, fcntl.F_ADD_SEALS,
                fcntl.F_SEAL_WRITE | fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_GROW | fcntl.F_SEAL_SEAL)
    return fd


def read_sealed(pid: Any, fd: Any, name: str) -> Tuple[Optional[Dict[str, Any]], str]:
    """The record in process pid's fd, when it is a fully sealed memfd called `name`."""
    try:
        pid, fd = int(pid), int(fd)
    except (TypeError, ValueError):
        return None, "the doorbell names no process and fd"
    p = f"/proc/{pid}/fd/{fd}"
    try:
        if not os.readlink(p).startswith(f"/memfd:{name} "):
            return None, f"fd {fd} of pid {pid} is not a {name} memfd"
        with open(p, "rb") as f:
            need = fcntl.F_SEAL_WRITE | fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_GROW | fcntl.F_SEAL_SEAL
            if fcntl.fcntl(f.fileno(), fcntl.F_GET_SEALS) & need != need:
                return None, f"the {name} memfd is not sealed"
            rec = json.loads(f.read(65536).decode())
        return (rec, "") if isinstance(rec, dict) else (None, "the sealed record is not an object")
    except Exception as e:
        return None, f"cannot read fd {fd} of pid {pid} ({type(e).__name__})"


def _argv(pid: int) -> List[str]:
    try:
        return [a for a in Path(f"/proc/{pid}/cmdline").read_bytes().decode(errors="replace").split("\0") if a]
    except Exception:
        return []


def _instance_of(pid: int) -> str:
    try:
        env = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
        v = next((e[len(b"ORCH_INSTANCE="):].decode() for e in env if e.startswith(b"ORCH_INSTANCE=")), "")
        return str(Path(v).resolve()) if v else ""
    except Exception:
        return ""


def _role_ok(cfg, pid: int, script: str, modes: Tuple[str, ...]) -> str:
    """'' when pid runs lib/<script> (in one of `modes`, if any), for this instance, outside every session pane."""
    argv = _argv(pid)
    at = next((i for i, a in enumerate(argv) if Path(a).name == script), -1)
    if at < 0 or (modes and argv[at + 1:at + 2] not in [[m] for m in modes]):
        return f"pid {pid} is not the rig's {script}"
    if _instance_of(pid) != str(Path(cfg.instance).resolve()):
        return f"pid {pid} serves another instance"
    import capability
    s = capability.inside(cfg, pid) if capability.enabled(cfg) else None
    if s:
        return f"pid {pid} runs in session {s.get('sid')}'s pane"
    return ""


def proof(cfg, door: Dict[str, Any], k: int) -> Tuple[Optional[Dict[str, Any]], str]:
    """-> (the Owner's request, '') when the doorbell points at a sealed request the rig's bot made for line
    k from the allowed user; else (None, why not). Without memfd sealing (not Linux) the doorbell itself is
    the request and the sender id decides (v2)."""
    allowed = str((cfg.data.get("watchdog") or {}).get("status_bot_allowed_user") or "")
    if sealing():
        bad = _role_ok(cfg, int(door.get("pid") or 0), "telegram_bot.py", ("daemon", "poll")) \
            if str(door.get("pid", "")).isdigit() else "the doorbell names no bot process"
        if bad:
            return None, bad
        rec, bad = read_sealed(door.get("pid"), door.get("fd"), REQ_MEMFD)
        if rec is None:
            return None, bad
    else:
        rec = door
    if str(rec.get("k")) != str(k):
        return None, f"the sealed request is for line #{rec.get('k')}, the doorbell for #{k}"
    if sealing() and str(rec.get("instance")) != str(Path(cfg.instance).resolve()):
        return None, "the sealed request is for another instance"
    if not allowed or str(rec.get("from_id", "")) != allowed:
        return None, "the request does not come from watchdog.status_bot_allowed_user"
    return rec, ""


def answer_path(cfg, name: str) -> Path:
    return sdir(cfg) / "reverts" / f"{name}.json"


def expire() -> None:
    now = time.time()
    for req, (fd, at) in list(_HELD.items()):
        if now - at > HOLD_SECS:
            try:
                os.close(fd)
            except OSError:
                pass
            _HELD.pop(req, None)


def consume(rig) -> None:
    """Engine tick phase: every control/REVERT_<k>.json doorbell, proved and run; the answer goes back
    sealed. One bad entry never stops the others: each is answered (when it can be) and removed."""
    cfg = rig.cfg
    expire()
    for f in sorted(cfg.control.glob("REVERT_*.json")):
        m = REVERT_RE.match(f.name)
        door: Dict[str, Any] = {}
        try:
            if f.is_file() and not f.is_symlink():
                raw = json.loads(f.read_text())
                door = raw if isinstance(raw, dict) else {}
        except Exception:
            door = {}
        req = f"REVERT_{m.group(1) if m else 0}.{int(time.time() * 1000)}"
        try:
            if not m or not door:
                outcome, detail, text = "refused", "malformed request (not a plain file of JSON)", ""
            elif not scfg(cfg).get("telegram_revert", True):
                outcome, detail, text = "refused", "Telegram revert is off (steward.telegram_revert)", ""
            else:
                rec, why = proof(cfg, door, int(m.group(1)))
                if rec is None:
                    outcome, detail, text = "refused", why, ""
                    rig.st.event("steward:revert", "steward_revert_refused", k=m.group(1), detail=why[:300])
                    rig.st.engine_log(f"revert REFUSED (control/{f.name}) — {why}")
                else:
                    if REQ_RE.match(str(rec.get("req", ""))):
                        req = str(rec["req"])
                    outcome, detail, text = run(rig, int(m.group(1)), via="telegram",
                                                extra={"message_id": str(rec.get("message_id", ""))[:20]})
        except Exception as e:
            outcome, detail, text = "refused", f"could not be handled ({type(e).__name__}: {e})", ""
        try:
            ans = {"k": m.group(1) if m else "", "req": req, "outcome": outcome, "why": detail, "revert": text,
                   "handled": time.strftime("%F %T")}
            door_out = dict(ans)
            if sealing():
                fd = sealed(ANS_MEMFD, ans)
                _HELD[req] = (fd, time.time())
                door_out.update(pid=os.getpid(), fd=fd)
            p = answer_path(cfg, req)
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_name(f".{p.name}.tmp")
            tmp.write_text(json.dumps(door_out, indent=1, sort_keys=True, default=str))
            os.replace(tmp, p)
        except Exception as e:
            rig.st.engine_log(f"revert: could not answer control/{f.name} ({type(e).__name__}: {e})")
        finally:
            try:
                if f.is_dir() and not f.is_symlink():
                    shutil.rmtree(f, ignore_errors=True)
                else:
                    f.unlink(missing_ok=True)
            except Exception:
                pass


def request(cfg, k: int, msg: Dict[str, Any]):
    """The bot's side: the Owner's request sealed in a memfd this process holds, and the doorbell
    control/REVERT_<k>.json naming it. -> (answer path, the memfd, '') or (None, None, why not). Close the
    memfd (withdraw) once answered."""
    if not scfg(cfg).get("telegram_revert", True):
        return None, None, "Telegram revert is off on this rig (steward.telegram_revert)"
    if k not in acts(cfg):
        return None, None, f"STEWARD_LOG has no line #{k}"
    prev = reverted(cfg, k)
    if prev:
        return None, None, f"line #{k} was already reverted ({prev.get('ts')})"
    f = cfg.control / f"REVERT_{k}.json"
    if f.exists():
        return None, None, f"a revert of #{k} is already pending"
    req = f"REVERT_{k}.{int(time.time() * 1000)}"
    rec = {"k": k, "req": req, "from_id": str((msg.get("from") or {}).get("id", "")),
           "message_id": msg.get("message_id"), "epoch": int(time.time()),
           "instance": str(Path(cfg.instance).resolve())}
    fd = None
    door = rec
    if sealing():
        fd = sealed(REQ_MEMFD, rec)
        door = {"k": k, "pid": os.getpid(), "fd": fd}
    tmp = cfg.control / f".revert-{k}-{os.getpid()}.tmp"   # never matches the engine's REVERT_* glob
    tmp.write_text(json.dumps(door, indent=1, sort_keys=True))
    os.replace(tmp, f)
    return answer_path(cfg, req), fd, ""


def withdraw(cfg, k: int, fd, door_too: bool) -> None:
    """Close the request's memfd; when the engine never answered, remove the doorbell too (a doorbell that
    names a closed fd is refused anyway)."""
    if door_too:
        (cfg.control / f"REVERT_{k}.json").unlink(missing_ok=True)
    if fd is not None:
        try:
            os.close(fd)
        except OSError:
            pass


def wait_answer(cfg, p: Path, timeout: float, name: str = ANS_MEMFD) -> Dict[str, Any]:
    """The bot's side: the engine's answer, trusted only when it is sealed in the rig's engine process
    (else, without sealing, the plain file). An answer that does not prove itself is ignored."""
    end = time.time() + timeout
    while True:
        if p.exists():
            try:
                door = json.loads(p.read_text())
                if not sealing():
                    return door
                pid = int(door.get("pid") or 0)
                if not _role_ok(cfg, pid, "engine.py", ()):
                    rec, _ = read_sealed(pid, door.get("fd"), name)
                    if rec is not None and rec.get("req") == p.stem:
                        return rec
            except Exception:
                pass
        if time.time() >= end:
            return {"outcome": "timeout"}
        time.sleep(0.5)


# -- CLI ----------------------------------------------------------------------------------------
def listing(cfg, n: int = 15) -> str:
    a = acts(cfg)
    if not a:
        return "(no Steward acts yet — nothing to revert)"
    L = []
    for k in sorted(a)[-n:]:
        r = a[k]
        rv = reverted(cfg, k)
        argv, why = check(cfg, r)
        state = (f"REVERTED {rv.get('ts')}" if rv else "orch revert runs it" if argv else f"by hand ({why})")
        L.append(f"#{k:<4} {r.get('ts', '')[:16]} {r.get('session', '')} {r.get('action', '')} {r.get('target', '')} "
                 f"— revert: {r.get('revert', '')}  [{state}]")
    return "\n".join(L)


def main(argv: List[str]) -> int:
    from config_parser import load
    cfg = load()
    if not argv or argv[0] in ("list", "-l"):
        print(listing(cfg))
        return 0
    if argv[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    if not re.fullmatch(r"#?[0-9]+", argv[0]):
        print("usage: orch revert [<k> [--dry-run]]   (k = the # of a STEWARD_LOG line)")
        return 2
    k = int(argv[0].lstrip("#"))
    rec = acts(cfg).get(k)
    if rec is None:
        print(f"STEWARD_LOG has no line #{k}")
        return 1
    if "--dry-run" in argv[1:]:
        argv_, why = check(cfg, rec)
        rv = reverted(cfg, k)
        print(f"#{k}: {rec.get('action')} {rec.get('target', '')} by {rec.get('session')} — revert: {rec.get('revert')}")
        print("already reverted " + str(rv.get("ts")) if rv else
              f"orch revert {k} would run: {' '.join(argv_)}" if argv_ else f"orch revert {k} would only print it ({why})")
        return 0
    import capability
    me = capability.session(cfg)
    if me:
        print(f"REFUSED: `orch revert` is the operator's command; session {me} may not run it (a process that "
              f"descends from a session's pane is that session). If your work needs it, say so in PROGRESS.md "
              f"and block.")
        return 1
    from engine import Rig
    outcome, detail, text = run(Rig(cfg), k, via="shell")
    if outcome == "done":
        print(f"REVERTED #{k} ({rec.get('action')} {rec.get('target', '')}): {text} — {detail}")
        return 0
    if outcome == "manual":
        print(f"#{k} ({rec.get('action')} {rec.get('target', '')}): {detail}")
        return 3
    print(f"REFUSED #{k}: {detail}")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
