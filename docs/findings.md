# Findings

Everything below was established empirically. Recorded so nobody repeats it.

## Proton rejects rclone outright

rclone sends `X-Pm-Appversion: external-drive-rclone@<version>-stable` and gets:

```text
422 POST https://drive-api.proton.me/auth/v4
Code=2028: This version of the app is no longer supported, please update
```

Tried and rejected: `external-drive-rclone@1.75.0-stable` (rclone's default),
`external-drive-rclone@1.75.0` (dropping the pre-release suffix, in case Proton
does a semver comparison — `1.75.0-stable` sorts *below* `1.75.0`),
`external-drive-rclone@2.0.0`, and impersonating the official client as
`cli-drive@0.7.0`.

**Do not brute-force app version strings.** The impersonation attempt tripped
Proton's abuse protection:

```text
Code=2028: Our systems detected unusual activity targeting your account.
To protect you from potential compromise, we have temporarily limited access
```

That blocks *new logins* account-wide for a while. Existing sessions keep working.
Repeated failed auth also earns `429 Code=2011` with a ~1 hour retry-after.

Context from [Proton's own comments on the rclone
forum](https://forum.rclone.org/t/proton-drive-x-rclone/53609): Proton has never
officially supported rclone, only granted "gestures of goodwill" — including a
*temporary exemption* from mandatory upload-integrity checks that was never fixed
properly in rclone. A Proton engineer noted the app-version header matters
precisely because misidentifying it **bypasses data-integrity safeguards**. So
impersonation isn't just ineffective, it's a bad idea. There are also reports of
`"Item cannot be decrypted"` on files uploaded by rclone v1.75.0.

## Proton's prebuilt CLI needs AVX2; Synology Celerons don't have it

Symptom, and it is *nasty* to diagnose:

```text
exit=132              # 128 + 4 = SIGILL, illegal instruction
```

with **empty stdout and stderr**, which looks exactly like an auth failure if your
wrapper discards exit codes.

The CLI is TypeScript compiled with Bun, and Bun's default x64 target requires
AVX2. A Celeron J4125 (Gemini Lake) stops at `sse4_2`:

```text
$ grep -o -E 'avx2|avx|fma|sse4_2' /proc/cpuinfo | sort -u
sse4_2
```

Checking glibc is **not sufficient** — the binary only needs `GLIBC_2.17`, which
made it look broadly portable. Instruction set is the real constraint. Testing it
in a container on a modern laptop proves nothing, because the laptop has AVX2.

### The fix

Build it yourself with Bun's pre-AVX2 target. Proton publish the source at
[ProtonDriveApps/sdk](https://github.com/ProtonDriveApps/sdk) under `/cli`:

```sh
bun run build bun-linux-x64-baseline
```

See [`scripts/build-baseline-cli.sh`](../scripts/build-baseline-cli.sh). Gotchas:

- There is **no root workspace**. You must `bun install` separately in `cli/`,
  `client/js/` *and* `incubating/account/js/`, or the build fails on unresolved
  imports (`ttag`, `@noble/hashes`, `ky`) one layer at a time.
- Bun ≥ 1.3.14 required.
- Output lands in `cli/release/linux-x64-baseline/proton-drive`.
- Pin `CLI_VERSION`/`JS_VERSION` env vars — a shallow clone has no git tags, so
  version detection otherwise yields `0.0.0`.

The baseline build still contains some AVX2 opcodes (measured 1,750 versus 20,486
in the official build) but they sit in CPU-feature-guarded paths that never
execute. It runs fine on a J4125.

### The app identifier matters

A self-built CLI reports `external-drive-sdkclijs@<version>` — the SDK's own
identifier for forks, overridable via `CLI_APP_VERSION_NAME`. **Proton accepts
it.** That's the crucial difference from rclone's rejected string, and it means no
impersonation is needed.

## The CLI needs a keyring unless told otherwise

By default credentials go through `Bun.secrets` → `libsecret` → your desktop
keyring. In a container you get:

```text
Failed to load session from secrets: libsecret not available
```

`PROTON_DRIVE_CREDENTIALS_STORE` accepts `keychain` (default), `pass`, or
`unsafe_file`. The last writes a portable plaintext `auth-session.json` into the
app dir — the only workable option on a headless NAS.

`PROTON_DRIVE_CACHE_DIR` collapses cache/app/log into one directory, so the
container needs a single volume.

Only **two files** need transferring to seed a session: `auth-session.json` and
`clientUid.json`. Everything else regenerates.

### `auth login` works headlessly

Better than transferring anything: log in *on the NAS*. `auth login` is a browser
handoff, not a local-browser requirement. It prints a URL, then polls:

```text
INFO  [cli] Authenticating via web
DEBUG [cli] Checking authentication status
DEBUG [cli] Authentication not yet ready (2001: Invalid selector)
```

...every 5 s until you complete the sign-in on any device. So a container with a
TTY and the state volume mounted is enough, and the credential is then created on
the NAS and never exists anywhere else.

The URL goes to **stdout**, not into the CLI's log file — pipe the output through
something that buffers and you'll see nothing at all while it waits.

### There is no encrypted-at-rest option that stays unattended

Unattended means no human supplies a secret at run time, so the machine must hold
one it can read by itself. `pass` needs a passphrase-less GPG key (equivalent to
plaintext) or a passphrase after every boot; an encrypted Synology share needs
manual unlock after every reboot and the sync silently stops until you do it.
Use a dedicated, independently revocable session and keep the file `0600` in a
folder that is never exported.

## Revoking a session leaves four caches behind

After revoking a session in Proton, the CLI fails with:

```text
Invalid access token
```

...and `auth login` won't recover on its own, because local state still points at
the dead session. State lives in **four** places and `auth logout` only clears the
first:

1. the desktop keyring (`auth logout` handles this)
2. `~/.local/share/proton-drive-cli/`
3. `~/.cache/proton-drive-cli/` ← includes `cache-crypto.sqlite`, key material
4. `~/.local/state/proton-drive-cli/`

Remove 2–4 manually, then log in again.

## The first run after a cache rebuild failed: root-owned cache files

This failure was reproducible. Whenever the cache was cleared (new credentials, or
a manual reset), the next sync failed, and the run after it succeeded. The log
shows the cause:

```text
EACCES: permission denied, open '/state/events.json'
```

The CLI creates its cache files (`events.json`, `cache-*.sqlite`) whenever it
starts. `nas-task.sh` ran its CPU smoke test as the container's default user,
which is root, and that test was the first CLI start after a reset. So it
created those files owned by root with mode 0644. The sync then runs as
`$OWNER`, which can read them but not write them, and every section failed.

The next night's run fixed ownership at its start, which is why the run after
always worked. Retrying after 30 s couldn't help, because nothing changed the
ownership in between.

Fixed: the smoke test now runs as `$OWNER` and doesn't use `$BASE/state` at all.
It gets a throwaway cache inside its container instead. Its CLI runs as PID 1,
so if it left an event lock, the lock would read `{"pid":1}`, the value that
froze the mirror (below).

`nas-task.sh` also takes its run lock first, before writing the RUNNING marker,
installing staged files, or running the smoke test. The run lock used to come
after all of those. So a scheduled run that started during a long manual run
would delete the marker on its way out, and could run its smoke test against
the live state.

`nas-task.sh` still retries once after 30 s on `rc=1`, for transient errors. It
deliberately does *not* retry on `rc=2`, which means rate-limited and must back off.

## A leftover event lock silently freezes the mirror

The CLI keeps its cached folder tree current by following Proton's event stream.
One process at a time claims that job by writing its PID to `events.lock` in the
cache directory, and the lock counts as held while that PID is alive.

In a container, PID 1 always exists. In the sync container, PID 1 is the Python
engine. So a lock containing `{"pid":1}` never looks stale. On the NAS this
happened from the second night of syncing:

- `events.json` and `events.lock` stopped changing.
- The log showed "Disposing events manager" every run, but never "Updating latest
  event ID".
- For two months, folders added in Proton were missing from the mirror, with no
  error. Meanwhile the laptop's CLI, which has its own cache, listed them.

`nas-task.sh` now deletes `events.lock` right after taking its run lock, before
any CLI starts. To recover a cache that is already stale, also delete
`events.json` and `cache-*.sqlite*` once, as a session reset does.

To check it's healthy, look at `events.json`: its modification time should change
on every run.

## Other CLI behaviours

- `filesystem list` has **no recursion flag**. One call per folder.
- `/albums` and `/photos` are rejected: `"Path type albums is not supported"`.
  Photos have their own `photo`/`album` subcommands.
- `filesystem move` and `copy` are **server-side and instant** — 255 files moved in
  seconds with no re-upload.
- `filesystem download` accepts multiple paths, which is what makes batching work.
- Shared folders are addressed **by name**, not by UID, despite the help text's
  `/shared-with-me/NODE-UID/file.txt` example. UID form returns
  `Root node not found`.
- **Listings can be stale for a few seconds after a mutation.** A folder you just
  trashed may still appear.
- Undecryptable filenames exist in the wild; the engine skips and logs them.

### Upload speed is set by Proton, not by your connection

Measured on a ~335 Mb/s symmetric line, with a speed test confirming the line
itself reaches ~340 Mb/s up:

| Upload | Rate | Limited by |
| --- | --- | --- |
| tiny files (≤100 KB) | a few files per second, ~0.1–0.3 MB/s | API round trips for each file |
| one 62 MB file | ~5.7 MB/s (~46 Mb/s) | 4 MB blocks to Frankfurt/Zurich storage, ~1 MB/s each, ~12 in parallel |

So a tree with tens of thousands of small files is slow however fast the uplink
is, and a faster plan wouldn't help. Large files are the fast path.

### A folder's first listing is slow on the NAS

`filesystem list` decrypts every entry's name. With an empty cache, the NAS
(Celeron J4125, baseline build) manages about 800 names a minute per CLI
process, so a 7,000-file folder takes about 9 minutes the first time. Once
cached, the same listing takes seconds; the laptop lists 4,961 entries in
1.5 s.

`pd_sync.py` used to give each listing 300 s, which killed exactly these
first listings. Every run then failed on the same folders, never mirrored
them, and skipped its delete pass. The limit is now 1 hour, which only guards
against a hung CLI.

### CLI processes can't share a cache in parallel

Every CLI process opens the same SQLite cache in `$BASE/state`. That cache uses
WAL mode with a 5-second busy timeout. When the mirror listed with 4 processes
at once, the CLI logged 63 `database is locked` errors in two hours. Some
listings then failed with `Node not found` for folders that exist. So each
engine runs exactly one CLI process (see the interactive mode, below), and the
mirror and the backup use separate sessions and cache directories.

A separate cache per process is not a way around this either: each would need
its own copy of the session, and copies break as soon as one refreshes its
token.

### Recognising a real rate limit

When the SDK gives up on HTTP 429, the CLI prints `RateLimitedError: Too many
server requests, please try again later` to stderr and exits 1. Before it gives
up, the SDK retries by itself, honouring `retry-after`. `pd_sync.py` matches
only that wording, or `Code=2011`, and only on stderr.

It used to search everything, stdout included, for `2011`. That matched a file
name (`…v20110828…`) in a listing and ended a 5-hour walk as "rate limited".

The CLI also occasionally exits 0 with its JSON output cut short. This has
happened 8 times on the NAS and never on the laptop. `pd_sync.py` re-lists up to
3 times before counting it as an error.

### The CLI has an interactive mode, and it's ~30× faster

Started with no arguments, `proton-drive` reads one command per line from stdin
and prints `proton-drive> ` when each one is done. Session and cache stay warm
between commands. Driven that way (`nas/pd_repl.py`):

- a listing takes ~50 ms, against ~1.5 s for a fresh CLI on the laptop and
  several seconds on the NAS;
- the whole drive (82,500 files, 7,300 folders) walks in about a minute on the
  laptop. The NAS's old one-process-per-folder walk took 8.2 hours.

Details that matter when you drive it from a program:

- **There's no exit code.** Judge each command by its result: a listing parses
  as JSON, and an upload appears in a listing afterwards. Recoverable errors are
  printed to stderr and the prompt comes back. Anything else ends the process,
  so restart it.
- **stderr is a separate pipe and can trail the prompt.** Don't wait for it
  after every command: 50 ms each added 6 minutes to a full walk. Only wait when
  a command looks failed.
- **Quoting is POSIX-like.** Use double quotes, with `\"` and `\\` escapes.
  A newline can't be passed at all.
- **No progress bars.** Upload and download draw them only on a terminal.

### Address nodes by uid when a name can't be typed

Path segments may be node uids: `/my-files/<uid>` reaches any node in My Files
directly, and so does `/my-files/<any path>/<uid>`. Under `/shared-with-me`, the
share itself must be given by name, then uids below it. Uids are the way to
reach a name containing a newline; there was one, since removed. They're also
the way to act on one exact file.

Walking entirely by uid works, but it's about 6× slower than by name, because
the CLI resolves each uid separately rather than from its folder cache. Use
names, and fall back to uids.

### Trash is looked up by name, first match wins

`filesystem delete` works only on trashed items, given as `/trash/<name>`, and
it takes the **first** trashed item with that name. With a common name, such as
`cover.jpg`, that may be someone else's file. To permanently delete one exact
file:

1. rename it, by uid, to a unique name;
2. trash it;
3. delete `/trash/<unique name>`.

`pd_push.py` does this whenever it replaces a changed file.

### Local paths are wildcard patterns if they contain `* ? [ {`

The CLI glob-expands any local path containing `*`, `?`, `[` or `{`, for both
upload sources and download destinations. So a real folder like
`Album [FLAC]` or `Nevermind {Deluxe}` matches nothing. The command fails with
`No paths matched`, and for an upload batch that means none of it uploads.
The first music backup lost 989 of 1,586 files to this. Every one of them had
a bracket or brace in its path.

Escape `* ? [ ] { } \` with a backslash, but only when the path contains a
trigger character; without one, the path is taken literally.
`pd_repl.local_arg()` does this for both engines, and `pd` does it for paths
that exist.

### The CLI can't download a name containing a backslash

A file named `back\slash.flac` uploads fine, but `filesystem download` writes
nothing for it. Run in a batch through the interactive CLI, that one file used
to fail all 25 files. `pd_sync.py` now retries whatever a batch didn't deliver
one at a time, so only the file itself fails.

### An upload hangs if the file grows during it

Append to a file while the CLI uploads it, and the upload never finishes; it
doesn't fail either. `pd_push.py` gives every upload command a timeout, at least
30 minutes. It also stats each file before and after uploading, and leaves out
any file that changed.

### An interrupted upload leaves nothing behind

Kill the CLI 25 s into a 300 MB upload, and nothing appears in the folder: no
partial file, no `(1)`. The next upload of the same file lands exactly once, at
full size. This was the open question for a duplicate-free backup design. It's
covered by the `pd_push.py` test suite.

### qBittorrent writes unfinished downloads under their final names

With the default settings (no `.!qB` extension, no incomplete folder), a torrent
that's 1% done is a full-size, mostly empty sparse file with its real name. Some
had sat like that for over a year. A backup can't tell them apart by name or
age. `pd_push.py` reads qBittorrent's resume data (`BT_backup/*.fastresume`) for
completion, and also leaves out any sparse file.

### `upload -f skip` re-reads everything it skips

With `-f skip`, the CLI doesn't trust names. It reads and hashes each local file
whose name already exists remotely, on a single core, before skipping it.
Resuming a 106 GB tree that was half uploaded re-read about 55 GB of
already-uploaded data at ~22 MB/s, uploading almost nothing for over an hour. For
repeated runs, keep your own record of what's been uploaded and give the CLI only
new files.

### Thumbnail generation blocks uploads

The single biggest cause of bulk-upload failures. The CLI tries to generate a
thumbnail for anything it believes is an image and **rejects the whole file** if
it can't:

```text
ValidationError: Failed to generate thumbnails (use --skip-thumbnails to upload
without thumbnails): Image: format not supported on this machine
Image: unrecognised format (expected JPEG, PNG, WebP, GIF, BMP, TIFF, HEIC or AVIF)
```

Two triggers, both ordinary:

- **TIFF/HEIC/AVIF** need an OS codec that frequently isn't installed.
- **Extensions that lie about content** — a `.jpg` holding text, a `.gif` holding
  an executable. Real archives are full of these.

One upload of ~3,500 files failed almost entirely on this.

`-t` / `--skip-thumbnails` fixes it. Reproduced with a TIFF, a text file named
`.jpg`, and a DOS executable named `.gif`: all three failed without `-t`, all
three uploaded with it.

`pd add` therefore passes `-t` by default; `--thumbs` opts back in, and even then
the repair pass uses `-t` so failures still land. Thumbnails are a preview
convenience in the Proton apps, not something worth failing an upload over.

Not fixed by `-t`, and not reproducible here:

```text
ValidationError: The mime type of the file is invalid ("application/x-dosexec").
Allowed mime types are "application/octet-stream".
```

### Existing thumbnails cannot be removed without re-uploading

Thumbnails are **detectable** even though the CLI exposes nothing about them:
they are counted in the revision's `storageSize`, so comparing it against
`claimedSize` (the plaintext size) gives them away. Same 7,831-byte PNG uploaded
twice:

| upload | storageSize | overhead |
| --- | --- | --- |
| default | 10026 | ~2195 bytes |
| with `-t` | 7912 | ~81 bytes (encryption only) |

Anything over a few hundred bytes of overhead has a thumbnail.

Removing one is another matter. `-f replace` does **not** work, because the CLI
skips content-identical files outright:

```text
Transfer summary:
  Uploaded: 0 items (0 B)
```

The only thing that strips a thumbnail is deleting the remote file and uploading
it again (verified: overhead 2195 → 81).

That makes retro-active pruning a bad trade. On one real Drive, 266 files carried
~4.4 MiB of thumbnail data, and reclaiming it would mean deleting and re-uploading
3.44 GB — while breaking share links and revision history for each file. Turn
thumbnails off going forward and leave the existing ones alone.

### "You need to login first" is often a lie

A single `filesystem list` can fail with:

```text
You need to login first
```

...while the session is perfectly valid — running the same command immediately
afterwards succeeds, and other commands work throughout. The CLI loads and
refreshes its session on *every* invocation, so there is a window in which a call
can fail spuriously.

This matters for any script making many calls in a row: one blip aborts the whole
run and looks exactly like being logged out. `pd` retries three times with
backoff before believing it.

Check whether you are actually logged out with a plain `pd ls` before
re-authenticating. Re-logging in unnecessarily creates yet another session and
invalidates the one the NAS may be using.

### `-c skip` skips whole folders, not just files

`filesystem upload -c skip` applies the strategy to folders too, so re-uploading
a directory that already exists in Proton skips **the entire directory** and
uploads nothing inside it:

```text
Transfer summary:
  Uploaded: 0 items (0 B)
  Skipped: 1 items
  - projects_
```

That is not what anyone means by "skip existing". Use `-d merge -f skip`:
`merge` descends into the existing folder, and `skip` then applies to the files
within it, leaving identical ones alone.

### A failed download can still exit 0

Downloading from `/shared-with-me` on one machine printed:

```text
You need to login first
```

...wrote nothing, and **exited with status 0**. The session was fine — listing
worked, and `/my-files` downloads on the same machine worked. Whatever the real
cause, the lesson is that the exit code cannot be trusted on its own.

`pd_sync.py` therefore confirms every downloaded file exists at the expected size
before counting it as downloaded. Without that, a silent failure is recorded as
success and the delete pass then runs against a false picture of what synced.

### The CLI renames files when it writes them to disk

This one silently breaks change detection, and it took a "why is exactly one file
downloaded every single run?" to notice.

Proton had `1:13:2018 MAIL.txt`. On disk the CLI wrote `1_13_2018 MAIL.txt`. The
sync then looked for the original name, never found it, and re-downloaded it every
run forever. On a **mirrored** section it's worse: the sanitised file isn't in the
expected set, so it gets deleted as extraneous and re-downloaded — permanent churn,
and with `--trash-dir` a new trash copy every single run.

The exact rule, lifted from the CLI binary:

```js
fF0 = /[\x00-\x1f\x7f<>:"|?*\\/]/g
FX(name) => name.replace(fF0, "_")   // then "" -> "_", "." -> "_", ".." -> "__"
```

So control characters and `< > : " | ? * \ /` all become `_`. `pd_sync.py`
reimplements this as `local_name()` and keys its local index by the sanitised
path while keeping true names for remote calls.

Two different remote names can sanitise to the same local name (`a:b` and `a?b`
both become `a_b`). The engine logs the collision and skips the second rather than
letting them overwrite each other.

Symptom to watch for: a run that reports a small non-zero `downloaded=` with
`0.00 GB` transferred, every time, with everything else `unchanged`.

## Synology / DSM gotchas

- **DSM 7.3.1+ removed the Docker package.** Container Manager only.
- **File Station silently refuses to overwrite files it lacks write permission
  on.** An update can appear to apply while the old file is still in place —
  always verify by hash. Staging `<name>.new` files that a root task installs
  avoids the whole problem.
- `chown -R` to your own user can lock out the `admin` account doing the uploads.
  Group-writable (`g+rwX` with group `users`) keeps both able to manage the files.
- **Never overwrite a running shell script.** `sh` reads incrementally, so
  replacing the file mid-run makes it resume at a byte offset in different
  content: `syntax error: unexpected end of file`. The bootstrap copies the script
  and runs the copy.
- DSM Task Scheduler shows **no "currently running" state**, and doesn't save
  script output anywhere by default. Redirect to a file yourself, and publish a
  marker file so you can tell whether a run is in flight.
- `@eaDir` (and `#recycle`) are recreated constantly by DSM inside shares. A
  mirror must protect them **by path component**, not just at top level.
- Mount propagation note for desktops: a FUSE/NFS mount created on the host does
  appear inside a Flatpak sandbox if the sandbox's view of the parent is `slave`.

## NFS export settings that work

Cloned from a long-working share, with the privilege flipped:

```text
Squash   : Map all users to admin
Security : sys
Privilege: Read only
[x] Enable asynchronous
[x] Allow connections from non-privileged ports
[x] Allow users to access mounted subfolders
```

`Map all users to admin` means client uid mismatches don't matter — the server
evaluates access as `admin` regardless of the local user's uid.

Read-only is deliberate: the mirror would overwrite anything you wrote there, so
read-only turns silent data loss into a loud error.

Client side, matching options that fail fast instead of hanging:

```text
noauto,x-systemd.automount,x-systemd.mount-timeout=10,x-systemd.idle-timeout=600,
_netdev,soft,timeo=30,retrans=2,retry=0,x-gvfs-show,ro
```

Note `systemctl daemon-reload` regenerates the automount unit but does **not**
start it; the first time you need `systemctl start media-<name>.automount`.
