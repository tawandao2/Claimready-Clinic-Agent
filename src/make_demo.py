"""Builds the small demo set of damaged claims for the review queue: real claims for a few patients with known mistakes planted.
The planted_truth table says exactly what was changed, so the demo can show whether each mistake was found."""
import sys, duckdb, numpy as np, pandas as pd
import evaluate_claims as ev, claim_checks as cc

db, out = sys.argv[1:3]
ev.N = 24
rng = np.random.default_rng(7)
claims, items, dx, enc, cond = ev.load(db)
n = claims.groupby("patient_id").size()
pts = list(n[(n >= 60) & (n <= 400)].sample(12, random_state=3).index)
cl, it, dg = claims[claims.patient_id.isin(pts)].copy(), items[items.claim_id.isin(claims[claims.patient_id.isin(pts)].claim_id)].copy(), dx[dx.claim_id.isin(claims[claims.patient_id.isin(pts)].claim_id)].copy()
truth = []
for name, plant in ev.families(rng):
    cl = ev.prep(cl, it)
    cl, it, dg, plan = plant(cl, it, dg, enc)
    cl = cl.drop(columns=["lines_sum", "created_d", "end_d", "start_d"], errors="ignore")
    truth += [p for p in plan if p[3] == 1]
truth = pd.DataFrame(truth, columns=["claim_id", "rule", "case", "should_flag"]).drop(columns="should_flag")
con = duckdb.connect(out)
for k, v in (("claims", cl), ("claim_items", it), ("claim_diagnoses", dg), ("planted_truth", truth)):
    con.execute(f"create or replace table {k} as select * from v")
f = cc.check_claims(cl, it, dg, enc, cond); hit = set(zip(f.claim_id.str.replace("dup-", "", regex=False), f.rule))
truth["found"] = [(c, r) in hit for c, r in zip(truth.claim_id, truth.rule)]
print(len(cl), "claims,", len(truth), "planted;", int(truth.found.sum()), "found")
print(truth.groupby("rule").found.agg(["sum", "size"]).to_string())
