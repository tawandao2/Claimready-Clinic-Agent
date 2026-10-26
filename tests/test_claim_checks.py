import sys, pathlib
import pandas as pd
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "src"))
from claim_checks import check_claims

def claim(cid="c1", pid="p1", payer="Medicare", day="2024-03-01", total=100.0, paid=80.0, ctype="professional", end=None, created=None):
    return {"claim_id": cid, "patient_id": pid, "claim_type": ctype, "payer": payer, "created": created or f"{day}T10:00:00-05:00",
            "period_start": f"{day}T09:00:00-05:00", "period_end": end or f"{day}T10:00:00-05:00", "total": total, "paid_by_payer": paid}

def item(cid="c1", line=1, code="a", net=100.0, enc="e1"):
    return {"claim_id": cid, "line": line, "code": code, "net": net, "encounter_id": enc}

ENC = pd.DataFrame({"id": ["e1", "e2"], "start_date": ["2024-03-01", "2024-03-01"]})

def run(claims, items, dx=None, enc=ENC, cond=("d1",)):
    dx = pd.DataFrame(dx or [{"claim_id": "c1", "seq": 1, "condition_id": "d1"}])
    return check_claims(pd.DataFrame(claims), pd.DataFrame(items), dx, enc, set(cond))

def rules(f, cid=None): return set(f[f.claim_id == cid]["rule"]) if cid else set(f["rule"])

def test_a_clean_claim_has_no_findings():
    assert run([claim()], [item()]).empty

def test_total_must_match_lines():
    assert rules(run([claim(total=150.0)], [item()])) == {"total_mismatch"}
    assert run([claim(total=100.004)], [item()]).empty            # under a cent is rounding, not a mistake

def test_pharmacy_claims_are_exempt_from_the_total_check():
    assert run([claim(ctype="pharmacy")], [item(net=None)]).empty

def test_payment_above_billed_is_flagged():
    assert rules(run([claim(paid=100.5)], [item()])) == {"overpayment"}

def test_late_filing_boundary_is_365_days():
    assert run([claim(end="2024-03-01T10:00:00-05:00", created="2025-03-01T10:00:00-05:00")], [item()]).empty       # 365 days
    f = run([claim(end="2024-03-01T10:00:00-05:00", created="2025-03-02T10:00:00-05:00")], [item()]); assert rules(f) == {"late_filing"}

def test_exact_duplicates_are_both_flagged_and_name_each_other():
    f = run([claim("c1"), claim("c2")], [item("c1"), item("c2")], dx=[{"claim_id": "c1", "seq": 1, "condition_id": "d1"}])
    d = f[f.rule == "duplicate_claim"]; assert set(d.claim_id) == {"c1", "c2"} and "c2" in d[d.claim_id == "c1"].iloc[0]["detail"]

def test_different_services_are_not_duplicates():
    assert "duplicate_claim" not in rules(run([claim("c1"), claim("c2")], [item("c1", code="a"), item("c2", code="b")]))

def test_diagnosis_and_visit_must_exist_in_the_chart():
    f = run([claim()], [item(enc="gone")], dx=[{"claim_id": "c1", "seq": 1, "condition_id": "nope"}])
    assert rules(f) == {"diagnosis_not_in_chart", "visit_not_in_chart"}
    assert "nope" in f[f.rule == "diagnosis_not_in_chart"].iloc[0]["evidence_ids"]

def test_visit_outside_the_period_is_flagged_but_one_day_is_tolerated():
    far = pd.DataFrame({"id": ["e1"], "start_date": ["2024-03-10"]}); near = pd.DataFrame({"id": ["e1"], "start_date": ["2024-03-02"]})
    assert rules(run([claim()], [item()], enc=far)) == {"visit_outside_period"} and run([claim()], [item()], enc=near).empty

def test_late_evening_local_time_is_not_read_as_the_next_day():
    c = claim(day="2024-03-01"); c["period_start"] = "2024-03-01T21:30:00-05:00"; c["period_end"] = "2024-03-01T22:30:00-05:00"
    assert run([c], [item()]).empty      # in UTC this is March 2, but the visit is dated March 1

def test_payer_warning_needs_two_agreeing_claims_and_one_that_disagrees():
    mk = lambda cid, payer, day: claim(cid, payer=payer, day=day)
    cl = [mk("a", "Medicare", "2024-03-01"), mk("b", "Medicare", "2024-03-05"), mk("c", "Aetna", "2024-03-10")]
    it = [item(x, enc=None) for x in "abc"]; dx = [{"claim_id": x, "seq": 1, "condition_id": "d1"} for x in "abc"]
    f = run(cl, it, dx=dx); assert rules(f) == {"payer_changed"} and set(f.claim_id) == {"c"}
    two = run(cl[1:], it[1:], dx=dx[1:]); assert two.empty      # one neighbour only: cannot tell which payer is wrong
