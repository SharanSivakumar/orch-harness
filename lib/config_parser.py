#!/usr/bin/env python3
"""orch.yaml loader + path resolver for the standalone orchestration framework.

One source of truth for every surface: the bash engine sources this via
`--sh` (a shell-eval'able KEY=VALUE dump), the Rich TUI and the Telegram bot
import `load()` directly. Nothing else may parse orch.yaml.

Path model (three distinct roots — an earlier rig conflated them):

  instance   the directory holding orch.yaml
             (e.g. docs/rig)
  workspace  the cycle/project folder queue plan paths are relative to
             (default: instance/..  ->  the parent folder)
  root       the directory executors are launched in — their cwd, and the
             base every path printed into a prompt is relative to
             (default: workspace/../..  ->  the project root)
  git_repo   the repository parallel_guard stages and commits into
             (default: the workspace)

`root` and `git_repo` can genuinely differ: executors may run from
$HOME/projects/app while commits land in a docs repo (docs/.git).
"""
from __future__ import annotations

import os
import shlex
import sys
from pathlib import Path
from typing import Any, Dict, List

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
    "executors": {
        "default": "opus",
        "allowed": ["opus", "sonnet", "haiku", "hermes", "hermesflash", "hermespro",
                    "codex", "gemini", "opencode", "manual", "mock"],
        "max_unblock_attempts": 3,
        "retier_on_max_unblock": "opus",
        "unblock_executor": "hermes",
        # What the watcher does when a row's window dies with NO sentinel:
        # "halt" (the lane stops, the operator investigates) or "retier" (a
        # scoped row is re-dispatched ONCE on retier_on_max_unblock; a second
        # death, or a BLOCK on the retiered run, halts). Unattended runs on
        # hermes/gemini want "retier".
        "dead_window_policy": "halt",
        # Which executors get an automatic unblocker when they return BLOCKED.
        # Empty = every scoped-flavored executor. An explicit list wins, which
        # is how a lead-flavored scoped-ish tier (hermespro) can still be
        # auto-unblocked — an earlier rig hardcoded exactly that.
        "auto_unblock": [],
        "paste_kick": ["hermes", "hermesflash", "hermespro"],
        # Executors whose adapter takes the kick text as its launch prompt
        # (an argv, not a tmux paste) — e.g. opencode's --prompt. Like
        # paste_kick, listing one makes the template engine write
        # prompts/<id>.kick.txt at every dispatch.
        "arg_kick": [],
        "models": {},
        "bin": {},
    },
    "guard": {
        "git_repo": "",           # empty -> workspace
        "auto_phase_isolation": True,
        "shared_paths": [],
        "resources": ["git-lock"],
        "git_lock": "git-lock",
        "compliance_scanners": [],
    },
    "watchdog": {
        "enabled": False,
        "telegram_chat_id": "",
        "bot_token_env_var": "STATUS_BOT_TOKEN",
        "status_bot_allowed_user": "",
        "env_file": "~/.config/orch/env",
        "hermes_push_fallback": True,
        "poll_interval": 20,
        "quiet_hours": {"from": "", "to": ""},
    },
}

# executor -> prompt flavor. Instances may extend via executors.flavors.
DEFAULT_FLAVORS = {
    "opus": "lead", "sonnet": "lead", "haiku": "lead", "hermespro": "lead",
    "hermes": "scoped", "hermesflash": "scoped", "gemini": "scoped", "opencode": "scoped",
    "codex": "codex", "manual": "manual", "mock": "lead",
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

        self.state: Path = instance / "state"
        self.control: Path = instance / "control"
        self.leases: Path = instance / "leases"
        self.prompts: Path = instance / "prompts"
        self.unblock: Path = instance / "unblock"
        self.templates_local: Path = instance / "prompts"

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

    def flavor(self, executor: str) -> str:
        flavors = dict(DEFAULT_FLAVORS)
        flavors.update(self.data["executors"].get("flavors") or {})
        return flavors.get(executor, "lead")

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
            ("ORCH_RESOURCES", " ".join(g["resources"])),
            ("ORCH_GIT_LOCK", g["git_lock"]),
            ("ORCH_SHARED_PATHS", " ".join(g["shared_paths"])),
            ("ORCH_AUTO_ISOLATION", "1" if g["auto_phase_isolation"] else "0"),
            ("ORCH_SCANNERS", " ".join(g["compliance_scanners"])),
            ("ORCH_WATCHDOG", "1" if w["enabled"] else "0"),
            ("ORCH_TG_CHAT", str(w["telegram_chat_id"])),
            ("ORCH_TG_TOKEN_VAR", w["bot_token_env_var"]),
            ("ORCH_TG_ENVFILE", os.path.expanduser(w["env_file"])),
            ("ORCH_TG_HERMES_FALLBACK", "1" if w["hermes_push_fallback"] else "0"),
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


def load(start: str | None = None) -> Config:
    inst = find_instance(start)
    raw = yaml.safe_load((inst / CONFIG_NAME).read_text()) or {}
    return Config(inst, _merge(DEFAULTS, raw))


if __name__ == "__main__":
    args = sys.argv[1:]
    cfg = load(args[1] if len(args) > 1 else None)
    if args and args[0] == "--sh":
        print(cfg.sh())
    else:
        import json
        print(json.dumps({"instance": str(cfg.instance), "workspace": str(cfg.workspace),
                          "root": str(cfg.root), "git_repo": str(cfg.git_repo),
                          "lanes": cfg.lanes(), "tmux": cfg.tmux_session}, indent=2))
