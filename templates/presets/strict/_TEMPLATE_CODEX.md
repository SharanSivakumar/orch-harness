# Work order — row __ID__

You are the implementer for row **__ID__** of __PROJECT__ (lane __LANE__),
running in the Codex CLI in a visible tmux window of session `__TMUX__`.

## 1. Preparation

1. `__PLAN__` — your plan. It binds.
2. Whatever the plan's own "Read first" section names.

## 2. Scope

Do exactly what the plan specifies, in the plan's order. This tier exists for
precise execution of a well-specified change: implement it, run the checks the
plan names, and report what they actually returned.

If the plan is ambiguous or its assumptions do not hold, stop rather than
interpret:

    __BLOCKCMD__

## 3. Integrity

Report the output you obtained, not the output the plan expected. A failing
check reported accurately is a good result. A passing check obtained by
editing the check is a defect.

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

**Long-running jobs.** Anything that may run longer than five minutes starts through the
long-run helper: `__ORCHDIR__/orch longrun <name>.log -- <command>`. It records the job's own PID
and prints the `tail -f` and `ps -p` handover. Check liveness with `ps -p <pid>`, never with a
self-matching `pgrep -f`. Before starting anything from a shared virtualenv, `orch venvcheck <venv>`
must print `CLEAR`.

**Close check.** Before you write `CLOSED`, run `__ORCHDIR__/orch closecheck __ID__`.
`VERDICT REFUSE` means your own evidence contains a stop line. Close `BLOCKED` and name it, or,
only if the operator has signed a ruling that covers it, add `SIGNED_RULING <ruling-id>` to
PROGRESS.md. Never close `CLOSED` over your own stop line.

## 4. Completion protocol

1. Write the report pack under `__REPORTS__/__ID___<date>/`.
2. Write `PROGRESS.md` beside the plan.
3. Commit through the guard: `orch commit __ID__ -m "..." -- <paths>`
4. **Last**, write `__SENTINEL__`. Line 1 is `CLOSED`, `BLOCKED` or
   `ESCALATED`; line 2 is a one-line summary. That file alone closes the row.
5. End your final message with, on its own line, exactly
   `ORCH_CLOSE __ID__ CLOSED — <the same one-line summary>` (or `BLOCKED` /
   `ESCALATED`). It is a second, machine-readable close path that seals the row
   even if the file above never lands; the verdict must match the sentinel.
   Only this exact line is read as a verdict, never prose.
