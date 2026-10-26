# Setup-agent guide — build a rig, ask the Owner, hand over

**Read this if you were asked to set up a new rig (a new cycle, project or
workbench) on this framework.** You end with a configured, checked, committed
instance and a `RIG.md` card. You do **not** plan rows: a fresh planning
session does that next, from `docs/PLANNING_AGENT_GUIDE.md`.

You change nothing outside the new instance, its workspace's `rules/` folder,
and `DECISIONS.md` if the Owner rules something. You start nothing: no
`orch start`, no `round start`, no paid call.

---

## 0. Gather the facts (ask only what you cannot find)

| fact | where it usually is | example |
|---|---|---|
| project root (every executor's cwd) | the repo or workspace the rows will change | `/home/me/Projects/App` |
| cycle folder (the workspace: plans, reports, `DECISIONS.md`) | a new folder, its own git repo | `Docs/Cycle17` |
| instance folder | `<cycle folder>/ORCH_<NN>` | `Docs/Cycle17/ORCH_17` |
| rig name and tmux session | short, unique on this host | `Project 17`, `app17` |
| preset | a folder in `templates/presets/` whose doctrine fits the project | `strict` |
| Telegram chat id (only if the Owner wants phone pages) | an existing rig's `orch.yaml watchdog.telegram_chat_id` | `"123456789"` |

Check: the cycle folder is a git repo (`git init` it if the Owner agrees), the
tmux session name is not used by another rig (`orch setup check` verifies), and
the framework commit is tested (`ls .tested/$(git rev-parse HEAD)`).

## 1. Create the instance

```bash
<framework>/bin/orch init <cycle folder>/ORCH_<NN> --name "<name>" --session <tmux> --preset <preset>
cd <cycle folder>/ORCH_<NN>
```

This writes `orch.yaml` (with the preset's defaults applied), the `./orch`
wrapper, an empty `queue_A.tsv`, the preset's templates in `prompts/`, a
`rules/BASE.md` skeleton in the workspace, `setup.answers.yaml` and a first
`RIG.md`.

If `project.root` is not three levels up from the instance, fix it now:
`./orch setup apply --set project.root='"../.."'` (paths are relative to the
instance).

## 2. Ask the Owner which behaviours to turn on — one batch

```bash
./orch setup questions          # human-readable, recommendations marked
./orch setup questions --json   # the same, for your question tool
```

Ask every question in **one batch** with your question tool (at most four per
call, so two or three calls). Put the recommended option first and label it
"(Recommended)"; keep the option labels and add the `why` line to the
question. Questions whose recommendation the Owner has already ruled on in
`AGENTS.md`, the preset or an earlier rig may be applied without asking, but
say so in your report.

Then write the answers:

```bash
./orch setup apply --answer executors=opus,sonnet,opencode,gemini \
                   --answer unblocker=opus --answer review=none --answer capacity=large ...
./orch setup apply --set watchdog.telegram_chat_id='"<id>"'          # anything not in the catalogue
./orch setup apply --set guard.resources='["git-lock", {"name": "app-db", "slots": 3}]'
```

`apply` edits `orch.yaml` in place, keeps its comments, re-reads every value it
wrote, and records the answers in `setup.answers.yaml`. Never hand-edit a value
the catalogue covers; for anything else (`project.*`, `guard.*`,
`executors.bin/env/models`, `hooks`, `goal`) `--set dotted.key=<YAML value>`
is the safe route, and a direct edit is fine when `--set` cannot express it.

**Resources** deserve a question of their own: ask what two rows could corrupt
by running at once (a database, a tree measured before/after, a rate-limited
key, a GPU). Each becomes a `guard.resources` entry, with `slots: N` when N rows
may share it and `scope: host` when other rigs on the host use it too.

## 3. Write the standing rules with the Owner

`<workspace>/rules/BASE.md` is a skeleton with `TODO(setup)` slots: paths,
environment and secrets, resources, boundaries, Route A specifics, unattended
decisions, spend. Fill each from what the Owner tells you and what the project
already documents (its `AGENTS.md`, earlier cycles' rules). Ask the Owner the
ones you cannot source, in the same batch style. Keep it to standing rules:
anything specific to one round belongs in `rounds/<tag>/RULES.md`, written by
the planner. Delete the skeleton comment when every slot is filled.

Rig-specific wording that every work order must carry (for example a longer
NEVER-OPEN clause, or a delegation paragraph for unblockers) goes in
`prompts/_PART_<name>.md` or a `prompts/_TEMPLATE_*.md` override, never into
the framework's templates.

If the Owner rules anything a later row must follow, reserve an id
(`./orch gd next --reserve "<title>"`) and write a short entry in
`DECISIONS.md`.

## 4. Check, card, commit

```bash
./orch setup check      # lint, paths, git repo, unique tmux session, executor CLIs, BASE filled, framework tested
./orch setup card       # writes RIG.md from orch.yaml and the answers
./orch commit plan:setup -m "<NN> rig set up: <one line>" -- <instance>/orch <instance>/orch.yaml <instance>/RIG.md <instance>/setup.answers.yaml <instance>/prompts <instance>/queue_A.tsv rules/BASE.md
```

`setup check` must end with 0 errors. "No rows yet" is expected. A warning about
an executor CLI not on PATH is the Owner's to fix (install or log in) or to
accept by removing that executor.

## 5. Hand over and stop

Report to the Owner, short:

- where the rig is, and its tmux session;
- the answers you applied, and any you applied without asking (and why);
- what `rules/BASE.md` now says in five lines, and any slot left open;
- `setup check` result and the commit sha;
- the prompt for the next session, verbatim:

> Read `<instance>/RIG.md` and `<framework>/docs/PLANNING_AGENT_GUIDE.md`,
> then plan round `<tag>` for: <the goal in one sentence>.

You never start the rig. The planner ends at a clean `orch lint` and
`orch preflight`; the Owner (or the planner, when told to) runs
`./orch round start <tag>`.

## Changing a rig later

The same commands work on a running rig: `./orch setup apply …` (the engine
re-reads `orch.yaml` when it changes), then `./orch setup card`, then commit.
`./orch setup questions` shows the catalogue again; `RIG.md` shows what is set.
