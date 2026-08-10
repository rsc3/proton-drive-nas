#!/bin/sh
# qBittorrent "Run external program on torrent completion" hook.
# Uploads the finished torrent to Proton Drive using the official CLI.
#
# In qBittorrent: Options -> Downloads -> Run external program on completion:
#     /config/proton/qbt-on-complete.sh "%F" "%N"
#
#   %F = content path (the file, or the folder for multi-file torrents)
#   %N = torrent name
#
# This runs INSIDE the qBittorrent container, so that container needs:
#   -v .../bin/proton-drive:/usr/local/bin/proton-drive:ro
#   -v .../qbt-state:/config/proton/state        (writable by the container's user)
#   -v .../qbt-on-complete.sh:/config/proton/qbt-on-complete.sh:ro
#
# Use a SEPARATE Proton session and state dir from the mirror's. The CLI's cache
# assumes it is the only instance using it, and a separate session can be
# revoked independently.
set -u

SRC=${1:-}
NAME=${2:-$(basename "${SRC:-unknown}")}

REMOTE_PARENT=/my-files/torrents     # where uploads land in Proton
STATE=/config/proton/state
LOG=/config/proton/upload.log
CLI=${PROTON_DRIVE_BIN:-/usr/local/bin/proton-drive}

export PROTON_DRIVE_CREDENTIALS_STORE=unsafe_file
export PROTON_DRIVE_CACHE_DIR="$STATE"
export HOME="$STATE"

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') $*" >> "$LOG"; }

log "=== complete: name=$NAME src=$SRC"

[ -n "$SRC" ] || { log "FATAL: no content path given"; exit 1; }
[ -e "$SRC" ] || { log "FATAL: $SRC does not exist"; exit 1; }
[ -x "$CLI" ] || { log "FATAL: no CLI at $CLI"; exit 1; }
[ -f "$STATE/auth-session.json" ] || { log "FATAL: not logged in ($STATE)"; exit 1; }

# Refuse to touch the mirror. Writing there would be undone (or trashed) by the
# next sync, and could confuse its delete pass.
case "$SRC" in
    /volume1/proton/*|/proton/*)
        log "FATAL: $SRC is inside the Proton mirror; downloads must not live there"
        exit 1 ;;
esac

# Create the destination folder once; harmless if it already exists.
"$CLI" filesystem create-folder /my-files "$(basename "$REMOTE_PARENT")" \
    >> "$LOG" 2>&1

SIZE=$(du -sh "$SRC" 2>/dev/null | cut -f1)
log "uploading $SIZE -> $REMOTE_PARENT"

# -c skip: re-running after a partial upload resumes rather than duplicating.
if "$CLI" filesystem upload -t -c skip "$SRC" "$REMOTE_PARENT" >> "$LOG" 2>&1; then
    log "OK: $NAME uploaded"
    # The local copy is left in place on purpose: deleting it would stop you
    # seeding, and the nightly mirror will bring a copy back down anyway.
    # If you want it removed after upload, do it here, deliberately.
else
    rc=$?
    log "FAILED rc=$rc: $NAME (local copy kept; re-run this script to retry)"
    exit $rc
fi
