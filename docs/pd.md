# `pd` — manage Proton Drive from the command line

A thin wrapper around Proton's official CLI. Proton is the source of truth: make
changes here and the NAS mirror follows on its next run.

Paths without a leading `/` are assumed to be under `/my-files`, so
`pd ls videos` works. Escape a literal `/` inside a filename as `\/`.

## Install

```sh
install -m 0755 bin/pd ~/.local/bin/pd
```

Requires the official CLI at `~/.local/bin/proton-drive` and `python3`.
Get the CLI from <https://proton.me/download/drive/cli/index.html>, then:

```sh
proton-drive auth login
```

## Commands

### Looking around

```sh
pd ls                     # list /my-files
pd ls videos              # list a folder
pd ls /shared-with-me     # absolute paths work too
pd info videos/clip.mp4   # full metadata: sha1, size, revision
pd find clip              # match names in /my-files
pd find clip music        # ...or in a specific folder
```

`pd find` matches within **one** folder — it does not recurse, because the CLI has
no recursive listing and walking a large tree is one API call per folder.

### Adding files

```sh
pd add photo.jpg           # upload into /my-files
pd add *.mp4 videos        # upload into a folder
pd add ~/somedir videos    # folders work; uploaded recursively
```

Uses `-c skip`, so files that already exist are left alone rather than duplicated.
Existence is checked locally first, and it refuses rather than half-uploading if a
path is missing.

**`add` verifies itself.** After uploading it compares every file against
Proton by size and sha1, repairs anything missing or corrupt, and re-checks. So
re-running `pd add` on a partly-failed upload **converges** — which plain `add`
alone would not, because `-c skip` skips on existence rather than content and
would step straight over a corrupt file.

Use `pd add --no-verify ...` to skip the check on very large uploads, where
hashing everything locally costs real time.

#### Thumbnails are off by default

`add` passes `--skip-thumbnails`. This is deliberate, and it is the difference
between uploads that work and uploads that don't.

The CLI generates a thumbnail for anything it believes is an image and **rejects
the whole file** when it can't:

```text
ValidationError: Failed to generate thumbnails (use --skip-thumbnails to upload
without thumbnails): Image: format not supported on this machine (HEIC/AVIF/TIFF
require the OS codec...)
```

Two triggers, both ordinary in real directories:

- **TIFF, HEIC and AVIF** need an OS codec that often isn't installed.
- **Any file whose extension lies about its content** — a `.jpg` holding text, a
  `.gif` holding an executable — fails as `unrecognised format`.

One upload of ~3,500 files failed almost entirely this way. Turning thumbnails
off makes it a non-event, and uploads are quicker for it.

What you give up is preview images in Proton's web and mobile apps for files
uploaded this way. If you want them:

```sh
pd add --thumbs ~/photos
```

Even then you're covered: the repair pass always uses `--skip-thumbnails`, so
anything that fails thumbnail generation still gets uploaded on the second pass.

One failure this does *not* explain is a rejected mime type:

```text
ValidationError: The mime type of the file is invalid ("application/x-dosexec").
Allowed mime types are "application/octet-stream".
```

Not reproducible here. If files fail that way, they need renaming or repackaging.

#### What `add` overwrites, and what it leaves alone

> **`add` pushes local → Proton.** A file that exists in both but *differs* is
> overwritten with your local copy. If you edited it in Proton's web UI and your
> local copy is stale, that edit is lost. `add` prints a warning naming how many
> files it is about to replace.

Files that exist **only in Proton are never touched.** So the common case is safe:
create a file in the web UI inside a folder, then re-run `pd add` on your local
copy of that folder, and the new file stays exactly where it is. It's reported as
`EXTRA` — informational only, and it does **not** make the command fail.

Verified behaviour when re-adding a directory:

| Situation | Result |
| --- | --- |
| Local file, not in Proton | uploaded |
| Identical in both | left alone |
| Differs (you changed it locally) | **Proton copy overwritten** |
| Differs (you changed it in the web UI) | **Proton copy overwritten** — the risk |
| Exists only in Proton | **preserved**, listed as `EXTRA`, exit stays 0 |

Nothing here does two-way reconciliation. It can't reliably: the CLI cannot set
modification times, so there is no dependable way to tell which side is newer.
If you edit in the web UI, `pd get` the file before touching the local copy.

### Rearranging

```sh
pd mkdir 2026-trip                 # create in /my-files
pd mkdir videos clips              # create inside a folder
pd mv videos/a.mp4 2026-trip       # move
pd mv a.mp4 b.mp4 c.mp4 2026-trip  # several at once
pd cp videos/a.mp4 2026-trip       # copy
pd rn 2026-trip 2026-japan         # rename
```

`mv` and `cp` are **server-side and instant** — no download/re-upload, even for
gigabytes. The last argument is always the destination folder.

### Removing

```sh
pd rm old.mp4              # → Proton trash, recoverable
pd restore old.mp4         # undo that
pd purge old.mp4           # permanent; prompts, type DELETE to confirm
```

`pd rm` is the safe one and what you want almost always. Deletions also land in the
NAS mirror's own `.trash/<date>/` on the next sync, so there are two independent
safety nets.

### Checking an upload actually worked

```sh
pd add ~/stuff/project      # upload
pd verify ~/stuff/project   # compare every file: size + sha1
```

Reports `MISSING`, `SIZE`, `SHA1` or `EXTRA` per file and ends with `N/N
verified`. Exits non-zero on any problem, so it chains:

```sh
pd add ~/stuff/project && pd verify ~/stuff/project && rm -rf ~/stuff/project
```

To repair whatever it found:

```sh
pd verify --fix ~/stuff/project
```

Missing files are uploaded; corrupt ones are re-uploaded with `replace`, then it
re-checks automatically.

**Why `add` has to do this rather than just re-uploading.** The underlying
`-c skip` skips on *existence*, not content, so a file that uploaded short or
corrupt would be skipped on every retry and stay broken forever. Repeating a bare
upload fills in files that never arrived but never repairs damaged ones — which
is why `add` runs this check and re-uploads mismatches with `replace`.

For proof rather than a metadata check:

```sh
pd verify --deep ~/stuff/project
```

`--deep` downloads every file and re-hashes it. This matters because **Proton
does not recompute the sha1** — it stores whatever the uploading client claimed.
The fast check proves Proton's record matches your file; `--deep` proves the
bytes come back.

### Downloading

```sh
pd get videos/clip.mp4          # into current directory
pd get videos/clip.mp4 ~/Videos # into a directory
```

### Misc

```sh
pd share videos                        # sharing status
pd raw filesystem list /trash --json   # anything not wrapped
pd help
```

`pd raw` passes straight through to `proton-drive`, so nothing is locked away.

## Things that will bite you

**Listings can be stale for a few seconds after a change.** Trash a folder and
`pd ls` may still show it. Wait a moment and re-run; it isn't a failure.

**`/` is not a folder.** It's the sections root (`/my-files`, `/shared-with-me`,
`/trash`, …) and you can't create or upload into it. `pd` maps a bare `/` to
`/my-files` for you, which is what people mean.

**The NAS mirror is not instant.** It updates on its schedule. Nothing you do with
`pd` appears at `/media/proton` until the next sync run.

**Don't edit files on the NAS mount.** It's exported read-only precisely because
the mirror would overwrite your changes. Edit in Proton, or download, change, and
re-upload.

## Why a wrapper at all

The underlying commands are fine but verbose and absolute-path-only:

```sh
proton-drive filesystem move "/my-files/videos/a.mp4" "/my-files/2026-trip"
pd mv videos/a.mp4 2026-trip
```

`pd` also adds the confirmation on `purge`, local existence checks on `add`, and
size-annotated output for `find`.
