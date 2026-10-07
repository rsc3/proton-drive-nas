#!/bin/sh
# Run by DSM Task Scheduler via bootstrap.sh, as root. Two modes:
#
#   nas-task.sh                 Proton -> NAS mirror (nightly)
#   nas-task.sh push [NAME...] [--dry-run] [--allow-mass-delete]
#                               NAS -> Proton backup of PUSH_JOBS (weekly);
#                               NAMEs (e.g. "music") limit it to those jobs
#
# The modes have separate locks, logs, run markers and Proton sessions, so a
# multi-day backup never blocks the nightly mirror.
#
# Updates: drop <name>.new into $STAGE and the next run installs it. That avoids
# File Station's habit of silently refusing to overwrite read-only files.
#
# Mirror output goes to $BASE/log/sync.log, backup output to $BASE/log/push.log;
# both are copied to $STAGE on exit.

# ---------------------------------------------------------------- config ------
BASE=/volume1/docker/protondrive      # binary, engine, credentials, logs
DATA=/volume1/proton                  # mirror target (the exported share)
STAGE=/volume1/data/pd-staging        # network-writable staging directory
DOCKER=/usr/local/bin/docker
IMAGE=python:3-slim
OWNER=1026:100                        # uid:gid owning your shares ("ls -n")
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
# Top-level folders under /my-files the mirror leaves out: the backup lives
# there, and mirroring it would download every backed-up file back again.
MIRROR_EXCLUDE="_nas_backup"

# Backup jobs: "<local folder>:<folder under /my-files>", space separated.
PUSH_JOBS="/volume1/music:_nas_backup/music /volume1/video:_nas_backup/video"
# qBittorrent's resume data, so files of unfinished torrents are left out, and
# how its container save paths map to NAS paths.
QBT_BACKUP=/volume1/docker/qbittorrent/config/qBittorrent/BT_backup
QBT_MAP="/downloads/music=/volume1/music/torrents /downloads/video=/volume1/video/torrents"

MODE=mirror
PUSH_ARGS=""
# No `shift`: the script re-execs itself below with the original "$@".
if [ "${1:-}" = "push" ]; then
    MODE=push
    ONLY=""
    for a in "$@"; do
        case $a in push) ;; --*) PUSH_ARGS="$PUSH_ARGS $a" ;; *) ONLY="$ONLY $a" ;; esac
    done
    if [ -n "$ONLY" ]; then            # keep only the named jobs (by folder name)
        keep=""
        for job in $PUSH_JOBS; do
            for o in $ONLY; do [ "$(basename "${job%%:*}")" = "$o" ] && keep="$keep $job"; done
        done
        PUSH_JOBS=$keep
    fi
elif [ $# -gt 0 ]; then
    SECTIONS="$*"
fi
# ------------------------------------------------------------ end config ------

# --- relocate before doing anything ------------------------------------------
# sh reads scripts incrementally, so replacing this file while a run is in
# progress makes the running shell resume at a byte offset in different content
# and die with "syntax error: unexpected end of file". Since this file lives on a
# network share precisely so it can be updated at any time, copy it somewhere
# private and re-exec from there. The window before this runs is a few lines.
# cp to a temp name then mv: a second run starting mid-run swaps in a new file
# instead of rewriting the one the running shell is still reading.
if [ -z "${PD_RELOCATED:-}" ] && [ -f "$0" ]; then
    PRIV=$BASE/nas-task.run.sh
    mkdir -p "$BASE"
    if cp "$0" "$PRIV.tmp" 2>/dev/null && mv -f "$PRIV.tmp" "$PRIV"; then
        PD_RELOCATED=1 export PD_RELOCATED
        exec /bin/sh "$PRIV" "$@"
    fi
fi

if [ "$MODE" = push ]; then
    LOG=$BASE/log/push.log; LOCK=$BASE/push.lock; STATE=$BASE/state-push
    MARK=$STAGE/RUNNING-push; LOGCOPY=$STAGE/push.log; CLICOPY=$STAGE/cli-push.log
else
    LOG=$BASE/log/sync.log; LOCK=$BASE/sync.lock; STATE=$BASE/state
    MARK=$STAGE/RUNNING; LOGCOPY=$STAGE/sync.log; CLICOPY=$STAGE/cli.log
fi

# No desktop keyring exists on a NAS, so credentials come from the plaintext
# store in $STATE. See README security note.
CLI_ENV="-e PROTON_DRIVE_CREDENTIALS_STORE=unsafe_file \
-e PROTON_DRIVE_CACHE_DIR=/state -e PROTON_DRIVE_BIN=/opt/proton-drive -e HOME=/state"
# Containers default to UTC. DSM keeps a POSIX TZ string in /etc/TZ, which
# needs no zoneinfo in the image, so every line of sync.log is in local time.
[ -r /etc/TZ ] && CLI_ENV="$CLI_ENV -e TZ=$(cat /etc/TZ)"

mkdir -p "$BASE/log" "$BASE/bin" "$BASE/sync" "$STATE" "$DATA"
exec >> "$LOG" 2>&1

# --- lock --------------------------------------------------------------------
# Taken before anything else, so a run started while another is in progress
# (the midnight schedule during a long manual run) touches nothing: not the
# marker, the staged installs, the state dir, or the walk. Two concurrent
# syncs would have one walking while the other downloads, and a partial tree
# can look like deletions.
if command -v flock >/dev/null 2>&1; then
    exec 9>"$LOCK"
    flock -n 9 || { echo "$(date '+%Y-%m-%d %H:%M:%S') another run in progress, exiting"; exit 0; }
fi

# Mirror the log (and the CLI's own log) somewhere network-readable on every
# exit, including aborts. Clear the run marker.
trap 'tail -c 400000 "$LOG" > "$LOGCOPY" 2>/dev/null; \
      chmod 0666 "$LOGCOPY" 2>/dev/null; \
      cp "$STATE/proton-drive.log" "$CLICOPY" 2>/dev/null; \
      chmod 0666 "$CLICOPY" 2>/dev/null; \
      rm -f "$MARK"' EXIT
# A signal must end the run. A handler that only cleans up lets sh carry on with
# the next section after a kill, still holding the lock (it did).
trap 'echo "stopped by signal"; exit 143' INT TERM

# DSM shows no "currently running" state, so publish one.
date '+%Y-%m-%d %H:%M:%S started' > "$MARK" 2>/dev/null
chmod 0666 "$MARK" 2>/dev/null

echo "================ $(date '+%Y-%m-%d %H:%M:%S') $MODE run start ================"
if [ "$MODE" = push ]; then echo "jobs: $PUSH_JOBS ${PUSH_ARGS}"; else echo "sections: $SECTIONS"; fi

# --- install anything staged -------------------------------------------------
if [ -f "$STAGE/proton-drive.new" ]; then
    cp "$STAGE/proton-drive.new" "$BASE/bin/proton-drive.tmp" \
        && mv "$BASE/bin/proton-drive.tmp" "$BASE/bin/proton-drive" \
        && rm -f "$STAGE/proton-drive.new" && echo "installed new binary"
fi
# The engines and their shared CLI module. A run already in progress keeps the
# copy it loaded: mv swaps in a new file rather than rewriting the old one.
for f in pd_sync.py pd_push.py pd_repl.py; do
    if [ -f "$STAGE/$f.new" ]; then
        cp "$STAGE/$f.new" "$BASE/sync/$f.tmp" \
            && mv "$BASE/sync/$f.tmp" "$BASE/sync/$f" \
            && rm -f "$STAGE/$f.new" && echo "installed new $f"
    fi
done
# Credentials (mirror session only): staged copies are destroyed straight after
# install so tokens do not linger on a share. The sqlite caches belong to the
# OLD session, so drop them and let the CLI rebuild.
if [ "$MODE" = mirror ] && [ -f "$STAGE/auth-session.json.new" ]; then
    cp "$STAGE/auth-session.json.new" "$BASE/state/auth-session.json" \
        && echo "installed new auth-session.json"
    [ -f "$STAGE/clientUid.json.new" ] \
        && cp "$STAGE/clientUid.json.new" "$BASE/state/clientUid.json" \
        && echo "installed new clientUid.json"
    rm -f "$BASE/state"/cache-*.sqlite* "$BASE/state"/events.json \
          "$BASE/state"/events.lock
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
chmod 0600 "$BASE/state"/* "$BASE/state-push"/* 2>/dev/null

if [ -n "$EXPECT_SHA" ]; then
    ACTUAL=$(sha256sum "$BASE/bin/proton-drive" | cut -d' ' -f1)
    [ "$ACTUAL" = "$EXPECT_SHA" ] \
        && echo "binary: verified" \
        || echo "binary: UNEXPECTED hash $ACTUAL"
fi

[ -x "$DOCKER" ] || { echo "FATAL: no docker at $DOCKER"; exit 1; }
ENGINE=pd_sync.py; [ "$MODE" = push ] && ENGINE=pd_push.py
for p in "$BASE/bin/proton-drive" "$BASE/sync/$ENGINE" "$BASE/sync/pd_repl.py" \
         "$STATE/auth-session.json"; do
    [ -e "$p" ] || { echo "FATAL: missing $p"; exit 1; }
done

# --- smoke test --------------------------------------------------------------
# Narrow purpose: prove this CPU can execute the binary at all. Only a fatal
# signal means "wrong CPU build" (132=SIGILL). Anything else is an application
# complaint that the real run will report properly, so don't abort on it.
# The CLI creates cache files and takes the event lock even for --version, so
# give it a throwaway cache inside the container rather than $BASE/state: run
# as root it left root-owned files the sync couldn't write, and as PID 1 it
# could leave a lock that stops event updates (docs/findings.md).
echo "--- smoke test ---"
"$DOCKER" run --rm --user "$OWNER" $CLI_ENV \
    -e PROTON_DRIVE_CACHE_DIR=/tmp -e HOME=/tmp \
    -v "$BASE/bin/proton-drive":/opt/proton-drive:ro \
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

# The CLI's event lock records its owner's PID. In a container that is often 1,
# which always looks alive, so a leftover lock silently stops event updates and
# the entity cache goes stale (docs/findings.md). We hold the run lock and no
# CLI is using $STATE yet, so any lock here is leftover.
rm -f "$STATE/events.lock"

# --- backup mode -------------------------------------------------------------
if [ "$MODE" = push ]; then
    qmap=""
    for m in $QBT_MAP; do qmap="$qmap --qbt-map $m"; done
    qmount=""; qarg=""
    [ -d "$QBT_BACKUP" ] && qmount="-v $QBT_BACKUP:/qbt:ro" && qarg="--qbt /qbt"
    worst=0
    for job in $PUSH_JOBS; do
        src=${job%%:*}
        dest=${job#*:}
        name=$(basename "$src")
        echo "--- push $src -> /my-files/$dest ${PUSH_ARGS} ---"
        # Shares are mounted read-only at their own paths, so the backup can't
        # change them and qBittorrent's paths map one to one.
        "$DOCKER" run --rm --name "protondrive-push-$name" \
            --user "$OWNER" $CLI_ENV \
            -v "$BASE/bin/proton-drive":/opt/proton-drive:ro \
            -v "$BASE/sync":/opt/sync:ro \
            -v "$STATE":/state \
            -v "$src":"$src":ro $qmount \
            "$IMAGE" \
            python3 /opt/sync/pd_push.py --src "$src" --remote "/my-files/$dest" \
                --manifest "/state/manifest-$name.json" $qarg $qmap $PUSH_ARGS
        rc=$?
        # No automatic retry: rc=1 means anomalies or errors for a human to read,
        # and repeating a multi-hour upload pass wouldn't change them.
        case $rc in
            0) echo "--- $src OK" ;;
            2) echo "--- $src RATE LIMITED, stopping; will continue next run"; worst=2; break ;;
            *) echo "--- $src finished with rc=$rc (see anomalies/errors above)" ;;
        esac
        [ $rc -gt $worst ] && worst=$rc
    done
    echo "================ $(date '+%Y-%m-%d %H:%M:%S') push run end worst=$worst ======"
    exit $worst
fi

# --- sync each section -------------------------------------------------------
excl=""
for x in $MIRROR_EXCLUDE; do excl="$excl --exclude $x"; done

run_sync() {  # $1=remote section  $2=target dir  $3=extra args
    "$DOCKER" run --rm --name "protondrive-sync-$(basename "$2")" \
        --user "$OWNER" $CLI_ENV \
        -v "$BASE/bin/proton-drive":/opt/proton-drive:ro \
        -v "$BASE/sync":/opt/sync:ro \
        -v "$BASE/state":/state \
        -v "$2":/data \
        "$IMAGE" \
        python3 /opt/sync/pd_sync.py --root "$1" --target /data \
            --hash-cache "/state/hashcache-$(basename "$2").json" $excl $3
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

    # Retry once for transient errors instead of needing a human. Never retry
    # rc=2 (rate limited).
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
