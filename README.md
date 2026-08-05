# proton-drive-nas

One-way mirror of **Proton Drive** onto a **Synology NAS**, using Proton's own
official CLI — no rclone, no reverse-engineered API, no browser automation.

The NAS copy is then re-exported over NFS so it shows up as an ordinary folder on
your Linux desktop, with graceful failure when the NAS is unreachable.

```
Proton Drive ──(official CLI, in a container on the NAS)──> /volume1/proton
                                                                  │
                                                            NFS (read-only)
                                                                  ▼
                                                            /media/proton
```

Proton is the **source of truth**. You add, delete and rearrange files in Proton
(there's a small CLI wrapper, [`pd`](docs/pd.md), to make that painless) and the
NAS mirror follows on its next run.

## Why this exists

Proton Drive has no Linux sync client (a GUI one is in development, no committed
date). The obvious options both fail on a NAS:

| Approach | Problem |
|---|---|
| `rclone mount` on the desktop | Online-only filesystem on a laptop that suspends; blocks on `stat` when the network is away |
| `rclone sync` on the NAS | Proton **rejects rclone**: `422 Code=2028, "This version of the app is no longer supported"` |
| Proton's official CLI on the NAS | Prebuilt binary **requires AVX2**; most Synology Intel CPUs don't have it → `SIGILL` |
| Official GUI client | Doesn't exist yet |

The fix is to **build Proton's CLI yourself** from their published SDK, targeting
Bun's pre-AVX2 `baseline` build. That produces a binary that runs on a Celeron
J-series NAS *and* is accepted by Proton's API.

See [docs/findings.md](docs/findings.md) for the full set of dead ends, with
evidence — worth reading before you try to "simplify" any of this.

## What's here

| Path | What it is |
|---|---|
| [`bin/pd`](bin/pd) | Wrapper around the official CLI for day-to-day file management |
| [`nas/pd_sync.py`](nas/pd_sync.py) | The mirror engine. Walks Proton, diffs by sha1, downloads changes, trashes removals |
| [`nas/nas-task.sh`](nas/nas-task.sh) | What DSM Task Scheduler actually runs |
| [`nas/bootstrap.sh`](nas/bootstrap.sh) | The few lines you paste into DSM, once |
| [`scripts/build-baseline-cli.sh`](scripts/build-baseline-cli.sh) | Builds the AVX2-free CLI binary |
| [docs/setup.md](docs/setup.md) | Step-by-step install |
| [docs/pd.md](docs/pd.md) | `pd` command reference |
| [docs/findings.md](docs/findings.md) | Why it's built this way; what doesn't work |

## How the sync decides what to copy

Change detection uses the **sha1 that Proton reports for each file**, compared
against the local copy. Deliberately *not* modification times: the CLI cannot set
mtimes, so an mtime-based sync would re-copy everything forever. (This is also why
`rclone bisync` is unusable against Proton.)

Consequences:

- A re-run with nothing changed transfers nothing.
- Verification is real: a file is "unchanged" only if its bytes hash correctly.
- Every run reads local files to hash them, so runtime scales with data size.

## Design decisions worth knowing

**Downloads are batched.** Process startup dominates cost — a ~112 MB Bun binary
per invocation. Measured: **8.1 s/file** one-at-a-time versus **0.82 s/file** in
batches of 25. A failed batch retries its members individually to isolate the bad
file.

**Deletions go to trash, not oblivion.** Files removed upstream move to
`<target>/.trash/<date>/<original path>`. An accidental delete or botched
rearrange in Proton is recoverable from the NAS. Prune old dated folders yourself.

**Shared folders are additive, never mirrored.** If someone revokes a folder they
shared with you, `/shared-with-me` lists as *empty* — indistinguishable from "they
deleted everything". A mirror would wipe your copy. Sections marked `no-delete`
never remove anything.

**Each section gets its own subdirectory.** Required, not cosmetic: two sections
sharing one directory would each delete the other's files as "extraneous".

**The delete pass is skipped if the walk had any errors.** A partial listing must
never be mistaken for mass deletion.

**Walk cost is folder count, not bytes.** One `filesystem list` call per folder —
the CLI has no recursion flag. A 2-folder tree walks in ~20 s; a 251-folder tree
takes ~18 minutes regardless of size. Put big shared trees on their own schedule.

## Security

The NAS has no desktop keyring, so the CLI is run with
`PROTON_DRIVE_CREDENTIALS_STORE=unsafe_file`. It's named accurately: **the session
token is stored in plaintext** in `state/auth-session.json`.

- Anyone who reads that file can access your entire Drive.
- Keep it off any exported/shared path. Never inside the synced folder.
- Use a **dedicated session** for the NAS so you can revoke it independently.
- Revoking a session does **not** clean up the client. See
  [docs/findings.md](docs/findings.md#revoking-a-session-leaves-four-caches-behind).

## Status

Working, but assembled against a moving target. Proton has said that once their
SDK lands they'll deprecate older clients, so expect to rebuild the binary
occasionally. `scripts/build-baseline-cli.sh` exists for that.

Not implemented: two-way sync (would need conflict resolution and a local state DB,
given no mtime support), `/photos` and `/albums` (the CLI rejects them —
`"Path type albums is not supported"`).

## License

MIT — see [LICENSE](LICENSE).
