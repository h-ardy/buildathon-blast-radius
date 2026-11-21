"""Compare the in-container runner with pytest ground truth recorded by the mutation harnesses.
usage: fidelity.py <records.jsonl>... ; truth is the record's "failing" list (test numbers).
For r3-style records only the selected tests were run, so only those are compared."""
import importlib.util
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
KIT = os.environ.get("BR_KIT", os.path.join(REPO, "..", "kit"))  # unpacked starter kit
os.environ["BR_DATA"] = os.path.join(KIT, "sample")
sp = importlib.util.spec_from_file_location("sel", os.path.join(REPO, "blast.py"))
sel = importlib.util.module_from_spec(sp)
sp.loader.exec_module(sel)
tests = json.load(open(os.path.join(KIT, "sample", "tests.json")))["tests"]
ids = {t["number"]: t["id"] for t in tests}
num = {t["id"]: t["number"] for t in tests}
map_to_num = {i: num[t] for i, t in enumerate(sel.MAP_TESTS) if t in num}
bad = 0
for path in sys.argv[1:]:
    for line in open(path):
        r = json.loads(line)
        why, ctx = [], {}
        S = sel.select_commit(r["patch"], why, ctx)
        S = sorted({map_to_num[i] for i in S if i in map_to_num})
        if ctx.get("unknown_files") or len(S) > 400 or not S:
            continue
        runn = [n for n in S if sel.runnable(ids[n]) and tests[n]["seconds"] < 2]
        if not runn:
            continue
        got = sel.run_here([ids[n] for n in runn], ctx["patched"], ctx["tests"], False, 600)
        if got is None:
            print(r["desc"], "NO RESULT")
            continue
        got = {num[x] for x in got if x in num}
        truth = set(r["failing"]) & set(runn)
        ok = got == truth
        bad += not ok
        print(("OK  " if ok else "BAD ") + r["desc"][:60], len(runn), sorted(got)[:10], sorted(truth)[:10], flush=True)
print("mismatches:", bad)
