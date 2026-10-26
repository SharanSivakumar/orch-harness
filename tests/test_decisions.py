#!/usr/bin/env python3
"""Tests for lib/decisions.py — run: python3 -m unittest tests/test_decisions.py"""
from __future__ import annotations

import contextlib
import io
import os
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest import mock

LIB = Path(__file__).resolve().parent.parent / "lib"
sys.path.insert(0, str(LIB))
import decisions as d  # noqa: E402

LEDGER = textwrap.dedent("""\
    # DECISIONS — Demo

    Preamble. Rows append entries (`D-nn`), never rewrite one.

    | id | ruling |
    |---|---|
    | D-01 | **Purpose.** Build the thing. More text follows. |
    | D-02 | **Spend.** The cap is zero for every row. |
    | D-03 | A plain ruling without a bold lead, stated in full. |

    Trailing note for D-03.

    ## Ladder one — rulings of 2026-10-01

    ### D-04 — RULED (Owner, 2026-10-01) — the first heading ruling
    Body of four says many things about the first rule.

    ### `D-05` — **PROVISIONAL (row x, 2026-10-02)**: replace four
    **ELI5.** This entry supersedes D-04. It is the new rule for everyone.

    ### D-06 — RATIFIED (2026-10-02) — amends two
    Amends D-02 for rows `x*` only. The rest stands.

    ### D-07 — RULED — reopens three
    This reopens D-03 for a second look at the whole thing.

    ### D-08 — RULED — negations
    This does not supersede D-06. A later row would overrule D-07 if asked.
    Overrule by a new entry naming D-07.

    ### D-09 — PROPOSED — partial
    This supersedes D-01 (2)'s cap and replaces D-06 for hk rows only.

    ## Ladder two

    ```text
    ### D-99 — fenced, not an entry
    ```

    ### D-04 — RULED (2026-10-03) — duplicate id, second use
    Second body of the doubled id.

    #### Notes
    Still part of the second D-04.

    ### D-10 — RULED — annotated
    Body of ten is long enough to be a sentence.

    > **Annotation (2026-10-04):** SUPERSEDED by D-11.

    ### D-11 — RECORDED — withdraws seven
    This withdraws D-07. Clause (b) of D-06 is untouched.
    | D-12 | **Standalone.** An appended table row after a heading entry. |

    ### D-13 — no status word here
    Plain body that mentions D-50 which does not exist.
    """)

LEVEL2 = textwrap.dedent("""\
    # DECISIONS — Cycle X

    - E-01 charter — RATIFIED
    - E-02 thing — PROVISIONAL

    ## E-01 — RATIFIED — Charter
    Charter body is here and it is binding.

    ## `E-02` — PROVISIONAL — the second
    Intro sentence here today for the second one.

    ## ELI5
    Explained in plain words.

    ### `F-09` is SUPERSEDED on one point
    Still the body of E-02.

    # Part two — merged

    ## E-03 — RULED — third
    Third body with enough words in it.
    """)


def run(argv):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = d.main(argv)
    return rc, buf.getvalue()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.src = self.dir / "DECISIONS.md"
        self.src.write_text(LEDGER)
        self.out = self.dir / "decisions"

    def tearDown(self):
        self.tmp.cleanup()

    def entries(self):
        return d.load_entries(self.src)

    def one(self, key):
        hits = [e for e in self.entries() if e.key == key]
        self.assertEqual(len(hits), 1, key)
        return hits[0]


class ParseTest(Base):
    def test_entries_and_order(self):
        keys = [e.key for e in self.entries()]
        self.assertEqual(keys, ["D-01", "D-02", "D-03", "D-04#1", "D-05", "D-06", "D-07", "D-08",
                                "D-09", "D-04#2", "D-10", "D-11", "D-12", "D-13"])

    def test_fenced_heading_is_not_an_entry(self):
        self.assertFalse([e for e in self.entries() if e.id == "D-99"])

    def test_table_rows(self):
        e1 = self.one("D-01")
        self.assertEqual((e1.kind, e1.title, e1.lead), ("table", "Purpose", "Build the thing."))
        e3 = self.one("D-03")
        self.assertIn("Trailing note for D-03.", e3.text)
        self.assertEqual(e3.line_end, 11)
        self.assertEqual(self.one("D-12").kind, "table")
        self.assertNotIn("D-12", self.one("D-11").text.splitlines()[-1])

    def test_heading_metadata(self):
        e = self.one("D-05")
        self.assertEqual((e.status, e.date, e.title), ("PROVISIONAL", "2026-10-02", "replace four"))
        self.assertEqual(e.lead, "This entry supersedes D-04.")
        self.assertEqual(self.one("D-04#1").section, "Ladder one — rulings of 2026-10-01")
        self.assertEqual(self.one("D-04#2").section, "Ladder two")
        self.assertEqual(self.one("D-13").status, "")

    def test_deeper_heading_stays_in_body(self):
        self.assertIn("#### Notes", self.one("D-04#2").text)

    def test_duplicates_numbered(self):
        dups = [e for e in self.entries() if e.id == "D-04"]
        self.assertEqual([(e.occurrence, e.dup_count) for e in dups], [(1, 2), (2, 2)])

    def test_level2_entries_with_section_like_subheadings(self):
        es = d.parse(LEVEL2)
        self.assertEqual([e.id for e in es], ["E-01", "E-02", "E-03"])
        e2 = es[1]
        self.assertIn("## ELI5", e2.text)
        self.assertIn("F-09", e2.text)
        self.assertEqual(e2.title, "the second")
        self.assertEqual(es[2].section, "Part two — merged")


class SupersessionTest(Base):
    def test_supersede_vs_amend_vs_reopen(self):
        a = self.one("D-04#1")
        self.assertEqual((a.state, a.state_source, a.superseded_by), ("superseded", "inferred", ["D-05"]))
        self.assertIn("supersedes D-04", a.why)
        self.assertEqual(self.one("D-04#2").state, "active")
        self.assertEqual((self.one("D-02").state, self.one("D-02").amended_by), ("amended", ["D-06"]))
        self.assertEqual(self.one("D-03").state, "reopened")

    def test_partial_supersession_keeps_entry_active(self):
        self.assertEqual(self.one("D-01").state, "amended")
        e6 = self.one("D-06")
        self.assertTrue(e6.active)
        self.assertIn("D-09", e6.amended_by)
        kinds = {(r.kind, r.target) for r in self.one("D-09").refs}
        self.assertEqual(kinds, {("supersedes-part", "D-01"), ("supersedes-part", "D-06")})

    def test_negations_and_bare_infinitives_ignored(self):
        self.assertEqual(self.one("D-08").refs, [])

    def test_withdraw_and_annotation(self):
        e7 = self.one("D-07")
        self.assertEqual((e7.state, e7.superseded_by), ("withdrawn", ["D-11"]))
        e10 = self.one("D-10")
        self.assertEqual((e10.state, e10.superseded_by), ("superseded", ["D-11"]))

    def test_status_tsv_wins(self):
        self.out.mkdir()
        (self.out / "STATUS.tsv").write_text(
            "# id\tstatus\tby\tnote\nD-04#1\tactive\t\tstill binding\n"
            "D-06\twithdrawn\tD-11\tpulled\n")
        a = self.one("D-04#1")
        self.assertEqual((a.state, a.state_source, a.why), ("active", "manual", "still binding"))
        e6 = self.one("D-06")
        self.assertEqual((e6.state, e6.superseded_by), ("withdrawn", ["D-11"]))
        # a withdrawn entry amends nothing
        self.assertEqual(self.one("D-02").state, "active")

    def test_set_status_cli(self):
        rc, out = run(["set-status", "D-05", "superseded", "--by", "D-11", "--note", "x",
                       "--file", str(self.src)])
        self.assertEqual(rc, 0)
        self.assertEqual(self.one("D-05").state_source, "manual")
        lines = (self.out / "STATUS.tsv").read_text().splitlines()
        self.assertEqual(lines[-1].split("\t"), ["D-05", "superseded", "D-11", "x"])
        with contextlib.redirect_stderr(io.StringIO()):
            rc, _ = run(["set-status", "D-77", "active", "--file", str(self.src)])
        self.assertEqual(rc, 2)


class CommandsTest(Base):
    def test_index(self):
        rc, out = run(["index", "--file", str(self.src)])
        self.assertEqual(rc, 0)
        self.assertIn("DUPLICATE D-04", out)
        rows = [r.split("\t") for r in (self.out / "INDEX.tsv").read_text().splitlines()]
        self.assertEqual(rows[0], d.INDEX_COLS)
        self.assertEqual(len(rows), 15)
        col = {c: i for i, c in enumerate(rows[0])}
        d4 = [r for r in rows if r[0] == "D-04"]
        self.assertEqual([r[col["occurrence"]] for r in d4], ["1", "2"])
        self.assertEqual({r[col["duplicate"]] for r in d4}, {"yes"})
        self.assertEqual((d4[0][col["state"]], d4[0][col["state_source"]], d4[0][col["superseded_by"]]),
                         ("superseded", "inferred", "D-05"))
        d5 = next(r for r in rows if r[0] == "D-05")
        self.assertEqual(d5[col["refs_out"]], "supersedes:D-04")

    def test_digest(self):
        rc, out = run(["digest", "--file", str(self.src), "--out", str(self.out)])
        self.assertEqual(rc, 0)
        self.assertIn("source_bytes=", out)
        text = (self.out / "ACTIVE.md").read_text()
        self.assertIn("orch gd show <id>", text)
        sections = [ln for ln in text.splitlines() if ln.startswith("## ")]
        self.assertEqual(sections[0], "## Ladder two")
        self.assertTrue(sections[-1].startswith("## Superseded / withdrawn"))
        main, gone = text.split("## Superseded / withdrawn")
        self.assertNotIn("**D-04#1**", main)
        self.assertIn("**D-04#2**", main)
        self.assertIn("D-04#1→D-05", gone)
        self.assertIn("D-07→D-11", gone)
        self.assertRegex(main, r"\*\*D-02\*\* \(\S*\) \[amended by D-06\]")

    def test_digest_since_and_truncation(self):
        run(["digest", "--since", "D-10", "--max-chars", "40", "--file", str(self.src)])
        text = (self.out / "ACTIVE.md").read_text()
        self.assertNotIn("**D-06**", text)
        self.assertIn("**D-13**", text)
        for ln in text.splitlines():
            if ln.startswith("- **"):
                self.assertLessEqual(len(ln.split(") ", 1)[1]), 41)

    def test_show_duplicate(self):
        rc, out = run(["show", "D-04", "--file", str(self.src)])
        self.assertEqual(rc, 0)
        self.assertIn("occurrence 1 of 2", out)
        self.assertIn("occurrence 2 of 2", out)
        self.assertIn("Body of four", out)
        self.assertIn("Second body", out)
        self.assertIn("superseded by D-05", out)
        rc, out = run(["show", "D-04#2", "--file", str(self.src)])
        self.assertNotIn("Body of four", out)
        with contextlib.redirect_stderr(io.StringIO()):
            rc, _ = run(["show", "D-77", "--file", str(self.src)])
        self.assertEqual(rc, 1)

    def test_entry_text(self):
        both = d.entry_text(self.src, "D-04")
        self.assertIn("<!-- D-04 [occurrence 1 of 2] -->", both)
        self.assertIn("Second body", both)
        self.assertEqual(d.entry_text(self.src, "D-06").splitlines()[0],
                         "### D-06 — RATIFIED (2026-10-02) — amends two")

    def test_grep(self):
        _, out = run(["grep", "body", "--file", str(self.src)])
        self.assertIn("D-04#1", out)
        _, out = run(["grep", "body", "--active", "--file", str(self.src)])
        self.assertNotIn("D-04#1", out)
        self.assertIn("D-04#2", out)

    def test_check(self):
        rc, out = run(["check", "--file", str(self.src)])
        self.assertEqual(rc, 0)
        self.assertIn("DUPLICATE D-04", out)
        self.assertIn("UNKNOWN_MENTIONS", out)
        self.assertIn("D-50", out)
        self.assertRegex(out, r"NO_STATUS .*D-13")

    def test_instance_paths(self):
        ws = self.dir / "ws"
        inst = ws / "ORCH"
        inst.mkdir(parents=True)
        (inst / "orch.yaml").write_text("project:\n  name: t\n  workspace: ..\n")
        (ws / "DECISIONS.md").write_text(LEDGER)
        with mock.patch.dict(os.environ, {"ORCH_INSTANCE": str(inst)}):
            rc, _ = run(["index"])
        self.assertEqual(rc, 0)
        self.assertTrue((ws / "decisions" / "INDEX.tsv").is_file())


class NextIdTest(Base):
    def test_next_default_prefix_counts_mentions_and_reservations(self):
        _, out = run(["next", "--file", str(self.src)])
        self.assertEqual(out.strip(), "D-100")   # D-99 is mentioned (fenced); never collide
        self.out.mkdir()
        (self.out / "RESERVED.tsv").write_text("D-120\t2026-10-12T00:00:00+00:00\tx\n")
        _, out = run(["next", "--file", str(self.src)])
        self.assertEqual(out.strip(), "D-121")
        _, out = run(["next", "--prefix", "E", "--file", str(self.src)])
        self.assertEqual(out.strip(), "E-01")

    def test_reserve_records_line(self):
        _, out = run(["next", "--reserve", "a\ttitle", "--file", str(self.src)])
        self.assertEqual(out.strip(), "D-100")
        line = (self.out / "RESERVED.tsv").read_text().splitlines()[-1].split("\t")
        self.assertEqual((line[0], line[2]), ("D-100", "a title"))
        _, out = run(["next", "--reserve", "b", "--file", str(self.src)])
        self.assertEqual(out.strip(), "D-101")

    def test_concurrent_reservations_never_collide(self):
        go = self.dir / "go"
        script = textwrap.dedent(f"""\
            import sys, time, pathlib
            sys.path.insert(0, {str(LIB)!r})
            import decisions as d
            go = pathlib.Path({str(go)!r})
            while not go.exists():
                time.sleep(0.005)
            for i in range(20):
                print(d.reserve(pathlib.Path({str(self.src)!r}), pathlib.Path({str(self.out)!r}),
                                "D", "p" + sys.argv[1]))
            """)
        procs = [subprocess.Popen([sys.executable, "-c", script, str(n)], stdout=subprocess.PIPE,
                                  text=True) for n in (1, 2)]
        time.sleep(0.3)
        go.touch()
        got = []
        for p in procs:
            out, _ = p.communicate(timeout=60)
            self.assertEqual(p.returncode, 0)
            got += out.split()
        self.assertEqual(len(got), 40)
        self.assertEqual(len(set(got)), 40)
        self.assertEqual(sorted(int(x.split("-")[1]) for x in got), list(range(100, 140)))
        lines = (self.out / "RESERVED.tsv").read_text().splitlines()
        self.assertEqual(len(lines), 40)


class ApiTest(Base):
    def test_plan_rulings(self):
        plan = textwrap.dedent("""\
            # PLAN
            Rulings: DEC-489, DEC-493
            **Binding rulings:** D-12 D-14
            - **Rulings**: D-01 / D-02 and DEC-489
            These rulings: DEC-1 are prose, not a field.
            Ruling: none
            """)
        self.assertEqual(d.plan_rulings(plan),
                         ["DEC-489", "DEC-493", "D-12", "D-14", "D-01", "D-02"])
        self.assertTrue(d.PLAN_RULINGS.search("Rulings: DEC-489"))
        self.assertIsNone(d.PLAN_RULINGS.search("These rulings: DEC-1"))

    def test_binding_block(self):
        block = d.binding_block(self.dir, ["D-06", "D-04", "D-77", "D-06"])
        self.assertTrue(block.startswith("### Binding rulings"))
        self.assertIn("(3 id(s))", block)
        self.assertIn("Amends D-02 for rows `x*` only.", block)
        self.assertIn("Body of four", block)
        self.assertIn("Second body", block)
        self.assertIn("used 2× in the ledger", block)
        self.assertIn("marked superseded (inferred) by D-05", block)
        self.assertIn("`D-77` — NOT FOUND", block)
        self.assertEqual(d.binding_block(self.dir, []), "")

    def test_load_entries_reads_status_beside_ledger(self):
        self.out.mkdir()
        (self.out / "STATUS.tsv").write_text("D-13\tsuperseded\tD-11\tmanual\n")
        e = next(x for x in d.load_entries(self.src) if x.id == "D-13")
        self.assertEqual((e.state, e.state_source), ("superseded", "manual"))


if __name__ == "__main__":
    unittest.main()
