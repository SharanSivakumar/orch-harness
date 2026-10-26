#!/usr/bin/env python3
"""The v2 engine: one process per rig that schedules rows, watches sessions,
judges claims, runs reviews and unblockers, and keeps tmux tidy.

It replaces v1's one-bash-watcher-per-lane. The invariants v1 paid for are kept:

  * VISIBLE TMUX ONLY — every session is a named, attachable window.
  * QUEUE RESCANNED EVERY TICK — appending or editing rows while it runs is safe.
  * EXECUTOR RESOLVED AT DISPATCH — control/EXECUTOR_<id> > queue column > default.
  * A LIVE WINDOW WE DISPATCHED IS THE ROW RUNNING — never double-dispatch, never
    yank a running session; a window that merely wears the name is an ORPHAN.
  * DIED WITHOUT A CLAIM = HALT AND PAGE (or retier once, by policy).
  * SILENCE IS NEVER SUCCESS, AND NO CLAIM IS TRUSTED UNCHECKED.

What is new is in docs/V2_DESIGN.md. In one line: a row counts as done only
when its claim has passed the close pipeline and, by policy, an independent
review on a different executor.

Run through `orch start` (window `orch-engine`). `python3 engine.py tick` runs one
pass and exits (tests, debugging).
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tmuxio  # noqa: E402
import template_engine as te  # noqa: E402
from config_parser import Config, load  # noqa: E402
from leases import Leases  # noqa: E402
from rows import QRow, active_lanes, implicit_deps, find_row, load_rows, plan_dir, unmet_deps  # noqa: E402
from store import Store, negative, norm_outcome  # noqa: E402

LIB = Path(__file__).resolve().parent
SERVICE_WINDOWS = {"control", "orch-engine", "orch-bot", "bash", "zsh"}
HALTED_PHASES = {"blocked", "route_a", "halted"}
DISPATCHABLE = {"", "retry"}
SUB_VERDICTS = {"unblock": ("RESOLVED", "ESCALATE"), "review": ("ACCEPT", "REJECT"),
                "preflight": ("PASS", "FIX"), "roundreview": ("DONE", "FAILED"),
                "steward": ("RESOLVED", "PARKED", "INBOX", "ESCALATE", "CONFIRM", "OBJECT"),
                "planner": ("PASS", "FAIL")}   # steward: CONFIRM|OBJECT only for a panel falsifier (steward.verdicts_for)
IDLE_VERDICT = {"unblock": "ESCALATE", "review": "REJECT", "preflight": "FIX", "roundreview": "FAILED",
                "steward": "ESCALATE", "planner": "FAIL"}


def kind_of(sid: str) -> str:
    if sid.startswith("ub-"):
        return "unblock"
    if sid.startswith("rv-"):
        return "review"
    if sid.startswith("pf-"):
        return "preflight"
    if sid.startswith("rr-"):
        return "roundreview"
    if sid.startswith("sw-"):
        return "steward"
    if sid.startswith("pl-"):
        return "planner"
    return "row"


OUTCOME_TOKEN = r"[A-Za-z][A-Za-z0-9_-]*"


def close_pattern(sid: str, kind: str) -> "re.Pattern":
    """The ORCH_CLOSE line of one session. Groups: 1 the verdict, 2 a row's outcome, 3 the summary.
    Only a row carries an outcome: `CLOSED:<token>` (any token — an unknown one counts as negative),
    or `CLOSED: <go|nogo|no-go|held|fail|infeasible>` (with a space only the outcome words, so prose after a
    colon is never read as an outcome)."""
    verbs = "|".join(SUB_VERDICTS.get(kind, ("CLOSED", "BLOCKED", "ROUTE_A")))
    oc = (rf"(?::((?<=:){OUTCOME_TOKEN}|[ \t]+(?i:no[-_]?go|go|held|fail|infeasible)(?![A-Za-z0-9_-])))?"
          if kind == "row" else "()")
    return re.compile(rf"^\s*ORCH_CLOSE\s+{re.escape(sid)}\s+({verbs}){oc}\b[ \t]*(?:—|-)?[ \t]*(.*)$")


def parent_of(sid: str) -> str:
    """ub-<id>-<n> / rv-<id>-<n> -> <id>; pf-<scope> -> ''."""
    k = kind_of(sid)
    if k in ("unblock", "review"):
        return sid[3:].rsplit("-", 1)[0]
    return ""


def mem_available_mb() -> int:
    try:
        for line in open("/proc/meminfo"):
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) // 1024
    except Exception:
        pass
    return 1 << 30


def plan_sha(cfg: Config, row: QRow) -> str:
    try:
        return hashlib.sha256((cfg.workspace / row.plan).read_bytes()).hexdigest()
    except Exception:
        return ""


URGENCY_RANK = {"low": 0, "normal": 1, "critical": 2}


def framework_head() -> str:
    try:
        p = subprocess.run(["git", "-C", str(LIB.parent), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=15)
        return p.stdout.strip() if p.returncode == 0 else ""
    except Exception:
        return ""


def framework_dirty() -> bool:
    try:
        p = subprocess.run(["git", "-C", str(LIB.parent), "status", "--porcelain"], capture_output=True, text=True,
                           timeout=15)
    except Exception:
        return True
    return any(l.strip() and "__pycache__" not in l and ".tested/" not in l for l in p.stdout.splitlines())


def push_wanted(watchdog: Dict[str, Any], urgency: str) -> bool:
    """An engine event goes to the phone when its urgency reaches watchdog.push_min_urgency
    (default "normal"; "low" events never push)."""
    floor = max(1, URGENCY_RANK.get(str(watchdog.get("push_min_urgency") or "normal"), 1))
    return URGENCY_RANK.get(urgency, 0) >= floor


class Rig:
    """Everything that acts on a rig. The engine loop and the CLI share it, so a
    manual `orch dispatch` and an engine dispatch are the same code path."""

    def __init__(self, cfg: Optional[Config] = None):
        self.cfg = cfg or load()
        self.st = Store(self.cfg.state)
        self.leases = Leases(self.cfg)
        for d in (self.cfg.control, self.cfg.leases, self.cfg.prompts, self.cfg.unblock):
            d.mkdir(parents=True, exist_ok=True)

    # -- small helpers ------------------------------------------------------
    @property
    def session(self) -> str:
        return self.cfg.tmux_session

    def log(self, lane: str, msg: str) -> None:
        self.st.lane_log(lane or "-", msg)
        self.st.engine_log(f"[{lane or '-'}] {msg}")

    def notify(self, urgency: str, title: str, body: str, row: str = "", page_class: str = "") -> None:
        """With the Steward lane on, only a page of an allowed class (steward.page_classes — the
        Owner's ruling) reaches the phone as critical; every other critical event is a digest
        line (normal), because the Steward handles it."""
        sc = self.cfg.data.get("steward") or {}
        asked = urgency
        if urgency == "critical" and sc.get("enabled") and page_class not in (sc.get("page_classes") or []):
            urgency = "normal"
        try:    # the record of every notification, sent or not (urgency after the downgrade; never the body)
            with open(self.cfg.state / "notify.jsonl", "a") as fh:
                fh.write(json.dumps({"epoch": round(time.time(), 3), "ts": time.strftime("%F %T"), "urgency": urgency,
                                     "asked": asked, "page_class": page_class, "row": row,
                                     "title": str(title)[:120]}) + "\n")
        except Exception:
            pass
        try:
            if subprocess.run(["which", "notify-send"], capture_output=True).returncode == 0:
                subprocess.Popen(["notify-send", "-u", urgency, title, body],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass
        if push_wanted(self.cfg.data.get("watchdog") or {}, urgency):
            try:
                subprocess.Popen([sys.executable, str(LIB / "telegram_bot.py"), "push", "--title", title,
                                  "--body", body] + (["--row", row] if row else [])
                                 + (["--class", page_class] if page_class else []), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 env=dict(os.environ, ORCH_INSTANCE=str(self.cfg.instance)))
            except Exception:
                pass

    def hook(self, name: str, **env: str) -> None:
        cmd = (self.cfg.data.get("hooks") or {}).get(name)
        if not cmd:
            return
        e = dict(os.environ, ORCH_INSTANCE=str(self.cfg.instance), ORCH_HOOK=name)
        e.update({f"ORCH_{k.upper()}": str(v) for k, v in env.items()})
        try:
            subprocess.Popen(["bash", "-c", f"timeout 120 bash -c {shlex.quote(cmd)}"], cwd=str(self.cfg.root),
                             env=e, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as ex:
            self.st.engine_log(f"hook {name} failed to start: {ex}")

    def ctl(self, name: str) -> Path:
        return self.cfg.control / name

    def resolve_exec(self, row: QRow) -> Tuple[str, str]:
        """-> (executor, problem): the executor a dispatch really uses. The row's own executor (resolve_exec_raw),
        moved to its fallback target while that executor is in fallback (lib/fallback.py)."""
        x, prob = self.resolve_exec_raw(row)
        if prob or not x:
            return x, prob
        try:
            import fallback
            return fallback.effective(self.cfg, x), ""
        except Exception as e:
            if self.st.once(f"fallback-effective-{type(e).__name__}"):
                self.st.engine_log(f"fallback: could not resolve the fallback target ({type(e).__name__}: {e})")
            return x, ""

    def resolve_exec_raw(self, row: QRow) -> Tuple[str, str]:
        """-> (executor, problem). An override naming an unknown executor is a problem,
        never a silent fall-back to the default (v1 ran such rows on opus)."""
        allowed = set(self.cfg.data["executors"]["allowed"])
        try:
            o = re.sub(r"[^a-z0-9_-]", "", self.ctl(f"EXECUTOR_{row.id}").read_text().strip().split()[0].lower())
        except Exception:
            o = ""
        if o:
            if o in allowed:
                return o, ""
            return "", f"control/EXECUTOR_{row.id} names '{o}', which is not in executors.allowed"
        if row.queue_exec in allowed:
            return row.queue_exec, ""
        return "", f"queue executor '{row.queue_exec}' is not in executors.allowed"

    def lane_enabled(self, lane: str) -> bool:
        return self.ctl(f"RUN_{lane}").exists() or self.ctl("RUN_ALL").exists()

    def paused(self, lane: str) -> bool:
        return self.ctl("PAUSE").exists() or self.ctl(f"PAUSE_{lane}").exists()

    def stopped(self, lane: str = "") -> bool:
        return self.ctl("STOP").exists() or bool(lane and self.ctl(f"STOP_{lane}").exists())

    def parked(self, rid: str) -> bool:
        """A parked row (`orch park`, a Steward's or the panel's park) is a named carry: no path re-dispatches,
        retiers, nudges or halts it until it is unparked. Read from result.json itself, at the moment of acting,
        so a park the CLI wrote since the caller read the row still holds."""
        return str(self.st.result(rid).get("phase", "")) == "parked"

    def requeue(self, rid: str, res: Dict[str, Any], lane: str, what: str) -> bool:
        """Write the `retry` phase that makes the scheduler dispatch the row again, unless it is parked. -> written"""
        if self.parked(rid):
            self.st.event(rid, "held_parked", detail=f"{what} — not re-dispatched: the row is parked")
            self.log(lane, f"{rid}: {what} — NOT re-dispatched: the row is parked (unpark releases it)")
            return False
        self.st.write_result(rid, res)
        return True

    def gate_open(self, row: QRow) -> Tuple[bool, str]:
        go = self.ctl(f"GO_{row.id}")
        if not go.exists():
            return False, f"gated — release with: orch approve {row.id}"
        try:
            words = go.read_text().split()
        except Exception:
            words = []
        if len(words) >= 2 and words[0] == "sha256":
            cur = plan_sha(self.cfg, row)
            if cur and cur != words[1]:
                return False, f"plan changed since GO_{row.id} was given — re-approve: orch approve {row.id}"
        return True, ""

    # -- session records ----------------------------------------------------
    def inflight(self) -> Dict[str, Dict[str, Any]]:
        out: Dict[str, Dict[str, Any]] = {}
        for f in self.cfg.state.glob("*.dispatch.json"):
            sid = f.name[: -len(".dispatch.json")]
            d = self.st.dispatch(sid)
            if d.get("open"):
                out[sid] = d
        return out

    def close_session(self, sid: str, why: str) -> None:
        d = self.st.dispatch(sid)
        if d.get("open"):
            d.update(open=False, ended=time.strftime("%F %T"), ended_epoch=int(time.time()), end=why)
            self.st.write_dispatch(sid, d)
        owner = sid if kind_of(sid) == "row" else ""
        if owner:
            rel = self.leases.release(owner, d.get("resources") or [])
            if rel:
                self.st.engine_log(f"released {','.join(rel)} held for {sid}")

    def live_count(self, wins: Dict[str, tmuxio.Win]) -> Tuple[int, Dict[str, int]]:
        n, by = 0, {}
        for sid, d in self.inflight().items():
            if d.get("executor") == "manual":
                continue
            w = wins.get(sid)
            if w and w["alive"]:
                n += 1
                by[d.get("executor", "")] = by.get(d.get("executor", ""), 0) + 1
        return n, by

    # -- dispatch -------------------------------------------------------------
    def launch_cmd(self, sid: str, executor: str, order: Path, kind: str, plan_d: Optional[Path]) -> str:
        env = {"ORCH_INSTANCE": str(self.cfg.instance), "ORCH_ROW": sid, "ORCH_KIND": kind,
               "ORCH_PLAN_DIR": str(plan_d or "")}
        envs = " ".join(f"{k}={shlex.quote(v)}" for k, v in env.items())
        return f"{envs} {shlex.quote(str(LIB / 'launch.sh'))} {shlex.quote(sid)} {shlex.quote(executor)} {shlex.quote(str(order))}"

    def open_window(self, sid: str, executor: str, order: Path, kind: str, lane: str,
                    plan_d: Optional[Path], record: Dict[str, Any]) -> Tuple[bool, str]:
        wins = tmuxio.windows(self.session)
        w = wins.get(sid)
        if w and w["alive"]:
            return False, f"a live window named '{sid}' already exists (orphan or running) — not dispatching a second session"
        if w and not w["alive"]:
            tmuxio.kill(self.session, w["wid"])
        (self.cfg.state / f"{sid}.exit").unlink(missing_ok=True)
        tmuxio.ensure_session(self.session, str(self.cfg.root))
        ident = tmuxio.new_window(self.session, sid, str(self.cfg.root),
                                  self.launch_cmd(sid, executor, order, kind, plan_d))
        if not ident:
            return False, "tmux new-window failed"
        (self.cfg.state / f"{sid}.window").write_text(f"{ident} {int(time.time())}\n")
        rec = dict(record, sid=sid, kind=kind, executor=executor, lane=lane, ident=ident, open=True,
                   ts=time.strftime("%F %T"), epoch=int(time.time()), workorder=str(order),
                   boot_id=tmuxio.boot_id(), tmux_created=tmuxio.session_created(self.session))
        self.st.write_dispatch(sid, rec)
        import capability
        capability.register(self.cfg, sid, kind, ident)      # the pane is that session, ORCH_ROW or not
        if executor in (self.cfg.data["executors"].get("paste_kick") or []):
            threading.Thread(target=self.paste_kick, args=(sid, lane), daemon=True).start()
        try:
            import health
            health.Health(self).on_dispatch(executor, sid)
        except Exception:
            pass
        return True, ident

    def paste_kick(self, sid: str, lane: str) -> None:
        """TUIs that take a pasted prompt: paste prompts/<id>.kick.txt once the input prompt is ready."""
        kick = self.cfg.prompts / f"{sid}.kick.txt"
        ready = re.compile(r"Type your message|>\s*$")
        for _ in range(45):
            time.sleep(2)
            text = tmuxio.capture(self.session, sid, 40)
            if ready.search(text):
                time.sleep(1)
                ok = tmuxio.send_text(self.session, sid, kick.read_text() if kick.is_file() else "")
                self.log(lane, f"{sid} kick text {'pasted' if ok else 'paste FAILED'}")
                return
        self.log(lane, f"{sid} kick paste FAILED — the TUI never showed its prompt; paste prompts/{sid}.kick.txt by hand")
        self.notify("normal", f"{self.cfg.name}: {sid} kick paste failed",
                    f"Paste prompts/{sid}.kick.txt into tmux window '{sid}'.")

    def dispatch_row(self, row: QRow, executor: str, manual: bool = False) -> Tuple[bool, str]:
        from closepipe import git_head, probe_snapshot, protected_snapshot
        attempt = self.st.attempt(row.id) + 1
        if self.st.status(row.id)[0] != "NONE":
            self.st.set_aside(row.id, f"stale{attempt}")   # never let an old claim end the new attempt
        head = git_head(self.cfg.git_repo)
        snap = {"review": self.review_needed(row, executor), "probe": probe_snapshot(self.cfg, row),
                "meta": dict(row.meta), "protected": protected_snapshot(self.cfg, row.id, head)}
        if executor == "manual":
            order = te.write_prompt(self.cfg, row.id, row.lane, "manual", row.plan, attempt=attempt)
            self.st.write_dispatch(row.id, {"sid": row.id, "kind": "row", "executor": "manual", "lane": row.lane,
                                            "attempt": attempt, "open": True, "ts": time.strftime("%F %T"),
                                            "epoch": int(time.time()), "head": head,
                                            "workorder": str(order), "resources": row.resources,
                                            "snapshot": snap})
            self.st.event(row.id, "dispatch", attempt=attempt, executor="manual", lane=row.lane)
            self.log(row.lane, f"{row.id} WAITING FOR OPERATOR (manual executor) — work order {order.name}")
            self.notify("normal", f"{self.cfg.name}: {row.id} is YOURS (manual)",
                        f"Work order: prompts/{order.name} · close: orch done {row.id} \"summary\"")
            return True, "manual"
        try:
            order = te.write_prompt(self.cfg, row.id, row.lane, executor, row.plan, attempt=attempt)
        except SystemExit as e:
            return False, f"work order could not be rendered: {e}"
        left = te.leftovers(order.read_text())
        if left:
            self.st.engine_log(f"{row.id}: work order carries unfilled placeholders {left}")
        ok, how = self.open_window(row.id, executor, order, "row", row.lane, plan_dir(self.cfg, row),
                                   {"attempt": attempt, "head": head,
                                    "resources": row.resources, "manual": manual, "snapshot": snap})
        if not ok:
            return False, how
        for name in ("idle_nudges", "limit_notified", "timeout_notified", "orphan_notified", "rl"):
            self.st.rm(f"{row.id}.{name}")
        self.st.event(row.id, "dispatch", attempt=attempt, executor=executor, lane=row.lane,
                      manual=manual or None)
        self.log(row.lane, f"dispatched {row.id} attempt {attempt} (tmux window '{row.id}', executor {executor})")
        self.hook("on_dispatch", row=row.id, lane=row.lane, executor=executor, attempt=attempt)
        return True, how

    def dispatch_sub(self, sid: str, executor: str, order: Path, lane: str, parent: str,
                     extra: Dict[str, Any]) -> Tuple[bool, str]:
        import spend
        if not self.cfg.decides(executor):     # a junior tier never unblocks, reviews, plans or stewards
            return False, (f"{executor} is a {self.cfg.flavor(executor)} tier and may not run a "
                           f"{kind_of(sid)} session — set a hard tier in orch.yaml (orch lint names the key)")
        blk, why = spend.hold_blocks(self.cfg, executor, parent)    # planner, panel, preflight, review, unblock too
        if blk:
            return False, why
        ok, how = self.open_window(sid, executor, order, kind_of(sid), lane, None,
                                   dict(extra, parent=parent, attempt=1,
                                        parent_attempt=self.st.attempt(parent) if parent else 0))
        if ok:
            self.st.event(parent or sid, f"{kind_of(sid)}_dispatch", session=sid, executor=executor)
            self.log(lane, f"dispatched {sid} ({kind_of(sid)}, executor {executor})")
        return ok, how

    def unblock_eligible(self, executor: str) -> bool:
        lst = [x for x in (self.cfg.data["executors"].get("auto_unblock") or []) if x]
        if lst:
            return executor in lst
        return self.cfg.flavor(executor) == "jrdev"

    def review_needed(self, row: QRow, executor: str) -> bool:
        """The policy as configured now — evaluated once, at dispatch, into the attempt's
        snapshot. Judging reads the snapshot (review_required), so a row cannot switch its
        own review off by editing the queue or orch.yaml mid-attempt."""
        m = row.meta.get("review")
        if m in ("0", "1"):
            return m == "1"
        pol = (self.cfg.data.get("review") or {}).get("policy", "all")
        if pol == "none":
            return False
        if pol == "junior":
            return self.cfg.flavor(executor) == "jrdev"
        return executor != "manual"

    def review_required(self, row: QRow, executor: str) -> bool:
        snap = self.st.dispatch(row.id).get("snapshot") or {}
        if "review" in snap:
            return bool(snap["review"])
        return self.review_needed(row, executor)

    def reviewer_for(self, row: QRow, executor: str) -> str:
        rc = self.cfg.data.get("review") or {}
        allowed = list(self.cfg.data["executors"]["allowed"])
        cands = [row.meta.get("reviewer"), (rc.get("reviewer_map") or {}).get(executor),
                 rc.get("default_reviewer", "sonnet"), rc.get("fallback_reviewer", "opus")]
        cands = [c for c in cands if c and c in allowed and c != "manual"]
        if rc.get("require_different_executor", True):
            diff = [c for c in cands if c != executor]
            if diff:
                return diff[0]
            self.st.engine_log(f"{row.id}: no allowed reviewer differs from executor {executor} — using {cands[:1]}")
        return cands[0] if cands else "opus"

    # -- sub-session verdicts ----------------------------------------------------
    def sub_verdict(self, sid: str, after_epoch: float) -> Tuple[str, str]:
        """A sub-session's verdict counts only when the engine (or `orch preflight`/
        `orch round review`) dispatched THAT session after `after_epoch` and the verdict
        was written after the dispatch. Anything else — a status file a row wrote for
        its own reviewer, or a verdict left from an earlier attempt — reads FORGED."""
        v, summary, mt = self.st.status(sid)
        if v == "NONE":
            return "NONE", ""
        d = self.st.dispatch(sid)
        ep = float(d.get("epoch", 0) or 0)
        if not d or not d.get("ident") or ep + 1 < after_epoch or mt + 1 < ep:
            return "FORGED", summary
        return v, summary

    def consume(self, sid: str) -> None:
        self.st.set_aside(sid, "consumed")

    # -- the claim pipeline ---------------------------------------------------
    def reject_claim(self, row: QRow, tag: str, verdict: str, summary: str) -> None:
        """Rewrite the row's claim (kept aside as state/<id>.<tag>.status)."""
        self.st.set_aside(row.id, tag)
        self.st.write_status(row.id, verdict, summary)


class Engine(Rig):
    def __init__(self, cfg: Optional[Config] = None):
        super().__init__(cfg)
        self.cfg_mtime = (self.cfg.instance / "orch.yaml").stat().st_mtime
        self.jobs: Dict[str, Tuple[subprocess.Popen, float]] = {}
        self.sched_notes: Dict[str, str] = {}
        self.last_checks: Dict[int, float] = {}
        self.started = time.time()
        import health
        self.health = health.Health(self)
        self.steward_wants: set = set()     # executors a waiting Steward session needs first
        self.framework_sha = framework_head()
        self._last_update_check = time.time()
        self._last_bookkeeping = time.time()
        self._phase_errors: Dict[str, int] = {}

    # -- lifecycle ------------------------------------------------------------
    def reload_if_changed(self) -> None:
        p = self.cfg.instance / "orch.yaml"
        try:
            m = p.stat().st_mtime
        except FileNotFoundError:
            return
        if m != self.cfg_mtime:
            try:
                self.cfg = load(str(self.cfg.instance))
                self.leases = Leases(self.cfg)
                self.cfg_mtime = m
                self.st.engine_log("orch.yaml changed — configuration reloaded")
            except BaseException as e:  # a broken edit keeps the last good config
                self.st.engine_log(f"orch.yaml reload FAILED, keeping the last good configuration: {e}")
                self.cfg_mtime = m

    def run(self, wait_lock: bool = False) -> int:
        """wait_lock (the systemd unit): block until no other engine holds the lock, so a
        service engine and a tmux-launched one never fight — the service takes over when the
        other exits."""
        import signal
        lock = open(self.cfg.state / "engine.lock", "w")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | (0 if wait_lock else fcntl.LOCK_NB))
        except OSError:
            print("another engine already holds state/engine.lock for this rig — exiting")
            return 3
        lock.write(f"{os.getpid()}\n")
        lock.flush()
        def _term(*_):
            raise SystemExit(0)
        signal.signal(signal.SIGTERM, _term)
        self.st.ensure_adopted()
        (self.cfg.state / "engine.stopped").unlink(missing_ok=True)
        self.st.engine_log(f"engine up (pid {os.getpid()}, framework {self.framework_sha[:7]}"
                           f"{', service' if os.environ.get('ORCH_SERVICE') else ''})")
        print(f"[orch-engine] {self.cfg.name} — pid {os.getpid()} — tick {self.tick_s()}s. "
              f"This process IS the scheduler; stopping it stops dispatching (sessions keep running).", flush=True)
        self.reconcile_leases()
        try:
            self.health.startup()
        except Exception:
            self.st.engine_log(f"health startup probes failed:\n{traceback.format_exc()}")
        while True:
            if self.ctl("ENGINE_STOP").exists():
                self.ctl("ENGINE_STOP").unlink(missing_ok=True)
                (self.cfg.state / "engine.stopped").write_text(time.strftime("%F %T\n"))
                self.st.engine_log("engine stopped by control/ENGINE_STOP")
                print("[orch-engine] stopped (orch engine stop)", flush=True)
                return 0
            t0 = time.time()
            self.tick()
            (self.cfg.state / "engine.heartbeat").write_text(f"{int(time.time())} {os.getpid()}\n")
            why = self.reexec_reason()
            if why:
                self.st.engine_log(f"engine re-exec: {why}")
                print(f"[orch-engine] re-exec: {why}", flush=True)
                lock.close()
                os.execv(sys.executable, [sys.executable, str(LIB / "engine.py"), *sys.argv[1:]])
            time.sleep(max(1.0, self.tick_s() - (time.time() - t0)))

    def reexec_reason(self) -> str:
        """A Steward's restart request, or a newer TESTED framework commit on a clean tree
        (engine.self_update). Only between jobs, so no close check is cut off."""
        if self.jobs:
            return ""
        f = self.ctl("ENGINE_RESTART")
        if f.exists():
            why = f.read_text(errors="replace").strip()[:200]
            f.unlink(missing_ok=True)
            return f"restart requested ({why})"
        ec = self.cfg.data.get("engine") or {}
        if not ec.get("self_update", True) or time.time() - self._last_update_check < float(ec.get("update_check_secs", 60)):
            return ""
        self._last_update_check = time.time()
        head = framework_head()
        if not head or head == self.framework_sha:
            return ""
        if not (LIB.parent / ".tested" / head).exists():
            if self.st.once(f"untested-head-{head[:12]}"):
                self.st.engine_log(f"framework HEAD {head[:7]} is not tested (tests/run_all.sh) — engine stays on "
                                   f"{self.framework_sha[:7]}")
            return ""
        if framework_dirty():
            return ""
        return f"framework {self.framework_sha[:7]} -> {head[:7]} (tested)"

    def tick_s(self) -> float:
        return float((self.cfg.data.get("scheduler") or {}).get("tick_seconds", 15))

    def tick(self) -> None:
        self.reload_if_changed()
        for phase in (self.poll_jobs, self.monitor, self.judge_claims, self.rearm_requests, self.row_requests,
                      self.queue_watch, self.owner_answers, self.owner_reverts,
                      self.fallback_tick, self.health_tick, self.course_loop_tick, self.steward_tick, self.spend_tick, self.phase_tick, self.schedule,
                      self.reap, self.lane_completion, self.periodic_checks, self.flush_pages,
                      self.digest_tick, self.bookkeeping):
            try:
                phase()
                self._phase_errors.pop(phase.__name__, None)
            except Exception:
                self.st.engine_log(f"ERROR in {phase.__name__}:\n{traceback.format_exc()}")
                n = self._phase_errors[phase.__name__] = self._phase_errors.get(phase.__name__, 0) + 1
                if n == 5:
                    try:
                        import steward
                        steward.open_incident(self, "engine_error", f"rig:{phase.__name__}",
                                              f"engine phase {phase.__name__} failed 5 ticks in a row (state/engine.log)",
                                              klass="mechanical")
                    except Exception:
                        pass
        try:
            (self.cfg.state / "sched.json").write_text(json.dumps(self.sched_notes, indent=1, sort_keys=True))
        except Exception:
            pass

    def flush_pages(self) -> None:
        """Pages held during quiet hours go out on the first tick after the window ends,
        even when nothing else is pushed (at most one attempt a minute)."""
        if not (self.cfg.state / "held_pages.jsonl").exists() or time.time() - getattr(self, "_flush_at", 0) < 60:
            return
        self._flush_at = time.time()
        import telegram_bot
        if telegram_bot.in_quiet_hours(self.cfg):
            return
        subprocess.Popen([sys.executable, str(LIB / "telegram_bot.py"), "flush"], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, env=dict(os.environ, ORCH_INSTANCE=str(self.cfg.instance)))

    def rearm_requests(self) -> None:
        """Rows with rearm= on their queue line ask; the engine resets (lib/rearm.py)."""
        import rearm
        rearm.consume(self)

    def row_requests(self) -> None:
        """Planner / course-loop sessions ask `orch row add`; the engine validates and appends (lib/rowadd.py)."""
        import rowadd
        rowadd.consume(self)

    def queue_watch(self) -> None:
        """guard.queue_watch: a queue edited outside `orch row add` is reported (state/queues.sha)."""
        import rowadd
        rowadd.watch(self)

    def owner_answers(self) -> None:
        """watchdog.answers: control/ANSWER_<row>.json (written by the bot from the Owner's Telegram
        reply) turns a halted row into an unblocking one whose session is the answer session: it
        records the ruling and applies it. RESOLVED re-dispatches the row as any unblock does;
        ESCALATE halts and pages again (no ladder). The bot never acts; the engine does."""
        if not (self.cfg.data.get("watchdog") or {}).get("answers"):
            return
        import telegram_bot
        for f in sorted(self.cfg.control.glob("ANSWER_*.json")):
            rid = f.stem[len("ANSWER_"):]
            try:
                rec = json.loads(f.read_text())
            except Exception:
                continue
            done = self.cfg.state / "answers"
            done.mkdir(exist_ok=True)
            row = find_row(self.cfg, rid)
            ph = self.st.phase(rid)
            if row is None or ph not in telegram_bot.ELIGIBLE or str(rec.get("row")) != rid:
                os.replace(f, done / f"{rid}.{int(time.time())}.refused.json")
                self.log(row.lane if row else "", f"answer for {rid} refused (phase {ph or 'none'})")
                continue
            letter = str(rec.get("letter", "")).strip().upper()[:1]
            note = " ".join(str(rec.get("note", "")).split())[:1000]
            owner = (self.cfg.data.get("watchdog") or {}).get("owner_name") or "the Owner"
            ax = telegram_bot.answer_executor(self.cfg)
            n = self.st.read_int(f"{rid}.unblock_n", 0) + 1
            reason = (f"OWNER ANSWER — {owner} answered '{letter}'"
                      f"{' with the note: ' + repr(note) if note else ''} by Telegram reply at "
                      f"{time.strftime('%F %T', time.localtime(float(rec.get('epoch', time.time()))))} to this row's page "
                      f"(the row folder's PAGE.txt and the decision brief in its PROGRESS). You are the answer session, not an "
                      f"analyst: (1) record a RULED ledger entry '({owner}, via Telegram, answer {letter})' with "
                      f"`orch gd next --reserve`, quoting option {letter} as the page and the decision brief state it; (2) apply exactly "
                      f"what option {letter} says, as a dated amendment to the row's PLAN (Rulings line updated); (3) never "
                      f"cross a hard stop the answer does not itself release; (4) RESOLVED re-dispatches the row. If "
                      f"option {letter} is ambiguous or needs a choice it does not make, write a new PAGE.txt and ESCALATE.")
            self.st.write_int(f"{rid}.unblock_n", n)
            self.sheet_open(rid, n, f"Owner answer {letter} via Telegram")
            res = self.st.result(rid)
            res.update(phase="unblocking", unblock_n=n, reason=reason, unblock_epoch=time.time(),
                       answer=letter, answer_exec=ax)
            self.st.write_result(rid, res)
            os.replace(f, done / f"{rid}.{int(time.time())}.json")
            self.st.event(rid, "owner_answer", letter=letter, session=f"ub-{rid}-{n}")
            self.log(row.lane, f"{rid}: owner answer {letter} by Telegram — answer session ub-{rid}-{n} ({ax})")

    def owner_reverts(self) -> None:
        """Telegram `revert <k>`: control/REVERT_<k>.json from the bot — proved and run (lib/revert.py)."""
        import revert
        if any(self.cfg.control.glob("REVERT_*.json")):
            revert.consume(self)
        elif revert._HELD:
            revert.expire()        # sealed answers the bot has had time to read

    def reconcile_leases(self) -> None:
        live = set(self.inflight())
        rows, by_id = load_rows(self.cfg)
        for name in self.leases.res:
            for p, info in self.leases.holders(name):
                owner = info.get("owner", "")
                if (info.get("instance") in ("", str(self.cfg.instance)) and owner in by_id
                        and (info.get("pid") in ("", "0")) and owner not in live):
                    v, _, _ = self.st.status(owner)
                    if v != "NONE":
                        self.leases.release(owner, [name])
                        self.st.engine_log(f"reconcile: released '{name}' held by finished row {owner}")

    # -- v3 phases: health, Steward, digest, bookkeeping ------------------------
    def fallback_tick(self) -> None:
        """executors.fallback (lib/fallback.py): an executor whose confirmed rate-limited rows meet its entry's
        thresholds is put in fallback and its rows move. Nothing without an entry."""
        import fallback
        fallback.tick(self)

    def health_tick(self) -> None:
        self.health.tick()

    def course_loop_tick(self) -> None:
        """round.yaml on_fail: course_loop (lib/courseloop.py, drop D): a judge's negative outcome walks review ->
        answers -> planner -> rows -> re-armed judge, inside the open phase and its caps. Off without the key."""
        import courseloop
        courseloop.tick(self)

    def exec_policy(self, lane: str) -> Dict[str, Any]:
        """The rig's executor settings with the lane's round's `executors:` overrides (lib/rounds.py, drop D)."""
        import rounds
        try:
            return rounds.exec_policy(self.cfg, lane)
        except Exception as e:
            if self.st.once(f"exec-policy-error-{lane}-{type(e).__name__}"):
                self.st.engine_log(f"round executor policy for lane {lane} unreadable ({type(e).__name__}: {e}) — "
                                   f"the rig's executors: apply")
            return dict(self.cfg.data["executors"], round_max_parallel={}, round="")

    def steward_tick(self) -> None:
        import steward
        steward.tick(self, dict(self.sched_notes))

    def spend_tick(self) -> None:
        """spend.enabled (lib/spend.py): Telegram spend answers, the caps watch, one page per spend request,
        spend_wait rows woken when their request is approved."""
        import spend
        spend.tick(self, self.sched_notes)

    def phase_tick(self) -> None:
        """phases.yaml (lib/phases.py, drop D): an unapproved edit is reported; a phase at a cap goes to rest.
        Nothing here opens a phase — that is the Owner's (`orch phase open`)."""
        import phases
        phases.tick(self)

    def digest_tick(self) -> None:
        if not ((self.cfg.data.get("digest") or {}).get("times")):
            return
        import digest
        digest.tick(self)

    def bookkeeping(self) -> None:
        """engine.commit_bookkeeping: the engine's own files (block sheet, unblock analyses, the
        Steward's log/inbox/rulings/incidents, digests, reviews) are committed through the guard,
        so no human has to commit what the engine and its sub-sessions wrote."""
        ec = self.cfg.data.get("engine") or {}
        if not ec.get("commit_bookkeeping") or time.time() - self._last_bookkeeping < float(ec.get("bookkeeping_every", 1800)):
            return
        self._last_bookkeeping = time.time()
        inst = self.cfg.instance
        try:
            inst.relative_to(self.cfg.git_repo)
        except ValueError:
            return
        cands = [inst / "BLOCK_SHEET.md", inst / "unblock", inst / "steward", inst / "digest", inst / "reviews"]
        paths = [str(c.relative_to(self.cfg.git_repo)) for c in cands if c.exists()]
        if not paths:
            return
        p = subprocess.run(["git", "-C", str(self.cfg.git_repo), "status", "--porcelain", "--untracked-files=all",
                            "--", *paths],
                           capture_output=True, text=True)
        changed = []
        for l in p.stdout.splitlines():
            path = l[3:].strip().strip('"')
            if " -> " in path:
                path = path.split(" -> ", 1)[1]
            changed.append(path)
        try:   # the Owner's files (the rights table under steward/) are never the engine's to commit
            import protected
            pats = protected.patterns(self.cfg)
            kept = [c for c in changed if protected.matches(c, pats)]
            changed = [c for c in changed if not protected.matches(c, pats)]
            if kept:
                self.st.engine_log(f"bookkeeping leaves protected path(s) uncommitted: {' '.join(kept[:5])}")
        except Exception as e:
            self.st.engine_log(f"bookkeeping skipped: protected-path filter failed ({e})")
            return
        if not changed:
            return
        env = dict(os.environ, ORCH_INSTANCE=str(inst))
        for k in ("ORCH_ROW", "ORCH_KIND", "ORCH_PLAN_DIR"):
            env.pop(k, None)
        q = subprocess.run(["bash", str(LIB / "parallel_guard_core.sh"), "commit", "plan:engine", "-m",
                            f"engine bookkeeping: {len(changed)} file(s) (block sheet, unblock, steward, digest, reviews)",
                            "--", *changed], capture_output=True, text=True, env=env, cwd=str(self.cfg.git_repo))
        self.st.engine_log(f"bookkeeping commit ({len(changed)} file(s)): "
                           f"{'ok' if q.returncode == 0 else 'FAILED ' + ' '.join((q.stdout + q.stderr).split())[-300:]}")

    # -- async jobs (close checks, probes, metrics, periodic checks) ----------
    def start_job(self, key: str, args: List[str], out: Path, cwd: Optional[str] = None,
                  env: Optional[Dict[str, str]] = None) -> None:
        if key in self.jobs:
            return
        f = open(out, "w")
        os.chmod(out, 0o600)
        p = subprocess.Popen(args, cwd=cwd or str(self.cfg.root), stdout=f, stderr=subprocess.STDOUT,
                             stdin=subprocess.DEVNULL, env=dict(os.environ, ORCH_INSTANCE=str(self.cfg.instance),
                                                                **(env or {})))
        self.jobs[key] = (p, time.time())

    def job_running(self, key: str) -> bool:
        return key in self.jobs

    def poll_jobs(self) -> None:
        for key, (p, t0) in list(self.jobs.items()):
            if p.poll() is None:
                continue
            del self.jobs[key]
            kind, _, ident = key.partition(":")
            if kind == "probe":
                self.on_probe_done(ident, p.returncode)
            elif kind == "metric":
                self.on_metric_done(ident, p.returncode)
            elif kind == "check":
                self.on_periodic_done(int(ident), p.returncode)
            elif kind == "health":
                self.health.on_probe_done(ident, p.returncode)
            elif kind == "duty":
                import steward
                steward.on_duty_done(self, ident, p.returncode)
            elif kind == "spendrec":
                import spend
                spend.on_reconcile_done(self, ident, p.returncode)

    # -- monitor live sessions ------------------------------------------------
    def monitor(self) -> None:
        wins = tmuxio.windows(self.session)
        rows, by_id = load_rows(self.cfg)
        idle_cfg = self.cfg.data.get("idle") or {}
        now = time.time()
        for sid, d in self.inflight().items():
            kind = kind_of(sid)
            lane = d.get("lane", "")
            v, summary, _ = self.st.status(sid)
            if v != "NONE":
                if v == "CLOSED" and kind == "row" and sid in by_id and self.outcome_crosscheck_wait(by_id[sid]):
                    continue   # the session may still print its closing line's outcome: keep it open a moment
                self.close_session(sid, f"claim {v}")
                continue
            if d.get("executor") == "manual":
                continue
            w = wins.get(sid)
            ours = bool(w and w["alive"] and w.ident == d.get("ident"))
            if not ours:
                if w and w["alive"]:
                    if self.st.once(f"orphan-{sid}-{d.get('epoch')}"):
                        self.log(lane, f"{sid}: the live window is not the one dispatched (orphan) — waiting")
                    continue
                # dead or gone: re-read the claim once (a session may write it as it exits)
                time.sleep(0.5)
                v, summary, _ = self.st.status(sid)
                if v != "NONE":
                    self.close_session(sid, f"claim {v}")
                    continue
                rc, exited = self.exit_record(sid, d)
                if not exited and self.interrupted(d):
                    self.on_interrupted(sid, kind, d)
                    continue
                self.on_dead(sid, kind, d, (w or {}).get("dead_status", "") or rc, by_id,
                             gone=w is None and not exited)
                continue
            tail = tmuxio.tail_lines(tmuxio.capture(self.session, w["wid"], 80), 14)
            idle = now - (w["activity"] or now)
            # 0. health: a login prompt is infra (the Owner's), a survivor closes a half-open breaker
            if self.health.auth_check(sid, d, tail):
                tmuxio.kill(self.session, w["wid"])
                self.on_interrupted(sid, kind, d, why="its CLI asked for a login (executor breaker open, kind auth)")
                continue
            if now - float(d.get("epoch", now)) > float((self.cfg.data.get("health") or {}).get("fast_death_secs", 60)):
                self.health.on_survive(d.get("executor", ""), sid)
            # 1. auto-seal from the final turn only
            if self.try_seal(sid, kind, d, tail, idle):
                continue
            # 2. usage limits — pane tail only (a parked row is never nudged on: it waits for its unpark)
            held = kind == "row" and self.parked(sid)
            import fallback
            if kind == "row" and fallback.has_entry(self.cfg, d.get("executor", "")):
                # an executor with a fallback entry: the rate-limit watch is lib/fallback.py's. A confirmed
                # row moves (fallback_tick); one that cannot (no free target) waits as a usage limit does.
                rec = fallback.observe(self, sid, d, tmuxio.capture(self.session, w["wid"], 120))
                if rec.get("confirmed"):
                    if not fallback.resolve(self.cfg, d.get("executor", "")):
                        self.limit_check(sid, d, tail, nudge=not held)
                    continue
            elif self.limit_check(sid, d, tail, nudge=not held):
                continue
            # 3. idle without a claim
            na = int(idle_cfg.get("nudge_after", 0) or 0)
            if na and idle > na and not held:
                self.idle_nudge(sid, kind, d, by_id, idle, int(idle_cfg.get("max_nudges", 2)),
                                int(idle_cfg.get("block_after", 0) or 0))
            # 4. wall-clock budget
            if kind == "row":
                row = by_id.get(sid)
                tmo = (row.timeout_minutes if row else 0) or int((self.cfg.data.get("scheduler") or {}).get("row_timeout_minutes", 0) or 0)
                if tmo and now - float(d.get("epoch", now)) > tmo * 60:
                    self.on_timeout(sid, d, tmo)

    def try_seal(self, sid: str, kind: str, d: Dict[str, Any], tail: List[str], idle: float) -> bool:
        if idle < 20:
            return False
        # a row may declare its outcome on the closing line: ORCH_CLOSE <id> CLOSED:nogo — …
        pat = close_pattern(sid, kind)
        for line in reversed(tail):
            m = pat.match(line)
            if m and not re.search(r"<[A-Za-z][^<>]*>", line):
                if not self.sub_evidence_ok(sid, kind):
                    return False
                oc = (m.group(2) or "") if kind == "row" and m.group(1) == "CLOSED" else ""
                self.st.write_status(sid, m.group(1), m.group(3).strip() or "sealed from the closing line", oc)
                self.st.event(parent_of(sid) or sid, "sealed", session=sid, verdict=m.group(1),
                              outcome=norm_outcome(oc) or None)
                self.log(d.get("lane", ""), f"{sid} sealed {m.group(1)}{':' + norm_outcome(oc) if oc else ''} "
                                            f"from its closing line (idle {int(idle)}s)")
                self.close_session(sid, f"sealed {m.group(1)}")
                return True
        return False

    def sub_evidence_ok(self, sid: str, kind: str) -> bool:
        """What `orch verdict` requires before a sub-session's verdict counts — the seal
        path may not skip it."""
        if kind == "review":
            f = self.cfg.instance / "reviews" / f"{sid[3:]}.md"
        elif kind == "preflight":
            f = self.cfg.instance / "preflight" / f"{sid[3:]}.review.md"
        elif kind == "roundreview":
            import rounds
            d = rounds.review_dir(self.cfg, sid[3:])        # rounds/<tag>/review, or review+<judge> (v3.1)
            return d.is_dir() and any(d.iterdir())
        elif kind == "steward":
            import steward
            f = steward.evidence_file(self.cfg, sid)
        elif kind == "planner":
            import steward
            f = steward.planner_evidence(self.cfg, sid)
        else:
            return True
        return f.is_file() and f.stat().st_size >= 40

    def limit_check(self, sid: str, d: Dict[str, Any], tail: List[str], nudge: bool = True) -> bool:
        ex = self.cfg.data["executors"]
        fam = self.cfg.family(d.get("executor", ""))
        pat = self.cfg.pick(ex.get("limit_patterns") or {}, d.get("executor", ""))
        if isinstance(pat, list):
            pat = "|".join(f"(?:{p})" for p in pat)
        if not pat:
            return False
        text = "\n".join(tail[-8:])
        if not re.search(pat, text, re.I):
            if self.st.has(f"{sid}.limited"):
                self.st.rm(f"{sid}.limited")
                self.log(d.get("lane", ""), f"{sid} is past its usage limit")
            return False
        now = int(time.time())
        last = self.st.read_int(f"{sid}.limited", 0)
        if not last:
            self.st.write_int(f"{sid}.limited", now)
            self.st.event(parent_of(sid) or sid, "limit", session=sid, executor=d.get("executor"))
            self.notify("normal", f"{self.cfg.name}: {sid} hit a usage limit",
                        "It is nudged to continue every 15 min until the limit lifts; never retiered or killed.")
            return True
        if now - last > 900 and nudge:
            resume = self.cfg.pick(ex.get("limit_resume_text") or {}, d.get("executor", ""), "continue")
            tmuxio.send_text(self.session, sid, resume)
            self.st.write_int(f"{sid}.limited", now)
            self.log(d.get("lane", ""), f"{sid} usage-limit banner — resume nudge sent")
        return True

    def idle_nudge(self, sid: str, kind: str, d: Dict[str, Any], by_id: Dict[str, QRow],
                   idle: float, max_n: int, block_after: int) -> None:
        n = self.st.read_int(f"{sid}.idle_nudges", 0)
        last = float(self.st.read_int(f"{sid}.idle_last", 0))
        now = time.time()
        if n < max_n and now - last > (self.cfg.data["idle"].get("nudge_after", 1200)):
            row = by_id.get(parent_of(sid) or sid)
            text = te.nudge_text(self.cfg, sid, kind, d.get("lane", ""), d.get("executor", ""),
                                 row.plan if row else "")
            if text and tmuxio.send_text(self.session, sid, text):
                self.st.write_int(f"{sid}.idle_nudges", n + 1)
                self.st.write_int(f"{sid}.idle_last", int(now))
                self.st.event(parent_of(sid) or sid, "idle_nudge", session=sid, n=n + 1, idle=int(idle))
                self.log(d.get("lane", ""), f"{sid} idle {int(idle)}s with no claim — nudge {n + 1}/{max_n} sent")
                self.hook("on_idle", row=sid, idle=int(idle))
            return
        if n >= max_n and block_after and now - last > block_after:
            w = tmuxio.windows(self.session).get(sid)
            if w and w.ident == d.get("ident"):
                tmuxio.kill(self.session, w["wid"])   # its resources are released below: the work must stop too
            if kind == "row":
                self.st.write_status(sid, "BLOCKED", f"idle without a claim after {n} nudges (idle {int(idle)}s)")
            else:
                verdict = IDLE_VERDICT[kind]
                self.st.write_status(sid, verdict, f"{kind} session idle without a verdict after {n} nudges")
            self.log(d.get("lane", ""), f"{sid} idle past the block budget — claim written by the engine")
            self.close_session(sid, "idle budget")

    def on_timeout(self, sid: str, d: Dict[str, Any], tmo: int) -> None:
        if not self.st.once(f"timeout-{sid}-{d.get('attempt')}"):
            return
        detail = f"attempt {d.get('attempt')} passed its {tmo} min budget"
        self.st.event(sid, "timeout", attempt=d.get("attempt"), detail=detail)
        if (self.cfg.data.get("scheduler") or {}).get("timeout_action") == "block":
            w = tmuxio.windows(self.session).get(sid)
            if w and w.ident == d.get("ident"):
                tmuxio.kill(self.session, w["wid"])
            self.st.write_status(sid, "BLOCKED", detail)
            self.close_session(sid, "timeout")
        self.notify("normal", f"{self.cfg.name}: {sid} over its time budget", detail)

    def exit_record(self, sid: str, d: Dict[str, Any]) -> Tuple[str, bool]:
        """lib/launch.sh writes state/<sid>.exit when the CLI exits by itself. -> (rc, exited)."""
        try:
            rc, ep = (self.cfg.state / f"{sid}.exit").read_text().split()[:2]
            return rc, float(ep) + 1 >= float(d.get("epoch", 0) or 0)
        except Exception:
            return "", False

    def interrupted(self, d: Dict[str, Any]) -> bool:
        """The session was cut off by a host reboot or a tmux server restart, not by its own failure:
        the boot id changed, or the tmux session is newer than the dispatch."""
        b, cur = d.get("boot_id", ""), tmuxio.boot_id()
        if b and cur and b != cur:
            return True
        if not tmuxio.has_session(self.session):
            return True     # the whole tmux session is gone: the server (or the host) went away
        tc = int(d.get("tmux_created", 0) or 0)
        now_tc = tmuxio.session_created(self.session)
        return bool(tc and now_tc and now_tc > tc + 1)

    def on_interrupted(self, sid: str, kind: str, d: Dict[str, Any], why: str = "") -> None:
        """Re-dispatch what a reboot, tmux restart or login prompt cut off — never charged: no
        attempt is counted against retier, unblock or review budgets, and nothing halts."""
        why = why or "cut off by a host reboot or tmux server restart"
        lane = d.get("lane", "")
        self.close_session(sid, f"interrupted — {why}")
        d = self.st.dispatch(sid)
        d["interrupted"] = True
        self.st.write_dispatch(sid, d)
        self.st.event(parent_of(sid) or sid, "interrupted", session=sid, attempt=d.get("attempt"), why=why,
                      executor=d.get("executor"))
        if kind == "row" and self.parked(sid):
            self.log(lane, f"{sid}: {why} — NOT re-dispatched: the row is parked (unpark releases it)")
            return
        self.log(lane, f"{sid}: {why} — re-dispatching, not charged")
        if kind == "row":
            if self.st.phase(sid) in ("", "retry"):
                self.requeue(sid, {"phase": "retry", "status_mtime": 0, "why": f"interrupted: {why}"}, lane,
                             f"interrupted: {why}")
        elif kind == "unblock":
            parent = parent_of(sid)
            res = self.st.result(parent)
            if res.get("phase") == "unblocking":
                res["unblock_epoch"] = time.time()
                self.st.write_result(parent, res)
        elif kind == "roundreview":
            try:
                import rounds
                rounds.dispatch_round_review(self.cfg, self.st, sid[3:], rig=self)
            except Exception as e:
                self.st.engine_log(f"{sid}: round review not re-dispatched: {e}")
        # review: drive_review re-dispatches on its own; steward/planner: lib/steward.py

    def on_dead(self, sid: str, kind: str, d: Dict[str, Any], dead_status: str, by_id: Dict[str, QRow],
                gone: bool = False) -> None:
        lane = d.get("lane", "")
        x = d.get("executor", "")
        if kind == "row" and self.parked(sid):
            # parked while it ran (the Owner then closed its window, or it ended by itself): the death is
            # recorded and the row stays parked — no retier, no halt, no page, no infra count
            self.close_session(sid, f"window dead while parked (exit {dead_status or '?'})")
            self.st.event(sid, "dead", session=sid, attempt=d.get("attempt"), exit=dead_status or "?",
                          executor=x, parked=True)
            self.log(lane, f"{sid}: window dead with no claim while PARKED — recorded; stays parked, not "
                           f"re-dispatched or retiered (unpark releases it)")
            return
        # only a pane that died by itself is an infra suspect; a window that vanished was killed
        # (operator, reaper) or lost with the tmux server (interrupted(), checked before this)
        if not gone and self.health.is_fast(d):
            # infra suspect: the first fast death of a session is retried unchanged; enough distinct
            # fast deaths open the executor's breaker (lib/health.py)
            n = self.st.read_int(f"{sid}.fast_deaths", 0) + 1
            self.st.write_int(f"{sid}.fast_deaths", n)
            opened = self.health.record_fast_death(x, sid)
            if n == 1 or opened or self.health.state_of(x) != "closed":
                self.st.event(parent_of(sid) or sid, "dead", session=sid, attempt=d.get("attempt"),
                              exit=dead_status or "?", executor=x, fast=True)
                self.on_interrupted(sid, kind, d, why=f"died {int(time.time() - float(d.get('epoch', 0) or 0))}s "
                                                      f"after dispatch with no claim (infra suspect, executor {x})")
                return
        self.close_session(sid, f"window dead (exit {dead_status or '?'})")
        self.st.event(parent_of(sid) or sid, "dead", session=sid, attempt=d.get("attempt"),
                      exit=dead_status or "?", executor=d.get("executor"))
        if kind in ("steward", "planner"):
            self.log(lane, f"{sid}: {kind} session ended with no verdict (exit {dead_status or '?'})")
            return
        if kind != "row":
            parent = parent_of(sid)
            self.log(lane, f"{sid}: {kind} session ended with no verdict (exit {dead_status or '?'})")
            if parent and (int(d.get("parent_attempt", 0) or 0) != self.st.attempt(parent)
                           or self.st.phase(parent) not in ("reviewing", "unblocking")):
                return   # a session of an attempt that no longer exists (reset/retry) — never touches the new one
            if kind == "review" and parent:
                n = self.st.read_int(f"{parent}.review_deaths", 0) + 1
                self.st.write_int(f"{parent}.review_deaths", n)
                if n <= 1:
                    return  # the claim judge re-dispatches a reviewer
            if parent:
                self.halt(parent, by_id.get(parent), "halted",
                          f"{sid} ({kind}) ended without a verdict — operator: inspect, then orch reset {parent}")
            return
        row = by_id.get(sid)
        ex = self.exec_policy(row.lane if row else lane)     # the round's executors: override the rig's
        pol = ex.get("dead_window_policy", "halt")
        x = d.get("executor", "")
        if self.parked(sid):        # parked since the check above (the CLI writes it beside the engine)
            self.log(lane, f"{sid}: window dead with no claim while PARKED — recorded; stays parked")
            return
        if (pol == "retier" and row and self.unblock_eligible(x) and not self.st.has(f"{sid}.retiered")
                and self.cfg.decides(ex["retier_on_max_unblock"])):     # never retier onto a junior tier
            target = ex["retier_on_max_unblock"]
            self.ctl(f"EXECUTOR_{sid}").write_text(f"{target}\n")
            self.st.touch(f"{sid}.retiered")
            self.st.write_result(sid, {"phase": "retry", "status_mtime": 0, "why": "dead window, retiered"})
            self.st.event(sid, "retier", to=target, detail=f"window died with no claim (exit {dead_status or '?'})")
            self.log(lane, f"{sid}: window dead with NO claim — dead_window_policy=retier: re-dispatching once on {target}")
            self.notify("normal", f"{self.cfg.name}: {sid} ({x}) died with no claim", f"Retiered to {target}, re-dispatched once.")
            return
        self.halt(sid, row, "halted", f"session ended with NO claim (exit {dead_status or '?'}) — investigate, then: orch reset {sid}")

    def page_body(self, rid: str, row: Optional[QRow], summary: str) -> str:
        """The phone body of a halt / Route A page: the row folder's PAGE.txt (the executor's or
        unblocker's plain-language page, cap 700 chars) when present, else the summary (cap 400)."""
        if row is None:
            from rows import find_row
            row = find_row(self.cfg, rid)
        d = plan_dir(self.cfg, row) if row else None
        page = ""
        try:
            if d and (d / "PAGE.txt").is_file():
                page = (d / "PAGE.txt").read_text(errors="replace").strip()[:700]
        except Exception:
            page = ""
        body = page or " ".join(str(summary).split())[:400]
        if (self.cfg.data.get("watchdog") or {}).get("answers"):
            return body       # the bot adds the answer line (reply by letter, or Remote Control)
        return f"{body}\nanswer: {rid} — reply via Remote Control to a Claude Code session"

    def halt(self, rid: str, row: Optional[QRow], phase: str, why: str) -> None:
        res = self.st.result(rid)
        _, _, mt = self.st.status(rid)
        res.update(phase=phase, why=why, status_mtime=mt, halted_at=time.strftime("%F %T"))
        self.st.write_result(rid, res)
        lane = row.lane if row else ""
        self.st.event(rid, "halt", phase=phase, detail=why)
        self.log(lane, f"HALTED {rid}: {why}")
        self.notify("critical", f"{self.cfg.name} — {rid} HALTED ({phase})", self.page_body(rid, row, why),
                    row=rid)
        self.hook("on_halt", row=rid, phase=phase, why=why)

    # -- judging claims -------------------------------------------------------
    def judge_claims(self) -> None:
        rows, by_id = load_rows(self.cfg, include_archived=False)
        live = self.inflight()
        for row in rows:
            ph = self.st.phase(row.id)
            if ph == "claimed":
                if row.id in live and self.outcome_crosscheck_wait(row):
                    continue
                if row.id in live:
                    self.close_session(row.id, "claim")
                self.judge_row(row)
            elif ph == "verifying":
                self.collect_check(row)
            elif ph == "reviewing":
                self.drive_review(row, live)
            elif ph == "unblocking":
                self.drive_unblock(row, live)
            elif ph == "blocked_claim":   # the engine stopped between recording a block and acting on it
                res = self.st.result(row.id)
                self.on_blocked(row, res.get("executor", ""), res.get("summary", "") or res.get("reason", ""))
        self.judge_subs(by_id)

    def go_dependents(self, rid: str) -> List[str]:
        return [r.id for r in load_rows(self.cfg, include_archived=False)[0]
                if rid in r.deps and str(r.meta.get("needs", "")).lower() == "go"]

    def outcome_crosscheck_wait(self, row: QRow) -> bool:
        """A CLOSED claim with no outcome (or `go`) from a row that a needs=go dependent names: the
        session may still be printing `ORCH_CLOSE <id> CLOSED:nogo` after an `orch done` without
        --outcome. Wait (True) until its pane is idle for 20 s (at most 180 s after the claim), then
        read the closing line; a negative outcome there wins and is written as line 3. Fail-safe: a
        claim and its own closing line that disagree never let a GO-only dependent run."""
        v, summary, mt = self.st.status(row.id)
        cur = self.st.claim_outcome(row.id)
        if v != "CLOSED" or negative(cur) or not self.go_dependents(row.id):
            return False
        d = self.st.dispatch(row.id)
        w = tmuxio.windows(self.session).get(row.id)
        if not d.get("open") or not w or not w["alive"] or d.get("ident") != w.ident:
            return False
        now = time.time()
        idle = now - w["activity"] if w["activity"] else 1e9
        if idle < 20 and now - mt < 180:
            return True
        pat = close_pattern(row.id, "row")
        for line in reversed(tmuxio.tail_lines(tmuxio.capture(self.session, row.id, 80), 80)):
            m = pat.match(line)
            if m and m.group(1) == "CLOSED" and not re.search(r"<[A-Za-z][^<>]*>", line):
                oc = norm_outcome(m.group(2) or "")
                if negative(oc) and oc != cur:
                    self.st.write_status(row.id, "CLOSED", summary, oc)
                    self.st.event(row.id, "outcome_from_closing_line", outcome=oc, claim_outcome=cur or None)
                    self.log(row.lane, f"{row.id}: the claim said outcome '{cur or 'none'}', its closing line says "
                                       f"{oc} — the negative outcome is recorded")
                break
        return False

    def judge_row(self, row: QRow) -> None:
        v, summary, mt = self.st.status(row.id)
        d = self.st.dispatch(row.id)
        attempt = int(d.get("attempt", 0) or 0)
        x = d.get("executor") or self.resolve_exec(row)[0]
        self.st.event(row.id, "claim", attempt=attempt, verdict=v, summary=summary[:300])
        self.log(row.lane, f"{row.id} claims {v} — {summary}")
        self.hook("on_claim", row=row.id, verdict=v, summary=summary)
        base = {"attempt": attempt, "verdict": v, "summary": summary, "status_mtime": mt,
                "executor": x, "claimed_at": time.strftime("%F %T"), "claimed_epoch": time.time()}
        if v == "CLOSED":
            base["outcome"] = self.st.claim_outcome(row.id)   # '' = none; dependents with needs=go read it
            self.st.write_result(row.id, dict(base, phase="verifying"))
            self.st.rm(f"{row.id}.judge_runs")
            out = self.cfg.state / f"{row.id}.check.json"
            out.unlink(missing_ok=True)
            self.start_job(f"judge:{row.id}", [sys.executable, str(LIB / "closepipe.py"), "judge", row.id],
                           self.cfg.state / f"{row.id}.check.log")
            return
        if v == "BLOCKED":
            import spend
            req = spend.wait_request(self.cfg, row.id, summary)
            if req is not None:      # `SPEND_WAIT <id>` from orch spend run: the money is the Owner's
                spend.on_wait(self, row, base, req)
                return
            if summary.startswith("launch failed") and self.health.on:
                n = self.st.read_int(f"{row.id}.fast_deaths", 0) + 1
                self.st.write_int(f"{row.id}.fast_deaths", n)
                opened = self.health.record_fast_death(x, row.id)
                if n == 1 or opened:
                    self.st.set_aside(row.id, f"launchfail{attempt}")
                    if not self.requeue(row.id, dict(base, phase="retry", status_mtime=0,
                                                     why=f"infra: {summary} — retried, not charged"),
                                        row.lane, f"infra: {summary}"):
                        return
                    self.log(row.lane, f"{row.id}: {summary} — infra suspect, retried once (not charged)")
                    return
            self.st.write_result(row.id, dict(base, phase="blocked_claim"))
            self.on_blocked(row, x, summary)
            return
        if v == "ROUTE_A":
            self.st.write_result(row.id, dict(base, phase="route_a"))
            self.st.event(row.id, "halt", phase="route_a", detail=summary)
            self.log(row.lane, f"HALTED {row.id}: ROUTE_A — {summary}")
            self.notify("critical", f"{self.cfg.name} — {row.id} ROUTE_A", self.page_body(row.id, row, summary),
                        row=row.id)
            self.hook("on_halt", row=row.id, phase="route_a", why=summary)
            return
        self.st.write_result(row.id, dict(base, phase="halted", why=f"unrecognized claim '{v}'"))
        self.notify("critical", f"{self.cfg.name} — {row.id} wrote an unrecognized claim", v)

    def collect_check(self, row: QRow) -> None:
        if self.job_running(f"judge:{row.id}"):
            return
        f = self.cfg.state / f"{row.id}.check.json"
        res = self.st.result(row.id)
        try:
            chk = json.loads(f.read_text())
        except Exception:
            chk = None
        if chk is None or abs(float(chk.get("status_mtime", -1)) - float(res.get("status_mtime", -2))) > 1e-6:
            # the judge died, never ran (engine restart), or judged an EARLIER claim: run it again
            runs = self.st.read_int(f"{row.id}.judge_runs", 0) + 1
            self.st.write_int(f"{row.id}.judge_runs", runs)
            if runs > 3:
                chk = {"ok": False, "checks": [{"name": "judge", "ok": False, "blocking": True,
                       "detail": f"the close-check job failed {runs - 1}x (state/{row.id}.check.log)"}]}
            else:
                f.unlink(missing_ok=True)
                self.start_job(f"judge:{row.id}", [sys.executable, str(LIB / "closepipe.py"), "judge", row.id],
                               self.cfg.state / f"{row.id}.check.log")
                return
        res.update(checks=chk.get("checks", []), commits=chk.get("commits", 0),
                   commit_lines=chk.get("commit_lines", []))
        attempt = res.get("attempt")
        if not chk.get("ok"):
            import closepipe
            why = closepipe.failures(chk)
            n = self.st.read_int(f"{row.id}.check_fails", 0) + 1
            self.st.write_int(f"{row.id}.check_fails", n)
            self.st.event(row.id, "check_failed", attempt=attempt, detail=why[:600], n=n)
            self.log(row.lane, f"{row.id} CLOSED claim REFUSED by the close checks ({n}): {why}")
            pol = (self.cfg.data.get("close") or {}).get("on_fail", "redispatch")
            maxf = int((self.cfg.data.get("close") or {}).get("max_fail", 2))
            self.reject_claim(row, f"refused{attempt}", "BLOCKED", f"close checks refused the CLOSED claim: {why}")
            _, _, mt = self.st.status(row.id)
            if pol == "halt" or n > maxf:
                res.update(phase="blocked", status_mtime=mt, why=f"close checks failed {n}x: {why}")
                self.st.write_result(row.id, res)
                self.halt(row.id, row, "blocked", f"close checks failed {n}x: {why}")
            elif pol == "unblock" and self.unblock_eligible(res.get("executor", "")):
                res.update(phase="blocked_claim", status_mtime=mt)
                self.st.write_result(row.id, res)
                self.on_blocked(row, res.get("executor", ""), f"close checks refused the CLOSED claim: {why}")
            else:
                self.st.set_aside(row.id, f"refused{attempt}b")
                self.requeue(row.id, dict(res, phase="retry", status_mtime=0,
                                          why=f"close checks refused the claim: {why}"),
                             row.lane, "close checks refused the claim")
            return
        self.st.rm(f"{row.id}.check_fails")
        if self.review_required(row, res.get("executor", "")):
            res.update(phase="reviewing")
            self.st.write_result(row.id, res)
            self.log(row.lane, f"{row.id} passed the close checks — independent review next")
        else:
            self.accept(row, res, "close checks passed; review not required")

    def accept(self, row: QRow, res: Dict[str, Any], why: str) -> None:
        res.update(phase="accepted", accepted_at=time.strftime("%F %T"), why=why)
        try:   # the row's protected-path base ends with its acceptance (a re-run starts a new one)
            import protected
            protected.clear_base(self.cfg, row.id)
        except Exception:
            pass
        self.st.write_result(row.id, res)
        self.st.event(row.id, "accept", attempt=res.get("attempt"), commits=res.get("commits"), detail=why)
        self.log(row.lane, f"{row.id} ACCEPTED — {res.get('summary', '')}")
        self.notify("low", f"{self.cfg.name}: {row.id} accepted", res.get("summary", ""))
        if negative(norm_outcome(res.get("outcome", ""))):
            # a measured negative verdict (NO-GO, HELD, FAIL): accepted as a result, but dependents
            # with needs=go stay held, and the Steward scan opens a negative_outcome incident
            held = [r.id for r in load_rows(self.cfg, include_archived=False)[0]
                    if row.id in r.deps and str(r.meta.get("needs", "")).lower() == "go"]
            self.st.event(row.id, "negative_outcome", outcome=res.get("outcome"), held=",".join(held) or None)
            self.log(row.lane, f"{row.id} closed with outcome {res.get('outcome')} — "
                               f"{'held (needs=go): ' + ','.join(held) if held else 'no needs=go dependents'}")
            self.notify("normal", f"{self.cfg.name}: {row.id} outcome {res.get('outcome')}",
                        f"{res.get('summary', '')}"[:400])
        self.hook("on_accept", row=row.id, lane=row.lane, summary=res.get("summary", ""))
        for n in ("unblock_n", "check_fails", "review_rejects", "review_deaths"):
            self.st.rm(f"{row.id}.{n}")
        self.after_accept(row, res)

    # -- review ---------------------------------------------------------------
    def drive_review(self, row: QRow, live: Dict[str, Dict[str, Any]]) -> None:
        res = self.st.result(row.id)
        attempt = int(res.get("attempt", 1) or 1)
        k = self.st.read_int(f"{row.id}.review_deaths", 0)
        rv = f"rv-{row.id}-{attempt}" + (f"r{k}" if k else "")
        if rv in live:
            return
        v, summary = self.sub_verdict(rv, float(res.get("claimed_epoch", 0) or 0))
        if v in ("ACCEPT", "REJECT"):
            self.consume(rv)
            self.on_review_verdict(row, res, rv, v, summary)
            return
        if v == "FORGED":
            self.st.set_aside(rv, "forged")
            self.st.event(row.id, "forged_verdict", session=rv)
            self.log(row.lane, f"{rv}: a verdict exists for a review the engine never dispatched — ignored, set aside")
            if self.st.once(f"forged-{rv}"):
                self.notify("critical", f"{self.cfg.name}: forged review verdict for {row.id}",
                            f"state/{rv}.status was written without a dispatched reviewer. Set aside; a real review runs.")
        elif v != "NONE":
            # a reviewer that could not run (launch failure) or wrote nonsense: retry once, then halt
            self.st.set_aside(rv, "bad")
            n = self.st.read_int(f"{row.id}.review_deaths", 0) + 1
            self.st.write_int(f"{row.id}.review_deaths", n)
            if n > 1:
                _, _, mt = self.st.status(row.id)
                res.update(phase="blocked", status_mtime=mt)
                self.st.write_result(row.id, res)
                self.halt(row.id, row, "blocked", f"no reviewer could run ({v}: {summary}) — fix review config, then orch reset {row.id}")
                return
            return
        if self.stopped(row.lane):
            return
        if not self.capacity_ok(self.reviewer_for(row, res.get("executor", "")), row=row.id)[0]:
            self.sched_notes[rv] = "review waiting for capacity"
            return
        self.start_review(row, res, rv)

    def start_review(self, row: QRow, res: Dict[str, Any], rv: str) -> None:
        reviewer = self.reviewer_for(row, res.get("executor", ""))
        d = self.st.dispatch(row.id)
        head = d.get("head", "")
        allow = " ".join(shlex.quote(p) for p in __import__("closepipe").allowlist(self.cfg, row.id))
        diffcmd = (f"git -C {shlex.quote(str(self.cfg.git_repo))} log --stat {head}..HEAD -- {allow}"
                   if head else "(no dispatch head recorded — inspect the row folder and report pack)")
        checks = "; ".join(f"{c['name']}: {'ok' if c['ok'] else 'FAIL'} — {c['detail']}"
                           for c in res.get("checks", []))
        order = te.write_review(self.cfg, row.id, row.lane, rv, reviewer, res.get("executor", ""),
                                row.plan, res.get("summary", ""), checks, diffcmd, int(res.get("attempt", 1)))
        ok, how = self.dispatch_sub(rv, reviewer, order, row.lane, row.id, {"reviewer_of": row.id})
        if not ok:
            self.sched_notes[rv] = f"review not dispatched: {how}"

    def on_review_verdict(self, row: QRow, res: Dict[str, Any], rv: str, v: str, summary: str) -> None:
        reviewfile = self.cfg.instance / "reviews" / f"{rv[3:]}.md"
        rel = te.rel_to_root(self.cfg, reviewfile)
        res["review"] = {"session": rv, "verdict": v, "summary": summary, "file": rel,
                         "reviewer": self.st.dispatch(rv).get("executor", "")}
        self.st.event(row.id, "review_verdict", attempt=res.get("attempt"), session=rv, verdict=v,
                      summary=summary[:300], file=rel)
        if v == "ACCEPT":
            self.accept(row, res, f"review {rv}: {summary}")
            return
        n = self.st.read_int(f"{row.id}.review_rejects", 0) + 1
        self.st.write_int(f"{row.id}.review_rejects", n)
        rc = self.cfg.data.get("review") or {}
        self.log(row.lane, f"{row.id} REJECTED by {rv} ({n}): {summary}")
        self.reject_claim(row, f"rejected{res.get('attempt')}", "BLOCKED", f"review {rv} rejected the work: {summary} (findings: {rel})")
        _, _, mt = self.st.status(row.id)
        if n > int(rc.get("max_rejects", 2)):
            res.update(phase="blocked", status_mtime=mt)
            self.st.write_result(row.id, res)
            self.halt(row.id, row, "blocked", f"rejected by review {n}x — last: {summary} ({rel})")
            return
        if rc.get("on_reject", "redispatch") == "unblock" and self.unblock_eligible(res.get("executor", "")):
            res.update(phase="blocked_claim", status_mtime=mt)
            self.st.write_result(row.id, res)
            self.on_blocked(row, res.get("executor", ""), f"review rejected: {summary} (findings: {rel})")
            return
        self.st.set_aside(row.id, f"rejected{res.get('attempt')}b")
        if not self.requeue(row.id, dict(res, phase="retry", status_mtime=0, why=f"review rejected: {summary}"),
                            row.lane, "review rejected the claim"):
            return
        self.notify("normal", f"{self.cfg.name}: {row.id} rejected by review — re-dispatching", f"{summary} ({rel})")

    # -- blocked rows and unblockers -------------------------------------------
    def on_blocked(self, row: QRow, executor: str, reason: str) -> None:
        if not self.unblock_eligible(executor) or self.st.has(f"{row.id}.retiered"):
            why = ("retiered row blocked again" if self.st.has(f"{row.id}.retiered")
                   else f"{executor} rows are not auto-unblocked")
            self.halt(row.id, row, "blocked", f"BLOCKED — {reason} ({why}; see BLOCK_SHEET.md)")
            return
        n = self.st.read_int(f"{row.id}.unblock_n", 0) + 1
        mx = int(self.cfg.data["executors"]["max_unblock_attempts"])
        if n > mx:
            if self.parked(row.id):
                self.log(row.lane, f"{row.id} BLOCKED past max unblock — NOT retiered: the row is parked")
                return
            target = self.exec_policy(row.lane)["retier_on_max_unblock"]     # the round's, else the rig's
            if not self.cfg.decides(target):
                self.halt(row.id, row, "blocked", f"BLOCKED — {reason} (max unblock; retier target {target} is a "
                                                  f"junior tier, so nobody may take the row over; see BLOCK_SHEET.md)")
                return
            self.ctl(f"EXECUTOR_{row.id}").write_text(f"{target}\n")
            self.st.touch(f"{row.id}.retiered")
            self.st.set_aside(row.id, f"blocked{n}")
            self.st.rm(f"{row.id}.unblock_n")
            self.st.write_result(row.id, {"phase": "retry", "status_mtime": 0, "why": f"max unblock — retiered to {target}"})
            self.st.event(row.id, "retier", to=target, detail=f"BLOCKED after {mx} unblock attempts: {reason[:200]}")
            self.log(row.lane, f"{row.id} BLOCKED after {mx} unblock attempts — retiered to {target} (one shot)")
            self.notify("normal", f"{self.cfg.name}: {row.id} max-unblock — retiering to {target}", reason)
            return
        self.st.write_int(f"{row.id}.unblock_n", n)
        self.sheet_open(row.id, n, reason)
        res = self.st.result(row.id)
        res.update(phase="unblocking", unblock_n=n, reason=reason, unblock_epoch=time.time())
        self.st.write_result(row.id, res)
        self.notify("low", f"{self.cfg.name}: {row.id} blocked — auto-unblocker running", reason)

    def drive_unblock(self, row: QRow, live: Dict[str, Dict[str, Any]]) -> None:
        res = self.st.result(row.id)
        n = int(res.get("unblock_n", self.st.read_int(f"{row.id}.unblock_n", 1)) or 1)
        ub = f"ub-{row.id}-{n}"
        if ub in live:
            return
        v, summary = self.sub_verdict(ub, float(res.get("unblock_epoch", 0) or 0))
        if v in ("RESOLVED", "ESCALATE"):
            self.consume(ub)
            self.on_unblock_verdict(row, res, ub, n, v, summary)
            return
        if v == "FORGED":
            self.st.set_aside(ub, "forged")
            self.st.event(row.id, "forged_verdict", session=ub)
            self.log(row.lane, f"{ub}: a verdict exists for an unblocker the engine never dispatched — ignored")
        elif v != "NONE":
            self.st.set_aside(ub, "bad")
        d = self.st.dispatch(ub)
        if d.get("ended") and float(d.get("epoch", 0) or 0) + 1 >= float(res.get("unblock_epoch", 0) or 0):
            _, _, mt = self.st.status(row.id)
            res.update(phase="blocked", status_mtime=mt)
            self.st.write_result(row.id, res)
            self.halt(row.id, row, "blocked", f"{ub} ended without a usable verdict — inspect, then orch reset {row.id}")
            return
        if self.stopped(row.lane):
            return
        ubx = res.get("answer_exec") or te.unblock_executor(self.cfg, n, lane=row.lane)
        if not self.capacity_ok(ubx, row=row.id)[0]:
            self.sched_notes[ub] = "unblocker waiting for capacity"
            return
        x = res.get("executor") or self.resolve_exec(row)[0]
        order = te.write_unblock(self.cfg, row.id, row.lane, n, x, row.plan, res.get("reason", ""), ubx)
        self.dispatch_sub(ub, ubx, order, row.lane, row.id, {"unblock_of": row.id, "n": n})

    def on_unblock_verdict(self, row: QRow, res: Dict[str, Any], ub: str, n: int, v: str, summary: str) -> None:
        f = te.rel_to_root(self.cfg, self.cfg.unblock / f"{row.id}-{n}.md")
        self.st.event(row.id, "unblock_verdict", attempt=res.get("attempt"), session=ub, verdict=v,
                      summary=summary[:300], file=f)
        self.log(row.lane, f"{ub} returned {v} — {summary}")
        if v == "RESOLVED":
            if self.parked(row.id):     # parked while its unblocker ran: the row waits for its unpark
                self.st.event(row.id, "held_parked", detail=f"{ub} RESOLVED — not re-dispatched: the row is parked")
                self.log(row.lane, f"{row.id}: {ub} RESOLVED — NOT re-dispatched: the row is parked")
                return
            tmuxio.kill(self.session, row.id)
            self.st.set_aside(row.id, f"blocked{n}")
            if not self.requeue(row.id, {"phase": "retry", "status_mtime": 0, "why": f"{ub} RESOLVED: {summary}"},
                                row.lane, f"{ub} RESOLVED"):
                return
            self.notify("low", f"{self.cfg.name}: {row.id} unblocked (attempt {n})", summary)
            return
        if not res.get("answer") and self.ladder_up(row, res, ub, n, summary, f):
            return
        _, _, mt = self.st.status(row.id)
        res.update(phase="blocked", status_mtime=mt)
        self.st.write_result(row.id, res)
        self.halt(row.id, row, "blocked", f"{ub} ESCALATED: {summary} (analysis {f})")

    def ladder_up(self, row: QRow, res: Dict[str, Any], ub: str, n: int, summary: str, analysis: str) -> bool:
        """executors.unblock_ladder: an ESCALATE from unblocker n goes to the next, different
        tier of unblock_executors (within max_unblock_attempts) before the row halts and pages."""
        ex = self.cfg.data["executors"]
        nxt = n + 1
        if not ex.get("unblock_ladder") or nxt > int(ex["max_unblock_attempts"]):
            return False
        tier, up = self.st.dispatch(ub).get("executor", ""), te.unblock_executor(self.cfg, nxt, lane=row.lane)
        if not up or up == tier:
            return False
        note = f"unblocker {ub} ({tier}) ESCALATED: {summary} (its analysis: {analysis})"
        self.st.write_int(f"{row.id}.unblock_n", nxt)
        self.sheet_open(row.id, nxt, f"ladder — {note}")
        res.update(phase="unblocking", unblock_n=nxt, unblock_epoch=time.time(),
                   reason=f"{res.get('reason', '')} — LADDER: {note}; you are the next tier ({up})")
        self.st.write_result(row.id, res)
        self.st.event(row.id, "unblock_ladder", session=ub, frm=tier, to=up, summary=summary[:300])
        self.log(row.lane, f"{ub} ({tier}) ESCALATED — laddering to {up} as ub-{row.id}-{nxt} before paging")
        self.notify("normal", f"{self.cfg.name}: {row.id} escalated by {tier} — {up} unblocker next", summary)
        return True

    def sheet_open(self, rid: str, n: int, reason: str) -> None:
        sheet = self.cfg.instance / "BLOCK_SHEET.md"
        with open(sheet, "a+") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            f.seek(0)
            text = f.read()
            if not text.strip():
                f.write("# BLOCK SHEET — every row stop and how it was resolved (appended by the engine and the unblockers)\n\n"
                        "Read newest-last. `OPEN` rows are being worked by an unblocker session (`ub-<row>-<n>`);\n"
                        "`ESCALATED` rows need the operator. Full analysis per stop: `unblock/<row>-<n>.md`.\n\n"
                        "| # | when | row | attempt | why it stopped (the executor's own line) | options weighed | chosen + why | what changed | outcome |\n"
                        "|---|------|-----|---------|------------------------------------------|-----------------|--------------|--------------|---------|\n")
                text = "x"
            k = len(re.findall(r"^\| \d+ ", text, re.M)) + 1
            why = " ".join(reason.replace("|", "/").split())[:220]
            f.write(f"| {k} | {time.strftime('%F %H:%M')} | {rid} | {n} | {why} | _unblocker running_ | "
                    f"_unblocker running_ | _unblocker running_ | OPEN |\n")
            fcntl.flock(f, fcntl.LOCK_UN)

    def judge_subs(self, by_id: Dict[str, QRow]) -> None:
        """Plan-review sessions: record their verdicts (rows' sub-sessions are judged
        from the row's own phase)."""
        for pat, label in (("pf-*.dispatch.json", "plan review"), ("rr-*.dispatch.json", "round review")):
            for f in self.cfg.state.glob(pat):
                sid = f.name[: -len(".dispatch.json")]
                v, summary, mt = self.st.status(sid)
                if v in ("PASS", "FIX", "DONE", "FAILED") and self.st.once(f"sub-verdict-{sid}-{int(mt)}"):
                    self.st.event(sid, f"{kind_of(sid)}_verdict", verdict=v, summary=summary[:300])
                    self.log("", f"{sid} {label}: {v} — {summary}")
                    self.notify("normal", f"{self.cfg.name}: {label} {sid} {v}", summary)

    # -- scheduling -----------------------------------------------------------
    def capacity_ok(self, executor: str, steward: bool = False, row: str = "") -> Tuple[bool, str]:
        s = self.cfg.data.get("scheduler") or {}
        blk, why = self.health.blocked(executor)
        if blk:
            return False, why
        import spend
        blk, why = spend.hold_blocks(self.cfg, executor, row)     # the OpenCode spend hold (kind spend)
        if blk:
            return False, why
        if not steward and executor in getattr(self, "steward_wants", set()):
            return False, f"a Steward session is waiting for a {executor} slot (it goes first)"
        need_disk = int(s.get("min_free_disk_mb", 0) or 0)
        if need_disk:
            import shutil as _sh
            try:
                free = _sh.disk_usage(str(self.cfg.root)).free // (1024 * 1024)
            except Exception:
                free = need_disk
            if free < need_disk:
                return False, f"free disk {free} MB < scheduler.min_free_disk_mb {need_disk} MB"
        wins = tmuxio.windows(self.session)
        n, by = self.live_count(wins)
        mx = int(s.get("max_parallel", 8) or 8)
        if n >= mx:
            return False, f"{n}/{mx} sessions live (scheduler.max_parallel)"
        cap = (self.cfg.data["executors"].get("max_parallel") or {}).get(executor)
        if cap and by.get(executor, 0) >= int(cap):
            return False, f"{by.get(executor, 0)}/{cap} {executor} sessions live"
        if row and not steward:
            blk = self.round_capacity(executor, row)
            if blk:
                return False, blk
        need = int(s.get("min_free_mb", 0) or 0)
        have = mem_available_mb()
        if need and have < need:
            return False, f"MemAvailable {have} MB < scheduler.min_free_mb {need} MB"
        return True, ""

    def round_capacity(self, executor: str, row: str) -> str:
        """round.yaml executors.max_parallel (drop D): '' while the row's round has fewer live sessions (its rows and
        their sub-sessions) on `executor` than its cap; the rig's caps apply as well, so a round only narrows them."""
        from rows import find_row
        r = find_row(self.cfg, row)
        if r is None:
            return ""
        ex = self.exec_policy(r.lane)
        cap = (ex.get("round_max_parallel") or {}).get(executor)
        if cap is None:
            return ""
        import rounds
        lanes = set(rounds.round_lanes(self.cfg, ex.get("round", "")))
        n = sum(1 for d in self.inflight().values() if d.get("executor") == executor and d.get("lane") in lanes)
        if n >= int(cap):
            return f"{n}/{cap} {executor} sessions live in round {ex.get('round')} (its executors.max_parallel)"
        return ""

    def schedule(self) -> None:
        self.sched_notes = {k: v for k, v in self.sched_notes.items() if not k.startswith(("rv-", "ub-"))}
        rows, by_id = load_rows(self.cfg)
        implicit = implicit_deps(self.cfg, rows)
        live = self.inflight()
        wins = tmuxio.windows(self.session)
        order = {l: i for i, l in enumerate(active_lanes(self.cfg))}
        ready: List[QRow] = []
        notes: Dict[str, str] = {}
        try:
            import rounds as rmod
        except Exception:
            rmod = None
        import phases
        try:
            fence = phases.Fence(self.cfg)       # the phase fence: no phases file, no fence (v2 rigs)
        except Exception as e:                   # fail closed, never open: every row waits, the rest goes on
            fence = phases.Fence.failed(self.cfg, f"{type(e).__name__}: {e}")
            self.st.engine_log(f"phases: the fence could not be built — {type(e).__name__}: {e}")
        for r in rows:
            if r.archived:
                continue
            if r.problems:
                notes[r.id] = "; ".join(r.problems)
                continue
            ph = self.st.phase(r.id)
            if ph not in DISPATCHABLE or r.id in live:
                if ph != "spend_wait":           # spend.tick owns that note; any other is stale once the row moved on
                    self.sched_notes.pop(r.id, None)
                continue
            w = wins.get(r.id)
            if w and w["alive"]:
                d = self.st.dispatch(r.id)
                if d.get("ident") == w.ident and not d.get("open"):
                    tmuxio.kill(self.session, w["wid"])   # our own finished session: clear it for the retry
                else:
                    notes[r.id] = "a live window wears this row's name (orphan) — close it or orch adopt"
                    continue
            if not self.lane_enabled(r.lane):
                notes[r.id] = f"lane {r.lane} not started (orch start {r.lane})"
                continue
            if self.stopped(r.lane):
                notes[r.id] = "STOP"
                continue
            blk = fence.block(r.lane, ph, new_start=not self.st.dispatch(r.id))
            if blk:
                notes[r.id] = blk
                continue
            unmet = unmet_deps(self.st, r, implicit)
            if unmet:
                neg = [u for u in unmet if ":" in u]
                notes[r.id] = f"waiting on {','.join(unmet[:6])}" + (
                    f" (needs=go: {','.join(neg[:6])} closed with a negative outcome)" if neg else "")
                continue
            if self.paused(r.lane):
                notes[r.id] = "PAUSED"
                continue
            if r.gate:
                ok, why = self.gate_open(r)
                if not ok:
                    notes[r.id] = why
                    if self.st.once(f"gate-{r.id}-{why[:20]}"):
                        self.log(r.lane, f"{r.id}: {why}")
                        self.notify("normal", f"{self.cfg.name}: {r.id} at its gate", why)
                    continue
            if rmod is not None:
                pf = rmod.preflight_block(self.cfg, self.st, r.lane)
                if pf:
                    notes[r.id] = pf
                    continue
            ready.append(r)
        ready.sort(key=lambda r: (-r.prio, order.get(r.lane, 999), r.index))
        for r in ready:
            x, prob = self.resolve_exec(r)
            if prob:
                notes[r.id] = prob
                if self.st.once(f"exec-{r.id}-{prob[:30]}"):
                    self.notify("normal", f"{self.cfg.name}: {r.id} cannot dispatch", prob)
                continue
            if r.gate and x != "manual" and not self.gate_probe_ok(r):
                notes[r.id] = f"pre-dispatch probe failing (state/{r.id}.probe.out)"
                continue
            new_start = not self.st.dispatch(r.id)
            if new_start and fence.on:
                blk = fence.at_rows_cap(r.lane)  # the rows cap holds within this pass too (ready rows all passed it)
                if blk:
                    notes[r.id] = blk
                    continue
            if x != "manual":
                ok, why = self.capacity_ok(x, row=r.id)
                if not ok:
                    notes[r.id] = f"READY — {why}"
                    continue
            newly: List[str] = []
            if r.resources:
                ok, newly, why = self.leases.acquire_all(r.id, r.resources, row_running=self.lease_row_running)
                if not ok:
                    notes[r.id] = f"READY — {why}"
                    continue
            ok, how = self.dispatch_row(r, x)
            if not ok:
                if newly:
                    self.leases.release(r.id, newly)
                notes[r.id] = how
                if self.st.once(f"dispatch-fail-{r.id}-{how[:30]}"):
                    self.log(r.lane, f"{r.id} NOT dispatched: {how}")
                    self.notify("normal", f"{self.cfg.name}: {r.id} not dispatched", how)
                continue
            if new_start and fence.on:
                fence.note_start(r.lane)
            notes.pop(r.id, None)
        self.sched_notes.update(notes)

    def lease_row_running(self, owner: str, inst: str) -> Optional[bool]:
        """Stale-span test for leases.acquire_all: a lease owned by one of OUR rows is
        stale when that row has no open session record."""
        if inst and inst != str(self.cfg.instance):
            return None
        d = self.st.dispatch(owner)
        if not d:
            return None
        return bool(d.get("open"))

    def gate_probe_ok(self, row: QRow) -> bool:
        """gate_probes.tsv (v1): run asynchronously; dispatch only on a recorded pass
        for the current plan."""
        f = self.cfg.instance / "gate_probes.tsv"
        if not f.is_file():
            return True
        cmd = ""
        bylane = ""
        for line in f.read_text().splitlines():
            if not line.strip() or line.startswith("#") or "\t" not in line:
                continue
            key, c = line.split("\t", 1)
            if key == row.id:
                cmd = c
            elif key == row.lane:
                bylane = c
        cmd = cmd or bylane
        if not cmd:
            return True
        passed = self.cfg.state / f"{row.id}.probe_passed"
        if passed.exists() and passed.read_text().strip() == plan_sha(self.cfg, row):
            passed.unlink()
            return True
        if not self.job_running(f"probe:{row.id}"):
            self.start_job(f"probe:{row.id}", ["bash", "-c", f"timeout 120 bash -c {shlex.quote(cmd)}"],
                           self.cfg.state / f"{row.id}.probe.out")
        return False

    def on_probe_done(self, rid: str, rc: int) -> None:
        rows, by_id = load_rows(self.cfg)
        row = by_id.get(rid)
        if rc == 0 and row:
            (self.cfg.state / f"{rid}.probe_passed").write_text(plan_sha(self.cfg, row))
            self.log(row.lane, f"{rid} pre-dispatch probe passed")
        elif row and self.st.once(f"probe-fail-{rid}"):
            self.log(row.lane, f"{rid} pre-dispatch probe FAILED (exit {rc}) — not dispatched")
            self.notify("normal", f"{self.cfg.name}: {rid} probe failed",
                        f"exit {rc}; output state/{rid}.probe.out — the engine retries every tick")

    # -- reaper -----------------------------------------------------------------
    def reap(self) -> None:
        rc = self.cfg.data.get("reaper") or {}
        if not rc.get("enabled", True):
            return
        grace = int(rc.get("closed_grace_secs", 300))
        hgrace = int(rc.get("halted_grace_secs", 0) or 0)
        now = time.time()
        live = self.inflight()
        for name, w in tmuxio.windows(self.session).items():
            if name in SERVICE_WINDOWS or name.startswith("watch-"):
                continue
            d = self.st.dispatch(name)
            if not w["alive"]:
                if name not in live and now - float(d.get("ended_epoch", 0) or 0) > 30:
                    tmuxio.kill(self.session, w["wid"])
                continue
            if name in live or not d:
                continue
            if d.get("ident") and w.ident != d.get("ident"):
                continue  # not the session we dispatched — never ours to kill
            ended = float(d.get("ended_epoch", 0) or 0)
            kind = kind_of(name)
            ph = self.st.phase(name) if kind == "row" else ""
            if kind != "row" or ph in ("reviewing", "accepted", "verifying", "retry"):
                if ended and now - ended > grace:
                    if tmuxio.kill(self.session, w["wid"]):
                        self.st.event(parent_of(name) or name, "reap", session=name)
            elif ph in HALTED_PHASES and hgrace and ended and now - ended > hgrace:
                if tmuxio.kill(self.session, w["wid"]):
                    self.st.event(name, "reap", session=name, halted=True)

    # -- after accept: progress signal, goal ------------------------------------
    def after_accept(self, row: QRow, res: Dict[str, Any]) -> None:
        pc = self.cfg.data.get("progress") or {}
        if int(pc.get("no_change_after", 0) or 0):
            noop = (int(res.get("commits", 0) or 0) == 0 and
                    re.search(pc.get("noop_pattern", r"$^"), res.get("summary", ""), re.I) is not None)
            streak = self.st.read_int("progress.noop_streak", 0)
            streak = streak + 1 if noop else 0
            self.st.write_int("progress.noop_streak", streak)
            if streak and streak % int(pc["no_change_after"]) == 0:
                msg = (f"{streak} accepted rows in a row changed nothing (no commits and a no-op summary). "
                       f"Latest: {row.id} — {res.get('summary', '')[:160]}")
                self.st.event(row.id, "no_progress", streak=streak)
                self.log(row.lane, f"NO PROGRESS: {msg}")
                if pc.get("action") == "pause":
                    self.ctl("PAUSE").write_text("no-progress streak\n")
                self.notify("critical", f"{self.cfg.name}: no progress", msg)
        try:
            import rounds as rmod
            g = rmod.goal_for_lane(self.cfg, row.lane)
        except Exception:
            g = None
        if g and g.get("metric_cmd") and g.get("run_on", "accept") == "accept":
            self.run_metric(row, g)

    def run_metric(self, row: QRow, g: Dict[str, Any]) -> None:
        key = f"metric:{row.id}|{row.lane}"
        if any(k.startswith("metric:") for k in self.jobs):
            self.st.touch("metric.pending", f"{row.id}|{row.lane}\n")
            return
        self.start_job(key, ["bash", "-c", g["metric_cmd"]], self.cfg.state / "metric.out")

    def on_metric_done(self, ident: str, rc: int) -> None:
        rid, _, lane = ident.partition("|")
        try:
            import rounds as rmod
            rmod.record_metric(self.cfg, self.st, rid, lane, rc, (self.cfg.state / "metric.out").read_text(errors="replace"),
                               self)
        except Exception:
            self.st.engine_log(f"metric record failed:\n{traceback.format_exc()}")
        if self.st.has("metric.pending"):
            nxt = (self.cfg.state / "metric.pending").read_text().strip()
            self.st.rm("metric.pending")
            r2, _, l2 = nxt.partition("|")
            rows, by_id = load_rows(self.cfg)
            if r2 in by_id:
                import rounds as rmod
                g = rmod.goal_for_lane(self.cfg, l2)
                if g:
                    self.run_metric(by_id[r2], g)

    # -- lanes complete, periodic checks ----------------------------------------
    def lane_completion(self) -> None:
        rows, _ = load_rows(self.cfg, include_archived=False)
        by_lane: Dict[str, List[QRow]] = {}
        for r in rows:
            by_lane.setdefault(r.lane, []).append(r)
        for lane, rs in by_lane.items():
            if rs and all(self.st.done(r.id) for r in rs):
                if self.st.once(f"lane-complete-{lane}-{len(rs)}"):
                    self.log(lane, "LANE COMPLETE — every row accepted")
                    self.notify("normal", f"{self.cfg.name} lane {lane} COMPLETE", f"{len(rs)} rows accepted.")

    def periodic_checks(self) -> None:
        checks = (self.cfg.data.get("watchdog") or {}).get("checks") or []
        now = time.time()
        for i, c in enumerate(checks):
            if not isinstance(c, dict) or not c.get("cmd"):
                continue
            every = int(c.get("every", 300))
            if now - self.last_checks.get(i, 0) < every or self.job_running(f"check:{i}"):
                continue
            self.last_checks[i] = now
            self.start_job(f"check:{i}", ["bash", "-c", f"timeout 120 bash -c {shlex.quote(c['cmd'])}"],
                           self.cfg.state / f"check_{i}.out")

    def on_periodic_done(self, i: int, rc: int) -> None:
        checks = (self.cfg.data.get("watchdog") or {}).get("checks") or []
        if i >= len(checks):
            return
        c = checks[i]
        title = c.get("title") or f"check {i}"
        key = f"periodic-{i}"
        if rc == 0:
            self.st.forget(key)
            return
        if self.st.once(key):
            try:
                first = (self.cfg.state / f"check_{i}.out").read_text(errors="replace").strip().splitlines()[:1]
            except Exception:
                first = []
            self.notify("normal", f"{self.cfg.name}: {title}", (first[0] if first else f"exit {rc}")[:300])


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "tick":
        e = Engine()
        e.st.ensure_adopted()
        e.tick()
        # let fast jobs finish so a single tick is observable in tests
        deadline = time.time() + float(os.environ.get("ORCH_TICK_WAIT", "0"))
        while e.jobs and time.time() < deadline:
            time.sleep(0.2)
            e.poll_jobs()
        return 0
    return Engine().run(wait_lock="--wait-lock" in sys.argv[1:])


if __name__ == "__main__":
    sys.exit(main())
