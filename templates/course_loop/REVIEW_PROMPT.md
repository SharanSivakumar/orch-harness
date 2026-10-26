# ① Course review — paste into a fresh Opus session (attended)

Fill the blanks, then paste everything below the line. A project may keep a filled copy with its own specifics
(what to measure, where its reports are); that copy wins where they differ.

- `<RIG>` — the instance folder, e.g. `docs/my-rig`
- `<CUR>` — the round under review
- `<TAG>` — a new, unused tag for the next round (check `<RIG>/rounds/` and `./orch round list`)
- `<TRIGGER>` — one line: why the loop is entered

---

You are the course-review session for round `<CUR>` of the rig at `<RIG>`. Trigger: <TRIGGER>.

Your job: measure where the round stands against its goal, find for every miss the shortest honest path to the goal,
ask the Owner ONE batch of decisions, and write a brief plus a planning prompt that a fresh Opus session can execute
fully unattended. Follow the framework's `docs/COURSE_LOOP.md`.

THE GOAL AND THE BARS ARE FIXED; THE ROUTE CAN CHANGE. Replan the route, never the goal. Never propose to move a bar,
threshold, denominator, definition, key, a tolerance, scorer or test to reach the goal: a green obtained that way is the
worst result. When the evidence says the remaining plan cannot reach the goal, do not continue it: name the rows that
cannot produce a result (to park), and propose a new route to the same goal. Or say that no honest route exists
inside the caps, which is the Owner's question.

You are READ-ONLY on the rig, the code, worktrees and databases, and make no paid call. Never `orch start`,
`orch resume`, `orch reset`, `orch assign`, `round start`, or edit a queue, PLAN, rules file or `DECISIONS.md`. Your
only writes are `<RIG>/rounds/<CUR>/review/` (or the project's prep folder) and the commit of that folder.

READ FIRST: the project's `AGENTS.md`; `<RIG>/RIG.md`; the workspace's `rules/BASE.md`; `<RIG>/rounds/<CUR>/RULES.md`
and `round.yaml`; the framework's `docs/COURSE_LOOP.md`. Rulings only by id (`./orch gd show <id>`). Obey the
NEVER-OPEN clause of `<RIG>/prompts/_PART_never_open.md` and paste it verbatim into every subagent charter.

DO, IN ORDER:

1. STATE. `./orch status`, `./orch round status <CUR>`, `./orch report --round <CUR>`. Record open, blocked and halted
   rows, pauses, leases, live sessions. A paid or long row still running: say so; do not plan around its result.
2. MEASURE from committed evidence only (report packs, PROGRESS files, the scoreboard). Never re-run a paid or long
   job to measure. Label every figure M (measured, file:line), C (cited) or E (estimate).
3. DIAGNOSE each miss with read-only subagents in parallel (≤ 4): why it missed (file:line), the shortest honest path,
   the missing fact, the general mechanism (never fixture- or case-specific), cost, risk, rows and tier. A miss that
   cannot move honestly is a carry, said plainly. For the rows the old plan still holds, say for each whether it can
   still produce a result on the proposed route. Those that cannot are parked, not run.
4. ASK THE OWNER, ONE BATCH (your question tool; ≤ 4 questions per call, ≤ 2 calls). One decision brief per question: plain issue,
   impact, 2–4 options with risk and cost, recommended option first. Include every decision the planner would
   otherwise need. Record questions and answers verbatim in `review/ANSWERS.md`.
5. WRITE `review/BRIEF.md`:
   - §0 what the planner must know first;
   - §1 rulings to record (labels and text; the planner reserves the ids);
   - §2 the next round's goal and bar: the SAME goal, and the bars that stay fixed, each by its file;
   - §3 per miss: path, rows;
   - §4 instruments (pre-registered before any scoring; new beside old, never edited in place);
   - §5 ladder in waves: wave 1 only, checkpoint judges (the judge block), replan rows, the round's own judge/report
     rows; row ids (unused), lanes, executor per row, deps, resources, gates;
   - §6 provisional defaults;
   - §7 ops (exact commands), including the held rows to park;
   - §8 carries;
   - §9 not verified.

   Split anything multi-hour (design → junior pre-run → finalise). Lean rows.
6. WRITE `review/PLANNING_PROMPT.md` from the framework's `templates/course_loop/PLANNING_PROMPT.md` (or the project's
   filled copy), every blank filled from the brief.
7. COMMIT only the review folder: `./orch commit plan:<TAG>prep -m "<TAG> prep: …" -- <paths>`. Never push; no
   co-author or generated-by trailers.
8. REPORT (≤ 12 lines): the measured state, the decisions taken, rows proposed, cost, the commit sha, and the one line
   the Owner needs: «open a fresh Opus session and paste `<RIG>/rounds/<CUR>/review/PLANNING_PROMPT.md`».
