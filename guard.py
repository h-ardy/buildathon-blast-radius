"""Launcher for blast.py that makes the batch impossible to reject.

Every line blast.py prints passes through here first. A line is let through only if it is valid JSON for
a commit in /data/commits, not answered before, with at most 8 workers and only known, distinct test
numbers; anything else is replaced by the safe answer for that commit (every test, packed longest first).
If blast.py fails in any way, even while loading, every commit not answered yet gets the safe answer.
The safe answer can cost time, never a miss and never a rejected run.
"""
import json
import os
import sys
import threading
import traceback
from pathlib import Path

DATA = Path(os.environ.get("BR_DATA", "/data"))
REAL_OUT = sys.stdout
ANSWERED = set()
LOCK = threading.RLock()
# the scorer stops a batch after 3 minutes (4.5 in the final); past this, every open commit gets the
# safe answer and the process ends, so a stuck selector can never time the whole batch out
WATCHDOG = float(os.environ.get("BR_WATCHDOG", "160"))


def log(msg):
    try:
        sys.stderr.write(f"[guard] {msg}\n")
        sys.stderr.flush()
    except Exception:  # noqa
        pass


def load_tests():
    tests = json.loads((DATA / "tests.json").read_text())["tests"]
    return {int(t["number"]): float(t.get("seconds") or 0.0) for t in tests}


def commits():
    return sorted(p.stem for p in (DATA / "commits").glob("*.patch"))


def safe_workers(secs):
    """every test, longest first onto the least loaded of 8 workers"""
    loads = [[0.0, []] for _ in range(8)]
    for n in sorted(secs, key=lambda k: (-secs[k], k)):
        w = min(loads, key=lambda x: x[0])
        w[0] += secs[n]
        w[1].append(n)
    return [sorted(w[1]) for w in loads if w[1]]


def emit(line):
    with LOCK:
        REAL_OUT.write(line + "\n")
        REAL_OUT.flush()


def safe_answer(commit, secs, why):
    with LOCK:
        if commit in ANSWERED:
            return
        ANSWERED.add(commit)
        log(f"{commit}: safe answer (every test): {why}")
        emit(json.dumps({"commit": commit, "workers": safe_workers(secs)}))


def check(text, valid, secs, known):
    """-> the line to print (the original if valid), or None to drop it"""
    try:
        o = json.loads(text)
        commit = o["commit"]
    except Exception:  # noqa
        log(f"dropped unreadable line {text[:120]!r}")
        return None
    if not isinstance(commit, str) or commit not in known:
        log(f"dropped line for unknown commit {str(commit)[:60]!r}")
        return None
    if commit in ANSWERED:
        log(f"dropped second line for {commit}")
        return None
    try:
        ws = o["workers"]
        ok = isinstance(ws, list) and all(isinstance(w, list) for w in ws)
        if ok:
            ws = [w for w in ws if w]  # an empty worker runs nothing: leave it out
            ok = len(ws) <= 8
        seen = set()
        if ok:
            for w in ws:
                for n in w:
                    if type(n) is not int or n not in valid or n in seen:
                        ok = False
                        break
                    seen.add(n)
                if not ok:
                    break
    except Exception:  # noqa
        ok = False
    if not ok:
        safe_answer(commit, secs, "invalid line from the selector")
        return None
    ANSWERED.add(commit)
    return json.dumps({"commit": commit, "workers": ws})


class Filter:
    """stands in for stdout: complete lines are checked, then passed on"""

    def __init__(self, valid, secs, known):
        self.buf = ""
        self.valid, self.secs, self.known = valid, secs, known
        self.pid = os.getpid()

    def write(self, s):
        if os.getpid() != self.pid:  # a forked child: never speaks for the batch
            return len(s)
        with LOCK:
            self.buf += s
            while "\n" in self.buf:
                line, self.buf = self.buf.split("\n", 1)
                if line.strip():
                    out = check(line.strip(), self.valid, self.secs, self.known)
                    if out is not None:
                        emit(out)
        return len(s)

    def flush(self):
        pass

    def isatty(self):
        return False

    def fileno(self):
        return REAL_OUT.fileno()


def main():
    try:
        secs = load_tests()
        known = set(commits())
    except Exception:  # noqa  nothing sensible can be printed without the inputs
        log("cannot read the inputs:\n" + traceback.format_exc())
        raise
    flt = Filter(set(secs), secs, known)
    sys.stdout = flt

    def watchdog():
        if os.getpid() != flt.pid:
            return
        with LOCK:
            log(f"watchdog: {WATCHDOG:.0f} s reached")
            for c in sorted(known - ANSWERED):
                safe_answer(c, secs, "out of time")
            try:
                sys.stderr.flush()
            finally:
                os._exit(0)

    timer = threading.Timer(WATCHDOG, watchdog)
    timer.daemon = True
    timer.start()
    try:
        here = os.path.dirname(os.path.abspath(__file__))
        if here not in sys.path:
            sys.path.insert(0, here)
        import importlib

        sel = importlib.import_module(os.environ.get("BR_SELECTOR", "blast"))
        sel.main()
    except BaseException:  # noqa  includes SystemExit and KeyboardInterrupt
        log("selector failed:\n" + traceback.format_exc()[-3000:])
    finally:
        if os.getpid() != flt.pid:
            os._exit(1)
        sys.stdout = REAL_OUT
        if flt.buf.strip():
            out = check(flt.buf.strip(), flt.valid, secs, known)
            if out is not None:
                emit(out)
        with LOCK:
            for c in sorted(known - ANSWERED):
                safe_answer(c, secs, "no answer from the selector")
            timer.cancel()


if __name__ == "__main__":
    main()
