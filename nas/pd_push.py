#!/usr/bin/env python3
"""Back up a local tree to Proton Drive (local -> Proton), never making copies.

One-way: the NAS share is the source of truth. Mirrors deletions into Proton's
trash. Built so it can never create duplicates or extra versions:

* The CLI never resolves a name clash: uploads use `-f skip`, and only after
  this script has confirmed the remote name is free. Never replace/keep-both.
* A file already in Proton under our name but not in the manifest is adopted
  only if size and sha1 match; anything else is reported and left alone.
* A changed file is replaced by permanently deleting the old copy first, so no
  version history accumulates.
* After every batch the remote folder must hold exactly the expected names.

Left out, to be picked up by a later run: files modified in the last
--min-age-hours, files of torrents qBittorrent hasn't finished (it writes
unfinished downloads under their final names), sparse files (an unfinished
download from any client), partial-download suffixes, and files that change
while they upload.

The manifest (path -> [size, mtime_ns, sha1, uid]) makes reruns cheap and
resumable: unchanged files cost no API calls.

Exit codes: 0 ok, 1 errors or anomalies, 2 rate limited (retry later).
"""
import argparse
import hashlib
import json
import os
import sys
import time
import unicodedata

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pd_repl import Repl, CliError, RateLimited, esc, local_arg  # noqa: E402

SKIP_DIRS = {"@eaDir", "#recycle", "@tmp", ".AppleDouble"}
SKIP_FILES = {".DS_Store", "Thumbs.db", "desktop.ini"}
PARTIAL_SUFFIXES = (".!qb", ".part", ".!ut", ".crdownload", ".tmp")
BIDI = {0x200e, 0x200f, 0x202a, 0x202b, 0x202c, 0x202d, 0x202e}


def nfc(s):
    return unicodedata.normalize("NFC", s)


def sha1_of(path):
    h = hashlib.sha1()
    with open(path, "rb") as f:
        while True:
            b = f.read(1 << 22)
            if not b:
                return h.hexdigest()
            h.update(b)


# ------------------------------------------------------------ qBittorrent ----
def bdecode(b, i=0):
    c = b[i:i + 1]
    if c == b"i":
        j = b.index(b"e", i)
        return int(b[i + 1:j]), j + 1
    if c == b"l":
        i += 1
        out = []
        while b[i:i + 1] != b"e":
            v, i = bdecode(b, i)
            out.append(v)
        return out, i + 1
    if c == b"d":
        i += 1
        out = {}
        while b[i:i + 1] != b"e":
            k, i = bdecode(b, i)
            v, i = bdecode(b, i)
            out[k] = v
        return out, i + 1
    j = b.index(b":", i)
    n = int(b[i:j])
    return b[j + 1:j + 1 + n], j + 1 + n


def lt_name(s):
    """libtorrent drops bidi control characters from file names."""
    return "".join(c for c in s if ord(c) not in BIDI)


def unfinished_files(qbt_dir, mapping, log, src="/"):
    """Absolute local paths of every file belonging to a torrent qBittorrent
    hasn't finished. mapping: container save path prefix -> local prefix."""
    out, n_tor, n_unf = set(), 0, 0
    if not qbt_dir:
        return out
    for fn in sorted(os.listdir(qbt_dir)):
        if not fn.endswith(".fastresume"):
            continue
        h = fn[:-len(".fastresume")]
        try:
            fr, _ = bdecode(open(os.path.join(qbt_dir, fn), "rb").read())
            t, _ = bdecode(open(os.path.join(qbt_dir, h + ".torrent"), "rb").read())
        except (OSError, ValueError, IndexError) as e:
            log(f"WARN cannot read qBittorrent resume data {h}: {e}")
            continue
        n_tor += 1
        info = t.get(b"info", t)
        sp = (fr.get(b"qBt-savePath") or fr.get(b"save_path") or b"").decode("utf-8", "replace").rstrip("/")
        local = None
        for pre, loc in mapping:
            if sp == pre or sp.startswith(pre + "/"):
                local = loc + sp[len(pre):]
                break
        src_dir = os.path.normpath(src)
        if local is None or not (os.path.normpath(local) + "/").startswith(src_dir.rstrip("/") + "/"):
            continue                      # saves somewhere outside this source
        pieces = fr.get(b"pieces", b"")
        if pieces and all(x & 1 for x in pieces):
            continue                      # complete
        n_unf += 1
        name = lt_name(info.get(b"name.utf-8", info.get(b"name", b"")).decode("utf-8", "replace"))
        mapped = [m.decode("utf-8", "replace") for m in fr.get(b"mapped_files", [])]
        if b"files" in info:
            rels = ["/".join(lt_name(p.decode("utf-8", "replace")) for p in f.get(b"path.utf-8", f[b"path"]))
                    for f in info[b"files"]]
            rels = [os.path.join(name, r) for r in rels]
        else:
            rels = [name]
        for i, r in enumerate(rels):
            if i < len(mapped) and mapped[i]:
                r = mapped[i]
            out.add(os.path.normpath(os.path.join(local, r)))
    log(f"qBittorrent: {n_tor} torrents, {n_unf} unfinished under this source "
        f"({len(out)} files left out)")
    return out


# ------------------------------------------------------------------ engine ---
class Push:
    def __init__(self, a, cli, log):
        self.a, self.cli, self.log = a, cli, log
        self.anomalies = []
        self.stats = {"uploaded": 0, "bytes": 0, "adopted": 0, "unchanged": 0,
                      "replaced": 0, "deleted": 0, "errors": 0}
        self.skipped = {}
        self.manifest = {}

    # -- manifest -------------------------------------------------------------
    def load_manifest(self):
        try:
            self.manifest = json.load(open(self.a.manifest))
        except FileNotFoundError:
            self.manifest = {}

    def save_manifest(self):
        if self.a.dry_run:
            return
        tmp = self.a.manifest + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.manifest, f)
        os.replace(tmp, self.a.manifest)

    def anomaly(self, msg):
        self.anomalies.append(msg)
        self.log(f"ANOMALY {msg}")

    def skip(self, why, rel):
        self.skipped.setdefault(why, []).append(rel)

    # -- local ------------------------------------------------------------------
    def scan_local(self, unfinished):
        src = self.a.src
        local, by_key, walk_errors = {}, {}, []
        self.present = set()        # NFC keys of every file on disk, skipped or not
        now = time.time()

        def onerror(e):
            walk_errors.append(str(e))

        for dp, dns, fns in os.walk(src, onerror=onerror):
            dns[:] = sorted(d for d in dns if d not in SKIP_DIRS)
            for fn in sorted(fns):
                full = os.path.join(dp, fn)
                rel = os.path.relpath(full, src)
                self.present.add(nfc(rel))
                if fn in SKIP_FILES or fn.startswith("._"):
                    continue
                if fn.lower().endswith(PARTIAL_SUFFIXES):
                    self.skip("partial-download name", rel)
                    continue
                if "\n" in rel or "\r" in rel:
                    self.anomaly(f"newline in name, not uploaded: {rel!r}")
                    continue
                try:
                    rel.encode("utf-8")
                except UnicodeEncodeError:
                    self.anomaly(f"name is not valid UTF-8, not uploaded: {rel!r}")
                    continue
                try:
                    st = os.lstat(full)
                except OSError as e:
                    walk_errors.append(str(e))
                    continue
                if not os.path.isfile(full) or os.path.islink(full):
                    continue
                if os.path.normpath(full) in unfinished:
                    self.skip("unfinished torrent", rel)
                    continue
                if st.st_size > (1 << 20) and st.st_blocks * 512 < 0.9 * st.st_size:
                    self.skip("sparse (unfinished download)", rel)
                    continue
                if now - st.st_mtime < self.a.min_age_hours * 3600:
                    self.skip("modified recently", rel)
                    continue
                key = nfc(rel)
                if key in by_key:
                    self.anomaly(f"two local names normalise to {key!r}: {by_key[key]!r} and {rel!r}; both skipped")
                    local.pop(key, None)
                    continue
                by_key[key] = rel
                local[key] = (full, st.st_size, st.st_mtime_ns)
        return local, walk_errors

    # -- remote -----------------------------------------------------------------
    def ensure_folder(self, path_parts):
        """Make sure /root/<parts...> exists; return its CLI path."""
        cur = self.a.remote
        for part in path_parts:
            child = f"{cur}/{esc(part)}"
            names = {nfc(e["name"]["value"]): e for e in self.cli.list(cur)
                     if (e.get("name") or {}).get("ok")}
            hit = names.get(nfc(part))
            if hit is None:
                if self.a.dry_run:
                    return None
                self.cli.cmd("filesystem", "create-folder", cur, part)
                names = {nfc(e["name"]["value"]): e for e in self.cli.list(cur)
                         if (e.get("name") or {}).get("ok")}
                hit = names.get(nfc(part))
                if hit is None:
                    raise CliError(f"create-folder {child} not confirmed")
            if hit["type"] != "folder":
                raise CliError(f"{child} exists and is not a folder")
            cur = child
        return cur

    def ensure_root(self):
        parts = self.a.remote.strip("/").split("/")
        cur = "/" + parts[0]
        for part in parts[1:]:
            names = {e["name"]["value"]: e for e in self.cli.list(cur)
                     if (e.get("name") or {}).get("ok")}
            if part not in names:
                if self.a.dry_run:
                    return False
                self.cli.cmd("filesystem", "create-folder", cur, part)
                if part not in {e["name"]["value"] for e in self.cli.list(cur)
                                if (e.get("name") or {}).get("ok")}:
                    raise CliError(f"could not create {cur}/{part}")
            cur = f"{cur}/{esc(part)}"
        return True

    def scan_remote(self):
        files, folders = {}, {"": self.a.remote}
        pending = [(self.a.remote, "")]
        while pending:
            path, prefix = pending.pop()
            for e in self.cli.list(path):
                nm = e.get("name") or {}
                if not nm.get("ok"):
                    self.anomaly(f"undecryptable name under {prefix or '/'}")
                    continue
                rel = f"{prefix}/{nm['value']}".lstrip("/")
                if e.get("type") == "folder":
                    folders[nfc(rel)] = f"{path}/{esc(nm['value'])}"
                    pending.append((folders[nfc(rel)], rel))
                else:
                    rev = e.get("activeRevision") or {}
                    files[nfc(rel)] = {"uid": e["uid"], "size": rev.get("claimedSize"),
                                       "sha1": ((rev.get("claimedDigests") or {}).get("sha1") or "").lower(),
                                       "name": nm["value"]}
        return files, folders

    def remote_entries(self, folder_path):
        return {nfc(e["name"]["value"]): e for e in self.cli.list(folder_path)
                if (e.get("name") or {}).get("ok")}

    def purge(self, uid, name):
        """Permanently delete one remote file. Trash lookups are by name and
        take the first match, so rename it to something unique first."""
        tmp = f"{name}.pd-push-purge-{uid[-10:]}-{int(time.time())}"
        self.cli.cmd("filesystem", "rename", f"/my-files/{uid}", tmp)
        self.cli.cmd("filesystem", "trash", f"/my-files/{uid}")
        self.cli.cmd("filesystem", "delete", f"/trash/{esc(tmp)}")
        if any(e.get("uid") == uid for e in self.cli.list("/trash")):
            raise CliError(f"purge of {name} not confirmed (still in trash as {tmp})")

    # -- upload one folder's batch ---------------------------------------------
    def upload_batch(self, folder_path, items, known):
        """items: [(key, full, size, mtime_ns)]; known: names expected in the
        folder already. Returns False if the folder must not be touched again."""
        pre = []
        for key, full, size, mtime in items:
            pre.append((key, full, size, mtime, sha1_of(full)))
        total = sum(x[2] for x in pre)
        before = {k: e["uid"] for k, e in self.remote_entries(folder_path).items()}
        said = ""
        try:
            out, err = self.cli.cmd("filesystem", "upload", "-t", "-f", "skip", "-d", "merge",
                                    *[local_arg(x[1]) for x in pre], folder_path,
                                    timeout=max(1800, int(total / 1.5e6) + 900))
            said = (out + err).strip()
        except RateLimited:
            raise
        except CliError as e:
            said = str(e)
            self.log(f"WARN upload command failed ({said[:200]}); checking what landed")
        reported = False
        entries = self.remote_entries(folder_path)
        expected = set(known)
        for key, full, size, mtime, digest in pre:
            name = nfc(os.path.basename(full))
            e = entries.get(name)
            rev = (e or {}).get("activeRevision") or {}
            ok = (e is not None and rev.get("claimedSize") == size and
                  ((rev.get("claimedDigests") or {}).get("sha1") or "").lower() == digest)
            try:
                st = os.stat(full)
                unchanged = (st.st_size, st.st_mtime_ns) == (size, mtime)
            except OSError:
                unchanged = False
            if e is not None and before.get(name) == e["uid"]:
                # It was there before this upload, so it isn't ours: never touch.
                self.anomaly(f"{key}: a different file with this name appeared in Proton; not touched")
                continue
            if e is not None and (not ok or not unchanged):
                why = "changed while uploading" if not unchanged else "landed with wrong size/sha1"
                self.log(f"{why}: {key}; removing the remote copy")
                try:
                    self.purge(e["uid"], e["name"]["value"])
                except CliError as x:
                    self.anomaly(f"could not remove bad remote copy of {key}: {x}")
                    return False
                self.skip(why, key)
                continue
            if e is None:
                if not unchanged:
                    self.skip("changed while uploading", key)
                    continue
                self.stats["errors"] += 1
                self.log(f"ERROR upload not confirmed: {key}")
                if not reported:          # once per batch: what the CLI said
                    reported = True
                    self.log("  the upload command said: "
                             + (said + self.cli.late_stderr()).strip().replace("\n", " | ")[-600:])
                continue
            expected.add(name)
            self.manifest[key] = [size, mtime, digest, e["uid"]]
            self.stats["uploaded"] += 1
            self.stats["bytes"] += size
        self.save_manifest()
        extra = set(self.remote_entries(folder_path)) - expected
        if extra:
            self.anomaly(f"unexpected names in {folder_path}: {sorted(extra)[:10]}; folder left alone")
            return False
        return True

    # -- main -----------------------------------------------------------------
    def run(self):
        a = self.a
        self.load_manifest()
        if not os.path.isdir(a.src) or not os.listdir(a.src):
            self.log(f"FATAL source missing or empty: {a.src} -- nothing done")
            return 1
        unfinished = unfinished_files(a.qbt, a.qbt_map, self.log, a.src)
        local, walk_errors = self.scan_local(unfinished)
        for e in walk_errors[:20]:
            self.log(f"ERROR reading local tree: {e}")
        self.log(f"local: {len(local)} files, {sum(v[1] for v in local.values())/1e9:.1f} GB; "
                 + ", ".join(f"skipped {len(v)} {k}" for k, v in sorted(self.skipped.items())))

        if not self.ensure_root():
            remote_files, remote_folders = {}, {"": a.remote}
        else:
            remote_files, remote_folders = self.scan_remote()
        self.log(f"remote: {len(remote_files)} files, {len(remote_folders) - 1} folders")

        new, changed = [], []
        for key, (full, size, mtime) in sorted(local.items()):
            m = self.manifest.get(key)
            r = remote_files.get(key)
            if m and r and r["uid"] == m[3]:
                if m[0] == size and m[1] == mtime:
                    self.stats["unchanged"] += 1
                else:
                    changed.append(key)
                continue
            if r is not None:
                if r["size"] == size and r["sha1"] and r["sha1"] == sha1_of(full):
                    self.manifest[key] = [size, mtime, r["sha1"], r["uid"]]
                    self.stats["adopted"] += 1
                else:
                    self.anomaly(f"{key} already in Proton with different content; not touched")
                continue
            if m:
                self.manifest.pop(key, None)  # gone remotely: upload again
            new.append(key)
        self.save_manifest()

        gone = [k for k in self.manifest if k not in self.present]
        nbytes = sum(local[k][1] for k in new)
        self.log(f"plan: {len(new)} new ({nbytes/1e9:.2f} GB), {len(changed)} changed, "
                 f"{len(gone)} deleted locally, {self.stats['unchanged']} unchanged, "
                 f"{self.stats['adopted']} adopted")
        if a.dry_run:
            return self.finish(walk_errors)

        # uploads, folder by folder
        by_folder = {}
        for key in new:
            by_folder.setdefault(os.path.dirname(key), []).append(key)
        for folder, keys in sorted(by_folder.items()):
            try:
                path = remote_folders.get(folder) or self.ensure_folder(folder.split("/") if folder else [])
                remote_folders[folder] = path
                known = {nfc(n) for n in self.remote_entries(path)}
                for i in range(0, len(keys), a.batch):
                    chunk = [(k, *local[k]) for k in keys[i:i + a.batch]]
                    if not self.upload_batch(path, chunk, known):
                        break
                    known |= {nfc(os.path.basename(c[1])) for c in chunk}
            except RateLimited:
                raise
            except CliError as e:
                self.stats["errors"] += 1
                self.log(f"ERROR in folder {folder or '/'}: {e}")

        # changed files: one version only -- purge the old copy, then upload
        for key in changed:
            full, size, mtime = local[key]
            old = remote_files[key]
            try:
                self.purge(old["uid"], old["name"])
                self.manifest.pop(key, None)
                self.save_manifest()
                path = remote_folders.get(os.path.dirname(key)) or self.ensure_folder(os.path.dirname(key).split("/"))
                known = set(self.remote_entries(path))
                before = self.stats["uploaded"]
                self.upload_batch(path, [(key, full, size, mtime)], known)
                if self.stats["uploaded"] > before:
                    self.stats["uploaded"] -= 1
                    self.stats["replaced"] += 1
            except RateLimited:
                raise
            except CliError as e:
                self.stats["errors"] += 1
                self.log(f"ERROR replacing {key}: {e}")

        # deletions -> Proton trash, with guards
        if walk_errors or self.stats["errors"]:
            self.log("DELETE PASS SKIPPED: errors this run -- refusing to delete on a partial picture")
        elif len(gone) >= 3 and len(gone) > 0.10 * len(self.manifest) and not a.allow_mass_delete:
            self.anomaly(f"{len(gone)} of {len(self.manifest)} backed-up files are gone locally "
                         f"(>10%); delete pass skipped. Rerun with --allow-mass-delete if intended.")
        else:
            trash_uids = None
            for key in gone:
                uid = self.manifest[key][3]
                try:
                    self.cli.cmd("filesystem", "trash", f"/my-files/{uid}")
                    if trash_uids is None or uid not in trash_uids:
                        trash_uids = {e.get("uid") for e in self.cli.list("/trash")}
                    if uid in trash_uids:
                        self.manifest.pop(key)
                        self.stats["deleted"] += 1
                        self.log(f"TRASH {key}")
                    else:
                        self.stats["errors"] += 1
                        self.log(f"ERROR trash not confirmed: {key}")
                except CliError as e:
                    if "not found" in str(e).lower():
                        self.manifest.pop(key)   # already gone remotely
                    else:
                        self.stats["errors"] += 1
                        self.log(f"ERROR trashing {key}: {e}")
            self.save_manifest()
        return self.finish(walk_errors)

    def finish(self, walk_errors):
        s = self.stats
        tag = " (DRY RUN)" if self.a.dry_run else ""
        self.log(f"=== done{tag}: uploaded={s['uploaded']} ({s['bytes']/1e9:.2f} GB) "
                 f"replaced={s['replaced']} adopted={s['adopted']} unchanged={s['unchanged']} "
                 f"trashed={s['deleted']} errors={s['errors'] + len(walk_errors)} "
                 f"anomalies={len(self.anomalies)}")
        for why, rels in sorted(self.skipped.items()):
            self.log(f"  left out ({why}): {len(rels)}" + (f", e.g. {rels[0]}" if rels else ""))
        if self.cli.restarts:
            self.log(f"note: the CLI restarted {self.cli.restarts} time(s)")
        return 1 if (s["errors"] or walk_errors or self.anomalies) else 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--src", required=True, help="local folder to back up")
    ap.add_argument("--remote", required=True, help="Proton folder, e.g. /my-files/_nas_backup/music")
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--qbt", help="qBittorrent BT_backup folder (read-only)")
    ap.add_argument("--qbt-map", action="append", default=[], metavar="SAVEPATH=LOCAL",
                    help="qBittorrent save-path prefix -> local path, e.g. /downloads/music=/src/music/torrents")
    ap.add_argument("--min-age-hours", type=float, default=24)
    ap.add_argument("--batch", type=int, default=25)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--allow-mass-delete", action="store_true")
    ap.add_argument("--log")
    a = ap.parse_args()
    if not a.remote.startswith("/my-files/") or a.remote.rstrip("/") == "/my-files":
        ap.error("--remote must be a folder under /my-files")
    a.remote = a.remote.rstrip("/")
    a.qbt_map = [tuple(x.split("=", 1)) for x in a.qbt_map]

    logf = open(a.log, "a") if a.log else None

    def log(msg):
        line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
        print(line, flush=True)
        if logf:
            logf.write(line + "\n")
            logf.flush()

    log(f"=== push start{' (DRY RUN)' if a.dry_run else ''}: {a.src} -> {a.remote}")
    cli = Repl(os.environ.get("PROTON_DRIVE_BIN", "proton-drive"))
    p = Push(a, cli, log)
    try:
        return p.run()
    except RateLimited as e:
        p.save_manifest()
        log(f"RATE LIMITED: {e}")
        return 2
    finally:
        cli.close()


if __name__ == "__main__":
    sys.exit(main())
