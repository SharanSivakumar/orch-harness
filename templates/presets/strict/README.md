# strict preset

    orch init my-rig --preset strict

A guarded starting point for a project whose rows must never invent facts,
weaken a check, or leak private material. `orch init` copies the `_*` files and
this README into the instance's `prompts/`, where they override the framework's
generic templates, and applies `setup.defaults.yaml` to the new `orch.yaml`.

The preset is deliberately thin. It carries **mechanism** (the never-open
clause, the block protocol, the close checks, the work-order shape) and a
doctrine skeleton you fill in for your project. Project-specific rules belong in
the workspace, never in these files.

## Layout

- `_PART_doctrine.md` — the permanent doctrine: North Star placeholder, the
  never-list, Route A / Route B, long jobs. Every work order includes it with
  `{{> doctrine}}`. **Fill in the North Star before the first run.**
- `_PART_never_open.md` — the verbatim never-open clause every subagent charter
  carries.
- `_TEMPLATE_*.md` / `_KICK_*.txt` — thin overrides that include the parts and
  the framework's shared `read_first`, `honesty` and `close` parts.
- `setup.defaults.yaml` — the standing preferences applied to `orch.yaml`
  (documented in the file; edit freely).

## What the project supplies — and where

- `<workspace>/rules/BASE.md` — the project's standing rules (paths, secrets,
  resources, the wall). Every work order points at it.
- `<instance>/rounds/<tag>/RULES.md` — a round's delta (`orch round new <tag>`).
- `<workspace>/DECISIONS.md` — the ruling ledger. Rows read only the rulings
  their PLAN lists on a `Rulings:` line and look others up with
  `orch gd show <id>`. Ids come from `orch gd next --reserve`.
- `guard.compliance_scanners` — project code that refuses a commit containing
  material the project treats as private. The framework ships no scanner.
