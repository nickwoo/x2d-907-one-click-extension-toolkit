#!/system/bin/sh
# Lens profile selection worker (Phase 1, source only).
# Manages files and markers only. NO odindb calls, NO /proc access, NO
# image-file access, NO camera-service interaction. Follows the
# speed-buff-worker.sh shape: transaction lock, fail-closed on any error
# (selection unchanged, non-zero exit), atomic marker replacement.
# Actions: status | select <id> [aperture] | log
# Exit codes: 35 unknown action, 36 lock timeout, 37 signal,
# 39 invalid id/aperture, 40 profiles JSON unreadable,
# 41 /blackbox missing, 42 log write failed (marker rolled back).
set -eu
# Camera tool directories first; keep the caller's PATH behind them so the
# script never loses standard tools on units where PATH already carries them.
export PATH=/system/bin:/system/xbin:/sbin:$PATH
export TMPDIR=/tmp
PROFILES=/system/etc/X2dLensProfiles.json
MARKER=/blackbox/x2d-lens-profile.selected
LOG=/blackbox/x2d-lens-profile.log
D=/tmp/x2d-lens-profile
FULL_STOPS="1.4 2 2.8 4 5.6 8 11 16 22 32"
action=${1:-status}
id=${2:-}
aperture=${3:-}
# /blackbox is the camera's persistent store. Never create it: if it is
# absent the payload process cannot persist anything, so fail closed.
[ -d /blackbox ] || exit 41
case "$action" in
 status)
  # Lock-free read: marker replacement is an atomic rename (see select),
  # so a concurrent read sees either the old or the new marker, never torn.
  sel_id=""; sel_ap=""
  if [ -f "$MARKER" ]; then
   content=$(cat "$MARKER")
   case "$content" in
    *" "*) sel_id=${content%% *}; sel_ap=${content#* };;
    *) sel_id=$content;;
   esac
  fi
  if [ -n "$sel_id" ]; then id_json="\"$sel_id\""; else id_json=null; fi
  ap_json=null
  case "$sel_ap" in
   1.4|2|2.8|4|5.6|8|11|16|22|32) ap_json=$sel_ap;;
  esac
  if [ -n "$sel_id" ]; then msg="Lens profile selected"; else msg="No lens profile selected"; fi
  printf '{"ready":true,"selectedProfileId":%s,"aperture":%s,"message":"%s"}\n' "$id_json" "$ap_json" "$msg"
  exit 0
  ;;
 log)
  # Lock-free read; a missing log is an empty log, not an error.
  [ -f "$LOG" ] && cat "$LOG"
  exit 0
  ;;
 select) ;;
 *) exit 35;;
esac
# select: validate everything BEFORE taking the lock or touching state.
case "$id" in
 ''|*[!a-z0-9-]*) exit 39;;
esac
if [ -n "$aperture" ]; then
 case "$aperture" in
  1.4|2|2.8|4|5.6|8|11|16|22|32) ;;
  *) exit 39;;
 esac
fi
[ -f "$PROFILES" ] && [ -r "$PROFILES" ] || exit 40
# Spacing-insensitive id check: the shipped JSON is compact, but a hand-edited
# copy may carry spaces, so strip blanks before matching the quoted id.
if ! tr -d ' \t' < "$PROFILES" | grep -F -q "\"id\":\"$id\""; then exit 39; fi
mkdir -p "$D"; chmod 700 "$D"
n=0
while ! mkdir "$D/lock" 2>/dev/null; do
 n=$((n+1)); [ "$n" -lt 15 ] || exit 36
 sleep 1
done
trap 'rmdir "$D/lock"' EXIT
trap 'exit 37' HUP INT TERM
old_marker=$(cat "$MARKER" 2>/dev/null || true)
if [ -n "$aperture" ]; then new_content="$id $aperture"; else new_content="$id"; fi
printf '%s' "$new_content" > "$MARKER.tmp"
mv "$MARKER.tmp" "$MARKER"
ts=$(date -u +%Y-%m-%dT%H:%M:%SZ)
if ! printf '%s,%s,%s\n' "$ts" "$id" "$aperture" >> "$LOG"; then
 # Log append failed: roll the marker back so selection and log never diverge.
 if [ -n "$old_marker" ]; then printf '%s' "$old_marker" > "$MARKER"; else rm -f "$MARKER"; fi
 exit 42
fi
printf '{"ready":true,"selectedProfileId":"%s","aperture":%s,"message":"Lens profile selected"}\n' "$id" "${aperture:-null}"
