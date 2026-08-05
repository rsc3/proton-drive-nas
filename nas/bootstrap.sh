#!/bin/sh
# Paste this into DSM Task Scheduler (User-defined script, run as root).
# Adjust the two paths for your NAS.
#
# It copies the real script and runs the copy. That matters: sh reads scripts
# incrementally, so replacing the staged file while a run is in progress would
# make the running shell resume at a byte offset in different content and die
# with "syntax error: unexpected end of file". Copying first makes staging an
# update at any time safe.
#
# Optional arguments select sections, e.g. for a separate weekly schedule:
#   ... nas-task.run.sh /shared-with-me:shared-with-me:no-delete

S=/volume1/data/pd-staging/nas-task.sh
T=/volume1/docker/protondrive/nas-task.run.sh

[ -f "$S" ] || { echo "missing $S" >&2; exit 1; }
cp "$S" "$T" && /bin/sh "$T" "$@"
