#!/usr/bin/env bash
# parallel_guard_core.sh — resource leases + a git staging firewall.
#
# It does NOT stop sessions overlapping. It stops them overlapping *on a
# resource*, and it stops one session staging another session's half-finished
# work. Generalized from the earlier per-lane guard; the 70-line
# hand-maintained `case` allowlist is gone — the allowlist is now DERIVED
# (see state_resolver.allowlist).
#
#   orch guard status
#   orch hold   <id> <resource>[,<resource>...] -- <command...>
#   orch commit <id> -m "msg" -- <path>...
#   orch guard start|finish <id>
#   orch guard allowlist <id>
#   orch guard protected                     (the Owner's paths, lib/protected.py)
#   orch guard force-release <resource>      (operator only)
#
# `hold` acquires, runs, and releases EVEN IF the command fails or is killed:
# a crashed run can never leave a lease behind.
#
# Pair leases: a before/after measurement pair must
# hold a read lease on EVERY tree it measures from its first label to its
# second, so no other row can commit into a measured tree between the two.
#   * `hold <id> tree-a,tree-b -- cmd` takes several leases atomically, in
#     sorted order (no lock-order deadlock between two multi-holds);
#   * a lease is RE-ENTRANT for its owner: a row that holds `tree-a` across a
#     span (`orch lease acquire tree-a <id>` … `orch lease release tree-a <id>`,
#     or `guard finish <id>`) passes straight through its own `hold <id> tree-a`
#     and `commit <id>` inside the span; the inner call never releases it.
set -uo pipefail

ORCH_LIB="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
_cfg="$(python3 "$ORCH_LIB/config_parser.py" --sh)" || exit 2
eval "$_cfg"
LEASES="$ORCH_INSTANCE/leases"; RUNNING="$ORCH_INSTANCE/state/running"
mkdir -p "$LEASES" "$RUNNING"
export ORCH_INSTANCE

die(){ echo "REFUSED: $*" >&2; exit 1; }
lease_owner(){ head -1 "$LEASES/$1.lock/owner" 2>/dev/null || true; }
allow_paths(){
  if planning "$1"; then [[ -n $ORCH_REPO_PREFIX ]] && echo "$ORCH_REPO_PREFIX" || echo "."; return; fi
  python3 "$ORCH_LIB/state_resolver.py" --allowlist "$1"
}

# Leases go through lib/leases.py (v2): slots, host-scope resources shared by every
# rig on the machine, stale-holder reclaim, and the same owner files the engine reads.
acquire(){ # resource id -> 0 acquired by this call, 10 already held by <id> (re-entrant)
  python3 "$ORCH_LIB/leases.py" acquire "$2" "$1" --pid $$ --wait
}
release(){ # resource id
  python3 "$ORCH_LIB/leases.py" release "$2" "$1" >/dev/null
  echo "[guard] '$1' RELEASED by $2"
}

# A path is inside an allowlist entry only on a path boundary (v1 compared bare
# prefixes, so `PHASE_A/a1` also admitted `PHASE_A/a10/…`). Entries ending in `_`
# (a row's report-pack prefix, REPORTS/<id>_) or `/` are prefixes by design.
in_allow(){ # path allowlist-lines -> 0 inside
  local rp=$1 a
  while read -r a; do
    [[ -z $a ]] && continue
    if [[ $rp == "$a" || $rp == "$a"/* ]]; then return 0; fi
    if [[ $a == *_ || $a == */ ]] && [[ $rp == "$a"* ]]; then return 0; fi
  done <<< "$2"
  return 1
}
# Planning commits (`orch commit plan:<tag> …`) may stage anything in the
# workspace except the rig's live state; they still pass the scanners and git-lock.
planning(){ [[ $1 == plan:* ]]; }
# v3 (lib/capability.py, guard.capability_check): a process that descends from a session's pane
# IS that session, whatever ORCH_ROW / ORCH_KIND say — an unset or borrowed id is replaced by the
# pane's own before any check reads it, so "operator" means: outside every session of this rig.
CAP_OFF=0
session_identity(){
  local who rc=0
  who=$(python3 "$ORCH_LIB/capability.py" who) || rc=$?
  (( rc == 2 )) && CAP_OFF=1                       # guard.capability_check: false — v2 behaviour
  (( rc == 3 )) && echo "[guard] ⚠  the capability check failed (above) — the environment decides" >&2
  (( rc == 0 )) && [[ -n $who ]] || return 0
  if [[ ${ORCH_ROW:-} != "${who%% *}" ]]; then
    echo "[guard] this process descends from session ${who%% *}'s pane (ORCH_ROW='${ORCH_ROW:-}') — acting as ${who%% *}" >&2
  fi
  ORCH_ROW=${who%% *}; who=${who#* }; ORCH_KIND=${who%% *}
  export ORCH_ROW ORCH_KIND
}
in_session(){ [[ -n ${ORCH_ROW:-} || -n ${ORCH_KIND:-} ]]; }
operator_overrides(){ # the operator's switches refuse inside a session (v2 allowed them: check off)
  local v
  (( CAP_OFF )) && return 0
  for v in ORCH_SCANNER_OVERRIDE ORCH_SHARED_WHOLE; do
    [[ ${!v:-} == 1 ]] && in_session && die "$v=1 is the operator's override; session ${ORCH_ROW:-?} may not use it.
       A commit that needs it is the operator's: say so in PROGRESS.md and block (or escalate)."
  done
  return 0
}
rig_private(){ # repo-relative path -> 0 when it is the instance's live state
  local inst_rel="${ORCH_INSTANCE#"$ORCH_GIT_REPO"/}"
  [[ $inst_rel == "$ORCH_INSTANCE" ]] && return 1
  case $1 in "$inst_rel"/state/*|"$inst_rel"/control/*|"$inst_rel"/leases/*) return 0 ;; esac
  return 1
}

cmd_status(){
  echo "=== sessions currently registered ==="
  shopt -s nullglob; local any=0 f r
  for f in "$RUNNING"/*.run; do
    any=1; printf "  %-12s started %s  pid %s\n" "$(basename "$f" .run)" "$(sed -n 1p "$f")" "$(sed -n 2p "$f")"
  done
  (( any )) || echo "  (none registered)"
  echo; echo "=== resource leases ==="
  python3 "$ORCH_LIB/leases.py" status
}

cmd_start(){
  local id=${1:?usage: guard start <id>} p
  planning "$id" && die "planning ids need no registration — commit with: orch commit $id -m \"msg\" -- <paths>"
  [[ -n "$(allow_paths "$id")" ]] || die "'$id' resolves to an empty allowlist — is it in a queue_*.tsv with a plan path?"
  printf '%s\n%s\n' "$(date '+%F %T')" "$$" > "$RUNNING/$id.run"
  echo "[guard] session '$id' registered."; echo
  cmd_status; echo
  echo "[guard] Your allowlist (nothing else may be staged by this session):"
  while read -r p; do [[ -n $p ]] && echo "    $p"; done < <(allow_paths "$id")
}
cmd_finish(){
  local id=${1:?usage: guard finish <id>} r
  for r in $ORCH_RESOURCES; do release "$r" "$id"; done
  rm -f "$RUNNING/$id.run"
  echo "[guard] session '$id' finished; every lease it held is released."
}

HOLD_ID=""; HOLD_GOT=()
hold_release_all(){ local r; for r in "${HOLD_GOT[@]}"; do release "$r" "$HOLD_ID"; done; HOLD_GOT=(); }
cmd_hold(){ # id resource[,resource...] -- command...
  local id=${1:?} spec=${2:?} r arc=0; shift 2
  [[ "${1:-}" == "--" ]] || die "usage: hold <id> <resource>[,<resource>...] -- <command...>"
  shift
  local -a want=()
  IFS=',' read -ra want <<< "$spec"
  mapfile -t want < <(printf '%s\n' "${want[@]}" | sed '/^$/d' | sort -u)
  (( ${#want[@]} )) || die "no resource given to hold"
  for r in "${want[@]}"; do
    [[ " $ORCH_RESOURCES " == *" $r "* ]] || die "unknown resource '$r' (have: $ORCH_RESOURCES)"
  done
  (( $# )) || die "no command given to hold"
  arc=0
  HOLD_ID=$id; HOLD_GOT=()
  # A signal ENDS the hold: release what was taken, then exit. A handler that
  # returned would let a killed wait loop keep waiting, or a killed command's
  # caller carry on as if it had the leases.
  trap 'hold_release_all' EXIT
  trap 'hold_release_all; exit 130' INT
  trap 'hold_release_all; exit 143' TERM
  # all-or-nothing: nothing is held while waiting for the rest
  local out newly
  out=$(python3 "$ORCH_LIB/leases.py" acquire "$id" "$(IFS=,; echo "${want[*]}")" --pid $$ --wait | tee /dev/stderr) || arc=$?
  newly=$(printf '%s\n' "$out" | sed -n 's/^\[lease\] \(.*\) ACQUIRED by .*/\1/p' | tail -1)
  [[ -n $newly ]] && IFS=',' read -ra HOLD_GOT <<< "$newly"
  if [[ ${arc:-0} != 0 && ${arc:-0} != 10 ]]; then die "could not take ${want[*]}"; fi
  "$@"; local rc=$?
  trap - EXIT INT TERM
  hold_release_all
  echo "[guard] command exited $rc while holding '${want[*]}'"
  return $rc
}

cmd_allowlist(){ allow_paths "${1:?usage: guard allowlist <id>}"; }

cmd_commit(){ # id -m msg -- path...
  local id=${1:?}; shift
  [[ "${1:-}" == "-m" ]] || die "usage: commit <id> -m \"msg\" -- <path>..."
  local msg=$2; shift 2
  [[ "${1:-}" == "--" ]] || die "usage: commit <id> -m \"msg\" -- <path>..."
  shift
  (( $# )) || die "no paths given"
  session_identity
  operator_overrides

  if planning "$id" && [[ -n ${ORCH_ROW:-} ]] && [[ ${ORCH_ROW} != "pl-${id#plan:}-"* ]]; then
    die "planning commits ($id) come from the operator's shell or the planner session of that round (pl-${id#plan:}-<n>), not from session ${ORCH_ROW}"
  fi
  # (a planner session pl-<tag>-<n> commits as plan:<tag>, checked just above)
  if [[ -n ${ORCH_ROW:-} && $id != "$ORCH_ROW" ]] && ! { [[ $ORCH_ROW == ub-* ]] && [[ $ORCH_ROW == "ub-$id-"* ]]; } \
     && ! { planning "$id" && [[ $ORCH_ROW == "pl-${id#plan:}-"* ]]; }; then
    die "session ${ORCH_ROW} may commit only as itself (or as the row it unblocks), never as '$id'"
  fi
  local allow; allow=$(allow_paths "$id")
  [[ -n "$allow" ]] || die "'$id' has no resolvable allowlist"
  # Accept paths written repo-relative OR project-root-relative; compare both.
  local p a ok rp
  local -a rel=()
  for p in "$@"; do
    rp="${p#"$ORCH_ROOT"/}"; rp="${rp#./}"
    # strip a leading root->repo prefix if the caller used a root-relative path
    local repo_from_root="${ORCH_GIT_REPO#"$ORCH_ROOT"/}"
    [[ -n $repo_from_root && $rp == "$repo_from_root"/* ]] && rp="${rp#"$repo_from_root"/}"
    ok=0
    if planning "$id"; then rig_private "$rp" || ok=1; [[ $allow == "." ]] || in_allow "$rp" "$allow" || ok=0
    else in_allow "$rp" "$allow" && ok=1; fi
    (( ok )) || die "'$p' is outside $id's allowlist — staging another session's files is how work gets lost.
       Allowlist:
$(printf '         %s\n' $allow)"
    rel+=( "$rp" )
  done

  # Commits are serialized on ONE declared resource (guard.git_lock).
  local held="$ORCH_GIT_LOCK"
  [[ " $ORCH_RESOURCES " == *" $held "* ]] || die "guard.git_lock '$held' is not in guard.resources — fix orch.yaml"
  local own_lock=0 arc=0
  acquire "$held" "$id" || arc=$?
  (( arc == 0 )) && own_lock=1
  if (( own_lock )); then
    trap 'release "'"$held"'" "'"$id"'"' EXIT
    trap 'release "'"$held"'" "'"$id"'"; exit 130' INT
    trap 'release "'"$held"'" "'"$id"'"; exit 143' TERM
  else
    trap 'exit 130' INT; trap 'exit 143' TERM   # never commit on after a signal
  fi
  cd "$ORCH_GIT_REPO" || die "cannot cd to $ORCH_GIT_REPO"
  local tries=0
  while [[ -f .git/index.lock ]] && (( tries < 20 )); do
    echo "[guard] git index.lock present — waiting"; sleep 3; tries=$((tries+1))
  done
  [[ -f .git/index.lock ]] && die "git index.lock still present after 60s — another commit is stuck"

  # Shared paths (guard.shared_paths) are staged HUNK BY HUNK: only the hunks
  # that name this row go in; every other row's pending edit of the same file
  # stays in the working tree for its own commit (a whole-file add
  # carried another row's half-written entry under this row's message).
  local -a plain=() shared=()
  local sp is_shared
  for rp in "${rel[@]}"; do
    is_shared=0
    for sp in $ORCH_SHARED_PATHS; do
      [[ -n $ORCH_REPO_PREFIX ]] && sp="$ORCH_REPO_PREFIX/$sp"
      [[ $rp == "$sp" ]] && is_shared=1
    done
    (( is_shared )) && shared+=( "$rp" ) || plain+=( "$rp" )
  done
  if (( ${#plain[@]} )); then git add -- "${plain[@]}" || die "git add failed for: ${plain[*]}"; fi
  local hs_rc
  for rp in "${shared[@]}"; do
    hs_rc=0
    if [[ ${ORCH_SHARED_WHOLE:-} == 1 ]] || planning "$id"; then
      echo "[guard] ⚠  ORCH_SHARED_WHOLE=1 — staging the whole of shared path '$rp' (on the record: say so in the message)."
      git add -- "$rp" || hs_rc=2
    else
      python3 "$ORCH_LIB/hunk_stage.py" "$id" "$rp" || hs_rc=$?
    fi
    if (( hs_rc == 4 )); then
      git reset -q; die "shared path '$rp' changed, but no hunk names '$id' — nothing of this row's to stage there; nothing committed, index reset.
       Every entry a row writes into a shared path names the row. Another row's pending edit is never committed under '$id'."
    fi
    (( hs_rc == 0 )) || { git reset -q; die "hunk staging of '$rp' failed (exit $hs_rc) — nothing committed, index reset"; }
  done
  # Compare the REAL name, never the C-quoted one: non-ASCII
  # filenames otherwise fail every pack commit.
  # --no-renames: a staged rename lists its source too (a `git mv` out of a protected or another
  # row's path would otherwise show only its destination)
  local staged s; staged=$(git -c core.quotepath=false diff --cached --name-only --no-renames)
  while IFS= read -r s; do
    [[ -z "$s" ]] && continue
    ok=0
    if planning "$id"; then rig_private "$s" || ok=1; [[ $allow == "." ]] || in_allow "$s" "$allow" || ok=0
    else in_allow "$s" "$allow" && ok=1; fi
    (( ok )) || { git reset -q; die "the index carried '$s', outside $id's allowlist — nothing committed, index reset"; }
  done <<< "$staged"

  # --- protected paths (v3, lib/protected.py) -------------------------------
  # Scorers, gates, keys, caps, phases, the rights table: only the operator's own
  # `plan:` commit (no session identity, never the engine's plan:engine) stages them.
  local -a staged_arr=()
  mapfile -t staged_arr < <(printf '%s\n' "$staged" | sed '/^$/d')
  local prot="" prc=0 operator=0
  planning "$id" && [[ $id != plan:engine ]] && ! in_session && operator=1
  if (( ${#staged_arr[@]} )); then
    prot=$(printf '%s\n' "${staged_arr[@]}" | python3 "$ORCH_LIB/protected.py" match -) || prc=$?
  fi
  local -a prot_paths=()
  [[ -n $prot ]] && mapfile -t prot_paths < <(printf '%s\n' "$prot" | cut -f1)
  if (( prc == 3 )); then
    if (( operator )); then
      # only what the operator named: a protected file someone else left staged is not swept in
      local pp named
      for pp in "${prot_paths[@]}"; do
        named=0
        for rp in "${rel[@]}"; do [[ $pp == "$rp" || $pp == "${rp%/}"/* ]] && named=1; done
        (( named )) || { git reset -q; die "the index carried protected path '$pp', which this commit did not name — nothing committed, index reset (stage the Owner's files by naming them)"; }
      done
      echo "[guard] operator commit of protected path(s): ${prot_paths[*]}"
    else
      git reset -q
      die "'$id' may not stage a protected path (the Owner's: guard.protected_paths + the automatic set) — nothing committed, index reset:
$(printf '%s\n' "$prot" | sed 's/^/         /; s/\t/   (pattern: /; s/$/)/')
       Only the operator's own plan: commit changes these; a row that needs one changed escalates (Route A)."
    fi
  elif (( prc != 0 )); then
    git reset -q; die "the protected-path check failed (exit $prc) — nothing committed, index reset"
  fi

  # --- pluggable compliance scanners -------------------------------------
  # Each configured scanner is called as:  <scanner> --check <file>...
  # over COPIES of the staged blobs (content, not paths). Non-zero exit =
  # refuse. This is where a project puts its private / secret /
  # licence checks; the framework ships none.
  local hit=0
  if [[ -n "${ORCH_SCANNERS// }" ]]; then
    local scan_dir; scan_dir=$(mktemp -d)
    local -a scan_paths=()
    while IFS= read -r s; do
      [[ -z "$s" ]] && continue
      local flat="${s//\//__}"
      git show ":$s" > "$scan_dir/$flat" 2>/dev/null && scan_paths+=( "$scan_dir/$flat" )
    done <<< "$staged"
    if (( ${#scan_paths[@]} )); then
      local sc out rc
      for sc in $ORCH_SCANNERS; do
        [[ $sc == /* ]] || sc="$ORCH_INSTANCE/$sc"
        [[ -x $sc || $sc == *.py ]] || { echo "[guard] scanner '$sc' not executable — treating as a REFUSAL"; hit=1; continue; }
        # NB: capture the scanner's own status, never a pipeline's last stage.
        if [[ $sc == *.py ]]; then out=$(python3 "$sc" --check "${scan_paths[@]}" 2>&1); rc=$?
        else out=$("$sc" --check "${scan_paths[@]}" 2>&1); rc=$?; fi
        printf '%s\n' "$out" | sed "s|$scan_dir/||g; s|__|/|g"
        (( rc == 0 )) || hit=1
      done
    fi
    rm -rf "$scan_dir"
  fi
  if (( hit )); then
    if [[ "${ORCH_SCANNER_OVERRIDE:-}" == "1" ]]; then
      echo "[guard] ⚠  ORCH_SCANNER_OVERRIDE=1 — committing the flagged content ANYWAY."
      echo "    This is a decision on the record; state it in the commit message."
    else
      git reset -q
      die "staged content was refused by a compliance scanner (above) — nothing committed, index reset.
       A legitimate exception is the operator's: ORCH_SCANNER_OVERRIDE=1 from the operator's shell and a sentence in the commit message."
    fi
  fi

  git commit -q -m "$msg" && echo "[guard] committed: $(git log --oneline -1)"
  local rc=$?
  # Every operator plan: commit is recorded: the close check never charges it to a row, and an
  # Owner ruling committed this way is one a protected-path close may cite (lib/protected.py).
  if (( rc == 0 && operator )); then
    python3 "$ORCH_LIB/protected.py" record "$(git rev-parse HEAD)" "$id" ${prot_paths[@]+"${prot_paths[@]}"} \
      || echo "[guard] ⚠  could not record this operator commit in state/protected_commits.jsonl"
  fi
  trap - EXIT INT TERM
  (( own_lock )) && release "$held" "$id"
  return $rc
}

cmd_force_release(){
  local r=${1:?usage: guard force-release <resource>} d
  session_identity
  in_session && die "\`orch guard force-release\` is the operator's call; session ${ORCH_ROW:-?} may not run it"
  python3 "$ORCH_LIB/leases.py" status | grep -F " $r " || true
  echo "This is the operator's call, not an executor's. Ctrl-C now if that holder is alive."
  sleep 5
  for d in "$LEASES/$r.lock" "$LEASES/$r#"*.lock "$HOME/.local/state/orch/leases/$r.lock" "$HOME/.local/state/orch/leases/$r#"*.lock; do
    [[ -d $d ]] && rm -rf "$d" && echo "[guard] '$(basename "$d")' force-released."
  done
}

case "${1:-}" in
  status)        cmd_status ;;
  start)         cmd_start "${2:-}" ;;
  finish)        cmd_finish "${2:-}" ;;
  allowlist)     cmd_allowlist "${2:-}" ;;
  protected)     python3 "$ORCH_LIB/protected.py" list ;;
  hold)          shift; cmd_hold "$@" ;;
  commit)        shift; cmd_commit "$@" ;;
  force-release) cmd_force_release "${2:-}" ;;
  *) sed -n '2,18p' "$0"; exit 1 ;;
esac
