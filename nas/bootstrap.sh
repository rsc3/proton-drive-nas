#!/bin/sh
# Paste this into DSM Task Scheduler (User-defined script, run as root).
# Adjust the path. This is the only thing you should ever need to paste.
#
# nas-task.sh relocates itself to $BASE/nas-task.run.sh and re-execs before
# doing any work, so staging an update to the file below is safe even while a
# run is in progress. (sh reads scripts incrementally, so overwriting a running
# script makes the shell resume at a byte offset in different content and die
# with "syntax error: unexpected end of file".)
#
# Optional arguments select sections, e.g. for a separate weekly schedule:
#   /bin/sh /volume1/data/pd-staging/nas-task.sh /shared-with-me:shared-with-me:no-delete

S=/volume1/data/pd-staging/nas-task.sh

[ -f "$S" ] || { echo "missing $S" >&2; exit 1; }
/bin/sh "$S" "$@"
