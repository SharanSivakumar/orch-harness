# Planning-agent guide

**Read this if you have been asked to plan a cycle, a phase, or a batch of
work that will be executed by the orchestration harness.**

Your job is to produce a ladder the rig can run without you. This document is
the contract. Satisfy it and `orch lint` passes and `orch start` executes your
plan; miss it and the rig stops, correctly, at the first gap.

Hand this file to your planning agent along with the goal. Nothing else about
the framework needs to be explained to it.

---

## 1. What you are producing

Five things, in this order:

1. **An instance directory** — usually `<your-cycle-folder>/ORCHESTRATION`.
2. **`orch.yaml`** — how this instance is wired.
3. **One `PLAN.md` per row** — the actual work, one folder per row.
4. **`queue_<LANE>.tsv`** — the ladder: ids, dependencies, gates, tiers.
5. **Prompt templates** (only if the shipped ones do not carry your doctrine).

Then run `orch lint` until it is clean. That is the definition of done for a
planning session. **Never dispatch anything yourself** — starting the ladder
is the operator's decision, not the planner's.

## 2. Create the instance

```bash
orch-harness/bin/orch init <project-folder>/rig \
  --name "My project" --session myproject [--preset strict]
```

This writes `orch.yaml`, an empty `queue_A.tsv`, the state directories, and a
self-locating `./orch` wrapper. Use `--preset strict` for the extra secret-handling, long-run and close-check clauses.

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
Pack REPORTS/<id>_<yyyymmdd>/ → PROGRESS.md → commits through the guard →
state/<id>.status LAST.

## Acceptance
What must be true for this row to be CLOSED, stated so a reviewer who was not
there can check it.
```

**Files in scope.** If a duty writes outside the plan's own folder, say so
explicitly in the duty *and* add the path to the queue's 6th column (§5).
A row that tries to stage an undeclared path is refused by the guard — this is
working as intended, but it costs a dispatch, so declare it up front.

## 5. Write the queue

`queue_<LANE>.tsv`, one row per line, whitespace-separated:

```
# id   deps      gate  executor  plan_path                        [extra allowlist]
setup  -         0     opus      PHASE_A/setup/PLAN.md
b0     setup     0     hermes    PHASE_A/b0_census/PLAN.md
l1     b0        1     hermes    PHASE_A/l1_land/PLAN.md
r1     l1,b0     0     opus      PHASE_A/r1_review/PLAN.md        PHASE_A/l1_land
```

| Column | Rule |
|---|---|
| `id` | short, unique across ALL lanes, stable — it names the window, the sentinel, the prompt and the report prefix. Never rename a live row. |
| `deps` | comma-separated ids, or `-`. Met when the dep's sentinel says `CLOSED`. Cross-lane deps are fine and are how lanes synchronize. |
| `gate` | `1` = the lane parks here until a human runs `orch approve <id>`. |
| `executor` | the default tier for this row. Overridable at runtime; never edit the queue to retier. |
| `plan_path` | relative to the workspace. |
| 6th column | optional extra paths this row may stage. |

**Lanes.** Put work in separate lanes when it is genuinely independent — they
run concurrently. Use dependencies, not lane order, to express sequence. One
lane per queue file; the lane letter is the filename.

**Gates.** Put a `1` where a human must look before the ladder continues:
before anything that spends money, writes somewhere shared or remote, or
commits to a direction that is expensive to reverse. A gate is cheap. A gate
you wish you had added is not.

**Appending later is safe and expected.** The watcher re-reads the queue every
pass. A planning session can add rows to a running lane — write the plan, write
the prompt if it needs a custom one, then append the row with a dep on the row
before it.

## 6. Choose tiers honestly

| Tier | Use it when | It behaves like |
|---|---|---|
| `opus` / `sonnet` / `haiku` | the row needs judgment, review, design, or adjudication | full autonomy inside the plan; escalates rather than guessing |
| `hermes`, `gemini`, `opencode` (scoped) | the row is mechanical and the plan is complete | scoped: implements exactly, **stops** at the first decision the plan does not make |
| `codex` | a well-specified mechanical change | as scoped, on a different engine |
| `manual` | only a human can do it | the lane waits and announces the work order |
| `mock` | you are testing the ladder itself | writes a scripted sentinel, no model, no spend |

A scoped row that stops is **not** a failure — the auto-unblocker picks it up,
fixes the *plan*, and re-runs the row. But it costs a cycle, so: **if you find
yourself writing "use your judgment" in a plan, the row is not a scoped row.**

Before a first real run, consider setting every row to `mock` and running the
ladder end to end. It proves the dependency graph, the gates and the allowlists
in about a minute, at zero cost.

## 7. Configure `orch.yaml`

Most defaults are right. The ones that need your attention:

```yaml
project:
  root: "../../.."         # executors' cwd — check this resolves to your project root
  workspace: ".."          # plan paths are relative to this
  tmux_session: "myproject"  # MUST be unique; never reuse a live session name

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
| `_TEMPLATE_LEAD.md` | `opus`, `sonnet`, `haiku` |
| `_TEMPLATE_SCOPED.md` | `hermes`, `hermesflash`, `gemini`, `opencode` |
| `_TEMPLATE_CODEX.md` | `codex` |
| `_TEMPLATE_MANUAL.md` | `manual` |
| `_TEMPLATE_UNBLOCK.md` | auto-unblocker sessions |
| `_KICK_*.txt` | the short text pasted into paste-driven TUIs |

Placeholders the engine fills: `__ID__` `__LANE__` `__MODEL__` `__PLAN__`
`__WORKSPACE__` `__ORCHDIR__` `__REPORTS__` `__PROJECT__` `__TMUX__`
`__SENTINEL__` `__CLOSECMD__` `__BLOCKCMD__`, plus `__UB__` `__N__` `__MAX__`
`__EXEC__` `__REASON__` in unblock templates. `__SUB__` is an alias of
`__ID__`.

A per-row work order can also be hand-written to
`<instance>/prompts/<id>.<flavor>.md` — the engine never overwrites one that
already exists. Do this when a row needs context no template can carry.

## 9. Validate, then stop

```bash
./orch lint
```

Fix every ERROR. Warnings are judgment calls — a missing plan file is expected
while you are still writing them, an empty allowlist is usually a real mistake.

Then **stop and hand over**. Report to the operator:

- the ladder, in order, with tiers and dependencies
- every gate you placed and what they are asked to decide at each one
- anything you could not resolve and what you assumed instead
- the one command that starts it: `./orch start`

## 10. Rules that are not negotiable

1. **Never write another row's `state/*.status`.** A row writes its own
   sentinel, once, as its final act. Nothing else.
2. **Never edit the queue to retier a row.** That is `orch assign`.
3. **Never pre-create a `control/GO_*` file.** That is the operator releasing
   a gate; creating it in advance silently disarms the gate.
4. **Never plan a row that ends by waiting for a human** unless its executor is
   `manual`. Agent rows end with a sentinel — `CLOSED`, `BLOCKED` or `ESCALATED`.
5. **Never reuse a row id**, including for a re-run. Reset it or name it anew.
6. **Never point two instances at one workspace with the same tmux session.**
   Separate `state/` and separate sessions, or they will fight over rows.
