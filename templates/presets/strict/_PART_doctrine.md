## Project doctrine (permanent; the workspace rules add the cycle's specifics)

**North Star.** TODO(project): one paragraph naming what this project produces and what a correct result means. Delete this line and write yours before the first dispatch. A measured STOP is a successful result; a green check obtained by changing definitions, hiding failures, weakening tests or special-casing an input is the worst result.

**Never:** invent a value, amount, percentage, threshold, date, file, command, flag, measurement, citation or completion claim · weaken or delete a test, edit ground truth, narrow a denominator, demote an assertion or widen a tolerance · add input-, fixture-, customer- or environment-specific code as a fix (every fix is a named general mechanism) · touch `main`, production, a remote service, a paid API, a credential, a tag or a merge outside what your plan and its gate authorise · put a name, address, account number, personal record, extracted document body or secret in evidence, prompts, logs or commit messages (counts, classes, hashes, page numbers only; bodies live in gitignored `*.local*` paths) · add AI co-author or generated-by attribution anywhere.

**Route A** (stop, escalate: `__ESCALATECMD__`): any of the above would be needed; spend beyond a declared cap; a remote write; a private-data or secret exposure (report the class, never the value); a change to a bar, scorer, ground truth or the ladder's shape; anything the workspace rules reserve to the Owner. Before `orch escalate` or an ESCALATE verdict, write `PAGE.txt` in the row folder (`__PLAN_DIR__/PAGE.txt`): ≤ 600 chars, one plain-language sentence on what happened, 2–4 lettered options each ≤ 1 line, one marked (recommended); no personal data, no sealed names, no secrets. The engine sends it as the phone page.

**Route B** (everything else): decide, write the decision and its reasoning in PROGRESS.md before acting, and proceed. A decision that later rows must follow gets a ruling id from `orch gd next --reserve "<title>"` and a short entry in DECISIONS.md. Never end a session waiting for the Owner on a Route B matter.

{{> never_open}}

**Long jobs and shared venvs.** Anything that may pass five minutes starts through `__ORCHDIR__/orch longrun <row folder>/<name>.log -- <cmd…>` (the job's own PID, `tail -f` + `ps -p` handover; never a self-matching `pgrep -f`). `__ORCHDIR__/orch venvcheck <venv>` must print CLEAR before you run from or arm a shared venv.
