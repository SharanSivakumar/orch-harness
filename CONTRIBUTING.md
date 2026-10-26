# Contributing

Thanks for helping make `orch` better. Ideas, bug reports and patches are all welcome.

## Ground rules

- **Mechanism, not doctrine.** The framework carries the machinery (scheduling,
  sentinels, leases, the guard). Project-specific rules belong in a preset or
  in your own `prompts/`, not in `lib/`.
- **No project-specific code in `lib/`.** No names, paths, tickets or domain
  words. If a fix only makes sense for one project, it is a preset.
- **Every behaviour change comes with a test** in `tests/` (the suite that owns
  the mechanism). Never weaken or delete an assertion to get green.
- **Keep it dependency-light:** bash, Python 3 stdlib, tmux, git. Optional TUI
  extras may use `rich`.

## Workflow

1. Open an issue describing the problem or idea (use the templates).
2. Fork, branch from `main`, make a focused change.
3. Run the checks below, then open a pull request that links the issue.

## Checks

```bash
bash -n bin/orch lib/*.sh lib/adapters/*.sh tests/*.sh
python3 -m py_compile lib/*.py
tests/run_all.sh              # every suite; isolated sandboxes, no models, no network
```

`tests/run_all.sh` stamps `.tested/<commit>` on a clean tree; `orch start`
refuses a dirty framework tree (`ORCH_ALLOW_DIRTY=1` overrides on the record).

The mock-cycle test uses its own unique tmux session and temp directory. To
isolate it completely, run it with `TMUX_TMPDIR=$(mktemp -d)`.

## Security

Never put credentials, tokens or personal data in issues, PRs or test
fixtures. Report vulnerabilities privately through GitHub security advisories.
