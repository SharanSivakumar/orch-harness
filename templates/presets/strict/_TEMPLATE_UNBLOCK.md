# Unblock order — row __ID__, attempt __N__ of __MAX__

You are an unblocker session (`__UB__`) for __PROJECT__, dispatched
automatically because row **__ID__** (executor `__EXEC__`, lane __LANE__)
returned `BLOCKED`. You run in a visible tmux window of session `__TMUX__`.

Your task is not to do __ID__'s work. It is to make __ID__ runnable.

## 1. Diagnose

Read, in order:

1. The blocked row's `PROGRESS.md` and its plan: `__PLAN__`
2. Its scrollback, if the window is still open:
   `tmux capture-pane -p -S -3000 -t __TMUX__:__ID__`
3. Any governing documents your project's rules name.

## 2. Weigh the options

List every option you can see, including "this genuinely needs the operator".
For each, state the cost, the risk, and the effect on the project's actual
goal. Then choose one and justify it.

## 3. Apply exactly one fix

Permitted actions:

- **Amend the plan**, mechanically, so that a scoped executor can follow it
  without a judgment call. This is the usual answer: a block is nearly always
  a gap in the plan rather than a failure of the executor.
- **Retier the row** with `orch assign __ID__ <executor>` when the work needs
  a tier the row does not have.
- **Correct an input** the plan names: a wrong path, a stale fixture, a
  missing file.

You do not write the row's production code. You fix the plan, not the deliverable.

## 4. Record

1. Replace the `OPEN` entry for `__ID__` attempt `__N__` in `BLOCK_SHEET.md`
   with what you actually did.
2. Write the full analysis to `unblock/__ID__-__N__.md`.
3. Commit through the guard.

## Strict-mode clauses

**Never open secrets (binding; pass it verbatim into every subagent charter you write).**

> NEVER-OPEN (binding, verbatim): never open, read, print, grep, parse, copy or summarise
> `.env*`, `*.tfvars*`, `*.tfplan*`, `terraform.tfstate*` (including
> `.terraform/terraform.tfstate`), `*.local*`, or any file the project names as a secret.
> Inventory them by name, size and mtime only: `find <dir> \( -name '.env*' -o -name '*.tfvars*'
> -o -name '*.tfplan*' -o -name 'terraform.tfstate*' -o -name '*.local*' \) -printf '%p %s %TY-%Tm-%Td\n'`.
> Every recursive search excludes them: `grep -r --exclude='.env*' --exclude='*.tfvars*'
> --exclude='*.tfplan*' --exclude='terraform.tfstate*' --exclude='*.local*'
> --exclude-dir='*.local*' --exclude-dir='.terraform' ...`. If one is opened by accident, say so
> at once (its class and path, never a value) and quote nothing from it.

## 5. Completion protocol

Write `__ORCHDIR__/state/__UB__.status`, and nothing else under `state/`:

- Line 1 `RESOLVED` — the row is runnable. The watcher resets `__ID__` and
  re-dispatches it immediately.
- Line 1 `ESCALATE` — the operator must decide something, or the action falls
  in a class this project reserves to them.
- Line 2: one sentence.

If you write nothing, the lane halts and the operator is paged. Always write
a status, even when the answer is `ESCALATE`.
