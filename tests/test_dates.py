#!/usr/bin/env python3
"""Offline tests for the mirror's date handling (nas/pd_sync.py). No Proton login.

    python3 tests/test_dates.py
"""
import os, sys, tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "nas"))
from pd_sync import apply_date, claimed_ns  # noqa: E402

results = []


def check(name, cond, detail=""):
    results.append(bool(cond))
    print(f"{'PASS' if cond else 'FAIL'}  {name}  {detail}", flush=True)


# 2014-11-09T23:38:50Z, the stored date of projects_/talks/AutoActivation.m
T = 1415576330 * 1_000_000_000

check("claimed_ns: Z suffix with milliseconds", claimed_ns("2014-11-09T23:38:50.000Z") == T)
check("claimed_ns: fractional seconds kept",
      claimed_ns("2022-11-07T21:44:38.986Z") == 1667857478_986_000_000)
check("claimed_ns: explicit offset", claimed_ns("2014-11-09T18:38:50-05:00") == T)
check("claimed_ns: no zone means UTC", claimed_ns("2014-11-09T23:38:50") == T)
check("claimed_ns: before 1970", claimed_ns("1969-12-31T23:59:59Z") == -1_000_000_000)
for bad in (None, "", "yesterday", 1415576330, "2014-13-45T99:00:00Z"):
    check(f"claimed_ns: {bad!r} -> None", claimed_ns(bad) is None)

d = tempfile.mkdtemp(prefix="pd_dates_")
f = os.path.join(d, "a.txt")
with open(f, "w") as fh:
    fh.write("hello")
meta = {"mtime": "2014-11-09T23:38:50.000Z"}

st = os.stat(f)
cache = {f: [st.st_size, st.st_mtime_ns, "abc"]}
check("apply_date sets the stored date", apply_date(f, meta, cache) == "set" and os.stat(f).st_mtime_ns == T)
check("apply_date moves the matching cache entry to the new mtime", cache[f] == [5, T, "abc"], cache[f])
check("apply_date is a no-op once the date is right", apply_date(f, meta, cache) == "same")

os.utime(f, ns=(T, T + 500_000))                       # off by 0.5 ms
check("apply_date tolerates sub-millisecond differences", apply_date(f, meta) == "same")

os.utime(f, ns=(T, T + 10**9))
stale = {f: [5, 123, "old"]}
check("apply_date leaves a stale cache entry alone",
      apply_date(f, meta, stale) == "set" and stale[f] == [5, 123, "old"], stale[f])

now = os.stat(f).st_mtime_ns
os.utime(f, ns=(now, now + 7 * 10**9))
before = os.stat(f).st_mtime_ns
check("apply_date dry run reports but does not touch",
      apply_date(f, meta, dry_run=True) == "set" and os.stat(f).st_mtime_ns == before)

check("apply_date ignores files without a stored date",
      apply_date(f, {"mtime": None}) == "same" and os.stat(f).st_mtime_ns == before)
check("apply_date reports a missing file instead of raising",
      apply_date(os.path.join(d, "gone"), meta).startswith("stat failed"))

os.remove(f)
os.rmdir(d)
print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
