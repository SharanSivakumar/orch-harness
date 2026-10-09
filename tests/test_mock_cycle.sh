#!/usr/bin/env bash
# test_mock_cycle.sh — end-to-end integration test in an isolated sandbox.
#
# Builds a throwaway project (its own git repo, its own tmux session, its own
# instance directory) under a temp dir, runs a real multi-lane ladder through
# the real engine using the `mock` executor, and asserts on the results.
#
# It proves, with no model, no network and no spend:
#   1. init + lint on a fresh instance
#   2. dispatch -> visible tmux window -> sentinel -> auto-chain
#   3. dependency gating across lanes
#   4. approval gates (GO_) and `orch approve`
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

HOME_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ORCH="$HOME_DIR/bin/orch"
SBX="$(mktemp -d "${TMPDIR:-/tmp}/orch-sandbox-XXXXXX")"
SESSION="orchtest$$"
PASS=0; FAIL=0

ok(){   PASS=$((PASS+1)); printf '  \033[32mPASS\033[0m  %s\n' "$*"; }
bad(){  FAIL=$((FAIL+1)); printf '  \033[31mFAIL\033[0m  %s\n' "$*"; }
note(){ printf '\n\033[1m== %s\033[0m\n' "$*"; }
have(){ [[ -f "$INST/state/$1.status" ]] && head -1 "$INST/state/$1.status"; }

DECOY=""
cleanup(){
  [[ -n $DECOY ]] && kill "$DECOY" 2>/dev/null
  tmux kill-session -t "$SESSION" 2>/dev/null
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
t = t.replace('retier_on_max_unblock: "opus"', 'retier_on_max_unblock: "mock"')
t = t.replace('unblock_executor: "hermes"', 'unblock_executor: "mock"')
t = t.replace('models: {}', 'models: {}\n  flavors: {mock: scoped}')
t = t.replace('paste_kick: [hermes, hermespro]', 'paste_kick: []')
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

note "4b. a shared path is staged hunk by hunk "
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

note "6. run both lanes end to end"
printf 'CLOSED\na1 done\n' > "$INST/control/MOCK_a1"
printf 'CLOSED\na2 done\n' > "$INST/control/MOCK_a2"
printf 'CLOSED\na3 done\n' > "$INST/control/MOCK_a3"
printf 'BLOCKED\na4 needs a decision the plan does not make\n' > "$INST/control/MOCK_a4"
printf 'CLOSED\nb1 done\n' > "$INST/control/MOCK_b1"
printf 'CLOSED\nb2 done\n' > "$INST/control/MOCK_b2"
for n in 1 2 3; do printf 'RESOLVED\nplan amended (attempt %s)\n' "$n" > "$INST/control/MOCK_ub-a4-$n"; done
# A FOREIGN instance's watcher on the same lane letter. Lane letters
# repeat across rigs, and a live rig on this host runs its own `watch-A`; a bare
# process-name match read it as this sandbox's watcher, so `orch start` skipped
# lane A and nothing ran. The decoy makes that collision deterministic here.
mkdir -p "$SBX/foreign/lib" "$SBX/foreign/inst"
printf '%s\n' '#!/usr/bin/env bash' 'trap '"'"'kill $c 2>/dev/null; exit 0'"'"' TERM' 'sleep 900 & c=$!; wait' \
  > "$SBX/foreign/lib/orchestrator_core.sh"
ORCH_INSTANCE="$SBX/foreign/inst" bash "$SBX/foreign/lib/orchestrator_core.sh" run A </dev/null >/dev/null 2>&1 &
DECOY=$!
sleep 1
"$ORCH" start >"$SBX/start.out" 2>&1
grep -q "started watcher watch-A" "$SBX/start.out" \
  && ok "a foreign instance's watcher on the same lane letter is not ours (lane A started)" \
  || bad "orch start took a foreign instance's watcher for its own: $(grep watch-A "$SBX/start.out")"

for i in $(seq 1 40); do [[ $(have a1) == CLOSED ]] && break; sleep 1; done
[[ $(have a1) == CLOSED ]] && ok "a1 dispatched and closed itself" || bad "a1 never closed"
for i in $(seq 1 40); do [[ $(have a2) == CLOSED ]] && break; sleep 1; done
[[ $(have a2) == CLOSED ]] && ok "a2 auto-chained after its dependency" || bad "a2 did not chain"
for i in $(seq 1 40); do [[ $(have b2) == CLOSED ]] && break; sleep 1; done
[[ $(have b2) == CLOSED ]] && ok "lane B ran in parallel and completed" || bad "lane B did not complete"

note "7. the approval gate holds until it is approved"
sleep 4
[[ -z "$(have a3)" ]] && ok "a3 is held at its approval gate (no sentinel)" || bad "a3 ran through its gate"
"$ORCH" status | grep -qE "GATE HOLD before a3|GATED" && ok "status names the gate" || bad "status does not show the gate"
# The gated row's declared pre-dispatch probe: approved, but its
# prerequisite is absent, so the watcher must not dispatch it — and must say so.
printf 'a3\ttest -f %s\n' "$SBX/probe_ok" > "$INST/gate_probes.tsv"
"$ORCH" approve a3 >/dev/null
sleep 6
[[ -z "$(have a3)" ]] && ! tmux list-windows -t "$SESSION" -F '#{window_name}' | grep -qx a3 \
  && ok "an approved gated row whose probe fails is NOT dispatched" || bad "a3 was dispatched although its probe failed"
grep -q "a3 pre-dispatch probe FAILED" "$INST/state/events_A.log" && grep -q "PROBE FAILED before a3" "$INST/state/lane_A.info" \
  && ok "the probe failure is an event line and the lane banner" || bad "the probe failure was not reported"
"$ORCH" dispatch a3 2>&1 | grep -q "pre-dispatch probe failed" \
  && ok "orch dispatch refuses a gated row whose probe fails" || bad "orch dispatch ignored the failing probe"
touch "$SBX/probe_ok"
for i in $(seq 1 40); do [[ $(have a3) == CLOSED ]] && break; sleep 1; done
[[ $(have a3) == CLOSED ]] && ok "orch approve released the gate and a3 ran" || bad "a3 did not run after approve"
grep -q "a3 pre-dispatch probe PASSES again" "$INST/state/events_A.log" \
  && ok "the lane dispatched by itself once the probe passed" || bad "no event line for the passing probe"

note "7b. closing-line auto-seal ('said Closed then sat at the prompt' fix)"
. "$HOME_DIR/lib/closing.sh"
export ORCH_TMUX="$SESSION"
# parser: verdict + summary extracted from the exact token line (em-dash separator)
V=$(closing_verdict "ORCH_CLOSE c1 CLOSED — 240 rows stop printing a zero")
[[ $V == CLOSED ]] && ok "closing_verdict parses CLOSED" || bad "verdict parse: got '$V'"
S=$(closing_summary "ORCH_CLOSE c1 CLOSED — 240 rows stop printing a zero" CLOSED)
[[ $S == "240 rows stop printing a zero" ]] && ok "closing_summary strips the token" || bad "summary parse: got '$S'"
[[ -z $(closing_verdict "Closed. all done!") ]] && ok "free text is never read as a verdict" || bad "free text was read as a verdict"
# regression: the work-order template's step-5 line is instruction
# text, not a verdict. Its code span starts with a backtick and is quoted mid-line,
# so it must never seal a row -- even when the prompt is still on the visible pane.
TPL="\`ORCH_CLOSE c1 CLOSED — <the same one-sentence summary>\` (or \`BLOCKED\`/\`ESCALATED\`"
[[ -z $(closing_line c1 "$TPL") ]] \
  && ok "closing_line ignores template/instruction text (backtick-prefixed)" \
  || bad "closing_line parsed template text as a closing line"
# regression: the TUI rendered the same template line WITHOUT its
# backticks, indented, so it matched the column-0 form and sealed a row that had
# done nothing. A placeholder summary is never a verdict.
TPL_RENDERED="     ORCH_CLOSE c1 CLOSED — <the same one-sentence summary> (verdict matching the sentinel)."
[[ -z $(closing_line c1 "$TPL_RENDERED") ]] \
  && ok "closing_line ignores the backtick-less rendered template line" \
  || bad "closing_line parsed the rendered template line as a closing line"
[[ $(closing_line c1 "$TPL_RENDERED"$'\n'"  ORCH_CLOSE c1 BLOCKED — rulings needed") == *"rulings needed" ]] \
  && ok "a genuine closing line below the rendered template still reads" \
  || bad "the rendered template masked a genuine closing line"
[[ -n $(closing_line c1 "ORCH_CLOSE c1 BLOCKED — rulings needed") ]] \
  && ok "closing_line still reads a genuine column-0 closing line" \
  || bad "closing_line failed on a genuine closing line"
# end to end: a live window whose pane shows ONLY the template line must NOT seal
tmux new-window -d -t "$SESSION" -n c1tpl "printf '$TPL\\n'; sleep 300" 2>/dev/null
sleep 2
rm -f "$INST/state/c1tpl.status"
ORCH_INSTANCE="$INST" seal_from_pane c1tpl
[[ -z "$(have c1tpl)" ]] \
  && ok "a pane showing only template text is NOT sealed" || bad "a template-only pane was wrongly sealed"
tmux kill-window -t "$SESSION:c1tpl" 2>/dev/null
# end to end: the REAL overdue-close case still seals -- a genuine closing line at
# column 0 (possibly indented) is read as a verdict despite no sentinel file
tmux new-window -d -t "$SESSION" -n c1 "printf 'ORCH_CLOSE c1 BLOCKED — rulings needed\\n'; sleep 300" 2>/dev/null
sleep 2
rm -f "$INST/state/c1.status"
ORCH_INSTANCE="$INST" seal_from_pane c1
[[ $(head -1 "$INST/state/c1.status" 2>/dev/null) == BLOCKED ]] \
  && ok "pane seal wrote the BLOCKED sentinel" || bad "pane seal did not seal BLOCKED"
[[ $(sed -n 2p "$INST/state/c1.status" 2>/dev/null) == "rulings needed" ]] \
  && ok "pane seal captured the summary" || bad "pane seal summary wrong"
# an already-written sentinel is never overwritten by the seal
printf 'ESCALATED\na ruling is mandatory\n' > "$INST/state/c1.status"
ORCH_INSTANCE="$INST" seal_from_pane c1
grep -q '^ESCALATED$' "$INST/state/c1.status" \
  && ok "seal never overwrites an existing sentinel" || bad "seal overwrote an existing sentinel"
tmux kill-window -t "$SESSION:c1" 2>/dev/null

note "8. the auto-unblock loop"
for i in $(seq 1 90); do [[ -f "$INST/state/a4.blocked.1.status" ]] && break; sleep 1; done
[[ -f "$INST/state/a4.blocked.1.status" ]] && ok "a4's BLOCKED verdict was archived (attempt 1)" \
  || bad "no blocked archive for a4"
[[ -f "$INST/BLOCK_SHEET.md" ]] && grep -q "a4" "$INST/BLOCK_SHEET.md" \
  && ok "the block sheet logged the stop" || bad "block sheet missing or empty"
ls "$INST/prompts"/ub-a4-1.*.md >/dev/null 2>&1 && ok "an unblocker work order was generated" \
  || bad "no unblocker work order"
for i in $(seq 1 30); do [[ -s "$INST/state/ub-a4-1.status" ]] && break; sleep 1; done
[[ $(head -1 "$INST/state/ub-a4-1.status" 2>/dev/null) == RESOLVED ]] \
  && ok "the unblocker returned RESOLVED and the row was re-dispatched" || bad "unblocker verdict missing"
for i in $(seq 1 120); do [[ -f "$INST/state/a4.retiered" ]] && break; sleep 1; done
[[ -f "$INST/state/a4.retiered" ]] \
  && ok "max-unblock exhaustion retiered the row instead of paging" || bad "no retier after max unblock attempts"
for i in $(seq 1 60); do grep -q "a4 returned BLOCKED" "$INST/state/events_A.log" && break; sleep 1; done
for i in $(seq 1 60); do grep -q "HALTED" "$INST/state/lane_A.info" && break; sleep 1; done
grep -q "HALTED" "$INST/state/lane_A.info" \
  && ok "a retiered row that blocks again HALTS the lane (no infinite unblock loop)" \
  || bad "the lane did not halt after the retiered row blocked again"

note "8b. a halted lane's banner says its watcher is gone"
# The banner used to be a single line nobody reads back, so a HALTED written at
# the halt stayed on disk looking current long after the halt was cleared.
for i in $(seq 1 30); do grep -q "watcher DOWN" "$INST/state/lane_A.info" && break; sleep 1; done
grep -q "watcher DOWN" "$INST/state/lane_A.info" \
  && ok "lane_A.info is freeze-marked when the watcher exits" \
  || bad "lane_A.info still reads as live after the watcher exited: $(cat "$INST/state/lane_A.info")"
"$ORCH" status | grep -q "no watcher" \
  && ok "orch status flags a halted lane with no watcher behind it" \
  || bad "orch status did not mention the missing watcher"
kill "$DECOY" 2>/dev/null; DECOY=""

note "9. state_resolver: OUR live process beats a stale sentinel; a stranger's does not"
row_state(){ python3 "$HOME_DIR/lib/state_resolver.py" --json | python3 -c \
  "import json,sys; d=json.load(sys.stdin); print([r['state'] for l in d['lanes'] for r in l['rows'] if r['id']=='$1'][0])"; }
printf 'BLOCKED\nstale sentinel left over from an earlier halt\n' > "$INST/state/b2.status"
tmux new-window -d -t "$SESSION" -n b2 "sleep 120" 2>/dev/null
sleep 1
# Bearing the row's NAME proves nothing — a finished session left open after
# `orch reset` bears it too, and adopting one is how a lane reported RUNNING
# for hours with nothing running.
ST=$(row_state b2)
[[ $ST == ORPHAN ]] && ok "an unstamped live window is an ORPHAN, not RUNNING (got ORPHAN)" \
  || bad "an unstamped window was read as work (got $ST)"
"$ORCH" status | grep -q "orch adopt b2" \
  && ok "the orphan banner names the recovery" || bad "orphan banner did not name 'orch adopt'"
# Claiming it is deliberate and explicit; only then does live beat the sentinel.
"$ORCH" adopt b2 >/dev/null 2>&1
ST=$(row_state b2)
[[ $ST == RUNNING ]] && ok "an adopted live window overrides a stale BLOCKED sentinel (got RUNNING)" \
  || bad "stale sentinel won over the adopted live process (got $ST)"
tmux kill-window -t "$SESSION:b2" 2>/dev/null
sleep 1
ST=$(row_state b2)
[[ $ST == BLOCKED ]] && ok "with the window gone the sentinel is authoritative again" \
  || bad "expected BLOCKED once the window died, got $ST"
# A spent unblocker sentinel from an earlier attempt series must not survive
# the reset: the next ub-b2-1 would read it back as a fresh verdict.
printf 'RESOLVED\nan earlier series\n' > "$INST/state/ub-b2-1.status"
"$ORCH" reset b2 >/dev/null 2>&1
[[ ! -e "$INST/state/ub-b2-1.status" ]] && ls "$INST/state"/ub-b2-1.status.consumed-* >/dev/null 2>&1 \
  && ok "reset retires the row's spent ub-<id>-*.status (moved aside, not deleted)" \
  || bad "reset left a stale unblocker sentinel behind"
[[ -f "$INST/state/b2.window" ]] \
  && bad "reset kept the window stamp — the old session would still count as work" \
  || ok "reset drops the window stamp with the sentinel"

note "9b. a CLOSED sentinel is terminal even over a matching live window"
# A finished session left open (e.g. a Claude TUI that never /exit'd after
# writing its sentinel) is a zombie, not proof of work. The lane has moved on,
# so every read-only surface must read the row CLOSED, not RUNNING.
tmux new-window -d -t "$SESSION" -n b2 "sleep 300" 2>/dev/null
"$ORCH" adopt b2 >/dev/null 2>&1           # a matching window this rig claims
printf 'CLOSED\nfinished\n' > "$INST/state/b2.status"
sleep 1
ST=$(row_state b2)
[[ $ST == CLOSED ]] && ok "a CLOSED sentinel wins over a residual live window (got CLOSED)" \
  || bad "a closed row read as $ST behind its leftover window"
tmux kill-window -t "$SESSION:b2" 2>/dev/null
"$ORCH" reset b2 >/dev/null 2>&1

note "10. dead panes are never candidates"
tmux new-window -d -t "$SESSION" -n deadrow "true" 2>/dev/null
sleep 2
python3 "$HOME_DIR/lib/state_resolver.py" --json | grep -q '"deadrow"' \
  && bad "a dead pane appeared in live_windows" || ok "dead panes are filtered out of live windows"

note "11. runtime retiering"
"$ORCH" assign a4 gemini >/dev/null 2>&1
grep -qx gemini "$INST/control/EXECUTOR_a4" && ok "assign wrote the executor override" || bad "assign did not write the override"
"$ORCH" assignments all | grep -q "^a1 " && ok "assignments lists closed rows with 'all'" || bad "assignments all missing rows"
"$ORCH" assign a1 mock 2>&1 | grep -q "REFUSED" && ok "assign refuses a CLOSED row" || bad "assign allowed retiering a CLOSED row"

note "11b. the two Hermes tiers launch with their own model pins"
hq=$(ORCH_INSTANCE="$INST" ORCH_BIN_HERMES=echo ORCH_MODEL_HERMES=qwen3.8-27b ORCH_MODEL_HERMES_PROVIDER=scaleway \
     bash "$HOME_DIR/lib/adapters/hermes.sh" zz hermes /dev/null)
[[ $hq == "-m qwen3.8-27b --provider scaleway" ]] && ok "hermes launches its pinned model and provider" \
  || bad "hermes argv was '$hq'"
hf=$(ORCH_INSTANCE="$INST" ORCH_BIN_HERMES=echo bash "$HOME_DIR/lib/adapters/hermesflash.sh" zz hermesflash /dev/null)
[[ $hf == "-m deepseek-v4-flash-0731 --provider scaleway" ]] && ok "hermesflash launches the flash default with a named provider" \
  || bad "hermesflash argv was '$hf'"
hf=$(ORCH_INSTANCE="$INST" ORCH_BIN_HERMES=echo ORCH_MODEL_HERMESFLASH=m-x ORCH_MODEL_HERMESFLASH_PROVIDER=p-y \
     bash "$HOME_DIR/lib/adapters/hermesflash.sh" zz hermesflash /dev/null)
[[ $hf == "-m m-x --provider p-y" ]] && ok "hermesflash honours its orch.yaml pin" || bad "hermesflash pin argv was '$hf'"

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

note "14. the watcher waits out an orphan window instead of adopting it"
# The reported failure, end to end: a row is reset while the finished session's
# window is still open. The watcher used to adopt that window and report RUNNING
# forever, and killing it halted the lane. It must now WAIT, keep its hands off
# a window it does not own, and dispatch by itself the moment the window is
# closed — with no `orch start`.
mkdir -p "$SBX/project/cycle/PHASE_B/b3"
echo "# plan for b3" > "$SBX/project/cycle/PHASE_B/b3/PLAN.md"
printf '%s\t%s\t%s\t%s\t%s\n' b3 - 0 mock PHASE_B/b3/PLAN.md >> "$INST/queue_B.tsv"
tmux new-window -d -t "$SESSION" -n b3 "sleep 300" 2>/dev/null    # the leftover session
"$ORCH" start B >/dev/null 2>&1
for i in $(seq 1 40); do grep -q "ORPHAN WINDOW" "$INST/state/lane_B.info" && break; sleep 1; done
grep -q "ORPHAN WINDOW 'b3'" "$INST/state/lane_B.info" \
  && ok "the watcher refuses to adopt an unstamped window and says so" \
  || bad "the lane banner did not report the orphan: $(head -1 "$INST/state/lane_B.info")"
[[ -f "$INST/state/b3.status" ]] \
  && bad "the orphan row wrote a sentinel — it should not have run" \
  || ok "the orphan row did not run behind a window that was not working it"
tmux list-windows -t "$SESSION" -F '#{window_name}' | grep -qx b3 \
  && ok "the leftover session was never yanked" || bad "the watcher killed a window it did not own"
# Closing the window is the WHOLE recovery — no restart, no reset.
tmux kill-window -t "$SESSION:b3" 2>/dev/null
for i in $(seq 1 40); do [[ -f "$INST/state/b3.status" ]] && break; sleep 1; done
[[ $(have b3) == CLOSED ]] \
  && ok "closing the orphan let the lane dispatch and close b3 with no 'orch start'" \
  || bad "the lane did not resume after the orphan was closed (b3=$(have b3))"

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
for f in p1.lead.md p1.scoped.md p1.codex.md ub-p1-1.scoped.md; do
  grep -q "NEVER-OPEN (binding, verbatim)" "$PI/prompts/$f" 2>/dev/null \
    && grep -q -- "--exclude='\*.local\*'" "$PI/prompts/$f" \
    && ok "work order $f carries the verbatim never-open clause" \
    || bad "work order $f lacks the never-open clause"
done
ORCH_INSTANCE="$PI" "$ORCH" prompt p1 2>/dev/null | grep -q "p1.lead.md" \
  && grep -q "NEVER-OPEN (binding, verbatim)" "$PI/prompts/p1.lead.md" \
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
[[ -z "$(have d1)" ]] && grep -q "stop_lines=1 " "$SBX/cc1.out" && grep -q "^REFUSED" "$SBX/cc1.out" \
  && ok "orch done refused a CLOSED over a HEADS_UNCHANGED no line (falsifier, *.local*, prose and quotations not counted)" \
  || bad "orch done did not refuse the close: $(head -3 "$SBX/cc1.out")"
printf '\n### DEC-9 — PROPOSED (d1)\nnot a signature\n' >> "$SBX/project/cycle/DECISIONS.md"
echo 'SIGNED_RULING DEC-9' >> "$D1/PROGRESS.md"
"$ORCH" done d1 "closing on a proposal" > "$SBX/cc2.out" 2>&1
[[ -z "$(have d1)" ]] && grep -q "none is a signed" "$SBX/cc2.out" \
  && ok "a PROPOSED entry is not a signed ruling (still refused)" || bad "a PROPOSED entry let the close through"
printf '\n### DEC-8 — RATIFIED (the operator)\nthe moved head is accepted for d1\n' >> "$SBX/project/cycle/DECISIONS.md"
echo 'SIGNED_RULING DEC-8' >> "$D1/PROGRESS.md"
"$ORCH" done d1 "closed on a signed ruling" > "$SBX/cc3.out" 2>&1
[[ $(have d1) == CLOSED ]] && grep -q "signed=DEC-8" "$SBX/cc3.out" \
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
t = t.replace('paste_kick: []', 'paste_kick: []\n  arg_kick: [opencode]')
f.write_text(t)
PY
mkdir -p "$SBX/project/cycle/PHASE_O/o1"; echo "# plan for o1" > "$SBX/project/cycle/PHASE_O/o1/PLAN.md"
printf '%s\t%s\t%s\t%s\t%s\n' o1 - 0 opencode PHASE_O/o1/PLAN.md > "$INST/queue_O.tsv"
"$ORCH" lint > "$SBX/lint_o.out" 2>&1 && ok "lint accepts an opencode row (adapter + kick resolve)" \
  || bad "lint refused the opencode row: $(tail -3 "$SBX/lint_o.out")"
"$ORCH" prompt o1 >/dev/null 2>&1
[[ -s $INST/prompts/o1.scoped.md ]] && grep -q "scoped tier" "$INST/prompts/o1.kick.txt" 2>/dev/null \
  && ok "an arg_kick executor gets its kick written (flavor fallback _KICK_SCOPED.txt)" \
  || bad "no kick written for the arg_kick executor"
echo "OPENCODE KICK __ID__ -> __WORKORDER__" > "$INST/prompts/_KICK_OPENCODE.txt"
"$ORCH" prompt o1 >/dev/null 2>&1
grep -qx "OPENCODE KICK o1 -> .*prompts/o1.scoped.md" "$INST/prompts/o1.kick.txt" \
  && ok "the executor's own _KICK_OPENCODE.txt wins over the flavor's kick, and is rewritten" \
  || bad "the executor-specific kick was not used: $(head -1 "$INST/prompts/o1.kick.txt")"
STUB="$SBX/opencode-stub"; printf '#!/usr/bin/env bash\nprintf "%%s\\n" "$@" > "%s"\n' "$SBX/oc_argv" > "$STUB"; chmod +x "$STUB"
( cd "$SBX/project/cycle" && ORCH_BIN_OPENCODE="$STUB" ORCH_ARG_KICK=opencode ORCH_MODEL_OPENCODE=m-1 \
    ORCH_MODEL_OPENCODE_PROVIDER=prov "$HOME_DIR/lib/adapters/opencode.sh" o1 opencode "$INST/prompts/o1.scoped.md" )
[[ $(sed -n 1,3p "$SBX/oc_argv" | tr '\n' ' ') == "-m prov/m-1 --auto " ]] && [[ $(sed -n 4p "$SBX/oc_argv") == --prompt ]] \
  && grep -q "^OPENCODE KICK o1" "$SBX/oc_argv" \
  && ok "the adapter launches -m provider/model --auto --prompt <kick>" || bad "adapter argv: $(tr '\n' ' ' < "$SBX/oc_argv")"
echo "other/m-2" > "$INST/control/OPENCODE_MODEL_o1"
( cd "$SBX/project/cycle" && ORCH_BIN_OPENCODE="$STUB" ORCH_ARG_KICK="" ORCH_MODEL_OPENCODE=m-1 \
    ORCH_MODEL_OPENCODE_PROVIDER=prov "$HOME_DIR/lib/adapters/opencode.sh" o1 opencode "$INST/prompts/o1.scoped.md" )
[[ $(sed -n 2p "$SBX/oc_argv") == other/m-2 ]] && grep -q "scoped tier" "$SBX/oc_argv" && ! grep -q "OPENCODE KICK" "$SBX/oc_argv" \
  && ok "a per-row model override wins; outside arg_kick the full work order is the prompt" \
  || bad "override/work-order argv: $(tr '\n' ' ' < "$SBX/oc_argv" | cut -c1-200)"
rm -f "$INST/control/OPENCODE_MODEL_o1"
"$ORCH" done o1 "opencode row closed by the CLI" > "$SBX/o1done.out" 2>&1
[[ $(have o1) == CLOSED ]] && ok "orch done closes an opencode row (it does not write its own sentinel)" \
  || bad "orch done refused the opencode row: $(head -2 "$SBX/o1done.out")"

printf '\n\033[1m== result ==\033[0m\n'
printf '  %d passed, %d failed\n' "$PASS" "$FAIL"
[[ $FAIL -eq 0 ]] || exit 1
