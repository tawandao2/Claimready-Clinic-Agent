"""Reads Synthea FHIR bundles and keeps the claims, their lines, and the visit notes in DuckDB.
Every row keeps the FHIR id it came from, so any finding can point back to a source record."""
import base64, glob, json, os, sys
import duckdb
import pandas as pd

PAY = "line_prvdr_pmt_amt"


def pid(ref):
    return ref.split(":")[-1].split("/")[-1] if ref else None


def parse_bundle(b):
    by_url = {e.get("fullUrl"): e["resource"] for e in b["entry"]}
    claims, items, dx, notes = [], [], [], []
    paid = {}
    for e in b["entry"]:
        r = e["resource"]
        if r["resourceType"] == "ExplanationOfBenefit":
            total = 0.0
            for it in r.get("item", []):
                for a in it.get("adjudication", []):
                    if a["category"]["coding"][0]["code"].endswith(PAY):
                        total += a["amount"]["value"]
            paid[pid(r["claim"]["reference"])] = total
    for e in b["entry"]:
        r = e["resource"]; t = r["resourceType"]
        if t == "Claim":
            cid = r["id"]
            claims.append({"claim_id": cid, "patient_id": pid(r["patient"]["reference"]), "claim_type": r["type"]["coding"][0]["code"],
                           "payer": r["insurance"][0]["coverage"].get("display"), "created": r.get("created"),
                           "period_start": r.get("billablePeriod", {}).get("start"), "period_end": r.get("billablePeriod", {}).get("end"),
                           "provider": r.get("provider", {}).get("display") or r.get("facility", {}).get("display"),
                           "total": r.get("total", {}).get("value"), "paid_by_payer": paid.get(cid)})
            for it in r.get("item", []):
                c = it["productOrService"]["coding"][0]
                items.append({"claim_id": cid, "line": it["sequence"], "code_system": c.get("system"), "code": c.get("code"),
                              "display": c.get("display"), "net": it.get("net", {}).get("value"),
                              "service_start": it.get("servicedPeriod", {}).get("start") or it.get("servicedDate"),
                              "encounter_id": pid(it["encounter"][0]["reference"]) if it.get("encounter") else None,
                              "diagnosis_seqs": ",".join(str(x) for x in it.get("diagnosisSequence", []))})
            for d in r.get("diagnosis", []):
                dx.append({"claim_id": cid, "seq": d["sequence"], "condition_id": pid(d["diagnosisReference"]["reference"])})
        elif t == "DocumentReference":
            a = r["content"][0]["attachment"]
            if "data" in a:
                enc = r.get("context", {}).get("encounter", [{}])[0].get("reference")
                notes.append({"note_id": r["id"], "patient_id": pid(r["subject"]["reference"]), "encounter_id": pid(enc),
                              "date": r.get("date"), "text": base64.b64decode(a["data"]).decode("utf-8", "replace")})
    return claims, items, dx, notes


def load(fhir_dir, out_db, patient_ids=None):
    rows = {"claims": [], "claim_items": [], "claim_diagnoses": [], "notes": []}
    for f in sorted(glob.glob(os.path.join(fhir_dir, "*.json"))):
        b = json.load(open(f))
        if "entry" not in b or not any(e["resource"]["resourceType"] == "Patient" for e in b["entry"]):
            continue
        pat = next(e["resource"]["id"] for e in b["entry"] if e["resource"]["resourceType"] == "Patient")
        if patient_ids is not None and pat not in patient_ids:
            continue
        for k, v in zip(rows, parse_bundle(b)):
            rows[k] += v
    con = duckdb.connect(out_db)
    for k, v in rows.items():
        df = pd.DataFrame(v)
        con.execute(f"create or replace table {k} as select * from df")
    return {k: len(v) for k, v in rows.items()}


if __name__ == "__main__":
    ids = None
    if len(sys.argv) > 3:
        ids = set(duckdb.connect(sys.argv[3], read_only=True).execute("select id from patients").fetchdf()["id"])
    print(load(sys.argv[1], sys.argv[2], ids))
