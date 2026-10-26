"""Copies the chart tables for the demo cohort into the same database as the claims."""
import sys, duckdb
chart_db, cohort_db, out = sys.argv[1:4]
con = duckdb.connect(out)
con.execute(f"attach '{chart_db}' as ch (read_only)"); con.execute(f"attach '{cohort_db}' as co (read_only)")
for t in ("patients", "conditions", "observations", "medications", "procedures", "encounters"):
    key = "id" if t == "patients" else "patient_id"
    con.execute(f"create or replace table {t} as select * from ch.{t} where {key} in (select id from co.patients)")
    print(t, con.execute(f"select count(*) from {t}").fetchone()[0])
