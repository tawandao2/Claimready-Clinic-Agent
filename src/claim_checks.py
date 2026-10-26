"""Pre submission claim checks. Plain rules, no AI. Each finding names a rule, the claim, and the lines or records behind it.
Rules were chosen by running them on the clean synthetic claims first: a rule that fired on normal claims was dropped or made a warning."""
import pandas as pd

TOL = 0.01                # dollars
FILING_LIMIT_DAYS = 365   # Medicare's 12 month filing limit, used as the example limit
PAYER_WINDOW_DAYS = 30
RULES = {
    "total_mismatch": ("error", "Claim total does not equal the sum of its lines (professional and hospital claims)"),
    "overpayment": ("error", "Payment recorded is higher than the amount billed"),
    "duplicate_claim": ("error", "Same patient, payer, start date, total and services billed twice (professional and hospital claims)"),
    "late_filing": ("error", f"Claim created more than {FILING_LIMIT_DAYS} days after the service period ended"),
    "diagnosis_not_in_chart": ("error", "Claim lists a diagnosis that is not in the patient's chart"),
    "visit_not_in_chart": ("error", "A claim line points at a visit that is not in the chart"),
    "visit_outside_period": ("error", "A claim line's visit date falls outside the claim's service period"),
    "payer_changed": ("warning", f"Payer differs from what at least two other claims for the patient within {PAYER_WINDOW_DAYS} days agree on (can be a real coverage change)"),
}


def _day(s):
    """Calendar date as written, ignoring the time zone. Visit dates have no zone, so claim times must not be shifted to UTC either."""
    return pd.to_datetime(s.astype(str).str[:10], errors="coerce")


def check_claims(claims, items, dx, enc, cond_ids):
    """Returns one row per finding: claim_id, rule, severity, detail, evidence_ids."""
    c = claims.copy()
    c["created_d"], c["start_d"], c["end_d"] = _day(c["created"]), _day(c["period_start"]), _day(c["period_end"])
    out = []
    def add(df, rule, detail, ev=None):
        for r in df.itertuples():
            out.append({"claim_id": r.claim_id, "rule": rule, "severity": RULES[rule][0], "detail": detail(r),
                        "evidence_ids": ev(r) if ev else [r.claim_id]})

    s = items.groupby("claim_id")["net"].sum(min_count=1).rename("lines_sum")
    c = c.join(s, on="claim_id")
    bill = c[c["claim_type"] != "pharmacy"]
    add(bill[(bill["lines_sum"].fillna(0) - bill["total"]).abs() > TOL], "total_mismatch",
        lambda r: f"Claim total {r.total:.2f} but the lines add up to {0 if pd.isna(r.lines_sum) else r.lines_sum:.2f}")
    add(c[c["paid_by_payer"] > c["total"] + TOL], "overpayment", lambda r: f"Payment {r.paid_by_payer:.2f} is more than the billed total {r.total:.2f}")

    codes = items.groupby("claim_id")["code"].agg(lambda x: "|".join(sorted(map(str, x)))).rename("code_set")
    b = bill.join(codes, on="claim_id")
    key = ["patient_id", "claim_type", "payer", "period_start", "total", "code_set"]
    d = b[b.duplicated(key, keep=False)]
    for _, grp in d.groupby(key):
        ids_ = sorted(grp["claim_id"])
        for cid in ids_:
            out.append({"claim_id": cid, "rule": "duplicate_claim", "severity": "error",
                        "detail": f"Same patient, payer, start date, total and services as claim(s) {', '.join(i for i in ids_ if i != cid)}", "evidence_ids": ids_})

    c["lag"] = (c["created_d"] - c["end_d"]).dt.days
    add(c[c["lag"] > FILING_LIMIT_DAYS], "late_filing", lambda r: f"Created {int(r.lag)} days after the service period ended")

    bad = dx[~dx["condition_id"].isin(cond_ids)]
    add(c[c["claim_id"].isin(bad["claim_id"])], "diagnosis_not_in_chart",
        lambda r: "Diagnosis record(s) not in chart: " + ", ".join(bad[bad["claim_id"] == r.claim_id]["condition_id"]),
        lambda r: [r.claim_id] + list(bad[bad["claim_id"] == r.claim_id]["condition_id"]))

    it = items.dropna(subset=["encounter_id"]).merge(enc[["id", "start_date"]].rename(columns={"id": "encounter_id"}), on="encounter_id", how="left")
    miss = it[it["start_date"].isna()]
    add(c[c["claim_id"].isin(miss["claim_id"])], "visit_not_in_chart", lambda r: "Line(s) point at missing visit(s): " + ", ".join(sorted(set(miss[miss["claim_id"] == r.claim_id]["encounter_id"]))),
        lambda r: [f"{r.claim_id}:line{n}" for n in miss[miss["claim_id"] == r.claim_id]["line"]])
    it = it.dropna(subset=["start_date"]).merge(c[["claim_id", "start_d", "end_d"]], on="claim_id")
    it["vd"] = _day(it["start_date"])
    out_ = it[(it["vd"] < it["start_d"] - pd.Timedelta(days=1)) | (it["vd"] > it["end_d"] + pd.Timedelta(days=1))]
    add(c[c["claim_id"].isin(out_["claim_id"])], "visit_outside_period",
        lambda r: f"Visit on {out_[out_['claim_id'] == r.claim_id].iloc[0]['vd'].date()} is outside the claim period {r.start_d.date()} to {r.end_d.date()}",
        lambda r: [f"{r.claim_id}:line{n}" for n in out_[out_["claim_id"] == r.claim_id]["line"]])

    nb = bill[["claim_id", "patient_id", "payer", "created_d"]]
    j = nb.merge(nb, on="patient_id", suffixes=("", "_o"))
    j = j[(j["claim_id"] != j["claim_id_o"]) & ((j["created_d"] - j["created_d_o"]).abs().dt.days <= PAYER_WINDOW_DAYS)]
    g = j.groupby("claim_id").agg(n=("payer_o", "size"), kinds=("payer_o", "nunique"), other=("payer_o", "first"))
    me = nb.set_index("claim_id")["payer"]
    # needs at least two nearby claims that all agree on one payer, and this claim must disagree with them
    flagged = g[(g["n"] >= 2) & (g["kinds"] == 1) & (g["other"] != me.reindex(g.index))]
    add(c[c["claim_id"].isin(flagged.index)], "payer_changed", lambda r: f"Payer is {r.payer}, but the patient's {int(flagged.loc[r.claim_id, 'n'])} other claims within {PAYER_WINDOW_DAYS} days all list {flagged.loc[r.claim_id, 'other']}")
    return pd.DataFrame(out, columns=["claim_id", "rule", "severity", "detail", "evidence_ids"])
