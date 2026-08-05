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
import queue
import shutil
import subprocess
import sys
import threading
import time

CLI = os.environ.get("PROTON_DRIVE_BIN", "proton-drive")

# Artifacts the NAS itself creates inside the share. Never download, never
# delete -- DSM recreates @eaDir constantly and fighting it is pointless.
LOCAL_KEEP = {"@eaDir", "#recycle", ".DS_Store", "@tmp", ".trash"}


def esc(name):
    """Proton path syntax: literal / in a name is backslash-escaped."""
    return name.replace("\\", "\\\\").replace("/", "\\/")


class RateLimited(Exception):
    pass


def run_cli(args, attempts=5, timeout=1800):
    delay = 3.0
    last = ""
    for n in range(attempts):
        p = subprocess.run([CLI, *args], capture_output=True, text=True,
                           timeout=timeout)
        if p.returncode == 0:
            return p.stdout
        # Keep BOTH streams and the return code. Truncating this to 300 chars
        # from the front hid the real message behind a banner of '=' characters
        # for several debugging rounds -- never do that again.
        err = (p.stderr or "").strip()
        out = (p.stdout or "").strip()
        last = (f"rc={p.returncode}"
                + (f" | stderr: {err}" if err else "")
                + (f" | stdout: {out}" if out else "")
                + (" | (both streams empty -- likely killed by a signal)"
                   if not err and not out else ""))
        if "2011" in last or "too many requests" in last.lower():
            raise RateLimited(last[:400])
        if n == attempts - 1:
            break
        time.sleep(delay)
        delay *= 2
    raise RuntimeError(f"{' '.join(args)}: {last[:4000]}")


def walk(root, workers, log):
    """Return (files, folders) as dicts keyed by path relative to root."""
    files, folders = {}, {}
    pending = queue.Queue()
    pending.put(root)
    lock = threading.Lock()
    stop = threading.Event()
    err = {"rate_limited": None, "count": 0}

    def rel(path):
        return path[len(root):].lstrip("/")

    def worker():
        while not stop.is_set():
            try:
                path = pending.get(timeout=2)
            except queue.Empty:
                return
            try:
                entries = json.loads(run_cli(
                    ["filesystem", "list", path, "--json"],
                    timeout=300) or "[]")
            except RateLimited as e:
                with lock:
                    err["rate_limited"] = str(e)
                stop.set()
                pending.task_done()
                return
            except Exception as e:
                with lock:
                    err["count"] += 1
                    log(f"ERROR listing {path}: {e}")
                pending.task_done()
                continue

            for e in entries:
                nm = e.get("name") or {}
                if not nm.get("ok"):
                    with lock:
                        err["count"] += 1
                        log(f"SKIP undecryptable name, uid={e.get('uid')}")
                    continue
                name = nm["value"]
                if "/" in name or name in ("", ".", ".."):
                    with lock:
                        err["count"] += 1
                        log(f"SKIP unmappable name {name!r} in {path}")
                    continue
                child = f"{path}/{esc(name)}"
                rev = e.get("activeRevision") or {}
                if e.get("type") == "folder":
                    with lock:
                        folders[rel(child)] = True
                    pending.put(child)
                else:
                    with lock:
                        files[rel(child)] = {
                            "remote": child,
                            "size": rev.get("claimedSize",
                                            rev.get("storageSize")),
                            "sha1": (rev.get("claimedDigests") or {}).get("sha1"),
                        }
            pending.task_done()

    ts = [threading.Thread(target=worker, daemon=True) for _ in range(workers)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()

    if err["rate_limited"]:
        raise RateLimited(err["rate_limited"])
    return files, folders, err["count"]


def sha1_of(path, buf=1 << 20):
    h = hashlib.sha1()
    with open(path, "rb") as f:
        while chunk := f.read(buf):
            h.update(chunk)
    return h.hexdigest()


def needs_download(local, meta, log):
    if not os.path.exists(local):
        return True
    try:
        if meta["size"] is not None and os.path.getsize(local) != meta["size"]:
            return True
        if meta["sha1"]:
            return sha1_of(local) != meta["sha1"].lower()
        # No remote hash and size matches: assume unchanged.
        return False
    except OSError as e:
        log(f"WARN stat/hash failed for {local}: {e}; will re-download")
        return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="/my-files")
    ap.add_argument("--target", required=True)
    ap.add_argument("--workers", type=int, default=4)
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

    t0 = time.time()
    try:
        files, folders, walk_errors = walk(args.root, args.workers, log)
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
    # `filesystem download` takes `path... localFolder`, and process startup
    # (a ~112MB Bun binary) dominates per-file cost, so batching is a large win:
    # measured 8.1s/file one-at-a-time.
    todo = {}  # local parent dir -> [(rel, meta)]
    for rel, meta in sorted(files.items()):
        local = os.path.join(args.target, rel)
        if not needs_download(local, meta, log):
            stats["unchanged"] += 1
            continue
        log(f"GET  {rel} ({(meta['size'] or 0)/1e6:.1f} MB)")
        if args.dry_run:
            stats["downloaded"] += 1
            continue
        todo.setdefault(os.path.dirname(local) or args.target, []).append(
            (rel, meta))

    def fetch(parent, items):
        """Download items into parent. Returns list of (rel, error) failures."""
        paths = [m["remote"] for _, m in items]
        try:
            run_cli(["filesystem", "download", "-f", "replace", "-d", "merge",
                     *paths, parent])
            return []
        except RateLimited:
            raise
        except Exception as e:
            if len(items) == 1:
                return [(items[0][0], str(e))]
            # Isolate the culprit rather than failing the whole batch.
            log(f"WARN batch of {len(items)} failed ({str(e)[:120]}); "
                f"retrying individually")
            failures = []
            for one in items:
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
            for rel, meta in chunk:
                if rel not in failed:
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
        def protected(rel_path):
            return bool(set(rel_path.split(os.sep)) & LOCAL_KEEP)

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

    log(f"=== done{tag} in {time.time()-t0:.1f}s: "
        f"downloaded={stats['downloaded']} ({stats['bytes']/1e9:.2f} GB) "
        f"unchanged={stats['unchanged']} deleted={stats['deleted']} "
        f"errors={stats['errors'] + walk_errors}")
    return 1 if (stats["errors"] or walk_errors) else 0


if __name__ == "__main__":
    sys.exit(main())
