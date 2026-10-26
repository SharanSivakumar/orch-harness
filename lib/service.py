#!/usr/bin/env python3
"""Always-on (v3): the engine and the bot as systemd USER units, plus a dead-man timer.

Why: a reboot used to leave the engine down until a human started it — the
engine lived only in a tmux window.

  orch service install     writes and enables, per rig (slug = the rig's tmux session):
                             orch-engine-<slug>.service   the engine (Restart=on-failure,
                                                          KillMode=process — agent sessions
                                                          live in tmux and are never killed)
                             orch-bot-<slug>.service      the Telegram poller (when watchdog.enabled)
                             orch-deadman-<slug>.timer    every 5 min: a stale heartbeat restarts
                                                          the engine; 2 failed restarts page once
                                                          (page class engine_down)
                           and tells you whether `loginctl enable-linger` is needed so the
                           units start at boot without a login (that one command is yours).
  orch service uninstall | status
  orch service deadman     what the timer runs

The engine under systemd keeps the tmux window `orch-engine` as a live view of its log, so
the rig stays visible. A deliberate `orch engine stop` (state/engine.stopped) is never
undone by the dead-man.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
LIB = Path(__file__).resolve().parent
UNIT_DIR = Path(os.environ.get("ORCH_SYSTEMD_USER_DIR", os.path.expanduser("~/.config/systemd/user")))


def slug(cfg) -> str:
    return re.sub(r"[^a-z0-9-]", "", cfg.tmux_session.lower()) or "rig"


def unit_name(cfg, what: str) -> str:
    """orch-engine-<slug>.service, orch-bot-<slug>.service"""
    return f"orch-{what}-{slug(cfg)}.service"


def _systemctl(*args: str) -> Tuple[int, str]:
    if os.environ.get("ORCH_NO_SYSTEMD") == "1" or not shutil.which("systemctl"):
        return 127, "systemctl unavailable"
    try:
        p = subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True, timeout=30)
        return p.returncode, (p.stdout + p.stderr).strip()
    except Exception as e:
        return 1, str(e)


def installed(cfg) -> bool:
    return (UNIT_DIR / unit_name(cfg, "engine")).is_file()


def active(cfg) -> bool:
    if not installed(cfg):
        return False
    rc, out = _systemctl("is-active", unit_name(cfg, "engine"))
    return rc == 0 and out.strip() == "active"


def start_unit(cfg) -> Tuple[bool, str]:
    rc, out = _systemctl("start", unit_name(cfg, "engine"))
    return rc == 0, out


def stop_unit(cfg) -> Tuple[bool, str]:
    rc, out = _systemctl("stop", unit_name(cfg, "engine"))
    return rc == 0, out


def ensure_viewer(rig) -> None:
    """The engine runs under systemd; the tmux window orch-engine shows its log live."""
    import tmuxio
    if not active(rig.cfg):
        return
    wins = tmuxio.windows(rig.session)
    w = wins.get("orch-engine")
    if w and w["alive"]:
        return
    cmd = f"tail -n 50 -F {rig.cfg.state / 'engine.log'}"
    if w:
        tmuxio.respawn(rig.session, "orch-engine", cmd)
    else:
        tmuxio.new_window(rig.session, "orch-engine", str(rig.cfg.root), cmd)


def _env_lines(cfg) -> str:
    keep = {"PATH": os.environ.get("PATH", ""), "HOME": os.path.expanduser("~"), "LANG": os.environ.get("LANG", "C.UTF-8"),
            "ORCH_INSTANCE": str(cfg.instance), "ORCH_SERVICE": "1"}
    return "\n".join(f'Environment="{k}={v}"' for k, v in keep.items() if v)


def units(cfg) -> dict:
    py = sys.executable
    eng = f"""[Unit]
Description=orch engine — {cfg.name} ({cfg.instance})
After=default.target

[Service]
Type=simple
{_env_lines(cfg)}
WorkingDirectory={cfg.root}
ExecStart={py} {LIB / 'engine.py'} --wait-lock
Restart=on-failure
RestartSec=15
# agent sessions run inside tmux; only the engine process itself is ever stopped or restarted
KillMode=process
TimeoutStopSec=20

[Install]
WantedBy=default.target
"""
    bot = f"""[Unit]
Description=orch Telegram bot — {cfg.name}
After=network-online.target

[Service]
Type=simple
{_env_lines(cfg)}
WorkingDirectory={cfg.root}
ExecStart={py} {LIB / 'telegram_bot.py'} daemon
Restart=always
RestartSec=30

[Install]
WantedBy=default.target
"""
    dm = f"""[Unit]
Description=orch dead-man check — {cfg.name}

[Service]
Type=oneshot
{_env_lines(cfg)}
ExecStart={py} {LIB / 'service.py'} deadman
"""
    tm = f"""[Unit]
Description=orch dead-man timer — {cfg.name}

[Timer]
OnBootSec=3min
OnUnitActiveSec=5min
Persistent=true

[Install]
WantedBy=timers.target
"""
    out = {unit_name(cfg, "engine"): eng, f"orch-deadman-{slug(cfg)}.service": dm,
           f"orch-deadman-{slug(cfg)}.timer": tm}
    w = cfg.data.get("watchdog") or {}
    if w.get("enabled") and w.get("answers"):
        # only one process may long-poll a bot token, and a host gateway may already poll it; pages are
        # pushes and need no bot — the unit exists only when the Owner answers (and reverts) by Telegram
        out[unit_name(cfg, "bot")] = bot
    return out


def linger_needed() -> bool:
    if not shutil.which("loginctl"):
        return False
    try:
        p = subprocess.run(["loginctl", "show-user", os.environ.get("USER", ""), "--property=Linger"],
                           capture_output=True, text=True, timeout=10)
        return "Linger=yes" not in p.stdout
    except Exception:
        return False


def install(cfg) -> int:
    UNIT_DIR.mkdir(parents=True, exist_ok=True)
    us = units(cfg)
    for name, text in us.items():
        (UNIT_DIR / name).write_text(text)
        print(f"wrote {UNIT_DIR / name}")
    bot = unit_name(cfg, "bot")
    if bot not in us and (UNIT_DIR / bot).exists():
        # an earlier install wrote it; without watchdog.answers it would be a second long-poller of the token
        _systemctl("disable", "--now", bot)
        (UNIT_DIR / bot).unlink(missing_ok=True)
        print(f"removed {bot} (watchdog.answers is off: pages are pushes and need no bot)")
    _systemctl("daemon-reload")
    for name in us:
        if name.endswith(".timer") or not name.startswith("orch-deadman"):
            rc, out = _systemctl("enable", name)
            print(f"enable {name}: {'ok' if rc == 0 else out}")
    print(f"The engine unit starts with `orch start` (or `systemctl --user start {unit_name(cfg, 'engine')}`); "
          f"a running tmux engine hands over when it stops.")
    rc, _ = _systemctl("start", f"orch-deadman-{slug(cfg)}.timer")
    if bot in us:
        print(f"bot: `systemctl --user start {unit_name(cfg, 'bot')}` once the tmux bot (orch bot stop) is stopped")
    if linger_needed():
        print("\nONE COMMAND FOR THE OWNER — so these units start at boot without anyone logging in:\n"
              f"    loginctl enable-linger {os.environ.get('USER', '$USER')}\n")
    return 0


def uninstall(cfg) -> int:
    for name in list(units(cfg)) + [unit_name(cfg, "bot")]:
        _systemctl("disable", "--now", name)
        (UNIT_DIR / name).unlink(missing_ok=True)
        print(f"removed {name}")
    _systemctl("daemon-reload")
    return 0


def status(cfg) -> int:
    for name in units(cfg):
        rc, out = _systemctl("is-active", name)
        print(f"{name:<40} {'installed' if (UNIT_DIR / name).is_file() else 'NOT installed':<14} {out or '-'}")
    hb = cfg.state / "engine.heartbeat"
    print(f"heartbeat age: {int(time.time() - hb.stat().st_mtime) if hb.exists() else '-'} s; "
          f"linger {'needed' if linger_needed() else 'ok'}")
    return 0


def deadman(cfg) -> int:
    """Stale heartbeat (> 5 min) and no deliberate stop -> restart the unit; 2 restarts that did
    not bring the heartbeat back -> one page (class engine_down) per outage."""
    hb = cfg.state / "engine.heartbeat"
    f = cfg.state / "deadman.json"
    try:
        dm = json.loads(f.read_text())
    except Exception:
        dm = {}
    if (cfg.state / "engine.stopped").exists():
        dm = {}
        f.write_text(json.dumps(dm))
        return 0
    age = time.time() - hb.stat().st_mtime if hb.exists() else 1e9
    if age < 300:
        if dm.get("restarts"):
            dm = {}
            f.write_text(json.dumps(dm))
        return 0
    if not installed(cfg):
        return 0
    dm["restarts"] = int(dm.get("restarts", 0)) + 1
    dm["last"] = time.strftime("%F %T")
    rc, out = _systemctl("restart", unit_name(cfg, "engine"))
    with open(cfg.state / "engine.log", "a") as lf:
        lf.write(f"{time.strftime('%F %T')} dead-man: heartbeat {int(age)}s old — restart {dm['restarts']} "
                 f"({'ok' if rc == 0 else out[:200]})\n")
    if dm["restarts"] >= 3 and not dm.get("paged"):
        dm["paged"] = True
        subprocess.run([sys.executable, str(LIB / "telegram_bot.py"), "push", "--title", "engine down",
                        "--body", f"The {cfg.name} engine has not run for {int(age // 60)} min and 2 automatic "
                                  f"restarts did not bring it back. Look: systemctl --user status "
                                  f"{unit_name(cfg, 'engine')}; tail {cfg.state / 'engine.log'}", "--force"],
                       env=dict(os.environ, ORCH_INSTANCE=str(cfg.instance)), capture_output=True, timeout=60)
    f.write_text(json.dumps(dm))
    return 0


def main(argv) -> int:
    from config_parser import load
    cfg = load()
    sub = argv[0] if argv else "status"
    import capability
    if sub != "deadman" and capability.session(cfg):
        print(f"REFUSED: `orch service {sub}` is the operator's command (session {capability.session(cfg)})")
        return 1
    return {"install": install, "uninstall": uninstall, "status": status, "deadman": deadman}.get(
        sub, lambda c: (print("usage: orch service install|uninstall|status|deadman"), 2)[1])(cfg)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
