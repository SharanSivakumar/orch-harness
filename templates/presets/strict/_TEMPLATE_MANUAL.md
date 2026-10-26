# Work order — __ID__ (manual row, lane __LANE__)

This row is the Owner's. Rows that depend on `__ID__` wait until it is claimed and accepted.

**Plan:** `__PLAN__`

When it is done: write `__PLAN_DIR__/PROGRESS.md` (what was done, how to verify), then

    __CLOSECMD__

If it cannot be done: `__BLOCKCMD__` · hand it to an agent: `orch assign __ID__ <executor>`
