# Setup

Assumes a Synology NAS with an x86-64 CPU and a Linux desktop. Adjust the
placeholders: `NAS_IP`, `LAN_CIDR`, and the uid/gid that owns your shares.

## 0. Prerequisites

- **Container Manager** installed (Package Center). DSM 7.3.1+ removed the old
  Docker package.
- A **shared folder** for the mirror — this guide uses `proton`. **Do not encrypt
  it**: an encrypted share needs manual unlock after every reboot, which would
  silently break the scheduled sync.
- A second share you can write to over the network, used as a staging area.
  Any existing one works.
- Container Manager → **Registry** → download **`python:3-slim`**. Do not create a
  container; the scheduled task invokes the image directly.

## 1. Build the CLI for your NAS's CPU

Skip only if your NAS CPU has AVX2 (`grep avx2 /proc/cpuinfo` on the NAS). Most
Synology Celerons do not — see [findings](findings.md).

On a machine with Bun (or let the script install it):

```sh
./scripts/build-baseline-cli.sh
```

Produces `proton-drive-baseline`. Verify:

```sh
./proton-drive-baseline --version
# Proton Drive CLI external-drive-sdkclijs@0.7.0
```

## 2. Create a dedicated session for the NAS

Use a **separate** session from your desktop's, so it can be revoked
independently. The NAS has no keyring, so it must be the plaintext store.

> **This is a plaintext credential.** Anyone who reads `auth-session.json` has
> your whole Drive. Keep it out of any exported path, and never inside the
> synced folder. This is inherent to unattended operation, not a bug: nobody is
> present to supply a secret at 3am, so the machine has to hold one it can read.

### Preferred: log in on the NAS itself

`auth login` is a browser handoff — it prints a URL, polls every 5 s, and
completes when you finish signing in on *any* device. So it works headlessly, and
the credential is then born on the NAS and never exists anywhere else. No copies
in transit, on your desktop, or in a staging share.

It needs a TTY. Either enable SSH temporarily and run:

```sh
docker run --rm -it \
  -e PROTON_DRIVE_CREDENTIALS_STORE=unsafe_file \
  -e PROTON_DRIVE_CACHE_DIR=/state -e HOME=/state \
  -v /volume1/docker/protondrive/bin/proton-drive:/opt/proton-drive:ro \
  -v /volume1/docker/protondrive/state:/state \
  python:3-slim /opt/proton-drive auth login
```

...or, without SSH: in Container Manager create a container from `python:3-slim`
with those two volumes and the command `sleep infinity`, start it, open its
**Terminal**, run `/opt/proton-drive auth login` there, then delete the container.
The `state` volume persists.

Note the URL is printed on **stdout**, not written to the CLI's log file — don't
pipe the output somewhere you can't read it.

### Alternative: mint on your desktop and copy

Simpler, at the cost of the credential existing in two places (and in whatever
you copy it with):

```sh
mkdir -p ~/proton-nas-creds && chmod 700 ~/proton-nas-creds
PROTON_DRIVE_CREDENTIALS_STORE=unsafe_file \
PROTON_DRIVE_CACHE_DIR=$HOME/proton-nas-creds \
proton-drive auth login
chmod 600 ~/proton-nas-creds/*
```

Only `auth-session.json` and `clientUid.json` need to go to the NAS. If you use
the staging mechanism, the task shreds the staged copies after installing them.

### Why not something encrypted at rest

| Option | Why it doesn't work headlessly |
| --- | --- |
| `PROTON_DRIVE_CREDENTIALS_STORE=pass` | GPG-encrypted, but needs either a passphrase-less key (equivalent to plaintext) or a passphrase typed after every boot |
| Encrypted shared folder for `state/` | Real encryption at rest, but requires manual unlock after every reboot, and the sync silently stops until you do |
| Docker/DSM secrets | DSM has no secrets service for containers |

The workable posture is: a dedicated, independently revocable session; created on
the NAS; `0600` in a folder that is never exported; rotated when you like.

## 3. Lay out files on the NAS

Target layout:

```text
/volume1/docker/protondrive/
├── bin/proton-drive        <- the baseline binary from step 1
├── sync/pd_sync.py         <- nas/pd_sync.py   (mirror engine)
├── sync/pd_repl.py         <- nas/pd_repl.py   (shared; both engines need it)
├── sync/pd_push.py         <- nas/pd_push.py   (only for the backup, step 9)
├── state/                  <- mirror session: auth-session.json + clientUid.json
├── state-push/             <- backup session (step 9), created automatically
└── log/                    <- created automatically
```

Copy them over however you like. Note that **File Station silently refuses to
overwrite files it lacks write permission on**, so for later updates prefer the
staging mechanism below and verify by hash.

## 4. Configure and stage the task script

Edit the config block at the top of `nas/nas-task.sh`:

```sh
BASE=/volume1/docker/protondrive   # where step 3 put things
DATA=/volume1/proton               # the mirror target share
STAGE=/volume1/<staging-share>/pd-staging   # writable over the network
OWNER=1026:100                     # uid:gid owning your shares
EXPECT_SHA=                        # optional: sha256 of your binary
SECTIONS="/my-files:my-files /shared-with-me:shared-with-me:no-delete"
MIRROR_EXCLUDE="_nas_backup"       # never mirror the backup back down
# only for the backup (step 9):
PUSH_JOBS="/volume1/music:_nas_backup/music /volume1/video:_nas_backup/video"
QBT_BACKUP=/volume1/docker/qbittorrent/config/qBittorrent/BT_backup
QBT_MAP="/downloads/music=/volume1/music/torrents /downloads/video=/volume1/video/torrents"
```

Find `OWNER` by looking at an existing file: `ls -n` on any share.

Put `nas-task.sh` in the staging directory. Updating it there is how you deploy
changes later, without touching DSM again.

## 5. Create the scheduled task

Control Panel → **Task Scheduler** → Create → Scheduled Task → **User-defined
script**. Run as **root**. Paste the contents of `nas/bootstrap.sh`, adjusting the
paths.

The bootstrap copies the script and runs the copy, so staging an update mid-run
can't corrupt the running shell.

Then **Run** it manually. Output goes to `$BASE/log/sync.log`, mirrored to
`$STAGE/sync.log` on exit. A `$STAGE/RUNNING` file exists while a run is in
flight, since DSM won't tell you.

Look for:

```text
binary: baseline build, verified
smoke test PASSED
=== done in ...: downloaded=N ... errors=0
```

Then set a schedule (nightly, off-hours).

The first run after setup, or after a cache reset, is the slow one. The NAS
decrypts every name once, at about 800 a minute. Later runs walk from the
warm cache.

## 6. Export over NFS

Control Panel → File Services → **NFS** → enable, max protocol NFSv4.1.

Shared Folder → `proton` → Edit → **NFS Permissions** → Create:

| Field | Value |
| --- | --- |
| Hostname or IP | `LAN_CIDR` e.g. `192.168.1.0/24` |
| Privilege | **Read only** |
| Squash | `Map all users to admin` |
| Security | `sys` |
| Enable asynchronous | checked |
| Allow connections from non-privileged ports | checked |
| Allow users to access mounted subfolders | checked |

Read-only is deliberate: the mirror would overwrite anything written there, so
read-only turns silent data loss into an error.

## 7. Mount it on the desktop

Add to `/etc/fstab` (one line):

```text
NAS_IP:/volume1/proton  /media/proton  nfs4  noauto,x-systemd.automount,x-systemd.mount-timeout=10,x-systemd.idle-timeout=600,_netdev,soft,timeo=30,retrans=2,retry=0,x-gvfs-show,ro  0 0
```

Then:

```sh
sudo mkdir -p /media/proton
sudo systemctl daemon-reload
sudo systemctl start media-proton.automount   # needed the first time only
```

`daemon-reload` generates the automount unit but does not start it; at boot it's
pulled in automatically.

Those options are what make it **fail gracefully**: `soft` + `timeo=30` +
`retry=0` + `mount-timeout=10` mean access errors out in ~10 s when the NAS is
unreachable instead of hanging, and `idle-timeout=600` unmounts when unused.

Verify:

```sh
ls /media/proton/my-files          # mounts on demand
touch /media/proton/my-files/x     # must fail: read-only
```

## 8. Install the management wrapper

```sh
install -m 0755 bin/pd ~/.local/bin/pd
pd ls
```

See [pd.md](pd.md). All changes go through Proton; the mirror follows.

## 9. Optional: back up NAS folders to Proton

`nas-task.sh push` uploads `PUSH_JOBS` (local folder → folder under
`/my-files`) with `nas/pd_push.py`. It's independent of the mirror:

| | Mirror | Backup |
| --- | --- | --- |
| Command | `nas-task.sh` | `nas-task.sh push` |
| Proton session | `$BASE/state` | `$BASE/state-push` |
| Lock | `sync.lock` | `push.lock` |
| Log | `log/sync.log` → `$STAGE/sync.log` | `log/push.log` → `$STAGE/push.log` |
| Running marker | `$STAGE/RUNNING` | `$STAGE/RUNNING-push` |

The two can run at the same time, and a second backup run exits at once.

**1. Log in a separate session for the backup.** Two processes can't share one
cache, and a copied session breaks as soon as either copy refreshes its token.
Over SSH:

```sh
sudo mkdir -p /volume1/docker/protondrive/state-push
sudo chown 1026:100 /volume1/docker/protondrive/state-push
sudo docker run --rm -it --user 1026:100 \
  -e PROTON_DRIVE_CREDENTIALS_STORE=unsafe_file \
  -e PROTON_DRIVE_CACHE_DIR=/state -e HOME=/state \
  -v /volume1/docker/protondrive/bin/proton-drive:/opt/proton-drive:ro \
  -v /volume1/docker/protondrive/state-push:/state \
  python:3-slim /opt/proton-drive auth login
```

Open the printed URL in a browser and approve it. The command finishes by
itself.

**2. Dry run.** `nas-task.sh push --dry-run` lists, per job, what would be
uploaded and what is left out, and why. Nothing is changed.

**3. First real runs, by hand.** `nas-task.sh push`. Large first uploads take
days: Proton accepts about 46 Mb/s per file. A run that stops (a reboot, a rate
limit) resumes from its manifest (`state-push/manifest-<name>.json`).

**4. Then schedule it,** for example weekly:
`/bin/sh /volume1/<staging>/pd-staging/nas-task.sh push`.

What a run leaves out, to pick up on a later run:

- files modified in the last 24 hours;
- files of torrents qBittorrent hasn't finished, read from `QBT_BACKUP`
  (qBittorrent writes unfinished downloads under their final names);
- sparse files (a download any client never finished);
- partial-download names (`*.!qB`, `*.part`, …);
- `@eaDir` and `#recycle`;
- any file that changes while it uploads.

Deletions on the NAS move the Proton copy to Proton's trash. The deletion pass
is skipped if the run had errors, if the source is empty, or if 3 or more files
and over 10% of the backup would go. Pass `--allow-mass-delete` to override
the last one.

## Desktop file manager note

An NFS mount looks *local* to KDE/GNOME, so thumbnailers will happily generate
previews for every video over the network. In Dolphin: open the folder, then
**View → uncheck Show Previews** (a per-folder setting).

## Updating later

Drop files into the staging directory and the next run installs them:

| Staged file | Installed as |
| --- | --- |
| `proton-drive.new` | `$BASE/bin/proton-drive` |
| `pd_sync.py.new` | `$BASE/sync/pd_sync.py` |
| `pd_push.py.new` | `$BASE/sync/pd_push.py` |
| `pd_repl.py.new` | `$BASE/sync/pd_repl.py` |
| `auth-session.json.new` | `$BASE/state/auth-session.json` (+ clears stale cache) |
| `nas-task.sh` | used directly on the next run |

Staged credentials are shredded after install so tokens don't linger on a share.
