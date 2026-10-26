"""Tests the note checker itself. Takes valid notes (from the rules only writer) for every approval request and every
flagged demo claim, damages them in known ways, and counts how many damaged notes the checker catches."""
import sys, re, copy, json, collections
import duckdb, pandas as pd
import assistant as A, claim_checks as cc, prior_auth as pa, evaluate_claims as ev


def mutations(rep, bundle):
    S, must = rep["statements"], {f["id"] for f in bundle["findings"] if f["must_cover"]}
    mk = lambda st: A.Report.model_validate({"statements": st})
    out = []
    for i, s in enumerate(S):
        m = re.search(r"\d[\d,]*(?:\.\d+)?", s["text"])
        if m:
            raw = m.group(0).rstrip("."); dec = len(raw.split(".")[1]) if "." in raw else 0
            c = copy.deepcopy(S); c[i]["text"] = s["text"].replace(raw, f"{float(raw.replace(',', '')) + 7:.{dec}f}", 1); out.append(("changed a number", mk(c)))
        c = copy.deepcopy(S); c[i]["finding_ids"] = ["made_up_id"]; out.append(("invented finding id", mk(c)))
        c = copy.deepcopy(S); c[i]["text"] += " The insurer will pay this claim."; out.append(("promised an outcome", mk(c)))
        c = copy.deepcopy(S); c[i]["text"] += " It is safe to submit now."; out.append(("told someone to submit", mk(c)))
        c = copy.deepcopy(S); c[i]["text"] += " The patient also owes 4,000 dollars."; out.append(("invented extra claim", mk(c)))
        others = [f["id"] for f in bundle["findings"] if f["id"] not in s["finding_ids"]]
        if others:
            c = copy.deepcopy(S); c[i]["finding_ids"] = [others[0]]; out.append(("cited the wrong finding", mk(c)))
        c = copy.deepcopy(S); del c[i]
        if set(s["finding_ids"]) & must and not any(set(x["finding_ids"]) & set(s["finding_ids"]) for x in c): out.append(("dropped a required finding", mk(c)))
    return out


def main(db, demo, out_path):
    c = duckdb.connect(db, read_only=True); pts = c.execute("select id from patients").fetchdf()["id"].tolist(); c.close()
    bundles = []
    for k in pa.EVAL:
        for p in pts:
            bundles.append(A.order_bundle(pa.evaluate_order(db, {"patient_id": p, "order_type": k, "order_date": "2026-10-25"})))
    cl, it, dg, enc, cond, truth = None, None, None, None, None, None
    import importlib; app_data = None
    q = lambda d, s: duckdb.connect(d, read_only=True).execute(s).fetchdf()
    cl, it, dg = (q(demo, f"select * from {t}") for t in ("claims", "claim_items", "claim_diagnoses"))
    enc, cond = q(db, "select id, start_date from encounters"), set(q(db, "select id from conditions")["id"])
    F = cc.check_claims(cl, it, dg, enc, cond)
    for cid in F["claim_id"].unique(): bundles.append(A.claim_bundle(cl, F, cid))
    res, base_ok, n_notes = collections.defaultdict(lambda: [0, 0]), 0, 0
    for b in bundles:
        base = A.write_note(b, A.RulesOnlyWriter()); n_notes += 1; base_ok += base["status"] == "ready_for_review"
        if base["status"] != "ready_for_review": continue
        for kind, rep in mutations(base, b):
            r = res[kind]; r[1] += 1; r[0] += bool(A.validate(rep, b))
    summary = {"valid_notes": n_notes, "valid_notes_passed": base_ok, "kinds": {k: {"caught": v[0], "tried": v[1]} for k, v in res.items()}}
    summary["total_caught"] = sum(v[0] for v in res.values()); summary["total_tried"] = sum(v[1] for v in res.values())
    json.dump(summary, open(out_path, "w"), indent=2); return summary


if __name__ == "__main__":
    print(json.dumps(main(*sys.argv[1:4]), indent=1))
