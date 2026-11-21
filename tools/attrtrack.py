# pytest plugin for map building only: record, per test, which class attributes of sqlglot
# classes are read (owner class, attribute name). Runs at the pinned commit.
import collections
import enum
import json
import os
import sys
import types

import pytest

_cur = [None]
_events = collections.defaultdict(set)
_classes = {}


def canon(k):
    t = type(k)
    if t is str:
        return "S" + k
    if isinstance(k, enum.Enum):
        return "E" + k.name
    if isinstance(k, type):
        return "T" + k.__name__
    if t in (int, float, bool):
        return "N" + repr(k)
    return "?"


def _ev(tag, k):
    c = _cur[0]
    if c is not None and tag is not None:
        _events[c].add((tag[0], tag[1] + "\x01" + k))


def _star(self):
    _ev(getattr(self, "_tag", None), "*")


def _size(self):
    _ev(getattr(self, "_tag", None), "#")


class TDict(dict):
    __slots__ = ("_tag",)

    def __getitem__(self, k):
        try:
            _ev(getattr(self, "_tag", None), canon(k))
        except Exception:
            pass
        return dict.__getitem__(self, k)

    def get(self, k, d=None):
        try:
            _ev(getattr(self, "_tag", None), canon(k))
        except Exception:
            pass
        return dict.get(self, k, d)

    def __contains__(self, k):
        try:
            _ev(getattr(self, "_tag", None), canon(k))
        except Exception:
            pass
        return dict.__contains__(self, k)

    def setdefault(self, k, d=None):
        _ev(getattr(self, "_tag", None), canon(k))
        return dict.setdefault(self, k, d)

    def pop(self, k, *a):
        _ev(getattr(self, "_tag", None), canon(k))
        return dict.pop(self, k, *a)


def _starwrap(base, names):
    def mk(name):
        orig = getattr(base, name)
        rec = _size if name in ("__len__", "__bool__") else _star

        def f(self, *a, **kw):
            rec(self)
            return orig(self, *a, **kw)

        f.__name__ = name
        return f

    return {n: mk(n) for n in names if hasattr(base, n)}


_DICT_STAR = ["__iter__", "keys", "values", "items", "copy", "__len__", "__or__", "__ror__", "__eq__",
              "__ne__", "__reduce_ex__", "__reduce__", "__repr__", "update", "popitem", "__reversed__",
              "__bool__"]
for _n, _f in _starwrap(dict, _DICT_STAR).items():
    setattr(TDict, _n, _f)
TDict.__copy__ = lambda self: (_star(self), dict(self))[1]
TDict.__deepcopy__ = lambda self, memo: (_star(self), __import__("copy").deepcopy(dict(self), memo))[1]
TDict.__hash__ = None

_SET_STAR = ["__iter__", "__len__", "__or__", "__ror__", "__and__", "__rand__", "__sub__", "__rsub__",
             "__xor__", "__rxor__", "union", "intersection", "difference", "symmetric_difference",
             "issubset", "issuperset", "isdisjoint", "copy", "__eq__", "__ne__", "__le__", "__lt__",
             "__ge__", "__gt__", "__reduce__", "__reduce_ex__", "__repr__", "__bool__"]


def _mk_set(base):
    def contains(self, k):
        try:
            _ev(getattr(self, "_tag", None), canon(k))
        except Exception:
            pass
        return base.__contains__(self, k)

    ns = {"__slots__": ("_tag",), "__contains__": contains}
    ns.update(_starwrap(base, _SET_STAR))
    ns["__hash__"] = base.__hash__ if base is frozenset else None
    return type("T" + base.__name__, (base,), ns)


TSet = _mk_set(set)
TFrozenSet = _mk_set(frozenset)

_SEQ_STAR = ["__iter__", "__len__", "__getitem__", "index", "count", "__add__", "__radd__", "__mul__",
             "__rmul__", "__eq__", "__ne__", "__lt__", "__le__", "__gt__", "__ge__", "__reduce__",
             "__reduce_ex__", "__repr__", "__reversed__", "copy", "__bool__"]


def _mk_seq(base):
    def contains(self, k):
        try:
            _ev(getattr(self, "_tag", None), canon(k))
        except Exception:
            pass
        return base.__contains__(self, k)

    ns = {"__slots__": ("_tag",) if base is list else (), "__contains__": contains}
    ns.update(_starwrap(base, _SEQ_STAR))
    if base is tuple:
        ns["__hash__"] = tuple.__hash__
    return type("T" + base.__name__, (base,), ns)


TList = _mk_seq(list)
TTuple = _mk_seq(tuple)
_tuple_tags = {}


def wrap(tag, val):
    t = type(val)
    try:
        if t is dict:
            w = TDict(val)
            w._tag = tag
            return w
        if t is set:
            w = TSet(val)
            w._tag = tag
            return w
        if t is frozenset:
            w = TFrozenSet(val)
            w._tag = tag
            return w
        if t is list:
            w = TList(val)
            w._tag = tag
            return w
    except Exception:
        return val
    return val


class TAttr:
    __slots__ = ("tag", "val")

    def __init__(self, tag, val):
        self.tag = tag
        self.val = val

    def __get__(self, obj, owner=None):
        c = _cur[0]
        if c is not None:
            _events[c].add(self.tag)
        return self.val


_SKIP_TYPES = (
    types.FunctionType,
    staticmethod,
    classmethod,
    property,
    type,
    types.BuiltinFunctionType,
    types.MethodDescriptorType,
    types.WrapperDescriptorType,
    types.MemberDescriptorType,
    types.GetSetDescriptorType,
)


def _key(c):
    return f"{c.__module__}.{c.__qualname__}"


def _collect(mods):
    seen = set()
    out = []

    def add(c):
        if id(c) in seen or not isinstance(c, type):
            return
        if not (c.__module__ or "").startswith("sqlglot"):
            return
        seen.add(id(c))
        out.append(c)
        for v in list(vars(c).values()):
            if isinstance(v, type):
                add(v)
        for s in type.__subclasses__(c):
            add(s)

    for m in mods:
        for v in list(vars(m).values()):
            add(v)
    return out


def install():
    import importlib
    import pkgutil

    import sqlglot

    mods = []
    for info in pkgutil.walk_packages(sqlglot.__path__, prefix="sqlglot."):
        if info.name.endswith("__main__"):
            continue
        try:
            mods.append(importlib.import_module(info.name))
        except Exception:
            pass
    # the generator caches its dispatch table per class for the whole process; turn that off so
    # every test that generates SQL reads TRANSFORMS itself
    import sqlglot.generator as g

    class NoCache(dict):
        def get(self, k, d=None):
            return d

        def __setitem__(self, k, v):
            pass

    g._DISPATCH_CACHE = NoCache()
    if os.environ.get("KEYS"):
        orig_build = g._build_dispatch

        def build(cls):
            saved = _cur[0]
            _cur[0] = None  # copying TRANSFORMS touches every key; the dispatch table records real use
            try:
                base = orig_build(cls)
            finally:
                _cur[0] = saved
            d = TDict(base)
            d._tag = (_key(cls), "__dispatch__")
            return d

        g._build_dispatch = build

    for c in _collect(mods):
        if issubclass(c, enum.Enum):
            continue
        k = _key(c)
        _classes[k] = [_key(b) for b in c.__bases__ if (b.__module__ or "").startswith("sqlglot")]
        for name, val in list(vars(c).items()):
            if name.startswith("__") and name.endswith("__"):
                continue
            if isinstance(val, _SKIP_TYPES) or isinstance(val, TAttr):
                continue
            if hasattr(type(val), "__get__"):
                continue
            try:
                setattr(c, name, TAttr((k, name), wrap((k, name), val) if os.environ.get("KEYS") else val))
            except (TypeError, AttributeError):
                pass


def pytest_configure(config):
    install()


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item, nextitem):
    _cur[0] = item.nodeid
    yield
    _cur[0] = None


def pytest_sessionfinish(session):
    out = {
        "classes": _classes,
        "events": {t: sorted(f"{a}\t{b}" for a, b in ev) for t, ev in _events.items()},
    }
    with open(os.environ.get("ATTR_OUT", "/tmp/attrs.json"), "w") as f:
        json.dump(out, f)
