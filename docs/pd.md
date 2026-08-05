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
