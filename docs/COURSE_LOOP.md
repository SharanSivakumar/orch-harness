# Course loop — correcting a round mid-way or after it ends

A round runs until its own judge/report rows close it. When it is not going to
reach its goal — a judge says the bar is not met, the goal metric stalls, too
many rows block, or the Owner simply wants to change direction — the course
loop turns the evidence into the Owner's decisions and the decisions into new
rows, without anyone re-reading the whole rig by hand. It works for any rig on
this framework; a project may keep a thin layer of its own specifics (for
example `docs/my-rig/COURSE_LOOP/`).

```
 trigger (stall · judge "bar NOT met" · blocks piling up · Owner's call)
    │
 ① REVIEW   read-only; measures the round against its goal; writes a brief
    │        and ONE batch of Owner decisions (decision brief)
 ② OWNER    answers the batch (letters)
    │
 ③ PLAN     unattended planning session: records rulings, writes the next
    │        round's RULES.md, cuts PLANs + queues, lint, preflight, commit
 ④ RUN      ./orch round start <next tag>; the old round is closed or keeps
             running alongside — the brief says which
```

## Native course loop (v3) — inside an open phase, no human

A round whose `round.yaml` declares `judge: <row>`, `on_fail: course_loop` and `max_loops: <n>` runs ①–④ by
itself when its judge is accepted with a negative outcome (`orch done <judge> "…" --outcome nogo|held|fail`):

| step | who | what |
|---|---|---|
| trigger | engine | the loop count goes up (`phases.yaml` `loops`, `state/courseloop/<tag>.json`); the previous `review/` is kept as `review.loop<n-1>/` |
| ① review | `rr-<tag>` (dispatched by the engine) | BRIEF.md and, only for what the decision rights leave to the Owner or the panel, QUESTIONS.md |
| ② answers | a Steward session on a `round_review` incident (the panel for class P) | `ANSWERS.md` within the rights (`Q1: B — why`), W questions to the Owner's inbox (`Q3: INBOX #k`), then `orch sw plan <tag>-l<n> "brief"` |
| ③ plan | the planner `pl-<tag>-l<n>-<k>` | `orch sw round-new <tag>-l<n> --phase <P> <new lane>…` (lanes of its own, never the judge's), PLANs, `orch row add` per row, `orch sw round-start`, PASS |
| ④ run | engine | the rows the planner added run; when each is accepted or parked — at least one accepted — the engine re-arms the judge |
| judge | engine | GO ends the loop; another negative outcome starts loop n+1 |

The bounds:

- **Inside an open phase only.** While the round's phase rests or is closed the loop waits; a correction round
  lives in the judged round's phase. **Starting a new phase is the Owner's** (`orch phase open`) — no loop, Steward
  or planner opens, re-opens or starts one.
- **At most `max_loops`** (default 1) for the round **and** the phase's `caps.loops`. When the judge fails once more
  with no loop left, the engine RESTS the phase (running rows finish, nothing new starts) and writes one inbox item:
  more loops (raise `max_loops` / the phase's loops cap) or stop and re-scope. Without a phases file only
  `max_loops` bounds it and the inbox item is all that happens.
- **A measured NO-GO is never re-run to obtain a GO.** No session resets, re-arms (`orch rearm`) or grants
  `rearm=` of the loop's judge while it holds a negative outcome; only the loop's own re-arm, after the correction's
  rows are done, runs it again. A judge reset or re-judged under the loop (the Owner's `orch reset`) ends that loop.
- A step that cannot go on (no planner dispatched, the planner failed and nothing re-planned, no row added, the
  round not started, the review failing twice) stops the loop with an inbox item; nothing pages.
- Only a round the operator created loops: a round a session created (`by:` in round.yaml) gives itself no budget.
  It loops only inside a phase the Owner opened with `orch phase open <P> --session-loops on`, and every such loop
  counts in that phase's `caps.loops` (v3.1). The loop needs the Steward lane.

### v3.1 — checkpoint judges, the infeasible outcome, the pinned bar (`docs/V3_DESIGN.md` §15)

**The rule:** the goal and the bars are fixed; the route can change. A loop that would reach GO by moving a bar is
the worst result, so the engine makes it impossible to reach that way.

- **Several judges per round.** `judges: [<row>, {row: <row>, max_loops: <n>}]` declares checkpoint judges (the end
  of a wave, `docs/PLANNING_AGENT_GUIDE.md` § Waves). Each loops independently, with names keyed
  `<tag>+<judge>`:

  | what | name |
  |---|---|
  | loop record | `state/courseloop/<tag>+<judge>.json` |
  | review session | `rr-<tag>+<judge>` |
  | review folder | `rounds/<tag>/review+<judge>/` |
  | incident subject | `round:<tag>+<judge>` |
  | suggested correction round | `<tag>-<judge>-l<n>` |

  The round's `judge:` keeps every name it had.
- **`--outcome infeasible`: the remaining plan cannot reach the goal.** For a loop judge, the loop is the replan,
  run in this order:
  1. the engine records the held wave;
  2. the review proposes a new route to the same goal and names the held rows it makes moot;
  3. the Steward (or the panel, per the rights) parks them and plans the new route;
  4. the planner cuts it;
  5. the judge re-measures the same bar.

  For any other row, the Steward's `negative_outcome` incident runs the same steps. No session ever re-runs a row
  accepted `infeasible`.
- **Replans vs failure loops.** A checkpoint's loop is a replan; the round judge's loop is a failure loop. Both count
  in `caps.loops`, unless the Owner gives the phase `caps.replans` (`orch phase open <P> --caps replans=<n>`), in
  which case checkpoint loops count there instead.
- **The bar pin** (`steward.bar_pin`, default on). When a loop starts, the engine pins sha256 of:
  - the judge's PLAN;
  - the round's `goal:`;
  - every round.yaml `bars:` file.

  If any of them changes before the re-arm, the loop stops: event `course_loop_bar_moved`, one inbox item. The pin
  stays in force after the re-arm too:
  - a re-armed judge not yet dispatched is parked;
  - a running one is flagged;
  - no new loop starts while the bar stays moved.

  To adopt a new bar, the Owner records a ruling, then runs `orch reset <judge>`.
- **Declare checkpoints up front.** No command writes `judges:`. The planner lists every planned checkpoint id when
  it plans the round, and each one loops once a replan row adds its row.

The rest of this page is the attended form (and what each step does in detail).

## Triggers

| trigger | what happens by itself |
|---|---|
| the round declares a `goal` with `stall_after: N` and `auto_review: true` | after N accepted rows without improvement the round's lanes pause, the Owner is paged, and ① starts as `rr-<tag>` |
| `progress.no_change_after` accepted rows changed nothing | the Owner is paged; start ① by hand |
| a judge or report row closes "bar NOT met" | with `on_fail: course_loop` in its round: the native loop above; otherwise its page names it — start ① by hand |
| the Owner wants to adjust mid-round | start ① by hand, any time |

Mid-round: if the new rows must start from a fixed point, `./orch pause <lane>`
the lanes that would race them first (running sessions finish; nothing new
dispatches). Never kill a session that is waiting on a usage limit.

## ① Review — two ways

**Quick, unattended:** `./orch round review <tag>`. It renders
`rounds/<tag>/REPORT.md` (attempts, redone hours, blocks, refusals, no-op rows,
metric series) and dispatches `rr-<tag>` on Opus, which reads that report, the
scoreboard, `BLOCK_SHEET.md`, the review findings and a sample of PROGRESS
files, and writes:

- `rounds/<tag>/review/BRIEF.md` — the measured state with sources, what moved
  and what did not, the likely reasons, and the proposed next round;
- `rounds/<tag>/review/QUESTIONS.md` — the Owner's decisions in decision-brief form.

**Deep, attended:** open a fresh Opus session and paste
`templates/course_loop/REVIEW_PROMPT.md` (or the project's filled copy) with
its blanks filled. Use it when the gap needs diagnosis in code, data or reports
that the engine's telemetry does not show. It asks the Owner directly and also
writes the planning prompt.

Both are read-only on the rig, the code and the data, and make no paid call.

## ② The Owner answers

One batch, one letter per question. After a quick review the Owner answers in
any session that can write files (or tells the operator): the answers go
verbatim into `rounds/<tag>/review/ANSWERS.md`, one line per question
(`Q3: B — <any words the Owner added>`). After a deep review the session that
asked records them in its prep folder (`DECISIONS_ASKED.md`).

A recommendation is not an answer. Silence is not an answer.

## ③ Plan — unattended

Open a fresh Opus session and paste `templates/course_loop/PLANNING_PROMPT.md`
filled from the brief (the deep review fills it for you). The planner follows
`docs/PLANNING_AGENT_GUIDE.md` and, in order:

1. records each Owner answer as a ruling: `./orch gd next --reserve "<title>"`
   and a short entry in `DECISIONS.md` (the Owner's words verbatim);
2. creates the next round: `./orch round new <next> --title "…" --lanes …`
   (or `round add-lanes <tag> …` when the rows extend the running round), and
   writes only the delta in `rounds/<next>/RULES.md`; changes
   `rules/BASE.md` only where the Owner ruled a standing change;
3. declares a `goal` in `rounds/<next>/round.yaml` only when a real metric
   command exists — never an invented one;
4. cuts one PLAN per row (Acceptance, Verification with a fenced command,
   `Rulings:` line) and the queue lines; re-cuts WAITING rows of the old round
   with a dated block, never rewriting history;
5. `./orch lint`, `./orch preflight --round <next>` (and `--review` when the
   round requires a plan review) until clean;
6. commits its own paths: `./orch commit plan:<next> -m "…" -- <paths>`;
7. starts the round only if the Owner's instruction says so:
   `./orch round start <next>` (it enables the lanes and brings the engine up;
   `./orch unstop` / `./orch resume` first if the rig is stopped or paused).
   **A course loop that runs as a rig row** cannot run `orch round start` or
   `orch reset`: its queue line carries `rearm=round,<judge-id>`, it creates the
   round with `orch round new|add-lanes`, then asks the engine:
   `./orch rearm round-start <next> "loop <n> rows"` and
   `./orch rearm <judge-id> "loop <n>: judge re-run"` (PLANNING_AGENT_GUIDE §1,
   *Unattended course loops*). The engine logs `REARM …` or `REARM REFUSED …`;
   the row checks the lane log before it closes;
8. reports: rows per lane and executor, rulings used, lint/preflight result,
   commit, what it started.

## ④ Run and close

The engine runs the new rows. When a round's own report row closes,
`./orch round close <tag>` archives its lanes (archived rows still satisfy
deps and still appear in `orch report`). If the goal is still not met, the loop
starts again at ①.

## Files

| file | written by |
|---|---|
| `rounds/<tag>/REPORT.md` | `orch round review` (engine telemetry) |
| `rounds/<tag>/review/BRIEF.md`, `QUESTIONS.md` | the `rr-<tag>` session |
| `rounds/<tag>/review/ANSWERS.md` | the Owner (or the operator, verbatim) |
| `rounds/<next>/round.yaml`, `RULES.md` | the planner (`orch round new`) |
| `DECISIONS.md` entries | the planner (ids from `orch gd next --reserve`) |
| `templates/course_loop/*.md` | framework — copy into the project to add its specifics |
