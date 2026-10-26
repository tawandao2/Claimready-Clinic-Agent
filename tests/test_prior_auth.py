import sys, pathlib, duckdb, pytest
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "src"))
from prior_auth import evaluate_order

ORDER = "2026-10-25"

class Db:
    def __init__(self, path, age=60, sex="female"):
        self.path, self.n = str(path), 0
        c = duckdb.connect(self.path)
        c.execute("create table patients (id varchar, birth_date varchar, gender varchar, deceased_date varchar)")
        c.execute("create table conditions (id varchar, patient_id varchar, code varchar, display varchar, status varchar, onset_date varchar, abatement_date varchar)")
        c.execute("create table observations (id varchar, patient_id varchar, loinc varchar, display varchar, value varchar, unit varchar, effective_date varchar)")
        c.execute("create table medications (id varchar, patient_id varchar, rxnorm varchar, display varchar, status varchar, authored_date varchar)")
        c.execute("create table procedures (id varchar, patient_id varchar, snomed varchar, display varchar, performed_date varchar)")
        c.execute("create table encounters (id varchar, patient_id varchar, code varchar, display varchar, start_date varchar)")
        c.execute("create table notes (note_id varchar, patient_id varchar, encounter_id varchar, date varchar, text varchar)")
        c.execute("insert into patients values ('p1', ?, ?, null)", [f"{2026 - age}-01-01", sex]); c.close()
    def add(self, table, *vals):
        self.n += 1; c = duckdb.connect(self.path)
        c.execute(f"insert into {table} values (?, 'p1', {','.join('?' * len(vals))})", [f"{table[:3]}{self.n}", *vals]); c.close(); return f"{table[:3]}{self.n}"
    def cond(self, code, name, onset, end=None): return self.add("conditions", code, name, "active", onset, end)
    def med(self, name, date, status="completed"): return self.add("medications", "1", name, status, date)
    def proc(self, name, date): return self.add("procedures", "1", name, date)
    def lab(self, loinc, v, date): return self.add("observations", loinc, "x", str(v), "u", date)
    def visit(self, date):
        e = self.add("encounters", "1", "visit", date); c = duckdb.connect(self.path)
        c.execute("insert into notes values (?, 'p1', ?, ?, 'note')", [f"note-{e}", e, date]); c.close(); return e

def run(db, kind, **kw):
    return evaluate_order(db.path, {"patient_id": "p1", "order_type": kind, "order_date": ORDER, **kw})

def st(r): return {c["id"]: c["status"] for c in r["criteria"]}

@pytest.fixture
def db(tmp_path): return Db(tmp_path / "t.duckdb")

# ------------------------------------------------ CGM
def test_cgm_ready_with_evidence(db):
    d = db.cond("44054006", "Diabetes mellitus type 2", "2015-01-01"); i = db.med("insulin glargine", "2025-12-01"); e = db.visit("2026-08-01")
    r = run(db, "CGM"); assert r["decision"] == "ready" and st(r) == {"dx": "met", "insulin": "met", "visit": "met"}
    ev = {c["id"]: c["evidence_ids"] for c in r["criteria"]}
    assert ev["dx"] == [d] and ev["insulin"] == [i] and ev["visit"][0] == e and ev["visit"][1] == f"note-{e}"
    assert r["draft_request"]["requires_clinician_attestation"] and r["clinician_to_confirm"]

def test_cgm_oral_drugs_do_not_count_as_insulin(db):
    db.cond("44054006", "Diabetes mellitus type 2", "2015-01-01"); db.med("metformin 500 MG", "2025-12-01", "active"); db.visit("2026-08-01")
    r = run(db, "CGM"); assert st(r)["insulin"] == "not_met" and r["decision"] == "missing"

def test_cgm_visit_older_than_six_months_fails(db):
    db.cond("44054006", "Diabetes mellitus type 2", "2015-01-01"); db.med("insulin glargine", "2025-12-01"); db.visit("2026-03-01")
    assert st(run(db, "CGM"))["visit"] == "not_met"

def test_records_after_the_order_date_are_invisible(db):
    db.cond("44054006", "Diabetes mellitus type 2", "2015-01-01"); db.visit("2026-08-01"); db.med("insulin glargine", "2026-11-30")
    assert st(run(db, "CGM"))["insulin"] == "not_met"

# ------------------------------------------------ DXA
def test_dxa_needs_23_months_since_last_scan(db):
    db.cond("1", "Osteoporosis (disorder)", "2020-01-01"); db.proc("Bone density scan (procedure)", "2025-03-10")
    r = run(db, "DXA"); assert st(r)["frequency"] == "not_met" and r["decision"] == "missing"
    db2 = Db(db.path + "2", 70); db2.cond("1", "Osteoporosis (disorder)", "2020-01-01"); db2.proc("Bone density scan (procedure)", "2024-09-10")
    assert run(db2, "DXA")["decision"] == "ready"   # September 2024 to October 2026 is 25 months

def test_dxa_older_woman_without_a_reason_goes_to_review_not_approval(tmp_path):
    d = Db(tmp_path / "a.duckdb", 65, "female"); r = run(d, "DXA")
    assert st(r)["qualifying"] == "needs_review" and r["decision"] == "review"
    m = Db(tmp_path / "b.duckdb", 65, "male"); assert st(run(m, "DXA"))["qualifying"] == "not_met"

def test_dxa_long_steroid_use_qualifies(db):
    db.med("predniSONE 5 MG", "2026-01-01"); assert st(run(db, "DXA"))["qualifying"] == "met"

# ------------------------------------------------ GLP-1
def glp(db, a1c=8.2, metformin_date="2026-01-01"):
    db.cond("44054006", "Diabetes mellitus type 2", "2015-01-01"); db.med("metformin 500 MG", metformin_date); db.lab("4548-4", a1c, "2026-09-01")

def test_glp1_metformin_first_variant_ready(db):
    glp(db); r = run(db, "GLP1", policy_variant="metformin_first"); assert r["decision"] == "ready" and "sglt2" not in st(r)

def test_glp1_centene_policy_also_needs_sglt2(db):
    glp(db); r = run(db, "GLP1"); assert st(r)["sglt2"] == "not_met" and r["decision"] == "missing"
    db.med("empagliflozin 10 MG", "2025-06-01"); assert run(db, "GLP1")["decision"] == "ready"

def test_glp1_controlled_hba1c_is_not_a_metformin_failure(db):
    glp(db, a1c=6.2); assert st(run(db, "GLP1", policy_variant="metformin_first"))["metformin"] == "not_met"

def test_glp1_metformin_under_three_months_fails(db):
    glp(db, metformin_date="2026-09-01"); assert st(run(db, "GLP1", policy_variant="metformin_first"))["metformin"] == "not_met"

def test_glp1_type1_and_thyroid_tumor_history_block(db):
    glp(db); db.cond("46635009", "Diabetes mellitus type 1", "2015-01-01"); assert st(run(db, "GLP1", policy_variant="metformin_first"))["dx"] == "not_met"
    d2 = Db(db.path + "2"); glp(d2); d2.cond("1", "Medullary thyroid carcinoma", "2020-01-01")
    assert st(run(d2, "GLP1", policy_variant="metformin_first"))["contra"] == "not_met"

def test_glp1_dose_over_limit_fails(db):
    glp(db); assert st(run(db, "GLP1", policy_variant="metformin_first", dose_mg_per_week=2.4))["dose"] == "not_met"

def test_glp1_low_egfr_goes_to_review(db):
    d = Db(db.path + "2"); d.cond("44054006", "Diabetes mellitus type 2", "2015-01-01"); d.lab("33914-3", 22, "2026-09-01")
    assert st(run(d, "GLP1", policy_variant="metformin_first"))["metformin"] == "needs_review"

# ------------------------------------------------ MRI
def test_mri_pathway_a_pain_plus_old_treatment(db):
    db.cond("1", "Chronic low back pain (finding)", "2026-06-01"); db.proc("Physical therapy procedure (regime/therapy)", "2026-07-01")
    assert run(db, "MRI_LUMBAR")["decision"] == "ready"

def test_mri_new_pain_or_recent_treatment_is_not_enough(db):
    db.cond("1", "Chronic low back pain (finding)", "2026-10-01"); db.proc("Physical therapy procedure (regime/therapy)", "2026-10-10")
    r = run(db, "MRI_LUMBAR"); assert r["decision"] == "missing" and st(r)["pain"] == "not_met"

def test_mri_cancer_history_skips_the_wait_but_a_fracture_needs_review(db):
    db.cond("1", "Malignant neoplasm of colon (disorder)", "2020-01-01"); assert run(db, "MRI_LUMBAR")["decision"] == "ready"
    d = Db(db.path + "2"); d.cond("1", "Fracture of bone (disorder)", "2020-01-01"); assert run(d, "MRI_LUMBAR")["decision"] == "review"
    d3 = Db(db.path + "3"); d3.cond("1", "Fractured dental filling (finding)", "2020-01-01"); assert st(run(d3, "MRI_LUMBAR"))["red_flag"] == "not_met"

def test_mri_resolved_back_pain_does_not_count(db):
    db.cond("1", "Chronic low back pain (finding)", "2025-01-01", end="2025-06-01"); assert st(run(db, "MRI_LUMBAR"))["pain"] == "not_met"

# ------------------------------------------------ refusals
def test_unknown_patient_and_unsupported_order_are_refused(db):
    assert evaluate_order(db.path, {"patient_id": "nope", "order_type": "CGM", "order_date": ORDER})["status"] == "refused"
    assert run(db, "BRAIN_SURGERY")["status"] == "refused"

def test_every_evidence_id_exists_in_the_chart(db):
    db.cond("44054006", "Diabetes mellitus type 2", "2015-01-01"); db.med("insulin glargine", "2025-12-01"); db.visit("2026-08-01")
    c = duckdb.connect(db.path); real = {r[0] for t, k in [("conditions", "id"), ("medications", "id"), ("encounters", "id"), ("notes", "note_id")] for r in c.execute(f"select {k} from {t}").fetchall()}; c.close()
    assert all(e in real for cr in run(db, "CGM")["criteria"] for e in cr["evidence_ids"])
