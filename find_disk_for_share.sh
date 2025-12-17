#!/usr/bin/env bash
set -euo pipefail

# Initialize globals early to be safe under 'set -u'
IS_DIR_TARGET=0
FINDDISK_COPY_LABEL="${FINDDISK_COPY_LABEL:-UNBALANCED}"

# =====================================================================
#  find_disk_for_share.sh — find which physical mount(s) hold a path
#  Useful for Unraid + Unbalanced: tell me which /mnt/diskX path(s) match
#
#  Usage:
#    find_disk_for_share.sh [--help] [--recursive] <path>
# =====================================================================

# ----------------------------- vibe check -----------------------------
is_tty() { [[ -t 1 ]]; }

# Colors (TTY only)
RESET=""; BOLD=""; DIM=""
RED=""; GRN=""; YLW=""; BLU=""; MAG=""; CYN=""; WHT=""
if is_tty; then
  RESET=$'\e[0m'; BOLD=$'\e[1m'; DIM=$'\e[2m'
  RED=$'\e[31m'; GRN=$'\e[32m'; YLW=$'\e[33m'; BLU=$'\e[34m'
  MAG=$'\e[35m'; CYN=$'\e[36m'; WHT=$'\e[37m'
fi

ts() { date +"%H:%M:%S"; }

die() { echo "${RED}${BOLD}[$(ts)] FAIL $*${RESET}" >&2; exit 2; }
warn(){ echo "${YLW}${BOLD}[$(ts)] WARN  $*${RESET}" >&2; }
info(){ echo "${CYN}${BOLD}[$(ts)] INFO  $*${RESET}"; }
ok()  { echo "${GRN}${BOLD}[$(ts)] OK    $*${RESET}"; }

# -------------------------- box UI (drip) -----------------------------
use_unicode() {
  [[ "${LANG:-}" == *UTF-8* ]] || [[ "${LC_ALL:-}" == *UTF-8* ]] || [[ "${LC_CTYPE:-}" == *UTF-8* ]]
}

if use_unicode; then
  HORIZ="─"; VERT="│"; TL="┌"; TR="┐"; BL="└"; BR="┘"; SEP="├"; SEP2="┤"
else
  HORIZ="-"; VERT="|"; TL="+"; TR="+"; BL="+"; BR="+"; SEP="+"; SEP2="+"
fi

term_cols() {
  if command -v tput >/dev/null 2>&1; then
    tput cols 2>/dev/null || echo "${COLUMNS:-120}"
  else
    echo "${COLUMNS:-120}"
  fi
}
TERM_COLS="$(term_cols)"
(( TERM_COLS < 60 )) && TERM_COLS=60
BOX_INNER_MAX="${FINDDISK_WIDTH:-$((TERM_COLS-4))}"

elide_middle() {
  local s="$1" max="$2"
  (( max < 10 )) && { echo "${s:0:max}"; return; }
  if (( ${#s} <= max )); then
    echo "$s"
  else
    local keep_left=$(( (max-1)/2 ))
    local keep_right=$(( max-1-keep_left ))
    echo "${s:0:keep_left}…${s: -keep_right}"
  fi
}

# elide_end: preserve the end of filenames/paths which often contain unique info
elide_end() {
  local s="$1" max="$2"
  (( max < 4 )) && { echo "${s:0:max}"; return; }
  if (( ${#s} <= max )); then
    echo "$s"
  else
    echo "${s:0:max-1}…"
  fi
}

box_print() {
  local title="$1"; shift || true
  local lines=("$@")

  local maxlen=${#title}
  local l
  for l in "${lines[@]}"; do
    (( ${#l} > maxlen )) && maxlen=${#l}
  done
  (( maxlen > BOX_INNER_MAX )) && maxlen=$BOX_INNER_MAX

  local fill
  printf -v fill '%*s' $((maxlen+2)) ""
  fill=${fill// /$HORIZ}

  echo -e "${MAG}${BOLD}${TL}${fill}${TR}${RESET}"
  printf "%b %s %b\n" "${MAG}${BOLD}${VERT}${RESET}" \
    "$(printf "%-*s" "$maxlen" "$(elide_middle "$title" "$maxlen")")" \
    "${MAG}${BOLD}${VERT}${RESET}"
  echo -e "${MAG}${BOLD}${SEP}${fill}${SEP2}${RESET}"

  for l in "${lines[@]}"; do
    # Use end-ellipsis for mapping/listing lines (title starts with "Mapping:") so
    # the unique tail (episode numbers, extensions) remains visible. Otherwise
    # use middle-ellipsis to keep context on both sides.
    if [[ "$title" == Mapping:* ]]; then
      l="$(elide_end "$l" "$maxlen")"
    else
      l="$(elide_middle "$l" "$maxlen")"
    fi
    printf "%b %s %b\n" "${MAG}${BOLD}${VERT}${RESET}" \
      "$(printf "%-*s" "$maxlen" "$l")" \
      "${MAG}${BOLD}${VERT}${RESET}"
  done

  echo -e "${MAG}${BOLD}${BL}${fill}${BR}${RESET}"
}

# Print full copyable lines for the paste boxes (un-elided, optionally with trailing / for dirs)
print_copy_lines() {
  local label="$1"; shift || true
  local paths=("$@")
  (( ${#paths[@]} == 0 )) && return 0

  for p in "${paths[@]}"; do
    # Normalize trailing slash when the target is a directory
    if [[ "$IS_DIR_TARGET" -eq 1 && "${p: -1}" != "/" ]]; then
      p="${p}/"
    fi
    if is_tty; then
      printf '%b%s%b %s\n' "${WHT}${BOLD}" "${label}:" "${RESET}" "$p"
    else
      printf '%s %s\n' "${label}:" "$p"
    fi
  done
}

# ------------------------------ args ----------------------------------
help() {
  cat <<EOF
${BOLD}find_disk_for_share.sh${RESET} — find which physical mount(s) contain a path.

${BOLD}Usage:${RESET}
  $(basename "$0") [--help] [--recursive] [--winner-only] [--limit N] [--include-dirs] <path>

${BOLD}Options:${RESET}
  --help        Show this help
  --recursive   Scan recursively under the directory (default: only immediate children)
  --winner-only Print only the winning /mnt/diskX/... path and exit (for scripting)
  --limit N     Limit mapping samples to the first N entries (default: 50)
  --include-dirs  Include directories in listings (by default we count files only)
  --verbose|-v   Show diagnostic output (Scan, per-disk samples, df)
  --raw          Print only the unbalanced paths (one per line) for scripting

${BOLD}Tip:${RESET}
  Give it /mnt/user/... OR /mnt/diskN/... OR /mnt/cache*... — it will still search all mounts.
EOF
}

RECURSIVE=0
WINNER_ONLY=0
VERBOSE=${FINDDISK_VERBOSE:-0}
RAW=0
LIMIT=${FINDDISK_LIMIT:-50}
INCLUDE_DIRS=0
ARGS=()

# Lightweight arg parsing (supports --limit N or --limit=N)
while (( "$#" )); do
  a="$1"
  case "$a" in
    --help|-h) help; exit 0 ;;
    --recursive|-r) RECURSIVE=1; shift ;;
    --winner-only|-w) WINNER_ONLY=1; shift ;;
    --verbose|-v) VERBOSE=1; shift ;;
    --raw) RAW=1; shift ;;
    --include-dirs) INCLUDE_DIRS=1; shift ;;
    --limit|-L)
      if [[ -n "${2:-}" && "${2}" =~ ^[0-9]+$ ]]; then
        LIMIT="$2"; shift 2
      else
        die "Missing numeric argument for --limit"
      fi ;;
    --limit=*) LIMIT="${a#*=}"; shift ;;
    *) ARGS+=("$a"); shift ;;
  esac
done

[[ ${#ARGS[@]} -ge 1 ]] || die "Missing <path>. Run with --help for usage."
TARGET="${ARGS[0]}"

# --------------------------- normalize path ---------------------------
TARGET_ABS="$(realpath -s "$TARGET" 2>/dev/null || true)"
[[ -n "$TARGET_ABS" ]] || die "Could not resolve path: $TARGET"
[[ -e "$TARGET_ABS" ]] || die "Path does not exist: $TARGET_ABS"

# Detect mount prefix for the input so we can compute a relative path no matter what.
MOUNT_PREFIX=""
CANON_USER0_PATH=""
if [[ "$TARGET_ABS" =~ ^(/mnt/user0)(/|$) ]]; then
  MOUNT_PREFIX="/mnt/user0"
elif [[ "$TARGET_ABS" =~ ^(/mnt/user)(/|$) ]]; then
  MOUNT_PREFIX="/mnt/user"
elif [[ "$TARGET_ABS" =~ ^(/mnt/disk[0-9]+)(/|$) ]]; then
  MOUNT_PREFIX="${BASH_REMATCH[1]}"
elif [[ "$TARGET_ABS" =~ ^(/mnt/cache-temp)(/|$) ]]; then
  MOUNT_PREFIX="${BASH_REMATCH[1]}"
elif [[ "$TARGET_ABS" =~ ^(/mnt/cache[^/]*)(/|$) ]]; then
  # /mnt/cache, /mnt/cache2, /mnt/cache_pool_name, etc.
  MOUNT_PREFIX="${BASH_REMATCH[1]}"
else
  warn "Target is not under /mnt/user, /mnt/user0, /mnt/diskN, or /mnt/cache* — mapping may be incomplete."
fi

REL_TARGET=""
if [[ -n "$MOUNT_PREFIX" ]]; then
  if [[ "$TARGET_ABS" == "$MOUNT_PREFIX" ]]; then
    REL_TARGET=""
  else
    REL_TARGET="${TARGET_ABS#"$MOUNT_PREFIX"/}"
  fi
fi

CANON_USER_PATH="/mnt/user"
CANON_USER0_PATH=""
[[ -n "$REL_TARGET" ]] && CANON_USER_PATH="/mnt/user/$REL_TARGET"
if [[ "$MOUNT_PREFIX" == "/mnt/user0" && -n "$REL_TARGET" ]]; then
  CANON_USER0_PATH="/mnt/user0/$REL_TARGET"
fi

# Track whether the input target is a directory so we can normalize trailing slashes
IS_DIR_TARGET=0
[[ -d "$TARGET_ABS" ]] && IS_DIR_TARGET=1

# Candidate roots to search (exclude /mnt/user on purpose)
shopt -s nullglob
CANDIDATE_ROOTS=(/mnt/disk* /mnt/cache /mnt/cache* /mnt/cache-temp)

# Build unique existing roots ONCE
declare -a roots_to_check=()
declare -A _seen_roots=()
for pat in "${CANDIDATE_ROOTS[@]}"; do
  for d in $pat; do
    [[ -e "$d" ]] || continue
    [[ -n "${_seen_roots[$d]:-}" ]] && continue
    _seen_roots["$d"]=1
    # never search /mnt/user as a "root" or it'll trivially match itself
    [[ "$d" == "/mnt/user" ]] && continue
    roots_to_check+=("$d")
  done
done
[[ ${#roots_to_check[@]} -gt 0 ]] || die "No candidate roots found (expected /mnt/disk*, /mnt/cache*)."

# ------------------------------ file list -----------------------------
declare -a files=()
if [[ -d "$TARGET_ABS" ]]; then
  # default behavior: count files only (fast). Use --include-dirs to include directories.
  if [[ $RECURSIVE -eq 1 ]]; then
    if [[ $INCLUDE_DIRS -eq 0 ]]; then
      find_type_flag='-type f'
    else
      find_type_flag=''
    fi
    while IFS= read -r -d $'\0' f; do files+=("$f"); done < <(find "$TARGET_ABS" -mindepth 1 $find_type_flag -print0)
  else
    if [[ $INCLUDE_DIRS -eq 0 ]]; then
      find_type_flag='-type f'
    else
      find_type_flag=''
    fi
    while IFS= read -r -d $'\0' f; do files+=("$f"); done < <(find "$TARGET_ABS" -maxdepth 1 -mindepth 1 $find_type_flag -print0)
  fi
else
  files=("$TARGET_ABS")
fi

if (( VERBOSE == 1 )); then
  box_print "Scan" \
    "Target:    $TARGET_ABS" \
    "Mount:     ${MOUNT_PREFIX:-"(unknown)"}" \
    "Rel path:  ${REL_TARGET:-"(n/a)"}" \
    "User path: ${CANON_USER_PATH}${CANON_USER0_PATH:+  (array-only: $CANON_USER0_PATH)}" \
    "Mode:      $([[ $RECURSIVE -eq 1 ]] && echo "recursive" || echo "top-level")" \
    "Items:     ${#files[@]}" \
    "Roots:     ${#roots_to_check[@]} candidate mount(s)"
fi

# ------------------------------ mapping -------------------------------
declare -A disk_counts
declare -A files_on_disk
missing_total=0
declare -a missing_files=()

# Fast path: if target is a directory and we know its relative path under /mnt/user,
# scan each disk's version of that directory directly (O(disks) rather than O(items×disks)).
if [[ -d "$TARGET_ABS" && -n "$REL_TARGET" ]]; then
  for d in "${roots_to_check[@]}"; do
    dir="$d/$REL_TARGET"
    [[ -d "$dir" ]] || continue

    if [[ $RECURSIVE -eq 1 ]]; then
      if [[ $INCLUDE_DIRS -eq 1 ]]; then
        find_flags='-mindepth 1'
      else
        find_flags='-mindepth 1 -type f'
      fi
    else
      if [[ $INCLUDE_DIRS -eq 1 ]]; then
        find_flags='-maxdepth 1 -mindepth 1'
      else
        find_flags='-maxdepth 1 -mindepth 1 -type f'
      fi
    fi

    count=$(find "$dir" $find_flags -print0 2>/dev/null | tr -cd '\0' | wc -c)
    disk_counts["$d"]=$count
    if (( count > 0 )); then
      map=$(find "$dir" $find_flags -print0 2>/dev/null | head -z -n "$LIMIT" | xargs -0 -n 1 basename 2>/dev/null || true)
      files_on_disk["$d"]+="$map"$'\n'
    fi
  done
else
  # Fallback: per-item checks (handles file inputs and unknown mounts). Collect only up to LIMIT samples per disk.
  for f in "${files[@]}"; do
    relf=""

    if [[ -n "$MOUNT_PREFIX" ]]; then
      if [[ "$f" == "$MOUNT_PREFIX" ]]; then
        relf=""
      elif [[ "$f" == "$MOUNT_PREFIX/"* ]]; then
        relf="${f#"$MOUNT_PREFIX"/}"
      else
        missing_total=$((missing_total+1))
        missing_files+=("$f (not under $MOUNT_PREFIX)")
        continue
      fi
    else
      if [[ "$f" == /mnt/user/* ]]; then
        relf="${f#/mnt/user/}"
      else
        missing_total=$((missing_total+1))
        missing_files+=("$f (unhandled mount)")
        continue
      fi
    fi

    found=0
    for d in "${roots_to_check[@]}"; do
      if [[ -n "$relf" ]]; then
        cand="$d/$relf"
      else
        cand="$d"
      fi

      if [[ -e "$cand" ]]; then
        found=1
        disk_counts["$d"]=$(( ${disk_counts["$d"]:-0} + 1 ))

        if [[ -d "$TARGET_ABS" ]]; then
          rel_display="${f#"$TARGET_ABS"/}"
        else
          rel_display="$(basename "$f")"
        fi
        # only collect up to LIMIT samples for the mapping listing to avoid massive output
        if (( ${disk_counts["$d"]} <= LIMIT )); then
          files_on_disk["$d"]+="$rel_display"$'\n'
        fi
      fi
    done

    if [[ $found -eq 0 ]]; then
      missing_total=$((missing_total+1))
      missing_files+=("$f")
    fi
  done
fi

# ------------------------------- output -------------------------------
if [[ ${#disk_counts[@]} -eq 0 ]]; then
  box_print "Result" \
    "No matches found on any candidate roots." \
    "Searched ${#files[@]} item(s)."
else
  # summary + winner
  winner_disk=""
  winner_count=0
  summary_lines=()

  while IFS= read -r d; do
    c="${disk_counts[$d]}"
    summary_lines+=("$d: $c item(s)")
    if (( c > winner_count )); then
      winner_count="$c"
      winner_disk="$d"
    fi
  done < <(printf '%s\n' "${!disk_counts[@]}" | sort -V)

  # Prepare sorted list and counts
  mapfile -t _sorted_disks < <(printf '%s\n' "${!disk_counts[@]}" | sort -V)
  num_mounts=${#_sorted_disks[@]}

  if (( RAW == 1 )); then
    # raw output: one path per line for scripting
    for d in "${_sorted_disks[@]}"; do
      if [[ -n "$REL_TARGET" ]]; then
        out="${d}/${REL_TARGET}"
      else
        out="$d"
      fi
      if (( IS_DIR_TARGET == 1 )) && [[ "${out: -1}" != "/" ]]; then out="${out}/"; fi
      printf '%s\n' "$out"
    done
    exit 0
  fi

  if (( num_mounts == 1 )); then
    box_print "Result" "$winner_disk: $winner_count item(s)"
  else
    # multiple mounts: show summary + winner indication
    summary_lines+=("")
    summary_lines+=("Leading: $winner_disk with $winner_count hit(s)")
    box_print "Result" "${summary_lines[@]}"
  fi

  # If requested, just print the winning path (nice for scripting) and exit
  if [[ $WINNER_ONLY -eq 1 ]]; then
    if [[ -n "$winner_disk" ]]; then
      if [[ -n "$REL_TARGET" ]]; then
        out="${winner_disk}/${REL_TARGET}"
      else
        out="${winner_disk}"
      fi
      if (( IS_DIR_TARGET == 1 )) && [[ "${out: -1}" != "/" ]]; then out="${out}/"; fi
      printf '%s\n' "$out"
    fi
    exit 0
  fi

  # The money boxes: paths to paste into Unbalanced (one per matching mount).
  # Split into array (physical /mnt/disk*) and cache/pool matches so the user
  # doesn't accidentally try to 'balance' the cache.
  array_paths=()
  cache_paths=()
  while IFS= read -r d; do
    if [[ -n "$REL_TARGET" ]]; then
      path="$d/$REL_TARGET"
    else
      path="$d"
    fi

    if [[ "$d" == /mnt/disk* ]]; then
      array_paths+=("$path")
    else
      cache_paths+=("$path")
    fi
  done < <(printf '%s\n' "${!disk_counts[@]}" | sort -V)

  # Show paste box early (action-first UX)
  if (( ${#array_paths[@]} > 0 )); then
    box_print "Paste into Unbalanced (array)" "${array_paths[@]}"
    print_copy_lines "$FINDDISK_COPY_LABEL" "${array_paths[@]}"
  fi
  if (( ${#cache_paths[@]} > 0 )); then
    box_print "Paste into Unbalanced (cache/pool)" "${cache_paths[@]}"
    print_copy_lines "$FINDDISK_COPY_LABEL" "${cache_paths[@]}"
  fi
  # If neither array nor cache paths (edge case), fall back to combined box
  if (( ${#array_paths[@]} == 0 && ${#cache_paths[@]} == 0 )); then
    box_print "Unbalanced paths" "(none)"
  fi

  # Detailed mapping (compact) — show only when helpful (verbose, multiple mounts, or missing items)
  if (( VERBOSE == 1 || num_mounts > 1 || missing_total > 0 )); then
    while IFS= read -r d; do
      IFS=$'\n' read -r -d '' -a maplines <<< "${files_on_disk[$d]}"$'\0' || true
      if (( ${disk_counts[$d]:-0} > LIMIT )); then
        maplines+=("... and $(( ${disk_counts[$d]} - LIMIT )) more (only first $LIMIT shown)")
      fi
      box_print "Mapping: $d" \
        "Hits:  ${disk_counts[$d]} item(s)" \
        "" \
        "${maplines[@]}"
    done < <(printf '%s\n' "${!files_on_disk[@]}" | sort -V)
  fi
fi

if [[ $missing_total -gt 0 ]]; then
  box_print "Missing items ($missing_total)" "${missing_files[@]}"
fi

# Disk space for involved mounts (only in verbose/multi-mount/missing cases)
if [[ ${#disk_counts[@]} -gt 0 ]] && (( VERBOSE == 1 || num_mounts > 1 || missing_total > 0 )); then
  while IFS= read -r d; do
    IFS=$'\n' read -r -d '' -a df_lines <<< "$(df -h "$d" | sed -n '1p;2p')"$'\0' || true
    box_print "Disk space: $d" "${df_lines[@]}"
  done < <(printf '%s\n' "${!disk_counts[@]}" | sort -V)
fi

ok "All done. Now go bully Unbalanced into doing cardio."
