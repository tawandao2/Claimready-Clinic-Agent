import sys, pathlib, base64
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "src"))
from load_claims import parse_bundle, pid

def bundle():
    pt = "urn:uuid:p1"
    claim = {"resourceType": "Claim", "id": "c1", "patient": {"reference": pt}, "type": {"coding": [{"code": "professional"}]},
             "insurance": [{"coverage": {"display": "Medicare"}}], "created": "2020-01-01", "total": {"value": 100.0},
             "diagnosis": [{"sequence": 1, "diagnosisReference": {"reference": "urn:uuid:cond1"}}],
             "item": [{"sequence": 1, "productOrService": {"coding": [{"system": "s", "code": "1", "display": "Exam"}]}, "net": {"value": 100.0},
                       "diagnosisSequence": [1], "encounter": [{"reference": "urn:uuid:e1"}]},
                      {"sequence": 2, "productOrService": {"coding": [{"system": "s", "code": "2", "display": "Finding"}]}}]}
    eob = {"resourceType": "ExplanationOfBenefit", "claim": {"reference": "urn:uuid:c1"}, "item": [{"adjudication": [
        {"category": {"coding": [{"code": "https://x/variables/line_prvdr_pmt_amt"}]}, "amount": {"value": 80.0}}]}]}
    doc = {"resourceType": "DocumentReference", "id": "d1", "subject": {"reference": pt}, "date": "2020-01-01",
           "context": {"encounter": [{"reference": "urn:uuid:e1"}]},
           "content": [{"attachment": {"data": base64.b64encode(b"Chief Complaint: thirst").decode()}}]}
    return {"entry": [{"fullUrl": "urn:uuid:c1", "resource": r} for r in (claim, eob, doc)]}

def test_pid():
    assert pid("urn:uuid:abc") == "abc" and pid("Patient/xyz") == "xyz" and pid(None) is None

def test_claim_lines_payment_and_notes_are_parsed():
    claims, items, dx, notes = parse_bundle(bundle())
    assert claims[0]["patient_id"] == "p1" and claims[0]["payer"] == "Medicare" and claims[0]["paid_by_payer"] == 80.0
    assert len(items) == 2 and items[0]["encounter_id"] == "e1" and items[1]["net"] is None and items[0]["diagnosis_seqs"] == "1"
    assert dx[0]["condition_id"] == "cond1" and notes[0]["encounter_id"] == "e1" and "thirst" in notes[0]["text"]
