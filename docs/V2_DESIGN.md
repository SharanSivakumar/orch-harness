# orch v2 — design

> Generation note: “v2” is the design generation of the single-engine
> architecture. It ships in orch-harness 2.0 and later; it is not a release
> number by itself.

v2 keeps everything an operator or an executor already relies on — visible tmux
windows, `queue_<L>.tsv`, `state/<id>.status`, `control/GO_*`, `orch done`,
`orch commit`, `orch hold` — and replaces the part that kept costing rows: one
bash watcher per lane that trusted any sentinel it saw.

Evidence for every change below comes from long-running rigs: many closed rows
landed nothing, and most block-sheet stops were plan, tool, framework or
environment defects.

## 1. One engine per rig

`orch start` brings up ONE Python engine (`lib/engine.py`) in the tmux window
`orch-engine`. It replaces the per-lane `orchestrator_core.sh run <L>` watchers.

* **Single writer.** One process schedules, monitors, validates and reaps, so
  there is no bash/Python drift, no N-watcher race on BLOCK_SHEET,
  and an 80-lane rig costs one process, not 80.
* **Hot reload.** `orch.yaml` is re-read whenever its mtime changes.
* **Single-instance lock** (`state/engine.lock`, flock) and a heartbeat
  (`state/engine.heartbeat`), so liveness is a lock question, not a `ps` grep.
* **Lanes are enabled explicitly**: `orch start A B` writes `control/RUN_A`,
  `control/RUN_B`; `orch start` alone enables every active lane. A lane with no
  `RUN_` marker is never dispatched (the v1 "only started lanes run" semantics).

## 2. Scheduling: rows, not lanes

* `scheduler.mode: serial` (default, v1 semantics: a row waits for the row
  above it in its lane) or `dag` (only declared deps). Per lane:
  `scheduler.lane_modes: {X: dag}`.
* **Capacity.** `scheduler.max_parallel` sessions in total, optional
  `executors.max_parallel.<executor>`, and **RAM admission**: no dispatch while
  `MemAvailable < scheduler.min_free_mb`.
* **Resources with slots.** `guard.resources` entries may be
  `{name: local-db, slots: 3, scope: host}`. A row declares `res=a,b` in its
  queue line; the engine takes one slot of each **all-or-nothing** at dispatch
  and frees them when the session ends. `scope: host` locks live in
  `~/.local/state/orch/leases`, shared by every rig on the host.
* **Stale leases** are detected (holder pid dead, or owner row not running) and
  reclaimed with an event line (stale locks used to sit until an operator removed them).
* **Priority**: `prio=<n>` (higher first), then lane order, then queue order.

Queue lines stay `id deps gate executor plan [extras…]`. Extra tokens that
contain `=` are row metadata; the rest stay allowlist extras:

```
gS3  gS1  0  sonnet  PHASE_G/gS3/PLAN.md  res=app-db  review=1  reviewer=opus  timeout=120  prio=5
```

## 3. Honest close pipeline (every path)

A row's `state/<id>.status` is now a **claim**. Whatever wrote it — the agent,
`orch done`, or an auto-seal — the engine validates it before any dependent
may start:

1. no stop line in the row's evidence unless a properly signed ruling covers it
   (fixed: the ruling's own heading/status line must say RULED or
   RATIFIED; a PROPOSED entry that quotes "RULED" no longer counts);
2. `PROGRESS.md` exists and was written after dispatch (`close.require_progress`);
3. the summary is not a template placeholder;
4. an acceptance probe passes when the row has one (`<plan dir>/ACCEPT.sh` or
   `accept=<file>`), run from the project root with a timeout;
5. commits since dispatch are counted (required only with
   `close.require_commit: true`).

`orch done` runs the same checks synchronously, so the executor sees a refusal
while it can still fix it. A failed check rewrites the claim to BLOCKED (the
original is kept as `state/<id>.claim.<attempt>.status`) and the row is
re-dispatched with the findings (`close.on_fail: redispatch`, capped by
`close.max_fail`) or sent to the unblocker.

Auto-seal (`ORCH_CLOSE <id> <VERDICT> — …`) now works for every executor and
only from the final turn: the line must be among the last lines of an idle pane.

## 4. Review gates

**After close.** With `review.policy: all|junior|none` (default `all`) a
validated CLOSED row goes to `REVIEWING`: the engine dispatches `rv-<id>-<n>`
on a *different* executor (`review.reviewer_map`, `reviewer=` per row). The
reviewer re-runs the PLAN's verification, checks Acceptance and the honesty
rules against the diff since dispatch, writes `reviews/<id>-<n>.md` and closes
with `orch verdict rv-<id>-<n> ACCEPT|REJECT "…"`. Dependents wait for
`ACCEPTED`. A REJECT re-dispatches the row with the review attached
(`review.on_reject: redispatch|unblock`, `review.max_rejects`).

**Before dispatch.** `orch preflight [--round R | ids]` lints the round's
PLANs mechanically (Acceptance/Verification present, not DRAFT, ruling ids
exist, `orch hold` resources exist, GO files not pre-released, `res=` names
known) and with `--run` executes each Verification block once: a block that
already passes before the row has run is flagged as possibly unable to fail.
`--review` dispatches a plan-review session `pf-<R>` (default opus). A round
with `preflight.required: true` is not dispatched until `pf-<R>` says PASS.

## 5. Rounds, archive, decisions, rules

* `orch round new|add-lanes|start|status|close|list` — a round is a tag, its
  lanes, an optional goal, and an optional `rounds/<tag>/RULES.md` delta.
* `orch archive <lanes>` / `orch round close` move finished queues to
  `archive/`. Archived rows still satisfy deps and still show in `orch report`;
  they are never polled or drawn.
* `orch gd next|index|digest|show|grep|set-status|check` — decision ids are
  allocated under a lock (hand allocation could collide when two sessions
  appended at once), `decisions/ACTIVE.md` is a compact digest of live rulings,
  and the archive is never rewritten.
* A PLAN line `Rulings: DEC-489, DEC-493` inlines exactly those rulings into
  the work order. Templates point at `rules/BASE.md` + the round delta instead
  of "read all of DECISIONS.md and EXECUTION_RULES.md".

## 6. Goal (optional) and progress signal

A rig or round may declare `goal: {metric_cmd, name, direction, target,
instrument, stall_after}`. After each ACCEPTED row (or `orch score`) the metric
runs and lands in `scoreboard.tsv`; a changed `instrument` starts a new series
instead of reading as movement. `stall_after: N` accepted rows with no
improvement pauses the round's lanes and pages.

Without a goal, the engine still counts consecutive accepted rows that changed
nothing (no commits since dispatch and a no-op summary — INERT, NOT NEEDED,
SUPERSEDED …) and notifies at `progress.no_change_after`.

## 7. Session hygiene

* **Reaper**: windows of accepted/reviewing rows and finished unblock/review
  sessions are killed after `reaper.closed_grace_secs`; dead panes are cleared.
  Replaces a cron reaper.
* **Idle nudge**: a session idle past `idle.nudge_after` with no claim gets
  `_NUDGE.txt` typed into it (max `idle.max_nudges`), then optionally BLOCKED
  (`idle.block_after`). Replaces a separate idle-nudge script.
* **Usage limits** are detected for every executor from the pane tail only,
  with per-executor patterns and resume text.
* **Dead windows** record tmux's exit status (`dead.<attempt>`), never pane text.
* **Launch args/env** per executor in `orch.yaml` (`executors.args`,
  `executors.env`) replace `claude-effort-high`-style wrapper scripts.
* **Hooks**: `hooks.on_dispatch|on_claim|on_accept|on_block|on_halt|on_idle`.
* **Periodic checks**: `watchdog.checks: [{cmd, every}]` (quota watch etc.).
* A launch failure writes BLOCKED instead of leaving `exec bash` behind.
* `GO_<id>` records the plan's sha256; a plan edited after approval re-gates.

## 8. Telemetry and retries

`state/attempts.jsonl` is append-only and survives `orch reset`: dispatch,
claim, check results, review, unblock, retier, dead, idle, limit, accept,
reap. `orch report [--round R]` gives per-row attempts and wall-clock, redo
hours, unblock/reject counts, per-executor medians and no-op rows. A
re-dispatched row's work order carries a **Previous attempts** section (block
reasons, failed checks, review findings, unblocker analyses).

Work orders are re-rendered at every dispatch unless hand-written (no
`orch:generated` marker). Templates support `{{> part}}` includes.

## 9. Framework hygiene

`orch start` refuses to run from a dirty framework tree (`ORCH_ALLOW_DIRTY=1`
overrides on the record) and warns when the current commit has not passed
`tests/run_all.sh`. Each rig's `./orch` shim pins a framework checkout, so a
framework change reaches a rig only when its shim is moved.

## 10. Migration

`orch migrate [--apply] [--archive-complete] [--retire-stale-controls]`:
refuses while v1 lane watchers run; stamps `state/engine.adopted` (rows CLOSED
before adoption are grandfathered as ACCEPTED); archives fully closed lanes;
retires `EXECUTOR_*`/`GO_*` controls of closed rows to `control/.retired/`;
reports stale leases. Dry run by default.
