"""Scores the claim checks. Each family of mistake is planted into its own fresh copy of the clean claims, so one planting
cannot change another's surroundings. The checks must name the right rule. Borderline cases are included: some should be
flagged and some 'negative controls' should not. False alarms on the untouched claims are reported too.
This measures the checks, not real world denial rates."""
import json, sys
import duckdb, numpy as np, pandas as pd
import claim_checks as cc

N = 300


def load(db):
    c = duckdb.connect(db, read_only=True)
    t = lambda q: c.execute(q).fetchdf()
    return (t("select * from claims"), t("select * from claim_items"), t("select * from claim_diagnoses"),
            t("select id, start_date from encounters"), set(t("select id from conditions")["id"]))


ts = lambda d: d.dt.strftime("%Y-%m-%dT12:00:00+00:00")


def prep(cl, it):
    cl["lines_sum"] = cl["claim_id"].map(it.groupby("claim_id")["net"].sum(min_count=1))
    for c in ("created", "period_end", "period_start"):
        cl[c[:2] + "_" + c.split("_")[-1] + "_d" if False else {"created": "created_d", "period_end": "end_d", "period_start": "start_d"}[c]] = cc._day(cl[c])
    return cl


def tiers(rng, ids, specs):
    ids = list(ids); rng.shuffle(ids); k = len(specs)
    return [(ids[i::k], spec) for i, spec in enumerate(specs)]


def families(rng):
    """Each family: (name, plant function). plant(cl, it, dg, enc) -> (cl, it, dg, plan rows)."""
    def pick(mask, cl, n):
        ids = list(cl.loc[mask, "claim_id"]); return list(rng.choice(ids, size=min(n, len(ids)), replace=False))

    def total(cl, it, dg, enc):
        ok = (cl["claim_type"] != "pharmacy") & ((cl["lines_sum"].fillna(0) - cl["total"]).abs() <= cc.TOL); plan = []
        for ids, (d, f) in tiers(rng, pick(ok, cl, N * 4 // 3), [(0.5, 1), (5, 1), (50, 1), (0.004, 0)]):
            cl.loc[cl.claim_id.isin(ids), "total"] += d; plan += [(i, "total_mismatch", f"off by {d}", f) for i in ids]
        return cl, it, dg, plan

    def paid(cl, it, dg, enc):
        plan = []
        for ids, (d, f) in tiers(rng, pick(cl["total"].notna(), cl, N * 4 // 3), [(0.5, 1), (5, 1), (50, 1), (0.005, 0)]):
            m = cl.claim_id.isin(ids); cl.loc[m, "paid_by_payer"] = cl.loc[m, "total"] + d; plan += [(i, "overpayment", f"over by {d}", f) for i in ids]
        return cl, it, dg, plan

    def late(cl, it, dg, enc):
        plan = []
        for ids, (d, f) in tiers(rng, pick(cl["claim_id"].notna(), cl, N * 4 // 3), [(366, 1), (400, 1), (800, 1), (365, 0)]):
            m = cl.claim_id.isin(ids); cl.loc[m, "created"] = ts(cl.loc[m, "end_d"] + pd.Timedelta(days=d)); plan += [(i, "late_filing", f"{d} days late", f) for i in ids]
        return cl, it, dg, plan

    def outside(cl, it, dg, enc):
        has = cl["claim_id"].isin(it.dropna(subset=["encounter_id"])["claim_id"]); plan = []
        for ids, (d, f) in tiers(rng, pick(has, cl, N * 4 // 3), [(3, 1), (30, 1), (400, 1), (1, 0)]):
            m = cl.claim_id.isin(ids)
            cl.loc[m, "period_start"] = ts(cl.loc[m, "start_d"] + pd.Timedelta(days=d)); cl.loc[m, "period_end"] = ts(cl.loc[m, "end_d"] + pd.Timedelta(days=d))
            cl.loc[m, "created"] = cl.loc[m, "period_end"]; plan += [(i, "visit_outside_period", f"period moved {d} days", f) for i in ids]
        return cl, it, dg, plan

    def dxbad(cl, it, dg, enc):
        plan = []
        for i in pick(cl["claim_id"].isin(dg["claim_id"]), cl, N):
            dg.loc[dg.index[dg["claim_id"] == i][0], "condition_id"] = "missing-condition"; plan.append((i, "diagnosis_not_in_chart", "unknown diagnosis", 1))
        return cl, it, dg, plan

    def visitbad(cl, it, dg, enc):
        plan = []
        for i in pick(cl["claim_id"].isin(it.dropna(subset=["encounter_id"])["claim_id"]), cl, N):
            it.loc[it.index[(it["claim_id"] == i) & it["encounter_id"].notna()][0], "encounter_id"] = "missing-visit"; plan.append((i, "visit_not_in_chart", "unknown visit", 1))
        return cl, it, dg, plan

    def dup(cl, it, dg, enc):
        ids = pick(cl["claim_type"] != "pharmacy", cl, N)
        e = cl[cl.claim_id.isin(ids)].copy(); e["claim_id"] = "dup-" + e["claim_id"]
        ei = it[it.claim_id.isin(ids)].copy(); ei["claim_id"] = "dup-" + ei["claim_id"]
        return pd.concat([cl, e], ignore_index=True), pd.concat([it, ei], ignore_index=True), dg, [(i, "duplicate_claim", "exact copy", 1) for i in ids]

    def payer(cl, it, dg, enc):
        nb = cl[cl.claim_type != "pharmacy"][["claim_id", "patient_id", "payer", "created_d"]]
        j = nb.merge(nb, on="patient_id", suffixes=("", "_o")); j = j[(j.claim_id != j.claim_id_o) & ((j.created_d - j.created_d_o).abs().dt.days <= cc.PAYER_WINDOW_DAYS)]
        near = j.groupby("claim_id")["payer_o"].agg(set); payers = sorted(cl["payer"].unique()); plan = []
        usable = near[near.map(lambda s: len(s) == 1)]            # claims whose neighbours agree on one payer, so the right answer is clear
        usable = usable[[len(j[j.claim_id == i]) >= 2 for i in usable.index]] if False else usable
        by_patient = cl.set_index("claim_id")["patient_id"].reindex(usable.index)
        one_each = usable.index.to_series().groupby(by_patient.values).apply(lambda x: rng.choice(list(x)))   # one planted swap per patient
        counts = j.groupby("claim_id").size()
        ids = [i for i in one_each if counts.get(i, 0) >= 2]
        for i in list(rng.choice(ids, size=min(N, len(ids)), replace=False)):
            new = [p for p in payers if p not in near[i]]
            if new: cl.loc[cl.claim_id == i, "payer"] = new[0]; plan.append((i, "payer_changed", "payer switched", 1))
        return cl, it, dg, plan
    return [("totals", total), ("payments", paid), ("late filing", late), ("dates", outside), ("diagnoses", dxbad), ("visits", visitbad), ("duplicates", dup), ("payers", payer)]


def main(db, out_path, seed=0):
    rng = np.random.default_rng(seed)
    claims, items, dx, enc, cond = load(db)
    clean = cc.check_claims(claims, items, dx, enc, cond)
    res = {"clean_run": {"claims": len(claims), "error_flags": int((clean.severity == "error").sum()), "warning_flags": int((clean.severity == "warning").sum())},
           "planted": {}, "negative_controls": {}}
    caught = planted = collateral = 0; extra_rules = {}
    for name, plant in families(rng):
        cl, it, dg = prep(claims.copy(), items.copy()), items.copy(), dx.copy()
        cl, it, dg, plan = plant(cl, it, dg, enc)
        f = cc.check_claims(cl.drop(columns=["lines_sum", "created_d", "end_d", "start_d"]), it, dg, enc, cond)
        f["base"] = f["claim_id"].str.replace("dup-", "", regex=False)
        hit = set(zip(f["base"], f["rule"]))
        rows = pd.DataFrame(plan, columns=["claim_id", "rule", "case", "should_flag"]); rows["flagged"] = [(c, r) in hit for c, r in zip(rows.claim_id, rows.rule)]
        for (rule, case), g in rows[rows.should_flag == 1].groupby(["rule", "case"], sort=False):
            res["planted"][f"{rule} | {case}"] = {"planted": len(g), "caught": int(g.flagged.sum())}
        for (rule, case), g in rows[rows.should_flag == 0].groupby(["rule", "case"], sort=False):
            res["negative_controls"][f"{rule} | {case}"] = {"tried": len(g), "wrongly_flagged": int(g.flagged.sum())}
        p = rows[rows.should_flag == 1]; planted += len(p); caught += int(p.flagged.sum())
        extra = f[~f["base"].isin(set(rows.claim_id))]
        extra = extra.merge(clean[["claim_id", "rule"]].assign(was=1), on=["claim_id", "rule"], how="left"); extra = extra[extra.was.isna()]  # not already flagged on the clean data
        collateral += len(extra)
        for r, n in extra["rule"].value_counts().items(): extra_rules[r] = extra_rules.get(r, 0) + int(n)
    res["totals"] = {"planted": planted, "caught": caught, "recall": round(caught / planted, 4), "new_flags_on_untouched_claims": collateral, "new_flag_rules": extra_rules}
    json.dump(res, open(out_path, "w"), indent=2)
    return res


if __name__ == "__main__":
    r = main(sys.argv[1], sys.argv[2]); t = r["totals"]
    print("clean run:", r["clean_run"])
    for k, v in r["planted"].items(): print(f"  {k:46s} {v['caught']:4d} / {v['planted']}")
    for k, v in r["negative_controls"].items(): print(f"  control {k:38s} wrongly flagged {v['wrongly_flagged']} / {v['tried']}")
    print("totals:", t)
