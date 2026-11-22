"""Fingerprints of what TestExecutor.test_execute_tpc{h,ds} feeds its python executor and duckdb.

For each query the test executes: the parsed query, the plan execute() builds from optimize(), and
the duckdb SQL the test compares against. Same fingerprints before and after a patch, and a patch
that only changes functions those stages run (never the executor, table or setup code), means the
test sees exactly the same inputs as on the pinned tree, so it passes as it does there.
"""
import hashlib
import sys


def _h(*parts):
    return hashlib.sha1(repr(parts).encode()).hexdigest()


def _fp(o, exp, Step, memo):
    """structural digest; steps are shared between parents, so each is hashed once"""
    if isinstance(o, exp.Expr):
        return ("E", type(o).__module__, type(o).__qualname__,
                  tuple((k, _fp(v, exp, Step, memo)) for k, v in o.args.items()),
                  _fp(o._type, exp, Step, memo), tuple(o.comments) if o.comments else None,
                  _fp(o._meta, exp, Step, memo))
    if o is None or isinstance(o, (str, int, float, bool, bytes)):
        return ("P", type(o).__name__, repr(o))
    if isinstance(o, (list, tuple)):
        return ("L", type(o).__name__, tuple(_fp(v, exp, Step, memo) for v in o))
    if isinstance(o, dict):
        return ("D", type(o).__name__, tuple((_fp(k, exp, Step, memo), _fp(v, exp, Step, memo)) for k, v in o.items()))
    if isinstance(o, (set, frozenset)):
        return _h("S", type(o).__name__, tuple(sorted(repr(_fp(v, exp, Step, memo)) for v in o)))
    if isinstance(o, Step):
        if id(o) not in memo:
            memo[id(o)] = None  # a cycle through dependencies would show up as None
            # dependents mirror dependencies; skip them to avoid cycles
            memo[id(o)] = _h("St", type(o).__qualname__,
                             tuple((k, _fp(v, exp, Step, memo)) for k, v in sorted(vars(o).items()) if k != "dependents"))
        return memo[id(o)]
    if isinstance(o, type):
        return ("T", o.__module__, o.__qualname__)
    import enum
    if isinstance(o, enum.Enum):
        return ("N", type(o).__qualname__, o.name)
    raise TypeError(f"no fingerprint for {type(o)!r}")


def fingerprints(tree, which, base=None, max_diff=0, where=None):
    """-> list of hex digests, one per executed query, in fixture order; with base, stops once more
    than max_diff digests differ from it. where (a list) gets each query's fixture index."""
    sys.path.insert(0, tree)
    from sqlglot import exp, parse_one, transpile
    from sqlglot.optimizer import optimize
    from sqlglot.planner import Plan, Step
    from sqlglot.schema import ensure_schema
    from tests.helpers import TPCDS_SCHEMA, TPCH_SCHEMA, load_sql_fixture_pairs

    if which == "tpch":
        schema, sqls = TPCH_SCHEMA, load_sql_fixture_pairs("optimizer/tpc-h/tpc-h.sql")
    else:
        schema, sqls = TPCDS_SCHEMA, load_sql_fixture_pairs("optimizer/tpc-ds/tpc-ds.sql")
    out = []
    ndiff = 0
    for i, (meta, sql, _) in enumerate(sqls):
        if not meta.get("execute"):
            continue
        if where is not None:
            where.append(i)
        parts = []
        for step in ("parse", "plan", "duckdb"):
            try:
                if step == "parse":
                    e = parse_one(sql)
                    parts.append(_fp(e, exp, Step, {}))
                elif step == "plan":
                    # as sqlglot.executor.execute does it
                    s = ensure_schema(schema, dialect=None)
                    o = optimize(e, s, leave_tables_isolated=True, dialect=None)
                    parts.append(_fp(vars(Plan(o)), exp, Step, {}))
                else:
                    parts.append(transpile(sql, write="duckdb")[0])
            except TypeError as err:
                if "no fingerprint" in str(err):
                    raise
                parts.append(("X", type(err).__name__, str(err)[:300]))
            except Exception as err:  # noqa
                parts.append(("X", type(err).__name__, str(err)[:300]))
        out.append(hashlib.sha1(repr(parts).encode()).hexdigest())
        if base is not None:
            if len(out) > len(base):
                break
            if out[-1] != base[len(out) - 1]:
                ndiff += 1
                if ndiff > max_diff:
                    break
    return out


def run_queries(tree, idx):
    """run the tpcds subtests idx (fixture indices) as test_execute_tpcds does; raises if one fails"""
    import os
    import pickle

    sys.path.insert(0, tree)
    os.environ["SKIP_INTEGRATION"] = "0"
    from sqlglot import find_tables, parse_one
    from tests import test_executor as te

    T = te.TestExecutor
    sqls = list(te.load_sql_fixture_pairs("optimizer/tpc-ds/tpc-ds.sql"))
    need = set()
    for i in idx:
        need |= {t.name for t in find_tables(parse_one(sqls[i][1]))}
    full_h, full_d = te.TPCH_SCHEMA, te.TPCDS_SCHEMA
    # the test's own setup, loading only the tables these queries read
    te.TPCH_SCHEMA, te.TPCDS_SCHEMA = {}, {k: v for k, v in full_d.items() if k in need}
    try:
        T.setUpClass()
    finally:
        te.TPCH_SCHEMA, te.TPCDS_SCHEMA = full_h, full_d
    inst = T("test_execute_tpcds")
    te.initializer(full_d, T.tpcds_tables)
    for i in idx:
        args, sql, _ = T.tpcds_sqls[i]
        table = te.mp_execute(pickle.loads(pickle.dumps(parse_one(sql))), args)
        if table is not None:
            inst.subtestHelper(i, pickle.loads(pickle.dumps(table)), tpch=False)
    T.tearDownClass()


RUN_CAP = 15


def check(tree, base, max_diff):
    """-> "same", "pass" (the differing queries were run and pass), or "differ" """
    where = []
    got = fingerprints(tree, "tpcds", base, max_diff, where)
    if len(got) != len(base):
        return "differ"
    diff = [where[k] for k, (a, b) in enumerate(zip(got, base)) if a != b]
    if not diff:
        return "same"
    if len(diff) > max_diff:
        return "differ"
    # a changed query that runs far slower could time out in CI: cap the run
    import signal

    def slow(*_):
        raise TimeoutError("slow")

    old = signal.signal(signal.SIGALRM, slow)
    signal.alarm(RUN_CAP)
    try:
        run_queries(tree, diff)
    except BaseException:  # noqa
        return "differ"
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old)
    return "pass"


if __name__ == "__main__":
    # at image build: python tpcfp.py SG_TREE OUT.json (the pinned tree's fingerprints)
    import json
    import os

    os.chdir("/tmp")
    json.dump(fingerprints(sys.argv[1], "tpcds"), open(sys.argv[2], "w"))
