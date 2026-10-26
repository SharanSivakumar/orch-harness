#!/usr/bin/env python3
"""orch.yaml loader + path resolver for the standalone orchestration framework.

One source of truth for every surface: the bash engine sources this via
`--sh` (a shell-eval'able KEY=VALUE dump), the Rich TUI and the Telegram bot
import `load()` directly. Nothing else may parse orch.yaml.

Path model (three distinct roots — an earlier generation conflated them):

  instance   the directory holding orch.yaml
             (e.g. docs/rig)
  workspace  the cycle/project folder queue plan paths are relative to
             (default: instance/..  ->  docs)
  root       the directory executors are launched in — their cwd, and the
             base every path printed into a prompt is relative to
             (default: workspace/../..  ->  the project root)
  git_repo   the repository parallel_guard stages and commits into
             (default: the workspace)

`root` and `git_repo` may be genuinely different: executors run from the code
repository, while plan and report commits land in a separate docs repository.
"""
from __future__ import annotations

import os
import re
import shlex
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

try:
    import yaml
except ImportError:  # pragma: no cover
    sys.stderr.write("orch: PyYAML is required (pip install --user pyyaml)\n")
    raise SystemExit(2)

CONFIG_NAME = "orch.yaml"

DEFAULTS: Dict[str, Any] = {
    "version": "1.0",
    "project": {
        "name": "unnamed",
        "root": "../../..",
        "workspace": "..",
        "tmux_session": "orch",
        "reports_dir": "REPORTS",
    },
    "lanes": {
        "auto_discover": True,
        "order": [],
        "poll_interval": 60,
        "stall_warning_seconds": 2700,
    },
    # v2 engine (lib/engine.py). One process per rig schedules rows, not lanes.
    "scheduler": {
        "mode": "serial",          # serial: a row waits for the row above it in its lane (v1); dag: declared deps only
        "lane_modes": {},          # per-lane override, e.g. {X: dag}
        "tick_seconds": 15,        # engine loop period
        "max_parallel": 8,         # live sessions of every kind (rows, unblockers, reviewers, plan reviews)
        "min_free_mb": 3072,       # no dispatch while MemAvailable is below this
        "min_free_disk_mb": 0,     # no dispatch while the project root's filesystem has less free space; 0 = off
        "row_timeout_minutes": 0,  # 0 = none; a row may override with timeout=<min>
        "rearm_max": 5,            # engine re-arms per (caller row, target) pair, lifetime (lib/rearm.py)
        "timeout_action": "notify",  # notify | block
    },
    "close": {
        "stop_lines": True,        # lib/close_check.py — no CLOSED over the row's own stop line
        "require_progress": True,  # PROGRESS.md beside the plan, written after dispatch
        "require_commit": False,   # at least one commit since dispatch (counted either way)
        "protected": True,         # v3 lib/protected.py — no claim over a change to a protected path
        "probe_timeout": 900,      # seconds for <plan dir>/ACCEPT.sh or accept=<file>
        "on_fail": "redispatch",   # redispatch (same executor, findings attached) | unblock | halt
        "max_fail": 2,
    },
    "review": {
        "policy": "all",           # all | junior | none — which validated CLOSED rows get an independent review
        "default_reviewer": "sonnet",
        "fallback_reviewer": "opus",   # used when the mapped reviewer equals the row's own executor
        "reviewer_map": {"opus": "sonnet", "sonnet": "opus", "haiku": "sonnet",
                         "opencode": "sonnet", "gemini": "sonnet", "codex": "sonnet"},
        "require_different_executor": True,
        "on_reject": "redispatch",  # redispatch | unblock
        "max_rejects": 2,
    },
    "preflight": {
        "reviewer": "opus",
        "required": False,          # a round may also set preflight.required in round.yaml
        "run_timeout": 300,
    },
    "idle": {
        "nudge_after": 1200,        # seconds of pane silence with no claim before a nudge; 0 = never
        "max_nudges": 2,
        "block_after": 0,           # seconds after the last nudge before BLOCKED "idle without sentinel"; 0 = never
    },
    "reaper": {
        "enabled": True,
        "closed_grace_secs": 300,   # accepted/reviewing rows and finished unblock/review/plan-review sessions
        "halted_grace_secs": 0,     # BLOCKED/ROUTE_A windows; 0 = keep until reset
    },
    "progress": {
        "no_change_after": 10,      # consecutive accepted rows that changed nothing -> notify; 0 = off
        "action": "notify",         # notify | pause
        "noop_pattern": r"\b(INERT|NOT NEEDED|NOT-NEEDED|SUPERSEDED|WITHDRAWN|DROPPED|NO-?OP|RESERVE UNUSED|NOTHING TO DO|UNUSED)\b",
    },
    "goal": {},                     # optional: {metric_cmd, name, direction: up|down, target, instrument, stall_after, run_on: accept|manual}
    "hooks": {},                    # optional: on_dispatch|on_claim|on_accept|on_block|on_halt|on_idle -> shell command
    "framework": {
        "allow_dirty": False,       # refuse `orch start` from a dirty framework tree
    },
    # v3 (lib/health.py): executor circuit breaker. A session that dies with no claim within
    # fast_death_secs of its dispatch is an INFRA suspect: the row is retried on the same
    # executor, never retiered or charged. `threshold` distinct sessions of one executor dying
    # fast within window_secs OPEN the breaker: nothing dispatches to that executor, the
    # remedies run, and the probe (or, without one, a single trial dispatch after open_secs)
    # decides when it closes. Open longer than steward_after -> a Steward incident.
    "health": {
        "enabled": True,
        "fast_death_secs": 60,
        "window_secs": 1800,
        "threshold": 2,
        "open_secs": 600,           # no probe: half-open after this long (doubles per re-open, max 3600)
        "probe_every": 300,
        "probe_timeout": 120,
        "steward_after": 3600,
        "probes": {},               # {opencode: "opencode run --standalone 'Reply OK'"} — exit 0 = healthy
        "remedies": {},             # {opencode: ["pkill -f 'opencode serve --service' || true"]}
        "auth_patterns": {          # pane-tail patterns meaning "this CLI needs a login" (infra, not the row)
            "claude": r"Please run /login|Invalid API key|OAuth token (has )?expired|authentication_error",
        },
    },
    # v3 (lib/steward.py): the Steward lane. Where the rig would page the Owner or go quiet, the
    # engine opens an INCIDENT and dispatches a Steward session (sw-<n>-<k>) that acts through
    # typed, engine-validated `orch sw` requests, each logged with its revert in STEWARD_LOG.md.
    # Off by default: a v2 rig keeps paging as before.
    "steward": {
        "enabled": False,
        "tiers": {"mechanical": "sonnet", "judgment": "opus"},
        # class P (lib/panel.py): a decider whose acts are staged + an independent falsifier that
        # CONFIRMs (they execute) or OBJECTs (park + inbox). Needs two distinct allowed executors.
        "panel": {"enabled": True, "decider": "opus", "falsifier": "sonnet", "max_sessions": 4},
        "max_sessions_per_incident": 3,
        "max_resets_per_row_day": 3,
        "max_actions_per_session": 25,
        "max_parallel": 1,
        "sessions_per_day": 40,
        "gate_after": 600,          # a row held only by its gate this long -> incident
        "rest_after": 1200,         # nothing running, work left, this long -> incident ("rig at rest")
        "page_classes": ["spend", "pii", "secret", "engine_down"],
        "rights_file": "steward/RIGHTS.md",   # relative to the instance; the decision-rights table
        "planner": "opus",
        "default_gate_kind": "owner",   # a gate with no gate_kind= is the Owner's (the Steward never opens it)
        # lib/revert.py: the Owner's Telegram `revert <k>` (control/REVERT_<k>.json, run by the engine);
        # the bot waits revert_wait seconds for the engine's answer
        "telegram_revert": True,
        "revert_wait": 60,
        # periodic commands whose output opens incidents (lib/steward.py "Duties"); none by default.
        # Entries: {name: [a-z0-9_-]+, every: >=300 s, cmd: "...", timeout: 600, klass: mechanical|judgment}
        "duties": [],
    },
    # v3 (lib/spend.py): the spend governor. Paid commands run through `orch spend run`, which reserves
    # the estimate against the Owner-approved caps (state/spend_caps.approved.yaml, from SPEND_CAPS.yaml
    # by `orch spend caps approve`) and, over a cap, runs nothing and prints SPEND_WAIT <id>: the row
    # waits in spend_wait and the Owner gets ONE page (page_class spend), answered `spend <id> A|B`.
    # Off: `orch spend run` runs the command and books it in the ledger, with no cap.
    "spend": {
        "enabled": False,
        "dir": "",                  # host-wide ledger dir; "" = ~/.local/state/orch (ORCH_SPEND_DIR overrides, not in panes)
        "telegram_answers": True,   # the Owner's `spend <id> A|B` on Telegram (sealed like `revert <k>`)
        "answer_wait": 60,          # seconds the bot waits for the engine's answer
        "min_est_usd": 0.01,        # an estimate below this (or min_est.<provider>) is raised to it
        "min_est": {},              # {mistral: 0.05}
        "grant_secs": 86400,        # an approved request's headroom is held for its row this long
        # vC2 (lib/meter.py): the OpenCode meter reads opencode.db read-only — table message, JSON keys cost,
        # tokens, modelID, providerID, time only (SQLite authorizer) — and books each message once host-wide
        "opencode": {"meter": False, "db": "~/.local/share/opencode/opencode.db", "every": 300,
                     "settle_secs": 900, "family": "opencode", "executors": [], "headroom_usd": 1.0},
        "economy_pct": 80,          # at this % of any approved cap: state/spend/economy + a digest line
        "reconcile": [],            # [{provider, cmd, every}] — cmd prints `SPEND <provider> <usd_month_to_date>`
        "reconcile_tolerance_usd": 1.0,
        "reconcile_timeout": 120,
    },
    # v3 (lib/digest.py): the Owner's view on his schedule. times: ["08:00", "18:00"]
    "digest": {"times": [], "keep_days": 30},
    # v3: engine self-maintenance
    "engine": {
        "self_update": True,        # re-exec on a newer TESTED framework commit (clean tree, .tested/<sha>)
        "update_check_secs": 60,
        "commit_bookkeeping": False,  # commit BLOCK_SHEET / unblock / steward / digest files via the guard
        "bookkeeping_every": 1800,
    },
    "executors": {
        "default": "opus",
        "allowed": ["opus", "sonnet", "haiku", "codex", "gemini", "opencode", "manual", "mock"],
        "max_unblock_attempts": 3,
        "retier_on_max_unblock": "opus",
        "unblock_executor": "sonnet",
        # What the watcher does when a row's window dies with NO sentinel:
        # "halt" (the lane stops, the operator investigates) or "retier" (a
        # JR row is re-dispatched ONCE on retier_on_max_unblock; a second
        # death, or a BLOCK on the retiered run, halts). Unattended runs of
        # junior executors (opencode/gemini) want "retier".
        "dead_window_policy": "halt",
        # Which executors get an automatic unblocker when they return BLOCKED.
        # Empty = every jrdev-flavored executor. An explicit list wins, which
        # is how a HARD-flavored tier can still be auto-unblocked.
        "auto_unblock": [],
        # Executors whose TUI takes its prompt as a tmux paste once its input
        # line is ready (prompts/<id>.kick.txt). None of the built-in adapters
        # needs it; an instance may list an executor added via executors.adapters.
        "paste_kick": [],
        # Executors whose adapter takes the kick text as its launch prompt
        # (an argv, not a tmux paste) — e.g. opencode's --prompt. Like
        # paste_kick, listing one makes the template engine write
        # prompts/<id>.kick.txt at every dispatch.
        "arg_kick": [],
        "models": {},
        "bin": {},
        # v2: extra CLI args and environment per executor (replaces wrapper scripts
        # such as a claude launcher that only adds --effort high)
        "args": {},                 # {opus: ["--effort", "high"]}
        "env": {},                  # {opencode: {OPENCODE_DISABLE_SHARE: "1"}}
        "max_parallel": {},         # {opus: 4}
        "unblock_executors": [],    # two-stage unblocking: attempt n uses entry min(n, len)-1; empty = [unblock_executor]
        # usage-limit banners, matched against the LAST lines of the pane only
        "limit_patterns": {
            "claude": r"hit your (usage|session|weekly) limit|usage limit reached|limit will reset|limit reached\W.*reset",
            "codex": r"try again at|usage limit|codex/settings/usage",
            "gemini": r"RESOURCE_EXHAUSTED|quota exceeded|rate limit",
            "opencode": r"rate limit|429|quota exceeded|insufficient (credit|balance)",
        },
        "limit_resume_text": {"claude": "continue", "codex": "continue the task"},
        # lib/fallback.py (v3.2): executor VARIANTS and the RATE-LIMIT FALLBACK. Both empty by default.
        #   variants: {opencode_gemini: {base: opencode, model: "google/gemini-4-argon", env: {…}, args: […],
        #              max_parallel: 3}}   a second executor on the same adapter; inherits the base's flavor,
        #   kick behaviour, reviewer mapping, health remedies, probes, limit patterns and env/args.
        #   fallback: {opencode: {to: [opencode_gemini], limited_rows: 2, limited_minutes: 10}}
        #   limit_patterns (above) may name an executor or an adapter family and hold one regex or a list.
        #   limit_confirm_secs: a row counts as rate-limited when its pattern matches the pane tail on two
        #   checks at least this far apart with no other pane movement in between.
        "variants": {},
        "fallback": {},
        "limit_confirm_secs": 60,
    },
    "guard": {
        "git_repo": "",           # empty -> workspace
        "auto_phase_isolation": True,
        "shared_paths": [],
        "resources": ["git-lock"],
        "git_lock": "git-lock",
        "compliance_scanners": [],
        # v3 (lib/protected.py): what only the Owner may change. Globs relative to git_repo
        # (`*` crosses `/`; a trailing `/` or a directory protects its tree), plus — with
        # protected_auto — the instance's SPEND_CAPS.yaml, phases.yaml and steward.rights_file.
        # The guard stages them only for an operator `plan:` commit; the close check refuses
        # a claim that changed one unless PROGRESS names an Owner-signed ruling.
        "protected_paths": [],
        "protected_auto": True,
        # v3 (lib/capability.py): a caller that descends from a session's pane (/proc PPid chain
        # against the pane pids of the instance's session records) is that session, ORCH_ROW or
        # not — operator-only commands, park/unpark, inbox done, service, operator plan: commits,
        # ORCH_SCANNER_OVERRIDE / ORCH_SHARED_WHOLE and force-release refuse it. capability_cgroup
        # also counts a process re-parented out of the pane but still in its own per-pane cgroup.
        "capability_check": True,
        "capability_cgroup": True,
        # v3 (lib/rowadd.py): the engine keeps state/queues.sha and reports a queue edited outside
        # `orch row add` (and outside an operator plan: commit or `orch row sync`) in the digest
        "queue_watch": False,
        "queue_watch_grace": 600,  # seconds a difference stands before it is reported (an operator commit blesses it)
    },
    "watchdog": {
        "enabled": False,
        "telegram_chat_id": "",
        "bot_token_env_var": "STATUS_BOT_TOKEN",
        "status_bot_allowed_user": "",
        "env_file": "~/.config/orch/env",
        # Which engine events reach the phone: "normal" (default: normal + critical)
        # or "critical" (halts, Route A, forged verdicts, no-progress only). Explicit
        # `orch notify` pages from a row are never filtered.
        "push_min_urgency": "normal",
        "poll_interval": 20,
        "quiet_hours": {"from": "", "to": ""},   # pages inside the window are HELD, sent when it ends
        # Owner answers by Telegram reply (off by default: the bot is one-way). On: a reply
        # "<row> <LETTER>" to a halted row's page writes control/ANSWER_<row>.json; the engine
        # dispatches an answer session (answer_executor, default the top unblock tier) that
        # records the ruling under owner_name and applies it; RESOLVED re-dispatches the row.
        "answers": False,
        "answer_executor": "",
        "owner_name": "the Owner",
    },
}

# executor -> prompt flavor. Instances may extend via executors.flavors.
# Haiku is a junior tier: it implements a complete plan and blocks at the first judgment call.
DEFAULT_FLAVORS = {
    "opus": "hard", "sonnet": "hard", "haiku": "jrdev",
    "gemini": "jrdev", "opencode": "jrdev",
    "codex": "codex", "manual": "manual", "mock": "hard",
}


def _merge(base: Any, over: Any) -> Any:
    if isinstance(base, dict) and isinstance(over, dict):
        out = dict(base)
        for k, v in over.items():
            out[k] = _merge(base.get(k), v) if k in base else v
        return out
    return base if over is None else over


def find_instance(start: str | None = None) -> Path:
    """Walk up from `start` (default $ORCH_INSTANCE or cwd) to find orch.yaml."""
    env = os.environ.get("ORCH_INSTANCE")
    p = Path(start or env or Path.cwd()).resolve()
    if p.is_file():
        p = p.parent
    for cand in [p, *p.parents]:
        if (cand / CONFIG_NAME).is_file():
            return cand
    sys.stderr.write(
        f"orch: no {CONFIG_NAME} found in {p} or any parent.\n"
        f"      Run `orch init <dir>` to create an instance, or set ORCH_INSTANCE.\n")
    raise SystemExit(2)


class Config:
    def __init__(self, instance: Path, data: Dict[str, Any]):
        self.instance = instance
        self.data = data
        p = data["project"]
        self.name: str = p["name"]
        self.workspace: Path = (instance / p["workspace"]).resolve()
        self.root: Path = (instance / p["root"]).resolve()
        self.tmux_session: str = p["tmux_session"]
        self.reports_dir: str = p["reports_dir"]

        g = data["guard"]
        self.git_repo: Path = ((instance / g["git_repo"]).resolve()
                               if g.get("git_repo") else self.workspace)

        self._expand_variants()

        self.state: Path = instance / "state"
        self.control: Path = instance / "control"
        self.leases: Path = instance / "leases"
        self.prompts: Path = instance / "prompts"
        self.unblock: Path = instance / "unblock"
        self.templates_local: Path = instance / "prompts"

    # -- executor variants (lib/fallback.py) -------------------------------
    def variants(self) -> Dict[str, Dict[str, Any]]:
        """executors.variants as {name: {base, env, args, model, max_parallel}}; malformed entries are
        dropped here and named by `orch lint` (fallback.config_problems)."""
        out: Dict[str, Dict[str, Any]] = {}
        raw = self.data["executors"].get("variants") or {}
        if not isinstance(raw, dict):
            return out
        for name, v in raw.items():
            if isinstance(v, dict) and isinstance(v.get("base"), str) and v["base"] and str(name) != v["base"]:
                out[str(name)] = v
        return out

    def base_of(self, executor: str) -> str:
        """The executor a variant runs as (itself when it is not a variant). One level only."""
        v = self.variants().get(executor)
        return str(v["base"]) if v and str(v["base"]) not in self.variants() else executor

    def keys_for(self, executor: str) -> List[str]:
        """Config-table keys that may speak for an executor, most specific first: itself, its variant base,
        its adapter family."""
        out: List[str] = []
        for k in (executor, self.base_of(executor), self.adapter(executor)):
            if k and k not in out:
                out.append(k)
        return out

    def pick(self, table: Any, executor: str, default: Any = None, present: bool = False) -> Any:
        """The first non-empty entry of a per-executor table, by keys_for(); present=True takes the first key
        that is there at all (an explicit empty list then means "none")."""
        if isinstance(table, dict):
            for k in self.keys_for(executor):
                if (k in table) if present else table.get(k):
                    return table[k]
        return default

    def _expand_variants(self) -> None:
        """A declared variant is an allowed executor and inherits its base's kick lists, auto-unblock
        membership, reviewer mapping and (optionally) its own session cap. Everything else it inherits
        is looked up through keys_for() (flavor, adapter, limit patterns, health tables, launch env)."""
        e = self.data["executors"]
        vs = self.variants()
        if not vs:
            return
        allowed = list(e.get("allowed") or [])
        for name, v in vs.items():
            b = self.base_of(name)
            if name not in allowed:
                allowed.append(name)
            for key in ("paste_kick", "arg_kick", "auto_unblock"):
                lst = list(e.get(key) or [])
                if lst and b in lst and name not in lst:
                    lst.append(name)
                e[key] = lst
            rm = self.data.setdefault("review", {}).setdefault("reviewer_map", {})
            if name not in rm and b in rm:
                rm[name] = rm[b]
            if v.get("max_parallel") and name not in (e.get("max_parallel") or {}):
                e.setdefault("max_parallel", {})[name] = v["max_parallel"]
        e["allowed"] = allowed

    # -- derived ---------------------------------------------------------
    @property
    def ws_prefix(self) -> str:
        """Workspace path relative to the project root ('' when identical).

        This is the prefix a prompt needs so an executor sitting in `root`
        can open a plan named relative to the workspace.
        """
        try:
            rel = self.workspace.relative_to(self.root)
        except ValueError:
            return str(self.workspace)
        return "" if str(rel) == "." else str(rel)

    @property
    def repo_prefix(self) -> str:
        """Workspace path relative to the guard's git repo ('' when identical)."""
        try:
            rel = self.workspace.relative_to(self.git_repo)
        except ValueError:
            return str(self.workspace)
        return "" if str(rel) == "." else str(rel)

    def lanes(self) -> List[str]:
        cfg = self.data["lanes"]
        found = sorted(q.stem[len("queue_"):] for q in self.instance.glob("queue_*.tsv"))
        order = [str(x) for x in (cfg.get("order") or [])]
        if not cfg.get("auto_discover", True):
            return order
        return order + [l for l in found if l not in order]

    def junior_only(self, executor: str) -> bool:
        """A Haiku tier (by name or by its pinned model id) is junior whatever executors.flavors says."""
        v = self.variants().get(executor) or {}
        return (executor == "haiku" or "haiku" in self.model(executor).lower()
                or "haiku" in str(v.get("model", "")).lower()
                or (executor in self.variants() and self.base_of(executor) != executor
                    and self.junior_only(self.base_of(executor))))

    def flavor(self, executor: str) -> str:
        if self.junior_only(executor):
            return "jrdev"
        flavors = dict(DEFAULT_FLAVORS)
        flavors.update(self.data["executors"].get("flavors") or {})
        if executor not in flavors and self.base_of(executor) != executor:
            return self.flavor(self.base_of(executor))      # a variant has its base's flavor
        return flavors.get(executor, "hard")

    def decides(self, executor: str) -> bool:
        """May `executor` fill a decision role? Only a hard-flavored tier may; a junior (jrdev),
        codex or manual tier never unblocks, reviews, plans, stewards or takes over a row."""
        return self.flavor(executor) == "hard"

    def decision_roles(self) -> List[Tuple[str, str]]:
        """Every configured decision role as (config key, executor) — each must satisfy decides()."""
        e, rv = self.data["executors"], self.data.get("review") or {}
        sw, w = self.data.get("steward") or {}, self.data.get("watchdog") or {}
        out = [("executors.unblock_executor", e.get("unblock_executor")),
               ("executors.retier_on_max_unblock", e.get("retier_on_max_unblock"))]
        out += [(f"executors.unblock_executors[{i}]", x) for i, x in enumerate(e.get("unblock_executors") or [])]
        out += [(f"review.{k}", rv.get(k)) for k in ("default_reviewer", "fallback_reviewer")]
        out += [(f"review.reviewer_map.{k}", v) for k, v in (rv.get("reviewer_map") or {}).items()]
        out.append(("preflight.reviewer", (self.data.get("preflight") or {}).get("reviewer")))
        if sw.get("enabled"):
            out += [(f"steward.tiers.{k}", v) for k, v in (sw.get("tiers") or {}).items()]
            out.append(("steward.planner", sw.get("planner")))
            panel = sw.get("panel") or {}
            if panel.get("enabled", True):
                out += [(f"steward.panel.{k}", panel.get(k)) for k in ("decider", "falsifier")]
        if w.get("answers"):
            out.append(("watchdog.answer_executor", w.get("answer_executor")))
        return [(k, str(v)) for k, v in out if v]

    def resources(self) -> Dict[str, Dict[str, Any]]:
        """guard.resources as {name: {slots, scope}}; plain strings are 1-slot instance leases."""
        out: Dict[str, Dict[str, Any]] = {}
        for r in self.data["guard"].get("resources") or []:
            if isinstance(r, dict):
                name = str(r.get("name", "")).strip()
                if name:
                    out[name] = {"slots": max(1, int(r.get("slots", 1) or 1)),
                                 "scope": str(r.get("scope", "instance"))}
            elif str(r).strip():
                out[str(r).strip()] = {"slots": 1, "scope": "instance"}
        return out

    def resource_names(self) -> List[str]:
        return list(self.resources())

    def lane_mode(self, lane: str) -> str:
        s = self.data.get("scheduler") or {}
        return str((s.get("lane_modes") or {}).get(lane, s.get("mode", "serial")))

    def adapter(self, executor: str) -> str:
        """executors.adapters maps an executor name onto an adapter (e.g. an alias
        `opus-max: claude`); the built-in Claude tiers map to claude.sh."""
        custom = (self.data["executors"].get("adapters") or {}).get(executor)
        if custom:
            return str(custom)
        if self.base_of(executor) != executor:          # a variant runs through its base's adapter
            return self.adapter(self.base_of(executor))
        return {"opus": "claude", "sonnet": "claude", "haiku": "claude"}.get(executor, executor)

    def family(self, executor: str) -> str:
        """Adapter family a pattern table is keyed by (claude, codex, gemini, opencode…)."""
        return self.adapter(executor)

    def binary(self, key: str, default: str) -> str:
        return (self.data["executors"].get("bin") or {}).get(key, default)

    def model(self, key: str, default: str = "") -> str:
        return (self.data["executors"].get("models") or {}).get(key, default)

    # -- shell export ----------------------------------------------------
    def sh(self) -> str:
        e, l, g, w = (self.data["executors"], self.data["lanes"],
                      self.data["guard"], self.data["watchdog"])
        q = shlex.quote
        pairs = [
            ("ORCH_NAME", self.name),
            ("ORCH_INSTANCE", str(self.instance)),
            ("ORCH_WORKSPACE", str(self.workspace)),
            ("ORCH_ROOT", str(self.root)),
            ("ORCH_GIT_REPO", str(self.git_repo)),
            ("ORCH_WS_PREFIX", self.ws_prefix),
            ("ORCH_REPO_PREFIX", self.repo_prefix),
            ("ORCH_TMUX", self.tmux_session),
            ("ORCH_REPORTS", self.reports_dir),
            ("ORCH_LANES", " ".join(self.lanes())),
            ("ORCH_POLL", str(l["poll_interval"])),
            ("ORCH_STALL_SECS", str(l["stall_warning_seconds"])),
            ("ORCH_EXEC_DEFAULT", e["default"]),
            ("ORCH_EXECUTORS", " ".join(e["allowed"])),
            ("ORCH_MAX_UNBLOCK", str(e["max_unblock_attempts"])),
            ("ORCH_RETIER_ON_MAX", e["retier_on_max_unblock"]),
            ("ORCH_UNBLOCK_EXEC", e["unblock_executor"]),
            ("ORCH_DEAD_WINDOW", e.get("dead_window_policy", "halt")),
            ("ORCH_AUTO_UNBLOCK", " ".join(e["auto_unblock"])),
            ("ORCH_PASTE_KICK", " ".join(e["paste_kick"])),
            ("ORCH_ARG_KICK", " ".join(e["arg_kick"])),
            ("ORCH_RESOURCES", " ".join(self.resource_names())),
            ("ORCH_GIT_LOCK", g["git_lock"]),
            ("ORCH_SHARED_PATHS", " ".join(g["shared_paths"])),
            ("ORCH_AUTO_ISOLATION", "1" if g["auto_phase_isolation"] else "0"),
            ("ORCH_SCANNERS", " ".join(g["compliance_scanners"])),
            ("ORCH_WATCHDOG", "1" if w["enabled"] else "0"),
            ("ORCH_TG_CHAT", str(w["telegram_chat_id"])),
            ("ORCH_TG_TOKEN_VAR", w["bot_token_env_var"]),
            ("ORCH_TG_ENVFILE", os.path.expanduser(w["env_file"])),
            ("ORCH_QUIET_FROM", w["quiet_hours"]["from"] or ""),
            ("ORCH_QUIET_TO", w["quiet_hours"]["to"] or ""),
            ("ORCH_LIB", str(Path(__file__).resolve().parent)),
        ]
        out = [f"{k}={q(str(v))}" for k, v in pairs]
        for k, v in (e.get("bin") or {}).items():
            out.append(f"ORCH_BIN_{k.upper()}={q(str(v))}")
        for k, v in (e.get("models") or {}).items():
            out.append(f"ORCH_MODEL_{k.upper()}={q(str(v))}")
        return "\n".join(out)


def launch_env(cfg: Config, executor: str) -> str:
    """Shell lines for lib/launch.sh: the adapter path, ORCH_EXTRA_ARGS (shell-quoted)
    and the executor's own environment from executors.args / executors.env. Keys may
    name the executor (opus) or its adapter family (claude); the executor wins.

    A VARIANT (executors.variants) layers on its base: args are the variant's own (executors.args.<name>, else
    the variant's `args`, else the base's); env is the family's, then the base's, then executors.env.<name>, then
    the variant's `env`; its `model` becomes ORCH_VARIANT_MODEL (the claude and opencode adapters honour it).
    The adapter is handed the BASE's name (ORCH_ADAPTER_X) so its model pins and kick lists apply;
    ORCH_EXECUTOR carries the real name."""
    e = cfg.data["executors"]
    fam = cfg.family(executor)
    base = cfg.base_of(executor)
    var = cfg.variants().get(executor) if base != executor else None
    targs = e.get("args") or {}
    args = targs.get(executor)
    if args is None and var is not None:
        args = var.get("args")
    if args is None:
        args = targs.get(base, targs.get(fam, []))
    args = args or []
    if isinstance(args, str):
        args = shlex.split(args)
    env: Dict[str, Any] = {}
    tenv = e.get("env") or {}
    for k in (fam, base, executor):
        env.update(tenv.get(k, {}) or {})
    if var is not None and isinstance(var.get("env"), dict):
        env.update(var["env"])
    q = shlex.quote
    adapter = Path(__file__).resolve().parent / "adapters" / f"{cfg.adapter(executor)}.sh"
    out = [f"ORCH_ADAPTER={q(str(adapter))}",
           f"ORCH_ADAPTER_X={q(base)}",
           f"export ORCH_EXECUTOR={q(executor)}",
           f"export ORCH_EXTRA_ARGS={q(' '.join(q(str(a)) for a in args))}"]
    if var is not None and var.get("model"):
        out.append(f"export ORCH_VARIANT_MODEL={q(str(var['model']))}")
    for k, v in env.items():
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", str(k)):
            out.append(f"export {k}={q(os.path.expanduser(str(v)))}")
    return "\n".join(out)


def load(start: str | None = None) -> Config:
    inst = find_instance(start)
    raw = yaml.safe_load((inst / CONFIG_NAME).read_text()) or {}
    return Config(inst, _merge(DEFAULTS, raw))


if __name__ == "__main__":
    args = sys.argv[1:]
    cfg = load(args[1] if len(args) > 1 and args[0] != "--launch" else None)
    if args and args[0] == "--sh":
        print(cfg.sh())
    elif args and args[0] == "--launch":
        cfg = load()
        print(launch_env(cfg, args[1]))
    else:
        import json
        print(json.dumps({"instance": str(cfg.instance), "workspace": str(cfg.workspace),
                          "root": str(cfg.root), "git_repo": str(cfg.git_repo),
                          "lanes": cfg.lanes(), "tmux": cfg.tmux_session}, indent=2))
