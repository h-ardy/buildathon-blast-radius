"""Build compact maps (coverage, fixtures, dialect users, import graph) from runs at the pinned commit."""
import ast, coverage, json, gzip, sys, os, collections, importlib, pkgutil, inspect

COV, TRACK, NODEIDS, OUT, SRC, ATTRS = sys.argv[1:7]
ROOT = SRC.rstrip("/") + "/"
sys.path.insert(0, SRC)

ids = [l.strip() for l in open(NODEIDS) if l.strip()]
idx = {t: i for i, t in enumerate(ids)}
by_class = collections.defaultdict(list)
by_file = collections.defaultdict(list)
for i, t in enumerate(ids):
    by_class[t.rsplit("::", 1)[0]].append(i)
    by_file[t.split("::")[0]].append(i)

setids, sets = {}, []
def sid(s):
    k = tuple(sorted(s))
    if k not in setids:
        setids[k] = len(sets); sets.append(list(k))
    return setids[k]

# ---- line coverage
d = coverage.CoverageData(COV); d.read()
cov, imp = {}, {}
for f in sorted(d.measured_files()):
    rel = f.replace(ROOT, "")
    lines, implines = {}, []
    for ln, cs in d.contexts_by_lineno(f).items():
        ts = set()
        for c in cs:
            if c == "":
                implines.append(ln); continue
            nid = c.split("|")[0]
            if nid in idx: ts.add(idx[nid])
        if ts: lines[str(ln)] = sid(ts)
    cov[rel] = lines
    imp[rel] = sorted(implines)

# ---- tracking: opened files and dialect instantiation
t = json.load(open(TRACK))
opens = collections.defaultdict(set)
for key, paths in t["opened"].items():
    nid, _, phase = key.partition("|")
    if nid not in idx: continue
    who = by_class[nid.rsplit("::", 1)[0]] if phase == "setup" else [idx[nid]]
    for p in paths:
        if p.startswith(ROOT) and "__pycache__" not in p:
            opens[p.replace(ROOT, "")].update(who)
dial_users = collections.defaultdict(set)
for nid, ds in t["dialects"].items():
    if nid in idx:
        for x in ds: dial_users[x].add(idx[nid])

# ---- static fixture mentions in test sources
fixture_files = []
for dp, dn, fn in os.walk(os.path.join(ROOT, "tests/fixtures")):
    for f in fn:
        fixture_files.append(os.path.relpath(os.path.join(dp, f), os.path.join(ROOT, "tests/fixtures")))
def test_scopes(path):
    """yield (string constant, tests it belongs to)"""
    rel = os.path.relpath(path, ROOT)
    tree = ast.parse(open(path).read())
    out = []
    def visit(node, cls, fn):
        for ch in ast.iter_child_nodes(node):
            if isinstance(ch, ast.ClassDef):
                visit(ch, ch.name, None)
            elif isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef)) and fn is None:
                visit(ch, cls, ch.name)
            else:
                if isinstance(ch, ast.Constant) and isinstance(ch.value, str):
                    out.append((ch.value, rel, cls, fn))
                visit(ch, cls, fn)
    visit(tree, None, None)
    return out
consts = []
for dp, dn, fn in os.walk(os.path.join(ROOT, "tests")):
    if "fixtures" in dp: continue
    for f in fn:
        if f.endswith(".py"): consts += test_scopes(os.path.join(dp, f))
def owners(rel, cls, fn):
    if cls and fn and fn.startswith("test"):
        k = f"{rel}::{cls}::{fn}"
        return [idx[k]] if k in idx else []
    if cls:
        return by_class.get(f"{rel}::{cls}", [])
    return by_file.get(rel, [])
fixtures = {}
for r in fixture_files:
    noext = r.rsplit(".", 1)[0] if "." in os.path.basename(r) else r
    base = os.path.basename(r)
    stem = base.split(".")[0]
    s = set(opens.get("tests/fixtures/" + r, ()))
    for val, rel, cls, fn in consts:
        v = val.strip()
        if not v: continue
        if v in (r, noext, base) or (len(v) > 3 and (r.endswith("/" + v) or noext.endswith("/" + v))) or (v == stem and len(stem) > 3):
            s.update(owners(rel, cls, fn))
    fixtures["tests/fixtures/" + r] = sorted(s)

# ---- dialect classes, their component classes, import graph
import sqlglot
from sqlglot.dialects.dialect import Dialect
mods = {"sqlglot": sqlglot}
for info in pkgutil.walk_packages(sqlglot.__path__, prefix="sqlglot."):
    if info.name.endswith("__main__"): continue
    try: mods[info.name] = importlib.import_module(info.name)
    except Exception as e: print("import fail", info.name, e)
def key(c): return f"{c.__module__}.{c.__qualname__}"
dialects = {}
for m in list(mods.values()):
    for name, obj in vars(m).items():
        if isinstance(obj, type) and issubclass(obj, Dialect) and obj.__module__ == m.__name__:
            comps = set()
            for c in obj.__mro__:
                if c.__module__.startswith("sqlglot"): comps.add(key(c))
            for attr in ("tokenizer_class", "parser_class", "generator_class", "jsonpath_tokenizer_class"):
                k = getattr(obj, attr, None)
                if isinstance(k, type):
                    for c in k.__mro__:
                        if c.__module__.startswith("sqlglot"): comps.add(key(c))
            dialects[key(obj)] = {"comps": sorted(comps), "users": sorted(set().union(*[dial_users.get(key(s), set()) for s in [obj]]))}
# import graph: module -> modules importing it (direct)
importers = collections.defaultdict(set)
for name, m in mods.items():
    f = getattr(m, "__file__", None)
    if not f or not f.endswith(".py"): continue
    tree = ast.parse(open(f).read())
    pkg = name if f.endswith("__init__.py") else name.rsplit(".", 1)[0]
    for node in ast.walk(tree):
        targets = []
        if isinstance(node, ast.Import):
            targets = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                parts = pkg.split(".")
                parts = parts[: len(parts) - node.level + 1]
                base = ".".join(parts + ([node.module] if node.module else []))
            targets = [base] + [f"{base}.{a.name}" for a in node.names]
        for tname in targets:
            if tname in mods and tname != name:
                importers[tname].add(name)
def modpath(n):
    f = getattr(mods[n], "__file__", "") or ""
    return f.replace(ROOT, "")
# ---- class attribute readers
A = json.load(open(ATTRS))
areaders = collections.defaultdict(lambda: collections.defaultdict(set))
akeys = collections.defaultdict(lambda: collections.defaultdict(lambda: collections.defaultdict(set)))
astar = collections.defaultdict(lambda: collections.defaultdict(set))
aany = collections.defaultdict(lambda: collections.defaultdict(set))
alen = collections.defaultdict(lambda: collections.defaultdict(set))
for nid, evs in A["events"].items():
    if nid not in idx: continue
    i = idx[nid]
    for e in evs:
        ck, attr = e.split("\t", 1)
        if "\x01" in attr:
            attr, key = attr.split("\x01", 1)
            aany[ck][attr].add(i)
            if key in ("*", "?"):
                astar[ck][attr].add(i)
            elif key == "#":
                alen[ck][attr].add(i)
            else:
                akeys[ck][attr][key].add(i)
        else:
            areaders[ck][attr].add(i)
attrs = {ck: {a: sid(ts) for a, ts in d.items()} for ck, d in areaders.items()}
from sqlglot import exp as _exp
out = {
    "imptrig": json.load(open(os.environ["IMPTRIG"])) if os.environ.get("IMPTRIG") else {"triggers": [], "lines": {}},
    "exprclasses": {k: v.__name__ for k, v in _exp.EXPR_CLASSES.items()},
    "akeys": {ck: {a: {k: sid(ts) for k, ts in kd.items()} for a, kd in d.items()} for ck, d in akeys.items()},
    "astar": {ck: {a: sid(ts) for a, ts in d.items()} for ck, d in astar.items()},
    "alen": {ck: {a: sid(ts) for a, ts in d.items()} for ck, d in alen.items()},
    "aany": {ck: {a: sid(ts) for a, ts in d.items()} for ck, d in aany.items()},
    "attrs": attrs,
    "classes": A["classes"],
    "tests": ids,
    "sets": sets,
    "cov": cov,
    "imp": imp,
    "fixtures": fixtures,
    "dialects": dialects,
    "importers": {modpath(k): sorted(modpath(x) for x in v) for k, v in importers.items()},
    "modnames": {modpath(k): k for k in mods},
}
with gzip.open(OUT, "wt") as fh:
    json.dump(out, fh, separators=(",", ":"))
print("sets", len(sets), "files", len(cov), "fixtures", len(fixtures), "dialects", len(dialects), os.path.getsize(OUT))
