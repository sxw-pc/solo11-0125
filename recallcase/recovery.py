"""Sealed-goods recovery chain: custody handovers and factory receipt reconciliation.

The withdrawal chain (assessment -> withdrawal obligation -> withdrawal receipt)
proves that goods left the shelf. This module proves where the sealed goods are
afterwards: each handover moves physical custody between named custodians, and a
factory receipt only credits units the factory has actually taken into custody.
Withdrawal results are never rewritten here.
"""
import json
from .source import read,digest,canonical,utc
from .store import connect
from .policy import TABLES

def set_agreement(db_path,input_path):
    doc=read(input_path)
    for key in ("case_id","mode","stations","factory_custodian"):
        if key not in doc:raise ValueError("agreement_missing_"+key)
    if doc["mode"] not in ("distributor","station"):raise ValueError("agreement_mode_unknown")
    stations=doc["stations"]
    if not isinstance(stations,list) or not stations or any(not x for x in stations):
        raise ValueError("agreement_station_required")
    if not doc["factory_custodian"]:raise ValueError("agreement_factory_custodian_required")
    db=connect(db_path);t=TABLES;fp=digest(doc)
    db.execute("BEGIN IMMEDIATE")
    try:
        assessment=_current_assessment(db,doc["case_id"])
        old=db.execute(f"SELECT * FROM {t['agreement']} WHERE case_id=?",(doc["case_id"],)).fetchone()
        if old:
            if old["digest"]!=fp:raise ValueError("agreement_locked")
            result=_agreement_row(old)
        else:
            if db.execute(f"SELECT 1 FROM {t['handover']} WHERE case_id=?",(doc["case_id"],)).fetchone():
                raise ValueError("agreement_before_first_handover")
            db.execute(f"INSERT INTO {t['agreement']} VALUES (?,?,?,?,?,?,?,CURRENT_TIMESTAMP)",
                       (doc["case_id"],doc["mode"],canonical(stations),doc["factory_custodian"],
                        assessment["source_revision"],canonical(doc),fp))
            result={"case":doc["case_id"],"mode":doc["mode"],"stations":stations,
                    "factory_custodian":doc["factory_custodian"],"revision":assessment["source_revision"]}
        db.commit()
    except Exception:
        db.rollback();raise
    finally:db.close()
    return result

def record_handovers(db_path,input_path):
    doc=read(input_path)
    if "case_id" not in doc or not isinstance(doc.get("handovers"),list) or not doc["handovers"]:
        raise ValueError("handover_input_required")
    t=TABLES;db=connect(db_path)
    db.execute("BEGIN IMMEDIATE")
    try:
        assessment=_current_assessment(db,doc["case_id"])
        agreement=_agreement(db,doc["case_id"])
        pool=_withdrawal_pool(db,doc["case_id"])
        results=[]
        for row in doc["handovers"]:
            results.append(_insert_handover(db,t,doc["case_id"],assessment,agreement,pool,row))
        view=_recompute(db,t,doc["case_id"],agreement,pool)
        db.commit()
    except Exception:
        db.rollback();raise
    finally:db.close()
    return {"case":doc["case_id"],"handovers":results,"totals":_view_totals(pool,view)}

def record_exception(db_path,input_path):
    doc=read(input_path)
    for key in ("case_id","event_key","handover_key","kind","amount","evidence_ref","at"):
        if key not in doc:raise ValueError("exception_missing_"+key)
    if doc["kind"] not in ("loss","short","seal_change","late_correction"):
        raise ValueError("exception_kind_unknown")
    if doc["amount"]<0:raise ValueError("exception_amount_invalid")
    t=TABLES;db=connect(db_path)
    db.execute("BEGIN IMMEDIATE")
    try:
        _current_assessment(db,doc["case_id"])
        agreement=_agreement(db,doc["case_id"])
        pool=_withdrawal_pool(db,doc["case_id"])
        old=db.execute(f"SELECT digest FROM {t['recovery_exception']} WHERE event_key=?",
                       (doc["event_key"],)).fetchone()
        if old:
            if old["digest"]!=digest(doc):raise ValueError("exception_identity_conflict")
        else:
            handover=db.execute(f"SELECT * FROM {t['handover']} WHERE event_key=? AND case_id=?",
                                (doc["handover_key"],doc["case_id"])).fetchone()
            if not handover:raise ValueError("handover_missing")
            if doc["kind"]=="loss" and handover["received_at"]:
                raise ValueError("loss_requires_in_transit")
            if doc["kind"]=="short" and not handover["received_at"]:
                raise ValueError("short_requires_received_handover")
            if doc["kind"]=="seal_change":
                if not doc.get("new_seal"):raise ValueError("exception_new_seal_required")
                if doc.get("prior_seal") and doc["prior_seal"]!=handover["box_seal"]:
                    raise ValueError("seal_change_prior_mismatch")
            if doc["kind"] in ("loss","short"):
                writeoff=db.execute(
                    f"SELECT COALESCE(sum(amount),0) FROM {t['recovery_exception']} "
                    f"WHERE handover_key=? AND kind IN ('loss','short')",
                    (doc["handover_key"],)).fetchone()[0]
                if writeoff+doc["amount"]>handover["amount"]:
                    raise ValueError("exception_exceeds_handover")
            if doc["kind"]=="late_correction":
                writeoff=db.execute(
                    f"SELECT COALESCE(sum(amount),0) FROM {t['recovery_exception']} "
                    f"WHERE handover_key=? AND kind IN ('loss','short')",
                    (doc["handover_key"],)).fetchone()[0]
                if doc["amount"]>writeoff:raise ValueError("correction_exceeds_writeoff")
            db.execute(f"INSERT INTO {t['recovery_exception']} VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                       (doc["event_key"],doc["handover_key"],doc["case_id"],doc["kind"],doc["amount"],
                        doc.get("prior_seal"),doc.get("new_seal"),utc(doc["at"]).isoformat(),
                        doc["evidence_ref"],canonical(doc),digest(doc)))
            _refresh_handover_state(db,t,handover)
        view=_recompute(db,t,doc["case_id"],agreement,pool)
        db.commit()
    except Exception:
        db.rollback();raise
    finally:db.close()
    return {"case":doc["case_id"],"exception":doc["event_key"],"totals":_view_totals(pool,view)}

def record_factory_receipt(db_path,input_path):
    doc=read(input_path)
    for key in ("case_id","cert_no","submitter","at","lines"):
        if key not in doc:raise ValueError("factory_receipt_missing_"+key)
    if not isinstance(doc["lines"],list) or not doc["lines"]:
        raise ValueError("factory_receipt_lines_required")
    t=TABLES;db=connect(db_path)
    db.execute("BEGIN IMMEDIATE")
    try:
        assessment=_current_assessment(db,doc["case_id"])
        agreement=_agreement(db,doc["case_id"])
        pool=_withdrawal_pool(db,doc["case_id"])
        fp=digest(doc)
        old=db.execute(f"SELECT digest FROM {t['factory_receipt']} WHERE cert_no=?",
                       (doc["cert_no"],)).fetchone()
        if old:
            if old["digest"]!=fp:raise ValueError("factory_cert_identity_conflict")
            lines=[dict(x) for x in db.execute(
                f"SELECT origin_distributor,custodian,lot,seal,amount,responsible_party,verdict,reason "
                f"FROM {t['factory_receipt_line']} WHERE cert_no=? ORDER BY id",(doc["cert_no"],))]
        else:
            if doc.get("version") not in (None,assessment["source_revision"]):
                raise ValueError("stale_recall_version")
            view=_recompute(db,t,doc["case_id"],agreement,pool)
            seal_lineage=_seal_lineage(db,t,doc["case_id"])
            lines=[]
            for line in doc["lines"]:
                for key in ("origin_distributor","custodian","lot","seal","amount","responsible_party"):
                    if key not in line:raise ValueError("factory_line_missing_"+key)
                if line["amount"]<1:raise ValueError("factory_line_amount_invalid")
                verdict,reason=_line_verdict(line,agreement,pool,view,seal_lineage)
                lines.append({**line,"verdict":verdict,"reason":reason})
            db.execute(f"INSERT INTO {t['factory_receipt']} VALUES (?,?,?,?,?,?,?)",
                       (doc["cert_no"],doc["case_id"],assessment["source_revision"],doc["submitter"],
                        utc(doc["at"]).isoformat(),canonical(doc),fp))
            for line in lines:
                db.execute(f"INSERT INTO {t['factory_receipt_line']}"
                           f"(cert_no,case_id,origin_distributor,custodian,lot,seal,amount,responsible_party,verdict,reason)"
                           f" VALUES (?,?,?,?,?,?,?,?,?,?)",
                           (doc["cert_no"],doc["case_id"],line["origin_distributor"],line["custodian"],
                            line["lot"],line["seal"],line["amount"],line["responsible_party"],
                            line["verdict"],line["reason"]))
        view=_recompute(db,t,doc["case_id"],agreement,pool)
        db.commit()
    except Exception:
        db.rollback();raise
    finally:db.close()
    return {"case":doc["case_id"],"cert_no":doc["cert_no"],"lines":lines,"totals":_view_totals(pool,view)}

def _insert_handover(db,t,case_id,assessment,agreement,pool,row):
    for key in ("event_key","origin_distributor","from_custodian","to_custodian","lot",
                "amount","origin_seal","box_seal","released_at","release_ref"):
        if key not in row:raise ValueError("handover_missing_"+key)
    if row["amount"]<1:raise ValueError("handover_amount_invalid")
    if row["from_custodian"]==row["to_custodian"]:raise ValueError("handover_same_custodian")
    if row.get("version") not in (None,assessment["source_revision"]):
        raise ValueError("stale_recall_version")
    fp=digest(row)
    old=db.execute(f"SELECT digest,state FROM {t['handover']} WHERE event_key=?",
                   (row["event_key"],)).fetchone()
    if old:
        if old["digest"]!=fp:raise ValueError("handover_identity_conflict")
        return {"event_key":row["event_key"],"state":old["state"],"idempotent":True}
    key=(row["origin_distributor"],row["lot"])
    if key not in pool:raise ValueError("withdrawal_not_proven")
    seal_lineage=_seal_lineage(db,t,case_id)
    if row["origin_seal"] not in seal_lineage.get(key,set()):
        raise ValueError("seal_not_traceable")
    if row["from_custodian"]==row["origin_distributor"]:
        released_total=db.execute(
            f"SELECT COALESCE(sum(amount),0) FROM {t['handover']} "
            f"WHERE case_id=? AND origin_distributor=? AND lot=? AND from_custodian=?",
            (case_id,row["origin_distributor"],row["lot"],row["origin_distributor"])).fetchone()[0]
        if released_total+row["amount"]>pool[key]["withdrawn"]:
            raise ValueError("handover_exceeds_withdrawn")
    stations=set(json.loads(agreement["stations"]))
    factory=agreement["factory_custodian"]
    known=set(d for (d,_lot) in pool)|stations|{factory}
    if row["from_custodian"] not in known or row["to_custodian"] not in known:
        raise ValueError("custodian_outside_agreement")
    view=_recompute(db,t,case_id,agreement,pool)
    available=view["custody"].get((row["from_custodian"],row["lot"]),0)
    if row["amount"]>available:raise ValueError("custody_unavailable")
    received_at=utc(row["received_at"]).isoformat() if row.get("received_at") else None
    if received_at and not row.get("receive_ref"):raise ValueError("receive_ref_required")
    received_amount=row.get("received_amount")
    if received_amount is None:
        received_amount=row["amount"] if received_at else 0
    if received_amount<0 or received_amount>row["amount"]:
        raise ValueError("received_amount_invalid")
    if received_at:
        state="received" if received_amount==row["amount"] else "short"
    else:
        state="in_transit"
    db.execute(f"INSERT INTO {t['handover']} VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
               (row["event_key"],case_id,assessment["source_revision"],row["origin_distributor"],
                row["from_custodian"],row["to_custodian"],row["lot"],row["amount"],received_amount,
                row["origin_seal"],row["box_seal"],row.get("transport_ref"),
                utc(row["released_at"]).isoformat(),received_at,row["release_ref"],
                row.get("receive_ref"),canonical(row),fp,state))
    return {"event_key":row["event_key"],"state":state,"idempotent":False}

def _line_verdict(line,agreement,pool,view,seal_lineage):
    key=(line["origin_distributor"],line["lot"])
    if key not in pool:return "rejected","withdrawal_not_proven"
    if line["seal"] not in seal_lineage.get(key,set()):return "rejected","seal_not_traceable"
    if agreement["mode"]=="distributor":
        if line["responsible_party"]!=line["origin_distributor"]:
            return "rejected","responsibility_mismatch"
    elif line["responsible_party"] not in set(json.loads(agreement["stations"])):
        return "rejected","station_not_responsible"
    if line["custodian"]!=agreement["factory_custodian"]:
        return "rejected","custodian_not_factory"
    already=view["delivered"].get(key,0)
    factory_held=view["factory_held"].get(key,0)
    if already+line["amount"]>factory_held:return "rejected","factory_not_received"
    if already+line["amount"]>pool[key]["withdrawn"]:return "rejected","factory_receipt_cap_exceeded"
    # Tentatively accept: mutate the running view so later lines in the same cert
    # cannot credit the same physical units twice.
    view["delivered"][key]=already+line["amount"]
    return "accepted","factory_receipt_proven"

def _current_assessment(db,case_id):
    t=TABLES
    assessment=db.execute(f"SELECT * FROM {t['assessment']} WHERE case_id=? ORDER BY id DESC LIMIT 1",
                          (case_id,)).fetchone()
    if not assessment:raise ValueError("assess_first")
    if assessment["stale"]:raise ValueError("assessment_stale")
    return assessment

def _agreement(db,case_id):
    row=db.execute(f"SELECT * FROM {TABLES['agreement']} WHERE case_id=?",(case_id,)).fetchone()
    if not row:raise ValueError("agreement_first")
    return row

def _agreement_row(row):
    return {"case":row["case_id"],"mode":row["mode"],"stations":json.loads(row["stations"]),
            "factory_custodian":row["factory_custodian"],"revision":row["source_revision"]}

def _withdrawal_pool(db,case_id):
    """Valid withdrawal receipts are the only sealed units allowed into recovery."""
    t=TABLES
    pool={}
    rows=db.execute(
        f"SELECT r.payload FROM {t['receipt']} r JOIN {t['obligation']} o ON o.id=r.obligation_id "
        f"JOIN {t['assessment']} a ON a.id=o.assessment_id WHERE a.case_id=? AND r.verdict='valid'",
        (case_id,))
    for r in rows:
        payload=json.loads(r["payload"])
        key=(payload["target"],payload["lot"])
        entry=pool.setdefault(key,{"withdrawn":0,"seals":{}})
        entry["withdrawn"]+=payload["amount"]
        entry["seals"][payload["seal"]]=entry["seals"].get(payload["seal"],0)+payload["amount"]
    return pool

def _seal_lineage(db,t,case_id):
    lineage={}
    for (distributor,lot),entry in _withdrawal_pool(db,case_id).items():
        lineage[(distributor,lot)]=set(entry["seals"])
    for h in db.execute(f"SELECT origin_distributor,lot,origin_seal,box_seal FROM {t['handover']} WHERE case_id=?",
                        (case_id,)):
        seals=lineage.setdefault((h["origin_distributor"],h["lot"]),set())
        if h["origin_seal"] not in seals:raise ValueError("seal_lineage_broken")
        seals.add(h["box_seal"])
    for e in db.execute(f"SELECT h.origin_distributor,h.lot,e.prior_seal,e.new_seal FROM {t['recovery_exception']} e "
                        f"JOIN {t['handover']} h ON h.event_key=e.handover_key WHERE e.case_id=? AND e.kind='seal_change'",
                        (case_id,)):
        seals=lineage.setdefault((e["origin_distributor"],e["lot"]),set())
        if e["prior_seal"] and e["prior_seal"] not in seals:raise ValueError("seal_lineage_broken")
        seals.add(e["new_seal"])
    return lineage

def _exception_sums(db,t,handover_key):
    writeoff=db.execute(
        f"SELECT COALESCE(sum(amount),0) FROM {t['recovery_exception']} "
        f"WHERE handover_key=? AND kind IN ('loss','short')",(handover_key,)).fetchone()[0]
    correction=db.execute(
        f"SELECT COALESCE(sum(amount),0) FROM {t['recovery_exception']} "
        f"WHERE handover_key=? AND kind='late_correction'",(handover_key,)).fetchone()[0]
    return writeoff,correction

def _effective_received(db,t,handover_key):
    """Units physically held by the receiving custodian after loss/short/correction."""
    h=db.execute(f"SELECT * FROM {t['handover']} WHERE event_key=?",(handover_key,)).fetchone()
    writeoff,correction=_exception_sums(db,t,handover_key)
    return max(0,min(h["amount"],h["received_amount"]-writeoff+correction))

def _outstanding(db,t,h):
    """Units no longer with the sender and not yet accounted for by the receiver."""
    return max(0,h["amount"]-_effective_received(db,t,h["event_key"])-_net_gap(db,t,h))

def _net_gap(db,t,h):
    """Confirmed missing units for one handover, including its receiving-document shortfall.

    A handover still on the road (no receiving timestamp) cannot declare a confirmed gap;
    its unaccounted units stay in the in-transit bucket until received or lost on evidence.
    """
    if not h["received_at"]:
        writeoff,_correction=_exception_sums(db,t,h["event_key"])
        return writeoff
    writeoff,correction=_exception_sums(db,t,h["event_key"])
    # A receiving-document shortfall and a later loss exception describe the same units.
    gross_gap=max(writeoff,max(0,h["amount"]-h["received_amount"]))
    return max(0,gross_gap-correction)

def _refresh_handover_state(db,t,handover):
    kinds={x["kind"] for x in db.execute(
        f"SELECT kind FROM {t['recovery_exception']} WHERE handover_key=? ORDER BY rowid",
        (handover["event_key"],)).fetchall()}
    q=_effective_received(db,t,handover["event_key"])
    if not handover["received_at"] and "loss" in kinds and q==0:
        state="lost"
    elif q<handover["amount"]:
        state="short"
    elif "late_correction" in kinds:
        state="corrected"
    else:
        state="received" if handover["received_at"] else "in_transit"
    db.execute(f"UPDATE {t['handover']} SET state=? WHERE event_key=?",
               (state,handover["event_key"]))

def _recompute(db,t,case_id,agreement,pool):
    """Re-derive custody, in-transit and delivered quantities from raw evidence."""
    custody={k:v["withdrawn"] for k,v in pool.items()}
    factory=agreement["factory_custodian"]
    in_transit=[];missing=[];factory_held={};released=0;written_off=0
    handovers=db.execute(f"SELECT * FROM {t['handover']} WHERE case_id=? ORDER BY released_at,event_key",
                         (case_id,)).fetchall()
    for h in handovers:
        key=(h["origin_distributor"],h["lot"])
        q=_effective_received(db,t,h["event_key"])
        net_gap=_net_gap(db,t,h)
        balance=custody.get((h["from_custodian"],h["lot"]),0)-h["amount"]
        if balance<0:raise ValueError("custody_invariant_violation")
        custody[(h["from_custodian"],h["lot"])]=balance
        if h["from_custodian"]==key[0]:released+=h["amount"]
        if q>0:
            custody[(h["to_custodian"],h["lot"])]=custody.get((h["to_custodian"],h["lot"]),0)+q
        if h["to_custodian"]==factory and q>0:
            factory_held[key]=factory_held.get(key,0)+q
        written_off+=net_gap
        on_road=max(0,h["amount"]-q-net_gap)
        item={"event_key":h["event_key"],"from_custodian":h["from_custodian"],
              "to_custodian":h["to_custodian"],"lot":h["lot"],"amount":h["amount"],
              "outstanding":on_road,"written_off":net_gap,"state":h["state"]}
        if net_gap>0:missing.append(item)
        if on_road>0:in_transit.append(item)
    delivered={}
    cert_lines=db.execute(
        f"SELECT origin_distributor,lot,amount FROM {t['factory_receipt_line']} WHERE case_id=? AND verdict='accepted'",
        (case_id,))
    for line in cert_lines:
        key=(line["origin_distributor"],line["lot"])
        delivered[key]=delivered.get(key,0)+line["amount"]
        custody[(factory,key[1])]=custody.get((factory,key[1]),0)-line["amount"]
        if custody[(factory,key[1])]<0:raise ValueError("factory_receipt_invariant_violation")
    return {"custody":custody,"in_transit":in_transit,"missing":missing,
            "factory_held":factory_held,"delivered":delivered,"released":released,
            "written_off":written_off}

def _view_totals(pool,view):
    custody_rows=[{"custodian":k[0],"lot":k[1],"units":v}
                  for k,v in sorted(view["custody"].items()) if v]
    return _totals(pool,view,custody_rows)

def recovery_view(db,case_id):
    t=TABLES
    agreement=db.execute(f"SELECT * FROM {t['agreement']} WHERE case_id=?",(case_id,)).fetchone()
    assessment=db.execute(f"SELECT * FROM {t['assessment']} WHERE case_id=? ORDER BY id DESC LIMIT 1",
                          (case_id,)).fetchone()
    pool=_withdrawal_pool(db,case_id)
    if not agreement:
        return {"case":case_id,"agreement":None,"revision":assessment["source_revision"] if assessment else None,
                "stale":bool(assessment["stale"]) if assessment else None,
                "by_distributor":[{"distributor":k[0],"lot":k[1],"withdrawn":v["withdrawn"],
                                   "handed":0,"in_transit":0,"missing":0,"factory_received":0,
                                   "delivered":0,"handover_balance":v["withdrawn"],
                                   "delivery_balance":v["withdrawn"],
                                   "responsibility_balance":v["withdrawn"]} for k,v in sorted(pool.items())],
                "custody":[],"in_transit":[],"missing":[],"handovers":[],"exceptions":[],
                "factory_receipts":[],"totals":_empty_totals(pool)}
    view=_recompute(db,t,case_id,agreement,pool)
    stations=set(json.loads(agreement["stations"]))
    handovers=[_handover_dict(x) for x in db.execute(
        f"SELECT * FROM {t['handover']} WHERE case_id=? ORDER BY released_at,event_key",(case_id,))]
    exceptions=[dict(x) for x in db.execute(
        f"SELECT * FROM {t['recovery_exception']} WHERE case_id=? ORDER BY at,event_key",(case_id,))]
    certs=[]
    for cert in db.execute(f"SELECT * FROM {t['factory_receipt']} WHERE case_id=? ORDER BY cert_no",(case_id,)):
        lines=[dict(x) for x in db.execute(
            f"SELECT origin_distributor,custodian,lot,seal,amount,responsible_party,verdict,reason "
            f"FROM {t['factory_receipt_line']} WHERE cert_no=? ORDER BY id",(cert["cert_no"],))]
        certs.append({"cert_no":cert["cert_no"],"at":cert["at"],"submitter":cert["submitter"],
                      "version":cert["source_revision"],"lines":lines})
    by=[]
    for key in sorted(pool):
        distributor,lot=key
        # The distributor's own handover duty ends at the first leg leaving its custody.
        first_leg=db.execute(
            f"SELECT * FROM {t['handover']} WHERE case_id=? AND origin_distributor=? AND lot=? "
            f"AND from_custodian=? ORDER BY released_at,event_key",
            (case_id,distributor,lot,distributor)).fetchall()
        handed=sum(x["amount"] for x in first_leg)
        received=sum(_effective_received(db,t,x["event_key"]) for x in first_leg)
        in_transit=sum(_outstanding(db,t,x) for x in first_leg if not x["received_at"])
        missing=sum(_net_gap(db,t,x) for x in first_leg)
        factory_received=view["factory_held"].get(key,0)
        delivered=view["delivered"].get(key,0)
        # Handover duty is cleared once the full quantity left the distributor's custody.
        handover_balance=pool[key]["withdrawn"]-handed
        # Open responsibility covers units still on the road or confirmed missing.
        open_chain=in_transit+missing
        if agreement["mode"]=="station":
            delivery_balance=handover_balance
            responsibility_balance=open_chain
        else:
            delivery_balance=pool[key]["withdrawn"]-delivered
            responsibility_balance=delivery_balance
        by.append({"distributor":distributor,"lot":lot,"withdrawn":pool[key]["withdrawn"],
                   "handed":handed,"received_by_next":received,"in_transit":in_transit,
                   "missing":missing,"factory_received":factory_received,"delivered":delivered,
                   "handover_balance":handover_balance,"delivery_balance":delivery_balance,
                   "responsibility_balance":responsibility_balance})
    station_duty=[]
    if agreement["mode"]=="station":
        for station in sorted(stations):
            inflow=0;outflow=0;lost=0
            for h in db.execute(f"SELECT * FROM {t['handover']} WHERE case_id=? AND to_custodian=?",
                                (case_id,station)):
                inflow+=h["amount"];lost+=_net_gap(db,t,h)
            for h in db.execute(f"SELECT * FROM {t['handover']} WHERE case_id=? AND from_custodian=?",
                                (case_id,station)):
                outflow+=h["amount"];lost+=_net_gap(db,t,h)
            credited=db.execute(
                f"SELECT COALESCE(sum(amount),0) FROM {t['factory_receipt_line']} WHERE case_id=? "
                f"AND responsible_party=? AND verdict='accepted'",(case_id,station)).fetchone()[0]
            # Open duty = units the station still holds, has on the road, or has lost.
            # Factory-credited units have already left those buckets, so they are not subtracted twice.
            held=sum(v for (c,_lot),v in view["custody"].items() if c==station)
            road=sum(x["outstanding"] for x in view["in_transit"] if x["from_custodian"]==station)
            open_duty=held+road+lost
            station_duty.append({"station":station,"received":inflow,"released":outflow,
                                 "written_off":lost,"delivered_credited":credited,
                                 "responsibility_balance":open_duty})
    custody_rows=[{"custodian":k[0],"lot":k[1],"units":v} for k,v in sorted(view["custody"].items()) if v]
    totals=_totals(pool,view,custody_rows)
    return {"case":case_id,"revision":assessment["source_revision"] if assessment else None,
            "stale":bool(assessment["stale"]) if assessment else None,
            "agreement":_agreement_row(agreement),"by_distributor":by,"station_duty":station_duty,
            "custody":custody_rows,"in_transit":view["in_transit"],"missing":view["missing"],
            "handovers":handovers,"exceptions":exceptions,"factory_receipts":certs,"totals":totals}

def _handover_dict(row):
    return {"event_key":row["event_key"],"origin_distributor":row["origin_distributor"],
            "from_custodian":row["from_custodian"],"to_custodian":row["to_custodian"],
            "lot":row["lot"],"amount":row["amount"],"received_amount":row["received_amount"],
            "origin_seal":row["origin_seal"],
            "box_seal":row["box_seal"],"transport_ref":row["transport_ref"],
            "released_at":row["released_at"],"received_at":row["received_at"],
            "release_ref":row["release_ref"],"receive_ref":row["receive_ref"],
            "version":row["source_revision"],"state":row["state"]}

def _empty_totals(pool):
    withdrawn=sum(v["withdrawn"] for v in pool.values())
    return {"withdrawn":withdrawn,"released":0,"in_custody":withdrawn,"in_transit":0,"missing":0,
            "written_off":0,"factory_received":0,"delivered":0,"sealed_in_chain":withdrawn,
            "conservation":True}

def _totals(pool,view,custody_rows):
    withdrawn=sum(v["withdrawn"] for v in pool.values())
    in_custody=sum(x["units"] for x in custody_rows)
    in_transit=sum(x["outstanding"] for x in view["in_transit"])
    written_off=view["written_off"]
    missing=written_off
    factory_received=sum(view["factory_held"].values())
    delivered=sum(view["delivered"].values())
    sealed_in_chain=in_custody+in_transit
    # Every withdrawn unit is in exactly one terminal/chain bucket.
    conservation=withdrawn==delivered+written_off+sealed_in_chain
    return {"withdrawn":withdrawn,"released":view["released"],"in_custody":in_custody,
            "in_transit":in_transit,"missing":missing,"written_off":written_off,
            "factory_received":factory_received,"delivered":delivered,
            "sealed_in_chain":sealed_in_chain,"conservation":conservation}
