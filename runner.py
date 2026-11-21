"""Run a list of sqlglot unittest tests against patched sources, in this process.

Run as a script, or imported by blast.py, which calls prepare() and run() in a forked copy of itself.

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


class Finder(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    def __init__(self, patched):
        self.patched = patched

    def find_spec(self, name, path, target=None):
        if name not in self.patched:
            return None
        fn, src = self.patched[name]
        pkg = fn.endswith("__init__.py")
        s = importlib.util.spec_from_loader(name, self, origin=fn, is_package=pkg)
        if pkg:
            s.submodule_search_locations = [os.path.dirname(fn)]
        s.has_location = True
        return s

    def create_module(self, s):
        return None

    def exec_module(self, module):
        fn, src = self.patched[module.__name__]
        module.__file__ = fn
        exec(compile(src, fn, "exec"), module.__dict__)


def one_thread_duckdb():
    """The scorer allows 256 processes, threads included, and the host may have many cores.
    DuckDB starts a thread per core, so keep it to one thread."""
    try:
        import duckdb

        if getattr(duckdb.connect, "_one_thread", False):
            return
        _connect = duckdb.connect

        def _one_thread_connect(database=":memory:", *args, **kwargs):
            config = dict(kwargs.pop("config", None) or {})
            config.setdefault("threads", 1)
            return _connect(database, *args, config=config, **kwargs)

        _one_thread_connect._one_thread = True
        duckdb.connect = _one_thread_connect
    except Exception:  # noqa
        pass


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


def prepare(spec):
    sys.meta_path.insert(0, Finder(spec["patched"]))
    one_thread_duckdb()
    sys.path.insert(0, spec["tests_root"])
    os.environ["SKIP_INTEGRATION"] = "0"


def run(spec):
    """-> set of failing test ids"""
    failed = set()
    failfast = spec.get("failfast")
    class_tests = {}

    class Result(unittest.TestResult):
        def _id(self, test):
            mod = type(test).__module__
            return f"{mod.replace('.', '/')}.py::{type(test).__name__}::{test._testMethodName}"

        def addError(self, test, err):
            if isinstance(test, unittest.TestCase):
                failed.add(self._id(test))
            else:  # setUpClass / module fixtures: every test of that class
                name = str(test)
                for t in class_tests.get(name, ()):
                    failed.add(t)
                failed.add("ERROR:" + name)
            if failfast:
                self.stop()

        def addFailure(self, test, err):
            failed.add(self._id(test))
            if failfast:
                self.stop()

        def addSubTest(self, test, subtest, err):
            if err is not None:
                failed.add(self._id(test))
                if failfast:
                    self.stop()

        def addUnexpectedSuccess(self, test):
            failed.add(self._id(test))

    suite = unittest.TestSuite()
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
        class_tests[f"setUpClass ({modname}.{cls})"] = [f"{file}::{cls}::{m}" for m in meths]
        for m in meths:
            suite.addTest(klass(m))

    if not (failfast and failed):
        suite.run(Result())
    return failed


if __name__ == "__main__":
    SPEC = json.load(open(sys.argv[1]))
    prepare(SPEC)
    print(json.dumps({"failed": sorted(run(SPEC))}))
