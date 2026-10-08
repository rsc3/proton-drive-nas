#!/usr/bin/env python3
"""Mirror Proton Drive to a local directory using the official proton-drive CLI.

One-way: Proton -> local. Mirrors deletions. Change detection uses the sha1 that
Proton already reports per file, so it does not depend on modification times
(the CLI cannot set them, which is why a naive mtime sync would loop forever).

Exit codes: 0 ok, 1 errors occurred, 2 rate limited (safe to retry later).
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pd_repl import Repl, CliError, RateLimited, esc, local_arg  # noqa: E402

CLI = os.environ.get("PROTON_DRIVE_BIN", "proton-drive")

# Artifacts the NAS itself creates inside the share. Never download, never
# delete -- DSM recreates @eaDir constantly and fighting it is pointless.
LOCAL_KEEP = {"@eaDir", "#recycle", ".DS_Store", "@tmp", ".trash"}


# The CLI rewrites unsafe characters when it writes a file to disk, so the local
# name can differ from the remote one. Mirror its rule exactly, or every affected
# file is re-downloaded on every run (and, on a mirrored section, deleted as
# extraneous in between). Taken from the CLI binary:
#     fF0 = /[\x00-\x1f\x7f<>:"|?*\\/]/g
#     name.replace(fF0, "_"); "" -> "_"; "." -> "_"; ".." -> "__"
_UNSAFE = re.compile(r'[\x00-\x1f\x7f<>:"|?*\\/]')


def local_name(name):
    """The on-disk name the CLI will use for a given remote name."""
    z = _UNSAFE.sub("_", name)
    if z == "":
        return "_"
    if z == ".":
        return "_"
    if z == "..":
        return "__"
    return z


def walk(root, cli, log, exclude=()):
    """Return (files, folders, errors) keyed by LOCAL path relative to the target.

    Local keys are sanitised the same way the CLI sanitises names on download
    (see local_name). Remote paths keep the true names. Getting this wrong means
    the expected local file never exists, so it is re-downloaded every run -- and
    on a mirrored section the sanitised file is then deleted as "extraneous",
    producing infinite delete/download churn.

    One interactive CLI, one folder at a time: CLI processes can't share a
    cache, and a warm REPL lists a folder in ~50 ms, so parallelism isn't needed.
    `exclude` names top-level folders under root that are not mirrored at all.

    Folders are addressed by name, which the CLI resolves from its cache. A
    name the REPL can't take (it reads one command per line, so a newline) is
    addressed by node uid instead (`/my-files/<uid>`). Uids everywhere would
    work too but walk ~6x slower: the CLI looks each one up separately.
    Under /shared-with-me the share itself must always be named.
    """
    files, folders, errors = {}, {}, 0
    pending = [(root, root, "")]        # (cli path, readable path, local prefix)
    while pending:
        path, shown, prefix = pending.pop()
        try:
            entries = cli.list(path)
        except RateLimited:
            raise
        except CliError as e:
            errors += 1
            log(f"ERROR listing {shown}: {e}")
            continue
        parts = path.strip("/").split("/")
        if parts[0] == "shared-with-me":
            base = "/shared-with-me" if len(parts) == 1 else f"/shared-with-me/{parts[1]}"
        else:
            base = "/" + parts[0]
        for e in entries:
            nm = e.get("name") or {}
            if not nm.get("ok"):
                errors += 1
                log(f"SKIP undecryptable name, uid={e.get('uid')}")
                continue
            name = nm["value"]
            if not prefix and name in exclude:
                continue
            typable = "\n" not in name and "\r" not in name
            if typable:
                child = f"{path}/{esc(name)}"
            elif base == "/shared-with-me":     # a share's root: by name only
                errors += 1
                log(f"SKIP share with a newline in its name: {name!r}")
                continue
            else:
                child = f"{base}/{e['uid']}"
            child_shown = f"{shown}/{esc(name)}"
            child_rel = f"{prefix}/{local_name(name)}".lstrip("/")
            rev = e.get("activeRevision") or {}
            if e.get("type") == "folder":
                folders[child_rel] = True
                pending.append((child, child_shown, child_rel))
            elif child_rel in files:
                # Two different remote names can sanitise to the same local
                # name. Whoever lands first wins; flag the clash rather than
                # silently overwriting one with the other.
                errors += 1
                log(f"SKIP name collision after sanitising: {child_shown!r} and "
                    f"{files[child_rel]['shown']!r} both map to {child_rel!r}")
            else:
                files[child_rel] = {
                    "remote": child,
                    "shown": child_shown,
                    "size": rev.get("claimedSize", rev.get("storageSize")),
                    "sha1": (rev.get("claimedDigests") or {}).get("sha1"),
                }
    return files, folders, errors


def sha1_of(path, buf=1 << 20):
    h = hashlib.sha1()
    with open(path, "rb") as f:
        while chunk := f.read(buf):
            h.update(chunk)
    return h.hexdigest()


def needs_download(local, meta, log, cache=None):
    """cache: rel-path -> [size, mtime_ns, sha1] from earlier runs, so an
    unchanged local file isn't re-read every night just to re-hash it."""
    if not os.path.exists(local):
        return True
    try:
        st = os.stat(local)
        if meta["size"] is not None and st.st_size != meta["size"]:
            return True
        if meta["sha1"]:
            hit = cache.get(local) if cache is not None else None
            if hit and hit[0] == st.st_size and hit[1] == st.st_mtime_ns:
                digest = hit[2]
            else:
                digest = sha1_of(local)
                if cache is not None:
                    cache[local] = [st.st_size, st.st_mtime_ns, digest]
            return digest != meta["sha1"].lower()
        # No remote hash and size matches: assume unchanged.
        return False
    except OSError as e:
        log(f"WARN stat/hash failed for {local}: {e}; will re-download")
        return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="/my-files")
    ap.add_argument("--target", required=True)
    # Accepted for old callers and ignored: the walk uses one interactive CLI.
    ap.add_argument("--workers", type=int, default=1, help=argparse.SUPPRESS)
    ap.add_argument("--exclude", action="append", default=[],
                    help="top-level folder under --root to leave out entirely "
                         "(never downloaded, never deleted locally)")
    ap.add_argument("--hash-cache",
                    help="JSON file remembering local sha1s by size+mtime")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-delete", action="store_true")
    ap.add_argument("--trash-dir", default=None,
                    help="move deletions into this dir (relative to target) "
                         "under a dated subfolder, instead of erasing them")
    ap.add_argument("--log")
    args = ap.parse_args()

    # An unwritable log file must not kill the run -- stdout is captured by the
    # caller anyway, so degrade to console-only rather than aborting.
    logf = None
    if args.log:
        try:
            logf = open(args.log, "a")
        except OSError as e:
            print(f"WARN cannot write log file {args.log}: {e}; "
                  f"continuing with console output only", flush=True)

    def log(msg):
        line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
        print(line, flush=True)
        if logf:
            logf.write(line + "\n")
            logf.flush()

    tag = " (DRY RUN)" if args.dry_run else ""
    log(f"=== sync start{tag}: {args.root} -> {args.target}")

    if not os.path.isdir(args.target):
        log(f"FATAL target does not exist: {args.target}")
        return 1

    cli = Repl(os.environ.get("PROTON_DRIVE_BIN", "proton-drive"))
    try:
        return run(args, cli, log, tag)
    finally:
        cli.close()


def run(args, cli, log, tag):
    t0 = time.time()
    try:
        files, folders, walk_errors = walk(args.root, cli, log, set(args.exclude))
    except RateLimited as e:
        log(f"RATE LIMITED during walk: {e}")
        return 2
    log(f"remote: {len(files)} files, {len(folders)} folders, "
        f"{sum(f['size'] or 0 for f in files.values())/1e9:.2f} GB "
        f"(walk {time.time()-t0:.1f}s, {walk_errors} errors)")

    stats = {"downloaded": 0, "unchanged": 0, "deleted": 0, "errors": 0,
             "bytes": 0}

    # Directories first, so downloads have somewhere to land.
    for rel in sorted(folders):
        d = os.path.join(args.target, rel)
        if not os.path.isdir(d):
            log(f"MKDIR {rel}")
            if not args.dry_run:
                os.makedirs(d, exist_ok=True)

    # Decide what to fetch, then fetch in batches grouped by target directory.
    # `filesystem download` takes `path... localFolder`. Batching mattered most
    # when every command started a new CLI (8.1 s/file one at a time); with the
    # interactive CLI it still saves a round trip per file.
    cache = {}
    if args.hash_cache:
        try:
            cache = json.load(open(args.hash_cache))
        except (OSError, ValueError):
            cache = {}
    todo = {}  # local parent dir -> [(rel, meta)]
    for rel, meta in sorted(files.items()):
        local = os.path.join(args.target, rel)
        if not needs_download(local, meta, log, cache):
            stats["unchanged"] += 1
            continue
        log(f"GET  {rel} ({(meta['size'] or 0)/1e6:.1f} MB)")
        if args.dry_run:
            stats["downloaded"] += 1
            continue
        todo.setdefault(os.path.dirname(local) or args.target, []).append(
            (rel, meta))

    def landed(rel, meta):
        lp = os.path.join(args.target, rel)
        return os.path.exists(lp) and (meta["size"] is None
                                       or os.path.getsize(lp) == meta["size"])

    def fetch(parent, items):
        """Download items into parent. Returns list of (rel, error) failures."""
        paths = [m["remote"] for _, m in items]
        said = ""
        try:
            out, err = cli.cmd("filesystem", "download", "-f", "replace", "-d",
                               "merge", *paths, local_arg(parent),
                               timeout=4 * 3600)
            said = (out + err).strip()
        except RateLimited:
            raise
        except Exception as e:
            said = str(e)
        # The interactive CLI reports a failed command in its output, not by
        # raising, and one bad file can fail a whole batch. Retry whatever
        # didn't land one at a time, so it costs only itself.
        missing = [it for it in items if not landed(*it)]
        if not missing:
            return []
        if len(items) == 1:
            return [(items[0][0], (said + cli.late_stderr()).strip()[-300:]
                     or "nothing written")]
        log(f"WARN {len(missing)} of {len(items)} in a batch didn't land; "
            f"retrying them one at a time")
        failures = []
        for one in missing:
            failures.extend(fetch(parent, [one]))
        return failures

    BATCH = 25
    for parent in sorted(todo):
        items = todo[parent]
        os.makedirs(parent, exist_ok=True)
        for i in range(0, len(items), BATCH):
            chunk = items[i:i + BATCH]
            try:
                failures = fetch(parent, chunk)
            except RateLimited as e:
                log(f"RATE LIMITED during download: {e}")
                return 2
            failed = {r for r, _ in failures}
            for rel, err in failures:
                stats["errors"] += 1
                log(f"ERROR downloading {rel}: {err}")
            # Do NOT trust the exit code. The CLI has been observed printing
            # "You need to login first" and still exiting 0 with nothing
            # written, so confirm each file actually landed at the expected
            # size. Otherwise a silent failure is recorded as a success and the
            # delete pass proceeds on a false picture.
            for rel, meta in chunk:
                if rel in failed:
                    continue
                lp = os.path.join(args.target, rel)
                try:
                    got = os.path.getsize(lp)
                except OSError:
                    stats["errors"] += 1
                    log(f"ERROR {rel}: download reported success but the file "
                        f"is not there")
                    continue
                if meta["size"] is not None and got != meta["size"]:
                    stats["errors"] += 1
                    log(f"ERROR {rel}: download reported success but size is "
                        f"{got}, expected {meta['size']}")
                    continue
                stats["downloaded"] += 1
                stats["bytes"] += meta["size"] or 0

    # Mirror deletions. Skipped entirely if the walk had errors -- a partial
    # listing would look like mass deletion and wipe good data.
    if args.no_delete:
        log("delete pass skipped (--no-delete)")
    elif walk_errors or stats["errors"]:
        log(f"DELETE PASS SKIPPED: {walk_errors} walk errors, "
            f"{stats['errors']} download errors -- refusing to delete on a "
            f"partial picture")
    else:
        # Trash instead of erase: a file removed in Proton lands in
        # <target>/<trash-dir>/<date>/<original path>, so an accidental deletion
        # upstream is recoverable. The trash dir is in LOCAL_KEEP, so the sync
        # never scans, re-downloads or deletes its contents.
        trash_root = None
        if args.trash_dir:
            trash_root = os.path.join(args.target, args.trash_dir,
                                      time.strftime("%Y-%m-%d"))
            log(f"deletions go to {args.trash_dir}/{time.strftime('%Y-%m-%d')}/")

        # NB: pruning via dirnames only works with topdown=True, so protected
        # paths are filtered by component instead.
        excluded = {local_name(x) for x in args.exclude}

        def protected(rel_path):
            parts = rel_path.split(os.sep)
            return bool(set(parts) & LOCAL_KEEP) or parts[0] in excluded

        for dirpath, dirnames, filenames in os.walk(args.target, topdown=False):
            for fn in filenames:
                full = os.path.join(dirpath, fn)
                rel = os.path.relpath(full, args.target)
                if protected(rel):
                    continue
                if rel not in files:
                    if trash_root:
                        dest = os.path.join(trash_root, rel)
                        log(f"TRASH {rel}")
                    else:
                        dest = None
                        log(f"DEL  {rel}")
                    if not args.dry_run:
                        try:
                            if dest:
                                os.makedirs(os.path.dirname(dest), exist_ok=True)
                                # Same file deleted twice in one day: keep the
                                # newest rather than failing on an existing path.
                                if os.path.exists(dest):
                                    os.remove(dest)
                                shutil.move(full, dest)
                            else:
                                os.remove(full)
                        except OSError as e:
                            stats["errors"] += 1
                            log(f"ERROR removing {rel}: {e}")
                    stats["deleted"] += 1
            rel_d = os.path.relpath(dirpath, args.target)
            if rel_d == "." or protected(rel_d):
                continue
            if rel_d not in folders:
                remaining = [x for x in os.listdir(dirpath)
                             if x not in LOCAL_KEEP]
                if not remaining:
                    log(f"RMDIR {rel_d}")
                    if not args.dry_run:
                        shutil.rmtree(dirpath, ignore_errors=True)
                    stats["deleted"] += 1

    if args.hash_cache and not args.dry_run:
        live = {os.path.join(args.target, r) for r in files}
        try:
            tmp = args.hash_cache + ".tmp"
            json.dump({k: v for k, v in cache.items() if k in live}, open(tmp, "w"))
            os.replace(tmp, args.hash_cache)
        except OSError as e:
            log(f"WARN cannot write hash cache: {e}")
    if cli.restarts:
        log(f"note: the CLI restarted {cli.restarts} time(s) during this run")
    log(f"=== done{tag} in {time.time()-t0:.1f}s: "
        f"downloaded={stats['downloaded']} ({stats['bytes']/1e9:.2f} GB) "
        f"unchanged={stats['unchanged']} deleted={stats['deleted']} "
        f"errors={stats['errors'] + walk_errors}")
    return 1 if (stats["errors"] or walk_errors) else 0


if __name__ == "__main__":
    sys.exit(main())
