"""Label patches with the real suite: python label.py <worktree> <patchdir> <out.jsonl>"""
import json, os, subprocess, sys, tempfile, glob
WORK, PD, OUT = sys.argv[1:4]
PY = sys.executable
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
PLUGIN = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "mutate.py")).read().split("PLUGIN = r'''")[1].split("'''")[0]
tests = json.load(open("/data/tests.json"))["tests"]
num = {t["id"]: t["number"] for t in tests}
done = set()
if os.path.exists(OUT):
    done = {json.loads(l)["name"] for l in open(OUT)}
def git(*a):
    return subprocess.run(["git", "-C", WORK, *a], capture_output=True, text=True)
for p in sorted(glob.glob(os.path.join(PD, "*.patch"))):
    name = os.path.basename(p)[:-6]
    if name in done:
        continue
    git("reset", "-q", "--hard", "eb11aec"); git("clean", "-fdq")
    r = git("apply", p)
    if r.returncode:
        print(name, "apply failed", r.stderr[:200], flush=True); continue
    d = tempfile.mkdtemp()
    open(os.path.join(d, "failplug.py"), "w").write(PLUGIN)
    out = os.path.join(d, "failed.json")
    env = dict(os.environ, PYTHONPATH=f"{WORK}:{d}", SKIP_INTEGRATION="0", FAIL_OUT=out)
    subprocess.run([PY, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "failplug", "-n", "2", "--timeout=300", "tests"],
                   cwd=WORK, env=env, capture_output=True, text=True, timeout=1500)
    try:
        failed = json.load(open(out))
    except Exception:
        failed = ["RUN_ERROR"]
    fn = {num[x] for x in failed if x in num}
    for x in failed:
        if x.startswith("COLLECT:"):
            fn |= {t["number"] for t in tests if t["file"] == x[8:]}
    # pinned tests that no longer exist after the patch also fail
    with open(os.path.join(PD, name + ".patch")) as fh:
        txt = fh.read()
    rec = {"name": name, "failing": sorted(fn), "other": [x for x in failed if x not in num][:20]}
    with open(OUT, "a") as fh:
        fh.write(json.dumps(rec) + "\n")
    print(name, len(fn), rec["other"][:3], flush=True)
git("reset", "-q", "--hard", "eb11aec"); git("clean", "-fdq")
