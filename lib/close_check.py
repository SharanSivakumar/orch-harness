#!/usr/bin/env python3
"""Close-time stop-line check: a row may not be closed CLOSED over its own stop line.

A row's plan says «if X, stop». When its own instrument output or its PROGRESS
records that stop line and the row is closed CLOSED anyway, the record hides a
failed exact line (a row closed green over two of its own
`HEADS_UNCHANGED no` lines). `orch done <id>` therefore runs this check first and
refuses the CLOSED when the row's evidence carries such a line, unless the row's
PROGRESS names a signed ruling that covers it.

What is scanned (read-only):
  * the row's folder: the directory of its queue plan path, recursively;
  * the row's report packs: <workspace>/<reports_dir>/<id>_*/, recursively.
Only text evidence is read: *.out *.log *.tsv *.txt *.md *.csv. Never read:
anything whose path has a component containing `.local` (the never-open class),
`.env*`, `*.tfvars*`, `*.tfplan*`, `terraform.tfstate*`, and the plan-side files
that state stop conditions rather than results (PLAN*.md, EXPECTED*.md,
*ORIGINAL*.md), and deliberate falsifier / self-test outputs (a file name
containing `falsif` or `selftest`): those exist to prove a fence prints STOP
or REFUSED, so the line there is the pass, not a stop. Files larger than 5 MB
are skipped and counted.

A stop line is, in instrument output (*.out *.log *.tsv *.txt *.csv), one of:
  * `HEADS_UNCHANGED no` (any separator: space, `=`, `:`) leading the line, after
    an optional `label:` prefix — the form every heads check prints
    (`close: HEADS_UNCHANGED no`); a log line that merely quotes the phrase
    (a test's PASS message, a report sentence) is not a stop line;
  * `VERDICT STOP` or `VERDICT REFUSED` (instrument verdict form), anywhere;
  * a line whose leading token (after an optional `label:` prefix) is `STOP`
    or `REFUSED` as a whole word — `STOP-if`, `STOPPED` and table cells do not
    count.
In Markdown (*.md, prose that quotes these words while discussing them) only
the line-leading forms count: `STOP …`, `REFUSED …`, `label: HEADS_UNCHANGED no`,
`VERDICT STOP` at the start of a line. A mid-sentence quotation is not a stop
line; the instrument output it quotes is scanned on its own.

The escape is a signed ruling: a line in the row's PROGRESS.md
    SIGNED_RULING <ruling-id>          e.g.  SIGNED_RULING DEC-266
whose entry exists in <workspace>/DECISIONS.md (a `### <ruling-id> ` heading)
and is signed BY ITS OWN HEADING OR STATUS LINE: `RATIFIED` or `RULED` there,
never merely quoted in the body. A PROPOSED or PROVISIONAL entry is
not a signature. A table-row ledger (`| DEC-014 | RULED … |`) signs when the
cell after the id opens with RULED/RATIFIED and it is not superseded (v3). A
Steward ruling `SIGNED_RULING SW-<k>` signs a stop line only when the engine's
state/steward/rulings.jsonl holds it with class S or P and it is about the row;
it never covers a protected path (lib/protected.py).

usage: close_check.py <row-id>     exit 0 PASS · 3 REFUSE · 2 cannot run
Output: counts, and each hit as <path>:<line>: <first 160 chars of the line>.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config_parser import load  # noqa: E402

TEXT_SUFFIXES = {".out", ".log", ".tsv", ".txt", ".md", ".csv"}
MAX_BYTES = 5 * 1024 * 1024
NEVER_OPEN = (re.compile(r"\.local"), re.compile(r"^\.env"), re.compile(r"\.tfvars"),
              re.compile(r"\.tfplan"), re.compile(r"^terraform\.tfstate"))
PLAN_SIDE = re.compile(r"^(PLAN.*|EXPECTED.*|.*ORIGINAL.*)\.md$")
FALSIFIER = re.compile(r"falsif|selftest", re.I)

STOP_PATTERNS = (
    re.compile(r"^\s*(?:[\w.\-/\[\]]+:\s*)?HEADS_UNCHANGED\s*[:=]?\s*no\b", re.I),
    re.compile(r"\bVERDICT\s*[:=]?\s*(STOP|REFUSED)\b"),
    re.compile(r"^\s*(?:[\w.\-/\[\]]+:\s*)?(STOP|REFUSED)(?=$|[\s:.,;)])"),
)
LEAD = r"^\s*(?:[\w.\-/\[\]]+:\s*)?"
MD_PATTERNS = (
    re.compile(LEAD + r"HEADS_UNCHANGED\s*[:=]?\s*no\b", re.I),
    re.compile(LEAD + r"(?:\S+\s+)?VERDICT\s*[:=]?\s*(STOP|REFUSED)\b"),
    re.compile(LEAD + r"(STOP|REFUSED)(?=$|[\s:.,;)])"),
)
SIGNED_LINE = re.compile(r"\bSIGNED_RULING\b[^A-Za-z0-9]*([A-Z][A-Z0-9]*-[0-9]+)")


def never_open(rel: Path) -> bool:
    return any(pat.search(part) for part in rel.parts for pat in NEVER_OPEN)


def row_plan(cfg, rid: str) -> Optional[str]:
    from rows import find_row
    r = find_row(cfg, rid)
    return r.plan if r and r.plan else None


def evidence_roots(cfg, rid: str) -> List[Path]:
    roots: List[Path] = []
    plan = row_plan(cfg, rid)
    if plan:
        folder = (cfg.workspace / plan).parent
        # A plan at the workspace top level would make the whole workspace the
        # «row folder»; only a dedicated folder counts.
        if folder.is_dir() and folder.resolve() != cfg.workspace.resolve():
            roots.append(folder)
    rep = cfg.workspace / cfg.reports_dir
    if rep.is_dir():
        roots.extend(sorted(d for d in rep.glob(f"{rid}_*") if d.is_dir()))
    return roots


def scan(roots: List[Path]) -> Tuple[List[str], int, int]:
    hits: List[str] = []
    files = skipped = 0
    for root in roots:
        for dirpath, dirnames, filenames in os.walk(root):
            base = Path(dirpath)
            dirnames[:] = [d for d in dirnames if not never_open(Path(d))]
            for fn in sorted(filenames):
                f = base / fn
                rel = f.relative_to(root)
                if (never_open(rel) or f.suffix.lower() not in TEXT_SUFFIXES
                        or PLAN_SIDE.match(fn) or FALSIFIER.search(fn)):
                    continue
                pats = MD_PATTERNS if f.suffix.lower() == ".md" else STOP_PATTERNS
                try:
                    if f.stat().st_size > MAX_BYTES:
                        skipped += 1
                        continue
                    text = f.read_text(errors="replace")
                except Exception:
                    skipped += 1
                    continue
                files += 1
                for n, line in enumerate(text.splitlines(), 1):
                    if any(p.search(line) for p in pats):
                        hits.append(f"{f}:{n}: {line.strip()[:160]}")
    return hits, files, skipped


def signed_ruling(cfg, roots: List[Path], owner_only: bool = False,
                  rid: Optional[str] = None) -> Tuple[Optional[str], str]:
    """-> (ruling id if a PROGRESS names a signed one, note).

    An Owner ruling is signed in <workspace>/DECISIONS.md — a `### <id>` heading entry or a
    table-row entry (`| DEC-014 | RULED … |`), both read through lib/decisions.py. A Steward
    ruling `SW-<k>` signs only a stop line (never with `owner_only`), only from the engine's own
    record (state/steward/rulings.jsonl, class S or P), and only for the row it is about (`rid`
    named in its title or text, or the subject of its incident); a `SW-` id written into
    DECISIONS.md signs nothing. Protected paths use lib/protected.py's stricter owner_ruling."""
    named: List[str] = []
    for root in roots:
        p = root / "PROGRESS.md"
        if p.is_file():
            named += SIGNED_LINE.findall(p.read_text(errors="replace"))
    named = list(dict.fromkeys(named))
    if not named:
        return None, "no SIGNED_RULING line in PROGRESS.md"
    notes: List[str] = []
    sw = [r for r in named if r.startswith(STEWARD_PREFIX)]
    owner = [r for r in named if not r.startswith(STEWARD_PREFIX)]
    for k in sw:
        if owner_only:
            notes.append(f"{k} is a Steward ruling — only an Owner ruling covers a protected path")
            continue
        rec = steward_record(cfg, k)
        if not rec or str(rec.get("class", "")).upper() not in ("S", "P"):
            notes.append(f"{k} is not a class S/P ruling in state/steward/rulings.jsonl")
        elif not rid or not steward_scoped(cfg, rec, rid):
            notes.append(f"{k} is about another subject (it names neither {rid} nor an incident on it)")
        else:
            return k, f"{k} is a class-{str(rec['class']).upper()} Steward ruling on {rid} (state/steward/rulings.jsonl)"
    if owner:
        dec = cfg.workspace / "DECISIONS.md"
        if not dec.is_file():
            notes.append(f"SIGNED_RULING names {owner[0]} but there is no DECISIONS.md")
        else:
            text = dec.read_text(errors="replace")
            for k in owner:
                if entry_signed(text, k) or table_signed(dec, k):
                    return k, f"{k} is signed in DECISIONS.md"
            notes.append(f"SIGNED_RULING names {', '.join(owner)} but none is a signed (RATIFIED/RULED) entry")
    return None, "; ".join(notes)


STEWARD_PREFIX = "SW-"


def steward_record(cfg, k: str) -> Optional[dict]:
    try:
        import steward
        return steward.steward_ruling(cfg, k)
    except Exception:
        return None


def steward_scoped(cfg, rec: dict, rid: str) -> bool:
    """A Steward ruling is about `rid` when its title/text names it or its incident's subject is it."""
    pat = re.compile(rf"(?<![\w-]){re.escape(rid)}(?![\w-])")
    if pat.search(f"{rec.get('title', '')}\n{rec.get('text', '')}"):
        return True
    try:
        import json
        inc = json.loads((cfg.state / "steward" / "incidents" / f"{rec.get('incident')}.json").read_text())
        return str(inc.get("subject", "")) == rid
    except Exception:
        return False


def table_signed(dec: Path, rid: str) -> bool:
    """A table-row ledger entry (`| <id> | RULED (…) — … |`) signs when table_entry_signed holds and
    the entry is not superseded or withdrawn. Heading entries are judged by entry_signed."""
    try:
        import decisions
        entries = decisions.find(decisions.load_entries(dec), rid)
    except Exception:
        return False
    return any(e.kind == "table" and e.active and table_entry_signed(e) for e in entries)


def table_entry_signed(e) -> bool:
    """The cell after the id OPENS with RULED/RATIFIED (its first token, markdown emphasis aside) and
    that is the entry's status (decisions.STATUS) — so PROPOSED/ADOPTED/REJECTED/OPEN, «to be RULED»
    and «DEC-14 RATIFIED by …» (another id's status) never sign."""
    import decisions
    if e.kind != "table" or e.status not in ("RULED", "RATIFIED"):
        return False
    m = decisions.TABLE_ROW.match(e.head)
    cell = (m.group(2) if m else "").split("|", 1)[0]
    lead = re.sub(r"^[\s*_`]+", "", cell)[:200]
    first = decisions.STATUS.search(lead)
    return bool(first and first.group(1).upper() in ("RULED", "RATIFIED") and first.start() == 0)


SIGNED_WORD = re.compile(r"\b(RATIFIED|RULED)\b")
UNSIGNED_WORD = re.compile(r"\b(PROPOSED|PROVISIONAL|DRAFT|WITHDRAWN|SUPERSEDED)\b")
STATUS_LINE = re.compile(r"^\s*(?:[*_>\-\s]*)(?:status|verdict|ruling)\s*[*_]*\s*[:=—-]", re.I)


def entry_signed(text: str, rid: str) -> bool:
    """A ruling is signed only when the entry ITSELF says so: its heading
    carries RULED/RATIFIED and no PROPOSED/PROVISIONAL/DRAFT, or — for headings that
    carry no status — one of the first lines of its body is a status line (or opens
    with a bold «… RULED.» declaration). A PROPOSED entry that merely quotes the word
    RULED somewhere in its body is not a signature."""
    for m in re.finditer(rf"^###\s+{re.escape(rid)}\b(.*?)(?=^###\s|\Z)", text, re.M | re.S):
        block = m.group(0)
        heading = block.splitlines()[0]
        if UNSIGNED_WORD.search(heading):
            continue
        if SIGNED_WORD.search(heading):
            return True
        body = [l for l in block.splitlines()[1:] if l.strip()][:3]
        for line in body:
            lead = line.strip()[:200]
            if UNSIGNED_WORD.search(lead[:80]):
                break
            m2 = STATUS_LINE.match(line)
            if m2:
                value = line[m2.end():].strip(" *_`")
                if re.match(r"(RATIFIED|RULED)\b", value):
                    return True
                break
            if lead.startswith("**"):
                bold = lead[2:].split("**", 1)[0]
                if re.search(r"\b(RATIFIED|RULED)\W*$", bold) and not UNSIGNED_WORD.search(bold):
                    return True
    return False


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__.split("usage:")[1].splitlines()[0].strip())
        return 2
    rid = sys.argv[1]
    try:
        cfg = load()
        roots = evidence_roots(cfg, rid)
        hits, files, skipped = scan(roots)
        ruling, note = signed_ruling(cfg, roots, rid=rid) if hits else (None, "not needed")
    except SystemExit:
        raise
    except Exception as e:  # never a silent pass
        print(f"CLOSE_CHECK {rid} ERROR {type(e).__name__}: {e}")
        return 2
    verdict = "PASS" if not hits or ruling else "REFUSE"
    print(f"CLOSE_CHECK {rid} roots={len(roots)} files={files} skipped={skipped} "
          f"stop_lines={len(hits)} signed={ruling or 'none'} VERDICT {verdict}")
    for h in hits[:20]:
        print(f"  {h}")
    if len(hits) > 20:
        print(f"  … {len(hits) - 20} more")
    if hits:
        print(f"  ruling: {note}")
    return 0 if verdict == "PASS" else 3


if __name__ == "__main__":
    sys.exit(main())
