"""Coverage of the parts of TestExecutor.test_execute_tpc{h,ds} that the equivalence check does not
fingerprint (stage B): table setup, find_tables, pickling, ensure_*, the python executor, result
handling. Parsing, optimize() + Plan() and the duckdb transpile are stage A: their outputs are
compared before/after a patch instead.
usage: stageb.py SG_TREE tpcds|tpch OUT.json"""
import json, os, pickle, sys

tree, which, out = sys.argv[1:4]
sys.path.insert(0, tree)
os.chdir(tree)
os.environ["SKIP_INTEGRATION"] = "0"
import coverage

cov = coverage.Coverage(source=[os.path.join(tree, "sqlglot")], data_file=None)
cov.start()
# import time counts as stage B: functions run while modules load (metaclasses, builders) shape
# state the executor reads later. Load every module and dialect, as a full test run would.
import importlib, pkgutil
import sqlglot
for info in pkgutil.walk_packages(sqlglot.__path__, prefix="sqlglot."):
    if not info.name.endswith("__main__"):
        importlib.import_module(info.name)
from sqlglot.dialects.dialect import Dialect
for d in list(Dialect.classes):
    Dialect.get_or_raise(d)
cov.stop()

import pandas as pd
import numpy as np
from pandas.testing import assert_frame_equal
import sqlglot.executor as ex
from sqlglot import parse_one, transpile, find_tables
from tests import test_executor as te

tpch = which == "tpch"
_opt, _plan = ex.optimize, ex.Plan


def off(f):
    def g(*a, **k):
        cov.stop()
        try:
            return f(*a, **k)
        finally:
            cov.start()
    return g


ex.optimize = off(_opt)
ex.Plan = off(_plan)

cov.start()
te.TestExecutor.setUpClass()
cov.stop()
T = te.TestExecutor
schema = te.TPCH_SCHEMA if tpch else te.TPCDS_SCHEMA
tables = T.tpch_tables if tpch else T.tpcds_tables
sqls = T.tpch_sqls if tpch else T.tpcds_sqls
conn = T.tpch_conn if tpch else T.tpcds_conn
n = 0
for i, (meta, sql, _) in enumerate(sqls):
    expr = parse_one(sql)  # stage A
    cov.start()
    expr = pickle.loads(pickle.dumps(expr))
    te.initializer(schema, tables)
    table = te.mp_execute(expr, meta)
    if table is not None:
        table = pickle.loads(pickle.dumps(table))
    cov.stop()
    if table is None:
        continue
    n += 1
    a = conn.execute(transpile(sql, write="duckdb")[0]).fetchdf()  # stage A'
    cov.start()
    b = pd.DataFrame(((np.nan if c is None else c for c in r) for r in table.rows), columns=table.columns)
    cov.stop()
    assert_frame_equal(a, b, check_dtype=False, check_index_type=False)
data = cov.get_data()
res = {}
for f in data.measured_files():
    rel = os.path.relpath(f, tree)
    ls = sorted(data.lines(f) or [])
    if ls:
        res[rel] = ls
json.dump(res, open(out, "w"))
print(which, "queries", n, "files", len(res), "lines", sum(map(len, res.values())))
