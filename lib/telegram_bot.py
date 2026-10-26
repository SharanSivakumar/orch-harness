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
          navigation.

          It also takes the Owner's ANSWER to a page (watchdog.answers): from
          the allowed user only, "<row-id> <LETTER> [note]", or a bare
          "<LETTER> [note]" sent as a Telegram reply to a page message. Valid
          only while the row is halted (ROUTE_A, BLOCKED after its unblockers,
          HALTED) and, when the row's PAGE.txt lists lettered options, only for
          one of those letters. A valid answer writes control/ANSWER_<row>.json
          and nothing else; the engine dispatches an answer session that applies
          it (the bot itself never resets, releases or dispatches anything).
          Everything else is ignored.

Quiet hours HOLD pages (state/held_pages.jsonl); the first push or engine tick
after the window ends sends them, oldest first (collapsed into one message when
more than 3). Pages that carry a row are recorded in state/pages.jsonl
(Telegram message_id -> row) so a reply to them can be mapped.

          v3: "why <row>" answers with `orch why` (read-only, lib/why.py), and
          "spend <id> A|B" (or the letter as a reply to a spend page) answers a
          spend request the same way (control/SPEND_<id>.json, lib/spend.py).
          "revert <k>" (steward.telegram_revert) seals the request in a memfd
          this process holds and rings control/REVERT_<k>.json; the engine
          reads it through /proc (proving the bot wrote it) and runs the
          revert of STEWARD_LOG line k when it is one orch command (lib/revert.py).

The bot NEVER writes a sentinel, releases a gate, or sends a keystroke into a
pane. Operator actions happen in tmux or through `orch`; what chat can do is
record an answer or a revert file for the engine.

Token resolution, in order:
  1. $<watchdog.bot_token_env_var>          (process environment)
  2. the same key inside watchdog.env_file  (default ~/.config/orch/env)
  3. nothing -> push simply does nothing; poll idles.

A NOTE ON BOT TOKENS: only one process may long-poll a given bot token. If
another gateway already polls the token used for push, give this framework a
SECOND bot for polling — otherwise the two steal each other's updates.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
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
HELD = "held_pages.jsonl"
PAGES = "pages.jsonl"
ELIGIBLE = ("route_a", "blocked", "halted")   # Store.phase values a page answer may resolve
TEST_ROW = "TEST"
ANSWER_LINE = 'answer: reply "{row} <letter>" or reply to this message with the letter (or via Remote Control)'
OPTION_RE = re.compile(r"^\s*(?:[-*]\s*)?\**\(?([A-Z])\)?\**\s*[.):\u2014-]", re.M)


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


def _hm(now) -> str:
    """now: None (wall clock), an epoch number, or an 'HH:MM' string."""
    if isinstance(now, str):
        return now
    return datetime.fromtimestamp(now if now is not None else time.time()).strftime("%H:%M")


def in_quiet_hours(cfg: Config, now=None) -> bool:
    w = cfg.data["watchdog"]["quiet_hours"]
    qf, qt = (w.get("from") or ""), (w.get("to") or "")
    if not qf or not qt:
        return False
    now = _hm(now)
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


def send_msg(cfg: Config, chat_id, text: str, reply_to: Optional[int] = None) -> dict:
    """-> the sent message (with message_id) or {}. Telegram caps a message at 4096 chars."""
    params = {"chat_id": chat_id, "text": text[:4000]}
    if reply_to:
        params["reply_to_message_id"] = reply_to
    out = api(cfg, "sendMessage", **params)
    return (out.get("result") or {"ok": True}) if out.get("ok") else {}


def send(cfg: Config, chat_id, text: str) -> bool:
    return bool(send_msg(cfg, chat_id, text))


# --- push ------------------------------------------------------------------
INBOX_LINE = 'answer: reply "inbox {k} <letter>" or reply to this message with the letter'
INBOX_RE = re.compile(r"\s*inbox\s+#?([0-9]+)\s+([A-Za-z])(?:[\s.:,-]+(.*))?\s*", re.I | re.S)
SPEND_LINE = 'answer: reply "spend {id} A" or "spend {id} B", or reply to this message with the letter'
SPEND_RE = re.compile(r"spend\s+#?((?:sp)?[0-9]+)\s+([ab])(?:[\s.:,-]+(.*))?", re.I | re.S)


def compose(cfg: Config, title: str, body: str, row: str = "") -> str:
    text = f"{cfg.name} — {title}\n{body}"
    if row.startswith("spend:"):     # a spend page (lib/spend.py) is answered `spend <id> A|B`
        rid = row[len("spend:"):]
        if cfg.data["watchdog"].get("answers", False) and (cfg.data.get("spend") or {}).get("telegram_answers", True):
            return text + "\n" + SPEND_LINE.format(id=rid)
        return text + f"\nanswer on the host: orch spend answer {rid} A|B (no bot reads Telegram answers on this rig)"
    if row.startswith("inbox:"):     # a stall page (lib/steward.py idle_watch) is answered `inbox <k> <letter>`
        k = row[len("inbox:"):]
        if 'reply "inbox' in body:
            return text
        if cfg.data["watchdog"].get("inbox_answers", False):
            return text + "\n" + INBOX_LINE.format(k=k)
        return text + f"\nanswer on the host: orch steward inbox done {k} <LETTER>"
    if row and cfg.data["watchdog"].get("answers", False):
        text += "\n" + ANSWER_LINE.format(row=row)
    return text


def _append_jsonl(path: Path, rec: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.write(json.dumps(rec, sort_keys=True) + "\n")
        fcntl.flock(f, fcntl.LOCK_UN)


def record_page(cfg: Config, msg: dict, row: str, title: str) -> None:
    mid = msg.get("message_id")
    if row and mid is not None:
        _append_jsonl(cfg.state / PAGES, {"message_id": mid, "row": row, "title": title[:200],
                                          "epoch": int(time.time())})


def page_row(cfg: Config, message_id) -> str:
    try:
        lines = (cfg.state / PAGES).read_text().splitlines()
    except Exception:
        return ""
    for line in reversed(lines):
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if rec.get("message_id") == message_id:
            return str(rec.get("row", ""))
    return ""


def _deliver(cfg: Config, chat: str, title: str, text: str, row: str = "") -> bool:
    msg = send_msg(cfg, chat, text) if token(cfg) else {}
    if msg:
        record_page(cfg, msg, row, title)
        return True
    return False


def hold(cfg: Config, title: str, body: str, row: str = "", now=None) -> None:
    _append_jsonl(cfg.state / HELD, {"title": title, "body": body, "row": row,
                                     "epoch": int(now if isinstance(now, (int, float)) else time.time())})


def flush_held(cfg: Config, now=None) -> int:
    """Send the pages held during quiet hours, oldest first; more than 3 collapse into one
    message (count + each title + its first body line). -> pages sent."""
    f = cfg.state / HELD
    if not f.exists() or not armed(cfg) or in_quiet_hours(cfg, now):
        return 0
    chat = str(cfg.data["watchdog"]["telegram_chat_id"] or "")
    if not chat:
        return 0
    with open(f, "r+") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        recs = []
        for line in fh.read().splitlines():
            try:
                recs.append(json.loads(line))
            except Exception:
                pass
        recs.sort(key=lambda r: r.get("epoch", 0))
        ok = True
        n_all = len(recs)
        # a spend page is answered by its own text (A/B lines, `spend <id>`): never collapsed
        for r in [r for r in recs if str(r.get("row", "")).startswith("spend:")]:
            ok = _deliver(cfg, chat, r.get("title", ""), compose(cfg, r.get("title", ""), r.get("body", ""), r["row"]),
                          r["row"]) and ok
        recs = [r for r in recs if not str(r.get("row", "")).startswith("spend:")]
        if len(recs) > 3:
            lines = [f"{len(recs)} pages held during quiet hours (oldest first):"]
            for r in recs:
                first = (str(r.get("body", "")).strip().splitlines() or [""])[0]
                lines.append(f"- {r.get('title', '')}: {first[:160]}")
            rows = sorted({r["row"] for r in recs if r.get("row")})
            if rows:
                lines.append('answer: reply "<row> <letter>" — rows: ' + " ".join(rows))
            ok = _deliver(cfg, chat, "held pages", f"{cfg.name} — held pages\n" + "\n".join(lines))
        else:
            for r in recs:
                ok = _deliver(cfg, chat, r.get("title", ""),
                              compose(cfg, r.get("title", ""), r.get("body", ""), r.get("row", "")),
                              r.get("row", "")) and ok
        if ok:
            fh.seek(0)
            fh.truncate()
        fcntl.flock(fh, fcntl.LOCK_UN)
    if ok:
        f.unlink(missing_ok=True)
    return n_all if ok else 0


def do_push(cfg: Config, title: str, body: str, force: bool = False, row: str = "", now=None,
            page_class: str = "") -> int:
    quiet = in_quiet_hours(cfg, now)
    if quiet and page_class and page_class in (cfg.data["watchdog"].get("quiet_bypass") or []):
        force = force or armed(cfg)      # watchdog.quiet_bypass: the Owner's named classes go out at night too
    if not force:
        if not armed(cfg):
            return 0
        if quiet:
            hold(cfg, title, body, row, now)
            return 0
    chat = str(cfg.data["watchdog"]["telegram_chat_id"] or "")
    if not chat:
        return 0
    if not quiet:
        flush_held(cfg, now)
    _deliver(cfg, chat, title, compose(cfg, title, body, row), row)
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


# --- answers -----------------------------------------------------------------
def row_folder(cfg: Config, rid: str) -> Optional[Path]:
    from rows import find_row, plan_dir
    r = find_row(cfg, rid)
    return plan_dir(cfg, r) if r else None


def page_letters(cfg: Config, rid: str) -> list:
    if rid == TEST_ROW:
        return ["A", "B"]
    d = row_folder(cfg, rid)
    try:
        text = (d / "PAGE.txt").read_text(errors="replace") if d else ""
    except Exception:
        text = ""
    return sorted(set(OPTION_RE.findall(text)))


def known_row(cfg: Config, word: str) -> str:
    if word.upper() == TEST_ROW:
        return TEST_ROW
    from rows import load_rows
    _, by_id = load_rows(cfg)
    if word in by_id:
        return word
    low = {k.lower(): k for k in by_id}
    return low.get(word.lower(), "")


def parse_answer(cfg: Config, raw: str, reply_to) -> Optional[tuple]:
    """-> (row, LETTER, note) or None when the text is not shaped like an answer."""
    m = re.fullmatch(r"\s*(\S+)\s+([A-Za-z])(?:[\s.:,-]+(.*))?\s*", raw, re.S)
    if m and len(m.group(1)) > 1:
        rid = known_row(cfg, m.group(1))
        if rid:
            return rid, m.group(2).upper(), (m.group(3) or "").strip()
    m = re.fullmatch(r"\s*([A-Za-z])(?:[\s.:,-]+(.*))?\s*", raw, re.S)
    if m and reply_to is not None:
        rid = page_row(cfg, reply_to)
        if rid:
            return rid, m.group(1).upper(), (m.group(2) or "").strip()
    return None


def answer_ok(cfg: Config, rid: str, letter: str) -> str:
    """'' when the answer may be recorded, else why not."""
    letters = page_letters(cfg, rid)
    if letters and letter not in letters:
        return f"{rid}'s page offers {'/'.join(letters)}, not {letter}"
    if rid == TEST_ROW:
        return ""
    from store import Store
    ph = Store(cfg.state).phase(rid)
    if ph not in ELIGIBLE:
        return f"{rid} is not waiting for an answer (phase {ph or 'none'})"
    if (cfg.control / f"ANSWER_{rid}.json").exists():
        return f"an answer for {rid} is already pending"
    return ""


def _atomic_json(path: Path, rec: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(rec, indent=1, sort_keys=True))
    os.replace(tmp, path)


def handle_answer(cfg: Config, chat_id, msg: dict, raw: str) -> bool:
    """True when the message was an answer (recorded or refused with a reply)."""
    if not cfg.data["watchdog"].get("answers", False):
        return False
    reply_to = (msg.get("reply_to_message") or {}).get("message_id")
    got = parse_answer(cfg, raw, reply_to)
    if not got:
        return False
    rid, letter, note = got
    why = answer_ok(cfg, rid, letter)
    if why:
        send_msg(cfg, chat_id, f"not recorded: {why}", reply_to=msg.get("message_id"))
        return True
    rec = {"row": rid, "letter": letter, "note": note[:1000],
           "from_id": str((msg.get("from") or {}).get("id", "")), "message_id": msg.get("message_id"),
           "epoch": int(time.time())}
    if rid == TEST_ROW:
        _atomic_json(cfg.state / "test_answer.json", rec)
        send_msg(cfg, chat_id, f"test answer received: {letter}", reply_to=msg.get("message_id"))
        return True
    _atomic_json(cfg.control / f"ANSWER_{rid}.json", rec)
    who = answer_executor(cfg)
    send_msg(cfg, chat_id, f"recorded: {rid} = {letter}; an {who} session will apply it",
             reply_to=msg.get("message_id"))
    return True


def handle_inbox_answer(cfg: Config, chat_id, msg: dict, raw: str) -> bool:
    """watchdog.inbox_answers: `inbox <k> <letter> [note]`, or the letter as a reply to a stall page, writes
    control/INBOX_ANSWER_<k>.json and nothing else (the engine closes the item; the Steward wakes on it).
    True when the message was an inbox answer (recorded or refused with a reply)."""
    if not cfg.data["watchdog"].get("inbox_answers", False):
        return False
    m = INBOX_RE.fullmatch(raw)
    if m:
        k, letter, note = int(m.group(1)), m.group(2).upper(), (m.group(3) or "").strip()
    else:
        reply_to = (msg.get("reply_to_message") or {}).get("message_id")
        m = re.fullmatch(r"\s*([A-Za-z])(?:[\s.:,-]+(.*))?\s*", raw, re.S)
        row = page_row(cfg, reply_to) if (m and reply_to is not None) else ""
        if not row.startswith("inbox:") or not row[len("inbox:"):].isdigit():
            return False
        k, letter, note = int(row[len("inbox:"):]), m.group(1).upper(), (m.group(2) or "").strip()
    import steward
    item = {int(i["k"]): i for i in steward.inbox_items(cfg)}.get(k)
    why = ""
    if item is None:
        why = f"no open inbox item {k}"
    else:
        letters = steward.item_letters(item.get("body", ""))
        if letters and letter not in letters:
            why = f"inbox {k} offers {'/'.join(letters)}, not {letter}"
        elif (cfg.control / f"INBOX_ANSWER_{k}.json").exists():
            why = f"an answer for inbox {k} is already pending"
    if why:
        send_msg(cfg, chat_id, f"not recorded: {why}", reply_to=msg.get("message_id"))
        return True
    _atomic_json(cfg.control / f"INBOX_ANSWER_{k}.json",
                 {"k": k, "letter": letter, "note": note[:1000], "from_id": str((msg.get("from") or {}).get("id", "")),
                  "message_id": msg.get("message_id"), "epoch": int(time.time())})
    send_msg(cfg, chat_id, f"recorded: inbox {k} = {letter}; the engine closes it and the Steward picks it up within minutes",
             reply_to=msg.get("message_id"))
    return True


def handle_why(cfg: Config, chat_id, word: str) -> None:
    """`why <row>` — read-only, the same text as `orch why` (lib/why.py)."""
    try:
        import why
        from engine import Rig
        rid = known_row(cfg, word) or word
        send(cfg, chat_id, why.explain(Rig(cfg), rid)[:3800])
    except Exception as e:
        send(cfg, chat_id, f"why {word}: failed ({type(e).__name__})")


def handle_revert(cfg: Config, chat_id, msg: dict, k: int) -> None:
    """`revert <k>` from the allowed user: control/REVERT_<k>.json for the engine (lib/revert.py), then wait for
    its answer. The bot runs nothing itself."""
    import revert
    mid = msg.get("message_id")
    p, fd, why = revert.request(cfg, k, msg)
    if p is None:
        send_msg(cfg, chat_id, f"revert #{k} not recorded: {why}", reply_to=mid)
        return
    wait = float((cfg.data.get("steward") or {}).get("revert_wait", 60))
    try:
        ans = revert.wait_answer(cfg, p, wait)     # the sealed request lives in this process: keep it open
        if ans.get("outcome") == "timeout":
            revert.withdraw(cfg, k, None, door_too=True)
            ans = revert.wait_answer(cfg, p, 3)     # an engine mid-run when the bot gave up still answers
    finally:
        revert.withdraw(cfg, k, fd, door_too=False)
    out = ans.get("outcome")
    if out == "done":
        text = f"REVERTED #{k}: {ans.get('revert')} — {ans.get('why')}"
    elif out == "manual":
        text = f"#{k}: nothing ran — {ans.get('why')}"
    elif out == "timeout":
        text = (f"revert #{k}: the engine did not answer in {int(wait)}s — is it running? The request is withdrawn "
                f"(nothing ran: `orch revert` lists it). Send it again, or `orch revert {k}` on the host")
    else:
        text = f"revert #{k} REFUSED: {ans.get('why')}"
    send_msg(cfg, chat_id, text[:3800], reply_to=mid)


def handle_spend(cfg: Config, chat_id, msg: dict, rid: str, letter: str, note: str = "") -> None:
    """`spend <id> A|B` (or the letter as a reply to a spend page) from the allowed user: the answer sealed
    in a memfd this process holds, the doorbell control/SPEND_<id>.json; the engine proves and applies it
    (lib/spend.py) and answers sealed. The bot itself never raises a cap."""
    import revert
    import spend
    mid = msg.get("message_id")
    rid = rid if rid.lower().startswith("sp") else f"sp{rid}"
    rid = rid.lower()
    p, fd, why = spend.bot_request(cfg, rid, letter.upper(), msg, note)
    if p is None:
        send_msg(cfg, chat_id, f"spend {rid} {letter.upper()} not recorded: {why}", reply_to=mid)
        return
    wait = float((cfg.data.get("spend") or {}).get("answer_wait", 60))
    try:
        ans = revert.wait_answer(cfg, p, wait, name=spend.ANS_MEMFD)
        if ans.get("outcome") == "timeout":
            (cfg.control / f"SPEND_{rid}.json").unlink(missing_ok=True)
            ans = revert.wait_answer(cfg, p, 3, name=spend.ANS_MEMFD)
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
    if ans.get("outcome") == "done":
        text = str(ans.get("why"))
    elif ans.get("outcome") == "timeout":
        text = (f"spend {rid}: the engine did not answer in {int(wait)}s — is it running? Nothing changed; send it "
                f"again, or `orch spend answer {rid} {letter.upper()}` on the host")
    else:
        text = f"spend {rid} {letter.upper()} REFUSED: {ans.get('why')}"
    send_msg(cfg, chat_id, text, reply_to=mid)


def spend_reply(cfg: Config, chat_id, msg: dict, raw: str) -> bool:
    """A bare letter sent as a reply to a spend page. True when it was one."""
    rto = msg.get("reply_to_message") or {}
    reply_to = rto.get("message_id")
    if reply_to is None:
        return False
    rid = page_row(cfg, reply_to)
    m = re.fullmatch(r"\s*([A-Za-z])(?:[\s.:,-]+(.*))?\s*", raw, re.S)
    if not rid.startswith("spend:") or not m:
        return False
    rid = rid[len("spend:"):]
    # bound to what the Owner read: the replied-to message must be this request's page as the engine
    # would write it now (a page someone else pushed under the id, or a stale one, is refused)
    import spend
    rec = spend.load_request(cfg, rid)
    want = [l for l in spend.page_text(cfg, rec)[1].splitlines() if l.startswith(("A)", "Cap "))] if rec else []
    text = str(rto.get("text", ""))
    if not want or any(l not in text for l in want):
        send_msg(cfg, chat_id, f"spend {rid}: that message is not this request's current page — answer with "
                               f"'spend {rid} A|B' after checking `orch spend status`", reply_to=msg.get("message_id"))
        return True
    handle_spend(cfg, chat_id, msg, rid, m.group(1), (m.group(2) or "").strip())
    return True


def answer_executor(cfg: Config) -> str:
    w, ex = cfg.data["watchdog"], cfg.data["executors"]
    chain = [x for x in (ex.get("unblock_executors") or []) if x]
    return w.get("answer_executor") or (chain[-1] if chain else ex["unblock_executor"])


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
        raw = (msg.get("text") or "").strip()
        text = raw.lower()
        from_id = str((msg.get("from") or {}).get("id", ""))
        chat_id = (msg.get("chat") or {}).get("id")
        if from_id != allowed or chat_id is None:
            continue
        if text in TRIGGERS:
            send(cfg, chat_id, snapshot(cfg))
        elif text in HELP_WORDS:
            send(cfg, chat_id,
                 "Send 'status' and I reply with a live snapshot "
                 f"of {cfg.name} (lane state + the running window's tail). "
                 "To answer a page: reply to it with the letter, or send '<row> <letter> [note]'. "
                 "'why <row>': its phase, blocker and last events. "
                 "'revert <k>': undo the Steward's act #k (STEWARD_LOG; the engine runs it when it is one orch "
                 "command, else I send it back for you to do). "
                 "'spend <id> A|B': answer a spend page (A raises the cap by what it names, B holds). "
                 "'inbox <k> <letter> [note]': answer the Steward's inbox item k (or reply to a stall page). "
                 "I never touch gates or sentinels; an answer or a revert only records a file the engine acts on.")
        elif re.fullmatch(r"why\s+\S+", text):
            handle_why(cfg, chat_id, raw.split(None, 1)[1].strip())
        elif re.fullmatch(r"revert\s+#?[0-9]+", text):
            handle_revert(cfg, chat_id, msg, int(text.split()[1].lstrip("#")))
        elif SPEND_RE.fullmatch(raw.strip()):
            m = SPEND_RE.fullmatch(raw.strip())
            handle_spend(cfg, chat_id, msg, m.group(1), m.group(2), (m.group(3) or "").strip())
        elif spend_reply(cfg, chat_id, msg, raw):
            pass
        elif handle_inbox_answer(cfg, chat_id, msg, raw):
            pass
        else:
            handle_answer(cfg, chat_id, msg, raw)
        # anything else: silence, by design
    if max_id > offset:
        try:
            offset_path(cfg).parent.mkdir(parents=True, exist_ok=True)
            offset_path(cfg).write_text(str(max_id))
        except Exception:
            pass
    return 0


def do_test(cfg: Config) -> int:
    """Compose the status snapshot (printed) and send one answerable test page as row TEST
    (letters A/B). A reply writes state/test_answer.json and dispatches nothing."""
    print(snapshot(cfg))
    chat = str(cfg.data["watchdog"]["telegram_chat_id"] or "")
    if not (chat and token(cfg)):
        print("[bot] test page NOT sent: no chat id or no token (orch bot doctor)")
        return 0
    title = "test page"
    body = ("This is a test of answering pages.\nA) it works\nB) it works too\n"
            "Reply to this message with A or B (or send 'TEST A').")
    ok = _deliver(cfg, chat, title, compose(cfg, title, body, TEST_ROW), TEST_ROW)
    print(f"[bot] test page {'sent' if ok else 'NOT sent'}; the reply is read by the running bot daemon "
          f"(orch bot start) and lands in state/test_answer.json")
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
    print(f"push would send   {armed(cfg) and not in_quiet_hours(cfg) and bool(w['telegram_chat_id'])}")
    print(f"held pages        {sum(1 for _ in open(cfg.state / HELD)) if (cfg.state / HELD).exists() else 0}")
    print(f"answers           {w.get('answers', False)} (executor {answer_executor(cfg)})")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="telegram_bot")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("push"); p.add_argument("--title", required=True)
    p.add_argument("--body", default=""); p.add_argument("--force", action="store_true")
    p.add_argument("--row", default=""); p.add_argument("--class", dest="page_class", default="")
    sub.add_parser("flush")
    sub.add_parser("poll")
    sub.add_parser("daemon")
    sub.add_parser("test")
    sub.add_parser("doctor")
    a = ap.parse_args()
    cfg = load()
    if a.cmd == "push":
        return do_push(cfg, a.title, a.body, a.force, a.row, page_class=a.page_class)
    if a.cmd == "flush":
        flush_held(cfg)
        return 0
    if a.cmd == "poll":
        return do_poll(cfg)
    if a.cmd == "daemon":
        return do_daemon(cfg)
    if a.cmd == "doctor":
        return do_doctor(cfg)
    return do_test(cfg)


if __name__ == "__main__":
    raise SystemExit(main())
