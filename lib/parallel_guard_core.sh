#!/usr/bin/env bash
# parallel_guard_core.sh — resource leases + a git staging firewall.
#
# It does NOT stop sessions overlapping. It stops them overlapping *on a
# resource*, and it stops one session staging another session's half-finished
# work. Generalized from an earlier guard; the 70-line
# hand-maintained `case` allowlist is gone — the allowlist is now DERIVED
# (see state_resolver.allowlist).
#
#   orch guard status
#   orch hold   <id> <resource>[,<resource>...] -- <command...>
#   orch commit <id> -m "msg" -- <path>...
#   orch guard start|finish <id>
#   orch guard allowlist <id>
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
allow_paths(){ python3 "$ORCH_LIB/state_resolver.py" --allowlist "$1"; }

acquire(){ # resource id -> 0 acquired by this call, 10 already held by <id> (re-entrant)
  local r=$1 id=$2 waited=0 lock="$LEASES/$1.lock"
  if [[ -d $lock && $(lease_owner "$r") == "$id" ]]; then
    echo "[guard] '$r' already held by $id — re-entrant, left held for the enclosing span"
    return 10
  fi
  until mkdir "$lock" 2>/dev/null; do
    echo "$(date '+%F %T') [guard] '$r' held by $(lease_owner "$r") — $id waiting ${waited}s"
    sleep 15 & wait $!; waited=$((waited+15))   # interruptible: a signal is handled at once
    if (( waited % 300 == 0 )); then
      echo "[guard] still waiting after ${waited}s. If the holder died:"
      echo "        orch guard status — a stale lease is released with"
      echo "        orch guard force-release $r, and only by the operator."
    fi
  done
  printf '%s\n%s\n%s\n' "$id" "$(date '+%F %T')" "$$" > "$lock/owner"
  echo "[guard] '$r' ACQUIRED by $id"
}
release(){ # resource id
  local r=$1 id=$2 cur; cur=$(lease_owner "$r")
  [[ -z "$cur" ]] && return 0
  [[ "$cur" == "$id" ]] || { echo "[guard] not releasing '$r' — owner is '$cur', not '$id'" >&2; return 0; }
  rm -rf "${LEASES:?}/$r.lock"; echo "[guard] '$r' RELEASED by $id"
}

cmd_status(){
  echo "=== sessions currently registered ==="
  shopt -s nullglob; local any=0 f r
  for f in "$RUNNING"/*.run; do
    any=1; printf "  %-12s started %s  pid %s\n" "$(basename "$f" .run)" "$(sed -n 1p "$f")" "$(sed -n 2p "$f")"
  done
  (( any )) || echo "  (none registered)"
  echo; echo "=== resource leases ==="
  for r in $ORCH_RESOURCES; do
    if [[ -d "$LEASES/$r.lock" ]]; then
      printf "  %-14s HELD by %-12s since %s\n" "$r" "$(sed -n 1p "$LEASES/$r.lock/owner")" "$(sed -n 2p "$LEASES/$r.lock/owner")"
    else printf "  %-14s free\n" "$r"; fi
  done
}

cmd_start(){
  local id=${1:?usage: guard start <id>} p
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
  local id=${1:?} spec=${2:?} r arc; shift 2
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
  HOLD_ID=$id; HOLD_GOT=()
  # A signal ENDS the hold: release what was taken, then exit. A handler that
  # returned would let a killed wait loop keep waiting, or a killed command's
  # caller carry on as if it had the leases.
  trap 'hold_release_all' EXIT
  trap 'hold_release_all; exit 130' INT
  trap 'hold_release_all; exit 143' TERM
  for r in "${want[@]}"; do
    arc=0; acquire "$r" "$id" || arc=$?
    (( arc == 0 )) && HOLD_GOT+=( "$r" )
  done
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
    while read -r a; do [[ -n $a && $rp == "$a"* ]] && ok=1; done <<< "$allow"
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
    if [[ ${ORCH_SHARED_WHOLE:-} == 1 ]]; then
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
  local staged s; staged=$(git -c core.quotepath=false diff --cached --name-only)
  while IFS= read -r s; do
    [[ -z "$s" ]] && continue
    ok=0
    while read -r a; do [[ -n $a && $s == "$a"* ]] && ok=1; done <<< "$allow"
    (( ok )) || { git reset -q; die "the index carried '$s', outside $id's allowlist — nothing committed, index reset"; }
  done <<< "$staged"

  # --- pluggable compliance scanners -------------------------------------
  # Each configured scanner is called as:  <scanner> --check <file>...
  # over COPIES of the staged blobs (content, not paths). Non-zero exit =
  # refuse. This is where a project puts its client-material / secret /
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
       A legitimate exception needs ORCH_SCANNER_OVERRIDE=1 and a sentence in the commit message."
    fi
  fi

  git commit -q -m "$msg" && echo "[guard] committed: $(git log --oneline -1)"
  local rc=$?
  trap - EXIT INT TERM
  (( own_lock )) && release "$held" "$id"
  return $rc
}

cmd_force_release(){
  local r=${1:?usage: guard force-release <resource>}
  echo "Holder was: $(lease_owner "$r")"
  echo "This is the operator's call, not an executor's. Ctrl-C now if that holder is alive."
  sleep 5; rm -rf "${LEASES:?}/$r.lock"; echo "[guard] '$r' force-released."
}

case "${1:-}" in
  status)        cmd_status ;;
  start)         cmd_start "${2:-}" ;;
  finish)        cmd_finish "${2:-}" ;;
  allowlist)     cmd_allowlist "${2:-}" ;;
  hold)          shift; cmd_hold "$@" ;;
  commit)        shift; cmd_commit "$@" ;;
  force-release) cmd_force_release "${2:-}" ;;
  *) sed -n '2,18p' "$0"; exit 1 ;;
esac
