"""Drive the proton-drive CLI's interactive mode from Python.

Started with no arguments, the CLI reads one command per line from stdin and
prints a `proton-drive> ` prompt when each is done, keeping one session and
one warm cache. Starting a fresh CLI per command costs about 1.5 s on the laptop
and several on the NAS; through this, listing all of Proton takes about a minute.

One Repl per cache directory, ever: CLI processes sharing a cache lock each
other out (docs/findings.md). Commands are sequential; there is no exit code,
so callers verify results (listings parse, uploads show up in a listing).
"""
import json
import os
import re
import select
import signal
import subprocess
import threading
import time

PROMPT = b"proton-drive> "

# What the CLI prints when the SDK gives up on HTTP 429, plus the API's code.
# stderr only: "2011" inside a file name on stdout once aborted a whole walk.
RATE_LIMIT_RE = re.compile(
    r"RateLimitedError|too many (server )?requests|Code\W{0,3}2011\b", re.I)


def _now():
    """A clock that keeps running while the machine is suspended (monotonic
    doesn't), so a command left in flight across a laptop sleep times out
    on wake instead of minutes later."""
    try:
        return time.clock_gettime(time.CLOCK_BOOTTIME)
    except (AttributeError, OSError):
        return time.monotonic()


class CliError(Exception):
    pass


class RateLimited(CliError):
    pass


def quote(arg):
    """Quote one argument for the REPL's line splitter (POSIX-like, double
    quotes with \\" and \\\\ escapes). Newlines can't be passed at all."""
    if "\n" in arg or "\r" in arg:
        raise ValueError(f"newline in argument: {arg!r}")
    return '"' + arg.replace("\\", "\\\\").replace('"', '\\"') + '"'


_GLOB_TRIGGER = re.compile(r"[*?\[{]")
_GLOB_SPECIAL = re.compile(r"([*?\[\]{}\\])")


def local_arg(path):
    """A local path as the CLI must be given it. The CLI glob-expands any local
    path containing * ? [ or { (upload sources and download destinations), so
    a real folder like "Album [FLAC]" matches nothing and the whole command
    fails with "No paths matched". Backslash-escape the specials in that case;
    without a trigger character the path is taken literally, so leave it."""
    if _GLOB_TRIGGER.search(path):
        return _GLOB_SPECIAL.sub(r"\\\1", path)
    return path


def esc(name):
    """Proton path syntax: a literal / in a name is backslash-escaped."""
    return name.replace("\\", "\\\\").replace("/", "\\/")


class Repl:
    def __init__(self, cli, env=None, start_timeout=600):
        self.cli = cli
        self.env = env
        self.start_timeout = start_timeout
        self.p = None
        self.err = []
        self.err_lock = threading.Lock()
        self.restarts = 0
        self._started = False
        self._last = 0

    # -- process ---------------------------------------------------------------
    def _start(self):
        self.p = subprocess.Popen([self.cli], stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  env=self.env)
        threading.Thread(target=self._drain_stderr, args=(self.p,),
                         daemon=True).start()
        self._read_until_prompt(self.start_timeout)

    def _drain_stderr(self, p):
        for line in p.stderr:
            with self.err_lock:
                self.err.append(line.decode("utf-8", "replace"))

    def _kill(self):
        if self.p and self.p.poll() is None:
            try:
                self.p.send_signal(signal.SIGKILL)
                self.p.wait(timeout=30)
            except Exception:
                pass
        self.p = None

    def close(self):
        if self.p and self.p.poll() is None:
            try:
                self.p.stdin.write(b"exit\n")
                self.p.stdin.flush()
                self.p.wait(timeout=60)
            except Exception:
                self._kill()
        self.p = None

    def _stderr_since(self, n, settle=0.3):
        if settle:
            time.sleep(settle)          # stderr is a separate pipe; let it land
        with self.err_lock:
            return "".join(self.err[n:])

    def _read_until_prompt(self, timeout):
        fd = self.p.stdout.fileno()
        buf = bytearray()
        deadline = _now() + timeout
        while not buf.endswith(PROMPT):
            left = deadline - _now()
            if left <= 0:
                self._kill()
                raise CliError(f"timed out after {timeout}s")
            r, _, _ = select.select([fd], [], [], min(left, 30))
            if not r:
                continue
            chunk = os.read(fd, 1 << 20)
            if not chunk:
                with self.err_lock:
                    tail = "".join(self.err[-20:])
                self._kill()
                raise CliError(f"CLI exited: {tail.strip()[-1500:]}")
            buf += chunk
        return bytes(buf[:-len(PROMPT)])

    # -- commands --------------------------------------------------------------
    def cmd(self, *args, timeout=3600):
        """Run one command; return (stdout, stderr). Restarts a dead CLI."""
        if self.p is None or self.p.poll() is not None:
            if self._started:
                self.restarts += 1
            self._started = True
            self._start()
        line = " ".join(quote(a) for a in args).encode() + b"\n"
        with self.err_lock:
            n = len(self.err)
        try:
            self.p.stdin.write(line)
            self.p.stdin.flush()
        except (BrokenPipeError, OSError) as e:
            self._kill()
            raise CliError(f"CLI gone: {e}")
        out = self._read_until_prompt(timeout).decode("utf-8", "surrogateescape")
        # No settling delay here: across thousands of listings even 50 ms each
        # adds minutes. Callers that see a failure call late_stderr().
        self._last = n
        err = self._stderr_since(n, settle=0)
        if RATE_LIMIT_RE.search(err):
            raise RateLimited(err.strip()[-400:])
        return out, err

    def late_stderr(self, settle=0.5):
        """stderr of the last command, after giving the pipe time to deliver
        it (it's separate from stdout, so it can trail the prompt)."""
        err = self._stderr_since(self._last, settle=settle)
        if RATE_LIMIT_RE.search(err):
            raise RateLimited(err.strip()[-400:])
        return err

    def list(self, path, tries=3, timeout=3600):
        """Entries of a folder, or raise. Retries cut-short JSON and transient
        errors (the "You need to login first" blips)."""
        last = ""
        for attempt in range(tries):
            try:
                out, err = self.cmd("filesystem", "list", path, "--json",
                                    timeout=timeout)
            except RateLimited:
                raise
            except CliError as e:
                last = str(e)
                time.sleep(5 * (attempt + 1))
                continue
            s = out.strip()
            if s.startswith("["):
                try:
                    return json.loads(s)
                except json.JSONDecodeError as e:
                    last = f"cut-short JSON ({e})"
                    continue
            err = self.late_stderr()
            last = (err.strip() or s or "no output")[-1500:]
            if "not found" in last.lower():
                break                   # a real answer, not worth retrying
            time.sleep(5 * (attempt + 1))
        raise CliError(f"list {path}: {last}")
