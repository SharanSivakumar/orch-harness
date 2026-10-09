#!/usr/bin/env bash
# closing.sh — deterministically seal a row from its agent's declared closing line.
#
# Sourced by orchestrator_core.sh (the lane watcher) AND by the claude adapter
# (which runs in a fresh process), so both seal exactly the same way. Depends
# only on ORCH_TMUX and ORCH_INSTANCE being exported, plus a live tmux session.
#
# Closing protocol (enforced by the work-order templates): the agent's FINAL
# message must end with, on its own line, exactly
#
#     ORCH_CLOSE <id> CLOSED|BLOCKED|ESCALATED — <one-line summary>
#
# The sentinel file (state/<id>.status) remains the PRIMARY close. This line is
# a second, machine-readable close path for the two failure cases the file path
# misses: a session that says it is done and then sits idle at the prompt, or
# one that exits cleanly without writing the file.
#
# Safety: ONLY the exact token line is ever read as a verdict. Free text like
# "Closed." or "All done!" is deliberately ignored — the verdict (CLOSED vs
# BLOCKED vs ESCALATED) can never be guessed from prose, and this rig never
# fabricates a close. The verdict captured here is the agent's own declared one.

closing_line(){ # id [pane-text] -> last matching closing line, else empty
  local id=$1 txts=${2:-}
  [[ -z $txts ]] && txts=$(tmux capture-pane -p -t "${ORCH_TMUX:?ORCH_TMUX not set}:$id" 2>/dev/null)
  # A line still carrying an angle-bracket placeholder (`<the same one-sentence
  # summary>`) is the work order's own instruction, never a verdict. The TUI can
  # render the prompt's code span WITHOUT its backticks, so the template line
  # may sit indented at column 0 just like a real closing line.
  printf '%s\n' "$txts" \
    | grep -oE "^[[:space:]]*ORCH_CLOSE[[:space:]]+${id}[[:space:]]+(CLOSED|BLOCKED|ESCALATED)[[:space:]]*[^[:cntrl:]]*" \
    | grep -vE '<[[:alpha:]][^<>]*>' \
    | tail -1
}

closing_verdict(){ # closing-line -> CLOSED|BLOCKED|ESCALATED, else empty
  local l=$1
  [[ -n $l ]] && printf '%s\n' "$l" | grep -oE '(CLOSED|BLOCKED|ESCALATED)' | head -1
}

closing_summary(){ # closing-line verdict -> one-line summary (may be empty)
  local l=$1 verd=$2
  printf '%s\n' "$l" \
    | sed -E "s/^ORCH_CLOSE[[:space:]]+[^[:space:]]+[[:space:]]+${verd}[[:space:]]*(—|-)?[[:space:]]*//"
}

# Seal a row from its closing line in the live pane. id ; returns 0 only if it
# WROTE a sentinel; 1 if there was nothing to do (already closed / no verdict).
seal_from_pane(){ # id -> wrote?
  local id=$1 cl verd f
  f="$ORCH_INSTANCE/state/$id.status"
  [[ -s $f ]] && return 1                # already closed — never overwrite
  cl=$(closing_line "$id") || true
  verd=$(closing_verdict "$cl")
  [[ -z $verd ]] && return 1             # no declared verdict — leave it
  printf '%s\n%s\n' "$verd" "$(closing_summary "$cl" "$verd")" > "$f"
  echo "sealed $id as $verd from its closing line"
  return 0
}
