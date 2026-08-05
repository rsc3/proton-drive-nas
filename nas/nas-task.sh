#!/bin/sh
# Proton Drive -> NAS mirror. Run by DSM Task Scheduler via bootstrap.sh, as root.
#
# Updates: drop <name>.new into $STAGE and the next run installs it. That avoids
# File Station's habit of silently refusing to overwrite read-only files.
#
# All output goes to $BASE/log/sync.log, mirrored to $STAGE/sync.log on exit.

# ---------------------------------------------------------------- config ------
BASE=/volume1/docker/protondrive      # binary, engine, credentials, logs
DATA=/volume1/proton                  # mirror target (the exported share)
STAGE=/volume1/data/pd-staging        # network-writable staging directory
DOCKER=/usr/local/bin/docker
IMAGE=python:3-slim
OWNER=1026:100                        # uid:gid owning your shares ("ls -n")
WORKERS=4                             # parallel folder listings
EXPECT_SHA=                           # optional sha256 of your binary

# Sections to sync: "<remote>:<subdir>[:no-delete]", space separated.
#
# Each section gets its OWN subdirectory. Required, not cosmetic: the mirror
# deletes anything locally that isn't upstream, so two sections sharing a
# directory would delete each other's files every run.
#
# Use no-delete for anything shared WITH you: if the owner revokes access the
# section lists as EMPTY, which a mirror can't distinguish from "they deleted
# everything" and would wipe your copy.
#
# Do NOT add /albums or /photos - the CLI rejects them.
#
# NB: this value is word-split, so a '#' in here is a word, not a comment.
SECTIONS="/my-files:my-files /shared-with-me:shared-with-me:no-delete"
[ $# -gt 0 ] && SECTIONS="$*"
# ------------------------------------------------------------ end config ------

LOG=$BASE/log/sync.log

# No desktop keyring exists on a NAS, so credentials come from the plaintext
# store in $BASE/state. See README security note.
CLI_ENV="-e PROTON_DRIVE_CREDENTIALS_STORE=unsafe_file \
-e PROTON_DRIVE_CACHE_DIR=/state -e PROTON_DRIVE_BIN=/opt/proton-drive -e HOME=/state"

mkdir -p "$BASE/log" "$BASE/bin" "$BASE/sync" "$BASE/state" "$DATA"
exec >> "$LOG" 2>&1

# Mirror the log (and the CLI's own log) somewhere network-readable on every
# exit, including aborts. Clear the run marker.
trap 'tail -c 400000 "$LOG" > "$STAGE/sync.log" 2>/dev/null; \
      chmod 0666 "$STAGE/sync.log" 2>/dev/null; \
      cp "$BASE/state/proton-drive.log" "$STAGE/cli.log" 2>/dev/null; \
      chmod 0666 "$STAGE/cli.log" 2>/dev/null; \
      rm -f "$STAGE/RUNNING"' EXIT INT TERM

# DSM shows no "currently running" state, so publish one.
date '+%Y-%m-%d %H:%M:%S started' > "$STAGE/RUNNING" 2>/dev/null
chmod 0666 "$STAGE/RUNNING" 2>/dev/null

echo "================ $(date '+%Y-%m-%d %H:%M:%S') run start ================"
echo "sections: $SECTIONS"

# --- install anything staged -------------------------------------------------
if [ -f "$STAGE/proton-drive.new" ]; then
    cp "$STAGE/proton-drive.new" "$BASE/bin/proton-drive.tmp" \
        && mv "$BASE/bin/proton-drive.tmp" "$BASE/bin/proton-drive" \
        && rm -f "$STAGE/proton-drive.new" && echo "installed new binary"
fi
if [ -f "$STAGE/pd_sync.py.new" ]; then
    cp "$STAGE/pd_sync.py.new" "$BASE/sync/pd_sync.py.tmp" \
        && mv "$BASE/sync/pd_sync.py.tmp" "$BASE/sync/pd_sync.py" \
        && rm -f "$STAGE/pd_sync.py.new" && echo "installed new engine"
fi
# Credentials: staged copies are destroyed straight after install so tokens do
# not linger on a share. The sqlite caches belong to the OLD session, so drop
# them and let the CLI rebuild.
if [ -f "$STAGE/auth-session.json.new" ]; then
    cp "$STAGE/auth-session.json.new" "$BASE/state/auth-session.json" \
        && echo "installed new auth-session.json"
    [ -f "$STAGE/clientUid.json.new" ] \
        && cp "$STAGE/clientUid.json.new" "$BASE/state/clientUid.json" \
        && echo "installed new clientUid.json"
    rm -f "$BASE/state"/cache-*.sqlite* "$BASE/state"/events.json
    echo "cleared stale session cache"
    shred -u "$STAGE/auth-session.json.new" 2>/dev/null \
        || rm -f "$STAGE/auth-session.json.new"
    rm -f "$STAGE/clientUid.json.new"
fi

# --- permissions -------------------------------------------------------------
# The container runs as $OWNER, but DSM uploads happen as whoever you log in as.
# Group-writable keeps both able to manage these files. Credentials stay 0600.
chown -R "$OWNER" "$BASE" 2>/dev/null
chmod -R u+rwX,g+rwX "$BASE" 2>/dev/null
chmod 0755 "$BASE/bin/proton-drive" 2>/dev/null
chmod 0600 "$BASE/state"/* 2>/dev/null

if [ -n "$EXPECT_SHA" ]; then
    ACTUAL=$(sha256sum "$BASE/bin/proton-drive" | cut -d' ' -f1)
    [ "$ACTUAL" = "$EXPECT_SHA" ] \
        && echo "binary: verified" \
        || echo "binary: UNEXPECTED hash $ACTUAL"
fi

[ -x "$DOCKER" ] || { echo "FATAL: no docker at $DOCKER"; exit 1; }
for p in "$BASE/bin/proton-drive" "$BASE/sync/pd_sync.py" \
         "$BASE/state/auth-session.json"; do
    [ -e "$p" ] || { echo "FATAL: missing $p"; exit 1; }
done

# --- smoke test --------------------------------------------------------------
# Narrow purpose: prove this CPU can execute the binary at all. Only a fatal
# signal means "wrong CPU build" (132=SIGILL). Anything else is an application
# complaint that the real run will report properly, so don't abort on it.
echo "--- smoke test ---"
"$DOCKER" run --rm $CLI_ENV \
    -v "$BASE/bin/proton-drive":/opt/proton-drive:ro \
    -v "$BASE/state":/state \
    "$IMAGE" /opt/proton-drive --version
rc=$?
case $rc in
    132|133|134|136|139)
        echo "ABORT: fatal signal $rc - binary cannot execute on this CPU."
        echo "       Rebuild with scripts/build-baseline-cli.sh (no AVX2)."
        exit 1 ;;
    0) echo "smoke test PASSED" ;;
    *) echo "smoke test rc=$rc (not a CPU fault); continuing" ;;
esac

# --- lock --------------------------------------------------------------------
# Two concurrent runs would have one walking while the other downloads, and a
# partial tree can look like deletions.
if command -v flock >/dev/null 2>&1; then
    exec 9>"$BASE/sync.lock"
    flock -n 9 || { echo "another run in progress, exiting"; exit 0; }
fi

# --- sync each section -------------------------------------------------------
run_sync() {  # $1=remote section  $2=target dir  $3=extra args
    "$DOCKER" run --rm --name "protondrive-sync-$(basename "$2")" \
        --user "$OWNER" $CLI_ENV \
        -v "$BASE/bin/proton-drive":/opt/proton-drive:ro \
        -v "$BASE/sync/pd_sync.py":/opt/pd_sync.py:ro \
        -v "$BASE/state":/state \
        -v "$2":/data \
        "$IMAGE" \
        python3 /opt/pd_sync.py --root "$1" --target /data \
            --workers "$WORKERS" $3
}

worst=0
for entry in $SECTIONS; do
    section=${entry%%:*}
    rest=${entry#*:}
    subdir=${rest%%:*}
    flag=${rest#*:}
    [ "$flag" = "$rest" ] && flag=""

    # Mirrored sections move removals into <target>/.trash/<date>/ instead of
    # erasing them, so an accidental delete upstream stays recoverable.
    if [ "$flag" = "no-delete" ]; then
        extra="--no-delete"
    else
        extra="--trash-dir .trash"
    fi

    target=$DATA/$subdir
    mkdir -p "$target"
    chown "$OWNER" "$target" 2>/dev/null

    echo "--- sync $section -> $target ${extra} ---"
    run_sync "$section" "$target" "$extra"
    rc=$?

    # The first run after a session-cache rebuild fails and the next succeeds.
    # Absorb that instead of needing a human. Never retry rc=2 (rate limited).
    if [ $rc -eq 1 ]; then
        echo "--- $section rc=1; retrying once after 30s ---"
        sleep 30
        run_sync "$section" "$target" "$extra"
        rc=$?
        echo "--- $section retry rc=$rc ---"
    fi

    case $rc in
        0) echo "--- $section OK" ;;
        2) echo "--- $section RATE LIMITED, will retry next schedule" ;;
        *) echo "--- $section FAILED rc=$rc" ;;
    esac
    [ $rc -gt $worst ] && worst=$rc
done

echo "================ $(date '+%Y-%m-%d %H:%M:%S') run end worst=$worst ======"
exit $worst
