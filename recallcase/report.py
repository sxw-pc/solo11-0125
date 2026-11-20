"""Evidence-facing report reconstructed from durable entities."""
import json
from pathlib import Path
from .store import connect
from .policy import TABLES,EXTRA_TABLES

def report(db_path,out=None):
    db=connect(db_path);t=TABLES
    notices=[dict(x) for x in db.execute(f"SELECT * FROM {t['notice']} ORDER BY id")]
    cases=[]
    for row in db.execute(f"SELECT * FROM {t['case']} ORDER BY id"):
        result=db.execute(f"SELECT * FROM {t['assessment']} WHERE case_id=? ORDER BY id DESC LIMIT 1",
                          (row["id"],)).fetchone()
        obligations=[dict(x) for x in db.execute(f"SELECT * FROM {t['obligation']} WHERE assessment_id=? ORDER BY target",
                     (result["id"],))] if result else []
        for ob in obligations:
            ob["receipts"]=[dict(x) for x in db.execute(f"SELECT event_key,at,amount,verdict,reason FROM {t['receipt']} WHERE obligation_id=? ORDER BY event_key",(ob["id"],))]
        cases.append({"case":row["id"],"source":row["source_id"],"input_hash":row["input_hash"],
                      "state":result["state"] if result else None,
                      "outcome":result["outcome"] if result else None,
                      "revision":result["source_revision"] if result else None,
                      "stale":bool(result["stale"]) if result else None,
                      "detail":json.loads(result["detail"]) if result else None,
                      "obligations":obligations})
    evidence=[dict(x) for x in db.execute("SELECT source_id,revision,digest,captured_at FROM source_event ORDER BY source_id,revision")]
    conflicts=[dict(x) for x in db.execute("SELECT source_id,revision,digest,reason FROM source_conflict ORDER BY id")]
    auxiliary={name:[dict(x) for x in db.execute(f"SELECT * FROM {name} ORDER BY 1")]
               for name in EXTRA_TABLES if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(name,)).fetchone()}
    complaint_evidence=[dict(x) for x in db.execute(
        "SELECT source_id,version,digest FROM complaint_source ORDER BY source_id,version"
    )]
    from .signals import watch_days_view
    sales_evidence=[dict(x) for x in db.execute(
        "SELECT source_id,version,digest FROM sales_source ORDER BY source_id,version")]
    sales_days=[dict(x) for x in db.execute(
        "SELECT product,lot_text,sale_date,units,summary,source_id,source_version FROM sales_day "
        "ORDER BY product,lot_text,sale_date")]
    closeout_evidence=[dict(x) for x in db.execute(
        "SELECT source_id,version,digest FROM closeout_source ORDER BY source_id,version")]
    closeout_credentials=[dict(x) for x in db.execute(
        "SELECT product,lot_text,sale_date,channel,credential_id,closed_at,source_id,source_version "
        "FROM closeout_credential ORDER BY product,lot_text,sale_date,channel")]
    watch_charters=[dict(x) for x in db.execute("SELECT * FROM watch_charter ORDER BY product")]
    watch_decisions=[]
    for row in db.execute("SELECT * FROM watch_decision ORDER BY product,lot_text,charter,day_index"):
        entry=dict(row);entry["evidence"]=json.loads(entry["evidence"]);watch_decisions.append(entry)
    watch_investigations=[dict(x) for x in db.execute(
        "SELECT * FROM watch_investigation ORDER BY product,lot_text,charter")]
    watch_review=[dict(x) for x in db.execute("SELECT * FROM watch_review ORDER BY hold_key")]
    watch_days=watch_days_view(db)
    db.close()
    result={"notices":notices,"cases":cases,"source_evidence":evidence,"source_conflicts":conflicts,
            "complaint_evidence":complaint_evidence,"auxiliary":auxiliary,
            "sales_evidence":sales_evidence,"sales_days":sales_days,
            "closeout_evidence":closeout_evidence,"closeout_credentials":closeout_credentials,
            "watch_charters":watch_charters,"watch_decisions":watch_decisions,
            "watch_investigations":watch_investigations,"watch_review":watch_review,
            "watch_days":watch_days}
    if out:
        target=Path(out);target.parent.mkdir(parents=True,exist_ok=True)
        temp=target.with_name(target.name+".pending")
        temp.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
        temp.replace(target)
    return result

