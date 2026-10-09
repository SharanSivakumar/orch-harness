# orch-harness

A standalone, repo-agnostic rig for running a ladder of work across many AI
agents in parallel, where each agent is a **visible tmux session you can watch
at any moment** and each step closes itself with a **sentinel file**.

Born in a long-running multi-agent project where the same machinery was
copied and re-debugged into every new work folder. Here it is configuration,
not code: a project supplies an `orch.yaml`, a queue file, and its plans.

---

## What it does

- **Multi-lane DAG scheduling.** Lanes run in parallel; rows within a lane run
  in file order behind their declared dependencies. The queue is re-read on
  every pass, so **appending or editing rows while it runs is safe** and is the
  intended workflow.
- **Runtime executor retiering.** Any pending row can be moved between `opus`,
  `sonnet`, `haiku`, `hermes`, `hermesflash`, `hermespro`, `codex`, `gemini`,
  `opencode`, `manual` and `mock` without editing the queue. The executor is
  resolved at dispatch time. `hermes` and `hermesflash` are two scoped Hermes
  tiers with their own model pins (`executors.models.hermes[_provider]` and
  `executors.models.hermesflash[_provider]`); both share `executors.bin.hermes`.
  `opencode` is a scoped OpenCode TUI pinned by `executors.models.opencode`
  (model id) and `opencode_provider`, launched as `-m <provider>/<model> --auto
  --prompt …`; the TUI submits the prompt itself, so no tmux paste is involved.
  List it in `executors.arg_kick` to launch it on the row's kick text instead of
  the full work order; an executor's own `_KICK_<EXECUTOR>.txt` wins over its
  flavor's `_KICK_<FLAVOR>.txt`. Point `executors.bin.opencode` at a rig-local
  launcher that exports `OPENCODE_CONFIG` for deny rules, `share` and
  `autoupdate`. **A running watcher keeps the `orch.yaml` it started with**:
  after adding an executor, restart the lane watchers (`orch stop` → wait for
  `HALTED (STOP)` → `orch unstop` → `orch start <lanes>`), or an unknown
  executor resolves to `executors.default`.
- **Visible sessions only.** Never headless. `tmux attach` and watch any agent.
- **Sessions are identified, not just named.** A window wearing a row's name is
  not evidence the row is being worked — a *finished* session left open after
  `orch reset` wears the same name. Dispatch stamps the tmux identity it created
  (`state/<id>.window`); a live window that does not match is an **ORPHAN**, and
  the lane waits with its hands off it rather than reporting RUNNING. Close the
  window and the lane dispatches by itself; `orch adopt <id>` claims one you
  know is genuinely working.
- **Hard gates.** A row marked `1` stops the lane until a human runs
  `orch approve <id>`.
- **Self-healing.** A scoped-tier row that returns `BLOCKED` gets an automatic
  unblocker session that diagnoses the stop, fixes the *plan*, and re-runs the
  row. After N attempts it retiers instead of paging; if the retiered row
  blocks again, the lane halts.
- **Concurrency leases + a git staging firewall.** Sessions may overlap; they
  may not overlap *on a resource*, and no session can stage another's files.
- **One state, three surfaces.** A Rich TUI dashboard, a `status` line, and a
  Telegram bot all read through the same resolver, so they cannot disagree.

## What it deliberately does not do

It does not decide anything. It has no opinion about your tiers, your review
gates, your definition of done, or what a row is allowed to spend. All of that
lives in your plans and your prompt templates.

---

## Install

Nothing to build. Requirements: `bash`, `tmux`, `python3`, `pyyaml`, and
`rich` (dashboard only). Optionally put the CLI on your PATH:

```bash
ln -s /path/to/orch-harness/bin/orch ~/.local/bin/orch
```

## Five-minute start

```bash
orch init docs/rig --name "My project" --session myproject
cd docs/rig
$EDITOR orch.yaml queue_A.tsv     # see the guide below
./orch lint                       # validate before dispatching anything
./orch start                      # watchers come up in tmux
./orch dashboard                  # watch it
```

Every instance gets an `./orch` wrapper that pins itself, so agents can call
the CLI from anywhere without knowing where the framework lives.

---

## The three paths, and why there are three

The single most common source of confusion in the old rig was collapsing these:

| Setting | What it is | Example |
|---|---|---|
| `instance` | the folder holding `orch.yaml`, state, control, prompts | `docs/rig` |
| `project.workspace` | what queue plan paths and reports are relative to | `docs` |
| `project.root` | every executor's working directory | `$HOME/projects/app` |
| `guard.git_repo` | the repo the guard stages and commits into | `docs` |

`root` and `git_repo` can genuinely differ: agents work from the project
root, while the plan and report commits land in a separate docs repository.

---

## The queue file

`queue_<LANE>.tsv`, whitespace-separated, `#` comments:

```
id      deps      hard_gate  executor  plan_path                    [extra allowlist]
setup   -         0          opus      PHASE_A/setup/PLAN.md
b0      setup     0          hermes    PHASE_A/b0_census/PLAN.md
r1      b0,l2     1          opus      PHASE_A/r1_review/PLAN.md    PHASE_A/b0_census
```

- **deps** — comma-separated row ids (any lane), or `-`. A dep is met when its
  sentinel says `CLOSED`.
- **hard_gate** — `1` parks the lane before this row until `orch approve <id>`.
- **plan_path** — relative to the workspace. Its *directory* becomes the row's
  allowlist automatically.
- **extra allowlist** (optional 6th column) — extra paths this row may stage,
  for the case where a row legitimately writes into another row's folder.

## The sentinel contract

A row is closed by exactly one file: `state/<id>.status`.

```
CLOSED
one-line summary of what happened
```

Line 1 is `CLOSED`, `BLOCKED`, or `ESCALATED` (the row needs the operator). Anything else halts the
lane. **A session that ends without writing one halts the lane and pages you** —
silence is never treated as success.

## The allowlist, derived

There is no hand-maintained allowlist table any more. For row `id` with plan
`PHASE_A/thing/PLAN.md`:

```
PHASE_A/thing            the row's own phase folder
REPORTS/id_              its report pack prefix
<guard.shared_paths>     what every closing row touches
<queue column 6>         explicit extras
<phase folder>/ALLOWLIST one path per line, if the file exists
```

Check any row's with `orch guard allowlist <id>`.

---

## Commands

```
orch init [dir] [--preset <name>] [--name ...] [--session ...]
orch lint                          validate the ladder before dispatching
orch start [lanes...]              bring watchers up in tmux
orch status [full] | orch json     resolved state
orch dashboard                     adaptive all-lane view (compact in short panes)
orch dashboard focused [--lane L]  detailed lane view; auto-focuses when omitted
orch dashboard full [--lane L]     expanded multi-lane ladders
orch monitor                       one-second read-only quick-peek monitor
orch attach                        attach to the tmux session

orch approve <id>                  release a approval gate
orch assign <id> <executor|default>  retier a pending row
orch assignments [all]             effective executor per row
orch dispatch <id>                 fire one row by hand
orch done <id> "summary"           close a row from the CLI (refused over the row's own stop line)
orch closecheck <id>               the stop-line check `done` runs; for rows that write their own sentinel
orch blocked <id> "reason"         stop a row from the CLI
orch reset <id>                    clear sentinels, counters and the window stamp
orch adopt <id>                    claim an already-open window as the row's session
orch prompt <id>                   regenerate a row's work order

orch day | night                   pause / resume auto-chaining
orch pause [lane] | resume | stop [lane] | unstop

orch hold <id> <res>[,<res>...] -- <cmd>    run holding atomic leases (re-entrant for their owner)
orch lease acquire|release <res> <id>       hold a lease across a span (a parity pair)
orch commit <id> -m "msg" -- <paths>        commit through the firewall (shared paths hunk by hunk)
orch longrun [--venv <dir>] <log> -- <cmd>  setsid job; the job writes its own PID; tail/ps handover
orch venvcheck <venv>                       CLEAR / ARMED / BUSY before using or arming a shared venv
orch guard status|start|finish|allowlist|force-release

orch bot start|stop|status|poll|test|doctor    Telegram status bot
orch notify "Title" "Body"                     send a test push
```

## Close, commit and dispatch guards

- **No CLOSED over a stop line.** `orch done <id>` first runs `lib/close_check.py`:
  when the row folder or its report packs carry a `HEADS_UNCHANGED no`, `STOP` or
  `REFUSED` line (instrument output; line-leading forms in Markdown; falsifier and
  self-test outputs, plan-side files and every `*.local*` path excluded), the close is
  refused unless PROGRESS.md names `SIGNED_RULING <id>` for a RATIFIED/RULED entry in
  DECISIONS.md. Rows that write their own sentinel run `orch closecheck <id>` first.
- **Shared paths hunk by hunk.** For every `guard.shared_paths` entry `orch commit`
  stages only the zero-context hunks whose changed lines name the row id; other rows'
  pending hunks stay unstaged. No hunk of the row's own → refused, index reset.
  `ORCH_SHARED_WHOLE=1` stages a whole shared file on the record.
- **Pair leases.** `orch hold <id> a,b -- cmd` takes several leases in sorted order;
  a lease already held by `<id>` (a span opened with `orch lease acquire`) is passed
  through, never waited on and never released by the inner call.
- **Gated-row probes.** `<instance>/gate_probes.tsv` (`<row id | lane><TAB><command>`,
  row wins) declares a probe the watcher runs from the project root before an agent
  dispatch of a gate=1 row; while it fails the row is not dispatched (one event line,
  a banner, one notification; output in `state/<id>.probe.out`).
- **Watcher liveness is per instance.** A process `orchestrator_core.sh run <L>` counts
  as this instance's watcher only when the `ORCH_INSTANCE` in its environment resolves
  to this instance, so rigs sharing a host and a lane letter never mask each other.

## When a lane stops

Three states look similar from the outside and are not:

| what you see | what it means | what to do |
|---|---|---|
| `RUNNING <id>` | a session **this rig dispatched** holds the row | watch it: `orch attach` |
| `QUIET <id> · no output for N min` | that same session has printed nothing past the stall threshold. It may be thinking, or it may have finished its turn without writing a sentinel | look at the pane. Warned, never killed |
| `ORPHAN WINDOW '<id>'` | a window bears the row's name but this rig did not dispatch it — normally a finished session left open across an `orch reset` | close it (`tmux kill-window -t <session>:<id>`) and the lane dispatches within one poll. Or `orch adopt <id>` if it really is working |

`orch status` is the only authoritative surface. `state/lane_<L>.info` is a
breadcrumb the watcher writes and nothing reads back, so its second line always
says whether a watcher is still behind the first: `lane watcher pid N LIVE` or
`lane watcher DOWN since …`. A halted banner with `DOWN` under it is a frozen
record of a past halt, not a live alarm — and `orch status` appends
`[no watcher — 'orch start']` to the same lane for the same reason.

`orch start` decides what to (re)start from the watcher **process**, never from
the presence of its window, so a window that outlives its watcher can no longer
turn `orch start` into a silent no-op.

## Dashboard views

The default `current` view is adaptive. At normal terminal heights it gives
almost the whole screen to fitted lane ladders, keeping every lane visible and
placing current/actionable rows first. In a genuinely short split it falls back
to one row per lane with progress, state, executor, and the immediate reason it
is waiting. Attention and events share a three-line footer, and live panes take
space only while a session is actually running. `focused` expands one lane;
pass `--lane A` to choose it, or let the dashboard pick a running/attention
lane. `full` always uses expanded ladders. Every view remains read-only.

## Quick monitor

`orch monitor` is the operator's glance view for large rigs. It refreshes once
per second and answers what is running, which five lane-head rows are most
likely to run next, what needs operator action now, what is holding progress,
and where every lane currently stands. Approvals are shown only when their
dependencies are complete; future gates are counted without being presented as
immediate work.

```bash
orch monitor                 # live, refresh every second
orch monitor --once          # render one snapshot and exit
orch monitor --lane A        # restrict to one lane
orch monitor --next 8        # show eight upcoming lane heads
orch monitor --interval 2    # use a slower refresh
orch monitor --no-color      # plain terminal output
```

The monitor uses the same state resolver as `status` and `dashboard`. It has no
control keys and performs no writes, network or database calls, tmux input, or
model calls. Multiple monitor processes can safely observe the same rig.

## Telegram

`push` fires on gates, halts and completions. It is gated three ways: enabled
in `orch.yaml`, `control/WATCHDOG_ARMED` present, and outside quiet hours. It
fails silently by design — a chat outage must never break a lane.

`orch bot start` runs a polling daemon that answers `status` from one
authorized user id with the live snapshot plus the running pane's tail. It is
read-only: it never touches a gate, a sentinel, or a keystroke.

> Only one process may long-poll a given bot token. If a gateway already polls
> the token you use for push, create a **second** bot for polling.

## Tiers and models

A row's **executor** is the program that runs it (`opus`, `opencode`,
`hermes`, ...). Its **flavor** is the work order it receives. The two are
independent, so any model can play any role:

| Flavor | Work order | Behaviour |
|---|---|---|
| `lead` | `_TEMPLATE_LEAD.md` | Full autonomy inside the plan's scope; escalates with `ESCALATED` or blocks with evidence. |
| `scoped` | `_TEMPLATE_SCOPED.md` | Implements exactly the plan; blocks on any decision the plan does not make. An unblocker amends the plan. |
| `codex` | `_TEMPLATE_CODEX.md` | Precise execution in the Codex CLI. |
| `manual` | `_TEMPLATE_MANUAL.md` | A person does the work; the lane waits. |

Defaults: `opus`, `sonnet`, `haiku` are `lead`; `hermes`, `hermesflash`,
`gemini`, `opencode` are `scoped`. Change any of them with `executors.flavors`.

Two ready-made setups live in [`docs/examples/`](docs/examples):

- [`opencode-only.yaml`](docs/examples/opencode-only.yaml): everything runs in
  OpenCode on one strong model, with automatic unblockers. One tool, one API key.
- [`mixed-tiers.yaml`](docs/examples/mixed-tiers.yaml): Claude Code for lead
  rows, cheaper models for scoped rows.

## Templates

Work orders come from `templates/`, instance-first:

```
<instance>/prompts/_TEMPLATE_LEAD.md    used if present
<framework>/templates/_TEMPLATE_LEAD.md fallback
```

The shipped templates carry mechanism only — the sentinel contract, the block
protocol, the mandatory final act. Doctrine belongs to the project.
`templates/presets/strict/` adds a never-open secrets clause, the long-run
helper and the close check; `orch init --preset strict` copies it into an
instance. Write your own preset the same way: a folder of templates.

## Testing

```bash
tests/test_mock_cycle.sh        # 101 assertions, isolated sandbox, no models
KEEP=1 tests/test_mock_cycle.sh # keep the sandbox to inspect
```

It builds a throwaway project with its own git repo and tmux session and runs
a real two-lane ladder through the real engine using the `mock` executor. It
never touches a real instance.

---

## Writing a ladder for it

See [`docs/PLANNING_AGENT_GUIDE.md`](docs/PLANNING_AGENT_GUIDE.md) — the
contract a planning agent must satisfy so its phases slot straight in.
