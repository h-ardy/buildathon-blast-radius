"""Targeted mutants on lines that expensive tests execute (and cheap tests too).
Question: does an expensive selected test ever fail while every cheap selected test passes?

usage: mutate_exp.py <work-sqlglot> <repo> <n> <seed> <out.jsonl>
"""
import ast
import gzip
import json
import os
import random
import re
import subprocess
import sys
import tempfile

WORK, REPO, N, SEED, OUT = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4]), sys.argv[5]
rng = random.Random(SEED)
PY = sys.executable
TESTS = json.load(open(os.path.join(REPO, "..", "kit", "sample", "tests.json")))["tests"]
SECS = {t["number"]: t["seconds"] for t in TESTS}
IDS = {t["number"]: t["id"] for t in TESTS}
NUM = {t["id"]: t["number"] for t in TESTS}
EXP = {n for n, s in SECS.items() if s >= 2}
M = json.load(gzip.open(os.path.join(REPO, "map", "map.json.gz"), "rt"))
SETS = M["sets"]
EXP_MAP = {i for i, t in enumerate(M["tests"]) if t in NUM and NUM[t] in EXP}


def git(*a):
    return subprocess.run(["git", "-C", WORK, *a], capture_output=True, text=True).stdout


OPS = [
    (r" == ", " != "), (r" != ", " == "), (r" < ", " <= "), (r" <= ", " < "), (r" > ", " >= "),
    (r" >= ", " > "), (r" and ", " or "), (r" or ", " and "), (r"\bTrue\b", "False"),
    (r"\bFalse\b", "True"), (r" is not ", " is "), (r" is ", " is not "), (r"\bnot ", ""),
    (r"\+ 1\b", "+ 2"), (r"- 1\b", "- 2"), (r"\b0\b", "1"), (r"\b1\b", "2"), (r'"([A-Z_]{2,})"', r'"\1X"'),
]

# candidate lines: covered by an expensive test and by at least one cheap test
cands = []
for f, lines in M["cov"].items():
    if not f.startswith("sqlglot/"):
        continue
    for ln, s in lines.items():
        ts = set(SETS[s])
        if ts & EXP_MAP and ts - EXP_MAP:
            cands.append((f, int(ln)))
rng.shuffle(cands)

PLUGIN = r'''
import json, os
_failed = set()
def pytest_runtest_logreport(report):
    if report.failed:
        _failed.add(report.nodeid)
def pytest_sessionfinish(session):
    json.dump(sorted(_failed), open(os.environ["FAIL_OUT"], "w"))
'''


def run_tests(ids):
    d = tempfile.mkdtemp()
    open(os.path.join(d, "failplug.py"), "w").write(PLUGIN)
    out = os.path.join(d, "failed.json")
    env = dict(os.environ, PYTHONPATH=f"{WORK}:{d}", SKIP_INTEGRATION="0", FAIL_OUT=out)
    subprocess.run([PY, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "failplug", "-n", "2",
                    "--timeout=300", *ids], cwd=WORK, env=env, capture_output=True, text=True, timeout=1800)
    try:
        return json.load(open(out))
    except Exception:
        return ["RUN_ERROR"]


def select(patch):
    d = tempfile.mkdtemp()
    os.makedirs(os.path.join(d, "commits"))
    open(os.path.join(d, "commits", "m.patch"), "w").write(patch)
    os.symlink(os.path.join(REPO, "..", "kit", "sample", "tests.json"), os.path.join(d, "tests.json"))
    r = subprocess.run([PY, os.path.join(REPO, "select.py")], env=dict(os.environ, BR_DATA=d),
                       capture_output=True, text=True)
    line = json.loads(r.stdout.strip().splitlines()[-1])
    return {n for w in line["workers"] for n in w}


done = 0
for f, ln in cands:
    if done >= N:
        break
    git("checkout", "--", ".")
    path = os.path.join(WORK, f)
    L = open(path).read().split("\n")
    s = L[ln - 1]
    ops = OPS[:]
    rng.shuffle(ops)
    new = None
    for a, b in ops:
        if re.search(a, s):
            new = re.sub(a, b, s, count=1)
            break
    if not new or new == s:
        continue
    L[ln - 1] = new
    try:
        ast.parse("\n".join(L))
    except SyntaxError:
        continue
    open(path, "w").write("\n".join(L))
    patch = git("diff")
    sel = select(patch)
    exp_sel = sel & EXP
    if not exp_sel:
        continue
    failed = run_tests([IDS[n] for n in sorted(sel)])
    fnums = {NUM[x] for x in failed if x in NUM}
    cheap_fail = bool(fnums - EXP)
    exp_fail = sorted(fnums & EXP)
    rec = {"desc": f"{f}:{ln}", "selected": len(sel), "exp": sorted(exp_sel), "exp_fail": exp_fail,
           "cheap_fail": cheap_fail, "failing": sorted(fnums), "other": [x for x in failed if x not in NUM],
           "patch": patch}
    with open(OUT, "a") as fh:
        fh.write(json.dumps(rec) + "\n")
    flag = "DANGER" if exp_fail and not cheap_fail else ""
    print(f"{f}:{ln} sel={len(sel)} exp={sorted(exp_sel)} exp_fail={exp_fail} cheap_fail={cheap_fail} {flag}", flush=True)
    done += 1
git("checkout", "--", ".")
