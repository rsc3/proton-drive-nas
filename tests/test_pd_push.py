#!/usr/bin/env python3
"""End-to-end tests for nas/pd_push.py (and the mirror's download path) against
a throwaway Proton folder, /my-files/_pd_push_test. Needs a logged-in CLI.

    python3 tests/test_pd_push.py            # everything (~40 min: T10/T11 are slow)
    QUICK=1 python3 tests/test_pd_push.py    # skip the interrupted/growing-upload tests

The remote test folder and everything the tests send to the trash are purged
at the end, by uid (trash lookups by name could hit someone else's file).
"""
import os, shutil, signal, subprocess, sys, tempfile, time, unicodedata

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NAS = os.path.join(REPO, "nas")
sys.path.insert(0, NAS)
from pd_repl import Repl, esc  # noqa: E402

CLI = os.environ.get("PROTON_DRIVE_BIN", os.path.expanduser("~/.local/bin/proton-drive"))
TOP = "/my-files/_pd_push_test"
REMOTE = TOP + "/music"
W = tempfile.mkdtemp(prefix="pd_push_test_")
SRC, MAN, QBT = os.path.join(W, "src"), os.path.join(W, "manifest.json"), os.path.join(W, "qbt")
OLD = time.time() - 3 * 86400
QUICK = bool(os.environ.get("QUICK"))
results = []


def nfc(s):
    return unicodedata.normalize("NFC", s)


def check(name, cond, detail=""):
    results.append((name, bool(cond)))
    print(f"{'PASS' if cond else 'FAIL'}  {name}  {detail}", flush=True)


def write(rel, data, old=True):
    p = os.path.join(SRC, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as f:
        f.write(data)
    if old:
        os.utime(p, (OLD, OLD))
    return p


def push(*extra, background=False, src=None):
    cmd = [sys.executable, os.path.join(NAS, "pd_push.py"), "--src", src or SRC, "--remote", REMOTE,
           "--manifest", MAN, "--qbt", QBT, "--qbt-map", f"/downloads/music={SRC}/torrents", *extra]
    env = dict(os.environ, PROTON_DRIVE_BIN=CLI, PYTHONDONTWRITEBYTECODE="1")
    if background:
        return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env)
    p = subprocess.run(cmd, capture_output=True, text=True, env=env)
    out = p.stdout + p.stderr
    done = [l for l in out.splitlines() if "=== done" in l]
    return p.returncode, out, (done[-1] if done else out[-600:])


def remote_tree(r, root=REMOTE):
    out, stack = {}, [(root, "")]
    while stack:
        path, pre = stack.pop()
        for e in r.list(path):
            n = e["name"]["value"]
            rel = f"{pre}/{n}".lstrip("/")
            if e["type"] == "folder":
                stack.append((f"{path}/{esc(n)}", rel))
            else:
                out[nfc(rel)] = (e["uid"], (e.get("activeRevision") or {}).get("claimedSize"))
    return out


def trash_uids(r):
    return {e.get("uid") for e in r.list("/trash")}


def bencode(x):
    if isinstance(x, int): return b"i%de" % x
    if isinstance(x, str): x = x.encode()
    if isinstance(x, bytes): return b"%d:%s" % (len(x), x)
    if isinstance(x, list): return b"l" + b"".join(bencode(i) for i in x) + b"e"
    return b"d" + b"".join(bencode(k) + bencode(v) for k, v in sorted(x.items())) + b"e"


def purge_everything(r):
    """Remove the test folder and every item it sent to the trash, by uid."""
    top = next((e for e in r.list("/my-files") if e["name"]["value"] == TOP.rsplit("/", 1)[1]), None)
    if not top:
        return
    uids, stack = {top["uid"]}, [TOP]
    while stack:
        p = stack.pop()
        for e in r.list(p):
            if e["type"] == "folder":
                uids.add(e["uid"]); stack.append(p + "/" + esc(e["name"]["value"]))

    def purge(uid, trashed):
        tmp = f"pd-test-purge-{uid[-12:]}-{int(time.time())}"
        r.cmd("filesystem", "rename", f"/my-files/{uid}", tmp)
        if not trashed:
            r.cmd("filesystem", "trash", f"/my-files/{uid}")
        r.cmd("filesystem", "delete", f"/trash/{tmp}")
    for e in [e for e in r.list("/trash") if e.get("parentUid") in uids]:
        purge(e["uid"], True)
    purge(top["uid"], False)
    left = [e for e in r.list("/trash") if e.get("parentUid") in uids or e.get("uid") == top["uid"]]
    print(f"cleanup: test folder purged; left in trash: {len(left)}")


def main():
    os.makedirs(SRC); os.makedirs(QBT)
    r = Repl(CLI)
    if any(e["name"]["value"] == TOP.rsplit("/", 1)[1] for e in r.list("/my-files")):
        sys.exit(f"{TOP} already exists; remove it first")
    r.cmd("filesystem", "create-folder", "/my-files", TOP.rsplit("/", 1)[1])
    try:
        run(r)
    finally:
        purge_everything(r)
        r.close()
        shutil.rmtree(W, ignore_errors=True)
    print("\n%d/%d passed" % (sum(ok for _, ok in results), len(results)))
    return 0 if all(ok for _, ok in results) else 1


def run(r):
    # ---- T1 first upload -----------------------------------------------------
    write("album A/01 song.flac", os.urandom(3_000_000))
    write("album A/02 it's \"quoted\".flac", os.urandom(200_000))
    write("album A/cover.jpg", os.urandom(50_000))
    write("deep/b/c/track.mp3", os.urandom(400_000))
    write(unicodedata.normalize("NFD", "Café Tacvba/Re.flac"), os.urandom(100_000))
    write("loose file.txt", b"hello\n")
    write("@eaDir/thumb.jpg", b"x")
    expected = {nfc(x) for x in ["album A/01 song.flac", "album A/02 it's \"quoted\".flac", "album A/cover.jpg",
                                 "deep/b/c/track.mp3", "Café Tacvba/Re.flac", "loose file.txt"]}
    rc, out, done = push()
    tree = remote_tree(r)
    check("T1 first run uploads every eligible file", rc == 0 and set(tree) == expected, done)
    check("T1 no (1)/conflict names", not any("(1)" in k or "onflict" in k for k in tree))

    # ---- T2 rerun: nothing to do ---------------------------------------------
    rc, out, done = push()
    check("T2 rerun uploads nothing", rc == 0 and "uploaded=0" in done and "plan: 0 new" in out, done)

    # ---- T3 changed file: one version only -----------------------------------
    old_uid = tree["album A/cover.jpg"][0]
    write("album A/cover.jpg", os.urandom(60_000))
    os.utime(os.path.join(SRC, "album A/cover.jpg"), (OLD + 100, OLD + 100))
    rc, out, done = push()
    tree = remote_tree(r)
    new = tree.get("album A/cover.jpg")
    check("T3 changed file replaced", rc == 0 and "replaced=1" in done and new and new[0] != old_uid and new[1] == 60_000, done)
    check("T3 old copy purged, not left in trash", old_uid not in trash_uids(r))
    check("T3 still exactly the expected names", set(tree) == expected)

    # ---- T4 unexpected same-named remote file --------------------------------
    write("album A/03 new.flac", os.urandom(70_000))
    other = os.path.join(W, "03 new.flac")
    open(other, "wb").write(os.urandom(70_000))
    r.cmd("filesystem", "upload", "-t", "-f", "skip", other, f"{REMOTE}/album A")
    before = remote_tree(r)["album A/03 new.flac"]
    rc, out, done = push()
    after = remote_tree(r).get("album A/03 new.flac")
    check("T4 foreign file reported and left alone", rc == 1 and "different content" in out and after == before, done)
    os.remove(os.path.join(SRC, "album A/03 new.flac"))

    # ---- T5 skip rules -------------------------------------------------------
    write("fresh/new today.flac", os.urandom(10_000), old=False)
    p = os.path.join(SRC, "sparse/half done.flac"); os.makedirs(os.path.dirname(p))
    with open(p, "wb") as f:
        f.truncate(50 << 20)
    os.utime(p, (OLD, OLD))
    write("dl/video.mkv.part", os.urandom(10_000))
    write("torrents/Some Album/01.flac", os.urandom(80_000))
    write("torrents/Done Album/01.flac", os.urandom(80_000))
    info = {"name": "Some Album", "piece length": 16384, "pieces": b"\0" * 100,
            "files": [{"path": ["01.flac"], "length": 80_000}]}
    open(os.path.join(QBT, "aa.torrent"), "wb").write(bencode({"info": info}))
    open(os.path.join(QBT, "aa.fastresume"), "wb").write(bencode({"qBt-savePath": "/downloads/music", "pieces": b"\x01\x00\x00\x01\x01"}))
    open(os.path.join(QBT, "bb.torrent"), "wb").write(bencode({"info": dict(info, name="Done Album")}))
    open(os.path.join(QBT, "bb.fastresume"), "wb").write(bencode({"qBt-savePath": "/downloads/music", "pieces": b"\x01" * 5}))
    rc, out, done = push()
    tree = remote_tree(r)
    check("T5 recent file left out", "fresh/new today.flac" not in tree and "modified recently" in out)
    check("T5 sparse file left out", "sparse/half done.flac" not in tree and "sparse" in out)
    check("T5 .part left out", "dl/video.mkv.part" not in tree)
    check("T5 unfinished torrent left out", "torrents/Some Album/01.flac" not in tree and "unfinished torrent" in out)
    check("T5 finished torrent uploaded", "torrents/Done Album/01.flac" in tree)

    # ---- T12 names the CLI would glob-expand: [ ] { } * ? \ ------------------
    glob_files = ["Nirvana - Nevermind {Deluxe} [FLAC]/01 Smells [Remaster].flac",
                  "Nirvana - Nevermind {Deluxe} [FLAC]/02 plain.flac",
                  "odd/star*q?.flac", "odd/back\\slash [x].flac"]
    for g in glob_files:
        write(g, os.urandom(30_000))
    rc, out, done = push()
    tree = remote_tree(r)
    check("T12 paths with [ ] { } * ? \\ upload under their real names",
          all(nfc(g) in tree and tree[nfc(g)][1] == 30_000 for g in glob_files) and "not confirmed" not in out, done)

    # ---- T13 the mirror downloads into folders with those characters ---------
    mirror = os.path.join(W, "mirror")
    os.makedirs(mirror)
    env = dict(os.environ, PROTON_DRIVE_BIN=CLI, PYTHONDONTWRITEBYTECODE="1")
    p = subprocess.run([sys.executable, os.path.join(NAS, "pd_sync.py"), "--root", REMOTE, "--target", mirror],
                       capture_output=True, text=True, env=env)
    import pd_sync
    # The CLI can't download a name containing a backslash at all (it writes
    # nothing); that one must fail on its own without sinking its batch.
    good = [g for g in glob_files if "\\" not in g]
    want = [os.path.join(mirror, *[pd_sync.local_name(x) for x in g.split("/")]) for g in good]
    errs = [l for l in p.stdout.splitlines() if "ERROR" in l]
    check("T13 mirror downloads into folders with [ ] { } (names sanitised like the CLI)",
          all(os.path.exists(w) and os.path.getsize(w) == 30_000 for w in want),
          [l for l in p.stdout.splitlines() if "=== done" in l])
    check("T13 one undownloadable file fails alone, not its batch",
          len(errs) == 1 and "back_slash" in errs[0], errs[:2])

    # ---- T6 deletion goes to Proton trash ------------------------------------
    uid = tree["loose file.txt"][0]
    os.remove(os.path.join(SRC, "loose file.txt"))
    rc, out, done = push()
    check("T6 local deletion trashed in Proton", "loose file.txt" not in remote_tree(r) and uid in trash_uids(r), done)

    # ---- T7 mass-delete guard ------------------------------------------------
    aside = os.path.join(W, "aside")           # move files away and back unchanged
    moved = ["album A"] + [g for g in glob_files]
    for m in moved:
        dst = os.path.join(aside, m)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.move(os.path.join(SRC, m), dst)
    rc, out, done = push()
    check("T7 >10% deleted refused without flag", "delete pass skipped" in out and "album A/01 song.flac" in remote_tree(r), done)
    for m in moved:
        shutil.move(os.path.join(aside, m), os.path.join(SRC, m))

    # ---- T8 empty source -----------------------------------------------------
    empty = os.path.join(W, "empty"); os.makedirs(empty)
    rc, out, done = push(src=empty)
    check("T8 empty source does nothing", rc == 1 and "FATAL source missing or empty" in out)

    # ---- T9 lost manifest: adopt, don't re-upload ----------------------------
    n_before = len(remote_tree(r))
    os.remove(MAN)
    rc, out, done = push()
    check("T9 lost manifest adopts existing files", "uploaded=0" in done and "adopted=" in done and len(remote_tree(r)) == n_before, done)

    if QUICK:
        print("(QUICK: skipping T10 interrupted upload and T11 growing file)")
        return

    # ---- T10 interrupted upload: kill the CLI mid-transfer -------------------
    write("big/large take.flac", os.urandom(300 << 20))
    pr = push(background=True)
    time.sleep(25)
    for k in subprocess.run(["pgrep", "-P", str(pr.pid)], capture_output=True, text=True).stdout.split():
        os.kill(int(k), signal.SIGKILL)
    pr.communicate()
    rc, out, done = push()
    tree = remote_tree(r)
    names = [k for k in tree if k.startswith("big/")]
    check("T10 after interruption: exactly one copy, right size",
          names == ["big/large take.flac"] and tree["big/large take.flac"][1] == 300 << 20, f"{names} | {done}")

    # ---- T11 file growing during upload (the CLI hangs; the timeout ends it) --
    grow = write("big/growing.flac", os.urandom(150 << 20))
    pr = push(background=True)
    time.sleep(12)
    with open(grow, "ab") as f:
        f.write(os.urandom(1 << 20))
    os.utime(grow, (OLD + 999, OLD + 999))
    out2 = pr.communicate()[0]
    check("T11 file changed mid-upload: left for the next run",
          "big/growing.flac" not in remote_tree(r) and "changed while uploading" in out2)
    rc, out, done = push()
    check("T11 next run uploads the settled file once",
          remote_tree(r).get("big/growing.flac", (0, 0))[1] == (150 << 20) + (1 << 20), done)


if __name__ == "__main__":
    sys.exit(main())
