"""Mutation harness: make a patch, run the selector, run the real suite, compare.

usage: python mutate.py <workdir-sqlglot> <repo> <n> <seed> <out.jsonl> [kinds]
"""
import ast
import json
import os
import random
import re
import subprocess
import sys
import tempfile

WORK, REPO, N, SEED, OUT = (sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4]), sys.argv[5]) if len(sys.argv) > 5 else ("", "", 0, 0, "")
KINDS = sys.argv[6].split(",") if len(sys.argv) > 6 else ["classattr", "scalar", "modconst", "func", "comment", "fixture"]
rng = random.Random(SEED)
PY = sys.executable


def git(*a):
    return subprocess.run(["git", "-C", WORK, *a], capture_output=True, text=True).stdout


def py_files():
    return [f for f in git("ls-files", "sqlglot").split() if f.endswith(".py") and "_version" not in f]


def lines_of(f):
    return open(os.path.join(WORK, f)).read().split("\n")


def write(f, lines):
    open(os.path.join(WORK, f), "w").write("\n".join(lines))


def mutate_text(s):
    """one small token-level change of a source line; None if nothing applies"""
    ops = [
        (r" == ", " != "), (r" != ", " == "), (r" < ", " <= "), (r" <= ", " < "), (r" > ", " >= "),
        (r" >= ", " > "), (r" and ", " or "), (r" or ", " and "), (r"\bTrue\b", "False"),
        (r"\bFalse\b", "True"), (r" is not ", " is "), (r" is ", " is not "), (r"\bnot ", ""),
        (r"\+ 1\b", "+ 2"), (r"- 1\b", "- 2"), (r"\b0\b", "1"), (r"\b1\b", "2"), (r"\b2\b", "3"),
        (r'"([A-Z_]{2,})"', r'"\1X"'), (r"\bNone\b", "0"),
    ]
    rng.shuffle(ops)
    for a, b in ops:
        if re.search(a, s):
            return re.sub(a, b, s, count=1)
    return None


def pick_class_level(f, want):
    tree = ast.parse(open(os.path.join(WORK, f)).read())
    cands = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for st in node.body:
                if isinstance(st, (ast.Assign, ast.AnnAssign)) and getattr(st, "value", None) is not None:
                    v = st.value
                    if want == "scalar" and isinstance(v, (ast.Constant, ast.UnaryOp, ast.Tuple)) and st.lineno == st.end_lineno:
                        cands.append(st.lineno)
                    if want == "classattr" and st.end_lineno > st.lineno:
                        cands.extend(range(st.lineno + 1, st.end_lineno))
    return cands


def pick_module_level(f):
    tree = ast.parse(open(os.path.join(WORK, f)).read())
    out = []
    for st in tree.body:
        if isinstance(st, (ast.Assign, ast.AnnAssign)) and getattr(st, "value", None) is not None:
            out.extend(range(st.lineno, st.end_lineno + 1))
    return out


def pick_func_lines(f):
    tree = ast.parse(open(os.path.join(WORK, f)).read())
    out = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for st in ast.walk(node):
                if isinstance(st, ast.stmt) and st is not node and st.lineno == st.end_lineno:
                    out.append(st.lineno)
    return out


IMPLINES = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "implines.json")))


def pick_impexec(f):
    lines = set(IMPLINES.get(f, []))
    return [ln for ln in pick_func_lines(f) if ln in lines]


def make_mutant(kind):
    for _ in range(200):
        if kind == "testcode":
            tf = [f for f in git("ls-files", "tests").split() if f.endswith(".py") and "/fixtures/" not in f and "leakcheck" not in f and "gen_fixtures" not in f]
            f = rng.choice(tf)
            L = lines_of(f)
            cands = pick_func_lines(f)
            rng.shuffle(cands)
            for ln in cands[:30]:
                new = mutate_text(L[ln - 1])
                if new and new != L[ln - 1]:
                    L[ln - 1] = new
                    try:
                        ast.parse("\n".join(L))
                    except SyntaxError:
                        L = lines_of(f)
                        continue
                    write(f, L)
                    return f"{kind} {f}:{ln}"
            continue
        if kind == "fixture":
            fx = [f for f in git("ls-files", "tests/fixtures").split() if f.endswith(".sql")]
            f = rng.choice(fx)
            L = lines_of(f)
            idx = [i for i, l in enumerate(L) if l.strip() and not l.startswith("#") and not l.startswith("--")]
            if not idx:
                continue
            i = rng.choice(idx)
            words = L[i].split(" ")
            j = rng.randrange(len(words))
            words.insert(j, rng.choice(["42", "x", "0", "NULL"]))
            L[i] = " ".join(words)
            write(f, L)
            return f"{kind} {f}:{i+1}"
        f = rng.choice(py_files())
        L = lines_of(f)
        if kind == "comment":
            i = rng.randrange(len(L))
            indent = re.match(r"\s*", L[i]).group(0)
            L.insert(i, indent + rng.choice(["# Cheap check first", "# Kept for backwards compatibility", "# noqa"]))
            write(f, L)
            return f"{kind} {f}:{i+1}"
        if kind in ("classattr", "scalar"):
            cands = pick_class_level(f, kind)
        elif kind == "modconst":
            cands = pick_module_level(f)
        elif kind == "impexec":
            cands = pick_impexec(f)
        else:
            cands = pick_func_lines(f)
        rng.shuffle(cands)
        for ln in cands[:30]:
            new = mutate_text(L[ln - 1])
            if new and new != L[ln - 1]:
                L[ln - 1] = new
                try:
                    ast.parse("\n".join(L))
                except SyntaxError:
                    L = lines_of(f)
                    continue
                write(f, L)
                return f"{kind} {f}:{ln}"
    return None


PLUGIN = r'''
import json, os
_failed = set()
def pytest_runtest_logreport(report):
    if report.failed:
        _failed.add(report.nodeid)
def pytest_collectreport(report):
    if report.failed:
        _failed.add("COLLECT:" + report.nodeid)
def pytest_sessionfinish(session):
    json.dump(sorted(_failed), open(os.environ["FAIL_OUT"], "w"))
'''


def run_suite():
    d = tempfile.mkdtemp()
    open(os.path.join(d, "failplug.py"), "w").write(PLUGIN)
    out = os.path.join(d, "failed.json")
    env = dict(os.environ, PYTHONPATH=f"{WORK}:{d}", SKIP_INTEGRATION="0", FAIL_OUT=out)
    subprocess.run([PY, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "failplug", "-n", "2",
                    "--timeout=300", "-x" if False else "-q", "tests"], cwd=WORK, env=env,
                   capture_output=True, text=True, timeout=1500)
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
    return {n for w in line["workers"] for n in w}, r.stderr.strip()[-300:]


if __name__ == "__main__":
    tests = json.load(open(os.path.join(REPO, "..", "kit", "sample", "tests.json")))["tests"]
    num = {t["id"]: t["number"] for t in tests}
    for k in range(N):
        git("checkout", "--", ".")
        kind = KINDS[k % len(KINDS)]
        desc = make_mutant(kind)
        if not desc:
            continue
        patch = git("diff")
        sel, why = select(patch)
        failed = run_suite()
        fnums = {num[x] for x in failed if x in num}
        other = [x for x in failed if x not in num]
        missed = sorted(fnums - sel)
        rec = {"desc": desc, "selected": len(sel), "failing": sorted(fnums), "other": other,
               "missed": missed, "why": why, "patch": patch}
        with open(OUT, "a") as fh:
            fh.write(json.dumps(rec) + "\n")
        print(f"{desc}: sel={len(sel)} fail={len(fnums)} missed={missed} {other[:2]}", flush=True)
    git("checkout", "--", ".")
