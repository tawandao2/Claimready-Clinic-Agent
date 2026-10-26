import json, os, pathlib, sys
import duckdb
import pandas as pd
import streamlit as st

ROOT = pathlib.Path(__file__).parent
sys.path.insert(0, str(ROOT / "src"))
import assistant as A, claim_checks as cc, outbox, prior_auth as pa

DB, DEMO, OUT = str(ROOT / "data" / "claimready.duckdb"), str(ROOT / "data" / "planted_demo.duckdb"), ROOT / "outbox"
ORDER_DATE = "2026-10-25"
st.set_page_config(page_title="ClaimReady", layout="wide")


# ----------------------------------------------------------------- data
@st.cache_data
def q(sql, params=(), db=DB):
    con = duckdb.connect(db, read_only=True)
    try:
        return con.execute(sql, list(params)).fetchdf()
    finally:
        con.close()


@st.cache_data
def patients():
    p = q("select id, birth_date, gender from patients")
    p["age"] = ((pd.Timestamp(ORDER_DATE) - pd.to_datetime(p["birth_date"])).dt.days // 365.25).astype(int)
    p["label"] = p["id"].str[:8] + "  |  " + p["age"].astype(str) + " years, " + p["gender"]
    return p.sort_values("label").reset_index(drop=True)


@st.cache_data
def claim_data(source):
    if source == "damaged":
        cl, it, dg = (q(f"select * from {t}", db=DEMO) for t in ("claims", "claim_items", "claim_diagnoses"))
        truth = q("select * from planted_truth", db=DEMO)
        pids = tuple(cl["patient_id"].unique())
    else:
        cl, it, dg = (q(f"select * from {t}") for t in ("claims", "claim_items", "claim_diagnoses"))
        truth, pids = None, None
    enc = q("select id, start_date from encounters")
    cond = set(q("select id from conditions")["id"])
    return cl, it, dg, enc, cond, truth


@st.cache_data
def run_checks(source):
    cl, it, dg, enc, cond, _ = claim_data(source)
    return cc.check_claims(cl, it, dg, enc, cond)


def lookup(rid):
    """Finds a record by id in the chart or notes so evidence can be shown, not just cited."""
    for t, key in (("conditions", "id"), ("medications", "id"), ("procedures", "id"), ("observations", "id"), ("encounters", "id"), ("notes", "note_id")):
        r = q(f"select * from {t} where {key} = ?", (rid,))
        if len(r):
            return t, r.iloc[0].to_dict()
    return None, None


def writer_from(choice):
    if choice.startswith("Gemini"):
        try:
            return A.GeminiWriter()
        except Exception as e:
            st.sidebar.error(f"Gemini is not available ({str(e)[:80]}). Using the rules only writer.")
    return A.RulesOnlyWriter()


def show_note(bundle, writer):
    note = A.write_note(bundle, writer)
    if note["status"] == "ready_for_review":
        for s in note["statements"]:
            st.markdown(f"* {s['text']}  \n  <span style='color:gray;font-size:0.8em'>sources: {', '.join(s['finding_ids'])}</span>", unsafe_allow_html=True)
    else:
        st.error("The note was rejected by the checker, so nobody sees it: " + "; ".join(note["problems"]) if note["problems"] else "Nothing to write about.")
    return note


# ----------------------------------------------------------------- sidebar
with st.sidebar:
    st.header("Reviewer")
    reviewer = st.text_input("Your name", help="Every decision is logged under this name.")
    st.header("Note writer")
    have_key = bool(os.environ.get("GEMINI_API_KEY"))
    choice = st.radio("Who writes the plain English note", ["Rules only (no AI)", "Gemini" + ("" if have_key else " (no key set)")], index=0)
    st.caption("Either way, the same checker reviews the note. Rules decide every finding. The writer only describes them.")
    st.divider()
    st.caption("Synthetic patients and claims. Nothing here is sent to a real payer. Approvals write a file to a local folder.")
writer = writer_from(choice)

st.title("ClaimReady")
st.caption("Check the paperwork before the procedure and again before the bill. Rules decide, the AI only writes, a person signs off.")
t1, t2, t3, t4 = st.tabs(["Before the order", "Before the bill", "Scorecard", "Audit log"])

# ----------------------------------------------------------------- tab 1: prior authorization
with t1:
    P = patients()
    st.caption("The demo opens on patient 2503f21b, whose glucose monitor request is ready. Pick any other patient or order to see missing criteria. To search the list, clear the box first, then type.")
    start = P.index[P["id"].str.startswith("2503f21b")]
    c1, c2, c3 = st.columns([2, 2, 2])
    label = c1.selectbox("Patient", P["label"], index=int(start[0]) if len(start) else 0)
    pid = P.loc[P["label"] == label, "id"].iloc[0]
    kind = c2.selectbox("Order", list(pa.POLICIES), format_func=lambda k: pa.POLICIES[k]["order"])
    variant = None
    if kind == "GLP1":
        variant = c3.radio("Insurer's plan", ["centene", "metformin_first"], format_func=lambda v: {"centene": "Centene policy (needs metformin and an SGLT2 inhibitor)", "metformin_first": "Simpler plan (metformin first only)"}[v])
    order = {"patient_id": pid, "order_type": kind, "order_date": ORDER_DATE, **({"policy_variant": variant} if variant else {})}
    r = pa.evaluate_order(DB, order)
    st.markdown(f"**Rule:** {r['policy']['source']}  \n[Read the source]({r['policy']['url']})")
    if r["decision"] == "ready":
        st.success("Every criterion is met. Ready for clinician review.")
    elif r["decision"] == "review":
        st.warning("Some criteria need the clinician's judgment.")
    else:
        st.error("Criteria are missing. This request would likely be sent back.")
    icon = {"met": ":green[met]", "not_met": ":red[not met]", "needs_review": ":orange[needs review]"}
    for c in r["criteria"]:
        with st.expander(f"{icon[c['status']]}  {c['text']}", expanded=c["status"] != "met"):
            st.write(c["detail"])
            for rid in c["evidence_ids"]:
                tbl, rec = lookup(rid)
                if rec:
                    shown = {k: v for k, v in rec.items() if k not in ("patient_id", "text")}
                    st.caption(f"Source record, {tbl} ({rid})"); st.json(shown, expanded=False)
                    if tbl == "notes": st.text(rec["text"][:600])
    if r["clinician_to_confirm"]:
        st.markdown("**Only the clinician can confirm:**")
        for x in r["clinician_to_confirm"]:
            st.markdown(f"* {x}")
    st.subheader("Plain English note")
    bundle = A.order_bundle(r); note = show_note(bundle, writer)
    st.subheader("Clinician sign-off")
    if r["decision"] == "missing":
        st.info("Fix the missing criteria first. Approval is switched off until nothing is missing.")
    attest = st.checkbox("I reviewed the findings and the source records and take responsibility for this request.", key="attest_pa")
    b1, b2 = st.columns(2)
    ready = bool(reviewer.strip()) and attest and note["status"] == "ready_for_review"
    if b1.button("Approve draft request", disabled=not ready or r["decision"] == "missing", key="pa_ok"):
        e = outbox.record(OUT, "prior_auth", pid, "approved", reviewer, payload={"order": order, "policy": r["policy"], "decision": r["decision"], "criteria": r["criteria"], "note": note["statements"]})
        st.success(f"Written to the local outbox as {e['file']}. Nothing was sent to any payer.")
    if b2.button("Return to the care team", disabled=not reviewer.strip(), key="pa_back"):
        outbox.record(OUT, "prior_auth", pid, "returned", reviewer, note="returned for more documentation"); st.info("Returned and logged.")
    if not reviewer.strip(): st.caption("Enter your name in the sidebar to sign off.")

# ----------------------------------------------------------------- tab 2: claim check
with t2:
    source = st.radio("Claims to review", ["damaged", "clean"], horizontal=True,
                      format_func=lambda s: {"damaged": "Demo set with planted mistakes (12 patients)", "clean": "Your clean claims (all 82 patients)"}[s])
    cl, it, dg, enc, cond, truth = claim_data(source); F = run_checks(source)
    st.write(f"**{len(cl):,} claims checked. {F['claim_id'].nunique():,} have at least one finding.**")
    if source == "clean":
        st.info("The unaltered claims are almost all fine, which is the point: the checks should stay quiet on good claims. The few warnings are legitimate coverage changes that a person confirms.")
    sev = st.multiselect("Severity", ["error", "warning"], default=["error", "warning"])
    rules = st.multiselect("Rules", list(cc.RULES), default=list(cc.RULES))
    Q = F[F["severity"].isin(sev) & F["rule"].isin(rules)].merge(cl[["claim_id", "claim_type", "payer", "total"]], on="claim_id")
    if truth is not None and st.checkbox("Show what was planted (the answer key)"):
        tk = truth.assign(planted=truth["rule"] + ": " + truth["case"])[["claim_id", "planted"]].drop_duplicates("claim_id")
        Q = Q.merge(tk, on="claim_id", how="left")
    Q = Q.assign(claim=Q["claim_id"].str[:12])
    show = Q[[c for c in ["claim", "claim_type", "payer", "total", "rule", "severity", "detail", "planted"] if c in Q.columns]]
    st.dataframe(show, hide_index=True, width="stretch", height=260)
    ids = list(Q["claim_id"].unique())
    if ids:
        cid = st.selectbox("Open a claim", ids, format_func=lambda x: x[:12] + "  |  " + ", ".join(sorted(set(Q[Q.claim_id == x]["rule"]))))
        c = cl[cl.claim_id == cid].iloc[0]
        m1, m2, m3, m4 = st.columns(4); m1.metric("Billed total", f"{c.total:,.2f}"); m2.metric("Payment recorded", f"{c.paid_by_payer:,.2f}"); m3.metric("Payer", c.payer); m4.metric("Type", c.claim_type)
        st.caption("Claim lines"); st.dataframe(it[it.claim_id == cid][["line", "code", "display", "net", "encounter_id"]], hide_index=True, width="stretch")
        mine = F[F.claim_id == cid]
        for _, f in mine.iterrows():
            (st.error if f.severity == "error" else st.warning)(f"{cc.RULES[f.rule][1]}  \n{f.detail}")
        st.subheader("Plain English note"); note = show_note(A.claim_bundle(cl, F, cid), writer)
        st.subheader("Billing review")
        why = st.text_input("Note for the log (optional)", key=f"why_{cid}")
        has_err = (mine.severity == "error").any()
        a1, a2, a3 = st.columns(3)
        if a1.button("Clear for submission", disabled=has_err or not reviewer.strip(), key=f"ok_{cid}", help="Switched off while there is an error finding"):
            outbox.record(OUT, "claim", cid, "approved", reviewer, note=why, payload={"claim_id": cid, "findings": mine.to_dict("records"), "note": note["statements"]}); st.success("Cleared and logged. Nothing was sent to a payer.")
        if a2.button("Return to fix", disabled=not reviewer.strip(), key=f"back_{cid}"):
            outbox.record(OUT, "claim", cid, "returned", reviewer, note=why); st.info("Returned and logged.")
        if a3.button("Dismiss as false alarm", disabled=not reviewer.strip(), key=f"dis_{cid}"):
            outbox.record(OUT, "claim", cid, "dismissed", reviewer, note=why); st.info("Dismissed and logged.")

# ----------------------------------------------------------------- tab 3: scorecard
with t3:
    st.subheader("Claim checks")
    ev = json.load(open(ROOT / "eval" / "results_claims.json"))
    cr = ev["clean_run"]
    st.write(f"On the {cr['claims']:,} untouched claims the checks raised **{cr['error_flags']} errors** and **{cr['warning_flags']} warnings**.")
    rows = [{"Mistake planted": k.split(" | ")[0].replace("_", " "), "How": k.split(" | ")[1], "Planted": v["planted"], "Caught": v["caught"]} for k, v in ev["planted"].items()]
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch", height=38 + 35 * len(rows))
    st.write("**Look-alikes that should NOT be flagged** (harmless variations)")
    st.dataframe(pd.DataFrame([{"Case": k.split(" | ")[0].replace("_", " ") + ": " + k.split(" | ")[1], "Tried": v["tried"], "Wrongly flagged": v["wrongly_flagged"]} for k, v in ev["negative_controls"].items()]), hide_index=True, width="stretch")
    st.caption("These mistakes are exact and the rules were built to find them, so catching all of them is expected. It shows the checks work as written. It does not show how many real denials they would prevent.")
    st.subheader("Approval request rules")
    pr = json.load(open(ROOT / "eval" / "results_prior_auth.json"))
    st.dataframe(pd.DataFrame([{"Order": v["title"] + (" (" + k.split("(")[1] if "(" in k else ""), "Ready": v["decisions"].get("ready", 0), "Needs review": v["decisions"].get("review", 0), "Missing criteria": v["decisions"].get("missing", 0)} for k, v in pr["orders"].items()]), hide_index=True, width="stretch")
    st.caption(f"Across {pr['patients']} synthetic patients with the order date {pr['order_date']}. There is no answer key, because real insurer decisions are not public. The diabetes injection has no ready patients because Synthea's diabetes is well controlled (every HbA1c on metformin is under 6.5%).")

# ----------------------------------------------------------------- tab 4: audit
with t4:
    log = outbox.read_audit(OUT)
    st.write("Every decision made in this session, newest first. On Streamlit Cloud this log resets when the app restarts.")
    if log: st.dataframe(pd.DataFrame(log[::-1]), hide_index=True, width="stretch")
    else: st.info("No decisions yet.")
