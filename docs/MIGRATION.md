# Moving a rig from v1 to v2

v2 reads every v1 file as it is: queues, `state/<id>.status`, `control/GO_*`,
`control/EXECUTOR_*`, `control/PAUSE`/`STOP`, and generated prompts. What changes
is who runs the rig: one Python engine instead of one bash watcher per lane, and
a claim must be **accepted** before its dependents start.

Nothing in this file is required for a fresh `orch init`. It is the path for a
rig that already has history.

## What moves, and what waits

| Rig state | What to do |
|---|---|
| a rig with running v1 watchers | stop it first (step 1); v2 refuses to start while one is alive |
| a rig with `state/*.status` but no `*.result.json` | `orch migrate` grandfathers CLOSED rows as accepted; BLOCKED/ROUTE_A rows stay halted until `orch reset` |
| a fully closed lane | archive it with `orch migrate --archive-complete` or `orch archive <lane>` |
| a new project | skip this file; use [`SETUP_AGENT_GUIDE.md`](SETUP_AGENT_GUIDE.md) |

Each instance has an `./orch` wrapper that pins one framework checkout. To move
an instance, point that wrapper at this checkout; to keep an instance on v1,
give it a checkout of the last v1 release and point the wrapper there.

## Steps

1. **Stop v1.**
   ```bash
   ./orch stop
   ./orch status          # wait until no lane has a live watcher
   ```
2. **Point the wrapper at v2.** In `<instance>/orch`, the `exec` line must name
   this checkout's `bin/orch`. Confirm:
   ```bash
   ./orch version
   ```
3. **Dry run the migration.**
   ```bash
   ./orch migrate --archive-complete --retire-stale-controls
   ```
   It lists, and changes nothing: rows by phase, lanes that would be archived,
   controls of accepted rows that would be retired to `control/.retired/`, stale
   leases, and undeclared lock files.
4. **Apply.**
   ```bash
   ./orch migrate --apply --archive-complete --retire-stale-controls
   ./orch unstop
   ```
5. **Add the v2 sections you want.** Start with the defaults in the generated
   `orch.yaml`; `orch lint` validates the result. A useful starting point:
   ```yaml
   scheduler:
     mode: serial          # keep v1 lane order for the first round; switch to dag when plans declare every dep
     max_parallel: 8
     min_free_mb: 3072
   review:
     policy: all           # every claimed row is re-verified by a different model
     reviewer_map: {opencode: sonnet, gemini: sonnet, sonnet: opus, opus: sonnet}
   executors:
     args: {opus: ["--effort", "high"], sonnet: ["--effort", "high"]}
   reaper: {closed_grace_secs: 300}
   idle: {nudge_after: 1200, max_nudges: 2}
   ```
6. **Start only what should run.**
   ```bash
   ./orch start <lanes…>     # or: ./orch round start <tag>
   ```
   A lane with no `control/RUN_<lane>` is never dispatched.

## What to retire after the move

These v1 workarounds duplicate what the engine now does. Remove them once the
rig has run a clean round on v2:

- the cron job that reaped finished tmux windows (the engine's reaper);
- a separate idle-nudge script (built in: `idle.nudge_after`, `max_nudges`);
- wrapper scripts that only add CLI flags or environment (use
  `executors.args` / `executors.env`);
- ad-hoc quota watchers (declare them as
  `watchdog.checks: [{cmd: "<check>", every: 300, title: "quota"}]`).

## First round after migration

Keep `scheduler.mode: serial` for one round unless the v1 plans were written
with complete dependency declarations. Then:

```bash
./orch preflight --round <tag>              # static checks
./orch preflight --round <tag> --run        # optional: execute each Verification block once
./orch round start <tag>
./orch monitor
```

`--run` executes every row's Verification block once. Read them first: a block
can be slow or touch a shared resource.
