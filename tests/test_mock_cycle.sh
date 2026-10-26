#!/usr/bin/env bash
# test_mock_cycle.sh — guard, CLI and adapter checks in an isolated sandbox (v2).
#
# The engine paths (dispatch, chaining, gates, unblock, review, seal, dead windows,
# orphans, capacity, resources) moved to tests/test_v2_engine.py with the v2
# engine; this script keeps the guard, close-check, longrun, preset and adapter
# sections, which run without an engine.
#
# Builds a throwaway project (its own git repo, its own tmux session, its own
# instance directory) under a temp dir, runs a real multi-lane ladder through
# the real engine using the `mock` executor, and asserts on the results.
#
# It proves, with no model, no network and no spend:
#   1. init + lint on a fresh instance
#   2. dispatch -> visible tmux window -> sentinel -> auto-chain
#   3. dependency gating across lanes
#   4. hard gates (GO_) and `orch approve`
#   5. runtime retiering with `orch assign`
#   6. the convention-based guard allowlist: inside passes, outside REFUSED
#   7. compliance-scanner refusal + explicit override
#   8. atomic leases via `orch hold`
#   9. the auto-unblock loop, including max-unblock retiering
#  10. state_resolver's live-process-over-stale-sentinel precedence
#  11. an UNSTAMPED live window is an ORPHAN, never RUNNING
#  12. a lane banner is freeze-marked when its watcher exits
#  13. an orphan window is waited out, never adopted and never yanked
#  14. a claude/agent closing line (`ORCH_CLOSE <id> <VERDICT> — <summary>`)
#      auto-seals a finished session that never wrote its sentinel; free text
#      is never a verdict, and an existing sentinel is never overwritten
#  15. watcher liveness is scoped to THIS instance: another rig's watcher on
#      the same lane letter never counts as ours
#  16. every work order a preset renders carries the never-open clause
#  17. `orch done` refuses a CLOSED over the row's own stop line unless a
#      signed ruling is named
#  18. `orch commit` stages only the committing row's hunks of a shared path
#  19. `orch longrun` (setsid, the job's own PID) and `orch venvcheck`
#  20. pair leases: multi-tree `hold`, re-entrant for the owner
#  21. a gated row's declared pre-dispatch probe holds its dispatch until it passes
#  22. an arg_kick executor (opencode): executor-specific kick, adapter argv
#      (stub binary), per-row model override, CLI close
#
# It NEVER touches any real instance: the sandbox session name is unique per
# run and every path lives under the temp dir.
#
#   tests/test_mock_cycle.sh            # run, clean up on success
#   KEEP=1 tests/test_mock_cycle.sh     # keep the sandbox for inspection
set -u
# every tmux session of this suite lives on a PRIVATE server: its own TMUX_TMPDIR, no inherited $TMUX
# (a shell started inside a tmux pane has it, and tmux then talks to that pane's server), its own -L name
TMUX_PRIV="$(mktemp -d "${TMPDIR:-/tmp}/otm-XXXXXX")"
export TMUX_TMPDIR="$TMUX_PRIV"
unset TMUX ORCH_TMUX_SOCKET

HOME_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ORCH="$HOME_DIR/bin/orch"
SBX="$(mktemp -d "${TMPDIR:-/tmp}/orch-sandbox-XXXXXX")"
SESSION="orchtest$$"
PASS=0; FAIL=0
# no real agent CLI can start from this suite: stubs shadow every model binary
mkdir -p "$SBX/stubs"
for b in claude opencode codex gemini; do
  printf '#!/bin/sh\necho "test stub: $0 refused"; exit 97\n' > "$SBX/stubs/$b"; chmod +x "$SBX/stubs/$b"
done
export PATH="$SBX/stubs:$PATH"

ok(){   PASS=$((PASS+1)); printf '  \033[32mPASS\033[0m  %s\n' "$*"; }
bad(){  FAIL=$((FAIL+1)); printf '  \033[31mFAIL\033[0m  %s\n' "$*"; }
note(){ printf '\n\033[1m== %s\033[0m\n' "$*"; }
have(){ [[ -f "$INST/state/$1.status" ]] && head -1 "$INST/state/$1.status"; }

DECOY=""
cleanup(){
  [[ -n $DECOY ]] && kill "$DECOY" 2>/dev/null
  tmux kill-session -t "$SESSION" 2>/dev/null
  rm -rf "$TMUX_PRIV"
  if [[ ${KEEP:-0} == 1 ]]; then echo; echo "sandbox kept: $SBX"
  else rm -rf "$SBX"; fi
}
trap cleanup EXIT

# --------------------------------------------------------------------------
note "0. build the sandbox project"
mkdir -p "$SBX/project/cycle/PHASE_A"/{a1,a2,a3,a4} \
         "$SBX/project/cycle/PHASE_B"/{b1,b2} "$SBX/project/cycle/REPORTS"
for p in PHASE_A/a1 PHASE_A/a2 PHASE_A/a3 PHASE_A/a4 PHASE_B/b1 PHASE_B/b2; do
  echo "# plan for $p" > "$SBX/project/cycle/$p/PLAN.md"
done
echo "# decisions" > "$SBX/project/cycle/DECISIONS.md"
git -C "$SBX/project/cycle" init -q
git -C "$SBX/project/cycle" config user.email t@t; git -C "$SBX/project/cycle" config user.name t
git -C "$SBX/project/cycle" add -A >/dev/null; git -C "$SBX/project/cycle" commit -qm init
INST="$SBX/project/cycle/ORCHESTRATION"
export ORCH_INSTANCE="$INST"

"$ORCH" init "$INST" --name "sandbox" --session "$SESSION" >/dev/null
[[ -f "$INST/orch.yaml" ]] && ok "orch init created orch.yaml" || bad "orch init did not create orch.yaml"

python3 - "$INST/orch.yaml" "$SBX" <<'PY'
import sys, pathlib, re
f = pathlib.Path(sys.argv[1]); t = f.read_text()
t = t.replace('root: "../../.."', 'root: ".."')          # project root = the cycle folder
t = t.replace('default: "opus"', 'default: "mock"')
# rows run on a junior mock; unblockers and the retier target are a hard mock (a junior never decides)
t = t.replace('retier_on_max_unblock: "opus"', 'retier_on_max_unblock: "mockhard"')
t = t.replace('unblock_executor: "sonnet"', 'unblock_executor: "mockhard"')
t = t.replace('models: {}', 'models: {}\n  flavors: {mock: jrdev, mockhard: hard}\n  adapters: {mockhard: mock}')
t = re.sub(r'allowed: \[([^\]]*)\]', lambda m: f'allowed: [{m.group(1)}, mockhard]', t, count=1)
t = t.replace('poll_interval: 60', 'poll_interval: 2')
t = t.replace('compliance_scanners: []', 'compliance_scanners: ["scan.py"]')
t = t.replace('    - "git-lock"\n  compliance', '    - "git-lock"\n    - "tree-a"\n    - "tree-b"\n  compliance')
f.write_text(t)
PY

# a deliberately trivial scanner: refuse any staged blob containing SECRET
cat > "$INST/scan.py" <<'PY'
import sys
bad = 0
for p in sys.argv[2:]:
    try:
        if "SECRET" in open(p, errors="replace").read():
            print(f"scanner: forbidden marker in {p}"); bad = 1
    except Exception:
        pass
sys.exit(1 if bad else 0)
PY

printf '%s\t%s\t%s\t%s\t%s\n' \
  a1 - 0 mock PHASE_A/a1/PLAN.md \
  a2 a1 0 mock PHASE_A/a2/PLAN.md \
  a3 a2 1 mock PHASE_A/a3/PLAN.md \
  a4 a3 0 mock PHASE_A/a4/PLAN.md > "$INST/queue_A.tsv"
printf '%s\t%s\t%s\t%s\t%s\t%s\n' \
  b1 a1 0 mock PHASE_B/b1/PLAN.md PHASE_A/a1 > "$INST/queue_B.tsv"
printf '%s\t%s\t%s\t%s\t%s\n' b2 b1 0 mock PHASE_B/b2/PLAN.md >> "$INST/queue_B.tsv"
rm -f "$INST/queue_A.tsv.bak"

note "1. lint the fresh ladder"
if "$ORCH" lint > "$SBX/lint.out" 2>&1; then ok "lint clean on a valid ladder"
else bad "lint failed: $(tail -3 "$SBX/lint.out")"; fi

printf '%s\t%s\t%s\t%s\t%s\n' bad1 nosuch 0 mock PHASE_A/a1/PLAN.md >> "$INST/queue_A.tsv"
"$ORCH" lint 2>&1 | grep -q "depends on 'nosuch'" && ok "lint catches a dangling dependency" \
  || bad "lint missed a dangling dependency"
sed -i '/^bad1/d' "$INST/queue_A.tsv"

note "2. allowlist is derived by convention"
AL=$("$ORCH" guard allowlist a1)
grep -qx "PHASE_A/a1" <<< "$AL" && ok "allowlist contains the row's own phase folder" \
  || bad "allowlist missing phase folder: $AL"
grep -qx "REPORTS/a1_" <<< "$AL" && ok "allowlist contains the report-pack prefix" \
  || bad "allowlist missing report prefix: $AL"
grep -qx "DECISIONS.md" <<< "$AL" && ok "allowlist contains guard.shared_paths" \
  || bad "allowlist missing shared paths: $AL"
grep -qx "PHASE_A/a1" <<< "$("$ORCH" guard allowlist b1)" \
  && ok "queue column 6 grants a cross-folder path" || bad "6th-column allowlist ignored"

note "3. the guard refuses what is not on the allowlist"
echo "hello" > "$SBX/project/cycle/PHASE_A/a1/out.txt"
echo "hello" > "$SBX/project/cycle/PHASE_A/a2/out.txt"
"$ORCH" commit a1 -m "inside" -- PHASE_A/a1/out.txt >/dev/null 2>&1 \
  && ok "commit inside the allowlist succeeds" || bad "commit inside the allowlist was refused"
if "$ORCH" commit a1 -m "outside" -- PHASE_A/a2/out.txt >"$SBX/refuse.out" 2>&1; then
  bad "commit OUTSIDE the allowlist was allowed"
else
  grep -q "outside a1's allowlist" "$SBX/refuse.out" && ok "commit outside the allowlist is REFUSED" \
    || bad "refused, but not for the allowlist reason"
fi
git -C "$SBX/project/cycle" reset -q

note "4. compliance scanner refuses content, override lets it through"
echo "this line has a SECRET in it" > "$SBX/project/cycle/PHASE_A/a1/leak.txt"
if "$ORCH" commit a1 -m "leak" -- PHASE_A/a1/leak.txt >"$SBX/scan.out" 2>&1; then
  bad "scanner did not refuse the staged content"
else
  grep -q "forbidden marker" "$SBX/scan.out" && ok "compliance scanner refused the commit" \
    || bad "refused, but the scanner did not report: $(tail -2 "$SBX/scan.out")"
fi
ORCH_SCANNER_OVERRIDE=1 "$ORCH" commit a1 -m "leak, on the record" -- PHASE_A/a1/leak.txt >/dev/null 2>&1 \
  && ok "ORCH_SCANNER_OVERRIDE=1 commits it explicitly" || bad "override did not work"

note "4b. a shared path is staged hunk by hunk"
DEC="$SBX/project/cycle/DECISIONS.md"
printf '# decisions\n\n## one\n\n## two\n' > "$DEC"
git -C "$SBX/project/cycle" add DECISIONS.md && git -C "$SBX/project/cycle" commit -qm "decisions skeleton"
sed -i '2a b1: a half-written entry of another row' "$DEC"
printf '\n### DEC-1 — the entry of a1\n' >> "$DEC"
"$ORCH" commit a1 -m "a1 decision" -- DECISIONS.md > "$SBX/hunk1.out" 2>&1 \
  && ok "commit of a shared path by a1 succeeds" || bad "a1's shared-path commit failed: $(tail -2 "$SBX/hunk1.out")"
git -C "$SBX/project/cycle" show HEAD:DECISIONS.md | grep -q "the entry of a1" \
  && ! git -C "$SBX/project/cycle" show HEAD:DECISIONS.md | grep -q "half-written" \
  && ok "only a1's hunk went into a1's commit" || bad "a1's commit carried another row's hunk (or lost its own)"
git -C "$SBX/project/cycle" diff -- DECISIONS.md | grep -q "^+b1: a half-written" \
  && ok "the other row's hunk stays in the working tree, unstaged" || bad "the other row's pending hunk was lost"
if "$ORCH" commit a2 -m "nothing of mine" -- DECISIONS.md > "$SBX/hunk2.out" 2>&1; then
  bad "a commit of a shared path with no hunk naming the row went through"
else
  grep -q "no hunk names 'a2'" "$SBX/hunk2.out" && [[ -z $(git -C "$SBX/project/cycle" diff --cached --name-only) ]] \
    && ok "a row with no hunk of its own in a shared path is REFUSED, index clean" \
    || bad "refused, but not for the hunk reason or the index was left dirty"
fi
"$ORCH" commit b1 -m "b1 decision" -- DECISIONS.md >/dev/null 2>&1 \
  && git -C "$SBX/project/cycle" show HEAD:DECISIONS.md | grep -q "half-written" \
  && ok "the owning row commits its own hunk afterwards" || bad "b1 could not commit its own hunk"

note "5. leases are atomic and always released"
"$ORCH" hold a1 git-lock -- true >/dev/null 2>&1 && ok "hold acquires, runs, releases" || bad "hold failed"
[[ -d "$INST/leases/git-lock.lock" ]] && bad "lease left behind after hold" || ok "no lease left behind"
"$ORCH" hold a1 git-lock -- false >/dev/null 2>&1
[[ -d "$INST/leases/git-lock.lock" ]] && bad "lease left behind after a FAILING command" \
  || ok "lease released even when the held command fails"

note "5b. pair leases: several trees at once, re-entrant for their owner"
"$ORCH" hold a1 tree-a,tree-b -- bash -c "[[ -d '$INST/leases/tree-a.lock' && -d '$INST/leases/tree-b.lock' ]]" >/dev/null 2>&1 \
  && ok "one hold takes several tree leases at once" || bad "a multi-resource hold did not hold every tree"
[[ ! -d $INST/leases/tree-a.lock && ! -d $INST/leases/tree-b.lock ]] \
  && ok "and releases every one of them" || bad "a multi-resource hold left a lease behind"
"$ORCH" lease acquire tree-a a1 >/dev/null 2>&1
timeout 5 "$ORCH" hold a1 tree-a -- true >/dev/null 2>&1 && [[ -d $INST/leases/tree-a.lock ]] \
  && ok "the owner's own hold inside its pair span passes through and leaves the span's lease held" \
  || bad "the owner deadlocked on, or released, its own span lease"
timeout 5 "$ORCH" lease acquire tree-a a1 2>&1 | grep -q "already held by a1" \
  && ok "lease acquire is re-entrant for its owner" || bad "lease acquire waited on its own owner"
timeout 5 "$ORCH" hold b1 tree-a -- true >/dev/null 2>&1; rc=$?
(( rc == 124 )) && ok "another row waits while a tree is held across a pair span" || bad "b1 was not held off the span's tree (rc=$rc)"
"$ORCH" lease release tree-a a1 >/dev/null 2>&1
[[ ! -d $INST/leases/tree-a.lock ]] && ok "the span's release frees the tree" || bad "the span's lease was not released"

note "12. the dashboard renders read-only"
if COLUMNS=200 python3 "$HOME_DIR/lib/dashboard_tui.py" --once >"$SBX/dash.out" 2>&1; then
  grep -q "sandbox" "$SBX/dash.out" && ok "dashboard renders a frame" || bad "dashboard frame missing the instance name"
else bad "dashboard failed: $(tail -3 "$SBX/dash.out")"; fi

note "12b. the quick monitor renders read-only"
if COLUMNS=160 python3 "$HOME_DIR/lib/monitor_tui.py" --once --no-color >"$SBX/monitor.out" 2>&1; then
  grep -q "NEXT" "$SBX/monitor.out" && grep -q "NEEDS YOU" "$SBX/monitor.out" \
    && ok "monitor renders running/next/action sections" || bad "monitor frame missing a core section"
else bad "monitor failed: $(tail -3 "$SBX/monitor.out")"; fi

note "13. the telegram bot composes a snapshot without a token"
if python3 "$HOME_DIR/lib/telegram_bot.py" test >"$SBX/bot.out" 2>&1; then
  grep -q "Lane A" "$SBX/bot.out" && ok "bot composes a live snapshot" || bad "bot snapshot missing lane state"
else bad "bot test failed: $(tail -3 "$SBX/bot.out")"; fi
python3 "$HOME_DIR/lib/telegram_bot.py" push --title t --body b >/dev/null 2>&1 \
  && ok "push is silent and harmless when unconfigured" || bad "push errored when unconfigured"

note "15. every generated charter carries the never-open clause"
# A preset instance renders its own work orders; the clause must reach every
# flavour a session can be dispatched with, and the unblocker's.
PI="$SBX/preset/cycle/ORCH"
mkdir -p "$SBX/preset/cycle/PHASE_P/p1"; echo "# plan for p1" > "$SBX/preset/cycle/PHASE_P/p1/PLAN.md"
ORCH_INSTANCE="$PI" "$ORCH" init "$PI" --preset strict --name preset --session "${SESSION}p" >/dev/null 2>&1
printf '%s\t%s\t%s\t%s\t%s\n' p1 - 0 opus PHASE_P/p1/PLAN.md > "$PI/queue_A.tsv"
for x in opus gemini codex; do
  ORCH_INSTANCE="$PI" python3 "$HOME_DIR/lib/template_engine.py" prompt --id p1 --lane A --exec "$x" \
    --plan PHASE_P/p1/PLAN.md --force >/dev/null 2>&1
done
ORCH_INSTANCE="$PI" python3 "$HOME_DIR/lib/template_engine.py" unblock --id p1 --lane A --n 1 --exec gemini \
  --plan PHASE_P/p1/PLAN.md --reason test >/dev/null 2>&1
for f in p1.hard.md p1.jrdev.md p1.codex.md $(cd "$PI/prompts" && ls ub-p1-1.*.md); do
  grep -q "NEVER-OPEN (binding, verbatim)" "$PI/prompts/$f" 2>/dev/null \
    && grep -q -- "--exclude='\*.local\*'" "$PI/prompts/$f" \
    && ok "work order $f carries the verbatim never-open clause" \
    || bad "work order $f lacks the never-open clause"
done
ORCH_INSTANCE="$PI" "$ORCH" prompt p1 2>/dev/null | grep -q "p1.hard.md" \
  && grep -q "NEVER-OPEN (binding, verbatim)" "$PI/prompts/p1.hard.md" \
  && ok "orch prompt regenerates an opus row's work order with the clause" \
  || bad "orch prompt did not produce the clause"

note "16. orch done refuses a CLOSED over the row's own stop line"
D1="$SBX/project/cycle/PHASE_D/d1"; mkdir -p "$D1/artifacts"
echo "# plan for d1 — STOP if the heads move" > "$D1/PLAN.md"
printf '%s\t%s\t%s\t%s\t%s\n' d1 - 0 manual PHASE_D/d1/PLAN.md > "$INST/queue_D.tsv"
printf 'duty4_pre: HEADS_UNCHANGED yes\nclose: HEADS_UNCHANGED no\n' > "$D1/artifacts/heads_checks.out"
printf 'VERDICT STOP\n' > "$D1/artifacts/verify_falsify.out"      # a falsifier's STOP is its pass
printf 'STOP never read\n' > "$D1/artifacts/notes.local.txt"       # the never-open class is not opened
printf 'PASS  orch done refused a CLOSED over a HEADS_UNCHANGED no line\n' > "$D1/artifacts/test_log.out"  # a quotation, not a verdict
printf '# PROGRESS d1\nThe PLAN says STOP if the heads move; that is prose, not a stop line.\n' > "$D1/PROGRESS.md"
"$ORCH" done d1 "closing over a moved head" > "$SBX/cc1.out" 2>&1
[[ -z "$(have d1)" ]] && grep -q "1 stop line(s)" "$SBX/cc1.out" && grep -q "^REFUSED" "$SBX/cc1.out" \
  && ok "orch done refused a CLOSED over a HEADS_UNCHANGED no line (falsifier, *.local*, prose and quotations not counted)" \
  || bad "orch done did not refuse the close: $(head -3 "$SBX/cc1.out")"
printf '\n### DEC-9 — PROPOSED (d1)\nnot a signature\n' >> "$SBX/project/cycle/DECISIONS.md"
echo 'SIGNED_RULING DEC-9' >> "$D1/PROGRESS.md"
"$ORCH" done d1 "closing on a proposal" > "$SBX/cc2.out" 2>&1
[[ -z "$(have d1)" ]] && grep -q "none is a signed" "$SBX/cc2.out" \
  && ok "a PROPOSED entry is not a signed ruling (still refused)" || bad "a PROPOSED entry let the close through"
printf '\n### DEC-8 — RATIFIED (the Owner)\nthe moved head is accepted for d1\n' >> "$SBX/project/cycle/DECISIONS.md"
echo 'SIGNED_RULING DEC-8' >> "$D1/PROGRESS.md"
"$ORCH" done d1 "closed on a signed ruling" > "$SBX/cc3.out" 2>&1
[[ $(have d1) == CLOSED ]] && grep -q "signed ruling DEC-8" "$SBX/cc3.out" \
  && ok "a signed (RATIFIED) ruling named in PROGRESS lets the close through" \
  || bad "the signed ruling did not let the close through: $(head -3 "$SBX/cc3.out")"
"$ORCH" closecheck d1 >/dev/null 2>&1 && ok "orch closecheck exposes the same check (exit 0 once signed)" \
  || bad "orch closecheck failed on a signed row"

note "17. long jobs: setsid, the job's own PID, ps -p liveness; the shared-venv check"
LR="$SBX/lr"; mkdir -p "$LR"
"$ORCH" longrun "$LR/job.log" -- bash -c 'echo started; sleep 4; echo finished' > "$SBX/lr.out" 2>&1
LP=$(head -1 "$LR/job.pid" 2>/dev/null)
[[ -n $LP ]] && grep -q "pid=$LP " "$SBX/lr.out" && ps -p "$LP" -o args= | grep -q "sleep 4" \
  && ok "the recorded PID is the job itself, not a wrapper" || bad "longrun recorded the wrong PID: $(head -2 "$SBX/lr.out")"
[[ $(ps -o sid= -p "$LP" 2>/dev/null | tr -d ' ') == "$LP" ]] \
  && ok "the job leads its own session (setsid)" || bad "the job is not a session leader"
grep -q "tail -f $LR/job.log" "$SBX/lr.out" && grep -q "ps -p $LP" "$SBX/lr.out" \
  && ok "longrun prints the tail -f and ps -p handover" || bad "handover lines missing"
"$ORCH" longrun "$LR/job.log" -- true > "$SBX/lr2.out" 2>&1 \
  && bad "a second launch over an existing PID file went through" \
  || { grep -q "^REFUSED" "$SBX/lr2.out" && ok "a step is never re-launched over its PID file" || bad "refused without the reason"; }
for i in $(seq 1 10); do ps -p "$LP" >/dev/null 2>&1 || break; sleep 1; done
! ps -p "$LP" >/dev/null 2>&1 && grep -q finished "$LR/job.log" \
  && ok "ps -p liveness ends when the job ends, and the log holds its output" || bad "the job outlived its liveness check"
V="$SBX/venv"; mkdir -p "$V/bin"
"$ORCH" venvcheck "$V" 2>/dev/null | grep -q "VERDICT CLEAR" && ok "an idle venv reads CLEAR" || bad "an idle venv did not read CLEAR"
touch "$V/guard.active"
"$ORCH" venvcheck "$V" > "$SBX/vc.out" 2>&1; rc=$?
(( rc == 3 )) && grep -q "VERDICT ARMED" "$SBX/vc.out" && ok "an armed venv reads ARMED (exit 3)" || bad "an armed venv read: $(cat "$SBX/vc.out")"
"$ORCH" longrun --venv "$V" "$LR/armed.log" -- true >/dev/null 2>&1
[[ ! -e $LR/armed.pid ]] && ok "longrun --venv refuses to start from an armed venv" || bad "longrun started from an armed venv"
rm -f "$V/guard.active"
printf '#!/usr/bin/env bash\nsleep 6\n' > "$V/bin/tool"; chmod +x "$V/bin/tool"
"$V/bin/tool" & TP=$!; sleep 1
"$ORCH" venvcheck "$V" > "$SBX/vc2.out" 2>&1; rc=$?
(( rc == 4 )) && grep -q "VERDICT BUSY" "$SBX/vc2.out" && grep -q "$TP" "$SBX/vc2.out" \
  && ok "a venv a foreign process lives in reads BUSY (never arm it)" || bad "a busy venv read: $(cat "$SBX/vc2.out")"
kill "$TP" 2>/dev/null; wait "$TP" 2>/dev/null

note "18. an arg_kick executor (opencode): its own kick template, the adapter's argv, the CLI close"
python3 - "$INST/orch.yaml" <<'PY'
import sys, pathlib, re
f = pathlib.Path(sys.argv[1]); t = f.read_text()
t = re.sub(r'allowed: \[([^\]]*)\]', lambda m: f'allowed: [{m.group(1)}, opencode]', t, count=1)
t = t.replace('  dead_window_policy: "retier"', '  dead_window_policy: "retier"\n  arg_kick: [opencode]')
f.write_text(t)
PY
mkdir -p "$SBX/project/cycle/PHASE_O/o1"; echo "# plan for o1" > "$SBX/project/cycle/PHASE_O/o1/PLAN.md"
printf '%s\t%s\t%s\t%s\t%s\n' o1 - 0 opencode PHASE_O/o1/PLAN.md > "$INST/queue_O.tsv"
"$ORCH" lint > "$SBX/lint_o.out" 2>&1 && ok "lint accepts an opencode row (adapter + kick resolve)" \
  || bad "lint refused the opencode row: $(tail -3 "$SBX/lint_o.out")"
"$ORCH" prompt o1 >/dev/null 2>&1
[[ -s $INST/prompts/o1.jrdev.md ]] && grep -q "fresh executor" "$INST/prompts/o1.kick.txt" 2>/dev/null \
  && ok "an arg_kick executor gets its kick written (flavor fallback _KICK_JRDEV.txt)" \
  || bad "no kick written for the arg_kick executor"
echo "OPENCODE KICK __ID__ -> __WORKORDER__" > "$INST/prompts/_KICK_OPENCODE.txt"
"$ORCH" prompt o1 >/dev/null 2>&1
grep -qx "OPENCODE KICK o1 -> .*prompts/o1.jrdev.md" "$INST/prompts/o1.kick.txt" \
  && ok "the executor's own _KICK_OPENCODE.txt wins over the flavor's kick, and is rewritten" \
  || bad "the executor-specific kick was not used: $(head -1 "$INST/prompts/o1.kick.txt")"
STUB="$SBX/opencode-stub"; printf '#!/usr/bin/env bash\nprintf "%%s\\n" "$@" > "%s"\n' "$SBX/oc_argv" > "$STUB"; chmod +x "$STUB"
echo "other/m-2" > "$INST/control/OPENCODE_MODEL_o1"
( cd "$SBX/project/cycle" && ORCH_BIN_OPENCODE="$STUB" ORCH_ARG_KICK=opencode ORCH_MODEL_OPENCODE=m-1 \
    ORCH_MODEL_OPENCODE_PROVIDER=prov "$HOME_DIR/lib/adapters/opencode.sh" o1 opencode "$INST/prompts/o1.jrdev.md" )
[[ $(sed -n 1,2p "$SBX/oc_argv" | tr '\n' ' ') == "--auto --prompt " ]] && ! grep -qx -- "-m" "$SBX/oc_argv" \
  && grep -q "^OPENCODE KICK o1" "$SBX/oc_argv" \
  && ok "the adapter launches --auto --prompt <kick> and never picks a model (pins and overrides ignored)" \
  || bad "adapter argv: $(tr '\n' ' ' < "$SBX/oc_argv")"
( cd "$SBX/project/cycle" && ORCH_BIN_OPENCODE="$STUB" ORCH_ARG_KICK="" \
    "$HOME_DIR/lib/adapters/opencode.sh" o1 opencode "$INST/prompts/o1.jrdev.md" )
grep -q "JUNIOR tier" "$SBX/oc_argv" && ! grep -q "OPENCODE KICK" "$SBX/oc_argv" \
  && ok "outside arg_kick the full work order is the prompt" \
  || bad "work-order argv: $(tr '\n' ' ' < "$SBX/oc_argv" | cut -c1-200)"
rm -f "$INST/control/OPENCODE_MODEL_o1"
echo "# PROGRESS o1" > "$SBX/project/cycle/PHASE_O/o1/PROGRESS.md"
"$ORCH" done o1 "opencode row closed by the CLI" > "$SBX/o1done.out" 2>&1
[[ $(have o1) == CLOSED ]] && ok "orch done closes an opencode row (it does not write its own sentinel)" \
  || bad "orch done refused the opencode row: $(head -2 "$SBX/o1done.out")"

printf '\n\033[1m== result ==\033[0m\n'
printf '  %d passed, %d failed\n' "$PASS" "$FAIL"
[[ $FAIL -eq 0 ]] || exit 1
