# Findings

Everything below was established empirically. Recorded so nobody repeats it.

## Proton rejects rclone outright

rclone sends `X-Pm-Appversion: external-drive-rclone@<version>-stable` and gets:

```
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

```
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

```
exit=132              # 128 + 4 = SIGILL, illegal instruction
```

with **empty stdout and stderr**, which looks exactly like an auth failure if your
wrapper discards exit codes.

The CLI is TypeScript compiled with Bun, and Bun's default x64 target requires
AVX2. A Celeron J4125 (Gemini Lake) stops at `sse4_2`:

```
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

```
Failed to load session from secrets: libsecret not available
```

`PROTON_DRIVE_CREDENTIALS_STORE` accepts `keychain` (default), `pass`, or
`unsafe_file`. The last writes a portable plaintext `auth-session.json` into the
app dir — the only workable option on a headless NAS.

`PROTON_DRIVE_CACHE_DIR` collapses cache/app/log into one directory, so the
container needs a single volume.

Only **two files** need transferring to seed a session: `auth-session.json` and
`clientUid.json`. Everything else regenerates.

## Revoking a session leaves four caches behind

After revoking a session in Proton, the CLI fails with:

```
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

## The first run after a cache rebuild fails

Reproducible: whenever the session cache is cleared (new credentials, cleared
cache), the next sync fails, and the run after that succeeds. Observed repeatedly.

Not fully understood. `nas-task.sh` absorbs it by retrying once after 30 s on
`rc=1` — deliberately *not* on `rc=2`, which means rate-limited and must back off.

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
- Undecryptable filenames and names containing a literal `/` exist in the wild;
  the engine skips and logs them rather than guessing.

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

```
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

```
noauto,x-systemd.automount,x-systemd.mount-timeout=10,x-systemd.idle-timeout=600,
_netdev,soft,timeo=30,retrans=2,retry=0,x-gvfs-show,ro
```

Note `systemctl daemon-reload` regenerates the automount unit but does **not**
start it; the first time you need `systemctl start media-<name>.automount`.
