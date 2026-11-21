# pytest plugin: per-test record of files opened and dialect classes used
import sys, os, json, pytest
_cur = [None]
_opened = {}
_dialects = {}
ROOT = os.path.abspath("sqlglot") if os.path.isdir("sqlglot") else os.getcwd()

def _hook(event, args):
    if event == "open" and _cur[0] is not None:
        p = args[0]
        if isinstance(p, (str, bytes, os.PathLike)):
            try:
                p = os.path.abspath(os.fsdecode(p))
            except Exception:
                return
            _opened.setdefault(_cur[0], set()).add(p)

sys.addaudithook(_hook)

def pytest_configure(config):
    from sqlglot import dialects
    from sqlglot.dialects.dialect import Dialect
    orig_init = Dialect.__init__
    def init(self, *a, **k):
        if _cur[0] is not None:
            _dialects.setdefault(_cur[0], set()).add(type(self).__module__ + "." + type(self).__qualname__)
        return orig_init(self, *a, **k)
    Dialect.__init__ = init

_setup_opened = {}

@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_setup(item):
    _cur[0] = item.nodeid + "|setup"
    yield
    _cur[0] = item.nodeid

@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item, nextitem):
    _cur[0] = item.nodeid
    yield
    _cur[0] = None

def pytest_sessionfinish(session):
    out = {"opened": {k: sorted(v) for k, v in _opened.items()},
           "dialects": {k: sorted(v) for k, v in _dialects.items()}}
    with open(os.environ.get("TRACK_OUT", "/tmp/track.json"), "w") as f:
        json.dump(out, f)
