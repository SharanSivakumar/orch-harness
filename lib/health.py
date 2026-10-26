#!/usr/bin/env python3
"""Executor health (v3): a circuit breaker per executor, probes, remedies.

Why: when a CLI starts failing, windows die within seconds of dispatch and every
one used to be retiered to the top tier, one row at a time — the cause was the CLI,
not the rows. A whole tier could be silently lost while rows kept being charged.

Rules:

  * A session that dies with NO claim within `health.fast_death_secs` of dispatch is an
    infra suspect. The engine retries the row on the same executor and charges it
    nothing (no retier, no unblock, no halt). A row that dies fast a second time while
    the breaker stays closed is the row's own problem: the normal dead-window policy
    applies.
  * `health.threshold` DISTINCT sessions of one executor dying fast inside
    `health.window_secs` OPEN that executor's breaker: nothing new dispatches to it,
    its `health.remedies` run (e.g. kill a wedged `opencode serve --service`), and:
      - with a `health.probes.<executor>` command: the probe re-runs every
        `probe_every` seconds; exit 0 closes the breaker;
      - without one: after `open_secs` (doubling per re-open, max 1 h) the breaker
        goes HALF-open and lets ONE dispatch through as the trial; it closes once that
        session survives `fast_death_secs`, and re-opens if it dies fast.
  * A pane tail matching `health.auth_patterns.<family>` (a login prompt) opens the
    breaker at once with reason "auth" — a login is the Owner's, so a Steward incident
    records it for the inbox — and the row goes back to the queue unchanged.
  * Probes also run once when the engine starts (an adapter/CLI self-test), so a CLI
    flag change is caught before any row dies of it.
  * Open longer than `health.steward_after` -> a Steward incident (when the Steward
    lane is on), once per opening.

State: state/health.json (engine-written only).
"""
from __future__ import annotations

import json
import os
import re
import shlex
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))


class Health:
    def __init__(self, rig):
        self.rig = rig

    # -- config and state -------------------------------------------------------------
    @property
    def c(self) -> Dict[str, Any]:
        return self.rig.cfg.data.get("health") or {}

    @property
    def on(self) -> bool:
        return bool(self.c.get("enabled", True))

    def _path(self) -> Path:
        return self.rig.cfg.state / "health.json"

    def load(self) -> Dict[str, Dict[str, Any]]:
        try:
            return json.loads(self._path().read_text())
        except Exception:
            return {}

    def save(self, data: Dict[str, Dict[str, Any]]) -> None:
        p = self._path()
        tmp = p.with_name(f".{p.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(data, indent=1, sort_keys=True))
        os.replace(tmp, p)

    def entry(self, data: Dict[str, Dict[str, Any]], x: str) -> Dict[str, Any]:
        return data.setdefault(x, {"state": "closed", "deaths": [], "opens": 0})

    def _probe_cmd(self, x: str) -> str:
        return str(self.rig.cfg.pick(self.c.get("probes") or {}, x, ""))

    def _remedies(self, x: str) -> List[str]:
        v = self.rig.cfg.pick(self.c.get("remedies") or {}, x, [], present=True) or []     # itself, its variant base, its family
        return [v] if isinstance(v, str) else [str(r) for r in v]

    # -- questions the scheduler asks -------------------------------------------------
    def is_fast(self, d: Dict[str, Any], now: Optional[float] = None) -> bool:
        if not self.on:
            return False
        now = now or time.time()
        return now - float(d.get("epoch", now) or now) <= float(self.c.get("fast_death_secs", 60))

    def blocked(self, x: str) -> Tuple[bool, str]:
        """(True, why) while nothing may dispatch to executor x."""
        if not self.on or not x:
            return False, ""
        e = self.load().get(x)
        if not e or e.get("state", "closed") == "closed":
            return False, ""
        if e["state"] == "half" and not e.get("trial"):
            return False, ""     # exactly one trial dispatch goes through
        return True, f"executor {x} breaker {e['state'].upper()} — {e.get('reason', '')}"

    def state_of(self, x: str) -> str:
        return (self.load().get(x) or {}).get("state", "closed")

    # -- events -------------------------------------------------------------------------
    def on_dispatch(self, x: str, sid: str) -> None:
        if not self.on:
            return
        data = self.load()
        e = data.get(x)
        if e and e.get("state") == "half" and not e.get("trial"):
            e["trial"] = sid
            e["trial_epoch"] = int(time.time())
            self.save(data)
            self.rig.st.engine_log(f"health: {x} half-open — {sid} is the trial dispatch")

    def on_survive(self, x: str, sid: str) -> None:
        """A live session passed fast_death_secs: a half-open breaker whose trial it is closes."""
        if not self.on:
            return
        e = self.load().get(x)
        if e and e.get("state") == "half" and e.get("trial") == sid:
            self.close(x, f"trial session {sid} survived {self.c.get('fast_death_secs', 60)}s")

    def record_fast_death(self, x: str, sid: str) -> bool:
        """-> True when this death OPENS (or re-opens) the breaker."""
        if not self.on or not x:
            return False
        now = int(time.time())
        data = self.load()
        e = self.entry(data, x)
        win = float(self.c.get("window_secs", 1800))
        e["deaths"] = [d for d in e.get("deaths", []) if now - d[0] <= win] + [[now, sid]]
        if e.get("state") == "half" and e.get("trial") == sid:
            self.save(data)
            self.open(x, f"trial session {sid} died within {self.c.get('fast_death_secs', 60)}s again")
            return True
        distinct = {d[1] for d in e["deaths"]}
        self.save(data)
        if e.get("state", "closed") == "closed" and len(distinct) >= int(self.c.get("threshold", 2)):
            self.open(x, f"{len(distinct)} sessions ({', '.join(sorted(distinct)[:4])}) died with no claim "
                         f"within {self.c.get('fast_death_secs', 60)}s of dispatch")
            return True
        return False

    def open(self, x: str, reason: str, kind: str = "fast_deaths") -> None:
        data = self.load()
        e = self.entry(data, x)
        was = e.get("state", "closed")
        e.update(state="open", since=int(time.time()), reason=reason, kind=kind, trial="",
                 opens=int(e.get("opens", 0)) + 1, next_probe=int(time.time()) + 5, remedied=0,
                 steward_opened=False)
        self.save(data)
        self.rig.st.event(f"health:{x}", "breaker_open", executor=x, reason=reason[:300], was=was)
        self.rig.st.engine_log(f"health: {x} breaker OPEN — {reason}")
        self.rig.notify("normal", f"{self.rig.cfg.name}: executor {x} breaker open",
                        f"{reason}. Nothing dispatches to {x} until it is healthy again; rows are not charged.")
        self.remedy(x)

    def close(self, x: str, why: str) -> None:
        data = self.load()
        e = self.entry(data, x)
        if e.get("state", "closed") == "closed":
            return
        e.update(state="closed", closed_at=int(time.time()), trial="", deaths=[], reason="")
        self.save(data)
        self.rig.st.event(f"health:{x}", "breaker_close", executor=x, why=why[:300])
        self.rig.st.engine_log(f"health: {x} breaker CLOSED — {why}")
        self.rig.notify("low", f"{self.rig.cfg.name}: executor {x} healthy again", why)

    def remedy(self, x: str) -> None:
        data = self.load()
        e = self.entry(data, x)
        if int(e.get("remedied", 0)) >= 3:
            return
        cmds = self._remedies(x)
        if not cmds:
            return
        e["remedied"] = int(e.get("remedied", 0)) + 1
        self.save(data)
        script = " ; ".join(f"( {c} )" for c in cmds)
        self.rig.start_job(f"remedy:{x}", ["bash", "-c", f"timeout 120 bash -c {shlex.quote(script)}"],
                           self.rig.cfg.state / f"health_remedy_{x}.out")
        self.rig.st.event(f"health:{x}", "remedy", executor=x, n=e["remedied"])
        self.rig.st.engine_log(f"health: remedies for {x} started ({len(cmds)} command(s))")

    def probe(self, x: str) -> bool:
        cmd = self._probe_cmd(x)
        if not cmd or self.rig.job_running(f"health:{x}"):
            return False
        t = int(self.c.get("probe_timeout", 120))
        self.rig.start_job(f"health:{x}", ["bash", "-c", f"timeout {t} bash -c {shlex.quote(cmd)}"],
                           self.rig.cfg.state / f"health_probe_{x}.out")
        return True

    def on_probe_done(self, x: str, rc: int) -> None:
        data = self.load()
        e = self.entry(data, x)
        e["last_probe"] = int(time.time())
        e["last_probe_rc"] = rc
        st = e.get("state", "closed")
        self.save(data)
        self.rig.st.event(f"health:{x}", "probe", executor=x, rc=rc, state=st)
        if rc == 0:
            if st != "closed":
                self.close(x, "probe passed")
            return
        if st == "closed":
            self.open(x, f"probe failed (exit {rc}; state/health_probe_{x}.out)", kind="probe")
            return
        data = self.load()
        e = self.entry(data, x)
        e["next_probe"] = int(time.time()) + int(self.c.get("probe_every", 300))
        self.save(data)
        self.remedy(x)

    def auth_check(self, sid: str, d: Dict[str, Any], tail: List[str]) -> bool:
        """True when the pane shows a login prompt: the breaker opens (kind auth) and the
        caller returns the session's row to the queue."""
        if not self.on:
            return False
        x = d.get("executor", "")
        pat = self.rig.cfg.pick(self.c.get("auth_patterns") or {}, x)
        if not pat:
            return False
        text = "\n".join(tail[-8:])
        if not re.search(pat, text, re.I):
            return False
        if self.state_of(x) != "open":
            self.open(x, f"{sid}'s pane asks for a login — the {x} CLI needs the Owner to sign in", kind="auth")
        return True

    # -- the engine tick -------------------------------------------------------------------
    def startup(self) -> None:
        """A self-test of every probed executor when the engine comes up."""
        if not self.on:
            return
        for x in self.rig.cfg.data["executors"].get("allowed") or []:
            if self._probe_cmd(x):
                self.probe(x)

    def tick(self) -> None:
        if not self.on:
            return
        now = int(time.time())
        data = self.load()
        changed = False
        for x, e in data.items():
            st = e.get("state", "closed")
            if st == "closed":
                continue
            if st == "open":
                if self._probe_cmd(x):
                    if now >= int(e.get("next_probe", 0)) and not self.rig.job_running(f"health:{x}"):
                        e["next_probe"] = now + int(self.c.get("probe_every", 300))
                        changed = True
                        self.save(data)
                        self.probe(x)
                        data = self.load()
                else:
                    wait = min(3600, int(self.c.get("open_secs", 600)) * (2 ** max(0, int(e.get("opens", 1)) - 1)))
                    if now - int(e.get("since", now)) >= wait:
                        e.update(state="half", trial="", half_since=now)
                        changed = True
                        self.rig.st.event(f"health:{x}", "breaker_half", executor=x)
                        self.rig.st.engine_log(f"health: {x} breaker HALF-open after {wait}s — one trial dispatch")
            if (e.get("state") in ("open", "half") and not e.get("steward_opened")
                    and (now - int(e.get("since", now)) >= int(self.c.get("steward_after", 3600))
                         or e.get("kind") == "auth")):
                e["steward_opened"] = True
                changed = True
                try:
                    import steward
                    steward.open_incident(self.rig, "breaker", f"health:{x}",
                                          f"executor {x} breaker {e.get('state')} since "
                                          f"{time.strftime('%F %T', time.localtime(int(e.get('since', now))))}: "
                                          f"{e.get('reason', '')}", klass="mechanical" if e.get("kind") != "auth" else "inbox")
                except Exception as ex:
                    self.rig.st.engine_log(f"health: steward incident not opened: {ex}")
        if changed:
            self.save(data)

    def summary_lines(self) -> List[str]:
        out = []
        for x, e in sorted(self.load().items()):
            if e.get("state", "closed") != "closed":
                out.append(f"{x}: {e['state'].upper()} since "
                           f"{time.strftime('%F %T', time.localtime(int(e.get('since', 0))))} — {e.get('reason', '')}")
        return out
