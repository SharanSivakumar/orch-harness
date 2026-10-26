# Work order — __ID__  (manual row, lane __LANE__)

This row is yours, not an agent's. Rows that depend on `__ID__` wait until it is
claimed and accepted.

**Plan:** `__PLAN__`
**Report pack goes to:** `__REPORTS__/__ID___<date>/`

## When it is done

Write `__PLAN_DIR__/PROGRESS.md` (what was done, how to verify it), then:

    __CLOSECMD__

## If it cannot be done

    __BLOCKCMD__

## If you would rather an agent did it after all

    orch assign __ID__ <executor>

The engine picks the reassignment up on its next tick and dispatches it.
