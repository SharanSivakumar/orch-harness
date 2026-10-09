# Work order — row __ID__ (manual, lane __LANE__)

This row is assigned to a person, not an agent. The watcher holds lane
__LANE__ until a sentinel for `__ID__` appears.

**Plan:** `__PLAN__`
**Report pack:** `__REPORTS__/__ID___<date>/`

## To complete the row

    __CLOSECMD__

## If the row cannot be completed

    __BLOCKCMD__

## To hand the row to an agent instead

    orch assign __ID__ <executor>

The waiting watcher applies the reassignment on its next poll and dispatches
the row. No restart is needed.
