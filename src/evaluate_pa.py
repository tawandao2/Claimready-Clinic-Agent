"""Runs every supported order for every patient in the demo cohort and saves how the rules decided, and why.
There is no right answer to score against, because real insurer decisions are not public. This shows how the rules behave."""
import sys, json, collections, duckdb
import prior_auth as pa

db, out = sys.argv[1:3]
c = duckdb.connect(db, read_only=True); pts = c.execute("select id from patients").fetchdf()["id"].tolist(); c.close()
res = {"order_date": "2026-10-25", "patients": len(pts), "orders": {}}
for key, var in [("CGM", None), ("DXA", None), ("GLP1", "centene"), ("GLP1", "metformin_first"), ("MRI_LUMBAR", None)]:
    dec, crit = collections.Counter(), collections.defaultdict(collections.Counter)
    for p in pts:
        o = {"patient_id": p, "order_type": key, "order_date": res["order_date"]}
        if var: o["policy_variant"] = var
        r = pa.evaluate_order(db, o); dec[r["decision"]] += 1
        for x in r["criteria"]: crit[x["id"]][x["status"]] += 1
    res["orders"][key + (f" ({var})" if var else "")] = {"title": pa.POLICIES[key]["order"], "decisions": dict(dec), "criteria": {k: dict(v) for k, v in crit.items()}}
json.dump(res, open(out, "w"), indent=2); print({k: v["decisions"] for k, v in res["orders"].items()})
