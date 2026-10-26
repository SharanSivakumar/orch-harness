# Planning-agent guide

**Read this if you have been asked to plan a cycle, a phase, or a batch of
work that will be executed by the orchestration harness.**

Your job is to produce a ladder the rig can run without you. This document is
the contract. Satisfy it and `orch lint` passes and `orch start` executes your
plan; miss it and the rig stops, correctly, at the first gap.

Hand this file to your planning agent along with the goal. Nothing else about
the framework needs to be explained to it.

**Start here on an existing rig:** read the instance's `RIG.md` (paths,
executors, scheduling mode, review policy, resources, rounds — regenerate it
with `./orch setup card` if `orch.yaml` changed), then the workspace's
`rules/BASE.md`, then `./orch status`. Setting up a rig is not your job: if
there is no instance yet, a setup session does that first
(`docs/SETUP_AGENT_GUIDE.md`). A course correction follows
`docs/COURSE_LOOP.md` step ③.

---

## 1. What you are producing

For a new round on an existing rig (the rig itself — instance, `orch.yaml`,
`rules/BASE.md`, `RIG.md` — comes from the setup session):

1. Read `RIG.md` and `rules/BASE.md`; plan to the behaviour they describe
   (in `dag` mode every order must be a declared dep; with `review.policy:
   none` a builder that can be wrong needs a falsifier row on another model).
2. Pick an unused round tag and row-id prefix (`./orch round list`).
3. **A round** — `orch round new <tag> --title "…" [--preflight-required]`. Put
   the round's rules delta in `rounds/<tag>/RULES.md` (only what differs from
   `<workspace>/rules/BASE.md`; never re-state standing rules, never write
   "this section wins over §N" — the round file *is* the delta).
4. **One `PLAN.md` per row** — the actual work, one folder per row.
5. **`queue_<LANE>.tsv`** — ids, dependencies, gates, tiers, resources; then
   `orch round add-lanes <tag> <LANES…>`. A planner **session** (`pl-<tag>-<n>`) and a
   course-loop row add every line with `orch row add` (§5), never by editing the file.
6. **Rulings** — every decision you make that rows must follow gets an id from
   `orch gd next --reserve "<title>"` and a short entry in DECISIONS.md. Never pick
   an id by hand.

Then `orch lint` and `orch preflight --round <tag>` until both are clean, and —
when the round requires it — `orch preflight --round <tag> --review` for the
independent plan review. Commit with `orch commit plan:<tag> -m "…" -- <paths>`.
That is the definition of done for a planning session. **Never start the round
yourself** (`orch round start <tag>` is the operator's decision) unless the
operator's instruction says to.

### Unattended course loops and planner rows: `rearm=` (engine re-arm)

A row session can never run `orch reset` or `orch round start` (operator commands).
A planner or course-loop **row** that must re-run a judge after cutting fix rows, or
start the round holding them, asks the engine instead:

- its queue line carries `rearm=<judge-id>[,<other-id>…],round` (written by the
  planner/operator; the engine reads it from the row's **dispatch snapshot**, so
  editing the queue line after dispatch grants nothing);
- `orch rearm <judge-id> "<reason>"` — the engine resets that row on its next tick,
  through the operator's own reset code path, and logs `REARM <id> by <row>`;
- with `round`: the row may run `orch round new <tag> --lanes <L>` and
  `orch round add-lanes` itself, and asks for the start with
  `orch rearm round-start <tag> "<reason>"`.

Refused (and logged `REARM REFUSED`): no grant in the snapshot, the row itself,
any ub-/rv-/pf-/rr- session, a target that is running or has an unjudged claim,
and more than `scheduler.rearm_max` (default 5) re-arms per caller/target pair.
A course-loop row that copies itself for the next loop gives the copy the same
`rearm=` extra.

### Judges, falsifiers and GO rows: `--outcome` and `needs=go`

A claim says the work was done honestly; it does not say whether the answer was
GO. A row whose job is a verdict — a judge, a falsifier, a GO / NO-GO gate, a
measurement against a bar — closes with its outcome, and its PLAN's close section
says so:

- `orch done <id> "…" --outcome go|nogo|held|fail|infeasible` (seal line
  `ORCH_CLOSE <id> CLOSED:<outcome> — …`). A NO-GO is a successful, measured
  result: the row is still CLOSED and accepted. `infeasible` (v3.1) says *the
  remaining plan cannot reach the goal*. The route is then replanned toward the
  same goal (below), and the row is never re-run to obtain a GO.
- A row that may only run after a **GO** carries the queue extra `needs=go`: each
  dep it names must be accepted with outcome `go`, or with no outcome. Any
  negative outcome (`nogo`, `held`, `fail`, `infeasible`) holds it, and the Steward lane opens a
  `negative_outcome` incident to decide the course. A dependent that should run
  either way (a report, a course-loop review) carries no `needs=go`.
- Never cut a judge as a plain `orch done`, and never give its GO-only dependents
  a bare dep: that is how NO-GO and HELD rows let their dependents run.
- **The native course loop (v3).** When the Owner's instruction says the round
  corrects itself, the operator's `round.yaml` declares `judge: <judge-id>`,
  `on_fail: course_loop` and `max_loops: <n>` (a round a session creates loops only
  in a phase the Owner opened with `--session-loops on`, within its loops cap), and
  its checkpoints in `judges:` (v3.1, § Waves). Then a negative outcome runs review → answers → planner → rows → re-armed
  judge with no human, inside the open phase and its loops cap
  (`docs/COURSE_LOOP.md`). Do not also cut a course-loop row for that judge, and
  never reset a judge accepted with a negative outcome — the engine refuses it.
- **Every judge and checkpoint PLAN carries the judge block**
  (`templates/parts/judge_block.md`, verbatim). It asks "Is the bar met?" and "Can
  the remaining plan still reach the goal?", and says which outcome closes each case.

### Waves: plan what you can specify now, and let checkpoints steer the rest (v3.1)

A planning session cannot see 60 rows ahead. Plan in waves; the goal and the bars
are fixed, and only the route changes.

| piece | what it is | queue line |
|---|---|---|
| **wave 1** | only the rows you can specify now, each with a falsifiable Verification | as usual |
| **checkpoint judge** | a verdict row at the end of a wave, PLAN with the judge block; declared in `round.yaml` `judges:` so a negative outcome runs its own course loop (a *replan*) | deps: the wave's rows |
| **replan row** | reads the wave's measurements and the checkpoint's VERDICT, then adds the next wave, the next checkpoint and the next replan row with `orch row add` (each line's `rearm=` a subset of its own); when a pre-cut checkpoint's inputs are ready it arms it (`orch rearm <checkpoint>`) | `needs=go` on the checkpoint, `rearm=<checkpoint judge>,round` |
| **final judge** | the round's bar, its PLAN written **in wave 1** before any result exists; `round.yaml` `judge:` | added by the last replan row (deps: the last wave), or pre-cut and armed by a row **you** cut with `rearm=<final judge>` (no session may grant it) |

`round.yaml`:

```yaml
judge: p9F1                  # the round's final judge: a failure loop on its negative outcome
judges: [p9AJ1, {row: p9CJ1, max_loops: 2}]   # checkpoint judges: each its own loop (a replan)
on_fail: course_loop
max_loops: 1                 # default per judge
bars: [PHASE_P9/p9K1_scorer/BARS.md]   # optional: files the loop pins with the judge PLAN and goal:
```

Rules for waves:

- **Never plan rows whose shape depends on results not yet measured.** A replan row
  cuts them once the results exist.
- **Declare checkpoints up front.** No command writes `judges:`. List every
  checkpoint id of the planned waves when you plan the round. An id with no row yet is
  reported once and loops as soon as a replan row adds it.
- **A pre-cut judge closes with no outcome.** A judge cut before its inputs exist
  closes `NOT ARMED` with no outcome until armed, never `held`: `held` is negative, so on
  a loop judge it starts the loop. No outcome counts as met for `needs=go`, so its
  GO-only dependents are cut in the row that arms it, never before.
- **What a replan row may do.** It needs `rearm=…,round` to add rows. It may hand a
  checkpoint judge's grant to the next replan row while that judge holds no negative
  outcome. No session's line may grant `rearm=` of a round's `judge:`, or of any row
  accepted `infeasible`. An arming grant for the final judge is therefore always on a
  line you cut yourself.
- **Bars are written once.** Checkpoint PLANs cite the round's pre-registered bars file
  (list it in `bars:`) rather than restating numbers. A loop pins the judge PLAN, the
  `goal:` and the `bars:` files when it starts. If any of them changes, the loop stops
  before the re-arm, and the judge is never re-run against a moved bar.
- **Leave row-cap headroom.** The phase's `rows` cap counts every row a later wave or
  a loop adds. Plan wave 1 with at most about half the cap, and keep room for each
  remaining wave and a few rows per loop the phase allows (`caps.loops`, and
  `caps.replans` when the Owner set it).
- **Rounds you create.** A round a planner session creates (`by:`) loops only when
  its phase allows session loops (`orch phase open <P> --session-loops on`, the
  Owner's), always within `caps.loops`. Otherwise its negative checkpoint goes to the
  Steward's `negative_outcome` incident.

When a checkpoint closes:

| outcome | what happens |
|---|---|
| `go` | the replan row runs |
| `nogo` | the checkpoint's loop corrects the wave, then re-runs the checkpoint on the same bar |
| `infeasible` | the rest of the plan is not run. The loop reviews, the Steward (or the panel) parks the held rows the new route makes moot and plans a new route to the same goal, and the planner cuts it. The checkpoint re-measures the same bar |

## 2. The instance

If there is none yet, stop: a setup session creates it
(`docs/SETUP_AGENT_GUIDE.md`: `orch init … --preset <project>`, the Owner's
behaviour answers, `rules/BASE.md`, `RIG.md`). Never re-run `orch init` on an
existing instance, and change behaviour switches only with
`./orch setup apply` when the Owner asked for it.

## 3. Decide the row decomposition

**A row is a unit of work one fresh agent session can finish, alone, and prove.**

That is the whole heuristic, and it has three consequences:

- **Too big** is the common failure. If a row needs three unrelated skills or
  cannot be described in one plan, it is two rows. Sessions that run for hours
  hit context limits, and a limit hit mid-row wastes the whole row.
- **Too small** costs a full session's startup for a one-line change. Fold it
  into the row that already has the context.
- **Every row must be provable.** If you cannot name what its report pack
  contains and what would make it false, the row is not planned yet — it is a
  wish. Write the acceptance line first, then the duties.

Order rows so that **a review row follows any batch of rows that could be
wrong together**. Reviews are how a ladder catches its own drift.

## 4. Write one PLAN.md per row

Put it at `<workspace>/PHASE_<X>/<row_folder>/PLAN.md`. The *directory* of this
path becomes the row's write allowlist automatically, so **a row's plan folder
should be the place its work lands**.

A plan that executes cleanly has these sections. Keep the names — executors
are told to look for them:

```markdown
# PLAN — <id>: <one-line title>

## 0 · STOP-if
The conditions under which this row must stop rather than proceed. Be
concrete: "if the manifest has fewer than 40 rows, STOP" beats "if inputs
look wrong".

## 1 · Preconditions
What must already be true, and how to CHECK it — the artifact to open and
what it must say. The executor is told to verify these in evidence terms,
not to assume them.

## 2 · Duties
The actual work, numbered. Each duty names the files it touches and what
"done" looks like for that duty specifically.

## 3 · Close (MANDATORY FINAL ACT, in order)
Report pack only if this row needs one → PROGRESS.md (with the exact command
that verifies the work) → commits through the guard → `orch done <id> "…"`
(or `orch blocked` / `orch escalate`), which runs the close checks. A judge or
falsifier row adds `--outcome go|nogo|held|fail|infeasible` (§1) and carries the judge block.

## Acceptance
What must be true for this row to be CLOSED, stated so a reviewer who was not
there can check it.

## Verification
```bash
# the command(s) that prove Acceptance — and FAIL when the work is not done
```
```

**Acceptance and Verification are not optional.** `orch preflight` errors on a
plan without Acceptance, and with `--run` it executes the Verification block
once before the row has run: a block that already passes cannot tell done from
not-done, and is flagged. When the rig's `review.policy` (or the row's
`review=1`) asks for it, an independent reviewer on a different model re-runs
exactly this block against the row's diff after the row claims CLOSED.

**Rulings.** Name the rulings the row must follow on one line:
`Rulings: DEC-12, DEC-31`. The work order inlines their full text; the executor
never reads the whole ledger. A row that needs no ruling has no Rulings line.

**Machine acceptance (optional).** An executable `ACCEPT.sh` in the row folder
(or `accept=<file>` in the queue) runs from the project root when the row claims
CLOSED; a non-zero exit refuses the claim.

**Files in scope.** If a duty writes outside the plan's own folder, say so
explicitly in the duty *and* add the path to the queue's 6th column (§5).
A row that tries to stage an undeclared path is refused by the guard — this is
working as intended, but it costs a dispatch, so declare it up front.

## 5. Write the queue

`queue_<LANE>.tsv`, one row per line, whitespace-separated:

```
# id   deps      gate  executor  plan_path                        [extra allowlist]
setup  -         0     opus      PHASE_A/setup/PLAN.md
b0     setup     0     opencode  PHASE_A/b0_census/PLAN.md
l1     b0        1     opencode  PHASE_A/l1_land/PLAN.md
r1     l1,b0     0     opus      PHASE_A/r1_review/PLAN.md        PHASE_A/l1_land
```

| Column | Rule |
|---|---|
| `id` | short, unique across ALL lanes, stable — it names the window, the sentinel, the prompt and the report prefix. Never rename a live row. |
| `deps` | comma-separated ids, or `-`. Met when the dep's sentinel says `CLOSED`. Cross-lane deps are fine and are how lanes synchronize. |
| `gate` | `1` = the lane parks here until a human runs `orch approve <id>`. |
| `executor` | the default tier for this row. Overridable at runtime; never edit the queue to retier. |
| `plan_path` | relative to the workspace. |
| extras | `key=value` metadata — `res=a,b` (resource slots taken at dispatch), `review=0/1`, `reviewer=<exec>`, `timeout=<min>`, `prio=<n>`, `accept=<file>`, `rearm=<ids>[,round]` (engine re-arm, see §1), `needs=go` (the deps named must be accepted with outcome go or none — `nogo`, `held`, `fail` and `infeasible` hold the row, see §1), `gate_kind=steward\|owner\|spend`; other tokens are extra allowlist paths. |

**Lanes.** A lane is a queue file and a unit of enabling (`orch start <L>`).
In `scheduler.mode: dag` rows run as soon as their deps are accepted, whatever
their lane; in `serial` mode a row also waits for every row above it in its
lane. Express sequence with deps, not position, so either mode is right.

**Resources.** Anything two rows could corrupt by running at once (a database,
a tree being measured before/after, a rate-limited key) is a resource in
`guard.resources` — with `slots: N` when N rows may share it and `scope: host`
when other rigs on the machine use it too. Declare it on the row with `res=`;
the engine takes every slot the row needs all-or-nothing before dispatch.

**Gates.** Put a `1` where a human must look before the ladder continues:
before anything that spends money, writes somewhere shared or remote, or
commits to a direction that is expensive to reverse. A gate is cheap. A gate
you wish you had added is not.

**Appending later is safe and expected.** The watcher re-reads the queue every
pass. A planning session can add rows to a running lane — write the plan, write
the prompt if it needs a custom one, then append the row with a dep on the row
before it.

**`orch row add` (v3) — how sessions add rows.** A planner session `pl-<tag>-<n>`
(lanes of its own round) and a course-loop row whose queue line carried
`rearm=…,round` at dispatch never edit a queue file; they run

```
./orch row add <LANE> "<id> <deps> <gate> <executor> <plan_path> [extras]" "why"
```

one line per call, in dependency order. The engine validates the line and appends
it (the CLI waits for its answer, DONE or REFUSED with the reason):

- the id is **new**, never used on the rig before — a line never replaces a row, so no
  gate is ever removed or its `gate_kind` changed (that is refused by name); no
  `ub- rv- pf- rr- sw- pl-` prefix;
- deps exist (add rows in order), the executor and reviewer are allowed, the plan file
  exists in the workspace in a folder of its own (not another row's — no twins),
  `gate_kind` only on a gated row and never `steward` from a session;
- `rearm=` is never wider than the caller's own: a course-loop row grants a subset of
  its own snapshot grants and never itself; a planner grants rows of its round's
  lanes, never `round`; a session may not set `review=0`;
- lanes: a planner adds to its round's lanes; a course-loop row to its own lane and
  the lanes of rounds it created; neither may take another open round's lane;
- the phase fence and the phase's row cap (drop D).

The operator may use it too (validated the same way, appended at once) and may still
edit a queue by hand. With `guard.queue_watch` on, the engine records the queues
(`state/queues.sha`) and the digest reports any edit made outside `orch row add`
unless the operator committed it with `orch commit plan:<tag>` within
`guard.queue_watch_grace` (600 s; a gate removed or changed is reported at once) or
accepted it with `orch row sync`; `orch row diff` shows what differs.

## 6. Choose tiers honestly

| Tier | Use it when | It behaves like |
|---|---|---|
| `opus` | the row carries a big decision: design, adjudication, a judge, a ruling's application | full autonomy inside the plan; escalates rather than guessing |
| `sonnet` | most judgment rows, including some bigger choices the plan bounds | as above |
| `haiku`, `opencode`, `gemini` | the row is mechanical and the plan is complete | junior: implements exactly, **stops** at the first judgment call |
| `codex` | a well-specified mechanical change | as above, different engine |
| `manual` | only a human can do it | the lane waits and announces the work order |
| `mock` | you are testing the ladder itself | writes a scripted sentinel, no model, no spend |

A junior row that stops is **not** a failure — the auto-unblocker picks it up,
fixes the *plan*, and re-runs the row. But it costs a cycle, so: **if you find
yourself writing "use your judgment" in a plan, the row is not a junior row.**

Decisions belong to Opus, and to Sonnet within what a plan bounds — never to a
junior tier. Haiku is junior whatever a rig's `executors.flavors` says, and no
junior tier may unblock, review, plan, steward or take a row over (`orch lint`
refuses it; the engine will not dispatch it). A row that needs any decision is
an `opus` or `sonnet` row.

Before a first real run, consider setting every row to `mock` and running the
ladder end to end. It proves the dependency graph, the gates and the allowlists
in about a minute, at zero cost.

## 7. `orch.yaml` (the setup session's job)

You normally change nothing here. For reference, the parts a plan depends on:

```yaml
project:
  root: "../../.."         # executors' cwd — check this resolves to your project root
  workspace: ".."          # plan paths are relative to this
  tmux_session: "cycle13"  # MUST be unique; never reuse a live session name

guard:
  git_repo: ""             # empty = the workspace. Set it if commits land elsewhere.
  git_lock: "docs-git"     # must also appear in resources below
  shared_paths:            # files EVERY closing row touches
    - "DECISIONS.md"
  resources:               # anything two rows could collide on
    - "docs-git"
    - "staging-db"
  compliance_scanners: []  # scripts called as <scanner> --check <file>...
```

**Resources** are the point of the lease system. List anything two concurrent
rows could corrupt by touching at once: a git index, a database, an API key
with a rate limit, a GPU. Then say so in the plans: a duty that touches one
must run under `orch hold <id> <resource> -- <command>`.

## 8. Prompt templates

The shipped templates carry mechanism only. Override them per-instance by
putting a file in `<instance>/prompts/`:

| File | Used for |
|---|---|
| `_TEMPLATE_HARD.md` | `opus`, `sonnet` |
| `_TEMPLATE_JRDEV.md` | `haiku`, `opencode`, `gemini` |
| `_TEMPLATE_CODEX.md` | `codex` |
| `_TEMPLATE_MANUAL.md` | `manual` |
| `_TEMPLATE_UNBLOCK.md` | auto-unblocker sessions |
| `_TEMPLATE_REVIEW.md` | independent reviewers (`rv-<id>-<n>`) |
| `_TEMPLATE_PLANREVIEW.md` | plan reviewers (`pf-<scope>`) |
| `_TEMPLATE_ROUNDREVIEW.md` | round reviewers (`rr-<tag>`) |
| `_NUDGE*.txt` | what an idle session is told |
| `_PART_<name>.md` | a shared block, included with `{{> name}}` |
| `_KICK_*.txt` | the short text pasted into paste-driven TUIs |

Placeholders the engine fills: `__ID__` `__LANE__` `__MODEL__` `__PLAN__`
`__WORKSPACE__` `__ORCHDIR__` `__REPORTS__` `__PROJECT__` `__TMUX__`
`__SENTINEL__` `__CLOSECMD__` `__BLOCKCMD__`, plus `__UB__` `__N__` `__MAX__`
`__EXEC__` `__REASON__` in unblock templates. `__SUB__` is an alias of
`__ID__`.

A per-row work order can also be hand-written to
`<instance>/prompts/<id>.<flavor>.md` without the `orch:generated` first line —
the engine never overwrites it. Do this only when a row needs context no
template can carry. **Never put round-specific paragraphs into templates**; they
go in `rounds/<tag>/RULES.md`.

## 9. Validate, then stop

```bash
./orch lint
./orch preflight --round <tag> [--run] [--review]
```

Fix every ERROR. Warnings are judgment calls — a missing plan file is expected
while you are still writing them, an empty allowlist is usually a real mistake.

Then **stop and hand over**. Report to the operator:

- the ladder, in order, with tiers and dependencies
- every gate you placed and what they are asked to decide at each one
- anything you could not resolve and what you assumed instead
- the one command that starts it: `./orch round start <tag>` (it enables the
  round's lanes and brings the engine up; `./orch unstop` / `./orch resume`
  first if the rig is stopped or paused)

## 10. Rules that are not negotiable

1. **Never write any `state/` file by hand.** A row claims itself with
   `orch done|blocked|escalate`; sub-sessions with `orch verdict`. A planner or
   course-loop session adds rows with `orch row add`, never by editing a queue.
2. **Never edit the queue to retier a row.** That is `orch assign`.
3. **Never pre-create a `control/GO_*` file.** That is the operator releasing
   a gate; creating it in advance silently disarms the gate.
4. **Never plan a row that ends by waiting for a human** unless its executor is
   `manual`. Agent rows end with a sentinel — `CLOSED`, `BLOCKED` or `ROUTE_A`.
5. **Never reuse a row id**, including for a re-run. Reset it or name it anew.
6. **Never point two instances at one workspace with the same tmux session.**
   Separate `state/` and separate sessions, or they will fight over rows.
