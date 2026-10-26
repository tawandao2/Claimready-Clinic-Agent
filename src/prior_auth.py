"""Prior authorization rules. Plain code decides every criterion; nothing here calls an AI.
Each criterion is met, not_met or needs_review, and points at the chart records that show it.
Criteria are simplified from the public sources named in POLICIES. Real payers differ."""
import re
import duckdb
import pandas as pd

POLICIES = {
    "CGM": {"order": "Continuous glucose monitor", "source": "Medicare LCD L33822, Glucose Monitors (revision for services on or after 10/01/2024)",
            "url": "https://www.cms.gov/medicare-coverage-database/details/lcd-details.aspx?LCDId=33822"},
    "DXA": {"order": "Bone density scan (DXA)", "source": "Medicare 42 CFR 410.31, Bone mass measurement",
            "url": "https://www.law.cornell.edu/cfr/text/42/410.31"},
    "GLP1": {"order": "Semaglutide for type 2 diabetes", "source": "Centene clinical policy CP.PMN.183, GLP-1 receptor agonists (last review 02.22). One insurer's policy, used as an example",
             "url": "https://www.superiorhealthplan.com/content/dam/centene/Superior/policies/pharmacy-policies/CP.PMN.183-07012022.pdf"},
    "MRI_LUMBAR": {"order": "Lumbar spine MRI", "source": "Missouri Medicaid MRI of Lumbar Spine PA criteria (2009) and ACR guidance on low back pain imaging",
                   "url": "https://dss.mo.gov/mhd/cs/medprecert/pdf/mrilumbarspine.pdf"},
}
T2DM, T1DM = "44054006", "46635009"


def _rx(p):
    return re.sub(r"\((?!\?)", "(?:", p)  # groups in these patterns only group, they never capture


def _d(s):
    return pd.to_datetime(s, errors="coerce", utc=True).dt.tz_convert(None).dt.normalize()


class Chart:
    """One patient's record as of the order date. Nothing dated after the order is visible (no peeking at the future)."""
    def __init__(self, db, patient_id, as_of):
        self.pid, self.as_of = patient_id, pd.Timestamp(as_of)
        con = duckdb.connect(db, read_only=True)
        q = lambda t: con.execute(f"select * from {t} where patient_id = ?", [patient_id]).fetchdf()
        pat = con.execute("select * from patients where id = ?", [patient_id]).fetchdf()
        if pat.empty:
            raise KeyError(patient_id)
        self.sex = pat.iloc[0]["gender"]
        self.age = int((self.as_of - pd.Timestamp(pat.iloc[0]["birth_date"])).days // 365.25)
        self.cond, self.med, self.proc, self.obs, self.enc, self.note = (q(t) for t in ("conditions", "medications", "procedures", "observations", "encounters", "notes"))
        self.cond["date"] = _d(self.cond["onset_date"]); self.cond["end"] = _d(self.cond["abatement_date"])
        self.med["date"] = _d(self.med["authored_date"]); self.proc["date"] = _d(self.proc["performed_date"])
        self.obs["date"] = _d(self.obs["effective_date"]); self.enc["date"] = _d(self.enc["start_date"])
        self.cond, self.med, self.proc, self.obs, self.enc = (x[x["date"] <= self.as_of] for x in (self.cond, self.med, self.proc, self.obs, self.enc))
        con.close()

    def conds(self, pat=None, code=None):
        c = self.cond
        if code: c = c[c["code"].astype(str).isin(code if isinstance(code, (list, set, tuple)) else [code])]
        if pat: c = c[c["display"].str.contains(_rx(pat), case=False, regex=True)]
        return c.sort_values("date")

    def meds(self, pat):
        return self.med[self.med["display"].str.contains(_rx(pat), case=False, regex=True)].sort_values("date")

    def procs(self, pat):
        return self.proc[self.proc["display"].str.contains(_rx(pat), case=False, regex=True)].sort_values("date")

    def labs(self, loinc):
        return self.obs[self.obs["loinc"] == loinc].sort_values("date")

    def current(self, meds, days=365):
        """Medications that count as current: marked active, or started within `days` before the order."""
        return meds[(meds["status"] == "active") | (meds["date"] >= self.as_of - pd.Timedelta(days=days))]


def C(cid, text, status, detail, evidence=()):
    return {"id": cid, "text": text, "status": status, "detail": detail, "evidence_ids": list(evidence)}


def fmt(t): return t.strftime("%Y-%m-%d")


def ids(df): return [str(x) for x in df["id"]]


def month_idx(t): return t.year * 12 + t.month


# ---------------------------------------------------------------- CGM
def eval_cgm(ch, order):
    out = []
    dx = ch.conds(code=[T2DM, T1DM])
    out.append(C("dx", "Diabetes diagnosis", "met" if len(dx) else "not_met",
                 f"{dx.iloc[0]['display']} since {fmt(dx.iloc[0]['date'])}" if len(dx) else "No diabetes diagnosis on record", ids(dx.head(1))))
    ins = ch.current(ch.meds("insulin"))
    out.append(C("insulin", "Treated with insulin (oral medicines do not count)", "met" if len(ins) else "not_met",
                 f"Insulin on record: {ins.iloc[0]['display']} started {fmt(ins.iloc[0]['date'])}" if len(ins) else
                 "No current insulin. The other route, problematic hypoglycemia, is not shown in this record and would need clinician documentation", ids(ins.head(2))))
    since = ch.as_of - pd.Timedelta(days=183)
    enc = ch.enc[ch.enc["date"] >= since].sort_values("date")
    note = ch.note[ch.note["encounter_id"].isin(enc["id"])] if len(enc) else ch.note.iloc[0:0]
    note_ids = lambda n: [str(x) for x in n["note_id"]]
    last = enc.iloc[-1] if len(enc) else None
    out.append(C("visit", "Visit within 6 months before the order to review diabetes control", "met" if last is not None else "not_met",
                 f"Most recent visit {fmt(last['date'])}" if last is not None else "No visit in the 6 months before the order",
                 ([str(last["id"])] + note_ids(note[note["encounter_id"] == last["id"]].head(1))) if last is not None else []))
    return out, ["Clinician confirms the patient or caregiver has enough training to use the monitor (shown by the prescription)",
                 "Monitor is prescribed within its FDA labeled use"]


# ---------------------------------------------------------------- DXA
def eval_dxa(ch, order):
    out = []
    last = ch.procs("bone density")
    if len(last):
        gap = month_idx(ch.as_of) - month_idx(last.iloc[-1]["date"])
        out.append(C("frequency", "At least 23 months since the last bone density scan", "met" if gap >= 23 else "not_met",
                     f"Last scan {fmt(last.iloc[-1]['date'])}, {gap} months ago", ids(last.tail(1))))
    else:
        out.append(C("frequency", "At least 23 months since the last bone density scan", "met", "No earlier scan on record"))
    osteo = ch.conds(pat="osteopor")
    steroid = ch.meds("predniso|methylpredniso|dexamethasone")
    steroid = steroid[steroid["date"] <= ch.as_of - pd.Timedelta(days=90)]
    drug = ch.current(ch.meds("alendron|risedron|ibandron|zoledron|raloxifene|denosumab"))
    reasons = []
    if len(osteo): reasons.append((f"Osteoporosis diagnosed {fmt(osteo.iloc[0]['date'])} (stands in for vertebral findings on x-ray)", ids(osteo.head(1))))
    if len(steroid): reasons.append((f"Steroid medicine started {fmt(steroid.iloc[0]['date'])}, over 3 months before the order", ids(steroid.head(1))))
    if len(drug): reasons.append((f"Osteoporosis medicine on record: {drug.iloc[0]['display']}", ids(drug.head(1))))
    if reasons:
        out.append(C("qualifying", "Qualifying reason (osteoporosis, long steroid use, or monitoring osteoporosis treatment)", "met", reasons[0][0], reasons[0][1]))
    elif ch.sex == "female" and ch.age >= 51:
        out.append(C("qualifying", "Qualifying reason (osteoporosis, long steroid use, or monitoring osteoporosis treatment)", "needs_review",
                     "No qualifying record. The estrogen deficient woman route needs the clinician to document risk"))
    else:
        out.append(C("qualifying", "Qualifying reason (osteoporosis, long steroid use, or monitoring osteoporosis treatment)", "not_met", "No qualifying record"))
    return out, ["Clinician confirms the scan is medically necessary and ordered after evaluation"]


# ---------------------------------------------------------------- GLP-1
def eval_glp1(ch, order):
    """policy_variant "centene" follows the cited policy, including its SGLT2 step. "metformin_first" is a simpler plan that only asks for metformin first."""
    out = []
    out.append(C("age", "Age 18 or older", "met" if ch.age >= 18 else "not_met", f"Age {ch.age}"))
    t2, t1 = ch.conds(code=T2DM), ch.conds(code=T1DM)
    out.append(C("dx", "Type 2 diabetes, and no type 1 diabetes", "met" if len(t2) and not len(t1) else "not_met",
                 "Type 1 diabetes on record, which this drug is not approved for" if len(t1) else
                 (f"Type 2 diabetes since {fmt(t2.iloc[0]['date'])}" if len(t2) else "No type 2 diabetes diagnosis"), ids(pd.concat([t2.head(1), t1.head(1)]))))
    met = ch.meds("metformin"); a1c = ch.labs("4548-4")
    last_a1c = a1c.iloc[-1] if len(a1c) else None
    egfr = ch.labs("33914-3")
    long_enough = len(met) and met.iloc[0]["date"] <= ch.as_of - pd.Timedelta(days=90)
    if len(met) and long_enough and last_a1c is not None and float(last_a1c["value"]) >= 7.0:
        out.append(C("metformin", "At least 3 months of metformin and HbA1c still 7% or higher", "met",
                     f"Metformin since {fmt(met.iloc[0]['date'])}; latest HbA1c {float(last_a1c['value'])}% on {fmt(last_a1c['date'])}", ids(met.head(1)) + [str(last_a1c["id"])]))
    elif len(egfr) and float(egfr.iloc[-1]["value"]) < 30:
        out.append(C("metformin", "At least 3 months of metformin and HbA1c still 7% or higher", "needs_review",
                     f"eGFR {float(egfr.iloc[-1]['value'])} on {fmt(egfr.iloc[-1]['date'])} may rule out metformin. Clinician decides", [str(egfr.iloc[-1]["id"])]))
    elif not len(met):
        out.append(C("metformin", "At least 3 months of metformin and HbA1c still 7% or higher", "not_met", "No metformin on record"))
    elif not long_enough:
        out.append(C("metformin", "At least 3 months of metformin and HbA1c still 7% or higher", "not_met",
                     f"Metformin started {fmt(met.iloc[0]['date'])}, under 3 months before the order", ids(met.head(1))))
    else:
        out.append(C("metformin", "At least 3 months of metformin and HbA1c still 7% or higher", "not_met",
                     f"Latest HbA1c {float(last_a1c['value'])}% is under 7%" if last_a1c is not None else "No HbA1c on record", [str(last_a1c["id"])] if last_a1c is not None else []))
    if order.get("policy_variant", "centene") == "centene":
        sg = ch.meds("gliflozin")
        ok = len(sg) and sg.iloc[0]["date"] <= ch.as_of - pd.Timedelta(days=90)
        out.append(C("sglt2", "At least 3 months of an SGLT2 inhibitor (this policy's step)", "met" if ok else "not_met",
                     f"{sg.iloc[0]['display']} since {fmt(sg.iloc[0]['date'])}" if ok else "No SGLT2 inhibitor trial of 3 months on record", ids(sg.head(1)) if ok else []))
    bad = ch.conds(pat="medullary thyroid|multiple endocrine neoplasia"); pan = ch.conds(pat="pancreatitis")
    out.append(C("contra", "No thyroid C cell tumor history and no pancreatitis", "not_met" if len(bad) else ("needs_review" if len(pan) else "met"),
                 "Thyroid tumor history is a contraindication" if len(bad) else ("History of pancreatitis, clinician reviews" if len(pan) else "None on record"), ids(pd.concat([bad.head(1), pan.head(1)]))))
    dose = order.get("dose_mg_per_week", 0.5)
    out.append(C("dose", "Dose at or under 2 mg per week", "met" if dose <= 2 else "not_met", f"Ordered {dose} mg per week"))
    return out, ["Clinician confirms the drug is used within its FDA approved indication"]


# ---------------------------------------------------------------- MRI
def eval_mri(ch, order):
    out = []
    bp = ch.conds(pat="low back pain")
    bp = bp[(bp["end"].isna()) | (bp["end"] > ch.as_of)]
    first = bp.iloc[0]["date"] if len(bp) else None
    lasting = first is not None and (ch.as_of - first).days >= 42
    out.append(C("pain", "Low back pain for more than 6 weeks", "met" if lasting else "not_met",
                 f"Low back pain since {fmt(first)} ({(ch.as_of - first).days} days)" if first is not None else "No low back pain diagnosis", ids(bp.head(1))))
    yr = ch.as_of - pd.Timedelta(days=365)
    pt = ch.procs("physical therapy|chiropract"); pt = pt[pt["date"] >= yr]
    drugs = ch.meds("ibuprofen|naproxen|meloxicam|diclofenac|cyclobenzaprine|methocarbamol"); drugs = drugs[drugs["date"] >= yr]
    th = pd.concat([pt.assign(kind=pt["display"]), drugs.assign(kind=drugs["display"])]).sort_values("date")
    old = len(th) and th.iloc[0]["date"] <= ch.as_of - pd.Timedelta(days=42)
    out.append(C("therapy", "More than 6 weeks of treatment (therapy or anti inflammatory medicine) without enough relief", "met" if (old and lasting) else "not_met",
                 f"First treatment on record: {th.iloc[0]['kind']} on {fmt(th.iloc[0]['date'])}; pain still active" if (old and lasting) else
                 "No treatment 6 weeks or older on record within the last year" if not old else "Treatment on record but pain not documented as ongoing",
                 ids(th.head(1)) if old else []))
    mal = ch.conds(pat="malignant neoplasm|carcinoma"); fx = ch.conds(pat="fracture of (?!tooth)(?!dental)|^fracture of bone|fracture of (bone|spine|vertebra)")
    fx = fx[~fx["display"].str.contains("dental|filling", case=False)]
    out.append(C("red_flag", "Red flag that allows the scan without the 6 week wait (cancer history, fracture)",
                 "met" if len(mal) else ("needs_review" if len(fx) else "not_met"),
                 f"{mal.iloc[0]['display']}" if len(mal) else (f"{fx.iloc[0]['display']} on record. Clinician decides if it involves the spine" if len(fx) else "None on record"),
                 ids(mal.head(1)) if len(mal) else ids(fx.head(1))))
    return out, ["Clinician documents any new nerve symptoms such as weakness, numbness or bladder or bowel changes, which the record does not capture"]


EVAL = {"CGM": eval_cgm, "DXA": eval_dxa, "GLP1": eval_glp1, "MRI_LUMBAR": eval_mri}


def decide(kind, crit):
    s = {c["id"]: c["status"] for c in crit}
    if kind == "MRI_LUMBAR":
        if s["red_flag"] == "met" or (s["pain"] == "met" and s["therapy"] == "met"): return "ready"
        return "review" if s["red_flag"] == "needs_review" else "missing"
    if any(v == "not_met" for v in s.values()): return "missing"
    return "review" if any(v == "needs_review" for v in s.values()) else "ready"


def evaluate_order(db, order):
    kind = order["order_type"]
    if kind not in EVAL:
        return {"status": "refused", "reason": f"unsupported order type: {kind}"}
    try:
        ch = Chart(db, order["patient_id"], order["order_date"])
    except KeyError:
        return {"status": "refused", "reason": "unknown patient"}
    crit, confirm = EVAL[kind](ch, order)
    return {"status": "evaluated", "order": order, "policy": POLICIES[kind], "criteria": crit, "decision": decide(kind, crit),
            "clinician_to_confirm": confirm, "draft_request": {"status": "draft", "requires_clinician_attestation": True}}
