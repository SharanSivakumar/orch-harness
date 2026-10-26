#!/usr/bin/env bash
# Mock adapter — the dry-run executor used by the test suites. It occupies a real
# tmux window and writes a real claim, so it exercises dispatch -> claim -> close
# checks -> review -> accept with no model, no network and no spend. It is also
# the fastest way to prove a new ladder's graph before any real executor runs.
#
# Scripting a run (all optional, read at launch; <sid> is the session name, so
# reviewers and unblockers are scripted the same way: MOCK_rv-a1-1, MOCK_ub-a1-1):
#   control/MOCK_<sid>          line 1: the verdict; line 2: the summary; line 3 (rows, CLOSED):
#                               an outcome go|nogo|held|fail — written as OUTCOME, sealed as
#                               CLOSED:<x>, passed as `orch done --outcome <x>`
#                               default by kind: row CLOSED · ub- RESOLVED ·
#                               rv- ACCEPT · pf- PASS
#   control/MOCK_DELAY_<sid>    seconds to "work" before writing   (default 2)
#   control/MOCK_HANG_<sid>     never write a claim (idle / dead-window paths)
#   control/MOCK_EXIT_<sid>     exit at once with no claim (dead-window path)
#   control/MOCK_PANE_<sid>     print this file's text to the pane at once (a captured provider message, for the
#                               rate-limit fallback drills); with MOCK_HANG_ the pane then stays still
#   control/MOCK_TICKER_<sid>   keep printing a new line every second, forever (a row that is working normally)
#   control/MOCK_ONLY_<sid>     PANE / TICKER / HANG apply only on the executor(s) named here (words; the executor's
#                               real name, ORCH_EXECUTOR — a variant has its own); other executors run the row normally
#   control/MOCK_NOPROGRESS_<sid>  a row does not write PROGRESS.md
#   control/MOCK_SEAL_<sid>     print the ORCH_CLOSE line instead of writing the
#                               claim, then sit idle (auto-seal path)
#   control/MOCK_VIA_CLI_<sid>  close through `orch done` / `orch verdict`
#   control/MOCK_LINE_OUTCOME_<sid>  with MOCK_VIA_CLI_: `orch done` WITHOUT --outcome, then print
#                               `ORCH_CLOSE <sid> CLOSED:<outcome>` and sit idle (the claim and its
#                               closing line disagree)
#   control/MOCK_SW_<sid>       Steward/planner sessions: one `orch sw` request per line (shell words,
#                               e.g. `reset a1 "re-run"`), run in order before the verdict; outcomes in the pane
#   control/MOCKSW_<subject>, MOCKSWV_<subject>   a Steward session's actions / verdict, by its incident's subject
#   control/MOCKSWRUN_<subject>                   a Steward session's MOCK_RUN script, by its incident's subject
#   control/MOCKPL_<round>, MOCKPLRUN_<round>     a planner pl-<round>-<k>'s `orch sw` lines / MOCK_RUN script, by its
#                                                 round (the course-loop drills cannot know <k>)
#   control/MOCKPD_<subject>, MOCKPDV_<subject>   the panel's decider sw-<n>-d<k> (actions are staged; default RESOLVED)
#   control/MOCKPFV_<subject>                     the panel's falsifier sw-<n>-f<k> (default CONFIRM)
#   control/MOCK_RUN_<sid>      a bash script run INSIDE the session's pane before the claim (output in
#                               <instance>/mockrun/<sid>.out) — the capability drills run from here; when it exits 75 with a
#                               `SPEND_WAIT <id>` line (orch spend run refused), the claim is BLOCKED with that line
set -u
id=$1 model=$2 order=$3
C="$ORCH_INSTANCE/control"
S="$ORCH_INSTANCE/state"
case ${ORCH_KIND:-row} in
  unblock) dv=RESOLVED ;; review) dv=ACCEPT ;; preflight) dv=PASS ;; roundreview) dv=DONE ;;
  steward) dv=RESOLVED ;; planner) dv=PASS ;; *) dv=CLOSED ;;
esac
role=""   # the panel tier: sw-<n>-d<k> decider, sw-<n>-f<k> falsifier
[[ ${ORCH_KIND:-row} == steward && $id =~ ^sw-[0-9]+-d[0-9]+$ ]] && role=decider
[[ ${ORCH_KIND:-row} == steward && $id =~ ^sw-[0-9]+-f[0-9]+$ ]] && { role=falsifier; dv=CONFIRM; }
subj=""
if [[ ${ORCH_KIND:-row} == steward ]]; then
  n=${id#sw-}; n=${n%%-*}
  subj=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("subject",""))' "$S/steward/incidents/$n.json" 2>/dev/null | tr -c 'A-Za-z0-9_.:\n-' '_' || true)
  # control/MOCKSW_<subject> (actions) and MOCKSWV_<subject> (verdict) script it when MOCK_<sid> is absent
  case $role in
    decider)   va=MOCKPD_ vv=MOCKPDV_ ;;
    falsifier) va=MOCKPF_ vv=MOCKPFV_ ;;
    *)         va=MOCKSW_ vv=MOCKSWV_ ;;
  esac
  [[ ! -f "$C/MOCK_$id" && -n $subj && -f "$C/$vv$subj" ]] && cp "$C/$vv$subj" "$C/MOCK_$id"
  [[ ! -f "$C/MOCK_SW_$id" && -n $subj && -f "$C/$va$subj" ]] && cp "$C/$va$subj" "$C/MOCK_SW_$id"
  [[ -z $role && ! -f "$C/MOCK_RUN_$id" && -n $subj && -f "$C/MOCKSWRUN_$subj" ]] && cp "$C/MOCKSWRUN_$subj" "$C/MOCK_RUN_$id"
fi
if [[ ${ORCH_KIND:-row} == planner ]]; then
  rnd=${id#pl-}; rnd=${rnd%-*}
  [[ ! -f "$C/MOCK_SW_$id" && -f "$C/MOCKPL_$rnd" ]] && cp "$C/MOCKPL_$rnd" "$C/MOCK_SW_$id"
  [[ ! -f "$C/MOCK_RUN_$id" && -f "$C/MOCKPLRUN_$rnd" ]] && cp "$C/MOCKPLRUN_$rnd" "$C/MOCK_RUN_$id"
fi
verdict=$(sed -n 1p "$C/MOCK_$id" 2>/dev/null || true)
summary=$(sed -n 2p "$C/MOCK_$id" 2>/dev/null || true)
outcome=$(sed -n 3p "$C/MOCK_$id" 2>/dev/null | tr -d '[:space:]' || true)
: "${verdict:=$dv}"
: "${summary:=mock executor ran $id}"
[[ ${ORCH_KIND:-row} == row && $verdict == CLOSED ]] || outcome=""
delay=$(cat "$C/MOCK_DELAY_$id" 2>/dev/null || echo 2)

echo "[mock] session=$id kind=${ORCH_KIND:-row} executor=$model extra=(${ORCH_EXTRA_ARGS:-}) env MOCK_ENV=${MOCK_ENV:-}"
echo "[mock] work order: $order"
echo "[mock] verdict=$verdict delay=${delay}s"
[[ -f "$C/MOCK_EXIT_$id" ]] && { echo "[mock] exiting with no claim"; exit 3; }
only_ok(){ [[ ! -f "$C/MOCK_ONLY_$id" ]] || [[ " $(cat "$C/MOCK_ONLY_$id") " == *" ${ORCH_EXECUTOR:-$model} "* ]]; }
if [[ -f "$C/MOCK_PANE_$id" ]] && only_ok; then cat "$C/MOCK_PANE_$id"; fi
if [[ -f "$C/MOCK_TICKER_$id" ]] && only_ok; then
  i=0; while true; do i=$((i+1)); echo "test output line $i"; sleep 1; done
fi
sleep "$delay"
if [[ -f "$C/MOCK_RUN_$id" ]]; then
  mkdir -p "$ORCH_INSTANCE/mockrun"
  bash "$C/MOCK_RUN_$id" > "$ORCH_INSTANCE/mockrun/$id.out" 2>&1
  rc=$?
  echo "[mock] ran MOCK_RUN_$id (exit $rc)"
  # `orch spend run` refused (exit 75): claim BLOCKED with its SPEND_WAIT line, as a work order says
  if [[ $rc == 75 ]] && sw=$(grep -m1 -o '^SPEND_WAIT sp[0-9]*' "$ORCH_INSTANCE/mockrun/$id.out"); then
    verdict=BLOCKED summary=$sw outcome=""
  fi
fi
if [[ -f "$C/MOCK_HANG_$id" ]] && only_ok; then
  echo "[mock] MOCK_HANG_$id present — hanging deliberately, no claim will be written."
  while true; do sleep 60; done
fi
if [[ ${ORCH_KIND:-row} == row && -n ${ORCH_PLAN_DIR:-} && ! -f "$C/MOCK_NOPROGRESS_$id" ]]; then
  printf '# PROGRESS — %s\n\nmock run at %s\n' "$id" "$(date '+%F %T')" > "$ORCH_PLAN_DIR/PROGRESS.md"
fi
if [[ ${ORCH_KIND:-row} == review ]]; then
  mkdir -p "$ORCH_INSTANCE/reviews"
  printf '# review %s\n\n%s — %s\n' "$id" "$verdict" "$summary" > "$ORCH_INSTANCE/reviews/${id#rv-}.md"
fi
if [[ ${ORCH_KIND:-row} == roundreview ]]; then
  rk=${id#rr-} rd="$ORCH_INSTANCE/rounds/${id#rr-}/review"
  # a checkpoint judge's loop (v3.1) reviews round <tag> into rounds/<tag>/review+<judge>/
  [[ $rk == *+* && ! -f "$ORCH_INSTANCE/rounds/$rk/round.yaml" ]] && rd="$ORCH_INSTANCE/rounds/${rk%%+*}/review+${rk#*+}"
  mkdir -p "$rd"
  printf '# brief %s\n' "$id" > "$rd/BRIEF.md"
fi
if [[ ${ORCH_KIND:-row} == preflight ]]; then
  mkdir -p "$ORCH_INSTANCE/preflight"
  printf '# plan review %s\n\nmock: %s — %s\n' "$id" "$verdict" "$summary" > "$ORCH_INSTANCE/preflight/${id#pf-}.review.md"
fi
if [[ ${ORCH_KIND:-row} == steward || ${ORCH_KIND:-row} == planner ]]; then
  if [[ -f "$C/MOCK_SW_$id" ]]; then
    while IFS= read -r line || [[ -n $line ]]; do
      [[ -z ${line// } ]] && continue
      eval "set -- $line"
      echo "[mock] orch sw $*"
      "$ORCH_INSTANCE/orch" sw "$@"
    done < "$C/MOCK_SW_$id"
  fi
  if [[ ${ORCH_KIND} == steward ]]; then
    n=${id#sw-}; mkdir -p "$ORCH_INSTANCE/steward/incidents"
    [[ $role == decider ]] && n="${n%%-*}-panel"   # the decider's evidence is its proposal
    printf '# steward %s

mock analysis: %s — %s
' "$id" "$verdict" "$summary" > "$ORCH_INSTANCE/steward/incidents/${n}.md"
  else
    mkdir -p "$ORCH_INSTANCE/steward/plans"
    printf '# planner %s

mock plan: %s — %s
' "$id" "$verdict" "$summary" > "$ORCH_INSTANCE/steward/plans/${id}.md"
  fi
fi
if [[ -f "$C/MOCK_SEAL_$id" ]]; then
  echo "ORCH_CLOSE $id $verdict${outcome:+:$outcome} — $summary"
  while true; do sleep 60; done
fi
if [[ -f "$C/MOCK_VIA_CLI_$id" ]]; then
  case ${ORCH_KIND:-row} in
    row) if [[ $verdict == CLOSED && -f "$C/MOCK_LINE_OUTCOME_$id" ]]; then
           "$ORCH_INSTANCE/orch" done "$id" "$summary"
           echo "ORCH_CLOSE $id $verdict:$outcome — $summary"
           while true; do sleep 60; done
         elif [[ $verdict == CLOSED ]]; then "$ORCH_INSTANCE/orch" done "$id" "$summary" ${outcome:+--outcome "$outcome"}
         else "$ORCH_INSTANCE/orch" blocked "$id" "$summary"; fi ;;
    *)   "$ORCH_INSTANCE/orch" verdict "$id" "$verdict" "$summary" ;;
  esac
  echo "[mock] closed via the CLI (exit $?)"
  sleep 1; exit 0
fi
printf '%s\n%s\n%s' "$verdict" "$summary" "${outcome:+OUTCOME $outcome
}" > "$S/$id.status"
echo "[mock] wrote $S/$id.status = $verdict"
sleep 1
