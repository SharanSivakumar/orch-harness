#!/usr/bin/env python3
"""Decisions ledger: cheap, targeted access to a huge, append-only DECISIONS.md.

Every project keeps its rulings in `<workspace>/DECISIONS.md`. The file is the
archive and is NEVER rewritten — rows append, annotate, and that is all. Two
things went wrong with that alone:

  * every agent session was told to read the whole file (a large ledger can
    reach megabytes), which burns most of a context window on rulings that
    were superseded long ago;
  * ids were allocated by hand («next free number at append time»), so two
    sessions appending at once could take the same id (duplicate ids in one
    ledger).

This tool leaves the archive alone and builds derived files next to it, in
`<workspace>/decisions/`:

  INDEX.tsv     one row per entry occurrence: where it is, its date, status,
                section, title, the ids it supersedes / amends, who supersedes
                it, and whether its id is used more than once;
  ACTIVE.md     a compact digest of the entries still in force, one line each,
                newest section first — what a session reads instead of the
                archive; `show <id>` prints the full text when it matters;
  RESERVED.tsv  ids handed out by `next --reserve` under an exclusive flock, so
                two concurrent planners never get the same id;
  STATUS.tsv    manual status overrides (`set-status`), which win over anything
                the parser infers.

What counts as an entry (the three real ledgers use all of these):

  * a Markdown heading of any level whose text STARTS with an id —
    `### DEC-493 — …`, `## DEC-12 — PROVISIONAL — …`, `## \\`DEC-426\\` — …`;
    it runs until the next entry, or until a non-entry heading of a HIGHER
    level (fewer `#`), which opens a new section. Non-entry headings at the
    entry's own level or deeper (`## ELI5` inside a `## DEC-253`) are body;
  * a table row whose first cell is exactly an id — `| DEC-07 | ruling |` —
    when its table's header cell reads `id`, or when the row stands alone (an
    append after a heading entry). Prose after the last row, up to the next
    heading or table, belongs to that row;
  * an id is an entry id only when its prefix heads >= 3 entries or is the most
    common one, so `### \\`DEC-09\\` is SUPERSEDED …` inside an older entry
    stays body. Headings inside ``` fences are ignored.

Supersession is inferred conservatively, because a wrong «superseded» hides a
binding ruling from every digest reader (the worst direction to be wrong in):

  * full: B's text says `supersedes|replaces|overrules|overrides|withdraws A`
    with nothing between verb and id but «the / entry / ruling …», and nothing
    qualifying after it (end of clause, `)`, `in full`). A is inactive;
  * partial: `supersedes DEC-23 (2)'s …`, `… A for these rows`, `the X part
    of A`. A stays ACTIVE and is marked amended — partial supersession is
    what most real entries do;
  * `amends` / `reopens` never make A inactive; A is marked amended/reopened;
  * an entry's own annotation `SUPERSEDED by B` / `WITHDRAWN by B` (whole-clause,
    not «`o4` SUPERSEDED by …») marks it inactive;
  * negated or hypothetical verbs (`does not supersede`, `would overrule`,
    bare infinitives such as «overrule by a new entry naming …»), passive
    citations (`WITHDRAWN (DEC-542)`) and `retire` are ignored;
  * an entry that is itself withdrawn (heading or STATUS.tsv) supersedes
    nothing. STATUS.tsv (`id[#occ]<TAB>status<TAB>by<TAB>note`) always wins.

usage: decisions.py <next|index|digest|show|grep|set-status|check> … [--file F] [--out D]
Paths come from orch.yaml (`load()` → workspace) unless --file/--out are given.
"""
from __future__ import annotations

import argparse
import fcntl
import os
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))

LEDGER = "DECISIONS.md"
OUT_DIR = "decisions"
INACTIVE = ("superseded", "withdrawn")
MANUAL_STATES = ("superseded", "withdrawn", "active", "amended")

ID = r"[A-Z][A-Z0-9]*-\d+"
ID_RE = re.compile(rf"(?<![A-Za-z0-9_\-])({ID})(?![0-9A-Za-z])")
HEADING = re.compile(r"^(#{1,6})[ \t]+(.*?)\s*#*\s*$")
ENTRY_HEAD = re.compile(rf"^(#{{1,6}})[ \t]+(?:[*_`\[]+[ \t]*)?({ID})(?![0-9A-Za-z])(.*)$")
TABLE_ROW = re.compile(rf"^\s*\|\s*(?:[*_`]+\s*)?({ID})(?![0-9A-Za-z])\s*[*_`]*\s*\|(.*)$")
TABLE_HDR = re.compile(r"^\s*\|\s*(?:[*_`]+)?\s*(id|ids|ruling id|decision|#)\s*(?:[*_`]+)?\s*\|", re.I)
FENCE = re.compile(r"^\s*(```|~~~)")
RULE_LINE = re.compile(r"^\s*(-{3,}|\*{3,}|_{3,})\s*$")
DATE = re.compile(r"\b(20\d\d-[01]\d-[0-3]\d)\b")
STATUS = re.compile(
    r"(?<![A-Za-z])(?<!not )(?<!Not )(?<!NOT )"
    r"(RULED|RATIFIED|RECORDED|PROPOSED|PROVISIONAL|SUPERSEDED|WITHDRAWN|OVERRULED|"
    r"REJECTED|CONFIRMED|DECIDED|UNSIGNED|ACCEPTED|ADOPTED|ROUTE [AB]|Route [AB]|OPEN)"
    r"(?![A-Za-z0-9])")
SELF_INACTIVE = {"SUPERSEDED": "superseded", "OVERRULED": "superseded",
                 "WITHDRAWN": "withdrawn", "REJECTED": "withdrawn"}

# -- supersession grammar ---------------------------------------------------
SUP_ACTIVE = re.compile(
    r"\b(supersedes|superseding|replaces|replacing|overrules|overruling|overrides|"
    r"overriding|overrode|withdraws|withdrawing|withdrew)\b", re.I)
SUP_PERFECT = re.compile(
    r"\b(?:has|have|had)\s+(?:also\s+|now\s+|already\s+)?"
    r"(superseded|replaced|overruled|overridden|withdrawn)\b", re.I)
SUP_PASSIVE = re.compile(
    rf"\b(superseded|overruled|overridden|replaced|withdrawn)\s+(?:in\s+(?:full|effect)\s+)?"
    rf"by\s+[`*_]*({ID})(?![0-9A-Za-z])", re.I)
AMEND_ACTIVE = re.compile(
    r"\b(amends|amending|amendments?\s+(?:to|of)|reopens|reopening|re-opens|re-opening)\b", re.I)
AMEND_PASSIVE = re.compile(
    rf"\b(amended|reopened|re-opened)\s+by\s+[`*_]*({ID})(?![0-9A-Za-z])", re.I)
NEGATION = re.compile(
    r"\b(not|never|no longer|cannot|unless|if|whether|would|could|may|might|should|nor)\b|n't\b",
    re.I)
GAP_STOP = re.compile(r"[(;:.!?|—–]|\bby\b", re.I)
FULL_GAP_WORDS = {"the", "entry", "entries", "ruling", "rulings", "also", "hereby", "earlier",
                  "prior", "previous", "old", "older", "both", "outright", "entire", "whole",
                  "now", "thereby", "decision"}
FULL_POST = re.compile(
    r"^\s*($|[.,;:)!?]|[—–]|-\s|in full\b|entirely\b|wholly\b|outright\b|in its entirety\b|"
    r"and\b|which\b|that\b|"
    r"\((?:ruling|option|decision|commit)\b[^)]*\)|"          # a label: «A (ruling C)»
    r"(?:ruling|entry)\b(?!\s*\(?[0-9a-z]{1,3}\)))", re.I)
LIST_NEXT = re.compile(rf"^[\s`*_]*(?:,|/|&|\band\b|\bor\b)[\s`*_]*({ID})(?![0-9A-Za-z])")
NEG_BOUND = re.compile(r"[—–;:(.!?\n]")
CLAUSE_BOUND = re.compile(r"[—–:;(.!?>\n]")
SELF_PREFIX_WORDS = {"this", "entry", "ruling", "it", "is", "was", "now", "has", "been",
                     "hereby", "status", "annotation", "and", "in", "full", "effect", "therefore"}


@dataclass
class Ref:
    kind: str          # supersedes | supersedes-part | withdraws | withdraws-part | amends | reopens
    target: str
    snippet: str


@dataclass
class Entry:
    id: str
    kind: str          # heading | table
    level: int
    line_start: int    # 1-based, inclusive
    line_end: int
    section: str
    head: str          # the heading line (or the table row) as written
    text: str = ""
    occurrence: int = 1
    dup_count: int = 1
    nbytes: int = 0
    date: str = ""
    status: str = ""
    title: str = ""
    lead: str = ""
    refs: List[Ref] = field(default_factory=list)
    self_refs: List[Ref] = field(default_factory=list)   # «SUPERSEDED by B» on itself
    mentions: List[str] = field(default_factory=list)
    state: str = "active"
    state_source: str = ""
    superseded_by: List[str] = field(default_factory=list)
    amended_by: List[str] = field(default_factory=list)
    why: str = ""

    @property
    def prefix(self) -> str:
        return self.id.rsplit("-", 1)[0]

    @property
    def num(self) -> int:
        return int(self.id.rsplit("-", 1)[1])

    @property
    def key(self) -> str:
        return self.id if self.dup_count == 1 else f"{self.id}#{self.occurrence}"

    @property
    def active(self) -> bool:
        return self.state not in INACTIVE


# -- small text helpers -----------------------------------------------------
def one_line(s: str, limit: int = 0) -> str:
    s = re.sub(r"\s+", " ", s.replace("\t", " ")).strip()
    if limit and len(s) > limit:
        s = s[: limit - 1].rstrip() + "…"
    return s


def strip_md(s: str) -> str:
    s = re.sub(r"^\s*(?:>\s*)+", "", s)
    s = re.sub(r"^\s*(?:[-*+]|\d+[.)])\s+", "", s)
    return s.replace("**", "").replace("__", "").replace("`", "")


SENT_END = re.compile(r"(?<!e\.g)(?<!i\.e)(?<!\bvs)(?<!\bno)(?<!\bnr)[.!?](?=\s+[A-Z0-9«\"(]|\s*$)")
LABEL = re.compile(r"^\s*(?:\(\w{1,3}\)\s*)?\*\*[^*]{1,40}?[.:]\*\*\s*")


def sentences(s: str) -> List[str]:
    s, out, pos = one_line(s), [], 0
    for m in SENT_END.finditer(s):
        out.append(s[pos:m.end()].strip())
        pos = m.end()
    if s[pos:].strip():
        out.append(s[pos:].strip())
    return out


def first_sentence(s: str) -> str:
    return (sentences(s) or [""])[0]


def is_stamp(sentence: str) -> bool:
    """«Owner ruling (2026-10-12, …) — RULED.»: a status stamp, not the ruling."""
    if not STATUS.search(sentence):
        return False
    core = re.sub(r"\([^)]*\)", " ", DATE.sub(" ", STATUS.sub(" ", sentence)))
    return len(re.findall(r"[A-Za-z]{2,}", core)) < 4


def lead_sentence(paras: List[str]) -> str:
    """The first substantive sentence of the first paragraphs (labels, stamps skipped)."""
    for para in paras[:4]:
        for sent in sentences(strip_md(LABEL.sub("", para))):
            if not is_stamp(sent):
                return sent
    return ""


def norm_prose(text: str) -> str:
    """Join hard-wrapped lines (quote markers dropped); blank lines stay breaks."""
    lines = [re.sub(r"^\s*(?:>\s*)+", "", ln) for ln in text.splitlines()]
    return re.sub(r"(?<!\n)\n(?!\n)", " ", "\n".join(lines))


# -- parsing ----------------------------------------------------------------
def entry_prefixes(lines: Sequence[str]) -> set:
    counts: Counter = Counter()
    fence = False
    for line in lines:
        if FENCE.match(line):
            fence = not fence
            continue
        if fence:
            continue
        m = ENTRY_HEAD.match(line) or TABLE_ROW.match(line)
        if m:
            counts[m.group(2 if m.re is ENTRY_HEAD else 1).rsplit("-", 1)[0]] += 1
    if not counts:
        return set()
    top = counts.most_common(1)[0][0]
    return {p for p, n in counts.items() if n >= 3} | {top}


def parse(text: str, prefixes: Optional[set] = None) -> List[Entry]:
    """Split a ledger into entries (no state resolution; see load_entries)."""
    lines = text.splitlines()
    if prefixes is None:
        prefixes = entry_prefixes(lines)
    entries: List[Entry] = []
    cur: Optional[Entry] = None
    section = ""
    fence = prev_table = hdr_ok = False
    table_start = 0

    def finish(e: Optional[Entry], end: int) -> None:   # end: 0-based exclusive
        if e is None:
            return
        last = end - 1
        while last > e.line_start - 1 and (not lines[last].strip() or RULE_LINE.match(lines[last])):
            last -= 1
        e.line_end = last + 1
        e.text = "\n".join(lines[e.line_start - 1: e.line_end])
        e.nbytes = len(e.text.encode()) + 1
        entries.append(e)

    for i, line in enumerate(lines):
        if FENCE.match(line):
            fence = not fence
            prev_table = False
            continue
        if fence:
            continue
        is_table = line.lstrip().startswith("|")
        if is_table and not prev_table:
            table_start, hdr_ok = i, bool(TABLE_HDR.match(line))
        h = HEADING.match(line)
        if h:
            level = len(h.group(1))
            m = ENTRY_HEAD.match(line)
            if m and m.group(2).rsplit("-", 1)[0] in prefixes:
                finish(cur, i)
                cur = Entry(m.group(2), "heading", level, i + 1, i + 1, section, line)
            elif cur is None or cur.kind == "table" or level < cur.level:
                finish(cur, i)
                cur = None
                section = one_line(strip_md(h.group(2)), 160)
        elif is_table:
            m = TABLE_ROW.match(line)
            if m and m.group(1).rsplit("-", 1)[0] in prefixes and (i == table_start or hdr_ok):
                cut = table_start if cur is not None and cur.kind == "heading" else i
                finish(cur, cut)
                cur = Entry(m.group(1), "table", 0, i + 1, i + 1, section, line)
            elif i == table_start and cur is not None and cur.kind == "table":
                finish(cur, i)   # a new, non-entry table ends the previous row's trailing note
                cur = None
        prev_table = is_table
    finish(cur, len(lines))
    _number(entries)
    for e in entries:
        _describe(e)
        _refs(e, prefixes)
    return entries


def _number(entries: List[Entry]) -> None:
    seen: Dict[str, int] = Counter()
    for e in entries:
        seen[e.id] += 1
        e.occurrence = seen[e.id]
    for e in entries:
        e.dup_count = seen[e.id]


def _describe(e: Entry) -> None:
    """date, status, title and the lead sentence."""
    body = e.text.splitlines()[1:]
    early = [ln for ln in body if ln.strip()][:6]
    if e.kind == "heading":
        rest = ENTRY_HEAD.match(e.head).group(3)
    else:
        rest = TABLE_ROW.match(e.head).group(2).rstrip().rstrip("|")
    m = DATE.search(rest) or DATE.search("\n".join(early))
    e.date = m.group(1) if m else ""
    m = STATUS.search(rest[:300]) or STATUS.search("\n".join(early))
    e.status = m.group(1).upper() if m else ""
    head_core = re.sub(r"^[\s`*_\]]*[—–:|\-]*[\s*_`]*", "", rest)
    first = STATUS.match(head_core)
    if first and first.group(1).upper() in SELF_INACTIVE:
        e.state, e.state_source = SELF_INACTIVE[first.group(1).upper()], "heading"

    if e.kind == "heading":
        e.title = _clean_title(rest)
        paras: List[str] = []
        cur: List[str] = []
        for ln in body[:60]:
            if not ln.strip() or HEADING.match(ln) or ln.lstrip().startswith("|"):
                if cur:
                    paras.append(" ".join(cur))
                    cur = []
                continue
            cur.append(re.sub(r"^\s*(?:>\s*)+", "", ln))
        if cur:
            paras.append(" ".join(cur))
        e.lead = lead_sentence(paras)
    else:
        cell = rest.strip()
        b = re.match(r"^\*\*(.+?)\*\*\s*(.*)$", cell)
        if b:
            e.title = one_line(strip_md(b.group(1))).rstrip(".:")
            e.lead = lead_sentence([b.group(2)])
        else:
            e.lead = lead_sentence([cell])


def _clean_title(rest: str) -> str:
    t = one_line(rest.replace("**", "").replace("__", "").replace("`", ""))
    t = re.sub(r"^[\s\]*_]*[—–:|\-]*\s*", "", t)
    for _ in range(2):   # «RULED (the Owner, chat, 2026-09-18) — …» → «…»
        m = re.match(r"^(?:" + STATUS.pattern + r")(?:\s+by the Owner)?(?:\s*\([^)]*\))*\s*[:—–\-]*\s*", t)
        if not m or m.end() == 0:
            break
        t = t[m.end():]
    t = re.sub(r"\s*[—–\-]\s*(?:" + STATUS.pattern + r")\s*$", "", t)
    return t.strip()


def _snippet(prose: str, a: int, b: int) -> str:
    s = max(prose.rfind(". ", 0, a) + 2, prose.rfind("\n", 0, a) + 1, a - 90, 0)
    while 0 < s < a and prose[s - 1].isalnum():   # start on a word boundary
        s += 1
    m = re.search(r"[.!?](\s|$)|\n", prose[b:])
    e = b + (m.start() + 1 if m else len(prose) - b)
    return one_line(prose[s:e], 150)


def _negated(prose: str, at: int) -> bool:
    """A negation or hypothetical in the same clause, up to 40 chars before the verb."""
    lo = max(0, at - 40)
    cut = [m.end() for m in NEG_BOUND.finditer(prose, lo, at)]
    return bool(NEGATION.search(prose[cut[-1] if cut else lo:at]))


def _id_list(prose: str, pos: int) -> Tuple[List[str], int]:
    m = ID_RE.match(prose, pos)
    ids, end = [m.group(1)], m.end()
    while True:
        n = LIST_NEXT.match(prose[end:])
        if not n:
            return ids, end
        ids.append(n.group(1))
        end += n.end()


def _object(prose: str, verb_end: int, prefixes: set) -> Optional[Tuple[List[str], str, int]]:
    """-> (ids, 'full'|'part', end) for the object of an active verb, or None."""
    m = ID_RE.search(prose, verb_end, verb_end + 70)
    if not m:
        return None
    gap = prose[verb_end:m.start()]
    if GAP_STOP.search(gap) or "\n" in gap:
        return None
    ids, end = _id_list(prose, m.start())
    ids = [x for x in ids if x.rsplit("-", 1)[0] in prefixes]
    if not ids:
        return None
    words = re.findall(r"[A-Za-z]+", gap.lower())
    post = prose[end:].lstrip("`*_")
    if all(w in FULL_GAP_WORDS for w in words):
        return ids, ("full" if FULL_POST.match(post) else "part"), end
    if (words and words[-1] in ("of", "in")) or re.search(r"[«\"“]", gap):
        return ids, "part", end
    return None


def _self_clause(prose: str, at: int) -> bool:
    """True when the clause before a passive verb names nothing but the entry itself."""
    lo = max(0, at - 160)
    cut = [m.end() for m in CLAUSE_BOUND.finditer(prose, lo, at)]
    if not cut and lo > 0:
        return False
    pre = DATE.sub(" ", STATUS.sub(" ", prose[cut[-1] if cut else 0:at]))
    return all(w in SELF_PREFIX_WORDS for w in re.findall(r"[A-Za-z0-9]+", pre.lower()))


def _refs(e: Entry, prefixes: set) -> None:
    prose = norm_prose(e.text)
    e.mentions = sorted({x for x in ID_RE.findall(prose)
                         if x != e.id and x.rsplit("-", 1)[0] in prefixes})
    found: Dict[Tuple[str, str], Ref] = {}

    def add(kind: str, target: str, a: int, b: int) -> None:
        if target != e.id and (kind, target) not in found:
            found[(kind, target)] = Ref(kind, target, _snippet(prose, a, b))

    for rx, base in ((SUP_ACTIVE, None), (SUP_PERFECT, None), (AMEND_ACTIVE, "amend")):
        for v in rx.finditer(prose):
            if _negated(prose, v.start()):
                continue
            obj = _object(prose, v.end(), prefixes)
            if not obj:
                continue
            ids, extent, end = obj
            verb = v.group(1).lower()
            if base == "amend":
                kind = "reopens" if "open" in verb else "amends"
            else:
                kind = "withdraws" if verb.startswith("withdr") else "supersedes"
                if extent == "part":
                    kind += "-part"
            for t in ids:
                add(kind, t, v.start(), end)
    e.refs = [r for r in found.values()
              if not (r.kind.endswith("-part") and (r.kind[:-5], r.target) in found)]
    for rx, base in ((SUP_PASSIVE, "sup"), (AMEND_PASSIVE, "amend")):
        for v in rx.finditer(prose):
            by = v.group(2)
            if by == e.id or by.rsplit("-", 1)[0] not in prefixes:
                continue
            verb = v.group(1).lower()
            kind = ("withdraws" if verb == "withdrawn" else "supersedes") if base == "sup" \
                else ("reopens" if "open" in verb else "amends")
            if base == "sup" and not _self_clause(prose, v.start()):
                kind += "-part"   # «clause (B) OVERRULED by X»: a part of this entry, not all of it
            e.self_refs.append(Ref(kind, by, _snippet(prose, v.start(), v.end())))


# -- state resolution -------------------------------------------------------
def read_status(path: Path) -> Dict[str, Tuple[str, str, str]]:
    out: Dict[str, Tuple[str, str, str]] = {}
    if not path.is_file():
        return out
    for line in path.read_text(errors="replace").splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        parts = (line.split("\t") + ["", "", ""])[:4]
        if parts[1].strip().lower() in MANUAL_STATES:
            out[parts[0].strip()] = (parts[1].strip().lower(), parts[2].strip(), parts[3].strip())
    return out


def _pick(cands: List[Entry], src: Entry) -> List[Entry]:
    """A duplicated target: the occurrence the source could see (latest before it)."""
    if len(cands) <= 1:
        return cands
    before = [c for c in cands if c.line_start < src.line_start]
    return [before[-1] if before else cands[-1]]


def resolve(entries: List[Entry], overrides: Dict[str, Tuple[str, str, str]]) -> None:
    by_id: Dict[str, List[Entry]] = defaultdict(list)
    for e in entries:
        by_id[e.id].append(e)
    for e in entries:
        o = overrides.get(e.key) or overrides.get(e.id)
        if o:
            e.state, e.state_source = o[0], "manual"
            if o[1]:
                (e.superseded_by if o[0] in INACTIVE else e.amended_by).append(o[1])
            e.why = o[2]

    # Every inferred relation is an event at its source's position; applying them
    # in file order lets a later «reopens A» undo an earlier «supersedes A».
    events: List[Tuple[int, int, Entry, Entry, Ref]] = []
    for n, b in enumerate(entries):
        if b.state == "withdrawn" and b.state_source in ("manual", "heading"):
            continue   # a withdrawn entry supersedes nothing
        for r in b.refs:
            for a in _pick(by_id.get(r.target, []), b):
                events.append((b.line_start, n, a, b, r))
    for n, a in enumerate(entries):
        for r in a.self_refs:
            src = by_id.get(r.target)
            if src:
                events.append((src[-1].line_start, n, a, src[-1], r))
    for _, _, a, b, r in sorted(events, key=lambda ev: (ev[0], ev[1])):
        if a is b or a.state_source in ("manual",):
            continue
        if r.kind in ("supersedes", "withdraws"):
            if b.key not in a.superseded_by:
                a.superseded_by.append(b.key)
            if a.state not in INACTIVE:
                a.state = "superseded" if r.kind == "supersedes" else "withdrawn"
                a.state_source, a.why = "inferred", r.snippet
            continue
        if b.key not in a.amended_by:
            a.amended_by.append(b.key)
        if r.kind == "reopens" and a.state in INACTIVE and a.state_source == "inferred":
            a.state, a.why = "reopened", r.snippet
        elif a.state == "active":
            a.state = "reopened" if r.kind == "reopens" else "amended"
            a.state_source, a.why = "inferred", r.snippet


def load_entries(path, status_file=None) -> List[Entry]:
    """Parse a ledger and resolve states (STATUS.tsv beside it in decisions/ wins)."""
    path = Path(path)
    entries = parse(path.read_text(errors="replace"))
    sf = Path(status_file) if status_file else path.parent / OUT_DIR / "STATUS.tsv"
    resolve(entries, read_status(sf))
    return entries


def find(entries: List[Entry], ident: str) -> List[Entry]:
    rid, _, occ = ident.strip().strip("`*").partition("#")
    hits = [e for e in entries if e.id.upper() == rid.upper()]
    return [e for e in hits if str(e.occurrence) == occ] if occ else hits


def _label(e: Entry) -> str:
    return f"{e.id} [occurrence {e.occurrence} of {e.dup_count}]" if e.dup_count > 1 else e.id


def status_line(e: Entry) -> str:
    bits = [f"== {_label(e)}", f"lines {e.line_start}-{e.line_end}",
            e.status or "no status", e.date or "no date",
            f"state {e.state}" + (f" ({e.state_source})" if e.state_source else "")]
    if e.superseded_by:
        bits.append("superseded by " + ", ".join(e.superseded_by))
    if e.amended_by:
        bits.append("amended/reopened by " + ", ".join(e.amended_by))
    line = " · ".join(bits)
    return line + (f"\n   why: «{e.why}»" if e.why else "")


def entry_text(path, ident: str) -> str:
    hits = find(load_entries(path), ident)
    if len(hits) == 1:
        return hits[0].text
    return "\n\n".join(f"<!-- {_label(e)} -->\n{e.text}" for e in hits)


def binding_block(workspace, ids: Iterable[str]) -> str:
    """The full text of `ids`, for inlining into a work order."""
    ids = list(dict.fromkeys(i for i in ids if i))
    if not ids:
        return ""
    src = Path(workspace) / LEDGER
    entries = load_entries(src) if src.is_file() else []
    out = [f"### Binding rulings — verbatim from `{LEDGER}` ({len(ids)} id(s))", "",
           "These entries bind this row. They are copied whole from the archive; "
           "`orch gd show <id>` prints any other entry.", ""]
    for rid in ids:
        hits = find(entries, rid)
        if not hits:
            out += [f"> `{rid}` — NOT FOUND in `{LEDGER}`.", ""]
            continue
        for e in hits:
            if e.dup_count > 1:
                out.append(f"> `{e.id}` is used {e.dup_count}× in the ledger; this is occurrence {e.occurrence}.")
            if not e.active:
                out.append(f"> NOTE: `{e.key}` is marked {e.state} ({e.state_source}) by "
                           f"{', '.join(e.superseded_by) or '?'} — read that entry too.")
            elif e.amended_by:
                out.append(f"> NOTE: `{e.key}` is amended/partly superseded by {', '.join(e.amended_by)}.")
            out += [e.text, ""]
    return "\n".join(out).rstrip() + "\n"


PLAN_RULINGS = re.compile(
    r"^[ \t>]*(?:[-*+][ \t]+)?(?:\*\*|__)?[ \t]*(?:binding[ \t]+)?rulings?"
    r"(?:[ \t]*\(binding\))?[ \t]*(?:\*\*|__)?[ \t]*:[ \t]*(?:\*\*|__)?[ \t]*(?P<ids>.*)$",
    re.I | re.M)


def plan_rulings(text: str) -> List[str]:
    """Ids named on a PLAN's `Rulings:` / `**Binding rulings:**` lines, in order."""
    ids: List[str] = []
    for m in PLAN_RULINGS.finditer(text):
        ids += ID_RE.findall(m.group("ids"))
    return list(dict.fromkeys(ids))


# -- derived files ----------------------------------------------------------
INDEX_COLS = ["id", "occurrence", "line_start", "line_end", "bytes", "date", "status", "state",
              "state_source", "section", "title", "refs_out", "superseded_by", "amended_by",
              "duplicate"]


def write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def index_tsv(entries: List[Entry]) -> str:
    rows = ["\t".join(INDEX_COLS)]
    for e in entries:
        rows.append("\t".join([
            e.id, str(e.occurrence), str(e.line_start), str(e.line_end), str(e.nbytes), e.date,
            e.status, e.state, e.state_source, one_line(e.section, 120), one_line(e.title, 160),
            ",".join(f"{r.kind}:{r.target}" for r in e.refs), ",".join(e.superseded_by),
            ",".join(e.amended_by), "yes" if e.dup_count > 1 else ""]))
    return "\n".join(rows) + "\n"


def digest_md(entries: List[Entry], src: Path, max_chars: int = 280, since: str = "") -> str:
    floor = int(since.rsplit("-", 1)[1]) if since else -1
    pref = since.rsplit("-", 1)[0] if since else ""
    keep = [e for e in entries if not since or (e.prefix == pref and e.num >= floor)]
    active = [e for e in keep if e.active]
    gone = [e for e in keep if not e.active]
    sections: Dict[str, List[Entry]] = {}
    for e in active:
        sections.setdefault(e.section or "(no section)", []).append(e)
    head = [
        f"# ACTIVE decisions — digest of `{src.name}`", "",
        f"Derived file, written {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%MZ')} by "
        "`orch gd digest` — never edit it; the archive is never rewritten.",
        f"{len(active)} active entries of {len(keep)}" + (f" (from {since})" if since else "")
        + f"; {len(gone)} superseded/withdrawn are listed at the end.",
        "One line per entry: id (date, status) title — first sentence, truncated. "
        "[amended by …] = still in force, read the amendment too.",
        "**Full text: `orch gd show <id>`** · search: `orch gd grep <regex> --active`.", ""]
    body: List[str] = []
    for name in reversed(list(sections)):
        body += [f"## {name}", ""]
        for e in reversed(sections[name]):
            meta = ", ".join(x for x in (e.date, e.status) if x) or "—"
            tag = f" [amended by {', '.join(e.amended_by)}]" if e.amended_by else ""
            gist = e.title + (" — " if e.title and e.lead else "") + e.lead
            body.append(f"- **{e.key}** ({meta}){tag} {one_line(gist, max_chars)}")
        body.append("")
    if gone:
        items = [f"{e.key}→{'/'.join(e.superseded_by) or e.state}"
                 + ("" if e.state_source == "inferred" else f" ({e.state_source})") for e in gone]
        body += [f"## Superseded / withdrawn ({len(gone)})", "", " · ".join(items), ""]
    return "\n".join(head + body)


# -- id allocation ----------------------------------------------------------
def read_reserved(text: str) -> List[str]:
    return [ln.split("\t", 1)[0].strip() for ln in text.splitlines()
            if ln.strip() and not ln.startswith("#")]


def default_prefix(entries: List[Entry]) -> str:
    c = Counter(e.prefix for e in entries)
    if not c:
        raise SystemExit("decisions: no entries — pass --prefix")
    return c.most_common(1)[0][0]


def next_id(ledger_text: str, prefix: str, reserved: Iterable[str]) -> str:
    nums = [m for m in ID_RE.findall(ledger_text) if m.rsplit("-", 1)[0] == prefix]
    nums += [r for r in reserved if r.rsplit("-", 1)[0] == prefix]
    digits = [x.rsplit("-", 1)[1] for x in nums]
    top = max((int(d) for d in digits), default=0)
    width = max((len(d) for d in digits if int(d) == top), default=2)
    return f"{prefix}-{top + 1:0{width}d}"


def reserve(src: Path, out: Path, prefix: str, title: str) -> str:
    """Allocate and record the next id under an exclusive lock on RESERVED.tsv."""
    out.mkdir(parents=True, exist_ok=True)
    with open(out / "RESERVED.tsv", "a+") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            fh.seek(0)
            reserved = read_reserved(fh.read())
            nid = next_id(src.read_text(errors="replace"), prefix, reserved)
            stamp = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
            fh.seek(0, os.SEEK_END)
            fh.write(f"{nid}\t{stamp}\t{one_line(title)}\n")
            fh.flush()
            os.fsync(fh.fileno())
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)
    return nid


def append_locked(path: Path, line: str, header: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            fh.seek(0, os.SEEK_END)
            if header and fh.tell() == 0:
                fh.write(header + "\n")
            fh.write(line + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


# -- CLI --------------------------------------------------------------------
def resolve_paths(args) -> Tuple[Path, Path]:
    file, outdir = getattr(args, "file", None), getattr(args, "out", None)
    if file:
        src = Path(file).resolve()
        out = Path(outdir).resolve() if outdir else src.parent / OUT_DIR
    else:
        from config_parser import load
        cfg = load()
        src = cfg.workspace / LEDGER
        out = Path(outdir).resolve() if outdir else cfg.workspace / OUT_DIR
    if not src.is_file():
        raise SystemExit(f"decisions: no ledger at {src}")
    return src, out


def entries_for(src: Path, out: Path) -> List[Entry]:
    return load_entries(src, out / "STATUS.tsv")


def duplicates(entries: List[Entry]) -> Dict[str, List[int]]:
    d: Dict[str, List[int]] = defaultdict(list)
    for e in entries:
        if e.dup_count > 1:
            d[e.id].append(e.line_start)
    return dict(d)


def cmd_next(a, src, out) -> int:
    if a.reserve is not None:
        prefix = a.prefix or default_prefix(parse(src.read_text(errors="replace")))
        print(reserve(src, out, prefix, a.reserve))
        return 0
    text = src.read_text(errors="replace")
    prefix = a.prefix or default_prefix(parse(text))
    rp = out / "RESERVED.tsv"
    print(next_id(text, prefix, read_reserved(rp.read_text()) if rp.is_file() else []))
    return 0


def cmd_index(a, src, out) -> int:
    entries = entries_for(src, out)
    write_atomic(out / "INDEX.tsv", index_tsv(entries))
    dups = duplicates(entries)
    st = Counter((e.state, e.state_source) for e in entries)
    print(f"INDEX {out / 'INDEX.tsv'} entries={len(entries)} ids={len({e.id for e in entries})} "
          f"duplicate_ids={len(dups)} superseded_inferred={st[('superseded', 'inferred')]} "
          f"withdrawn_inferred={st[('withdrawn', 'inferred')]} "
          f"amended_inferred={st[('amended', 'inferred')] + st[('reopened', 'inferred')]} "
          f"manual={sum(n for (s, src_), n in st.items() if src_ == 'manual')}")
    for rid, starts in sorted(dups.items()):
        print(f"  DUPLICATE {rid} at lines {', '.join(map(str, starts))}")
    return 0


def cmd_digest(a, src, out) -> int:
    entries = entries_for(src, out)
    text = digest_md(entries, src, a.max_chars, a.since or "")
    write_atomic(out / "ACTIVE.md", text)
    sb, db = src.stat().st_size, len(text.encode())
    print(f"DIGEST {out / 'ACTIVE.md'} active={sum(e.active for e in entries)} "
          f"inactive={sum(not e.active for e in entries)} bytes={db} source_bytes={sb} "
          f"ratio={db / sb:.1%}")
    return 0


def cmd_show(a, src, out) -> int:
    entries = entries_for(src, out)
    missing = 0
    for ident in a.ids:
        hits = find(entries, ident)
        if not hits:
            print(f"== {ident} · NOT FOUND", file=sys.stderr)
            missing += 1
        for e in hits:
            print(status_line(e))
            print(e.text)
            print()
    return 1 if missing else 0


def cmd_grep(a, src, out) -> int:
    rx = re.compile(a.regex, 0 if a.case else re.I)
    shown = 0
    for e in entries_for(src, out):
        if a.active and not e.active:
            continue
        hits = [(n, ln) for n, ln in enumerate(e.text.splitlines(), e.line_start) if rx.search(ln)]
        if not hits:
            continue
        shown += 1
        if shown > a.limit:
            print(f"… more matches; raise --limit (now {a.limit})")
            break
        more = f" (+{len(hits) - 1} more lines)" if len(hits) > 1 else ""
        print(f"{e.key}  [{e.state}] {one_line(e.title or e.lead, 90)}")
        print(f"    L{hits[0][0]}: {one_line(hits[0][1], 160)}{more}")
    return 0


def cmd_set_status(a, src, out) -> int:
    if not find(load_entries(src, out / "STATUS.tsv"), a.id):
        print(f"decisions: {a.id} is not an entry in {src}", file=sys.stderr)
        return 2
    line = "\t".join([a.id, a.status, a.by or "", one_line(a.note or "")])
    append_locked(out / "STATUS.tsv", line, "# id\tstatus\tby\tnote")
    print(f"STATUS {a.id} -> {a.status}" + (f" by {a.by}" if a.by else ""))
    return 0


def cmd_check(a, src, out) -> int:
    entries = entries_for(src, out)
    ids = {e.id for e in entries}
    dups = duplicates(entries)
    unknown_refs = sorted({(e.key, r.target) for e in entries for r in e.refs if r.target not in ids})
    unknown_mentions = Counter(m for e in entries for m in e.mentions if m not in ids)
    no_status = [e.key for e in entries if not e.status]
    print(f"CHECK {src} entries={len(entries)} duplicate_ids={len(dups)} "
          f"unknown_ref_targets={len(unknown_refs)} unknown_mentions={len(unknown_mentions)} "
          f"no_status={len(no_status)}")
    for rid, starts in sorted(dups.items()):
        print(f"  DUPLICATE {rid} at lines {', '.join(map(str, starts))}")
    for key, t in unknown_refs[:20]:
        print(f"  UNKNOWN_REF {key} -> {t}")
    if unknown_mentions:
        print("  UNKNOWN_MENTIONS " + ", ".join(sorted(unknown_mentions)[:40])
              + (" …" if len(unknown_mentions) > 40 else ""))
    if no_status:
        print("  NO_STATUS " + ", ".join(no_status[:40]) + (" …" if len(no_status) > 40 else ""))
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    common = argparse.ArgumentParser(add_help=False)
    # SUPPRESS: a subparser default must not erase a value given before the command.
    common.add_argument("--file", default=argparse.SUPPRESS,
                        help="ledger to read (default: <workspace>/DECISIONS.md)")
    common.add_argument("--out", default=argparse.SUPPRESS,
                        help="derived-file dir (default: <ledger dir>/decisions)")
    p = argparse.ArgumentParser(prog="decisions.py", description=__doc__.split("\n\n")[0],
                                parents=[common])
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("next", parents=[common])
    s.add_argument("--prefix")
    s.add_argument("--reserve", metavar="TITLE")
    sub.add_parser("index", parents=[common])
    s = sub.add_parser("digest", parents=[common])
    s.add_argument("--since")
    s.add_argument("--max-chars", type=int, default=280)
    s = sub.add_parser("show", parents=[common])
    s.add_argument("ids", nargs="+")
    s = sub.add_parser("grep", parents=[common])
    s.add_argument("regex")
    s.add_argument("--active", action="store_true")
    s.add_argument("--case", action="store_true", help="case-sensitive")
    s.add_argument("--limit", type=int, default=40)
    s = sub.add_parser("set-status", parents=[common])
    s.add_argument("id")
    s.add_argument("status", choices=MANUAL_STATES)
    s.add_argument("--by")
    s.add_argument("--note")
    sub.add_parser("check", parents=[common])
    a = p.parse_args(argv)
    src, out = resolve_paths(a)
    return {"next": cmd_next, "index": cmd_index, "digest": cmd_digest, "show": cmd_show,
            "grep": cmd_grep, "set-status": cmd_set_status, "check": cmd_check}[a.cmd](a, src, out)


if __name__ == "__main__":
    sys.exit(main())
