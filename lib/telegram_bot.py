#!/usr/bin/env python3
"""Telegram watchdog (push) + interactive status bot (poll) — read-only.

Two jobs, one file:

  push    The watcher's phone leg. Fires on gates, halts, escalations and
          completions. Gated three ways: watchdog.enabled in orch.yaml, the
          control/WATCHDOG_ARMED sentinel, and the operator's quiet hours.
          Failure is ALWAYS silent — a lane must never break because a chat
          service is unreachable.

  poll    Answers "status" from ONE authorized user id with a live snapshot:
          the resolved lane/gate/executor summary plus the raw tail of the
          running executor's tmux pane. Deterministic, no LLM, no memory, no
          navigation. Everything else is ignored (one-way by design).

The bot NEVER writes a sentinel, releases a gate, or sends a keystroke into a
pane. Operator actions happen in tmux or through `orch`, never over chat.

Token resolution, in order:
  1. $<watchdog.bot_token_env_var>          (process environment)
  2. the same key inside watchdog.env_file  (e.g. ~/.config/orch/env)
  3. nothing -> push falls back to a configured hermes binary if
     watchdog.hermes_push_fallback is true; poll simply does nothing.

A NOTE ON BOT TOKENS: only one process may long-poll a given bot token. If
another gateway already polls the token used for push, give this framework a
SECOND bot for polling — otherwise the two steal each other's updates.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config_parser import load, Config          # noqa: E402
from state_resolver import Resolver             # noqa: E402

TRIGGERS = {"status", "st", "s", "ok", "ok?", "up?", "?"}
HELP_WORDS = {"/start", "help", "hello", "hi", "/help"}


# --- config helpers --------------------------------------------------------
def env_get(cfg: Config, key: str) -> str:
    if os.environ.get(key):
        return os.environ[key].strip()
    envf = Path(os.path.expanduser(cfg.data["watchdog"]["env_file"]))
    if not envf.is_file():
        return ""
    try:
        for line in envf.read_text(errors="replace").splitlines():
            line = line.strip()
            if line.startswith(key + "="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except Exception:
        pass
    return ""


def token(cfg: Config) -> str:
    return env_get(cfg, cfg.data["watchdog"]["bot_token_env_var"])


def armed(cfg: Config) -> bool:
    w = cfg.data["watchdog"]
    if not w["enabled"]:
        return False
    return (cfg.control / "WATCHDOG_ARMED").exists()


def in_quiet_hours(cfg: Config, now: Optional[str] = None) -> bool:
    w = cfg.data["watchdog"]["quiet_hours"]
    qf, qt = (w.get("from") or ""), (w.get("to") or "")
    if not qf or not qt:
        return False
    now = now or datetime.now().strftime("%H:%M")
    if qf < qt:                       # window inside one day
        return qf <= now < qt
    return now >= qf or now < qt      # window crosses midnight


# --- transport -------------------------------------------------------------
def api(cfg: Config, method: str, **params) -> dict:
    tok = token(cfg)
    if not tok:
        return {}
    url = f"https://api.telegram.org/bot{tok}/{method}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(url, timeout=25) as r:
            return json.loads(r.read().decode())
    except Exception:
        return {}


def send(cfg: Config, chat_id, text: str) -> bool:
    # Telegram caps a message at 4096 chars.
    out = api(cfg, "sendMessage", chat_id=chat_id, text=text[:4000])
    return bool(out.get("ok"))


def hermes_push(cfg: Config, chat: str, text: str) -> bool:
    """Fallback route: an existing hermes gateway already wired to Telegram."""
    if not cfg.data["watchdog"]["hermes_push_fallback"]:
        return False
    hb = cfg.binary("hermes", "") or shutil.which("hermes") or os.path.expanduser("~/.local/bin/hermes")
    if not (hb and os.access(hb, os.X_OK)):
        return False
    try:
        subprocess.run([hb, "send", "-t", f"telegram:{chat}", text],
                       capture_output=True, timeout=25)
        return True
    except Exception:
        return False


# --- push ------------------------------------------------------------------
def do_push(cfg: Config, title: str, body: str, force: bool = False) -> int:
    if not force:
        if not armed(cfg):
            return 0
        if in_quiet_hours(cfg):
            return 0
    chat = str(cfg.data["watchdog"]["telegram_chat_id"] or "")
    if not chat:
        return 0
    text = f"{cfg.name} — {title}\n{body}"
    if token(cfg) and send(cfg, chat, text):
        return 0
    hermes_push(cfg, chat, text)
    return 0


# --- poll ------------------------------------------------------------------
def snapshot(cfg: Config) -> str:
    r = Resolver(cfg)
    parts = [r.text_summary()[:2400]]
    win = r.focus_window()
    if win:
        pane = r.capture(win, 14)
        if pane:
            parts.append(f"--- window {win} (tail) ---")
            parts.append(pane[:900])
        else:
            parts.append(f"(window {win} not reachable — tmux attach -t {cfg.tmux_session})")
    else:
        names = " ".join(sorted(r.windows)) or f"(tmux session {cfg.tmux_session} not running)"
        parts.append("--- tmux windows ---")
        parts.append(names)
    return "\n".join(parts)


def offset_path(cfg: Config) -> Path:
    return cfg.state / "telegram.offset"


def do_poll(cfg: Config) -> int:
    """One polling pass. Silent when unconfigured; safe to run from cron."""
    if not token(cfg):
        return 0
    allowed = str(cfg.data["watchdog"]["status_bot_allowed_user"] or "")
    if not allowed:
        return 0
    try:
        offset = int(offset_path(cfg).read_text().strip() or "0")
    except Exception:
        offset = 0
    data = api(cfg, "getUpdates", offset=offset, timeout=0, limit=20)
    if not data.get("ok"):
        return 0
    max_id = offset
    for upd in data.get("result", []):
        max_id = max(max_id, upd.get("update_id", 0) + 1)
        msg = upd.get("message") or {}
        text = (msg.get("text") or "").strip().lower()
        from_id = str((msg.get("from") or {}).get("id", ""))
        chat_id = (msg.get("chat") or {}).get("id")
        if from_id != allowed or chat_id is None:
            continue
        if text in TRIGGERS:
            send(cfg, chat_id, snapshot(cfg))
        elif text in HELP_WORDS:
            send(cfg, chat_id,
                 "I answer one thing: send 'status' and I reply with a live snapshot "
                 f"of {cfg.name} (lane state + the running window's tail). "
                 "I am read-only — I never touch gates or sentinels.")
        # anything else: silence, by design
    if max_id > offset:
        try:
            offset_path(cfg).parent.mkdir(parents=True, exist_ok=True)
            offset_path(cfg).write_text(str(max_id))
        except Exception:
            pass
    return 0


def do_daemon(cfg: Config) -> int:
    interval = int(cfg.data["watchdog"]["poll_interval"])
    print(f"[bot] polling every {interval}s for {cfg.name} "
          f"(instance {cfg.instance}) — Ctrl-C to stop")
    if not token(cfg):
        print(f"[bot] no token in ${cfg.data['watchdog']['bot_token_env_var']} "
              f"or {cfg.data['watchdog']['env_file']} — idling (nothing will be answered)")
    while True:
        try:
            do_poll(cfg)
        except Exception as e:                       # never die on one bad pass
            print(f"[bot] poll error (continuing): {e}")
        time.sleep(interval)


def do_doctor(cfg: Config) -> int:
    w = cfg.data["watchdog"]
    print(f"instance          {cfg.instance}")
    print(f"watchdog.enabled  {w['enabled']}")
    print(f"WATCHDOG_ARMED    {(cfg.control / 'WATCHDOG_ARMED').exists()}")
    print(f"chat id set       {bool(w['telegram_chat_id'])}")
    print(f"allowed user set  {bool(w['status_bot_allowed_user'])}")
    print(f"token var         ${w['bot_token_env_var']}")
    print(f"token found       {'yes' if token(cfg) else 'NO'}")
    print(f"quiet hours       {w['quiet_hours']['from'] or '-'} .. {w['quiet_hours']['to'] or '-'}"
          f"   (now inside: {in_quiet_hours(cfg)})")
    print(f"hermes fallback   {w['hermes_push_fallback']}")
    print(f"push would send   {armed(cfg) and not in_quiet_hours(cfg) and bool(w['telegram_chat_id'])}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="telegram_bot")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("push"); p.add_argument("--title", required=True)
    p.add_argument("--body", default=""); p.add_argument("--force", action="store_true")
    sub.add_parser("poll")
    sub.add_parser("daemon")
    sub.add_parser("test")
    sub.add_parser("doctor")
    a = ap.parse_args()
    cfg = load()
    if a.cmd == "push":
        return do_push(cfg, a.title, a.body, a.force)
    if a.cmd == "poll":
        return do_poll(cfg)
    if a.cmd == "daemon":
        return do_daemon(cfg)
    if a.cmd == "doctor":
        return do_doctor(cfg)
    print(snapshot(cfg))          # test: compose, never send
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
