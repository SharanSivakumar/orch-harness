#!/usr/bin/env bash
# closing.sh — deterministically seal a row from its agent's declared closing line.
#
# Sourced by the claude adapter, which seals a session that exits cleanly without a
# claim. The engine seals idle sessions itself (lib/engine.py try_seal) with the same
# rule. Depends only on ORCH_TMUX and ORCH_INSTANCE being exported, plus tmux.
#
# Closing protocol (enforced by the work-order templates): the agent's FINAL
# message must end with, on its own line, exactly
#
#     ORCH_CLOSE <id> CLOSED|BLOCKED|ROUTE_A — <one-line summary>
#
# A row may carry its outcome on the verdict (v3): `CLOSED:nogo` (go|nogo|held|fail|infeasible) is
# sealed as CLOSED with line 3 `OUTCOME nogo`, exactly as `orch done --outcome nogo`.
#
# The sentinel file (state/<id>.status) remains the PRIMARY close. This line is
# a second, machine-readable close path for the two failure cases the file path
# misses: a session that says it is done and then sits idle at the prompt, or
# one that exits cleanly without writing the file.
#
# Safety: ONLY the exact token line is ever read as a verdict. Free text like
# "Closed." or "All done!" is deliberately ignored — the verdict (CLOSED vs
# BLOCKED vs ROUTE_A) can never be guessed from prose, and this rig never
# fabricates a close. The verdict captured here is the agent's own declared one.

closing_line(){ # id [pane-text] -> last matching closing line, else empty
  local id=$1 txts=${2:-}
  [[ -z $txts ]] && txts=$(tmux capture-pane -p -J -t "=${ORCH_TMUX:?ORCH_TMUX not set}:=$id" 2>/dev/null)
  # A line still carrying an angle-bracket placeholder (`<the same one-sentence
  # summary>`) is the work order's own instruction, never a verdict. The TUI can
  # render the prompt's code span WITHOUT its backticks, so the template line
  # may sit indented at column 0 just like a real closing line.
  printf '%s\n' "$txts" \
    | grep -oE "^[[:space:]]*ORCH_CLOSE[[:space:]]+${id}[[:space:]]+(CLOSED|BLOCKED|ROUTE_A|RESOLVED|ESCALATE|ACCEPT|REJECT|PASS|FIX)(:[[:blank:]]*[[:alpha:]][[:alnum:]_-]*)?([^[:alnum:]_][^[:cntrl:]]*|$)" \
    | grep -vE '<[[:alpha:]][^<>]*>' \
    | tail -1
}

closing_verdict(){ # closing-line -> CLOSED|BLOCKED|ROUTE_A, else empty
  local l=$1
  [[ -n $l ]] && printf '%s\n' "$l" | grep -oE '(CLOSED|BLOCKED|ROUTE_A|RESOLVED|ESCALATE|ACCEPT|REJECT|PASS|FIX)' | head -1
}

# The outcome rides on CLOSED only: `CLOSED:<token>` (any token; an unknown one counts as negative)
# or `CLOSED: <go|nogo|no-go|held|fail|infeasible>` (with a space, only these words — prose after a colon is
# never an outcome). Same rule as lib/engine.py close_pattern.
_OC_TIGHT='CLOSED:([[:alpha:]][[:alnum:]_-]*)'
_OC_SPACED='CLOSED:[[:blank:]]+([Nn][Oo][-_]?[Gg][Oo]|[Gg][Oo]|[Hh][Ee][Ll][Dd]|[Ff][Aa][Ii][Ll]|[Ii][Nn][Ff][Ee][Aa][Ss][Ii][Bb][Ll][Ee])([^[:alnum:]_-].*)?$'

closing_outcome(){ # closing-line verdict -> the outcome after CLOSED: (lower-case, '-'/'_' dropped), else empty
  local l=$1 verd=$2 head='^[[:space:]]*ORCH_CLOSE[[:space:]]+[^[:space:]]+[[:space:]]+'
  [[ $verd == CLOSED ]] || return 0
  { printf '%s\n' "$l" | sed -nE "s/${head}${_OC_TIGHT}.*/\1/p"
    printf '%s\n' "$l" | sed -nE "s/${head}${_OC_SPACED}/\1/p"; } | head -1 | tr -d '_-' | tr '[:upper:]' '[:lower:]'
}

closing_summary(){ # closing-line verdict -> one-line summary (may be empty)
  local l=$1 verd=$2 oc=""
  if [[ $verd == CLOSED && -n $(closing_outcome "$l" CLOSED) ]]; then oc=':[[:blank:]]*[[:alnum:]_-]+'; fi
  printf '%s\n' "$l" \
    | sed -E "s/^[[:space:]]*ORCH_CLOSE[[:space:]]+[^[:space:]]+[[:space:]]+${verd}${oc}[[:space:]]*(—|-)?[[:space:]]*//"
}

# Seal a row from its closing line in the live pane. id ; returns 0 only if it
# WROTE a sentinel; 1 if there was nothing to do (already closed / no verdict).
seal_from_pane(){ # id -> wrote?
  local id=$1 cl verd oc f
  f="$ORCH_INSTANCE/state/$id.status"
  [[ -s $f ]] && return 1                # already closed — never overwrite
  # only the attempt the engine has open: a pane of a refused/rejected attempt never re-seals
  grep -q '"open": true' "$ORCH_INSTANCE/state/$id.dispatch.json" 2>/dev/null || return 1
  case $id in
    rv-*) [[ $(wc -c < "$ORCH_INSTANCE/reviews/${id#rv-}.md" 2>/dev/null || echo 0) -ge 40 ]] || return 1 ;;
  esac
  cl=$(closing_line "$id") || true
  verd=$(closing_verdict "$cl")
  [[ -z $verd ]] && return 1             # no declared verdict — leave it
  oc=$(closing_outcome "$cl" "$verd")
  printf '%s\n%s\n%s' "$verd" "$(closing_summary "$cl" "$verd")" "${oc:+OUTCOME $oc
}" > "$f"
  echo "sealed $id as $verd from its closing line"
  return 0
}
