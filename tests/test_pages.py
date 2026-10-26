#!/usr/bin/env python3
"""Pages: quiet hours HOLD (not drop) and the PAGE.txt body of halt / Route A pages.

Throwaway instance, Telegram transport stubbed in-process (no network), clock injected.

    python3 -m unittest tests/test_pages.py -v
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path

HOME = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HOME / "lib"))

SBX = Path(tempfile.mkdtemp(prefix="orch-pages-"))
WS = SBX / "cycle"
INST = WS / "ORCH"


def setUpModule():
    WS.mkdir(parents=True)
    env = dict(os.environ, ORCH_INSTANCE=str(INST))
    env.pop("ORCH_ROW", None)
    subprocess.run([str(HOME / "bin" / "orch"), "init", str(INST), "--name", "pages", "--session", f"orchpg{os.getpid()}"],
                   capture_output=True, text=True, env=env, cwd=str(WS), check=True)
    import yaml
    f = INST / "orch.yaml"
    data = yaml.safe_load(f.read_text())
    data["watchdog"].update({"enabled": True, "telegram_chat_id": "42", 
                             "quiet_hours": {"from": "22:00", "to": "07:00"}})
    f.write_text(yaml.safe_dump(data, sort_keys=False))
    (INST / "control").mkdir(exist_ok=True)
    (INST / "control" / "WATCHDOG_ARMED").write_text("1\n")
    d = WS / "PHASE_P" / "p1"
    d.mkdir(parents=True)
    (d / "PLAN.md").write_text("# PLAN — p1\n")
    (INST / "queue_P.tsv").write_text("p1\t-\t0\tmock\tPHASE_P/p1/PLAN.md\n")
    os.environ["ORCH_INSTANCE"] = str(INST)


def tearDownModule():
    shutil.rmtree(SBX, ignore_errors=True)


class Pages(unittest.TestCase):
    def setUp(self):
        import telegram_bot as tb
        from config_parser import load
        self.tb = tb
        self.cfg = load(str(INST))
        self.sent = []
        self._orig = (tb.send_msg, tb.token)
        tb.send_msg = lambda cfg, chat, text, reply_to=None: self.sent.append(text) or {"message_id": len(self.sent)}
        tb.token = lambda cfg: "stub-token"
        (INST / "state" / tb.HELD).unlink(missing_ok=True)

    def tearDown(self):
        self.tb.send_msg, self.tb.token = self._orig

    def test_quiet_hours_hold_then_flush(self):
        tb, cfg = self.tb, self.cfg
        self.assertTrue(tb.in_quiet_hours(cfg, "23:30"))
        self.assertFalse(tb.in_quiet_hours(cfg, "08:00"))
        tb.do_push(cfg, "first", "body one\nmore", now="23:30")
        tb.do_push(cfg, "second", "body two", now="02:00")
        self.assertEqual(self.sent, [], "nothing goes out inside quiet hours")
        self.assertEqual(len((INST / "state" / tb.HELD).read_text().splitlines()), 2, "pages are held, not dropped")
        self.assertEqual(tb.flush_held(cfg, now="03:00"), 0, "no flush while still quiet")
        # the first push after the window sends the held pages first, oldest first, then itself
        tb.do_push(cfg, "third", "body three", now="07:30")
        self.assertEqual(len(self.sent), 3)
        self.assertIn("first", self.sent[0])
        self.assertIn("second", self.sent[1])
        self.assertIn("third", self.sent[2])
        self.assertFalse((INST / "state" / tb.HELD).exists())

    def test_flush_collapses_more_than_three(self):
        tb, cfg = self.tb, self.cfg
        for i in range(5):
            tb.hold(cfg, f"page {i}", f"line {i}\nsecond line", now=1000 + i)
        self.assertEqual(tb.flush_held(cfg, now="09:00"), 5)
        self.assertEqual(len(self.sent), 1, "more than 3 held pages collapse into one message")
        msg = self.sent[0]
        self.assertIn("5 pages held", msg)
        self.assertLess(msg.index("page 0"), msg.index("page 4"))
        self.assertIn("line 3", msg)
        self.assertNotIn("second line", msg, "only each body's first line")

    def test_failed_send_keeps_held(self):
        tb, cfg = self.tb, self.cfg
        tb.send_msg = lambda cfg, chat, text, reply_to=None: {}
        tb.hold(cfg, "kept", "body", now=1)
        self.assertEqual(tb.flush_held(cfg, now="09:00"), 0)
        self.assertTrue((INST / "state" / tb.HELD).exists(), "a page that could not be sent stays held")

    def test_force_push_ignores_quiet_hours(self):
        tb, cfg = self.tb, self.cfg
        tb.do_push(cfg, "forced", "body", force=True, now="23:00")
        self.assertEqual(len(self.sent), 1)


class PageBody(unittest.TestCase):
    def test_page_txt_body_and_fallback(self):
        from config_parser import load
        from engine import Engine
        me = types.SimpleNamespace(cfg=load(str(INST)))
        page = WS / "PHASE_P" / "p1" / "PAGE.txt"
        page.unlink(missing_ok=True)
        body = Engine.page_body(me, "p1", None, "x" * 900)
        self.assertEqual(body.splitlines()[0], "x" * 400, "no PAGE.txt: the summary, capped at 400")
        self.assertTrue(body.endswith("answer: p1 — reply via Remote Control to a Claude Code session"))
        page.write_text("The scan found hits.\nA) follow-up row (recommended)\nB) stop\n" + "y" * 900)
        body = Engine.page_body(me, "p1", None, "summary text")
        self.assertTrue(body.startswith("The scan found hits."))
        self.assertNotIn("summary text", body)
        self.assertEqual(len(body.rsplit("\nanswer:", 1)[0]), 700, "PAGE.txt capped at 700 chars")
        self.assertTrue(body.endswith("answer: p1 — reply via Remote Control to a Claude Code session"))


class Answers(unittest.TestCase):
    """watchdog.answers: the Owner's Telegram reply becomes control/ANSWER_<row>.json (bot) and an
    answer session (engine). Off by default; stubbed transport, throwaway instance."""

    def setUp(self):
        import telegram_bot as tb
        from config_parser import load
        from store import Store
        self.tb, self.cfg = tb, load(str(INST))
        self.cfg.data["watchdog"]["answers"] = True
        self.st = Store(self.cfg.state)
        self.sent = []
        self._orig = (tb.send_msg, tb.token)
        tb.send_msg = lambda cfg, chat, text, reply_to=None: self.sent.append(text) or {"message_id": 100 + len(self.sent)}
        tb.token = lambda cfg: "stub-token"
        for f in [self.cfg.state / tb.PAGES, self.cfg.state / "test_answer.json", self.cfg.state / "p1.result.json"] \
                + list(self.cfg.control.glob("ANSWER_*")):
            f.unlink(missing_ok=True)
        (WS / "PHASE_P" / "p1" / "PAGE.txt").write_text("Scan found hits.\nA) follow-up row (recommended)\nB) stop\n")

    def tearDown(self):
        self.tb.send_msg, self.tb.token = self._orig

    def msg(self, text, reply_to=None, frm="7"):
        m = {"message_id": 900, "text": text, "from": {"id": frm}, "chat": {"id": 42}}
        if reply_to is not None:
            m["reply_to_message"] = {"message_id": reply_to}
        return m

    def test_off_by_default(self):
        from config_parser import load
        cfg = load(str(INST))
        self.assertFalse(cfg.data["watchdog"]["answers"])
        self.assertFalse(self.tb.handle_answer(cfg, 42, self.msg("p1 A"), "p1 A"))
        self.assertEqual(list(cfg.control.glob("ANSWER_*")), [])

    def test_page_reply_records_answer_for_halted_row(self):
        tb, cfg = self.tb, self.cfg
        tb.do_push(cfg, "p1 ROUTE_A", "Scan found hits.", force=True, row="p1")
        self.assertIn('answer: reply "p1 <letter>"', self.sent[-1])
        mid = 100 + len(self.sent)
        self.assertEqual(tb.page_row(cfg, mid), "p1")
        # not halted yet: refused, nothing written
        self.assertTrue(tb.handle_answer(cfg, 42, self.msg("A", reply_to=mid), "A"))
        self.assertIn("not waiting", self.sent[-1])
        self.assertFalse((cfg.control / "ANSWER_p1.json").exists())
        self.st.write_result("p1", {"phase": "route_a", "status_mtime": 0})
        # a letter the page does not offer: refused
        tb.handle_answer(cfg, 42, self.msg("C", reply_to=mid), "C")
        self.assertIn("offers A/B", self.sent[-1])
        self.assertFalse((cfg.control / "ANSWER_p1.json").exists())
        # reply by letter to the page
        self.assertTrue(tb.handle_answer(cfg, 42, self.msg("a go ahead", reply_to=mid), "a go ahead"))
        import json
        rec = json.loads((cfg.control / "ANSWER_p1.json").read_text())
        self.assertEqual((rec["row"], rec["letter"], rec["note"]), ("p1", "A", "go ahead"))
        # a second answer while one is pending: refused
        tb.handle_answer(cfg, 42, self.msg("p1 B"), "p1 B")
        self.assertIn("already pending", self.sent[-1])

    def test_not_an_answer_is_ignored(self):
        self.assertFalse(self.tb.handle_answer(self.cfg, 42, self.msg("hello there"), "hello there"))
        self.assertFalse(self.tb.handle_answer(self.cfg, 42, self.msg("A"), "A"), "a bare letter needs a page to reply to")

    def test_test_row_round_trip_dispatches_nothing(self):
        tb, cfg = self.tb, self.cfg
        tb.handle_answer(cfg, 42, self.msg("TEST B"), "TEST B")
        self.assertIn("test answer received: B", self.sent[-1])
        self.assertTrue((cfg.state / "test_answer.json").exists())
        self.assertEqual(list(cfg.control.glob("ANSWER_*")), [])

    def test_engine_turns_answer_into_answer_session(self):
        from engine import Engine
        import json
        tb, cfg = self.tb, self.cfg
        self.st.write_result("p1", {"phase": "route_a", "status_mtime": 0})
        (cfg.control / "ANSWER_p1.json").write_text(json.dumps({"row": "p1", "letter": "A", "note": "", "epoch": 1}))
        me = types.SimpleNamespace(cfg=cfg, st=self.st, log=lambda *a: None, sheet_open=lambda *a: None)
        Engine.owner_answers(me)
        res = self.st.result("p1")
        self.assertEqual(res["phase"], "unblocking")
        self.assertEqual(res["answer"], "A")
        self.assertEqual(res["answer_exec"], tb.answer_executor(cfg))
        self.assertIn("answered 'A'", res["reason"])
        self.assertFalse((cfg.control / "ANSWER_p1.json").exists(), "the answer file is consumed")
        # an answer for a row that is not halted is refused and consumed, nothing else changes
        self.st.write_result("p1", {"phase": "accepted", "status_mtime": 0})
        (cfg.control / "ANSWER_p1.json").write_text(json.dumps({"row": "p1", "letter": "A", "epoch": 1}))
        Engine.owner_answers(me)
        self.assertEqual(self.st.phase("p1"), "accepted")
        self.assertTrue(list((cfg.state / "answers").glob("p1.*.refused.json")))

    def test_answers_off_engine_ignores_files(self):
        from engine import Engine
        from config_parser import load
        import json
        cfg = load(str(INST))
        self.st.write_result("p1", {"phase": "route_a", "status_mtime": 0})
        (cfg.control / "ANSWER_p1.json").write_text(json.dumps({"row": "p1", "letter": "A", "epoch": 1}))
        Engine.owner_answers(types.SimpleNamespace(cfg=cfg, st=self.st, log=lambda *a: None, sheet_open=lambda *a: None))
        self.assertEqual(self.st.phase("p1"), "route_a")
        (cfg.control / "ANSWER_p1.json").unlink()



class InboxAnswers(unittest.TestCase):
    """watchdog.inbox_answers (F5, (cadence)): `inbox <k> <LETTER> [note]`, or the letter as a reply
    to a stall page, becomes control/INBOX_ANSWER_<k>.json (bot); the engine closes the item with the answer (the
    Steward's wake_on_answer then picks it up). watchdog.quiet_bypass lets named page classes through quiet hours."""

    def setUp(self):
        import json
        import steward
        import telegram_bot as tb
        from config_parser import load
        from store import Store
        self.tb, self.steward, self.cfg = tb, steward, load(str(INST))
        self.cfg.data["watchdog"]["inbox_answers"] = True
        self.st = Store(self.cfg.state)
        self.rig = types.SimpleNamespace(cfg=self.cfg, st=self.st)
        self.sent = []
        self._orig = (tb.send_msg, tb.token)
        tb.send_msg = lambda cfg, chat, text, reply_to=None: self.sent.append(text) or {"message_id": 500 + len(self.sent)}
        tb.token = lambda cfg: "stub-token"
        sd = self.cfg.state / "steward"
        for f in [self.cfg.state / tb.PAGES, sd / "inbox.jsonl", sd / "inbox_n", self.cfg.state / tb.HELD] \
                + list(self.cfg.control.glob("INBOX_ANSWER_*")):
            f.unlink(missing_ok=True)
        self.k1 = steward.add_inbox(self.rig, 0, "P7 law route", "A) drop the law route\nB) wait for the fetch")
        self.k2 = steward.add_inbox(self.rig, 0, "CGI widening", "Options:\nA. pin U1\nB. hold")
        self.json = json

    def tearDown(self):
        self.tb.send_msg, self.tb.token = self._orig

    def msg(self, text, reply_to=None):
        m = {"message_id": 901, "text": text, "from": {"id": "7"}, "chat": {"id": 42}}
        if reply_to is not None:
            m["reply_to_message"] = {"message_id": reply_to}
        return m

    def test_off_by_default(self):
        from config_parser import load
        cfg = load(str(INST))
        self.assertFalse(cfg.data["watchdog"].get("inbox_answers", False))
        self.assertFalse(self.tb.handle_inbox_answer(cfg, 42, self.msg(f"inbox {self.k1} A"), f"inbox {self.k1} A"))
        self.assertEqual(list(cfg.control.glob("INBOX_ANSWER_*")), [])

    def test_answer_by_text_and_by_reply(self):
        tb, cfg, k1, k2 = self.tb, self.cfg, self.k1, self.k2
        raw = f"inbox {k1} a  drop it, cheapest"
        self.assertTrue(tb.handle_inbox_answer(cfg, 42, self.msg(raw), raw))
        rec = self.json.loads((cfg.control / f"INBOX_ANSWER_{k1}.json").read_text())
        self.assertEqual((rec["k"], rec["letter"], rec["note"]), (k1, "A", "drop it, cheapest"))
        self.assertIn(f"inbox {k1} = A", self.sent[-1])
        # a second answer while one is pending: refused
        tb.handle_inbox_answer(cfg, 42, self.msg(f"inbox {k1} B"), f"inbox {k1} B")
        self.assertIn("already pending", self.sent[-1])
        # a letter the item does not offer: refused
        tb.handle_inbox_answer(cfg, 42, self.msg(f"inbox {k2} C"), f"inbox {k2} C")
        self.assertIn("offers A/B", self.sent[-1])
        self.assertFalse((cfg.control / f"INBOX_ANSWER_{k2}.json").exists())
        # no such item / a closed item: refused
        tb.handle_inbox_answer(cfg, 42, self.msg("inbox 99 A"), "inbox 99 A")
        self.assertIn("no open inbox item 99", self.sent[-1])
        # the stall page carries the inbox row, so a bare letter as a reply answers that item
        tb.do_push(cfg, "STALLED", "Why: x\nDo: y", force=True, row=f"inbox:{k2}")
        self.assertIn(f'reply "inbox {k2} <letter>"', self.sent[-1])
        mid = 500 + len(self.sent)
        self.assertTrue(tb.handle_inbox_answer(cfg, 42, self.msg("b", reply_to=mid), "b"))
        rec = self.json.loads((cfg.control / f"INBOX_ANSWER_{k2}.json").read_text())
        self.assertEqual((rec["k"], rec["letter"]), (k2, "B"))
        # not an inbox answer: left to the other handlers
        self.assertFalse(tb.handle_inbox_answer(cfg, 42, self.msg("status"), "status"))
        self.assertFalse(tb.handle_inbox_answer(cfg, 42, self.msg("A"), "A"), "a bare letter needs an inbox page")

    def test_engine_closes_the_item_with_the_answer(self):
        cfg, k1, k2 = self.cfg, self.k1, self.k2
        (cfg.control / f"INBOX_ANSWER_{k1}.json").write_text(self.json.dumps({"k": k1, "letter": "A", "note": "go", "epoch": 1}))
        self.steward.consume_inbox_answers(self.rig)
        it = {i["k"]: i for i in self.steward.inbox_items(cfg, open_only=False)}[k1]
        self.assertEqual((it["status"], it["answer"], it["note"], it["done_by"]), ("done", "A", "go", "telegram"))
        self.assertFalse((cfg.control / f"INBOX_ANSWER_{k1}.json").exists(), "consumed")
        # an answer for an item that is already done: refused, consumed, nothing changes
        (cfg.control / f"INBOX_ANSWER_{k1}.json").write_text(self.json.dumps({"k": k1, "letter": "B", "epoch": 2}))
        self.steward.consume_inbox_answers(self.rig)
        it = {i["k"]: i for i in self.steward.inbox_items(cfg, open_only=False)}[k1]
        self.assertEqual(it["answer"], "A")
        self.assertTrue(list((cfg.state / "answers").glob(f"inbox{k1}.*.refused.json")))
        # off: the file waits, nothing is closed
        cfg.data["watchdog"]["inbox_answers"] = False
        (cfg.control / f"INBOX_ANSWER_{k2}.json").write_text(self.json.dumps({"k": k2, "letter": "A", "epoch": 3}))
        self.steward.consume_inbox_answers(self.rig)
        self.assertEqual([i["k"] for i in self.steward.inbox_items(cfg)], [k2])
        (cfg.control / f"INBOX_ANSWER_{k2}.json").unlink()

    def test_quiet_bypass_by_page_class(self):
        tb, cfg = self.tb, self.cfg
        cfg.data["watchdog"]["quiet_bypass"] = ["stall"]
        tb.do_push(cfg, "STALLED", "body", now="23:30", page_class="stall")
        self.assertEqual(len(self.sent), 1, "a bypass class goes out in quiet hours")
        tb.do_push(cfg, "IDLE", "body", now="23:30", page_class="idle")
        self.assertEqual(len(self.sent), 1, "any other class is held")
        self.assertTrue((cfg.state / tb.HELD).exists())

if __name__ == "__main__":
    unittest.main()
