# orch v3 — a rig that keeps running

> Generation note: “v3” is the design generation of the autonomy layer
> (Steward, spend, phases, course loops). It ships in orch-harness 2.0 and
> later; it is not a release number by itself. Every mechanism below is off or
> neutral by default.

v2 made claims honest. v3 makes the rig **keep moving without an operator**. The cases v2 still
left for a human:

- a reboot or crash left the engine down;
- every halt waited for `orch reset`;
- course loops and new rounds waited for someone to start a planner;
- a broken CLI retiered rows to the top tier one by one;
- pages went out for decisions the rules already allowed.

The evidence comes from long-running rigs:

- sessions sat idle for long stretches while work remained;
- rounds waited for a human to start the next one;
- most answers to pages simply accepted the recommended option.

Every v3 mechanism is **off or neutral by default**, so a v2 rig behaves as before until its
`orch.yaml` opts in (`orch setup questions`: autonomy, digest, always_on).

The build is in four drops:

- **A — runs overnight**: supervisor, reboot recovery, health breaker, Steward lane, digest.
- **B — nothing hides**: panel tier, outcomes, protected paths, capability check, engine-mediated queue edits, revert.
- **C — money is the only page**: spend governor (§11); OpenCode meter, spend hold, economy, billing reconcile, spend digest (§11a).
- **D — the phase runs itself**: the phase fence (§12); the native course loop and the executor policy per round (§13); the drill suite (§14).

This file describes what is built. Each drop adds its own section when it lands.

## 1. Always on (drop A) — `lib/service.py`, `lib/engine.py`

- **systemd user units.** `orch service install` writes three units:
  - `orch-engine-<slug>.service`: `engine.py --wait-lock`, with `Restart=on-failure` and
    `KillMode=process`. Agent sessions live in tmux and are never killed by a unit restart.
  - `orch-bot-<slug>.service` — only with `watchdog.enabled` **and** `watchdog.answers` (drop B 8b):
    only one process may long-poll a bot token, a host gateway may already poll it, and pages are
    pushes that need no bot. `install` removes an earlier install's bot unit when it is not wanted.
  - `orch-deadman-<slug>.timer`.

  `loginctl enable-linger` starts them at boot; that is a one-time Owner command. A service engine
  waits on the engine lock, so it takes over cleanly from a tmux-launched engine. The tmux window
  `orch-engine` becomes a live `tail -F` of `state/engine.log`.
- **Dead-man.** Every 5 min it checks the heartbeat; a heartbeat older than 5 min restarts the unit.
  When the third check still finds the heartbeat stale (the first two restarts did not help), it
  sends one page of class `engine_down`. A deliberate stop (`state/engine.stopped`) is never undone.
- **Interrupted is not dead.** Each dispatch records the host `boot_id` and the tmux session's
  creation time. The session counts as interrupted, not dead, when:
  - the boot id changed;
  - the tmux session is newer than the dispatch;
  - or the tmux session is gone.

  The engine then re-dispatches it without charging an attempt, retier, unblock or review budget.
  An unblocker gets a fresh `unblock_epoch`, a round review is re-dispatched, and a Steward session
  is removed from its incident's session count.
- **Died vs. killed.** `lib/launch.sh` runs the CLI as a child process and writes
  `state/<sid>.exit "<rc> <epoch>"` when it exits by itself. A window killed from outside HUPs the
  launcher first and leaves no record. (tmux ≥ 3 ignores a session-scoped `remain-on-exit`, so v2
  never actually recorded exit statuses.) The CLI runs in the background and the launcher `wait`s, so
  the HUP (or TERM) is handled at once, not deferred until the CLI exits: the launcher sends SIGTERM to
  the CLI's whole tree (`tmuxio.killtree`: its descendants, helpers that lead their own session, and
  orphans found by the `ORCH_LAUNCH_TAG` in their environment; `orch longrun` jobs stay), SIGKILL after
  `ORCH_KILL_GRACE` (5) seconds, and exits 129 with no record. A plain `tmux kill-window` therefore
  leaves no process behind, as `tmuxio.kill` never did.
- **Self-update.** Every `update_check_secs` the engine compares the framework HEAD with the commit
  it runs. It re-execs (same pid, sessions untouched) when HEAD moved, `.tested/<HEAD>` exists, the
  tree is clean and no job is in flight. `control/ENGINE_RESTART` (from `orch sw restart-engine`)
  re-execs it too.
- **Bookkeeping.** With `engine.commit_bookkeeping`, the engine commits its own files through the
  guard as `plan:engine`: block sheet, unblock analyses, the Steward's files, digests and reviews.
- **Disk admission.** `scheduler.min_free_disk_mb` stops dispatch when free disk falls below it,
  beside the existing RAM floor.
- **A phase that keeps failing.** After 5 consecutive ticks with an exception in the same engine
  phase, a Steward incident (`engine_error`) is opened.

## 2. Executor health (drop A) — `lib/health.py`

- **Fast death.** A session that dies by itself, with no claim, within `fast_death_secs` of
  dispatch is an infra suspect. The row is re-dispatched on the same executor and is not charged.
  A second fast death of the same row while the breaker stays closed is the row's own fault: the
  normal dead-window policy applies.
- **Opening.** `threshold` distinct sessions of one executor dying fast within `window_secs` open
  its breaker. While open:
  - nothing dispatches to that executor (`capacity_ok`);
  - its `remedies` run, at most 3 times per opening;
  - with a `probe`, it re-runs every `probe_every` seconds and exit 0 closes the breaker;
  - without a probe, the breaker goes half-open after `open_secs` (doubling per re-open, capped at
    1 h). One trial dispatch then either survives `fast_death_secs` (the breaker closes) or dies
    fast (it re-opens).
- **Startup self-test.** Probes also run once at engine start, so a CLI flag change is caught
  before any row dies of it.
- **Login prompts.** `auth_patterns`, matched on the last lines of the pane, open the breaker at
  once with kind `auth`. The Steward records an inbox item, since a login is the Owner's, and the
  row returns to the queue unchanged.
- **Long outages.** A breaker open longer than `steward_after` opens a Steward incident.

## 3. The Steward lane (drop A) — `lib/steward.py`

**Incidents.** The engine's scan opens one incident (`state/steward/incidents/<n>.json`) for each
case below. A signature (`state/steward/handled/`) stops a closed case from re-opening until its
state really changes.

| incident | when | class |
|---|---|---|
| `halt` | a row in blocked, route_a or halted, whatever put it there | judgment for ROUTE_A or an escalation, otherwise mechanical |
| `gate` | ready but for its gate for `gate_after` seconds | mechanical |
| `manual` | a manual row waiting for `gate_after` seconds | judgment |
| `undispatchable` | a bad executor or an unrenderable work order | mechanical |
| `rest` | nothing running, nothing READY, no STOP or PAUSE, unfinished unparked rows in enabled lanes, for `rest_after` seconds | judgment |
| `round_review` | `rr-<tag>` DONE with QUESTIONS.md and no ANSWERS.md | judgment |
| `planner` | a planner session failed or died | judgment |
| `breaker` | from lib/health.py | mechanical, or inbox for auth |
| `engine_error` | an engine phase failed 5 ticks in a row | mechanical |
| `negative_outcome` | a row accepted with outcome nogo, held or fail (§6) | judgment |
| `spend_caps` | SPEND_CAPS.yaml (or the approved copy) edited without `orch spend caps approve` (§11) | judgment (INBOX: caps are the Owner's) |

**Sessions.** A session `sw-<n>-<k>` runs on `steward.tiers.<class>`. A mechanical incident
escalates to the judgment tier. Past the last tier, or past `max_sessions_per_incident`, the engine
parks the subject row and writes an inbox item. **Nothing pages.**

Steward sessions take capacity like any other. When a Steward is waiting for an executor slot, rows
are not dispatched to that executor (`steward_wants`), so the Steward goes first. Limits:

- `steward.max_parallel` concurrent Steward sessions;
- `steward.sessions_per_day` Steward sessions in a day.

**Acting.** A Steward acts only through `orch sw <action> … "why"`:

1. The CLI writes `state/steward/requests/<sid>.<ms>.json` and waits for the engine's answer.
2. The engine checks the requester is a live session it dispatched, then checks the action set, the
   per-session action cap, the per-row daily reset cap, gate kinds and the phase fence.
3. It runs the operator's own code path.
4. It appends a line with the **revert** to `steward/STEWARD_LOG.md` (and `state/steward/log.jsonl`).

| action | what | refused when |
|---|---|---|
| `reset`, `retry <row> <exec>`, `assign` | operator reset / assign | the row is running, or reset ≥ `max_resets_per_row_day` today, or the Owner parked it (vD3) |
| `approve <row>` | release a gate | the gate's kind is not `steward` (queue extra `gate_kind=`; default `steward.default_gate_kind: owner`), or the Owner parked the row (vD3) |
| `park` / `unpark` | `phase=parked`: never dispatched, deps wait; unpark = reset | the row was parked by the Owner (only the Owner unparks it) |
| `start`, `pause`, `resume`, `disable` | lane controls | the phase fence (drop D) |
| `round-new`, `round-add-lanes`, `round-start`, `round-close`, `round-review` | `orch round …` as the operator (`ORCH_ACTOR` = the session) | the phase fence |
| `plan <tag> "brief"` | dispatch a planner `pl-<tag>-<k>` (`steward.planner`) | — |
| `rule S "<title>" "<text>" "<revert>"` | a provisional Steward ruling `SW-<k>` (`steward/RULINGS.md`, `state/steward/rulings.jsonl`) | class W or $; class P from any session but the panel (§5) |
| `escalate-panel` | hand the incident to the panel tier after this session's ESCALATE (§5) | no panel on the rig (then P is a wall) |
| `inbox "<title>" "<decision brief>" [row]` | an Owner inbox item (`steward/INBOX.md`) | — |
| `page <class> …` | a critical push | the class is not in `steward.page_classes`, or the same page was already sent |
| `remedy <exec>`, `restart-engine`, `note` | health remedies and probe; engine re-exec; a digest line | — |

**Verdicts.** `orch verdict sw-<n>-<k> RESOLVED|PARKED|INBOX|ESCALATE "…"` needs the analysis
`steward/incidents/<n>-<k>.md` first. A RESOLVED while the row is still halted with the same claim
reads as ESCALATE.

**Planners.** A planner session `pl-<tag>-<k>` is limited to:

- `round-new`;
- `round-add-lanes`, `round-start`, and `reset`/`approve` on its own round's rows;
- `orch commit plan:<tag>`.

It closes with PASS or FAIL. A FAIL, or a planner that dies, opens a `planner` incident.

**Paging.** With the Steward lane on, `Rig.notify(critical)` reaches the phone only with a
`page_class` in `steward.page_classes`. Every other critical event becomes a normal event and a
digest line. Halts are handled, not paged.

**Owner surfaces.**

- `orch steward status|incidents|log|inbox [done <k>]`;
- `orch steward close <n> "why"`: the Owner closes an incident by hand (see "Operator close" below);
- `orch park|unpark <row>`;
- `orch approve <row>`, which also releases a parked gate.

**Operator close.** `orch steward close <n> "why"` closes incident `n` through the same `close()` path as the
engine, with outcome `owner-closed`: evented, engine-logged, and, when the incident has a `sig`, marked handled, so
the next `scan` tick does not reopen the same signature (a closed halt incident is not reopened while the row's
halt stays the same). Refused inside any rig session (the `inbox done` capability rule), for an unknown or already
closed incident, for an empty reason, and while the incident's latest session is live. An incident in `working`
whose sessions are all dead may be closed.

**Duties.** `steward.duties` (none by default) are periodic commands whose output can open incidents of kind `duty`.
While the lane is enabled, `steward.tick` starts each due duty as an async engine job (`start_job`, like
`watchdog.checks`), cwd = the project root, so a slow duty never blocks a tick; at most one run per duty. The last
start is persisted in `state/steward/duties.json`, so a restart waits out `every`. stdout and stderr go to
`state/steward/duty_<name>.out` (overwritten per run). Each `INCIDENT <subject> <summary…>` line opens a `duty`
incident (class = the duty's `klass`; `sig` = sha1 of name, subject and summary; skipped when one is open for the
subject or the signature was handled). A non-zero exit or timeout with no INCIDENT line opens `duty:<name>` with
`duty <name> failed: exit <rc>: <first line>`. The bundle names the duty, its line and the output file.
`orch lint` checks the entries; `orch steward status` lists last start, last exit and next due.

The rig's decision rights live in `steward/RIGHTS.md` (`steward.rights_file`). The framework ships a
generic default in which everything non-mechanical is a wall.

## 4. Digest (drop A) — `lib/digest.py`

`digest.times` sends one digest per slot per day: an ordinary Telegram message, never a page, plus
`digest/<date>_<HHMM>.md`. `orch digest` renders one by hand. It reports, measured from state:

- rows accepted, now, and parked (with the reason);
- Steward incidents, acts with their reverts, and provisional rulings;
- the open inbox;
- executor health;
- hours idle while work was left;
- **human touches**: operator resets, assigns, approves, manual dispatches, round starts and
  parks/unparks with no Steward or re-arm author. The target is 0.

## 5. The panel tier — class P (drop B) — `lib/panel.py`

**Class P: new constructs, frozen schema or vocabulary changes and ADRs.** They are
decided provisionally by two sessions, never one; work builds on them; the digest flags them HIGH;
each carries its revert. The generic `templates/steward/RIGHTS.md` carries that row; a rig's own
rights table may name more P items.

**How an incident reaches the panel.** A Steward session runs `orch sw escalate-panel "why"` and
closes ESCALATE (the incident's class becomes `panel`; its tier ladder is skipped), or an incident
is opened with class `panel`.

**Decider** `sw-<n>-d<k>` on `steward.panel.decider` (default opus). Every `orch sw` action it takes
is **staged, not executed**: the engine appends it to `state/steward/staged/<n>.json` (rendered for
reading as `steward/incidents/<n>-staged.md`) and answers `STAGED #<i>`. Only `note` and `page` run
at once: they move no row, gate or lane, and a page is already limited to the Owner's classes. The
decider writes its proposal to `steward/incidents/<n>-panel.md` (its verdict needs it) and closes. A staged
action is checked when staged too (shape, row, lane, rule class, gate kind: `steward.precheck`), so a
CONFIRM rarely meets a refusal. The engine closes the decider's window when its verdict is read and
fingerprints the staged set (`staged_sha`); a CONFIRM executes only that set:

| decider verdict | what happens |
|---|---|
| RESOLVED | the proposal is ready: the falsifier runs |
| PARKED / INBOX | it chose the safe option itself: the staged actions are discarded, the normal park / inbox path runs |
| ESCALATE | the panel cannot decide: park + inbox, nothing executed |

**Falsifier** `sw-<n>-f<k>` on `steward.panel.falsifier` (default sonnet; it must differ from the
executor the decider ran on). It reads the proposal, the staged list and the bundle, tries to show
the proposal is wrong, writes `steward/incidents/<n>-f<k>.md` (it may not act: every `orch sw` request but
`note` is refused) and closes
`orch verdict sw-<n>-f<k> CONFIRM|OBJECT "…"`.

- **CONFIRM:** the engine executes the staged actions in order, through the same checks as a live
  request, as the decider. Each is logged in `STEWARD_LOG.md` with its revert and
  `[panel: CONFIRMed by sw-<n>-f<k>]`. A staged `rule P` becomes `SW-<k>` class P, with the decider
  and the falsifier on its record. The first refused action stops the rest: park + inbox naming what
  ran (with reverts) and what was refused. Each outcome is written before the next action runs, so an
  engine restart mid-CONFIRM never runs an action twice. A halt still halted with the same claim afterwards is
  parked + inboxed, as for a Steward RESOLVED.
- **OBJECT** — and any other falsifier verdict (an idle or launch-failure verdict too): the safe
  option. The subject row parks, one inbox item carries the proposal and the objection, nothing
  executes.

**Limits.** `rule P` from any other session is refused with the way to the panel. Panel sessions do
not count against `max_sessions_per_incident`; they have `steward.panel.max_sessions` (default 4).
Past it, or without a panel (`steward.panel.enabled: false`, an executor not allowed, decider =
falsifier — `steward.panel_ready()` is false), the incident is parked + inboxed: P is a wall.

**Digest.** A section **HIGH — panel (class P) decisions** comes before the inbox: every class P
ruling with its decider, falsifier and revert, and every panel outcome (`panel-confirmed`,
`panel-objected`, `panel-partial`).

Mock scripting for drills: `control/MOCKPD_<subject>` / `MOCKPDV_<subject>` (decider actions /
verdict), `MOCKPFV_<subject>` (falsifier verdict, default CONFIRM).

## 6. Outcomes and `needs=go` (drop B) — `lib/store.py`, `lib/rows.py`, `lib/engine.py`

The evidence: NO-GO and HELD rows closed as plain CLOSED, and their dependents ran anyway. A
claim says the work was done honestly; v3 lets a verdict row also say **what the answer was**, in a
form the scheduler reads.

- **Claim.** `orch done <id> "…" --outcome go|nogo|held|fail` (v3.1: also `infeasible`, §15.3) writes line 3 `OUTCOME <x>` in
  `state/<id>.status` (lines 1–2 and every parser unchanged). The seal accepts
  `ORCH_CLOSE <id> CLOSED:<x> — …` (one rule, `engine.close_pattern`, mirrored by the adapter's
  `lib/closing.sh`; drill test_21 pins both). Input is normalised (`NO-GO` → `nogo`). `--outcome`
  refuses any other word; a seal `CLOSED:<token>` or a hand-written `OUTCOME <token>` keeps an
  unknown token, which counts as negative. `CLOSED: <x>` with a space takes only the outcome words, so
  prose after a colon is never an outcome. An outcome rides on CLOSED only.
- **Claim vs. closing line.** A session may run `orch done` without `--outcome` and then print
  `CLOSED:nogo`. For a CLOSED claim with no outcome (or `go`) from a row that a `needs=go` dependent
  names, while its window is open, the engine keeps the session open until the pane is idle 20 s (at most
  180 s), reads the closing line, and a negative outcome there wins (line 3, event
  `outcome_from_closing_line`). No other row's timing changes; a closing line never upgrades a
  claim to `go`.
- **Result.** When the engine judges a CLOSED claim it copies the outcome into the result
  (`outcome`, '' for none); `Store.outcome(rid)` reads it (falling back to line 3 for a
  grandfathered close). The row is **accepted** either way: a NO-GO is a measured result, not a
  failure, and it still passes the close checks and the review.
- **`needs=go`.** A queue extra. `rows.unmet_deps()` — used by the scheduler, `orch dispatch` and
  `orch status` — treats a dep the row *names* (its deps column; not a serial lane's implicit deps)
  as unmet while it is accepted with a negative outcome, shown as `<dep>:<outcome>`. Accepted with
  `go` or with no outcome = met. `orch dispatch` keeps the operator's `FORCE=1` override. A
  `needs=` value other than `go` is a queue-line problem (the row does not dispatch); `needs=go` with
  an empty deps column is a lint warning (it holds on nothing).
- **Fail-safe.** An outcome token outside the four (a hand-written claim) is kept and counts as
  negative: an unknown answer never lets a GO-only dependent run.
- **Incident.** At accept the engine logs `negative_outcome` (attempts.jsonl, lane log, a normal
  notification). With the Steward lane on, its scan opens a `negative_outcome` incident (class
  judgment; signature = outcome + claim time + attempt, so it opens once per claim). The bundle names
  the held dependents and says that a negative outcome is never re-run to obtain a GO; the Steward
  parks the held rows, plans a correction, or INBOXes the Owner. The judge is accepted, so a
  PARKED / INBOX / unresolved incident never parks it. `steward.negative_outcome: off` disables the
  incident. Drop D turns this incident into the native course loop.
- **Neutral for v2.** A queue without `needs=go` and claims without `--outcome` behave exactly as
  before. The generic close part tells verdict rows to use `--outcome`; `docs/PLANNING_AGENT_GUIDE.md`
  §1 tells planners to cut judges with it and GO-only dependents with `needs=go`.

Mock scripting: line 3 of `control/MOCK_<sid>` is the outcome (written as `OUTCOME`, sealed as
`CLOSED:<x>` with `MOCK_SEAL_`, passed as `--outcome` with `MOCK_VIA_CLI_`).

## 7. Protected paths and ledger signatures (drop B) — `lib/protected.py`, `lib/close_check.py`

What only the Owner may change — scorers, gates, keys, caps, phases, the rights table — no row,
unblocker, reviewer, Steward or planner can change: the guard refuses to stage it and the close
pipeline refuses a claim that changed it, unless an Owner ruling the operator committed covers it.

- **The set.** `guard.protected_paths`: globs relative to the guard repo (`guard.git_repo`). `*` and
  `**` cross `/` (over-matching is the safe direction; `**/x` also matches a top-level `x`); a
  pattern ending in `/`, or naming a directory, protects its whole tree; a single string is one
  pattern. Plus the **automatic set** (`guard.protected_auto`, default on), relative to the
  instance: `SPEND_CAPS.yaml` (drop C), `phases.yaml` (drop D) and the rights table
  (`steward.rights_file`). A path outside the guard repo cannot be committed through it and is not
  listed. `orch guard protected` prints the set.
- **The guard.** `orch commit` checks the final index (`git diff --cached --no-renames`, so a
  `git mv` out of a protected tree shows its source): a protected path is staged only for a `plan:`
  commit from the operator's shell (no `ORCH_ROW` / `ORCH_KIND`) and never for `plan:engine` (the
  engine's bookkeeping, which also leaves protected paths out of its own commits and says so in
  `engine.log`). Even the operator's commit carries only protected paths it **named** (a protected
  file someone else left staged is refused, not swept in). Anything else resets the index and
  refuses. **Every** operator `plan:` commit is recorded in `state/protected_commits.jsonl` (sha, id,
  protected paths; `protected.py record` refuses inside a session): the close check never charges
  it to a row, and only a ruling ADDED by such a commit can sign a protected change. Staged paths
  reach `protected.py match` on stdin (no argument limit).
- **The row's base** (`state/<id>.protected_base.json`, a copy in the dispatch snapshot): the
  dispatch head, the protected paths already dirty with their sha256, the protected set and the
  guard repo path — made
  at the row's first dispatch and **kept across attempts and resets until the row is accepted**, so a
  change one attempt leaves behind (committed or not) is still charged to the next, and a row cannot
  switch `close.protected` off, narrow the set or point `guard.git_repo` elsewhere during its own
  run (the check runs if it is on now or was on at the base, over the union of both sets, in the
  base's repo; a base whose repo is gone fails). A repo with no commit yet at the base is recorded
  as `unborn` (every later commit is since the base); a head that could not be read is not kept.
- **The close check `protected`** (`close.protected`, default on; in `lib/closepipe.py` beside
  `stop_lines`). It fails a claim when, against the base:
  - a commit in `<base head>..HEAD` (`git log -m --no-renames`: merges and renames cannot hide one)
    touches a protected path, operator commits excepted, and that path still differs from the base
    (`git diff <base head> HEAD`) — a change reverted again is no change;
  - HEAD no longer descends from the base head (a reset or branch switch past an Owner commit):
    `git diff <base head> HEAD` over the protected set;
  - a protected path has an uncommitted change (staged, unstaged, untracked or deleted) that was not
    already there byte for byte at the base — the Owner's own pending edit is not charged — or a
    pending edit of the base was undone outside an operator commit; git runs with `core.fsmonitor`
    and `core.ignoreStat` off, and a protected path flagged assume-unchanged or skip-worktree
    (`git ls-files -v`) is charged as hidden;
  - the base head is unknown or no longer in the history (nothing can be cleared; after an Owner's
    history rewrite the operator removes `state/<id>.protected_base.json`).
- **The Owner's door** (`protected.owner_ruling`). PROGRESS names `SIGNED_RULING <id>` (never `SW-`)
  and:
  - **added by the operator:** the entry's own line (the `### <id>` heading or the `| <id> |` row)
    is among the lines an operator `plan:` commit added to `DECISIONS.md` (the newest 200 such
    commits reachable from HEAD). Text a session wrote — uncommitted, committed by itself, or
    already in the file when the operator later commits something else — signs nothing;
  - **signed and active as the operator last committed it:** in `DECISIONS.md` at the newest
    operator commit, RULED/RATIFIED by its heading or status line (headings) or its leading cell
    (tables), first status word RULED/RATIFIED (`decisions.STATUS`, which skips «not RULED»), not
    superseded or withdrawn (`decisions.resolve`, text only) — an Owner withdrawal wins;
  - **scoped:** its text names the row id, or every changed protected path (full path or file name).
  The refusal names what is missing per id.
- **Ledger forms for stop lines** (`close_check.signed_ruling`, v2's check). A `### <id>` heading
  entry signs as before (the working tree, no scope). New: a **table-row entry**
  (`| DEC-014 | RULED (the Owner, …) — … |`), read through `decisions.load_entries`, signs when the
  cell OPENS with RULED/RATIFIED (its first token, emphasis aside), that is the entry's status, and
  it is not superseded or withdrawn (so «DEC-14 RATIFIED by …», another id's status, does not sign). ADOPTED, PROPOSED, PROVISIONAL, REJECTED, OPEN do
  not sign. On the PV2 ledger: 108 RULED rows sign, none of its 33 ADOPTED or 7 PROVISIONAL rows;
  before, a table ledger signed nothing.
- **Steward rulings for stop lines.** `SIGNED_RULING SW-<k>` signs a **stop line** only when the
  engine's `state/steward/rulings.jsonl` holds `SW-<k>` with class S or P **and** it is about the row
  (its title/text names the row, or its incident's subject is the row). Never a protected path; an
  `SW-` id is never looked up in `DECISIONS.md`. The Steward template says how to use it.
- **Neutral for v2.** No `protected_paths` and none of the automatic files changed: every claim
  passes the new check (its line reads `none touched`), the guard stages as before (it now also
  lists rename sources, which only refuses a rename out of another row's path), heading ledgers
  sign exactly what they signed before, and v2 ledgers with no `SW-` ids are unaffected.

Known limits: an operator `plan:` commit that stages the whole `DECISIONS.md` (planning ids stage
shared paths whole) adopts whatever a session left uncommitted in it — the operator reviews that diff;
the check runs at the claim, so a change a session makes between a passing claim and acceptance is
charged only to rows whose base predates it; the guard's session identity comes from process ancestry since drop B item 4 (§8): a session that
unsets `ORCH_ROW`/`ORCH_KIND` no longer commits as the operator; the state files (`protected_commits.jsonl`, the base)
are plain files a session running as the same user could edit; the check reads the guard repo only
(a row that commits to another repo is outside it); a gitignored protected file is not seen; a
non-row session (Steward, planner, unblocker) that edits a protected file outside the guard is
charged only to rows whose base predates the edit, i.e. it stops their claims rather than its own.
The same holds between rows. A commit that is not a recorded operator `plan:` commit — made around the guard by
a session, or a raw `git commit` by the Owner — carries no row identity, so while its net change to a protected
path stands it is charged to **every** row not yet accepted whose base predates it: the row that made it and any
independent row that was running, halted, parked or waiting for a retry at the time (fail closed: an
unattributable change to the Owner's files stops every claim it could belong to). Rows first dispatched after it
are not charged; reverting it clears every row. Drill `test_44` (drill 5) starts its independent row after the
bypass for that reason.
An Owner who edits a protected file mid-run should commit it at once with `orch commit plan:<tag>`:
an uncommitted Owner edit made after a row's base refuses that row's claim.

## 8. Capability by process ancestry (drop B) — `lib/capability.py`

v2 refused operator power only while `ORCH_ROW` was set, and a session can unset a variable. The
engine starts every session as the first process of a tmux pane and records that pane's pid, so the
truth is in the process tree.

- **Who the caller is.** `capability.inside(cfg)` walks `/proc/<pid>/status` PPid from the caller up
  to pid 1 and compares each pid with the pane pids of the instance's session records — every
  `state/<sid>.dispatch.json` (`ident` = "`<wid> <pane_pid>`") and `state/<sid>.window`, **open or
  closed** (a window kept by the reaper's grace is still the session; a session that flips its own
  record to closed does not leave it), except an **adopted** record once closed (`orch adopt`
  usually adopts a window the operator made: it is the operator's again when the row ends). The
  nearest pane wins; of two records for one pid, the newest. `identity(cfg)` returns that session's
  `(sid, kind)`; outside every pane, `ORCH_ROW`/`ORCH_KIND` as in v2 (the v2 tests that run `orch`
  with `ORCH_ROW` set from the test shell keep their meaning). An `ORCH_ROW` naming another session
  is ignored inside a pane: a session claims and commits only as itself.
- **The user's pane registry.** `ORCH_INSTANCE` is an environment variable too: a session could
  point it at a decoy instance (an empty `state/`, the real `control/`/`leases/`/queues symlinked, or
  the real `state/` with `capability_check: false` in the decoy's `orch.yaml`). So the engine also
  writes `~/.local/state/orch/panes/<pid>.json` at every dispatch and adopt (home from the password
  database, not `$HOME`; dead pids pruned on write) with the rig's real paths. A registered pane
  counts for any instance that shares one of them (instance, state, control, leases, git repo), and
  the **owning** rig's switch decides. A rig that shares nothing — the throw-away sandboxes of
  `tests/run_all.sh`, run from inside every build row — is the caller's own to operate.
- **A reused pid is a stranger.** A pane pid counts only while the process holding it started no
  later than its record (+5 s for integer epochs): after the pane ended, or after a reboot, the pid
  belongs to a later process and is ignored, so the operator's shell is never taken for a session.
  A record without an epoch does not count. The boot id is not used (`ORCH_BOOT_ID` is an
  environment override a session could set).
- **Leaving the tree.** A double fork or `setsid` re-parents a process (to pid 1 or the user's
  subreaper). With `guard.capability_cgroup` (on), a live pane that sits in a cgroup of its own
  (tmux ≥ 3.2 on systemd: `tmux-spawn-*.scope`, distinct from its parent's) claims every process in
  that cgroup. Without per-pane cgroups the signal is silent — it never refuses on its own.
- **Fails loud, not open.** Records are parsed one by one (a malformed file is skipped, never a
  reason to drop the check); an unexpected failure of the check prints a warning (`capability.py
  who` exits 3, the guard says so) before the environment decides.
- **What refuses.** `orchctl` `OPERATOR_ONLY` (start, night, day, pause, resume, stop, unstop,
  disable, approve, dispatch, reset, adopt, engine, migrate, service, revert) through `caller()`;
  `orch park|unpark` and `orch inbox done` (`steward.ops`); `orch service` except `deadman`; `orch
  setup apply|defaults` (they write `orch.yaml`, the off switch included); `orch preflight
  --review`; `orch sw` uses the pane's id; `orch round …` and `orch rearm` use the pane's id and
  kind; the guard (`session_identity` in `parallel_guard_core.sh`) replaces `ORCH_ROW`/`ORCH_KIND`
  with the pane's before any check, so the operator `plan:` flag, the planner rule (`pl-<tag>-<n>`
  commits `plan:<tag>`), the self-commit rule and `protected.py record` all read the truth;
  `ORCH_SCANNER_OVERRIDE=1`, `ORCH_SHARED_WHOLE=1` and `orch guard force-release` refuse inside any
  session (by environment or ancestry). `orch revert` (item 6) is not routed by `bin/orch` yet; it
  is in `OPERATOR_ONLY`, and wherever item 6 builds it, it calls `capability.session(cfg)`. `orch row
  add` (§9) reads the caller from `capability.session(cfg)`: no session = the operator.
- **Unaffected.** The engine (a systemd unit or the `orch-engine` window — not a session pane) and
  its subprocesses, the bot window, the dead-man timer, the operator's shell, `orch dispatch` by the
  operator. `tests/run_all.sh` run from inside a dispatched session operates sandbox rigs that share
  none of that session's rig's files.
- **Config.** `guard.capability_check: true` (false = v2: the environment alone decides and the two
  overrides are allowed in sessions again), `guard.capability_cgroup: true`. **On by default
  (Route B, vB4):** for a v2 rig it changes nothing an operator or the engine does; what it changes is
  exactly what the spec asks — a session that unset or borrowed `ORCH_ROW` is refused, and sessions
  may no longer use `ORCH_SCANNER_OVERRIDE` / `ORCH_SHARED_WHOLE` (v2 allowed that).
- **Drills.** `test_25` (a mock session's pane runs `orch reset`, park/unpark, inbox done, service,
  a borrowed-id claim, a `plan:` commit, both overrides, force-release, `setup apply`, `preflight
  --review` and two decoy instances with `ORCH_ROW` unset — all refuse; a double-forked and a
  `setsid` child refuse where panes have their own cgroup; the operator's `orch reset` then works);
  `test_26` (closed records count, a reused pid and an epoch-less record do not, malformed
  neighbours are skipped, adoption ends with its row, the newest record of a pid wins, a shared
  cgroup is silent, the registry catches a decoy and leaves a sandbox alone, the owning rig's switch
  decides, the switch restores v2).

Known limits: a session of a rig that shares none of this instance's files is this instance's
operator (by design — the sandboxes); a decoy that shares only the queue files is not caught; a
process started through the tmux server or systemd (`tmux new-window|split-window|respawn-pane|
run-shell`, `tmux send-keys` into the operator's or the engine's window, `systemd-run --user`, a
write to `cgroup.procs`, at/cron) is outside the pane's tree and cgroup; a same-uid session can
still edit the real `orch.yaml` (`capability_check: false`) or delete its records and registry
entries — put `orch.yaml` in `guard.protected_paths` to have the close check charge such an edit;
on hosts without `/proc` the check is silent (v2 behaviour). Not covered (not operator-only in the
spec): `orch gd set-status`, `orch bot start|stop`, `orch notify`, `orch digest --send`, `orch init`
of a new instance. The review and the close checks remain the guard against deliberate escape.

## 9. Engine-mediated queue edits (drop B) — `lib/rowadd.py`

v2 planners appended rows by editing `queue_<L>.tsv`, so nothing stopped a planner from dropping a
gate, granting a row more re-arm power than it had, or cutting rows past a phase.

- **`orch row add <lane> "<queue line>" "why"`.** One line per call. The **operator** (no session,
  `capability.session(cfg)` — GDV3-2) is validated and appended in its own process. A **planner**
  `pl-<tag>-<k>` (lanes of its dispatch round) and a **course-loop row** (its dispatch snapshot carries
  `rearm=…,round`; its own lane and the lanes of rounds it created — `round.yaml` `by`) write
  `state/rowadd/requests/<sid>.<ms>.json`; the engine's tick phase `row_requests` re-checks and
  appends, and answers in `state/rowadd/done/` (the CLI waits, 120 s). Every other session is refused.
- **The request proves its writer.** A request file is plain JSON any same-uid process could write in
  another session's name. It carries the CLI's pid and start time; the engine answers it only while that
  process still runs, is an `orch row add` of that very line (`/proc/<pid>/cmdline`), and descends from
  the named session's pane (`capability.inside(cfg, pid)`). With `guard.capability_check: false` (or no
  `/proc`) the v2 rule applies: the name decides. One request that cannot be handled (not a JSON object,
  a directory) is answered REFUSED and moved out; it never stops the others.
- **Checks** (`rowadd.validate`, the CLI's quick answer and the engine's binding one), under
  `state/queues.lock` (also taken by `orch archive`/`round close` around the move):
  - the one parser (`rows.parse_line`) finds no problem; the lane is a lane name, not archived, and
    listed when `lanes.auto_discover` is off;
  - the id is **new**: no row in an active or archived lane, and **no history in `state/`** (a result,
    a claim, a dispatch record, events — an old accepted result would make a new gated row done at
    once); no session prefix (`ub- rv- pf- rr- sw- pl-`, which `kind_of` would misread). A line never
    replaces a row, so a gate is never removed and its `gate_kind` never changed — re-declaring a gated
    row is refused *by name* as a gate removal/change;
  - deps exist, the executor and `reviewer=` are allowed, the plan file exists inside the workspace
    (no absolute path, no `..`) and its folder is neither the instance, inside it, nor above it (it
    would become the row's allowlist);
  - `gate_kind` is steward|owner|spend and only on a gated row;
  - from a **session**: no **twin** (a plan or plan folder an existing row already has — a twin would
    route around that row's gate); no `gate_kind=steward` (a gate its own lane could pass); no
    `review=0`; a course-loop row's extra allowlist paths lie inside its own allowlist;
  - **`rearm=` never wider than the caller's own**: a course-loop row grants a subset of its snapshot
    grants and never itself; a planner grants rows of its round's lanes (what `orch sw reset` lets it
    reset) and never `round` (any tag; a planner starts only its own); nobody grants a row itself;
  - drop D's **phase fence and row cap**: `phases.fence(cfg, [lane])` and, when defined,
    `phases.row_cap(cfg, lane, row)` (each '' or why). No `lib/phases.py` → both pass; a phases.py
    that raises → refused (unlike `steward.phase_fence`, a broken fence is not an open one).
  Each add is an event `row_add` (`by`, `why`) and a lane-log line `ROW ADD`; a refusal is
  `row_add_refused`. An operator add is planning, not a "human touch".
- **Lanes belong to rounds.** A session (`orch round new|add-lanes` from a pane, or the engine acting
  for a planner/Steward with `ORCH_ACTOR`) may not list a lane another open round already lists, nor a
  lane whose rows already ran (`rounds.lane_claims`) — otherwise a planner could pull a live lane into
  its round and gain reset, approve and `row add` there. The operator may.
- **The queue watch** (`guard.queue_watch`, default **false**; `orch setup` turns it on with the
  Steward lane). The engine keeps `state/queues.sha` (lane → sha256 of the queue's row lines, comments
  and whitespace ignored) and the blessed copy `state/queues/<lane>.tsv`; the first tick takes the
  baseline from the queues as they are, and switching the watch off drops the record (a fresh baseline
  when it is on again). Tick phase `queue_watch` keys each difference by **what the edit is** (the
  added, removed and changed lines). A **gate removed or changed**, a **committed** edit (the file equals
  `HEAD` and the commit is no operator `plan:` commit — given 30 s for the guard to record one) and a
  removed lane are reported at once; only an uncommitted edit that
  touches no gate waits `guard.queue_watch_grace` seconds (600) from the moment the lane first differed
  (the clock never restarts while it keeps differing) — the operator's edit on its way to a `plan:`
  commit. A report is: event `queue_edited` (added / removed / changed ids, gate removals and kind changes
  named), the lane and engine logs, and the digest section **"Queue edited outside orch row add"** (the
  window's events plus what still differs). A later `row add` to the lane leaves the edit's key as it
  was (not re-reported, not blessed); a lane that matches the record again forgets its report, so the
  same edit made again is reported again (once the watch saw the restored queue). Blessed by:
  - `orch row add` — only its own line is added to the blessed copy;
  - the **operator's shell**: within the grace, the lane's file equals `HEAD` and the last commit that
    touched it is an operator `plan:` commit (`state/protected_commits.jsonl`, §7); a session's commit
    never blesses;
  - `orch row sync ["why"]` (operator only; event `queue_sync`).
  A lane archived with its blessed content is dropped silently; a lane file that disappears otherwise
  is reported as removed. `orch row diff` prints the differences (exit 1 when there are some).
- **Templates.** `_TEMPLATE_PLANNER.md` and `docs/PLANNING_AGENT_GUIDE.md` (§1, §5, §10) tell planner
  sessions and course-loop rows to add rows with `orch row add` and never edit a queue; the course-loop
  planning prompt (an operator-run session) adds new rows with it and commits any cell edit through the
  operator's `plan:` commit within the grace.
- **Drills.** `test_27` (a course-loop row's pane adds a valid line through the engine and is refused a
  gate removal, a gate-kind change, a wider `rearm=` and a hand-written request; a plain row is refused,
  and so is its request filed in the course-loop row's name; the watch reports a hand edit — gate
  removal named — in the digest, reports it again when repeated, ignores comments, is blessed by an
  operator `plan:` commit, does not re-report after a later `row add`, and `orch row sync` clears it);
  `test_28` (unit: who may add, the planner's and the row's scope and grants, twins, steward gates,
  extras, ids with history, round lane claims, the request proof, the operator path, the drop-D hook
  passing without `phases.py` and refusing when it raises, archived lanes, malformed requests).

Known limits: the watch detects, it does not stop — an edited row still dispatches (the digest and the
review are the guard); a same-uid session can rewrite `state/queues.sha`, the blessed copy and
`queues.reported.json`, or type into another pane through the tmux server; an edit undone within one
tick is never seen, and an uncommitted edit that touches no gate and is undone within the grace is not
reported; the operator's uncommitted hand edit is reported after the grace (a gate edit at once) until
committed or synced; an existing row cannot be re-cut through `row add` (the Owner, or an operator `plan:` commit,
edits it); `orch sw` and `orch rearm` requests still prove their writer by name only (drop A/v2 — the same
proof would apply there).

## 10. Revert, `orch why`, digest (drop B) — `lib/revert.py`, `lib/why.py`, `lib/digest.py`

**Revert.** Every Steward act is a line of `steward/STEWARD_LOG.md` (`state/steward/log.jsonl`) with
its revert. `orch revert <k>` (the operator's: `capability.session(cfg)`, GDV3-2) undoes line k:

- It runs only a revert that is **one orch command of a closed set** — the forms the Steward's own acts
  write: `orch assign <row> <executor|default>`, `orch unpark <row>`, `orch park <row> ["why"]`,
  `orch start|disable|pause|resume <lane>…`, `orch inbox done <k>`, and `rm <path>` when the path is
  this rig's `control/GO_<row>` for a known row (a trailing two-space `(comment)` is dropped).
- It must **undo the act it is logged with**: the inverse action (assign/retry → assign, park ↔
  unpark, start ↔ disable, pause ↔ resume, approve → `rm GO_`, inbox → `inbox done`) on the same row,
  the same lanes, or the inbox item that session filed. A `k` recorded twice in `log.jsonl` never runs
  (a second record could swap the revert the Owner reads in `STEWARD_LOG.md`).
- It parses with `shlex` and runs the operator's own code path in-process (`orchctl.assign`,
  `steward.park/unpark`, the lane files, the inbox record) — never a shell. A revert `orch start`
  enables lanes only (it never starts an engine).
- Anything else (`none …`, `rm -r rounds/…`, `edit …`, a compound or shell-metacharacter command,
  `a new ruling reverting SW-k …`) is **printed for the Owner, never run** (exit 3).
- A line is reverted **once**: `state/steward/reverts.jsonl` (under a lock) refuses a second success;
  a failed revert may be retried. `orch revert` lists the last acts and their state;
  `orch revert <k> --dry-run` shows what would run (read-only, any caller).
- Accounting: the underlying event carries `by="owner (revert #k)"`; event `steward_revert`; the
  digest counts each successful revert as a human touch (`revert #k`) and lists it under
  **Reverted by the Owner**.
- Removing a `GO_` file re-gates the row's next run; when the row already ran, the answer says so (a
  revert never undoes work a session did).

**Telegram `revert <k>`** (from `status_bot_allowed_user`; `steward.telegram_revert`, default true —
neutral on a rig without a Steward log). A file under `control/` or `state/` is plain data any same-uid
process can write, rename or rewrite, and a pid written in it proves nothing (any process can name the
live bot's). So the request and the answer each travel in a **sealed memfd** (`F_SEAL_WRITE|SHRINK|GROW|
SEAL`) held open by the process that made it; the file is only a doorbell naming that pid and fd:

- The bot seals {k, sender, message id, request id, instance} in a memfd it holds and rings
  `control/REVERT_<k>.json` {k, pid, fd}; it waits `steward.revert_wait` (60 s) for the answer.
- The engine phase `owner_reverts` reads the request through `/proc/<pid>/fd/<fd>` and runs it only when
  that process is a `telegram_bot.py daemon|poll` of **this** instance (its `ORCH_INSTANCE`) outside every
  session pane (`capability.inside`, when the check is on), the memfd is fully sealed, and the sealed
  request is for line k of this instance from the allowed user. Only that process can have put a memfd
  in its own fd table, and nobody can change it afterwards (GDV3-3 (2), applied to bot → engine).
- The answer goes back the same way: sealed in the engine (kept open 300 s), doorbell
  `state/steward/reverts/<req>.json`; the bot believes only an answer sealed by this instance's
  `engine.py`, so a forged "done" never reaches the Owner. When the engine does not answer, the bot
  withdraws the doorbell and says so.
- A forged or broken doorbell (no bot, a fd that is not a sealed request, another instance, a directory,
  bad JSON) is answered refused, recorded (`steward_revert_refused`, a digest line) and removed; it never
  stops the others, and no field of it chooses a path. Without memfd sealing (not Linux) the doorbell is
  the request and the sender id decides (v2).

**`orch why <id>`** (read-only, any caller; Telegram `why <row>` gives the same text). Plain words on:
the phase (running session and its age, helper sessions, the halt reason, the park reason and who may
unpark, the outcome of an accepted row) and, for a row that waits, every blocker in the scheduler's own
order — queue problems, lane not started, STOP, unmet deps (serial-lane deps and `needs=go` named),
PAUSE, the gate with its kind and who may release it, preflight, executor, the executor's breaker,
capacity, held resources — then the open Steward incident, the engine's own last note
(`state/sched.json`, with its age) and the last 5 events.

**Digest (item 8).** HIGH first for class P (§5), "Queue edited outside orch row add" (§9), and the
reverts above.

**Drills.** `test_29` (a Steward session assigns, pauses a lane and notes; a session's `orch revert` is
refused; the Owner's revert undoes the assign; a second revert is refused; a `none` revert is printed only; a
forged doorbell is refused and changes nothing; the real bot handler, in a process the engine can
prove, reverts the pause; the digest shows the reverts, the touch and the refusal); `test_30` (unit: the
revert grammar refuses a compound command, `rm` of anything but a `GO_` file, `orch reset`, unknown rows,
executors and lanes; a revert that is not the act's inverse, a duplicated k; forged Telegram requests —
a live bot-like process with a fd that is no sealed request, a path in `req`, a directory, a sealed
request made by a process that is not the bot — run nothing and write nowhere; a forged sealed answer
from a process that is not the engine is ignored; `orch why` names a lane not started, an unmet dep, an owner gate and who releases it;
the digest's HIGH section; the bot unit only with `watchdog.answers`, and an earlier install's unit removed). Drop B's other drills: §5–§9.

Known limits: same-uid state files (§8) — a session that rewrites `state/steward/log.jsonl` in place
changes what a revert runs (STEWARD_LOG.md keeps the original); a `telegram_bot.py`/`engine.py` process
a session plants outside its pane tree and cgroup (tmux server, systemd, cron — §8) passes the role
check; a revert undoes the act's control state, never work a session did meanwhile, and a stale
line restores the state before that act (an older assign after a newer one); reverting a Steward's
unpark re-parks the row as the Owner (only the Owner unparks it then); the bot's poll loop waits up to
`revert_wait` while a revert runs; the reverts
of `round-*`, `plan`, `rule` and `page` acts are the Owner's by hand; a bot started through the tmux
server or systemd by a session (V3_DESIGN §8's limit) would pass the proof; `orch why` computes capacity
fresh but cannot see the engine's in-memory Steward reservations (its last note in `sched.json` does).

## 11. The spend governor — money is the only page (drop C) — `lib/spend.py`

Off by default (`spend.enabled: false`): `orch spend run` then runs the command and books its cost, with no
cap and no page. A preset can turn it on.

**Ledger.** One host-wide JSON-lines file, `<spend.dir>/spend.jsonl` (default `~/.local/state/orch` of the
user's home from the password database, never `$HOME`, which a session could point at an empty ledger;
`ORCH_SPEND_DIR` overrides it for the operator's shell and the engine — tests — and is ignored inside a
session pane unless it names the same directory, so a session never picks an empty ledger). One line per
cost: ts, rig, instance, row, session, provider, model, usd, source (`metered` | `estimated` | `billing`),
envelope, phase (+ est, rc). Caps count this rig's lines (its instance path).

**Caps.** `<instance>/SPEND_CAPS.yaml` — `{daily_usd, monthly_usd, per_phase: {P2: …}, per_provider:
{opencode: …}, per_row_default, per_row: {<row>: …}}` — is the Owner's draft and a protected path (§7).
Nothing reads it for limits. `orch spend caps approve` (operator only, `capability.session`, GDV3-2)
validates it (known keys, finite numbers ≥ 0), copies it to `state/spend_caps.approved.yaml` and records
both shas and the caps in `state/spend_caps.approved.json`. Windows: `daily_usd` today, `monthly_usd` and
`per_provider` this calendar month, `per_phase` the phase's life (drop D's `phases.yaml` maps the row's
round to a phase), `per_row` / `per_row_default` the row's life. **With the governor on and nothing
approved, every cap is closed** (`daily_usd`, `monthly_usd` and `per_row_default` at 0): money waits for the
Owner, and an A then grants the shortfall on each — a one-off for that row inside a rig still closed for every
other row and capped for the month (no tamper is reported until a real `caps approve`).

**Tamper.** An edit of SPEND_CAPS.yaml after approval (or of the approved copy behind orch) is ignored —
the recorded approval stays in force — and the engine opens one Steward incident `spend_caps` per edit
(judgment; the Steward template says: INBOX the Owner, never approve). Event `spend_caps_tamper`, an
`orch spend caps` warning and a digest line.

**`orch spend run --provider P --est USD [--row id] [--envelope id] [--model M] -- <cmd…>`.**

1. The charged row is the caller's: a row session charges itself (`--row` naming another row is refused),
   an unblocker/reviewer its parent row, a Steward or planner session the rig; the operator names `--row`
   or charges the rig. The estimate is never below `spend.min_est_usd` (0.01) or `spend.min_est.<provider>`
   — with nothing approved, even a zero estimate waits.
2. Under a host-wide lock (`<dir>/spend.lock`) it checks the estimate against every applicable cap,
   counting the ledger, the **live reservations** (`<dir>/reservations/*.json`, one per running paid
   command; a reservation whose process died is booked once, at its estimate and its own time — the money
   may have been spent) and the **grants**: an approved request's headroom is held for its row and provider
   (`spend.grant_secs`, 24 h) until that row's next run takes it, so another row cannot spend the raise.
3. A cap would be crossed: nothing runs. It files `state/spend/requests/sp<n>.json` (each crossed cap
   with its limit, spent, running and **shortfall** in whole cents), prints `SPEND_WAIT sp<n>` and exits
   **75**. While a row has an open or held request for a provider, every run of that row and provider gets
   that request back (whatever the estimate: a smaller one never slips past the wait), and the operator's
   same need gets the same id — one page per request, never one per attempt. An unpaged request shows the
   current shortfall.
4. Otherwise it reserves, runs the command with `$ORCH_SPEND_OUT`, and books the actual the command wrote
   (`ORCH_SPEND_ACTUAL=<usd>`, optional `ORCH_SPEND_MODEL=`) as `metered`, else the estimate as
   `estimated`; an actual over the estimate is an event (`spend_over_estimate`). A command that never
   started books nothing. The exit code is the command's.

The work order's close part says it (when the governor is on): a paid command runs only through
`orch spend run`; on `SPEND_WAIT <id>` claim `orch blocked <row> "SPEND_WAIT <id>"` and stop.

**`spend_wait`.** A BLOCKED claim whose summary names `SPEND_WAIT <id>` of a request **for that row** puts
the row in phase `spend_wait` (a claim naming another row's request is an ordinary BLOCKED). No unblocker,
no halt, no halt page, no Steward incident, not "work left" for the rest detector; its dependents wait;
everything else keeps running. The engine tick `spend_tick` (before the scheduler): Telegram answers, the
caps watch, pages, wake-ups — an approved request sets the row aside to `retry` (event `spend_resume`),
and the re-run reserves again inside the raised cap. `orch why` names the request, its state and how to
answer; `orch status` shows the row as `SPEND_WAIT`. Switching the governor off re-dispatches rows still in
spend_wait (off = no caps); a malformed request file is skipped, never a tick failure.

**The one page.** The first tick that sees an open request marks it paged (under the lock, with the
sha of what it shows) and sends ONE `critical` page with `page_class=spend` (it reaches the phone with the
Steward lane on only when `spend` is in `steward.page_classes`, the default), in decision-brief form:

    money needed — spend sp3 (row m1, USD 3.00)
    row m1 wants USD 3.00 for openrouter (envelope E1) — command run.sh. It ran nothing and waits; …
    Cap daily_usd USD 5.00: spent USD 3.00, running USD 0.00 -> short USD 1.00.
    A) raise daily_usd by USD 1.00 (recommended: the plan names this spend)
    B) hold — the row stays in spend_wait
    answer: reply "spend sp3 A" or "spend sp3 B", or reply to this message with the letter

Exactly one option is marked: A when the command named an `--envelope` (planned spend), else B. The answer
line is the Telegram one only when a bot reads answers on the rig (`watchdog.answers` — the bot unit exists
only then, §10's 8b); otherwise it says `orch spend answer <id> A|B` on the host. Quiet hours hold a spend
page like any page but never collapse it into the held-pages summary (its A/B lines are the answer's
reference). The page is sent at most once: a failed push is not retried (the digest and `orch spend status`
still list the request).

**Answers.** `spend <id> A|B` on Telegram (or the bare letter as a reply to the page; `pages.jsonl` maps it),
from `watchdog.status_bot_allowed_user`, travels like `revert <k>` (§10): the bot seals {id, letter,
sender, the paged sha, instance} in a memfd it holds and rings `control/SPEND_<id>.json`; the engine runs
it only when it reads it from this instance's `telegram_bot.py daemon|poll` outside every pane, and answers
sealed (`state/spend/answers/<req>.json`). A Telegram answer applies only to a request that was paged and
only to the content that was paged (`paged_sha`); a reply letter also needs the replied-to message to carry
the request's current A/cap lines (a page pushed by anyone else under the id, or a stale one, is refused).
`orch spend answer <id> A|B` is the Owner's shell form (operator only).

- **A** raises each crossed cap of the approved copy by **exactly its shortfall** — a `per_row_default`
  crossing raises `per_row.<row>`, never the default — records the answer in `state/spend/answers.jsonl`
  as an Owner ruling `SPEND-<id>` (with each cap's from/to) and the raise in the approval record, and the
  engine wakes the row. A request is approved once.
- **B** holds: the request and the row stay; a later A still releases it; `orch reset` re-runs the row,
  which gets the same held request back (no new page).

Each answer is a human touch in the digest (`spend sp<n> A`), as is `orch spend caps approve`. The digest
shows spend in the window, each scalar cap's use, an unapproved edit and the waiting requests.

**Drills.** `test_31` (an over-cap run in a mock pane runs nothing; the row is in spend_wait, its
dependent waits and a sibling is accepted; exactly one `spend_page` for the request across ticks and a
re-run of the same need; no unblocker, halt or incident; `orch why`; a forged doorbell is refused and
changes nothing; a session may not approve caps or answer; the page's answer line follows `watchdog.answers`;
a reply to another text is refused; the real bot handler answers A as a reply to the page; the cap rises by
exactly the shortfall; the row resumes, books its metered actual and its dependent runs). `test_32` (an unapproved caps edit opens a `spend_caps` incident and the approved caps stay in force,
also against a hand-edited approved copy; B holds and a held need is not filed again; shortfalls in whole
cents, "exactly at the cap fits", the per-row default; a dead reservation is booked at its estimate;
validation; `$HOME` never moves the ledger; the estimate floor; closed caps refuse a cent; a held wait is
returned whatever the estimate; the approved headroom is a grant held for its row; a session cannot charge
another row; a claim naming another row's request is no spend_wait; off = run and book; digest lines).

Items 3 (meters, reconcile, economy) and 5 (the digest) are §11a (row vC2).

Known limits: the governor is cooperative — a session that runs a paid command without `orch spend run`,
or names an estimate far below the real cost of a command that reports no actual, is seen only by the meters
and the billing reconcile (vC2); each request's shortfall ignores the other open requests (answering A to two
requests on one cap raises it by both, and the second row may still need a new request); same-uid state files (§8): a session can
rewrite `state/spend/requests/*` or the approval record (the approved yaml's sha is checked, the record is
trusted), so a forged request still shows the Owner what A would raise before the answer (the Telegram
answer is bound to the paged sha); caps are per rig, the ledger host-wide (no host-wide cap); a
reservation's process found alive by pid and start time is trusted to be the run.

## 11a. Meters, the spend hold, economy, reconcile, the spend digest (drop C, vC2) — `lib/meter.py`, `lib/spend.py`

Measurement is off by default (`spend.opencode.meter: false`, `spend.reconcile: []`); it runs whether or not the
governor is on. What it enforces (requests, pages, the hold) needs the governor (`spend.enabled`).

**The OpenCode meter** (`lib/meter.py`). OpenCode keeps each assistant message in
`~/.local/share/opencode/opencode.db`, table `message` (`id`, `session_id`, `time_created` ms, `time_updated`,
`data` JSON). The same file holds `account`, `credential`, `control_account` (secrets) and the message text (`part`).
The meter's read surface is **enforced by SQLite**: the file is opened `mode=ro` with `PRAGMA query_only`, and an
authorizer denies every read except `message.id`, `message.time_created`, `message.data` and every function except
`json_extract` (`SELECT * FROM credential`, `SELECT lower(id) …`, `sqlite_master` and any write fail). Its one
query extracts `$.cost`, `$.tokens.{input,output,reasoning,cache.read,cache.write}`, `$.modelID`, `$.providerID`,
`$.time.{created,completed}`; `data` is never selected whole, and model/provider names that are not plain tokens
are booked as `?`.

- **Incremental, once per host.** The cursor (`time_created`, then `id` for ties) is host-wide
  (`<spend.dir>/meters/opencode.json`, keyed by the db path, under the spend lock), so two rigs never book one
  message twice. The first poll starts at the newest message (no back-fill of spend no cap governed). A message
  with tokens but no `time.completed`, younger than `settle_secs`, stops the poll there: its cost is not final;
  so does any message younger than 3 s (a concurrent insert may still commit behind it). An unfinished message
  older than `settle_secs` (a long step) is passed into the cursor's `pending` list (≤ 500), re-read by id every
  poll and booked when it finishes (after a day: at the cost it has then). A message without a positive cost (a
  prompt, an empty answer) is passed. Polled every `every` s by `spend_tick`.
- **Attribution.** Every tick each metering rig publishes its OpenCode-family sessions (`spend.opencode.executors`,
  else every executor whose adapter family is `spend.opencode.family`) as intervals `sid@start -> [start, end]` from
  its dispatch records (kept 8 days; a record closed without an end time ends at its start) in
  `<spend.dir>/meters/sessions/<instance-hash>.json`, refreshed at least every minute — a file silent for two
  minutes is a stopped rig, and its open sessions end at the file's mtime. A message whose window
  [created, completed] overlaps **exactly one** such session on the host goes to that session's rig and row (a row
  session → its row; `ub-`/`rv-`/`pf-` → the parent row, when it is a row of the queues; Steward/planner → the
  rig, row ""). Otherwise it is
  **shared** (row "", `attrib: shared`, `overlap: n`): the one rig whose sessions overlap it, else the polling rig.
- **Ledger line.** `provider: opencode`, `model` = modelID, `upstream` = providerID, `usd` = cost, `source:
  metered`, `meter: opencode`, `attrib`, `overlap`, `tokens`, `epoch` = the message's completion.

**A metered crossing** (governor on). After each poll, `update_hold` checks three scopes (spent + running >
limit): the **rig** — the caps a cost of `opencode` counts against (`daily_usd`, `monthly_usd`,
`per_provider.opencode`), and only when OpenCode itself spent in that cap's window (another provider's overrun is
not OpenCode's to hold); each **row** this rig metered this month — its `per_row` cap (whichever rig's poll booked
it); each **phase** of those rows — its `per_phase` cap. A crossing files ONE request per scope, `source: meter`,
whatever the amount (an unpaged one shows the current shortfall); each cap's `need` is its shortfall in whole
cents **plus `headroom_usd`** (1.00), so an A does not re-page on the next cent. It goes out as
the one spend page (§11), worded for money already spent, with **B recommended** (the cap is the plan; the other
tiers keep running):

    money needed — spend sp7 (OpenCode meter, the rig, USD 1.25)
    OpenCode spent past the cap (metered from its own db). Spend hold: nothing new dispatches to the metered …
    Cap per_provider.opencode USD 2.00: spent USD 2.75, running USD 0.00 -> short USD 1.25 (incl. headroom USD 0.50).
    A) raise per_provider.opencode by USD 1.25 — OpenCode runs again
    B) hold — OpenCode stays held (recommended: the cap is the plan; other tiers keep running)

**The OpenCode spend hold.** A breaker-like state of kind `spend` in `state/spend/hold.json` (kept apart from
`health.json`, whose tick would half-open it): while a meter request is open or held, `capacity_ok` and
`dispatch_sub` refuse the metered executors — for every row and sub-session (unblocker, reviewer, preflight,
round review, Steward, panel, planner) when the request is the rig's; for that row (and its sub-sessions) on a
`per_row` crossing; for the rows of that phase on a `per_phase` crossing. `orch why` and the scheduler note say `spend hold`; `orch status`'s spend
status and the digest show it. It closes when the request is approved (A raises each cap by its `need`, §11), or
when its caps no longer cross — a new day or month, or a higher `caps approve` — and the request becomes `lapsed`
(event `spend_lapsed`; an answer to a lapsed request is refused — nothing is raised). B therefore holds until the
cap's window resets, as its option line says. Events `spend_meter_crossing`, `spend_hold_open`, `spend_hold_close`. A meter's raise is
never a grant (nothing consumes it). Governor off: the hold is cleared, the meter keeps booking.

**Economy.** At `spend.economy_pct` (80) % of any approved cap with a day or month window (`daily_usd`,
`monthly_usd`, each `per_provider` entry; a lifetime `per_row`/`per_phase` cap that an A raised to its spend would
mark it for ever) the engine writes `state/spend/economy` (one line per cap)
and an event `spend_economy` once per cap, window and limit; the file goes when no cap is at the mark. The Steward
template: while it exists, prefer the cheaper tier in every choice. Recomputed every 60 s and at once on a new
approval or raise. Closed caps (nothing approved) are not economy; `0` turns it off.

**Billing reconcile.** `spend.reconcile: [{provider, cmd, every}]` (the framework ships no poller). `spend_tick`
starts each due `cmd` as an engine job (`spendrec:<i>`, `timeout reconcile_timeout`); it prints
`SPEND <provider> <usd_month_to_date>`. The engine compares it with the **host-wide** ledger's month to date for
that provider (billing is per account): a gap beyond `reconcile_tolerance_usd` (1.00) opens a Steward incident
`spend_reconcile` (judgment: find the spend outside `orch spend run`, INBOX the Owner, never raise a cap; one per
provider and month, again when the gap doubles) — without a Steward lane, a normal notification. Every run is a line in
`state/spend/reconcile.jsonl`; a failed command or a missing `SPEND` line is a `spend_reconcile_failed` event.
**Nothing is booked**: booking the gap would count it twice once the meter catches up.

**The digest** (`spend.digest_lines`): spend in the window; each approved cap's use in its own window (today,
this month, all time) with its percentage and the **economy** mark; an unapproved edit; the **OpenCode share**
(metered in the window, its share of the window's spend, to rows vs shared); the spend hold; the waiting requests
(meter ones marked); the last billing reconcile per provider (ok, or the gap). `orch spend status` adds the meter's
cursor, the hold, the economy lines and the reconcile schedule.

**Drills.** `test_33` (a fixture db holding only the needed columns plus a secrets table: history never
back-filled, a message during the only OpenCode session goes to its row, a prompt passes, an unfinished message
stops the poll until it finishes, a message with no session open is shared; nothing of the text or the secret
reaches the ledger; crossing `per_provider.opencode` files ONE request and ONE page across ticks and further spend,
the hold keeps an OpenCode row from dispatching while a mock row is accepted, `orch why` names it, no Steward
incident; A raises the cap by shortfall + headroom, the hold closes, the held row runs; 88 % of the raised cap is
the economy mark; the digest's OpenCode share and economy lines). `test_34` (the authorizer refuses the secrets
table, `sqlite_master`, other functions and writes; attribution units; the family default; a per-row hold; a
rig-wide hold refuses a sub-session dispatch; a reconcile gap opens one `spend_reconcile` incident, a match within
the tolerance and a failing poller do not, and nothing is booked; a higher approval lapses a crossing, closes the
hold, and an A to the lapsed request is refused; governor off: no hold, the meter books; a long step is passed into
`pending` without blocking the messages after it and booked when it finishes).

Known limits: attribution is by time overlap — two OpenCode sessions at once make their messages shared, and an
OpenCode session started by hand outside any rig is invisible (its spend is shared to a rig); session intervals
come from dispatch records, so a session's end is when the engine closed it; the hold is cooperative like the
rest of §11 (same-uid files) and holds only executors the rig dispatches (a session already running finishes);
the meter trusts OpenCode's own `cost` (a provider OpenCode cannot price books 0 — billing reconcile is the
backstop); reconcile compares host-wide month to date, so it sees every rig's spend of that provider at once,
and in local time (a provider billing in UTC can show a gap for a few hours around the month's turn; each rig
with the same entry opens its own incident). A's raise is exactly the paged need: OpenCode sessions already running
under the hold keep spending, and spend beyond the headroom before the answer files a new request and page once the
hold lifts. Rigs with different `spend.dir` each meter the whole db (the cursor lives in `spend.dir`); a message
another rig's poll books for this rig carries no phase (its `per_phase` cap sees it only through the row check).

## 12. The phase fence — new phases stay the Owner's (drop D, vD1) — `lib/phases.py`

**The phase rule.** The rig runs only inside a phase the Owner opened. No Steward, planner or row creates, opens or
starts a phase; at a phase's cap the phase rests. **Off without a phases file**: a rig with neither
`<instance>/phases.yaml` nor `state/phases.json` has no fence (v2 behaviour, every v2 suite unchanged).

**Files.** `phases.yaml` is the Owner's readable view and a protected path (§7):
`{phases: {P1: {state: open|rest|closed, opened, caps: {rows, rulings, days, loops}, rounds: [p1, p1d], loops: 0}}}`.
The fence trusts `state/phases.json`: the same phases plus the sha of the `phases.yaml` orch last wrote. Only
`orch phase` and the engine write them, always together (the view first, then the record), under
`state/phases.lock`, and leave the marker `state/phases.on`: deleting both files keeps the fence on with no phase
open (reported; the engine also reports a fence that vanished entirely). `orch phase off` (the Owner's) removes all
three.

- An edit of `phases.yaml` outside `orch phase` (a hand edit, a session setting `state: open`) is **ignored** — the
  record stays in force — and reported once per content: event `phases_tamper`, a normal notification, a digest
  line. `orch phase approve` (the Owner's) validates and adopts a hand edit and prints what it changes, phase by
  phase (it adopts whoever's edit is in the file: read the diff). A `phases.yaml` with no record is
  "never approved": the fence is on and no phase is open. Deleting `phases.yaml` keeps the fence (the record).
  Why not trust the file: the protected-path close check sees it only at a row's close, and Steward and planner
  sessions are not rows.
- **orch's own writes are not a row's change.** The engine rewrites `phases.yaml` while rows run (a rest, a round
  recorded, a loop counted). `protected.note_write` records the sha orch wrote (`state/protected_writes.json`,
  latest per path); the close check does not charge an uncommitted protected path whose content is exactly that
  sha, and only for `protected.ORCH_WRITTEN` (`phases.yaml`): a forged entry for any other path is ignored. A
  later edit, or a restored older version, is charged as before.

**Commands** (`orch phase …`; open, rest, close and approve refuse inside any session by ancestry, §8):

| command | does |
|---|---|
| `open <P> [--caps rows=N,rulings=N,days=N,loops=N] [--rounds a,b] ["why"]` | creates, re-opens or re-caps P; `--rounds` adds existing or future round tags (a round belongs to one phase). A re-open restarts the days and rulings clocks. Refused when P would be at a cap at once (it would rest on the next tick) — raise the cap. Warns when P has no caps. |
| `rest <P>`, `close <P>` | rest: running rows finish, nothing new starts; closed: nothing of P dispatches |
| `approve` | adopt a hand edit of phases.yaml (prints the per-phase diff) |
| `off` | remove the fence (phases.yaml, the record, the marker) — v2 behaviour |
| `status` | each phase, its state, its caps against their use, its rounds; the tamper note |

**The fence** (when on):

- **Engine.** `schedule()` builds one `phases.Fence` per pass (lane → round → phase). A row dispatches only when its
  lane's round is in an `open` phase; in `rest` a `retry` row (mid-life) still dispatches, a row that has not
  started (phase `''`, including a reset) does not; in `closed` nothing does. A lane in no round, or a round in no
  phase, waits (fail closed); so does a lane that rounds of different phases list (by folder name and by the
  file's own `tag:` — a round.yaml cannot borrow another round's tag), and every row while the fence cannot be
  built (a malformed round.yaml maps no lane; any other failure blocks all rows, the scheduler goes on). The rows
  cap also holds **within one scheduler pass**: a row's first start is counted as it dispatches, so six ready rows
  under `rows: 2` start two. The scheduler note starts `PHASE —` (`orch why` shows it), and the Steward's rest
  detector does not count those rows as work left (a resting phase is the Owner's, like `spend_wait`). Reviews,
  unblockers and the Steward's own sessions are not fenced: running rows finish.
- **Caps → rest** (`phase_tick`, before the scheduler): rows started (a dispatch record whose epoch is ≥ the phase's
  `rows_since`, the whole second of its first open — vD3: rows that ran before the phase existed are not its) ≥ `rows`; Steward rulings
  (`SW-<k>`, rig-wide) since the latest open ≥ `rulings`; days since the latest open ≥ `days × 1.5`; the phase's
  course-loop count ≥ `loops` (`phases.record_loop`, drop D item 2). The engine sets `state: rest` with
  `rest_reason`, logs event `phase_rest`, sends a normal notification and a digest line. **Nothing opens or starts
  a phase**: the engine only ever moves open → rest.
- **New rounds** name their phase: `orch round new <tag> --phase P` (operator, and the Steward / planner through
  `orch sw round-new <tag> --phase P <lane>… "why"`). Refused without `--phase`, for a phase that does not exist or
  is not open, or one at a cap; on success the round is recorded in P (`round.yaml` `phase:` is only a label).
  `--phase` on a rig without phases is refused, never ignored. A tag another phase holds, or a lane a round of
  another phase lists, is refused; a refused `round new` removes only the `round.yaml` it wrote.
- **Starting.** `orch round start` (operator; `--force` does not cross the fence), `orch sw round-start`,
  `orch sw start <lane>` and `orch dispatch <id>` (`FORCE=1` does not cross it) refuse a round, lane or row whose
  phase is not open (or is at a cap). The Steward's `plan <tag>` dispatches a planner only for a round of an open
  phase below its caps, or — a new round — while some phase is.
- **`orch row add`** (§9) refuses a lane outside an open phase and a row once the phase already holds `rows` rows
  (a row past the cap could never run).
- **Spend.** `spend.phase_of` (the `per_phase` caps, §11) reads the trusted record, not the file.
- **Digest.** A "Phases" section: each phase with its caps' use, rests and tamper reports. `orch phase open|rest|
  close|approve` count as human touches (the Owner's by design).

**Drills.** `test_35`: a row cannot open a phase; the Owner opens F1 (rows cap 2); a Steward session's `round-new`
without `--phase` and with a never-opened phase are refused and its `round-new --phase F1` is recorded; `orch row
add` past the rows cap is refused; two rows start → F1 rests while they run, both pass their close checks (the
engine rewrote the protected phases.yaml under them), the third never dispatches (note `PHASE`, not "work left"),
the Steward can start nothing and cut no round in it; `orch phase open F1` without a higher cap is refused, with
one the third row runs. `test_36`: neutral without phases (and `--phase` refused), shape validation, rest vs closed
vs no round, `--force` does not cross, days × 1.5, rulings since the open, loops, a hand edit ignored + reported
once + charged by the close check, operator-only commands, `approve`, a deleted file keeps the fence.
`test_37` (the adversarial pass's findings): two phases' rounds on one lane, a borrowed tag, a malformed
round.yaml, a failed fence, a round-new into another phase's tag (the folder's files survive) or lane, `orch
dispatch` with `FORCE=1`, the Steward's `plan`, the engine never resting a closed or re-capped phase, the approve
diff, a forged protected-write entry, both files deleted (the marker), a vanished fence reported, `orch phase off`.

**Known limits.** Same-uid state files: a session that writes `state/phases.json` itself (or deletes the record,
the view and the marker — then the fence is off; a running engine reports it once a day, an engine started
afterwards cannot tell) defeats the record (the
review and the close checks remain the guard, as for `state/spend_caps.approved.*`, §11). Rulings are counted
rig-wide since the latest open (rulings carry no phase). The rows cap counts rows started since the phase's first open (`rows_since`, kept across rest and re-open and
never moved by `orch phase approve`), so a re-open after a rows rest needs a higher cap; an old row run again counts.
A record written before vD3 has no `rows_since`: it counts every row that ever started until its next `orch phase
open` or `approve`, which sets `rows_since` to that record's latest open. A dependency across phases waits while the other phase rests. A session
that creates a `phases.yaml` on a v2 rig turns the fence on with nothing open: every row waits (fail closed,
reported). Reviewers and unblockers of rows already running still dispatch in a closed phase.

## 13. The native course loop and the executor policy per round (drop D, vD2) — `lib/courseloop.py`, `lib/rounds.py`

**The course loop.** v2's loop (docs/COURSE_LOOP.md) waited for a human at every step. A round the operator
created may now declare `judge: <row>` (a row of its own lanes), `on_fail: course_loop` and `max_loops: <n>`
(default 1). **Off by default**: no `on_fail`, no loop; a malformed declaration is reported once (event
`course_loop_config`, `orch lint`, `orch round status`) and does nothing. Engine tick phase `course_loop_tick`
(before the Steward's) walks one step per tick; the record is `state/courseloop/<tag>.json`, events
`course_loop_<step>` on `round:<tag>`:

| state | moves on when | to |
|---|---|---|
| idle / judging / done / held / rested / stopped | the judge is **accepted with a negative outcome** (nogo, held, fail, an unknown token) — a new claim (outcome + attempt + claim time) | the limits below, then `review` |
| review | `rr-<tag>` (dispatched by the engine, the loop's context in its work order) is DONE | a `round_review` incident (judgment), `answer` |
| answer | a planner was dispatched **from that incident** (the Steward's `orch sw plan <next>`, or the panel's CONFIRMed staged plan) for a round other than `<tag>` | `plan` |
| plan | a planner of round `<next>` closed PASS, added ≥ 1 row with `orch row add`, the round is in the judged round's phase, lists none of the judge's lanes, and is started | `run` |
| run | every row this loop's planners added (`row_add` events by a `planners.json` planner of `<next>` since the loop began) is accepted or parked, **at least one accepted**, none gone from the queues | the engine resets the judge (`course-loop:<tag>#<n>`), `judging` |

Every tick of an active state first checks that the judge is still the accepted claim the loop started from: reset
or re-judged under it, the loop ends — a GO as `done`, anything else `stopped` (the next negative claim starts a new
one). While the phase is not open, `answer` and `plan` wait instead of stopping (the Steward's `plan` is fenced in a
resting phase); when it re-opens and the incident closed without a planner (or the planners failed and nothing
re-planned), a fresh `round_review` incident asks for the plan once (state `answer`). `owns_negative` counts every
round that declares the judge, so a duplicate declaration switches the loop off but never the guard. The review incident opens when the review is DONE, with or without QUESTIONS.md (a review that asks the
Owner nothing still needs a correction planned); the loop does not read ANSWERS.md.

- **Limits, checked at the trigger.** The round's `max_loops` and the phase's `caps.loops` (the record's count).
  With none left, the phase **rests** (`phases.set_state … only_from=open`, event `phase_rest`, reason "course loop
  <tag>: …") and one inbox item says why — once per judge claim; raising `max_loops` or the cap (and re-opening)
  starts the loop for that same claim. While the round's phase is not open (or the round is in no phase) the loop
  is **held** and starts when it opens. Without a phases file only `max_loops` bounds it. Starting a phase is never
  the loop's (the phase rule).
- **Counting.** A loop counts as it starts: the record's `loops` and `phases.record_loop` (phases.yaml `loops`).
  **vD1's loops cap changes meaning:** `caps.loops: N` is the number of loops the phase allows — the loop that
  brings the count to N still runs (its planner creates a round, its rows dispatch), so `cap_reached` is
  `loops > N` for this key (≥ for the others); the course loop rests the phase when the judge fails once more.
- **The judge's NO-GO is the loop's.** The Steward's scan opens no `negative_outcome` incident for a course-loop
  judge; one opened before the round declared its loop (and with no session yet) is closed "handed to the native
  course loop". While a course-loop judge is accepted with a negative outcome (`courseloop.owns_negative`), no
  session re-runs it: `steward.execute` refuses `reset` / `retry` (Steward, planner, panel), `rearm.validate` refuses
  a row's `orch rearm` of it, and `orch row add` refuses a session's line whose `rearm=` names it (vB2's deferred
  item, scoped to the loop's judges so a rig without a loop behaves as before). Only the loop's re-arm — after the
  correction's rows are done — runs it again; the operator's `orch reset` stays the Owner's.
- **One judge, one loop.** A judge that two rounds name loops in neither (reported): one claim would start two loops.
- **Who acts.** The engine: the trigger, `rr-`, the incident, the re-arm. The Steward (judgment tier): ANSWERS.md
  within the rights, inbox for W, `escalate-panel` for P, `orch sw plan <tag>-l<n> "brief"`; its bundle carries a
  "native course loop" section with these steps. The planner: `round-new --phase`, `orch row add`, `round-start`.
  Templates: the round review gets `__LOOP__` (which loop, the judge's outcome); the Steward and planner templates
  say never to reset the judge.
- **Stops.** The Steward closed the incident without a planner; the planners of `<next>` failed and nothing
  re-planned (an open `planner` incident on `round:<next>` is waited for); PASS with no `row add`, an unstarted
  round, or a round in another phase; the review ended twice without DONE; the judge was reset or re-judged under
  the loop. Each: state `stopped`, event `course_loop_stop`, a normal notification, one inbox item. The next
  negative claim of the judge starts a loop again (within the limits).
- **Only the operator's rounds loop.** A round.yaml with `by:` (a Steward or planner made it) is ignored: a session
  never gives itself a loop budget. The loop needs `steward.enabled`. (v3.1, §15.5: unless its phase says
  `session_loops: true` — then within `caps.loops`, on lanes of its own, never on another round's judge.)
- **Restarts.** The record is written before the phase's count and before the review is dispatched; the phase count
  carries a `counted` flag re-tried on the next tick (a crash between `record_loop` and that save can count twice).
  A review that cannot be dispatched ten ticks in a row stops the loop.
- **Surfaces.** `orch round status` (judge, loops n/max, state), the digest section "Course loops (inside the open
  phase)", `orch lint` warnings.

**Executor policy per round.** `round.yaml` `executors: {retier_on_max_unblock, dead_window_policy,
unblock_executors, max_parallel}`. `rounds.exec_policy(cfg, lane)` (the engine's `exec_policy(lane)`) lays the
first three over the rig's `executors:` for the lane's round (an open round wins over a closed one; only a round
the operator made — a session's round, `by:`, only narrows `max_parallel`, its tier keys are ignored and linted) and returns
`round_max_parallel` + `round`; used by `on_dead` (dead-window policy and target), `on_blocked` (retier past
`max_unblock_attempts`), `drive_unblock` / `ladder_up` (`te.unblock_executor(cfg, n, lane=…)`) and `capacity_ok`
(`round_capacity`: live sessions of the round's rows and their reviewers/unblockers on that executor < the round's
cap; the rig's caps still apply, so a round only narrows them; Steward and other rig sessions are not counted).
Values are validated (`rounds.policy_problems`: executors in `executors.allowed`, `halt|retier`, a non-empty list,
whole numbers ≥ 0); a bad key is ignored, never half-applied, and named by `orch lint` / `orch round status`.
Cached on the round files' mtimes. `load_rounds` now skips a round.yaml that is not a mapping (it used to raise).

**Drills.** `test_38` (drill 6, in an open phase): the judge closes `--outcome nogo` through the
CLI → `rr-lp` writes QUESTIONS.md → incident → the Steward writes ANSWERS.md and plans `lp-l1` → the planner's
round-new `--phase`, `row add`, round-start → the row runs → the judge re-armed → nogo again at `max_loops: 1` → the
phase rests with the course-loop reason, one inbox item, no second review, the `needs=go` dependent never runs,
**zero human touches after the round start**, no page. `test_39` (no phases file): a review with no questions, the
re-armed judge closes GO, the loop is `done`, the dependent runs. `test_40` (units): neutral default, every refused
configuration, the loops-cap meaning, the refused session reset/retry of a NO-GO, the policy accessor, the unblock
tier, `capacity_ok`, `on_blocked` / `on_dead` with the round's values, lint. `test_41` (engine): a round's
`max_parallel {mock: 1}` serialises its rows; its `unblock_executors` picks the unblocker's tier. `test_42` (the
adversarial pass's findings, on a copy of the state): a row's `orch rearm` and a session's `rearm=` grant of the NO-GO
judge refused; an all-parked or vanished correction never re-arms; a judge reset or re-judged under the loop ends it
(GO = done); a correction planned into the judged round stops it; only the loop's own planners' rows count; one judge,
one loop; a session's round keeps the rig's tiers. `test_38` also checks the re-arm came after the correction's
accept. `test_36`'s loops-cap assertion follows the new meaning.

**Known limits.** Same-uid state: round.yaml is not a protected path (a session can edit `max_loops`, `judge` or
remove `by:`); deleting `state/courseloop/<tag>.json` resets the round's count; a forged record in state `run` with
the current claim's signature, or a forged `planners.json` entry plus a `row_add` line, reaches a re-arm — with phases
the loops cap in the fence's record still bounds the loops, without phases nothing does (the review and the digest are
the guard, as for every `state/` file, §7–§12). The re-arm waits only for rows added with `orch row add`. The loop
does not check that ANSWERS.md answers every question (the Steward's analysis and the digest are the record). An
Owner's Telegram answer session (`watchdog.answers`) keeps its own executor, not the round's `unblock_executors`.
`max_parallel` counts dispatch records by lane, so a lane two open rounds list counts for the first. In
`scheduler.mode: serial` a correction must live in lanes of its own (enforced: a round listing the judge's lane stops
the loop).

## 14. The drill suite (drop D, vD3) — `tests/test_v3_autonomy.py`

Every pre-registered drill of the autonomy plan (§6) is a test in `tests/test_v3_autonomy.py`, run by
`tests/run_all.sh` in a mock sandbox. Between the fault a drill injects and the recovery it expects, no operator
command runs (the drill's own setup — writing the queue, `orch start <lane>`, config the rig would carry — comes
before the fault). `test_99` checks the human touches the digest counts across the whole file (resets, assigns,
approvals, manual dispatches, round starts, Owner park/unpark, reverts, spend answers, phase acts); lane controls
(`start`, `stop`, `disable`) and config edits are not counted there — the map below says where a drill uses one.

| drill (plan §6) | expected | test(s) |
|---|---|---|
| 1 reboot | engine back ≤ 2 min, in-flight rows re-dispatched as interrupted, 0 halts | **`test_43`**: the engine run by the generated unit's own `ExecStart=` (`--wait-lock`) under a respawn loop that stands in for systemd's `Restart=on-failure` after the unit's `RestartSec`; the tmux server is killed under a running row and the engine is SIGKILLed; no orch command until the row is accepted. `test_01` (tmux session gone; recovers with `orch start`, the pre-unit path) and `test_02` (boot id) are the units |
| 2 broken CLI | breaker opens after 2 deaths, no retier, dispatch resumes once restored | `test_03` (the probe is configured before the fault; the drill only restores the "CLI") |
| 3 dead unblocker | Steward ticket → reset → accepted | `test_04` |
| 4 reserved non-wall question | Steward ruling with a revert line, row continues, digest shows it | `test_05` (asserts the digest's `SW-1 … revert:` line) |
| 5 wall (a row deletes a test) | engine refuses, row parks, inbox item, 0 pages | **`test_44`** (end to end: the guard refuses the staging, a bypass commit, the close check refuses ×3, halt → Steward → inbox → park; a later independent row runs), `test_06` (wall answer path, Owner unpark), `test_22`/`test_24` (protected-path checks; a reset does not launder a protected change) |
| 6 judge NO-GO | review → answers → planner → round starts, no human | `test_38` (+ `test_39` GO after the correction) |
| 7 over-cap paid call | exactly 1 page, the rest keeps running | `test_31` (`orch spend run`), `test_33` (OpenCode meter crossing) |
| 8 the guard | drills 1–7 fail on the previous framework generation | this file run against that generation — the build's drill record |

**Pages are measured, not assumed (vD3).** `Rig.notify` appends one line per notification to `state/notify.jsonl`
(`epoch`, `urgency` after the Steward-lane downgrade, `asked`, `page_class`, `row`, `title` ≤ 120 chars; never the
body) before any push. "0 pages" in a drill means no `critical` line since the test started (and nothing held for
quiet hours); the halt drills also assert their halt was announced as critical and downgraded, so a build that
pages every halt fails them, and `test_00`/`test_43` assert the record saw their notifications. The downgrade keeps
a halt off the phone only with `watchdog.push_min_urgency: critical` (a strict preset; v2's default `normal`
pushes downgraded halts too). Not recorded (they do not go through `Rig.notify`): `orch notify` (any session may
page with it, as in v2 — the course-loop planning prompt uses it), `orch digest --send` and the scheduled digest
(ordinary messages), and the dead-man's `engine_down` page.

**Fixes from the vD3 adversarial pass (drill `test_45`).** An Owner park is lifted only by
the Owner: `orch sw reset|retry|assign|approve` and a row's `rearm=` grant are refused on an Owner-parked row (a
reset cleared the park before). A phase's rows cap counts from its first open (§12).

Known limits: the tests never drive the real systemd. The unit's `Restart=on-failure` (the plan's §4.1 says
"always"; on-failure is what restarts a crash or SIGKILL — a clean `SIGTERM` exit, e.g. `systemctl stop`, is
deliberately not restarted) and `RestartSec` are read from the generated unit and emulated. The dead-man timer's
stale-heartbeat restart and its one `engine_down` page after 2 failed restarts are not drilled; `test_13` covers
only its deliberate-stop branch.

## 15. Adaptive plans (v3.1) — `lib/courseloop.py`, `lib/phases.py`, `lib/store.py`

**The rule:** the goal and the bars are fixed; the route can change. A planning session cannot see 60 rows ahead,
so a round is planned in waves and corrected as it measures. When a checkpoint finds the round wildly off track, or
finds that the rest of the plan cannot show a result, the rig does not run the rest of that plan. It reviews the
evidence and replans toward the same goal. A replan never reaches a goal by moving a bar, threshold, denominator,
definition, key, a tolerance, scorer or test. Under the North Star, a GO obtained that way is the worst result.

### 15.1 Waves

A planner cuts **wave 1**: only the rows it can specify now. Each wave ends in a **checkpoint judge**, a verdict
row that answers two questions (the standard judge block, `templates/parts/judge_block.md`):

1. Is the bar met for this wave?
2. Can the remaining plan still reach the goal?

The later waves are not cut yet. A **replan row** (queue extras `needs=go` on the checkpoint,
`rearm=<checkpoint judge>[,…],round`) runs after a GO. It reads the measurements, then adds the next wave, the next
checkpoint and the next replan row with `orch row add`. Each added line carries a subset of its own grants (§9). A
session may hand on a checkpoint judge's grant (§15.2); the planner's wave-1 lines carry the grants for the whole
chain. The round's **final judge** (`judge:`) carries the round's bar. The planner writes its PLAN in wave 1, so the
bar is fixed before any result exists. No session's line may grant `rearm=` of the final judge (vD2). So the final
judge is either:

- cut by the last replan row, with deps on the last wave, its PLAN already written; or
- pre-cut in wave 1 by the operator. It closes `NOT ARMED` with no outcome until an operator-cut row armed with its
  grant runs `orch rearm` (a track judge carrying `rearm=<final judge>`).

The planner leaves row-cap headroom in the phase for later waves and loops (the guide gives the rule).

### 15.2 Checkpoint judges: `judges:` in round.yaml

```yaml
judge: final1                # the round's final judge, unchanged (its loops are failure loops)
judges: [check1, {row: check2, max_loops: 2}]   # checkpoint judges (their loops are replans)
on_fail: course_loop         # one switch for all of them
max_loops: 1                 # default for each judge; an entry may set its own
bars: [phase1/check1_scorer/BARS.md]            # optional: files whose bytes are pinned (§15.6)
```

- **Each judge has its own loop.** The scalar `judge:` keeps every name it had: record `state/courseloop/<tag>.json`,
  review session `rr-<tag>`, folder `rounds/<tag>/review/`, incident subject `round:<tag>`, suggested correction
  round `<tag>-l<n>`. Existing round.yaml files and records load unchanged. A checkpoint judge `J`
  uses the key `<tag>+<J>`: record `state/courseloop/<tag>+<J>.json`, review `rr-<tag>+<J>` writing to
  `rounds/<tag>/review+<J>/`, incident `round:<tag>+<J>`, suggested round `<tag>-<J>-l<n>`. `+` cannot appear in a
  row id or a round tag (`rearm.ID_RE`/`TAG_RE`), so a key can never collide with a round. Two checkpoints of one
  round loop independently: separate counts, reviews, incidents and planners.
- **Checkpoints are declared up front.** No command writes `judges:`. The planner declares every checkpoint id of
  the planned waves when it plans the round, before any row exists. An id with no row yet is reported once ("not a
  row yet") and loops as soon as a replan row adds it. A checkpoint a session never declared closes into the
  Steward's `negative_outcome` incident, not a loop.
- **Refused declarations** are reported once (event, `orch lint`, `orch round status`), and that entry does nothing:
  - an entry that is not a row id or a `{row, max_loops}` mapping;
  - a judge outside the round's lanes;
  - a checkpoint that is also the round's `judge:` (the scalar wins);
  - `judges:` without `on_fail: course_loop`.
  - **One judge, one loop:** a row that two rounds name (as `judge:` or in `judges:`) loops in neither. There is one
    exception: when exactly one of those rounds is the Owner's (no `by:`), the Owner's loop stays and the session
    rounds are refused. A session's round never takes another round's judge. The guard on a final judge's grant
    follows every declaration, duplicates included.
- **Guards.** The guards of §13 apply to every loop judge: no session resets, retries or `orch rearm`s a judge
  holding a negative outcome; only the loop re-arms it. There is one difference, on purpose. A session's `orch row add`
  line may grant `rearm=<checkpoint judge>` while that judge holds no negative outcome. This is what lets a replan
  row chain the next wave's replan row; the grant is still a subset of the caller's own. The final `judge:` keeps
  vD2's rule: no session's line may grant it at all.

### 15.3 The infeasible outcome: `--outcome infeasible`

`orch done <id> "…" --outcome infeasible` (seal `CLOSED:infeasible`) means "the remaining plan cannot reach the goal".
It is a fifth outcome word, not `held` with a reason:

- **Machine-readable in one place.** The scheduler, the Steward's scan, the course loop, the digest and the drills
  all read line 3 `OUTCOME <x>`. A reason in the free-text summary is prose, which nothing can check.
- **`held` already means something else on the rigs.** It means "measured, not ready: wait" (a track-limit
  close, HELD rows). "Change the route" is the opposite advice.
- **Rollback-safe.** Since drop B, an engine that does not know the word reads it as a negative outcome, so its
  `needs=go` dependents stay held.

What it does:

- **Holds** the `needs=go` dependents, like any negative outcome.
- **Is never re-run to obtain a GO.** `steward.execute` refuses `reset`/`retry`, `rearm.validate` refuses
  `orch rearm`, and `orch row add` refuses a session's `rearm=` naming the row. Through `courseloop.owns_negative`,
  this now covers *any* row accepted `infeasible`, not only loop judges. The operator's `orch reset` stays the
  Owner's.
- **A loop judge** (scalar or checkpoint) starts its course loop as for `nogo`, with the replan steps:
  1. The engine records the **held wave**: the `needs=go` dependents not yet accepted, and every row whose deps reach
     one of them.
  2. `rr-` reviews with the loop context "the remaining plan cannot reach the goal". The brief proposes a new route to
     the same goal and names the held rows it makes moot.
  3. The Steward (or the panel, by the rights) parks those rows (`orch sw park <row> replan-<key>-<n> "why"`, an
     existing S right) and plans the new route (`orch sw plan <next>`).
  4. The planner cuts the new rows.
  5. The judge is re-armed and measures the same, pinned bar (§15.6).

  At the re-arm, the record and the digest list which held rows were parked and which were kept.
- **Any other row:** the Steward's `negative_outcome` incident, with a "remaining plan cannot reach the goal"
  section. The section lists the held wave and the same review, park, replan steps, under the same never-re-run rule.

The engine does not decide which held rows "cannot produce results". That is judgment, taken under the rights; the
recommended class for it is P. What the engine guarantees is the hold, the never-re-run rule and the pinned bar.

### 15.4 Replan loops vs failure loops, and what they count against

A checkpoint's loop is a **replan**: the route changes, and the next wave depends on it. A loop of the final judge is
a **failure loop**: the round missed its bar. The Owner's caps may separate the two:

| option | how | risk / cost |
|---|---|---|
| A — one budget (default: today's behaviour) | every loop counts in `caps.loops` | a plan with 3 checkpoints and `loops: 2` can adapt only twice; raising `loops` also loosens failure loops |
| **B — separate caps (recommended)** | the phase sets `caps.replans: N`: checkpoint loops count there (phases.yaml `replans`); final-judge loops stay in `caps.loops` | one more number for the Owner; neither budget can spend the other |
| C — replans uncounted | only the round's `max_loops` bounds a checkpoint | a session-cut round's judges could adapt without a phase bound — refused (§15.5) |

**Built: B behind the phase's own setting.** Without `caps.replans` on the phase, every loop counts in `loops`
(option A, the current behaviour). With it, checkpoint loops count in `replans`. Each counter follows the
`loops` rule:

- the loop that brings the count to N still runs;
- `> N` rests the phase;
- a loop with none left rests the phase and writes one inbox item.

A loop records its counter when it starts (`current.counter`), so a cap edited mid-loop does not re-count it. The
setting is a phase cap: the trusted record holds it, and only `orch phase open|approve` from the operator's shell
writes it (§12).

### 15.5 Loops on rounds a session creates (`by:`)

A round a planner or Steward created loops only inside a phase the Owner opened with
`orch phase open <P> --session-loops on` (phase key `session_loops: true`, operator-only like every phase write).
Then:

- its loops always count in `caps.loops` (never `replans`);
- the phase must have a `caps.loops`;
- its round.yaml `max_loops` can only narrow the budget, never widen it.

Its judges must sit in lanes no other open round lists, so it never loops on the Owner's rows. It declares them in
the round.yaml it created; no command writes them, and the digest and the review show them. Without the flag, or
without phases, the round's `on_fail` is refused as before ("created by session …"). A session never grants itself
a loop budget: the budget is the Owner's phase cap. Allowing session loops lets them spend the same `caps.loops` as
the Owner's round judges. That is the Owner's trade, made by switching the flag on.

### 15.6 The guards that stay, and the one added

Kept unchanged:

- the phase fence;
- caps rest a phase, and nothing opens one;
- `needs=go`;
- a negative outcome is never re-run;
- one judge, one loop;
- a correction lives in a round and lanes of its own, inside the judged round's phase;
- protected paths and the close check (§7);
- `orch row add`'s rules: new ids, no gate removed or changed, no twins, no wider `rearm=`, the row cap;
- the Owner park;
- the panel for class P.

**Added: the bar pin** (`steward.bar_pin`, default on). When a loop starts, the engine records sha256 of:

- the judge's PLAN file;
- the round.yaml `goal:` block;
- every path in round.yaml `bars:`.

At every later step it compares them: review, answer, plan, run, and also while the re-armed judge is pending
(`judging`). A loop chain keeps its first pin (`pin0`), so a new negative claim after the loop's own re-arm is checked
against the bar as it was when the chain began. A difference stops the loop before the re-arm: event
`course_loop_bar_moved`, `course_loop_stop`, "the bar moved", and one inbox item.

- A re-armed judge that has not been dispatched yet is parked (carry `bar-moved`), so it never measures a moved bar.
- A judge already running is flagged: its outcome is on a moved bar.
- No loop starts while the bar stays moved (`bar_hold`). Two things release it: the bar restored byte for byte, or
  the Owner's own `orch reset` or `orch unpark` of the judge. A replan that edits the judge's PLAN, the goal or a listed bar file therefore never
reaches a re-armed judge. A replan **row** that edits a test, key or scorer the Owner listed in
`guard.protected_paths` fails its close check (§7). The Owner adopts a changed bar the Owner's way: a ruling, then
`orch reset <judge>`, which ends the loop as superseded.

### 15.7 Known limits

- **Same-uid state.** As in §7–§13, a session can rewrite `state/courseloop/*.json`, `state/phases.json` or round.yaml.
  round.yaml is not a protected path, so a session can edit `judges:`, `bars:`, `max_loops` or remove `by:`. The
  phase's trusted counters still bound the loops; the review and the digest are the guard.
- **What the pin covers.** It covers only the judge's PLAN, the goal block and the listed `bars:` files, as they
  were when the loop started. An edit before the first loop is planning: the plan review's to catch. Tests, keys and
  scorers belong in `guard.protected_paths`. A loop already running when v3.1 lands has no pin.
- **Which held rows to park** is judgment: the engine lists the held wave and records the disposition, but does not
  decide it.
- **The "NOT ARMED" pattern.** A judge that closes with no outcome counts as met for `needs=go`. Cut a wave's
  GO-only dependents in the replan row that arms the judge, never before it. `held` is a negative outcome: on a loop
  judge it starts the loop (it spends a loop). Not armed is "no outcome", never `held`.
- **A judge that edits its own PLAN mid-run.** The judge's PLAN folder is its allowlist. An edit during its run is
  caught when the run claims (the loop stops and flags the outcome), but a GO has already released its `needs=go`
  rows. Listing judge PLANs and bar files in `guard.protected_paths` makes the close check refuse that claim.
- **Session loops share the Owner's loops cap** (§15.5), by design.
- **Concurrency.** A checkpoint loop and the final judge's loop of one round may run at the same time. Their
  corrections are separate rounds, and the phase's counters bound them together.

Drills: `test_46`–`test_52` in `tests/test_v3_autonomy.py`:

| drill | covers |
|---|---|
| `test_46` | two checkpoints loop independently |
| `test_47` | `infeasible`: the held wave, parked rows, the plain-row incident |
| `test_48` | the bar pin: planner, `goal:`, `bars:`, PLAN path, judging, between loops, `bar_pin: off`; a protected key fails the close check |
| `test_49` | a `by:` round loops only within the phase cap |
| `test_50` | a v3 record and `judge:` round are left unchanged |
| `test_51` | the loops and replans caps rest the phase |
| `test_52` | units |

## 16. Not yet built

Nothing of drops A–D or v3.1. Open items for later work are the known limits of §1–15.

## 17. Executor variants and the rate-limit fallback (v3.2) — `lib/fallback.py`

**Why.** A non-Claude executor (an OpenCode model on a provider plan, a Gemini key) can sit at its provider's rate
limit for hours; the engine's answer for a Claude usage limit ("the session waits", nudged every 15 min) idles the rig.
On a long-running rig, two OpenCode rows stood rate-limited for a long stretch and were nudged repeatedly before the
operator moved them to another executor by hand. The fallback moves the work to another executor, as mechanical acts the Owner can read and revert.

**Real samples** (a normal OpenCode install with its default model): OpenCode's own log and database hold
provider-quota failures with message `Rate limit exceeded. Please try again later.`,
class `provider.quota`, HTTP 429, body `{"type":"error","error":{"type":"FreeUsageLimitError", …}}` — in the same window
in which the engine's old path logged "hit a usage limit" for the two rows (the quota failure recurred after each resume nudge). That text is the shipped
pattern (`tests/fixtures/opencode_rate_limit.txt`). **UNPROVEN:** how the TUI frames and animates that text on screen (no
pane capture was kept), and any Gemini quota text (none has been observed).

**Variants** (`executors.variants`). `{name: {base, model, env, args, max_parallel}}`: the name is a full executor
(allowed, assignable, in `max_parallel`, `reviewer_map`, `unblock_executors`…). It runs through the base's adapter
(`launch.sh` hands the adapter the base's name, `ORCH_EXECUTOR` the real one) and inherits what the base has by
name: flavor, `paste_kick`/`arg_kick`/`auto_unblock`, `reviewer_map`, health probes/remedies/auth patterns, limit
patterns, `executors.args`/`env`. `model` is `ORCH_VARIANT_MODEL`: the OpenCode TUI has **no `--model` flag** (only
`opencode run` does; measured: it prints help and exits), so the adapter sets `OPENCODE_CONFIG_CONTENT={"model": …}`
(measured: the TUI then runs that model); the claude adapter uses it as `--model`. `orch lint` warns on `--model` in
`ORCH_OPENCODE_FLAGS`.

**Detection.** For a live row whose executor has a `fallback` entry the engine looks at the last 40 non-blank pane lines
every tick. The row is *rate-limited* when a `limit_patterns` regex matches and the tail has not otherwise changed
(digits inside the matching lines and spinner glyphs are ignored) for `limit_confirm_secs` (60) — any tick on which the
text is gone or other text moved restarts the clock, so a "429" printed once in test output that then moves on never
fires. State: `state/<row>.rl`. Such executors do not use the old usage-limit notify/nudge path unless no fallback
target is free.

**Trigger and acts** (engine phase `fallback_tick`, after `monitor`). `limited_rows` confirmed rows of X at once, or one
for `limited_minutes`; once X is in fallback, any later confirmed row moves at once. Target = the first of `to` not
itself in fallback (a target in fallback yields to its own entry's target: the chain; `orch lint` refuses a cycle).
(a) each confirmed row: window and process tree killed (`tmuxio.kill`), `orch reset`, `orch assign <row> <target>`;
(b) every pending row (phase none/retry, no open session) whose own executor is X: assigned to the target;
(c) live rows working normally and every parked row are never touched (a Claude executor's limited row too, unless that
executor has its own entry); (d) X is *in fallback* (`state/fallback/X.json`: since, rows moved with their previous
override); `Rig.resolve_exec` sends a row that resolves to X to the target meanwhile. Each act: a STEWARD_LOG line
(`assign`, revert `orch assign <row> X`), the first one opens and immediately closes an incident of kind
`executor_fallback` (outcome `resolved`, no session), one Owner notification (page class `executor_fallback`:
list it in `steward.page_classes` to reach the phone as critical; otherwise a digest line), and the digest gets an
"Executor fallback" section. The mechanism is mechanical and works with the Steward lane off, logged identically (the
notification is then critical, there being no lane to downgrade it).

**Restore.** `orch executor restore X ["why"]` (operator) or `orch sw executor-restore X "why"` (Steward; revert text
`orch executor fallback X`) clears the state and gives the pending rows the fallback moved back their previous
assignment; a row already running, finished, parked or re-assigned since keeps what it has. There is no automatic
restore: the digest names every executor still in fallback. `orch executor status` and
`orch executor fallback X --dry-run` (read-only) show the state and the exact plan.

**Isolation of the drills.** `tests/tmux_isolation.py` gives every suite a private tmux server; `tmuxio` honours
`ORCH_TMUX_SOCKET` (`tmux -L`); `tmuxio.tree`, `tagged`, `killtree` and `_signal_all` never select a tmux process or
pid 1 (a tmux server started from a tagged pane inherits the launch tag).

Drills: `tests/test_v3_fallback.py`, `tests/test_tmux_isolation.py`.
