# pytest plugin for map building: run ProcessPoolExecutor work in-process (with a pickle
# round trip, like the real pool) so coverage contexts and tracking see it.
import pickle, concurrent.futures as cf, pytest

class SyncPool:
    def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=(), **kw):
        if initializer is not None:
            initializer(*pickle.loads(pickle.dumps(initargs)))
    def submit(self, fn, *args, **kwargs):
        f = cf.Future()
        try:
            a, k = pickle.loads(pickle.dumps((args, kwargs)))
            r = fn(*a, **k)
            f.set_result(pickle.loads(pickle.dumps(r)))
        except BaseException as e:
            f.set_exception(e)
        return f
    def map(self, fn, *its, **kw):
        return [self.submit(fn, *a).result() for a in zip(*its)]
    def shutdown(self, *a, **k):
        pass
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False

def pytest_collection_modifyitems(session, config, items):
    import sys
    for name in ("tests.test_optimizer", "tests.test_executor"):
        m = sys.modules.get(name)
        if m is not None and hasattr(m, "ProcessPoolExecutor"):
            m.ProcessPoolExecutor = SyncPool
