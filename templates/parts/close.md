{{?__SPENDCMD__ **Paid commands.** A command that costs money runs only through the spend governor: `__SPENDCMD__`. If it prints `SPEND_WAIT <id>` (exit 75), nothing ran and nothing was charged: claim BLOCKED with exactly that line — `__SPENDBLOCK__` — and stop. The engine pages the Owner once and re-dispatches this row once the Owner approves; never retry around it, never run the command another way.

}}## Mandatory final act

Your session is not finished until the row is claimed. In order:

1. Write your report pack under `__REPORTS__/__ID___<date>/` when the plan asks for one.
2. Write `PROGRESS.md` in `__PLAN_DIR__/`: what you did, what you found, what you decided and why, what you left undone, and **the exact command that verifies your work** (a reviewer will run it).
3. Commit through the guard: `orch commit __ID__ -m "…" -- <paths>`.
4. Claim the row: `__CLOSECMD__`
   It runs the close checks NOW (PROGRESS present, no stop line in your evidence, acceptance probe if the plan has one). If it prints REFUSED, fix what it names and run it again — or stop honestly with `__BLOCKCMD__`.
   If your plan asks for a verdict (a judge, a checkpoint, a falsifier, a GO / NO-GO), add `--outcome go|nogo|held|fail|infeasible` to the claim and write `CLOSED:<outcome>` in the line below. A negative outcome is a successful, measured result, and it holds the rows that need a GO. `infeasible` means "the remaining plan cannot reach the goal": the route is replanned toward the same goal, and the bar is never moved. Your PLAN's judge block says which outcome applies.
5. Then end your final message with, on its own line, exactly
   `ORCH_CLOSE __ID__ CLOSED — <the same one-line summary>` (`CLOSED:<outcome>` for a verdict row; `BLOCKED` or `ROUTE_A` to match).
   It is a backup close path; only this exact line is read as a verdict. Make no further tool calls.

A CLOSED claim is not the end: the engine re-runs the checks and, by the rig's policy, an independent reviewer on a different model re-runs your verification against your diff before any dependent row starts. Write for that reader. Never write any file under `state/` by hand.
