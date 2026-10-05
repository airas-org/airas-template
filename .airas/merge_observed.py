"""プロセスごとの記録を observed.json に結合する。python3 merge_observed.py <dir> <run_id> <out>

全プロセスで同じ節（hook、modules、symbols、process.env）は上位に 1 回だけ書き、
各プロセスからは外す。1 件 1 プロセスの構成で同じものを件数分繰り返さないため。"""

import glob
import json
import sys

d, run_id, out = sys.argv[1:]
processes = [json.load(open(f)) for f in sorted(glob.glob(d + "/*.json"))]
shared = {}
for key in ("hook", "modules", "symbols"):
    values = [p[key] for p in processes if p.get(key)]
    if values and all(v == values[0] for v in values):
        shared[key] = values[0]
        for p in processes:
            p.pop(key, None)
envs = [p["process"]["env"] for p in processes]
if envs and all(e == envs[0] for e in envs):
    shared["env"] = envs[0]
    for p in processes:
        p["process"].pop("env")
json.dump(
    {"version": 1, "run_id": run_id, **shared, "processes": processes},
    open(out, "w"),
    ensure_ascii=False,
    indent=1,
)
