"""pytest plugin: record, per test, what every single comparison in sqlglot evaluated to and whether
a sibling operator would have given a different answer on the same operands (pinned tree only).

Output (OPMAP_OUT): {"sites": [[path, line, col, op], ...],
                     "hit": {sid: [test ids]}, "dis": {"sid:alt": [test ids]},
                     "shared": [sids evaluated while a module loaded or a class/session was set up],
                     "failed": [test ids that failed under recording]}
"""
import ast
import builtins
import importlib.abc
import importlib.machinery
import importlib.util
import json
import operator
import os
import sys

import pytest

ROOT = os.path.abspath(os.environ.get("OPMAP_ROOT", "."))
OPS = {"Eq": operator.eq, "NotEq": operator.ne, "Is": operator.is_, "IsNot": operator.is_not,
       "Lt": operator.lt, "LtE": operator.le, "Gt": operator.gt, "GtE": operator.ge,
       "In": lambda a, b: a in b, "NotIn": lambda a, b: a not in b}
FAMILY = {"Eq": ["NotEq", "Is", "IsNot", "Lt", "LtE", "Gt", "GtE"],
          "NotEq": ["Eq", "Is", "IsNot", "Lt", "LtE", "Gt", "GtE"],
          "Is": ["IsNot", "Eq", "NotEq"], "IsNot": ["Is", "Eq", "NotEq"],
          "Lt": ["LtE", "Gt", "GtE", "Eq", "NotEq"], "LtE": ["Lt", "Gt", "GtE", "Eq", "NotEq"],
          "Gt": ["GtE", "Lt", "LtE", "Eq", "NotEq"], "GtE": ["Gt", "Lt", "LtE", "Eq", "NotEq"],
          "In": ["NotIn"], "NotIn": ["In"]}

SITES = []          # sid -> (path, line, col, op)
SITE_OP = []
SITE_ALTS = []
CUR = [None]        # current key: test id, ("CLASS", parent id) or None
LOADING = [0]
REC = {}            # key -> [set(hit sids), set((sid, alt))]
SHARED = set()
FAILED = set()


SHARED_DIS = set()


def _rec(sid, dis_alts):
    if LOADING[0] or CUR[0] is None or isinstance(CUR[0], tuple) and CUR[0][0] != "CLASS":
        # module loads and anything outside a test shape state every later test sees
        SHARED.add(sid)
        for a in dis_alts:
            SHARED_DIS.add((sid, a))
        return
    r = REC.get(CUR[0])
    if r is None:
        r = REC[CUR[0]] = [set(), set()]
    r[0].add(sid)
    for a in dis_alts:
        r[1].add((sid, a))


IN_ALT = [0]


def __brc__(sid, a, b):
    op = SITE_OP[sid]
    if IN_ALT[0]:  # a comparison run only because we tried a sibling operator: not real execution
        return OPS[op](a, b)
    try:
        r = OPS[op](a, b)
    except BaseException:
        _rec(sid, SITE_ALTS[sid])
        raise
    dis = []
    IN_ALT[0] += 1
    try:
        for alt in SITE_ALTS[sid]:
            try:
                v = OPS[alt](a, b)
                same = v is r or (type(v) is bool and type(r) is bool and v == r)
            except BaseException:
                same = False
            if not same:
                dis.append(alt)
    finally:
        IN_ALT[0] -= 1
    _rec(sid, dis)
    return r


builtins.__brc__ = __brc__


class Rewriter(ast.NodeTransformer):
    def __init__(self, path):
        self.path = path

    def visit_Compare(self, node):
        self.generic_visit(node)
        if len(node.ops) != 1:
            return node
        op = type(node.ops[0]).__name__
        sid = len(SITES)
        SITES.append((self.path, node.lineno, node.col_offset, op))
        SITE_OP.append(op)
        SITE_ALTS.append(FAMILY[op])
        call = ast.Call(func=ast.Name(id="__brc__", ctx=ast.Load()),
                        args=[ast.Constant(sid), node.left, node.comparators[0]], keywords=[])
        return ast.copy_location(call, node)


class Loader(importlib.machinery.SourceFileLoader):
    def source_to_code(self, data, path, *, _optimize=-1):
        rel = os.path.relpath(path, ROOT)
        tree = ast.parse(data, path)
        tree = ast.fix_missing_locations(Rewriter(rel).visit(tree))
        return compile(tree, path, "exec", dont_inherit=True)

    def exec_module(self, module):
        LOADING[0] += 1
        try:
            super().exec_module(module)
        finally:
            LOADING[0] -= 1

    def get_code(self, fullname):  # never use cached bytecode
        path = self.get_filename(fullname)
        return self.source_to_code(self.get_data(path), path)


class Finder(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path, target=None):
        if not (name == "sqlglot" or name.startswith("sqlglot.")):
            return None
        spec = importlib.machinery.PathFinder.find_spec(name, path)
        if spec is None or not spec.origin or not spec.origin.endswith(".py"):
            return spec
        spec.loader = Loader(name, spec.origin)
        return spec


sys.meta_path.insert(0, Finder())


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_setup(item):
    CUR[0] = ("CLASS", item.parent.nodeid) if item.parent is not None else item.nodeid
    yield
    CUR[0] = None


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    CUR[0] = item.nodeid
    yield
    CUR[0] = None


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_teardown(item):
    CUR[0] = ("CLASS", item.parent.nodeid) if item.parent is not None else item.nodeid
    yield
    CUR[0] = None


def pytest_runtest_logreport(report):
    if report.failed:
        FAILED.add(report.nodeid)


def pytest_sessionfinish(session):
    items = getattr(session, "items", [])
    by_parent = {}
    for it in items:
        by_parent.setdefault(it.parent.nodeid if it.parent is not None else it.nodeid, []).append(it.nodeid)
    hit, dis = {}, {}
    for key, (h, d) in REC.items():
        tests = by_parent.get(key[1], []) if isinstance(key, tuple) else [key]
        for t in tests:
            for s in h:
                hit.setdefault(s, set()).add(t)
            for s, a in d:
                dis.setdefault(f"{s}:{a}", set()).add(t)
    out = {"sites": SITES, "hit": {k: sorted(v) for k, v in hit.items()},
           "dis": {k: sorted(v) for k, v in dis.items()}, "shared": sorted(SHARED), "shared_dis": sorted(f"{s}:{a}" for s, a in SHARED_DIS),
           "failed": sorted(FAILED)}
    json.dump(out, open(os.environ["OPMAP_OUT"], "w"))
