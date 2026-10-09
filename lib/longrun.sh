#!/usr/bin/env bash
# longrun.sh — start a long job the way every row must, and check a venv before using or arming it.
#
#   orch longrun <logfile> -- <command...>
#       Starts <command> detached in its own session (`setsid nohup`), stdout+stderr to <logfile>,
#       stdin from /dev/null. The CHILD writes its own PID to <logfile minus .log>.pid before it
#       execs the command, so the recorded PID is the job's real PID — never `$!`, which names the
#       `setsid` wrapper when setsid has to fork (the recorded PID exited at once and the job
#       ran on under another). Prints the PID and the two handover lines:
#           tail -f <logfile>
#           ps -p <pid> -o pid=,etime=,args=       (liveness — never a self-matching `pgrep -f`)
#       Refuses (exit 3) when the PID file already exists: a step is never re-launched over it.
#       With --venv <dir> before the logfile, the venv check below must print CLEAR first.
#
#   orch venvcheck <venv-dir>
#       Before RUNNING anything from a shared venv, and before ARMING one:
#         ARMED  (exit 3) — the venv carries an arming marker (<venv>/*.active, e.g. a run guard's
#                           active configuration): another row's instrumented run owns it; anything
#                           started from it inherits that run's guard and pollutes its evidence.
#         BUSY   (exit 4) — a process other than the caller lives in it (its argv runs from the
#                           venv, or VIRTUAL_ENV names it): never arm it now; wait until it is gone.
#         CLEAR  (exit 0) — neither.
#       Prints counts, marker file NAMES and PIDs only.
set -u

venvcheck(){ # venv -> prints one verdict line; 0 CLEAR, 3 ARMED, 4 BUSY, 64 usage
  local v=${1:-} real markers=() m pid args ve self=$$ parent=$PPID busy=()
  [[ -n $v && -d $v ]] || { echo "usage: orch venvcheck <venv-dir> (no such directory: '${v}')"; return 64; }
  real=$(readlink -f "$v")
  shopt -s nullglob; for m in "$real"/*.active; do markers+=( "$(basename "$m")" ); done; shopt -u nullglob
  while read -r pid args; do
    [[ $pid == "$self" || $pid == "$parent" ]] && continue
    if [[ $args == *"$real/"* || $args == *"$v/"* ]]; then busy+=( "$pid" ); continue; fi
    if [[ -r /proc/$pid/environ ]]; then
      ve=$(tr '\0' '\n' 2>/dev/null < "/proc/$pid/environ" | sed -n 's/^VIRTUAL_ENV=//p' | head -1)
      [[ -n $ve && $(readlink -f "$ve" 2>/dev/null) == "$real" ]] && busy+=( "$pid" )
    fi
  done < <(ps -eo pid=,args=)
  if (( ${#markers[@]} )); then
    echo "VENV_CHECK $real armed=${#markers[@]} (${markers[*]}) procs=${#busy[@]} VERDICT ARMED — another run owns this venv; do not start anything from it"
    return 3
  fi
  if (( ${#busy[@]} )); then
    echo "VENV_CHECK $real armed=0 procs=${#busy[@]} (pids ${busy[*]}) VERDICT BUSY — a foreign process lives in it; never arm it now"
    return 4
  fi
  echo "VENV_CHECK $real armed=0 procs=0 VERDICT CLEAR"
  return 0
}

longrun(){ # [--venv dir] logfile -- command...
  local venv="" log pidf pid i
  if [[ ${1:-} == --venv ]]; then venv=${2:?--venv needs a directory}; shift 2; fi
  log=${1:-}; shift || true
  [[ -n $log && ${1:-} == -- ]] || { echo "usage: orch longrun [--venv <dir>] <logfile> -- <command...>"; return 64; }
  shift
  (( $# )) || { echo "usage: orch longrun [--venv <dir>] <logfile> -- <command...>"; return 64; }
  if [[ -n $venv ]]; then venvcheck "$venv" || { echo "REFUSED: the venv check did not print CLEAR"; return 3; }; fi
  pidf="${log%.log}.pid"; [[ $pidf == "$log" ]] && pidf="$log.pid"
  [[ -e $pidf ]] && { echo "REFUSED: $pidf exists (a step is never re-launched over its PID file)"; return 3; }
  mkdir -p "$(dirname "$log")"
  # The child records ITS OWN pid ($$ of the bash that then execs the command),
  # so the file names the job itself whatever setsid does.
  setsid nohup bash -c 'echo $$ > "$0"; exec "$@"' "$pidf" "$@" > "$log" 2>&1 < /dev/null &
  for i in $(seq 1 50); do [[ -s $pidf ]] && break; sleep 0.1; done
  [[ -s $pidf ]] || { echo "FAILED: the job wrote no PID file within 5 s — see $log"; return 1; }
  pid=$(head -1 "$pidf")
  echo "LONGRUN pid=$pid log=$log pidfile=$pidf"
  echo "  follow:   tail -f $log"
  echo "  liveness: ps -p $pid -o pid=,etime=,args=     (never a self-matching pgrep -f)"
}

case "${1:-}" in
  venvcheck) shift; venvcheck "$@" ;;
  longrun)   shift; longrun "$@" ;;
  *) sed -n '2,23p' "$0"; exit 64 ;;
esac
