# Changelog

All notable changes to `orch-harness`. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## 2.0.0 — 2026-10-26

The single-engine release. v1 ran one bash watcher per lane and trusted any
sentinel it saw. v2 replaces that with one Python engine, judged claims, and an
optional autonomy layer. Every v1 queue, sentinel and control file is read as-is;
`docs/MIGRATION.md` is the upgrade path.

### Added

- **One engine per rig** (`lib/engine.py`): dependency-aware scheduling, hot
  reload of `orch.yaml`, a heartbeat and a single-instance lock, per-executor
  and RAM/disk capacity admission, resource slots, priorities and a window
  reaper.
- **Judged claims**: stop lines, `PROGRESS.md`, acceptance probes and commit
  checks run on every close path; a claim must be **accepted** before dependents
  start. Optional independent review on a different executor, with review maps,
  reject handling and plan review (`orch preflight --review`).
- **Rounds, archive and goals**: `orch round …`, a goal metric with an
  instrument-aware scoreboard, a stall rule, lane archive and round reviews.
- **Decisions without bloat**: `orch gd` allocates ruling ids under a lock,
  builds a live digest, and inlines only the rulings a PLAN names.
- **Telemetry**: append-only `state/attempts.jsonl`; `orch report` turns it into
  attempts, redone hours, rejects and no-op rows.
- **Autonomy (all opt-in)**: the Steward lane (incidents, engine-checked actions,
  inbox, revert), `orch digest`, systemd user units with a dead-man timer,
  executor health breakers and the spend governor with the OpenCode meter.
- **Adaptive plans**: phases and caps, course loops, checkpoint judges, replans,
  session-round loops and the bar pin.
- **Guard and capabilities**: convention-derived allowlists, hunk-by-hunk shared
  paths, pair leases, protected paths, the capability/ancestry check and
  engine-mediated queue edits (`orch row add`).
- **Operator surfaces**: `orch monitor` (read-only quick view), a dashboard with
  `current` / `focused` / `full` views, `orch why`, `orch report`, the Telegram
  bot and paged answers.
- **Test suites** for the engine, guard, decisions, setup, pages, isolation and
  the autonomy drills, all on private tmux servers with the `mock` executor.
- **`strict` preset**: a guarded starting point with a never-open clause,
  doctrine placeholders and safe defaults.

### Changed

- `orch start` now brings up one engine; lanes are enabled with
  `control/RUN_<lane>`.
- The queue gained metadata extras (`res`, `review`, `reviewer`, `timeout`,
  `prio`, `accept`, `preflight`, `rearm`, `gate_kind`, `needs`).
- The dashboard's compact view is the default in short terminals; the events
  footer renders a row instead of an empty table.
- Work orders are re-rendered at every dispatch unless hand-edited.

### Removed

- Per-lane bash watchers (`lib/orchestrator_core.sh`).
- The bundled adapter set is trimmed to Claude, Codex, Gemini, OpenCode and
  manual; a project may add its own adapter under `lib/adapters/`.
- Project-specific template presets. The framework ships mechanism only; a
  project keeps its doctrine in `templates/presets/<name>/` or its own
  `prompts/`.

## 1.0.0 — 2026-10-09

Initial public release: multi-lane watchers, visible tmux sessions, the sentinel
contract, the guard allowlist and shared-path staging, leases, the dashboard and
monitor, the Telegram status bot, and the `strict` preset concept.
