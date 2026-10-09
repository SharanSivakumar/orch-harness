#!/usr/bin/env bash
# orchestrator_core.sh — the standalone multi-lane DAG watcher.
#
# Generalized from a long-running multi-lane agent rig.
# Everything that was hardcoded there — root, session, lanes, executors,
# templates, allowlists, chat ids — now comes from the instance's orch.yaml,
# resolved once per invocation by lib/config_parser.py.
#
# Invariants carried over verbatim, because each was paid for by an incident:
#   * VISIBLE TMUX ONLY. Every executor runs in a named, attachable window.
#     Never headless — the operator must be able to watch any agent, anytime.
#   * QUEUE RESCANNED FRESH each pass. Appending or editing rows while the
#     watcher runs is safe and is the intended workflow.
#   * EXECUTOR RESOLVED AT DISPATCH TIME. control/EXECUTOR_<id> > queue column
#     > configured default. Retiering never edits the queue.
#   * A LIVE WINDOW IS THE ROW RUNNING. Never double-dispatch, never yank a
#     running session.
#   * DIED WITHOUT A SENTINEL = HALT AND PAGE. Silence is never success.
#   * STALL IS WARNED, NEVER KILLED.
#
# Subcommands are reached through bin/orch; `run` and `launch` are internal.
set -u

ORCH_LIB="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ORCH_HOME="$(dirname "$ORCH_LIB")"

# --- configuration ---------------------------------------------------------
_cfg="$(python3 "$ORCH_LIB/config_parser.py" --sh)" || exit 2
eval "$_cfg"
STATE="$ORCH_INSTANCE/state"; CONTROL="$ORCH_INSTANCE/control"
LEASES="$ORCH_INSTANCE/leases"; PROMPTS="$ORCH_INSTANCE/prompts"
UNBLOCK_DIR="$ORCH_INSTANCE/unblock"
BLOCK_SHEET="$ORCH_INSTANCE/BLOCK_SHEET.md"
mkdir -p "$STATE" "$CONTROL" "$LEASES" "$PROMPTS" "$UNBLOCK_DIR"
export DBUS_SESSION_BUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS:-unix:path=/run/user/$(id -u)/bus}"
# every ORCH_* setting is exported so adapters and executors inherit them
export $(compgen -v ORCH_)
export ORCH_HOME ORCH_LIB
# closing-line auto-seal helpers (used by wait_row and the claude adapter)
# shellcheck source=/dev/null
. "$ORCH_LIB/closing.sh"

# --- logging & notification ------------------------------------------------
log(){ local lane=$1; shift; printf '%s [%s] %s\n' "$(date '+%F %T')" "$lane" "$*" >> "$STATE/events_${lane}.log"; }

notify(){ # <urgency: low|normal|critical> <title> <body>
  local u=$1 t=$2 b=$3
  command -v notify-send >/dev/null 2>&1 && notify-send -u "$u" "$t" "$b" || true
  case $u in
    critical|normal) python3 "$ORCH_LIB/telegram_bot.py" push --title "$t" --body "$b" >/dev/null 2>&1 || true ;;
  esac
}
# lane_<L>.info is a breadcrumb NOBODY reads back — `orch status` resolves live
# state independently. So a banner written at a halt stayed on disk verbatim
# long after the halt was cleared, and read as current because it carried a
# plausible timestamp. Line 2 now says whether a watcher is behind line 1.
lane_info(){ # lane text
  local f="$STATE/lane_$1.info" t; t=$(date '+%F %T')
  printf '%s · %s\n' "$2" "$t" > "$f"
  if [[ -n ${ORCH_WATCHER_PID:-} ]]; then
    printf 'lane watcher pid %s LIVE as of %s\n' "$ORCH_WATCHER_PID" "$t" >> "$f"
  else
    printf 'written by the CLI at %s (no lane watcher in this process)\n' "$t" >> "$f"
  fi
}
lane_down(){ # lane — freeze-mark the banner when the watcher exits, for ANY reason
  local f="$STATE/lane_$1.info" first
  [[ -f $f ]] || return 0
  first=$(head -1 "$f" 2>/dev/null) || return 0
  { printf '%s\n' "$first"
    printf 'lane watcher DOWN since %s — the line above is FROZEN, not live. `orch status` is authoritative; `orch start` resumes this lane.\n' \
      "$(date '+%F %T')"; } > "$f.tmp" && mv -f "$f.tmp" "$f"
}

# --- sentinels & control ---------------------------------------------------
status_of(){ local f="$STATE/$1.status"; [[ -s $f ]] && head -1 "$f" | tr -dc 'A-Z_' || echo NONE; }
status_note(){ sed -n 2p "$STATE/$1.status" 2>/dev/null || true; }
stopped(){ [[ -f $CONTROL/STOP || -f $CONTROL/STOP_$1 ]]; }
paused(){ [[ -f $CONTROL/PAUSE || -f $CONTROL/PAUSE_$1 ]]; }
deps_met(){ local deps=$1 d; [[ $deps == "-" ]] && return 0
  IFS=',' read -ra arr <<< "$deps"
  for d in "${arr[@]}"; do [[ $(status_of "$d") == CLOSED ]] || return 1; done; }

valid_exec(){ [[ " $ORCH_EXECUTORS " == *" $1 "* ]]; }
window_exec(){ [[ $1 != manual ]]; }
claude_exec(){ case $1 in opus|sonnet|haiku) return 0;; *) return 1;; esac }
# Executors whose session can be wedged on a rate/usage-limit screen and is
# safe to auto-nudge past it. claude_exec is a subset; codex shows a
# time-based banner ("try again at HH:MM") rather than os's rolling reset.
limit_watched_exec(){ case $1 in opus|sonnet|haiku|codex) return 0;; *) return 1;; esac }
# True when a Codex "try again at HH:MM" reset has passed (or can't be parsed —
# treat as ready so a stuck row still gets nudged rather than wedged). Codex's
# cap is server-side and wall-clock, so sending Enter before it lifts is a no-op
# we'd rather skip than burn on the 15-min cadence.
codex_reset_passed(){ # pane -> 0 if reset passed/unparsable, 1 if still waiting
  local t ep
  t=$(printf '%s' "$1" | grep -oiE 'try again at [0-9]{1,2}:[0-9]{2}( ?[AP]M)?' | head -1 | sed -E 's/.*at //')
  [[ -n $t ]] || return 0
  ep=$(date -d "$t" +%s 2>/dev/null)
  [[ -n $ep && $(date +%s) -ge $ep ]]
}
paste_exec(){ [[ " $ORCH_PASTE_KICK " == *" $1 "* ]]; }
scoped_exec(){ # rows eligible for the auto-unblock loop
  # An explicit executors.auto_unblock list wins; otherwise every
  # scoped-flavored executor qualifies. The list exists because an earlier rig
  # auto-unblocked `hermespro`, which is lead-flavored — tier and
  # unblock-eligibility are not the same question.
  if [[ -n "${ORCH_AUTO_UNBLOCK// }" ]]; then
    [[ " $ORCH_AUTO_UNBLOCK " == *" $1 "* ]]; return
  fi
  local f; f=$(python3 - "$1" <<'PY'
import sys, os
sys.path.insert(0, os.environ["ORCH_LIB"])
from config_parser import load
print(load().flavor(sys.argv[1]))
PY
)
  [[ $f == scoped ]]; }

resolve_exec(){ # id [queue_exec] — override > queue > default, at dispatch time
  local id=$1 q=${2:-$ORCH_EXEC_DEFAULT} o
  o=$(head -1 "$CONTROL/EXECUTOR_$id" 2>/dev/null | tr -dc 'a-z' || true)
  if [[ -n $o ]]; then valid_exec "$o" && { echo "$o"; return; }; echo "$ORCH_EXEC_DEFAULT"; return; fi
  valid_exec "$q" && echo "$q" || echo "$ORCH_EXEC_DEFAULT"
}
exec_source(){ [[ -s "$CONTROL/EXECUTOR_$1" ]] && echo override || echo queue; }

adapter_for(){ # executor -> adapters/<file>.sh
  case $1 in
    opus|sonnet|haiku) echo claude ;;
    hermespro)         echo hermes_pro ;;
    *)                 echo "$1" ;;
  esac
}

gen_prompt(){ # id lane exec plan -> prompt path
  python3 "$ORCH_LIB/template_engine.py" prompt --id "$1" --lane "$2" --exec "$3" --plan "$4"
}

# --- tmux ------------------------------------------------------------------
window_alive(){ tmux list-windows -t "$ORCH_TMUX" -F '#{window_name} #{pane_dead}' 2>/dev/null | grep -q "^$1 0$"; }
# A window WEARING a row's name is not evidence the row is being worked. A
# finished session left open after `orch reset` wears the same name, and the
# watcher adopted it and reported RUNNING forever. So dispatch stamps the tmux
# identity it actually created, and only that identity counts as ours.
window_ident(){ # id -> "<window_id> <pane_pid>" of the LIVE window, empty if none
  # Space-separated on purpose: tmux does NOT expand \t in a -F format string,
  # it prints the two characters, so a tab-delimited format silently yields one
  # field. Window names, ids and pids never contain spaces.
  tmux list-windows -t "$ORCH_TMUX" \
      -F '#{window_name} #{pane_dead} #{window_id} #{pane_pid}' 2>/dev/null \
    | awk -v n="$1" '$1==n && $2=="0" {print $3, $4; exit}'
}
stamp_window(){ # id — record the identity this rig dispatched
  local ident; ident=$(window_ident "$1") || return 1
  [[ -n $ident ]] || return 1
  printf '%s %s\n' "$ident" "$(date +%s)" > "$STATE/$1.window"
}
window_is_ours(){ # id — 0 when the live window is the one this rig dispatched
  local live stamp
  live=$(window_ident "$1"); [[ -n $live ]] || return 1
  stamp=$(awk 'NR==1{print $1, $2}' "$STATE/$1.window" 2>/dev/null); [[ -n ${stamp// } ]] || return 1
  [[ $live == "$stamp" ]]
}
# Liveness of a lane is a PROCESS question, never a window question: a window
# that outlives its watcher made `orch start` a silent no-op and left the last
# banner — usually HALTED — standing.
#
# It is also a question about THIS instance. Lane letters repeat across rigs
# (every rig has an `A`), and a bare `ps | grep "run A"` counted another
# instance's watcher as ours: `orch start` then skipped the lane and nothing
# ever ran (the mock-cycle sandbox once saw a live rig's watch-A). Every
# watcher is launched with ORCH_INSTANCE in its environment, so a candidate is
# ours only when that instance resolves to ours. When the environment cannot be
# read, the candidate is counted as ours: a skipped start is recoverable, a
# second watcher on one lane is not.
instance_of_pid(){ # pid -> the ORCH_INSTANCE in its environment, empty if unreadable
  [[ -r /proc/$1/environ ]] || return 0
  tr '\0' '\n' 2>/dev/null < "/proc/$1/environ" | sed -n 's/^ORCH_INSTANCE=//p' | head -1
}
watcher_pids(){ # lane -> pids of THIS instance's live watcher processes for the lane
  local pid args inst mine re="orchestrator_core\\.sh run ${1}( |\$)"
  mine=$(readlink -f "$ORCH_INSTANCE" 2>/dev/null || echo "$ORCH_INSTANCE")
  while read -r pid args; do
    [[ $args =~ $re ]] || continue
    inst=$(instance_of_pid "$pid")
    if [[ -z $inst ]] || [[ $(readlink -f "$inst" 2>/dev/null || echo "$inst") == "$mine" ]]; then
      echo "$pid"
    fi
  done < <(ps -eo pid=,args=)
}
watcher_alive(){ [[ -n $(watcher_pids "$1") ]]; }
kill_dead_window(){ local id=$1 wid name dead
  while read -r wid name dead; do
    [[ $name == "$id" && $dead == 1 ]] && tmux kill-window -t "$wid"
  done < <(tmux list-windows -t "$ORCH_TMUX" -F '#{window_id} #{window_name} #{pane_dead}' 2>/dev/null)
}
ensure_session(){
  tmux has-session -t "$ORCH_TMUX" 2>/dev/null || {
    tmux new-session -d -s "$ORCH_TMUX" -n control -c "$ORCH_ROOT"
    tmux set-option -t "$ORCH_TMUX" remain-on-exit on; }
}

paste_kick(){ # id — deliver the kick text once Hermes' input prompt is ready
  local id=$1 tries=0 pane
  while (( tries < 45 )); do
    sleep 2; tries=$((tries+1))
    window_alive "$id" || return 1
    # Banner output appears several seconds before Hermes creates its input
    # widget. Treating any non-blank pane as ready can paste the work order
    # into a not-yet-mounted widget, where it remains invisibly buffered and
    # the row looks RUNNING forever. Wait for Hermes' explicit ready line.
    pane=$(tmux capture-pane -p -t "$ORCH_TMUX:$id" 2>/dev/null || true)
    if printf '%s' "$pane" | grep -q 'Welcome to Hermes Agent! Type your message or /help for commands\.'; then
      sleep 1
      tmux load-buffer -b "kick_$id" "$PROMPTS/$id.kick.txt" 2>/dev/null || return 1
      tmux paste-buffer -d -b "kick_$id" -t "$ORCH_TMUX:$id" 2>/dev/null || return 1
      # A long bracketed paste can consume the first Enter without submitting
      # the turn. Verify that Hermes rendered the user turn/initialization;
      # send one fallback Enter only when the first did not take.
      sleep 1; tmux send-keys -t "$ORCH_TMUX:$id" Enter
      sleep 2
      pane=$(tmux capture-pane -p -S -200 -t "$ORCH_TMUX:$id" 2>/dev/null || true)
      if ! printf '%s' "$pane" | grep -qE 'Initializing agent|● You are the fresh Hermes executor'; then
        tmux send-keys -t "$ORCH_TMUX:$id" Enter
        sleep 2
        pane=$(tmux capture-pane -p -S -200 -t "$ORCH_TMUX:$id" 2>/dev/null || true)
      fi
      printf '%s' "$pane" | grep -qE 'Initializing agent|● You are the fresh Hermes executor'
      return
    fi
  done
  return 1
}

dispatch(){ # lane id exec plan
  local lane=$1 id=$2 x=$3 plan=$4
  kill_dead_window "$id"
  if window_alive "$id"; then
    log "$lane" "$id already has a LIVE window — not dispatching a second session"
    lane_info "$lane" "$id already running (live tmux window) — dispatch skipped"
    return 1
  fi
  ensure_session
  gen_prompt "$id" "$lane" "$x" "$plan" >/dev/null || {
    log "$lane" "$id prompt generation FAILED — halting"; return 1; }
  rm -f "$STATE/$id.stall_notified" "$STATE/$id.limit_nudge" "$STATE/$id.reassign_noted" \
        "$STATE/$id.orphan_notified" "$STATE/$id.window"
  # The instance is passed EXPLICITLY: a tmux window inherits the tmux
  # server's environment, not the spawning process's, so an exported
  # ORCH_INSTANCE does not reach it. Every window that ever failed to find
  # its orch.yaml failed here.
  tmux new-window -d -t "$ORCH_TMUX" -n "$id" -c "$ORCH_ROOT" \
       "ORCH_INSTANCE='$ORCH_INSTANCE' '$ORCH_LIB/orchestrator_core.sh' launch $id $x"
  stamp_window "$id" || log "$lane" "WARNING: could not stamp $id's window — it may read as an orphan"
  log "$lane" "dispatched $id (tmux window '$id', executor $x)"
  if paste_exec "$x"; then
    ( if paste_kick "$id"; then log "$lane" "$id kick text pasted"
      else log "$lane" "$id kick paste FAILED — paste prompts/$id.kick.txt by hand"
           notify normal "$ORCH_NAME: $id kick paste failed" \
             "The TUI did not come up in time. Paste prompts/$id.kick.txt into tmux window '$id'."
      fi ) &
  fi
}

announce_manual(){ # lane id plan
  local lane=$1 id=$2 plan=$3
  gen_prompt "$id" "$lane" manual "$plan" >/dev/null
  lane_info "$lane" "WAITING FOR OPERATOR — $id is a manual row (work order: prompts/$id.manual.md · close: orch done $id · or hand it to an agent: orch assign $id <executor>)"
  if [[ ! -f "$STATE/$id.manual_notified" ]]; then
    touch "$STATE/$id.manual_notified"
    log "$lane" "$id WAITING FOR OPERATOR (manual executor)"
    notify normal "$ORCH_NAME: $id is YOURS (manual)" \
      "Work order: prompts/$id.manual.md · close: orch done $id \"summary\" · or reassign: orch assign $id $ORCH_EXEC_DEFAULT"
  fi
}

# --- gated-row pre-dispatch probe ---------------------------------
# A gated row's GO_ file says the operator released it; it does not say the row's
# prerequisites outside the workspace exist on this host (a pin file, a backend
# file, a state key, a CLI). A lane-S row was dispatched while its wrapper could
# not run here and burned an attempt finding out. So before an agent dispatch of
# a gate=1 row the watcher runs the probe the instance DECLARES for it, and does
# not dispatch while it fails. Declared in <instance>/gate_probes.tsv:
#     <row id | lane letter> <TAB> <command, run by bash -c from the project root>
# A row id wins over its lane. No declaration = no probe. The probe's output goes
# to state/<id>.probe.out (mode 600); only its exit code enters the event log.
gate_probe_cmd(){ # lane id -> declared probe command, empty if none
  local f="$ORCH_INSTANCE/gate_probes.tsv" key cmd byrow="" bylane=""
  [[ -f $f ]] || return 0
  while IFS=$'\t' read -r key cmd; do
    [[ -z ${key// } || $key == \#* || -z ${cmd// } ]] && continue
    [[ $key == "$2" ]] && byrow=$cmd
    [[ $key == "$1" ]] && bylane=$cmd
  done < "$f"
  printf '%s' "${byrow:-$bylane}"
}
gate_probe(){ # lane id -> 0 pass (or nothing declared), 1 fail
  local lane=$1 id=$2 cmd rc=0 out="$STATE/$2.probe.out"
  cmd=$(gate_probe_cmd "$lane" "$id"); [[ -n $cmd ]] || return 0
  ( umask 077; cd "$ORCH_ROOT" && timeout "${ORCH_PROBE_TIMEOUT:-120}" bash -c "$cmd" ) \
    > "$out" 2>&1 < /dev/null || rc=$?
  if (( rc == 0 )); then
    if [[ -f $STATE/$id.probe_failed ]]; then log "$lane" "$id pre-dispatch probe PASSES again (exit 0)"
    else log "$lane" "$id pre-dispatch probe passed (exit 0)"; fi
    rm -f "$STATE/$id.probe_failed"
    return 0
  fi
  lane_info "$lane" "PROBE FAILED before $id (exit $rc) — not dispatched; the probe re-runs every ${ORCH_POLL}s. Probe: gate_probes.tsv · output: state/$id.probe.out"
  if [[ ! -f $STATE/$id.probe_failed ]]; then
    touch "$STATE/$id.probe_failed"
    log "$lane" "$id pre-dispatch probe FAILED (exit $rc) — gated row NOT dispatched (declared in gate_probes.tsv; output state/$id.probe.out)"
    notify normal "$ORCH_NAME lane $lane: $id probe failed" \
      "The gated row's pre-dispatch probe exited $rc, so $id was not dispatched. Fix the prerequisite; the lane retries by itself."
  fi
  return 1
}

# --- the per-row wait ------------------------------------------------------
wait_row(){ # lane id qexec plan cur_exec
  local lane=$1 id=$2 qexec=$3 plan=$4 x=$5 newx act now lastn pane reset_at
  while true; do
    [[ -s $STATE/$id.status ]] && return 0
    stopped "$lane" && { lane_info "$lane" "HALTED (STOP) while waiting on $id"; log "$lane" "STOP while waiting on $id"; return 1; }
    newx=$(resolve_exec "$id" "$qexec")
    if [[ $newx != "$x" ]]; then
      if window_exec "$x" && window_alive "$id"; then
        if [[ ! -f "$STATE/$id.reassign_noted" ]]; then
          touch "$STATE/$id.reassign_noted"
          log "$lane" "$id reassigned $x->$newx while its window is LIVE — ignored until the window closes"
        fi
      else
        log "$lane" "$id executor reassigned $x -> $newx while waiting — acting on it"
        x=$newx
        if window_exec "$x"; then
          dispatch "$lane" "$id" "$x" "$plan" || true
          lane_info "$lane" "RUNNING $id (executor $x)"
        else
          rm -f "$STATE/$id.manual_notified"; announce_manual "$lane" "$id" "$plan"
        fi
        continue
      fi
    fi
    if window_exec "$x"; then
      if ! window_alive "$id"; then
        sleep 2; [[ -s $STATE/$id.status ]] && return 0
        # dead_window_policy=retier: a scoped row whose window died is re-dispatched
        # ONCE on the retier target (same mark as max-unblock: a second death, or
        # a BLOCK on the retiered run, halts the lane). lead rows still halt — a
        # dead opus window is an operator question, not a tier question.
        if [[ ${ORCH_DEAD_WINDOW:-halt} == retier ]] && scoped_exec "$x" && [[ ! -f $STATE/$id.retiered ]]; then
          log "$lane" "$id: session window dead/gone with NO sentinel (executor $x) — dead_window_policy=retier: re-dispatching once on $ORCH_RETIER_ON_MAX"
          notify normal "$ORCH_NAME lane $lane: $id ($x) died with no sentinel" \
            "Retiered to $ORCH_RETIER_ON_MAX and re-dispatched once (dead_window_policy=retier)."
          echo "$ORCH_RETIER_ON_MAX" > "$CONTROL/EXECUTOR_$id"
          rm -f "$STATE/$id.status" "$STATE/$id.stall_notified" "$STATE/$id.manual_notified" \
                "$STATE/$id.gate_notified" "$STATE/$id.reassign_noted" \
                "$STATE/$id.window" "$STATE/$id.orphan_notified"
          touch "$STATE/$id.retiered"
          return 3
        fi
        log "$lane" "$id: session window dead/gone with NO sentinel — halting lane"
        lane_info "$lane" "HALTED — $id ($x) ended with NO sentinel. Investigate, then: orch reset $id && orch start  (if you closed that window yourself, that is all this means)"
        notify critical "$ORCH_NAME lane $lane HALTED" \
          "$id ($x) ended with no sentinel. tmux attach -t $ORCH_TMUX; investigate; orch reset $id."
        return 1
      fi
      # usage-limit auto-resume: grep the FULL pane — the banner sits mid-pane.
      # Covers opus/sonnet/haiku (rolling "resets HH:MM") and codex (time-based
      # "try again at HH:MM"). claude resumes with "continue"; codex waits for
      # its wall-clock reset to pass, then types "continue the task" + Enter so
      # it actually issues a request (a bare Enter at Codex's idle prompt submits
      # nothing — it dropped the interrupted turn). Never nudged before the
      # server-side cap lifts (a no-op that would just burn the nudge).
      if limit_watched_exec "$x"; then
        pane=$(tmux capture-pane -p -t "$ORCH_TMUX:$id" 2>/dev/null || true)
        if printf '%s' "$pane" | grep -qiE 'usage limit|limit will reset|hit your (usage|session) limit|try again at|codex/settings/usage'; then
          now=$(date +%s); lastn=$(cat "$STATE/$id.limit_nudge" 2>/dev/null || echo 0)
          if (( now - lastn > 900 )); then
            if (( lastn == 0 )); then
              reset_at=$(printf '%s' "$pane" | grep -oiE 'try again at [0-9]{1,2}:[0-9]{2}( ?[AP]M)?' | head -1 | sed -E 's/.*at //')
              if claude_exec "$x"; then
                notify normal "$ORCH_NAME: $id hit the session limit" \
                  "It auto-resumes when the limit resets${reset_at:+ (~$reset_at)}; one ping when it is back."
              else
                notify normal "$ORCH_NAME: $id hit the usage limit" \
                  "Codex auto-resumes when the limit resets${reset_at:+ (~$reset_at)}; one ping when it is back."
              fi
              # Mark notified now so a codex row waiting out a far reset does not
              # re-notify on every poll; the 900s cadence below still governs the
              # actual resume once the reset passes.
              echo "$now" > "$STATE/$id.limit_nudge"
            fi
            if claude_exec "$x" || codex_reset_passed "$pane"; then
              if claude_exec "$x"; then
                tmux send-keys -t "$ORCH_TMUX:$id" "continue" Enter
              else
                tmux send-keys -t "$ORCH_TMUX:$id" "continue the task" Enter
              fi
              echo "$now" > "$STATE/$id.limit_nudge"
              log "$lane" "$id shows usage-limit banner — auto-resume nudge sent (executor $x)"
            fi
          fi
        fi
      fi
      # Auto-seal: a claude agent that printed its required closing line but
      # never wrote the status file (the "said Closed then sat at the prompt"
      # case) is sealed deterministically instead of leaving the row RUNNING.
      # Only the exact `ORCH_CLOSE <id> <VERDICT> — <summary>` token counts;
      # free text is never read as a verdict. The sentinel file (primary close)
      # wins if it already exists.
      if claude_exec "$x" && seal_from_pane "$id"; then
        log "$lane" "$id auto-sealed from its closing line: $(sed -n 2p "$STATE/$id.status" 2>/dev/null)"
        notify normal "$ORCH_NAME: $id auto-closed" "$(sed -n 2p "$STATE/$id.status" 2>/dev/null)"
        tmux send-keys -t "$ORCH_TMUX:$id" $'/exit\n' 2>/dev/null || true
        return 0
      fi
      act=$(tmux display -p -t "$ORCH_TMUX:$id" '#{window_activity}' 2>/dev/null || echo 0)
      now=$(date +%s)
      if (( act > 0 && now - act > ORCH_STALL_SECS )) && [[ ! -f $STATE/$id.stall_notified ]]; then
        notify normal "$ORCH_NAME lane $lane: $id quiet ${ORCH_STALL_SECS}s" \
          "Possibly stalled. tmux attach -t $ORCH_TMUX and check window '$id'."
        log "$lane" "$id quiet for ${ORCH_STALL_SECS}s — stall warning sent (never killed)"
        touch "$STATE/$id.stall_notified"
      fi
    fi
    sleep "$ORCH_POLL"
  done
}

# Fresh scan of the queue each call — live edits are safe.
next_row(){ # lane
  local lane=$1 id deps pause qexec plan rest st
  while read -r id deps pause qexec plan rest; do
    [[ -z ${id:-} || $id == \#* ]] && continue
    st=$(status_of "$id")
    [[ $st == CLOSED ]] && continue
    printf '%s %s %s %s %s %s\n' "$id" "$deps" "$pause" "$qexec" "$plan" "$st"
    return 0
  done < "$ORCH_INSTANCE/queue_${lane}.tsv"
  return 1
}

# --- auto-unblock ----------------------------------------------------------
unblock_attempts(){ cat "$STATE/$1.unblock_n" 2>/dev/null || echo 0; }
# An unblocker's sentinel is read by wait_unblock the moment it exists. A reset
# clears the row's unblock counter, so the next attempt reuses the name
# ub-<row>-1 — and a sentinel left from an EARLIER attempt was read back
# instantly as a fresh verdict (a stale RESOLVED re-dispatched the row on an
# outdated plan). Spent unblocker sentinels are moved aside, never
# deleted: they are the record of what that unblocker decided.
retire_ub_status(){ # file...
  local f ts; ts=$(date '+%Y%m%dT%H%M%S')
  for f in "$@"; do [[ -e $f ]] && mv -f "$f" "$f.consumed-$ts"; done; return 0
}
sheet_init(){ [[ -s $BLOCK_SHEET ]] && return 0
  cat > "$BLOCK_SHEET" <<'HDR'
# BLOCK SHEET — every scoped-row stop and how it was resolved (appended by the watcher and the unblockers)

Read newest-last. `OPEN` rows are being worked by an unblocker session (`ub-<row>-<n>` in the tmux session);
`ESCALATED` rows need the operator. Full analysis per stop: `unblock/<row>-<n>.md`.

| # | when | row | attempt | why it stopped (the executor's own line) | options weighed | chosen + why | what changed | outcome |
|---|------|-----|---------|------------------------------------------|-----------------|--------------|--------------|---------|
HDR
}
sheet_open(){ sheet_init
  local n_rows; n_rows=$(grep -c '^| [0-9]' "$BLOCK_SHEET" 2>/dev/null); n_rows=${n_rows:-0}
  printf '| %s | %s | %s | %s | %s | _unblocker running_ | _unblocker running_ | _unblocker running_ | OPEN |\n' \
    "$((n_rows+1))" "$(date '+%F %H:%M')" "$1" "$2" "$(printf '%s' "$3" | tr '|' '/' | cut -c1-220)" >> "$BLOCK_SHEET"
}
wait_unblock(){ # lane ub
  local lane=$1 ub=$2
  while true; do
    [[ -s $STATE/$ub.status ]] && return 0
    stopped "$lane" && { log "$lane" "STOP while unblocker $ub running"; return 1; }
    if ! window_alive "$ub"; then sleep 2; [[ -s $STATE/$ub.status ]] && return 0
      log "$lane" "$ub: unblocker window died with NO sentinel"; return 1; fi
    sleep "$ORCH_POLL"
  done
}
try_unblock(){ # lane id exec plan -> 0 = row reset (re-dispatch), 1 = halt lane
  local lane=$1 id=$2 x=$3 plan=$4 n reason ub verdict note ubx
  n=$(( $(unblock_attempts "$id") + 1 ))
  reason=$(status_note "$id")
  if (( n > ORCH_MAX_UNBLOCK )); then
    log "$lane" "$id BLOCKED again after $ORCH_MAX_UNBLOCK unblock attempts — retiering to $ORCH_RETIER_ON_MAX"
    notify normal "$ORCH_NAME: $id max-unblock — retiering to $ORCH_RETIER_ON_MAX" "$reason"
    cp -f "$STATE/$id.status" "$STATE/$id.blocked.$((ORCH_MAX_UNBLOCK+1)).status" 2>/dev/null || true
    echo "$ORCH_RETIER_ON_MAX" > "$CONTROL/EXECUTOR_$id"
    rm -f "$STATE/$id.status" "$STATE/$id.stall_notified" "$STATE/$id.manual_notified" \
          "$STATE/$id.gate_notified" "$STATE/$id.reassign_noted" "$STATE/$id.unblock_n" \
          "$STATE/$id.window" "$STATE/$id.orphan_notified"
    # Mark it: if the retiered row blocks AGAIN, the lane halts instead of
    # restarting the unblock cycle. An earlier rig relied on the retier target being
    # a higher tier to end the loop; that is an assumption, not a guarantee,
    # and a same-tier retier target would have looped forever.
    touch "$STATE/$id.retiered"
    log "$lane" "$id retiered $ORCH_RETIER_ON_MAX + reset — re-dispatching (one shot: a further BLOCK halts the lane)"
    return 0
  fi
  echo "$n" > "$STATE/$id.unblock_n"
  cp -f "$STATE/$id.status" "$STATE/$id.blocked.$n.status" 2>/dev/null || true
  sheet_open "$id" "$n" "$reason"
  ub="ub-$id-$n"; ubx="$ORCH_UNBLOCK_EXEC"
  if [[ -e $STATE/$ub.status ]]; then
    retire_ub_status "$STATE/$ub.status"
    log "$lane" "$ub: a sentinel from an earlier unblock cycle was moved aside before dispatch"
  fi
  python3 "$ORCH_LIB/template_engine.py" unblock --id "$id" --lane "$lane" --n "$n" \
    --exec "$x" --plan "$plan" --reason "$reason" >/dev/null || return 1
  ensure_session
  tmux new-window -d -t "$ORCH_TMUX" -n "$ub" -c "$ORCH_ROOT" \
       "ORCH_INSTANCE='$ORCH_INSTANCE' '$ORCH_LIB/orchestrator_core.sh' launch $ub $ubx"
  log "$lane" "$id BLOCKED (attempt $n/$ORCH_MAX_UNBLOCK) — unblocker $ub dispatched ($ubx)"
  lane_info "$lane" "UNBLOCKING $id (attempt $n/$ORCH_MAX_UNBLOCK) — session $ub · sheet: BLOCK_SHEET.md"
  notify low "$ORCH_NAME: $id blocked — auto-unblocker $ub running" "$reason"
  if paste_exec "$ubx"; then
    paste_kick "$ub" || { log "$lane" "$ub kick paste FAILED — halting"; return 1; }
  fi
  wait_unblock "$lane" "$ub" || return 1
  verdict=$(status_of "$ub"); note=$(status_note "$ub")
  log "$lane" "$ub returned $verdict — $note"
  case $verdict in
    RESOLVED)
      tmux kill-window -t "$ORCH_TMUX:$id" 2>/dev/null || true
      rm -f "$STATE/$id.status" "$STATE/$id.stall_notified" "$STATE/$id.manual_notified" \
            "$STATE/$id.reassign_noted" "$STATE/$id.window" "$STATE/$id.orphan_notified"
      log "$lane" "$id reset by unblocker $ub — re-dispatching"
      notify low "$ORCH_NAME: $id unblocked (attempt $n)" "$note"
      return 0 ;;
    *) return 1 ;;
  esac
}

# --- the lane loop ---------------------------------------------------------
run_lane(){
  local lane=$1 id deps pause qexec plan st x row rc
  [[ -f "$ORCH_INSTANCE/queue_${lane}.tsv" ]] || { echo "no queue_${lane}.tsv in $ORCH_INSTANCE"; exit 1; }
  log "$lane" "watcher up (pid $$)"
  lane_info "$lane" "watcher up"
  while true; do
    stopped "$lane" && { lane_info "$lane" "HALTED (STOP)"; log "$lane" "STOP"; return; }
    if ! row=$(next_row "$lane"); then
      lane_info "$lane" "LANE COMPLETE"; log "$lane" "lane complete"
      notify normal "$ORCH_NAME lane $lane COMPLETE" "All queued rows CLOSED."
      return
    fi
    read -r id deps pause qexec plan st <<< "$row"
    if [[ $st == BLOCKED || $st == ESCALATED ]]; then
      lane_info "$lane" "HALTED — $id previously returned $st (review, 'orch reset $id', 'orch start')"
      log "$lane" "halting: $id has prior $st sentinel"
      notify critical "$ORCH_NAME lane $lane HALTED" "$id previously returned $st. Review, then: orch reset $id && orch start"
      return
    fi
    if [[ $st != NONE ]]; then
      lane_info "$lane" "HALTED — $id carries unrecognized sentinel '$st'"
      notify critical "$ORCH_NAME lane $lane HALTED" "$id carries unrecognized sentinel '$st' — investigate."
      return
    fi
    if [[ $pause == 1 && ! -f $CONTROL/GO_$id ]]; then
      lane_info "$lane" "GATE HOLD before $id — release with: orch approve $id"
      if [[ ! -f "$STATE/$id.gate_notified" ]]; then
        touch "$STATE/$id.gate_notified"
        log "$lane" "gate hold before $id (waiting for control/GO_$id)"
        notify normal "$ORCH_NAME lane $lane paused before $id" "Operator gate. Release with: orch approve $id"
      fi
      sleep "$ORCH_POLL"; continue
    fi
    if paused "$lane" && ! window_alive "$id" && [[ ! -s $STATE/$id.status ]]; then
      lane_info "$lane" "PAUSED before $id ('orch night'/'orch resume' to continue · 'orch dispatch $id' to fire one row)"
      sleep "$ORCH_POLL"; continue
    fi
    if ! deps_met "$deps"; then
      lane_info "$lane" "waiting on deps [$deps] for $id"
      sleep "$ORCH_POLL"; continue
    fi
    x=$(resolve_exec "$id" "$qexec")
    if [[ $pause == 1 ]] && window_exec "$x" && ! window_alive "$id" && ! gate_probe "$lane" "$id"; then
      sleep "$ORCH_POLL"; continue
    fi
    if window_exec "$x"; then
      if window_alive "$id"; then
        if window_is_ours "$id"; then
          log "$lane" "adopting already-live window '$id' (watcher restart / manual dispatch)"
        else
          # ORPHAN: a window on this row's name that this rig did not dispatch
          # for the row AS IT NOW STANDS — in practice a finished session left
          # open after `orch reset`. Adopting it is how a lane came to report
          # RUNNING for hours with nothing running. The lane WAITS here rather
          # than lying or halting: close the window and the next pass dispatches
          # for real, with no `orch start` needed.
          lane_info "$lane" "ORPHAN WINDOW '$id' — a session this rig did not dispatch holds that name and is NOT working it. Close it (tmux kill-window -t $ORCH_TMUX:$id) and the lane dispatches within ${ORCH_POLL}s · or claim it: orch adopt $id"
          if [[ ! -f $STATE/$id.orphan_notified ]]; then
            touch "$STATE/$id.orphan_notified"
            log "$lane" "$id: live window is UNSTAMPED (orphan) — refusing to adopt, waiting for the operator"
            notify normal "$ORCH_NAME lane $lane: orphan window '$id'" \
              "A leftover session holds the name '$id' and is not working it. Close it (tmux kill-window -t $ORCH_TMUX:$id) and the lane resumes itself, or claim it: orch adopt $id"
          fi
          sleep "$ORCH_POLL"; continue
        fi
      else
        dispatch "$lane" "$id" "$x" "$plan" || true
      fi
      lane_info "$lane" "RUNNING $id (executor $x$( [[ $(exec_source "$id") == override ]] && echo ', assigned' ))"
    else
      announce_manual "$lane" "$id" "$plan"
    fi
    wait_row "$lane" "$id" "$qexec" "$plan" "$x"; rc=$?
    (( rc == 3 )) && continue   # window died, row retiered — the loop re-dispatches it
    (( rc != 0 )) && return
    st=$(status_of "$id")
    log "$lane" "$id returned $st — $(status_note "$id")"
    case $st in
      CLOSED)
        rm -f "$STATE/$id.gate_notified" "$STATE/$id.manual_notified"
        notify low "$ORCH_NAME lane $lane: $id CLOSED" "$(status_note "$id")" ;;
      BLOCKED)
        if [[ ! -f $STATE/$id.retiered ]] && scoped_exec "$x" && try_unblock "$lane" "$id" "$x" "$plan"; then continue; fi
        lane_info "$lane" "HALTED — $id returned $st (see BLOCK_SHEET.md)"
        notify critical "$ORCH_NAME lane $lane HALTED — $id $st" "$(status_note "$id") · unblock attempts: $(unblock_attempts "$id")"
        return ;;
      ESCALATED)
        lane_info "$lane" "HALTED — $id returned $st"
        notify critical "$ORCH_NAME lane $lane HALTED — $id $st" "$(status_note "$id")"
        return ;;
      *)
        lane_info "$lane" "HALTED — $id wrote unrecognized sentinel '$st'"
        notify critical "$ORCH_NAME lane $lane HALTED" "$id wrote unrecognized sentinel '$st' — investigate."
        return ;;
    esac
  done
}

# --- launch (runs INSIDE the row's tmux window) ----------------------------
launch(){
  local id=$1 x=${2:-$ORCH_EXEC_DEFAULT} flavor f ad
  flavor=$(python3 - "$x" <<'PY'
import sys, os
sys.path.insert(0, os.environ["ORCH_LIB"])
from config_parser import load
print(load().flavor(sys.argv[1]))
PY
)
  f="$PROMPTS/$id.$flavor.md"
  [[ -s $f ]] || { echo "FATAL: no work order $f"; exec bash; }
  ad="$ORCH_LIB/adapters/$(adapter_for "$x").sh"
  [[ -x $ad ]] || { echo "FATAL: no adapter $ad for executor '$x'"; exec bash; }
  cd "$ORCH_ROOT" || exit 1
  exec "$ad" "$id" "$x" "$f"
}

# --- leases (called BY executors) ------------------------------------------
lease(){ # acquire|release <name> <owner>
  local op=$1 name=$2 owner=$3 cur lock="$LEASES/$2.lock"
  case $op in
    acquire)
      # Re-entrant for its owner (pair leases): a row already
      # holding the lease across a span is told so, never left waiting on itself.
      if [[ -d $lock && $(head -1 "$lock/owner" 2>/dev/null) == "$owner" ]]; then
        echo "lease '$name' already held by $owner (re-entrant)"; return 0; fi
      until mkdir "$lock" 2>/dev/null; do
        cur=$(head -1 "$lock/owner" 2>/dev/null || echo '?')
        echo "$(date '+%F %T') lease '$name' held by $cur — $owner waiting"; sleep 30
      done
      printf '%s\n%s\n' "$owner" "$(date '+%F %T')" > "$lock/owner"
      echo "lease '$name' ACQUIRED by $owner" ;;
    release)
      cur=$(head -1 "$lock/owner" 2>/dev/null || echo '')
      [[ $cur == "$owner" ]] || { echo "REFUSED: '$name' owner is '$cur', not '$owner'"; exit 1; }
      rm -rf "$lock"; echo "lease '$name' RELEASED by $owner" ;;
    *) echo "usage: lease acquire|release <name> <owner>"; exit 1 ;;
  esac
}

# --- lane lifecycle --------------------------------------------------------
start_lanes(){
  command -v tmux >/dev/null || { echo "tmux not installed"; exit 1; }
  ensure_session
  local lane win lanes="${1:-$ORCH_LANES}"
  for lane in $lanes; do
    [[ -f "$ORCH_INSTANCE/queue_${lane}.tsv" ]] || { echo "skip lane $lane (no queue file)"; continue; }
    win="watch-$lane"
    if watcher_alive "$lane"; then
      echo "watcher $win already running (pid $(watcher_pids "$lane" | head -1))"
      continue
    fi
    # No watcher PROCESS for this lane, whatever its window looks like. Clear the
    # frozen banner FIRST, so a halt line cannot survive the restart that ends it.
    lane_info "$lane" "watcher starting…"
    if tmux list-windows -t "$ORCH_TMUX" -F '#{window_name}' | grep -qx "$win"; then
      tmux respawn-window -k -t "$ORCH_TMUX:$win" \
        "ORCH_INSTANCE='$ORCH_INSTANCE' '$ORCH_LIB/orchestrator_core.sh' run $lane"
      echo "respawned watcher $win (its window outlived its watcher process)"
    else
      tmux new-window -d -t "$ORCH_TMUX" -n "$win" -c "$ORCH_ROOT" \
        "ORCH_INSTANCE='$ORCH_INSTANCE' '$ORCH_LIB/orchestrator_core.sh' run $lane"
      echo "started watcher $win"
    fi
  done
  echo "Watch live:  tmux attach -t $ORCH_TMUX     (detach: Ctrl-b d)"
  echo "Status:      orch status"
}

find_row(){ # id -> "lane id deps pause qexec plan"
  local id=$1 lane line
  for lane in $ORCH_LANES; do
    line=$(grep -E "^$id[[:space:]]" "$ORCH_INSTANCE/queue_${lane}.tsv" 2>/dev/null | head -1 || true)
    [[ -n $line ]] && { echo "$lane $line"; return 0; }
  done
  return 1
}

assign(){ # id executor|default
  local id=$1 x=$2 lane rid deps pause qexec plan rest st cur
  read -r lane rid deps pause qexec plan rest < <(find_row "$id") || { echo "unknown row '$id'"; exit 1; }
  st=$(status_of "$id")
  [[ $st == CLOSED ]] && { echo "REFUSED: $id is CLOSED — 'orch reset $id' first to re-run under a new executor"; exit 1; }
  if window_alive "$id"; then
    echo "REFUSED: $id has a LIVE session window. A running session is never yanked."
    echo "To retier now: tmux kill-window -t $ORCH_TMUX:$id && orch reset $id && orch assign $id $x"
    exit 1
  fi
  if [[ $x == default ]]; then
    rm -f "$CONTROL/EXECUTOR_$id"; log "$lane" "ASSIGN: $id override cleared -> queue default ($qexec)"
    echo "$id -> queue default ($qexec)."
  else
    valid_exec "$x" || { echo "unknown executor '$x' (have: $ORCH_EXECUTORS, or 'default')"; exit 1; }
    echo "$x" > "$CONTROL/EXECUTOR_$id"; rm -f "$STATE/$id.manual_notified"
    log "$lane" "ASSIGN: $id -> $x"
    cur=$(resolve_exec "$id" "$qexec")
    echo "$id -> $cur. Takes effect at dispatch; a watcher waiting on this row picks it up within ${ORCH_POLL}s."
  fi
}

manual_dispatch(){ # id
  local id=$1 lane rid deps pause qexec plan rest st x
  read -r lane rid deps pause qexec plan rest < <(find_row "$id") || { echo "unknown row '$id'"; exit 1; }
  st=$(status_of "$id")
  [[ $st == CLOSED ]] && { echo "$id already CLOSED — 'orch reset $id' first"; exit 1; }
  window_alive "$id" && { echo "$id is already live (tmux window '$id') — watch it instead"; exit 1; }
  if [[ $pause == 1 && ! -f $CONTROL/GO_$id ]]; then
    echo "$id is behind a approval gate — release it deliberately: orch approve $id"; exit 1; fi
  if ! deps_met "$deps"; then
    if [[ ${FORCE:-0} == 1 ]]; then echo "WARNING: deps [$deps] not all CLOSED — dispatching anyway (FORCE=1)"
    else echo "deps [$deps] not all CLOSED — ladder order is binding. Override: FORCE=1 orch dispatch $id"; exit 1; fi
  fi
  x=$(resolve_exec "$id" "$qexec")
  if [[ $pause == 1 ]] && window_exec "$x" && ! gate_probe "$lane" "$id"; then
    echo "REFUSED: $id's pre-dispatch probe failed — see state/$id.probe.out (declared in gate_probes.tsv)"; exit 1
  fi
  if window_exec "$x"; then
    dispatch "$lane" "$id" "$x" "$plan"
    log "$lane" "MANUAL dispatch of $id (executor $x)"
    echo "dispatched $id (tmux window '$id', executor $x)."
    paused "$lane" && echo "lane $lane auto-chaining is PAUSED — nothing follows $id until 'orch night'/'orch resume'." \
                   || echo "note: the lane $lane watcher will auto-chain when $id closes; 'orch day' prevents that."
  else
    announce_manual "$lane" "$id" "$plan"
    echo "$id announced as a manual work order (prompts/$id.manual.md). Close: orch done $id \"summary\""
  fi
}

mark(){ # done|blocked id [summary]
  local verdict=$1 id=$2; shift 2; local summary="${*:-no summary given}" lane rid deps pause qexec plan rest x
  read -r lane rid deps pause qexec plan rest < <(find_row "$id") || { echo "unknown row '$id'"; exit 1; }
  x=$(resolve_exec "$id" "$qexec")
  case $x in
    hermes|hermesflash|gemini|opencode|manual|mock) ;;
    *) if [[ ${FORCE:-0} == 1 ]]; then
         echo "WARNING: $id runs '$x' (writes its own sentinel) — marking anyway (FORCE=1)"
       else
         echo "REFUSED: $id's executor is '$x' — that session writes its own sentinel."
         echo "If it is truly dead and you are closing it yourself: FORCE=1 orch $verdict $id \"...\""
         exit 1; fi ;;
  esac
  [[ $(status_of "$id") == CLOSED ]] && { echo "$id already CLOSED — nothing to do"; exit 1; }
  if [[ $verdict == done ]]; then
    # A row is never closed CLOSED over its own stop line: the
    # check reads the row folder and its report packs for `HEADS_UNCHANGED no`,
    # `STOP` or `REFUSED` lines; only a signed ruling named in PROGRESS.md
    # (`SIGNED_RULING <id>`) lets it through. No FORCE bypass: close BLOCKED, or
    # get the ruling.
    local cc_rc=0
    python3 "$ORCH_LIB/close_check.py" "$id" || cc_rc=$?
    if (( cc_rc != 0 )); then
      if (( cc_rc == 3 )); then
        echo "REFUSED: $id's evidence carries a stop line (above) and its PROGRESS.md names no signed ruling."
        echo "Close it BLOCKED (orch blocked $id \"…\"), or record the operator's ruling and add 'SIGNED_RULING <ruling-id>' to PROGRESS.md."
      else
        echo "REFUSED: the close check could not run (exit $cc_rc) — a CLOSED is never written unchecked."
      fi
      log "$lane" "CLI close of $id REFUSED by the close check (exit $cc_rc)"
      exit 1
    fi
    ls -d "$ORCH_WORKSPACE/$ORCH_REPORTS/${id}_"* >/dev/null 2>&1 \
      || echo "WARNING: no $ORCH_REPORTS/${id}_* pack found — the next review will likely block on the pack rule. Proceeding."
    printf 'CLOSED\n%s\n' "$summary" > "$STATE/$id.status"
  else
    printf 'BLOCKED\n%s\n' "$summary" > "$STATE/$id.status"
  fi
  log "$lane" "CLI marked $id $( [[ $verdict == done ]] && echo CLOSED || echo BLOCKED ) ($x row) — $summary"
  notify normal "$ORCH_NAME: $id marked $verdict" "$summary"
  echo "$id sentinel written ($verdict). The watcher takes it from here."
}

assignments(){ # [all]
  local lane id deps pause qexec plan rest st x src mark_
  printf '%-12s %-9s %-10s %-9s %s\n' row status executor source gate
  for lane in $ORCH_LANES; do
    while read -r id deps pause qexec plan rest; do
      [[ -z ${id:-} || $id == \#* ]] && continue
      st=$(status_of "$id")
      [[ $st == CLOSED && ${1:-} != all ]] && continue
      x=$(resolve_exec "$id" "$qexec"); src=$(exec_source "$id")
      mark_=$([[ $pause == 1 ]] && { [[ -f $CONTROL/GO_$id ]] && echo "GO_$id ✓" || echo "GO_$id ✗"; } || echo "-")
      printf '%-12s %-9s %-10s %-9s %s\n' "$id" "$st" "$x" "$src" "$mark_"
    done < "$ORCH_INSTANCE/queue_${lane}.tsv"
  done
}

adopt_row(){ # id — claim an already-open window as this rig's session for the row
  local id=$1 lane rid deps pause qexec plan rest
  read -r lane rid deps pause qexec plan rest < <(find_row "$id") || { echo "unknown row '$id'"; exit 1; }
  window_alive "$id" || { echo "no live window named '$id' in session $ORCH_TMUX — nothing to adopt"; exit 1; }
  if window_is_ours "$id"; then echo "$id's live window is already this rig's session — nothing to do"; exit 0; fi
  stamp_window "$id" || { echo "could not read the window's identity — is tmux healthy?"; exit 1; }
  rm -f "$STATE/$id.orphan_notified"
  log "$lane" "operator ADOPTED the live window as $id's session"
  echo "adopted: the open window '$id' now counts as $id's session; the lane waits for its sentinel."
  echo "Only do this for a session that is genuinely WORKING the row. A finished one should be closed instead:"
  echo "  tmux kill-window -t $ORCH_TMUX:$id     # the lane then dispatches a fresh session by itself"
}

reset_row(){
  shopt -s nullglob; retire_ub_status "$STATE"/ub-"$1"-[0-9]*.status; shopt -u nullglob
  rm -f "$STATE/$1.status" "$STATE/$1.stall_notified" "$STATE/$1.manual_notified" \
        "$STATE/$1.gate_notified" "$STATE/$1.reassign_noted" "$STATE/$1.unblock_n" \
        "$STATE/$1.limit_nudge" "$STATE/$1.retiered" \
        "$STATE/$1.window" "$STATE/$1.orphan_notified" "$STATE/$1.probe_failed"
  # The stamp goes with the sentinel ON PURPOSE: whatever session was open for
  # the OLD run of this row is not working the new one, so it must stop counting
  # as proof of work the moment the row is reset.
  echo "reset $1 — restart watchers with 'orch start' (an executor override is KEPT; clear with 'orch assign $1 default')"
}

watch_desktop(){ # open a real desktop terminal viewing the sessions
  command -v gnome-terminal >/dev/null 2>&1 || {
    echo "gnome-terminal not found — use: tmux attach -t $ORCH_TMUX"; exit 1; }
  local grp="view-$$-$RANDOM"
  gnome-terminal --title="$ORCH_NAME sessions — close anytime, sessions unaffected" \
    --geometry=200x50 -- \
    tmux new-session -t "$ORCH_TMUX" -s "$grp" \; set-option destroy-unattached on
  echo "viewer opened (Ctrl-b w = pick window). Open more with 'orch watch'."
}

regen_prompt(){ # id — regenerate a row's work order from the current template
  local id=$1 lane rid deps pause qexec plan rest x
  read -r lane rid deps pause qexec plan rest < <(find_row "$id") || { echo "unknown row '$id'"; exit 1; }
  x=$(resolve_exec "$id" "$qexec")
  python3 "$ORCH_LIB/template_engine.py" prompt --id "$id" --lane "$lane" --exec "$x" --plan "$plan" --force
}

case "${1:-}" in
  start)       shift; start_lanes "${*:-}" ;;
  prompt)      regen_prompt "$2" ;;
  watch)       watch_desktop ;;
  run)         export ORCH_WATCHER_PID=$$; trap 'lane_down "$2"' EXIT; run_lane "$2" ;;
  launch)      launch "$2" "${3:-}" ;;
  dispatch)    manual_dispatch "$2" ;;
  assign)      assign "$2" "$3" ;;
  assignments) assignments "${2:-}" ;;
  done)        mark done "$2" "${@:3}" ;;
  blocked)     mark blocked "$2" "${@:3}" ;;
  approve)     touch "$CONTROL/GO_$2"; rm -f "$STATE/$2.gate_notified"; echo "GO_$2 set" ;;
  reset)       reset_row "$2" ;;
  adopt)       adopt_row "$2" ;;
  lease)       lease "$2" "$3" "$4" ;;
  day)         touch "$CONTROL/PAUSE"; echo "DAY MODE — auto-chaining paused (running sessions unaffected, still babysat)."; echo "Fire rows yourself: orch dispatch <id>" ;;
  night)       rm -f "$CONTROL"/PAUSE "$CONTROL"/PAUSE_*; start_lanes; echo "NIGHT MODE — auto-chaining on." ;;
  pause)       touch "$CONTROL/PAUSE${2:+_$2}"; echo "paused ${2:-ALL}" ;;
  resume)      rm -f "$CONTROL"/PAUSE "$CONTROL"/PAUSE_*; echo resumed ;;
  stop)        touch "$CONTROL/STOP${2:+_$2}"; echo "STOP set — lane halts after the current session" ;;
  unstop)      rm -f "$CONTROL"/STOP "$CONTROL"/STOP_*; echo "STOP cleared" ;;
  notify)      notify "${4:-normal}" "$2" "$3" ;;
  *) echo "orchestrator_core.sh is internal — use bin/orch"; exit 1 ;;
esac
