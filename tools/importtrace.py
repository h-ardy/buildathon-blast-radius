"""Record, for every sqlglot line executed while importing sqlglot, which module-level or
class-body statement (file, line) was running when it executed."""
import importlib, inspect, json, os, pkgutil, sys
from collections import defaultdict

ROOT = sys.argv[1].rstrip("/") + "/"
OUT = sys.argv[2]
sys.path.insert(0, ROOT)
trig = defaultdict(set)
CO_OPT = inspect.CO_OPTIMIZED


def body_frame(f):
    while f is not None:
        fn = f.f_code.co_filename
        if fn.startswith(ROOT + "sqlglot/") and not (f.f_code.co_flags & CO_OPT):
            return f
        f = f.f_back
    return None


def tracer(frame, event, arg):
    fn = frame.f_code.co_filename
    if not fn.startswith(ROOT + "sqlglot/"):
        return tracer
    if event == "line" and (frame.f_code.co_flags & CO_OPT):
        b = body_frame(frame.f_back)
        if b is not None:
            trig[(fn[len(ROOT):], frame.f_lineno)].add((b.f_code.co_filename[len(ROOT):], b.f_lineno))
    return tracer


sys.settrace(tracer)
import sqlglot  # noqa
for info in pkgutil.walk_packages(sqlglot.__path__, prefix="sqlglot."):
    if info.name.endswith("__main__"):
        continue
    try:
        importlib.import_module(info.name)
    except Exception:
        pass
sys.settrace(None)
tid = {}
tl = []
out = defaultdict(dict)
for (f, ln), ts in trig.items():
    ids = []
    for t in ts:
        if t not in tid:
            tid[t] = len(tl)
            tl.append(list(t))
        ids.append(tid[t])
    out[f][str(ln)] = sorted(ids)
json.dump({"triggers": tl, "lines": out}, open(OUT, "w"))
print(len(trig), len(tl))
