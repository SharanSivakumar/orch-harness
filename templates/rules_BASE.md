# <PROJECT> — standing rules (BASE)

<!-- orch:setup-skeleton — written by `orch init`. The setup session fills every TODO(setup) with the Owner,
     then deletes this comment. `orch setup check` warns while a TODO(setup) remains. Keep this file to
     standing rules only: anything that belongs to one round goes in <instance>/rounds/<tag>/RULES.md. -->

Binds every row, unblocker and reviewer of this rig. Where it is silent, the project's `AGENTS.md` applies.
The engine's mechanics (claims, close checks, review, re-dispatch, usage limits, idle nudges, reaping) are configured
in `orch.yaml`, not here; `RIG.md` (from `orch setup card`) describes them.

## 0 · Precedence

The Owner's instruction or recorded ruling, then the row PLAN, then the round's `RULES.md` (adds to this file, wins
where they differ), then this file. Rulings bind a row through its PLAN's `Rulings:` line; any other ruling is read by
id (`orch gd show <id>`), never the whole ledger.

## 1 · Paths

| name | path |
|---|---|
| project root (every executor's cwd) | TODO(setup) |
| workspace (plans, reports, DECISIONS.md) | TODO(setup) |
| code repositories / worktrees rows may change | TODO(setup) |
| read-only inputs (corpora, fixtures, ground truth) | TODO(setup) |

## 2 · Environment and secrets

- How rows load their environment: TODO(setup) (a loader script that holds no secret values).
- Secret files are sourced or passed by path only, never opened or printed: TODO(setup).
- NEVER-OPEN: the clause in `prompts/_PART_never_open.md` binds verbatim; list project-specific additions here.

## 3 · Resources

Every resource two rows could corrupt by running at once is declared in `orch.yaml guard.resources` and used only
under `orch hold <id> <res> -- <cmd>`.

| resource | covers |
|---|---|
| `git-lock` | the workspace git index (commits through `orch commit`) |
| TODO(setup) | e.g. a database, a measured tree, a rate-limited key |

## 4 · Boundaries (never, in any row or unblocker)

- TODO(setup): branches, remotes, deploys, infrastructure, other rigs' files and state, production data.

## 5 · Route A and Route B

**Route A** (stop: `orch escalate <id> "<why>"`, a decision-brief note in PROGRESS.md): anything in §4; a secret or personal-data
exposure; spend beyond a declared cap; TODO(setup) project-specific stops.

**Route B** (everything else): decide, write the decision and its reasoning in PROGRESS.md before acting, proceed. A
decision later rows must follow gets an id from `orch gd next --reserve "<title>"` and a short ledger entry.

**Unattended decisions (optional).** TODO(setup): may an unblocker adopt the recommended option of an Owner-reserved
question provisionally (revertable, logged with its revert command), or must it escalate?

## 6 · Spend and remote calls

TODO(setup): which paid APIs exist, the caps, how a row reserves and books a call. Executor CLI sessions are not
metered by the framework.

## 7 · Evidence and close

- Lean rows: code + test + a short `PROGRESS.md` that names the exact command verifying the work.
- A report pack under `<reports_dir>/<id>_<yyyymmdd>/` only when the PLAN asks or for long runs and measurements.
- Commit only named paths: `orch commit <id> -m "<msg>" -- <paths>`; no AI co-author or generated-by trailers.
- Claim with `orch done` / `orch blocked` / `orch escalate`; never write under `state/` by hand.

## 8 · Long runs

Anything that may pass five minutes starts through `orch longrun <log> -- <cmd…>` (real PID, `tail -f` + `ps -p`
handover; never a self-matching `pgrep -f`).
