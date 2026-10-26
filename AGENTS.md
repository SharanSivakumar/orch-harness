# Agent guide — the orchestration framework

This repository is a framework for running a ladder of work across many AI agent sessions, each in a visible tmux
window, with claims checked before dependents start. It is project-agnostic: a project brings its own `orch.yaml`,
rules and plans; this repo never holds project content.

## What were you asked to do?

| your task | read, in full, then follow |
|---|---|
| **set up a new rig** (a new cycle, project or workbench) | [`docs/SETUP_AGENT_GUIDE.md`](docs/SETUP_AGENT_GUIDE.md) — init, ask the Owner one batch of behaviour questions (`orch setup questions`), write the rules, check, card, commit, hand over |
| **plan rows / a round on an existing rig** | the rig's `RIG.md` (from `orch setup card`), its `rules/BASE.md`, then [`docs/PLANNING_AGENT_GUIDE.md`](docs/PLANNING_AGENT_GUIDE.md) |
| **review a round or correct course** | [`docs/COURSE_LOOP.md`](docs/COURSE_LOOP.md) and `templates/course_loop/` |
| **move a v1 rig to v2** | [`docs/MIGRATION.md`](docs/MIGRATION.md) |
| **operate a rig** (start, pause, status) | [`README.md`](README.md) — commands and configuration |
| **execute a row** | nothing here: your work order (`prompts/<id>.<flavor>.md`) binds |
| **change the framework itself** | [`docs/V2_DESIGN.md`](docs/V2_DESIGN.md), then the rules below |

The three roles are separate sessions on purpose: a setup session ends with a configured rig and a `RIG.md`; a fresh
planning session reads that card and plans; the engine runs the rows. Each hand-off is a file, never chat memory.

## Customising per project, never in the framework

| what | where |
|---|---|
| behaviour switches (scheduling, unblocking, review, idle, phone, capacity, resources) | the instance's `orch.yaml` — `orch setup apply`, catalogue in `templates/setup/questions.yaml` |
| a project's defaults for new rigs | `templates/presets/<project>/setup.defaults.yaml` |
| doctrine every work order carries | `templates/presets/<project>/_PART_*.md`, copied into the instance's `prompts/` by `orch init --preset` |
| rig-specific wording | the instance's `prompts/_PART_*.md` / `_TEMPLATE_*.md` overrides |
| standing rules | `<workspace>/rules/BASE.md` |
| one round's rules | `<instance>/rounds/<tag>/RULES.md` |
| rulings | `<workspace>/DECISIONS.md`, ids from `orch gd next --reserve` |

## Rules for changing the framework

- Every change keeps `tests/run_all.sh` green; a new mechanism gets a test. The suites use sandboxes and the `mock`
  executor only — never a real rig, model or network.
- A rig runs only on a committed, tested framework: commit, then run `tests/run_all.sh` on the clean tree (it stamps
  `.tested/<sha>`). `orch start` refuses a dirty tree.
- New behaviour is a configurable mechanism with a safe default, documented in `README.md` (configuration) and, when a
  setup agent should ask about it, in `templates/setup/questions.yaml`. Nothing project-specific in `lib/`, `bin/` or
  the generic `templates/`.
- Never commit secrets, project data, or AI co-author / generated-by trailers.
