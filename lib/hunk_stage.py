#!/usr/bin/env python3
"""Stage only ONE row's hunks of a shared path.

A shared path (guard.shared_paths: DECISIONS.md, PLAN.md, LEDGER.md, the
reports index …) is edited by every closing row. `git add <path>` stages the
whole working-tree file, so a row's commit carried another row's half-written
entry under the wrong name. This helper stages, from the working tree into the
index, only the hunks that belong to the committing row:

  a hunk belongs to row <id> when one of its changed lines (+ or -) names <id>
  as a whole token (not preceded or followed by a letter or digit; so `bb1`
  matches `bb1`, `` `bb1` ``, `| bb1 |` and `REPORTS/bb1_20260923/`, and never
  `bb1d` or `xbb1`).

Hunks are taken at zero context (`git diff -U0`), so two edits in different
places of the file are separate hunks. Hunks that do not name the row stay in
the working tree, unstaged, for their own row's commit.

usage: hunk_stage.py <row-id> <repo-relative path>     (cwd = the git repo)
exit: 0 staged (all or some hunks, or nothing to stage) · 4 the path changed
      but no hunk names the row (nothing staged) · 2 error
Prints one line: HUNK_STAGE <path> hunks=<n> staged=<k> left=<n-k> [mode]
"""
from __future__ import annotations

import re
import subprocess
import sys
from typing import List, Tuple

HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$")


def git(*args: str, data: str | None = None) -> Tuple[int, str, str]:
    p = subprocess.run(["git", *args], input=data, capture_output=True, text=True)
    return p.returncode, p.stdout, p.stderr


def token_re(rid: str) -> re.Pattern:
    return re.compile(rf"(?<![A-Za-z0-9]){re.escape(rid)}(?![A-Za-z0-9])")


def split(diff: str) -> Tuple[List[str], List[List[str]]]:
    header: List[str] = []
    hunks: List[List[str]] = []
    for line in diff.splitlines(keepends=True):
        if line.startswith("@@"):
            hunks.append([line])
        elif hunks:
            hunks[-1].append(line)
        else:
            header.append(line)
    return header, hunks


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: hunk_stage.py <row-id> <path>")
        return 2
    rid, path = sys.argv[1], sys.argv[2]
    tok = token_re(rid)

    rc, _, _ = git("ls-files", "--error-unmatch", "--", path)
    if rc != 0:
        # Not in the index: a first creation. One row creates a file; stage it whole.
        rc, _, err = git("add", "--", path)
        if rc != 0:
            print(f"HUNK_STAGE {path} ERROR git add: {err.strip()}")
            return 2
        print(f"HUNK_STAGE {path} hunks=1 staged=1 left=0 new-file")
        return 0

    rc, diff, err = git("-c", "core.quotepath=false", "diff", "-U0", "--no-color",
                        "--no-ext-diff", "--", path)
    if rc != 0:
        print(f"HUNK_STAGE {path} ERROR git diff: {err.strip()}")
        return 2
    if not diff.strip():
        print(f"HUNK_STAGE {path} hunks=0 staged=0 left=0 unchanged")
        return 0
    header, hunks = split(diff)
    if not hunks or any(l.startswith("Binary files") for l in header):
        # A mode-only or binary change has no line hunks to attribute; it is
        # staged whole, and said so.
        rc, _, err = git("add", "--", path)
        print(f"HUNK_STAGE {path} hunks=0 staged=whole left=0 no-line-hunks")
        return 0 if rc == 0 else 2

    keep = [any(tok.search(l[1:]) for l in h[1:] if l[:1] in "+-") for h in hunks]
    n, k = len(hunks), sum(keep)
    if k == 0:
        print(f"HUNK_STAGE {path} hunks={n} staged=0 left={n} none-names-{rid}")
        return 4
    if k == n:
        rc, _, err = git("add", "--", path)
        if rc != 0:
            print(f"HUNK_STAGE {path} ERROR git add: {err.strip()}")
            return 2
        print(f"HUNK_STAGE {path} hunks={n} staged={k} left=0 all")
        return 0

    # Rebuild a patch of the kept hunks. Each hunk's new-side start shifts by
    # the net line delta of every SKIPPED hunk before it.
    out: List[str] = list(header)
    shift = 0
    for h, kept in zip(hunks, keep):
        m = HUNK.match(h[0].rstrip("\n"))
        if not m:
            print(f"HUNK_STAGE {path} ERROR unparsable hunk header")
            return 2
        a, b, c, d, tail = m.group(1), m.group(2), m.group(3), m.group(4), m.group(5)
        bn = 1 if b is None else int(b)
        dn = 1 if d is None else int(d)
        if not kept:
            shift += dn - bn
            continue
        c2 = int(c) - shift
        old = f"-{a}" + ("" if b is None else f",{b}")
        new = f"+{c2}" + ("" if d is None else f",{d}")
        out.append(f"@@ {old} {new} @@{tail}\n")
        out.extend(h[1:])
    patch = "".join(out)
    rc, _, err = git("apply", "--cached", "--unidiff-zero", "--whitespace=nowarn", "-", data=patch)
    if rc != 0:
        print(f"HUNK_STAGE {path} ERROR git apply --cached: {err.strip()}")
        return 2
    print(f"HUNK_STAGE {path} hunks={n} staged={k} left={n - k} partial "
          f"(the {n - k} hunk(s) naming no '{rid}' stay unstaged for their own row)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
