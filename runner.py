"""Run a list of sqlglot unittest tests against patched sources, in this process.

Input (json file, argv[1]): {"patched": {module: [path, source]}, "tests": [ids], "tests_root": dir,
"failfast": bool}. Prints {"failed": [ids], "ran": n} on stdout.

Pools that sqlglot's tests normally spawn are run in-process with the same pickle round trip.
"""

import concurrent.futures as cf
import importlib
import importlib.abc
import importlib.util
import json
import logging
import os
import pickle
import sys
import unittest

spec = json.load(open(sys.argv[1]))
PATCHED = spec["patched"]


class Finder(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    def find_spec(self, name, path, target=None):
        if name not in PATCHED:
            return None
        fn, src = PATCHED[name]
        pkg = fn.endswith("__init__.py")
        s = importlib.util.spec_from_loader(name, self, origin=fn, is_package=pkg)
        if pkg:
            s.submodule_search_locations = [os.path.dirname(fn)]
        s.has_location = True
        return s

    def create_module(self, s):
        return None

    def exec_module(self, module):
        fn, src = PATCHED[module.__name__]
        module.__file__ = fn
        exec(compile(src, fn, "exec"), module.__dict__)


sys.meta_path.insert(0, Finder())

# The scorer allows 256 processes, threads included, and the host may have many cores. numpy's
# BLAS and DuckDB each start a thread per core, so keep them to one thread.
try:
    import duckdb

    _connect = duckdb.connect

    def _one_thread_connect(database=":memory:", *args, **kwargs):
        config = dict(kwargs.pop("config", None) or {})
        config.setdefault("threads", 1)
        return _connect(database, *args, config=config, **kwargs)

    duckdb.connect = _one_thread_connect
except Exception:  # noqa
    pass
sys.path.insert(0, spec["tests_root"])
os.environ["SKIP_INTEGRATION"] = "0"


class SyncPool:
    def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=(), **kw):
        if initializer is not None:
            initializer(*pickle.loads(pickle.dumps(initargs)))

    def submit(self, fn, *args, **kwargs):
        f = cf.Future()
        try:
            a, k = pickle.loads(pickle.dumps((args, kwargs)))
            f.set_result(pickle.loads(pickle.dumps(fn(*a, **k))))
        except BaseException as e:  # noqa
            f.set_exception(e)
        return f

    def shutdown(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


failed = set()


class Result(unittest.TestResult):
    def _id(self, test):
        mod = type(test).__module__
        return f"{mod.replace('.', '/')}.py::{type(test).__name__}::{test._testMethodName}"

    def addError(self, test, err):
        if isinstance(test, unittest.TestCase):
            failed.add(self._id(test))
        else:  # setUpClass / module fixtures: every test of that class
            name = str(test)
            for t in CLASS_TESTS.get(name, ()):
                failed.add(t)
            failed.add("ERROR:" + name)
        if spec.get("failfast"):
            self.stop()

    def addFailure(self, test, err):
        failed.add(self._id(test))
        if spec.get("failfast"):
            self.stop()

    def addSubTest(self, test, subtest, err):
        if err is not None:
            failed.add(self._id(test))
            if spec.get("failfast"):
                self.stop()

    def addUnexpectedSuccess(self, test):
        failed.add(self._id(test))



suite = unittest.TestSuite()
CLASS_TESTS = {}
by_class = {}
for tid in spec["tests"]:
    file, cls, meth = tid.split("::")
    by_class.setdefault((file, cls), []).append(meth)
for (file, cls), meths in by_class.items():
    modname = file[:-3].replace("/", ".")
    try:
        mod = importlib.import_module(modname)
        if hasattr(mod, "ProcessPoolExecutor"):
            mod.ProcessPoolExecutor = SyncPool
        klass = getattr(mod, cls)
    except BaseException:  # noqa: the file cannot be imported: its tests fail
        for m in meths:
            failed.add(f"{file}::{cls}::{m}")
        continue
    CLASS_TESTS[f"setUpClass ({modname}.{cls})"] = [f"{file}::{cls}::{m}" for m in meths]
    for m in meths:
        suite.addTest(klass(m))

if not (spec.get("failfast") and failed):
    suite.run(Result())
print(json.dumps({"failed": sorted(failed)}))
