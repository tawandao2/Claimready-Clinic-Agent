import sys, pathlib, json, pytest
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "src"))
from outbox import record, read_audit

def test_approval_writes_a_file_with_the_attestation_and_a_log_line(tmp_path):
    e = record(tmp_path, "prior_auth", "abc123456789xyz", "approved", " Dr. Rao ", payload={"criteria": []})
    f = json.loads((tmp_path / e["file"]).read_text())
    assert f["attestation"]["reviewer"] == "Dr. Rao" and f["status"] == "attested_draft" and len(read_audit(tmp_path)) == 1

def test_returning_or_dismissing_logs_but_writes_no_request(tmp_path):
    record(tmp_path, "claim", "c1", "returned", "Pat", note="fix total", payload={"x": 1}); record(tmp_path, "claim", "c2", "dismissed", "Pat")
    assert [p.name for p in tmp_path.iterdir()] == ["audit_log.jsonl"] and [x["decision"] for x in read_audit(tmp_path)] == ["returned", "dismissed"]

def test_a_named_reviewer_and_a_valid_decision_are_required(tmp_path):
    with pytest.raises(ValueError): record(tmp_path, "claim", "c1", "approved", "  ")
    with pytest.raises(ValueError): record(tmp_path, "claim", "c1", "sent_it", "Pat")
    assert read_audit(tmp_path) == []

def test_the_log_only_grows(tmp_path):
    for i in range(3): record(tmp_path, "claim", f"c{i}", "dismissed", "Pat")
    assert len(read_audit(tmp_path)) == 3
