"""The human sign-off. Nothing leaves the app until a named person decides, and every decision is logged.
Approved requests are written as JSON files to a local folder that stands in for a payer portal or billing system."""
import json, pathlib, datetime as dt

DECISIONS = {"approved", "returned", "dismissed"}


def _now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def record(root, kind, subject_id, decision, reviewer, note="", payload=None):
    if not str(reviewer).strip():
        raise ValueError("a named reviewer is required")
    if decision not in DECISIONS:
        raise ValueError(f"decision must be one of {sorted(DECISIONS)}")
    root = pathlib.Path(root); root.mkdir(parents=True, exist_ok=True)
    entry = {"time": _now(), "kind": kind, "subject_id": subject_id, "decision": decision, "reviewer": reviewer.strip(), "note": note}
    if decision == "approved" and payload is not None:
        f = root / f"{kind}_{subject_id[:12]}_{dt.datetime.now().strftime('%H%M%S%f')}.json"
        f.write_text(json.dumps({"status": "attested_draft", "attestation": {"reviewer": reviewer.strip(), "time": entry["time"],
                                 "statement": "I reviewed the findings and the source records and take responsibility for this."}, **payload}, indent=2, default=str))
        entry["file"] = f.name
    with open(root / "audit_log.jsonl", "a") as a:
        a.write(json.dumps(entry) + "\n")
    return entry


def read_audit(root):
    p = pathlib.Path(root) / "audit_log.jsonl"
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []
