"""Merge recording runs of tools/opmap_plug.py into map/opmap.json.gz.
usage: opmap_build.py tests.json OUT.json.gz run1.json [run2.json ...]
A site counts as evaluated by a test if any run saw it; a sibling operator counts as differing if any
run saw it differ. Tests that failed while recorded are never dropped (left out of "hit")."""
import gzip
import json
import sys

tests = {t["id"]: t["number"] for t in json.load(open(sys.argv[1]))["tests"]}
sites, hit, dis, shared, failed, shared_dis = {}, {}, {}, set(), set(), set()
for path in sys.argv[3:]:
    d = json.load(open(path))
    failed |= set(d["failed"])
    local = {}
    for sid, (p, line, col, op) in enumerate(d["sites"]):
        key = f"{p}:{line}:{col}"
        if key not in sites:
            sites[key] = [op, None, len(sites)]
        elif sites[key][0] != op:
            raise SystemExit(f"operator mismatch at {key}")
        local[sid] = sites[key][2]
    for sid, ts in d["hit"].items():
        hit.setdefault(local[int(sid)], set()).update(ts)
    for k, ts in d["dis"].items():
        sid, alt = k.split(":")
        dis.setdefault(f"{local[int(sid)]}:{alt}", set()).update(ts)
    shared |= {local[s] for s in d["shared"]}
    for k in d["shared_dis"]:
        sid, alt = k.split(":")
        shared_dis.add(f"{local[int(sid)]}:{alt}")

FAMILY = {"Eq": ["NotEq", "Is", "IsNot", "Lt", "LtE", "Gt", "GtE"],
          "NotEq": ["Eq", "Is", "IsNot", "Lt", "LtE", "Gt", "GtE"],
          "Is": ["IsNot", "Eq", "NotEq"], "IsNot": ["Is", "Eq", "NotEq"],
          "Lt": ["LtE", "Gt", "GtE", "Eq", "NotEq"], "LtE": ["Lt", "Gt", "GtE", "Eq", "NotEq"],
          "Gt": ["GtE", "Lt", "LtE", "Eq", "NotEq"], "GtE": ["Gt", "Lt", "LtE", "Eq", "NotEq"],
          "In": ["NotIn"], "NotIn": ["In"]}
for v in sites.values():
    v[1] = FAMILY[v[0]]


def nums(ts):
    return sorted(tests[t] for t in ts if t in tests and t not in failed)


out = {"sites": sites,
       "hit": {str(k): nums(v) for k, v in hit.items()},
       "dis": {k: sorted(tests[t] for t in v if t in tests) for k, v in dis.items()},
       "shared": sorted(str(s) for s in shared), "shared_dis": sorted(shared_dis)}
json.dump(out, gzip.open(sys.argv[2], "wt", compresslevel=9))
print("sites", len(sites), "hit sites", len(hit), "shared", len(shared), "failed", len(failed))
