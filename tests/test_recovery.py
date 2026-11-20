import json
from pathlib import Path
import pytest
from recallcase.adoption import adopt
from recallcase.assessment import assess
from recallcase.settlement import settle
from recallcase.report import report
from recallcase.store import connect
from recallcase.policy import TABLES
from recallcase.recovery import (set_agreement,record_handovers,record_exception,
                                 record_factory_receipt,recovery_view)

FIX=Path(__file__).resolve().parent.parent/"fixtures"
REC=FIX/"recovery"

def base(db,agreement="agreement_station.json"):
    adopt(db,FIX/"recall_normal.json")
    assess(db,REC/"lineage_ab.json")
    settle(db,REC/"withdraw_ab.json")
    set_agreement(db,REC/agreement)

def view(db,case="case-ab"):
    con=connect(db)
    data=recovery_view(con,case)
    con.close()
    return data

def write(tmp_path,name,doc):
    path=tmp_path/name
    path.write_text(json.dumps(doc),encoding="utf-8")
    return path

def assert_conserved(v):
    t=v["totals"]
    assert t["conservation"] is True
    assert t["withdrawn"]==t["delivered"]+t["written_off"]+t["in_custody"]+t["in_transit"]
    # Physical quantity never exceeds the seven units that were withdrawn.
    assert t["withdrawn"]==7
    assert t["in_custody"]+t["in_transit"]+t["written_off"]+t["delivered"]==7

def test_station_protocol_closes_both_duties_with_seven_units(tmp_path):
    db=tmp_path/"state.db";base(db)
    record_handovers(db,REC/"handover_a_to_b.json")
    record_handovers(db,REC/"handover_b_to_factory_7.json")
    result=record_factory_receipt(db,REC/"factory_receipt_ab_station.json")
    assert {(x["origin_distributor"],x["verdict"]) for x in result["lines"]}=={
        ("D1","accepted"),("D2","accepted")}
    v=view(db)
    assert_conserved(v)
    assert v["totals"]["delivered"]==7 and v["totals"]["factory_received"]==7
    assert all(x["responsibility_balance"]==0 for x in v["by_distributor"])

def test_distributor_protocol_rejects_silent_responsibility_transfer(tmp_path):
    db=tmp_path/"state.db";base(db,"agreement_distributor.json")
    record_handovers(db,REC/"handover_a_to_b.json")
    record_handovers(db,REC/"handover_b_to_factory_7.json")
    # A station-responsible certificate cannot credit distributor-mode duties.
    rejected=record_factory_receipt(db,REC/"factory_receipt_ab_station.json")
    assert {x["reason"] for x in rejected["lines"]}=={"responsibility_mismatch"}
    v=view(db)
    assert v["totals"]["delivered"]==0
    assert {x["distributor"]:x["responsibility_balance"] for x in v["by_distributor"]}=={
        "D1":3,"D2":4}
    # Each distributor still needs its own factory receipt naming itself.
    accepted=record_factory_receipt(db,REC/"factory_receipt_ab_distributor.json")
    assert all(x["verdict"]=="accepted" for x in accepted["lines"])
    assert view(db)["totals"]["delivered"]==7

def test_split_boxes_share_one_truck_and_keep_single_custody(tmp_path):
    db=tmp_path/"state.db";base(db)
    record_handovers(db,REC/"handover_ab_split.json")
    v=view(db)
    assert {h["transport_ref"] for h in v["handovers"]}=={"TRUCK-1"}
    assert {h["box_seal"] for h in v["handovers"]}=={"BX-A1","BX-A2","BX-B1"}
    assert_conserved(v)
    by={x["distributor"]:x for x in v["by_distributor"]}
    assert by["D1"]["handed"]==3 and by["D2"]["handed"]==4
    # The same three D1 units cannot be released a second time.
    dup=json.loads((REC/"handover_a_to_b.json").read_text(encoding="utf-8"))
    dup["handovers"][0].update(event_key="ho-dup",box_seal="BX-DUP",
                               to_custodian="S1",transport_ref="TRUCK-X")
    with pytest.raises(ValueError,match="custody_unavailable|handover_exceeds_withdrawn"):
        record_handovers(db,write(tmp_path,"dup.json",dup))

def test_short_receipt_keeps_lookup_quantity_and_never_rewrites_withdrawal(tmp_path):
    db=tmp_path/"state.db";base(db)
    record_handovers(db,REC/"handover_short.json")
    record_exception(db,REC/"exception_short.json")
    v=view(db)
    assert_conserved(v)
    assert v["totals"]["written_off"]==1 and v["totals"]["in_custody"]==6
    d1=next(x for x in v["by_distributor"] if x["distributor"]=="D1")
    assert d1["missing"]==1 and d1["handover_balance"]==0
    # The lost unit stays on the responsibility ledger as still needing resolution.
    assert d1["responsibility_balance"]==1 and d1["delivery_balance"]==0
    # The withdrawal obligation itself stays complete; recovery never flips it back.
    con=connect(db)
    states=[r["state"] for r in con.execute(
        f"SELECT state FROM {TABLES['obligation']} ORDER BY target")]
    con.close()
    assert states==["complete","complete"]

def test_in_transit_loss_is_not_factory_receipt(tmp_path):
    db=tmp_path/"state.db";base(db)
    record_handovers(db,REC/"handover_in_transit_lost.json")
    before=view(db)["totals"]
    assert before["in_transit"]==2 and before["factory_received"]==0
    record_exception(db,REC/"exception_loss.json")
    after=view(db)["totals"]
    assert after["in_transit"]==0 and after["written_off"]==2 and after["factory_received"]==0
    assert after["conservation"] is True

def test_seal_change_preserves_lineage_and_late_correction_restores_gap(tmp_path):
    db=tmp_path/"state.db";base(db)
    record_handovers(db,REC/"handover_short.json")
    record_exception(db,REC/"exception_short.json")
    record_handovers(db,REC/"handover_seal.json")
    record_exception(db,REC/"exception_seal_change.json")
    v=view(db)
    assert any(e["kind"]=="seal_change" and e["new_seal"]=="BX-SEAL-R" for e in v["exceptions"])
    # The replacement seal is now traceable, so a factory receipt on it is admissible later.
    con=connect(db)
    from recallcase.recovery import _seal_lineage
    assert "BX-SEAL-R" in _seal_lineage(con,TABLES,"case-ab")[("D2","L-100-A")]
    con.close()
    corrected=record_exception(db,REC/"exception_correction.json")
    assert corrected["totals"]["written_off"]==0
    assert corrected["totals"]["conservation"] is True

def test_factory_certificate_cannot_be_reused_under_two_numbers(tmp_path):
    db=tmp_path/"state.db";base(db)
    record_handovers(db,REC/"handover_ab_split.json")
    record_handovers(db,REC/"handover_factory_7.json")
    record_factory_receipt(db,REC/"factory_receipt_station_7.json")
    second=record_factory_receipt(db,REC/"factory_receipt_duplicate.json")
    assert all(x["verdict"]=="rejected" and x["reason"]=="factory_not_received"
               for x in second["lines"])
    assert view(db)["totals"]["delivered"]==7

def test_revision_round_keeps_history_and_opens_only_incremental_gap(tmp_path):
    db=tmp_path/"state.db";base(db)
    record_handovers(db,REC/"handover_ab_split.json")
    adopt(db,REC/"recall_rev2_expand.json")
    # New recovery events are blocked until the new round is assessed.
    with pytest.raises(ValueError,match="assessment_stale"):
        record_handovers(db,REC/"handover_factory_7.json")
    assess(db,REC/"lineage_ab_rev2.json")
    settle(db,REC/"withdraw_ab_rev2_increment.json")
    set_agreement(db,REC/"agreement_station_r2.json")
    old=view(db,"case-ab");new=view(db,"case-ab-r2")
    # Round-one transport facts survive, aligned to revision one.
    assert len(old["handovers"])==3 and {h["version"] for h in old["handovers"]}=={1}
    # The new round only creates a gap for the three incremental units.
    assert sum(x["withdrawn"] for x in new["by_distributor"])==3
    assert sum(x["handover_balance"] for x in new["by_distributor"])==3
    assert new["totals"]["withdrawn"]==3 and new["totals"]["conservation"] is True

def test_scope_narrowing_does_not_delete_shipments_or_fake_receipts(tmp_path):
    db=tmp_path/"state.db";base(db)
    record_handovers(db,REC/"handover_ab_split.json")
    record_handovers(db,REC/"handover_factory_7.json")
    adopt(db,REC/"recall_rev3_narrow.json")
    assess(db,REC/"lineage_ab_rev2.json")
    v=view(db,"case-ab")
    assert len(v["handovers"])==5
    assert v["totals"]["factory_received"]==7 and v["totals"]["delivered"]==0

def test_same_files_rerun_are_idempotent_and_conflicts_are_rejected(tmp_path):
    db=tmp_path/"state.db";base(db)
    for _ in range(2):
        r=record_handovers(db,REC/"handover_ab_split.json")
        assert all(x["idempotent"] in (True,False) for x in r["handovers"])
    first=view(db)
    record_handovers(db,REC/"handover_ab_split.json")
    assert view(db)["totals"]==first["totals"]
    # Reusing an event key with different bytes is an identity conflict.
    changed=json.loads((REC/"handover_ab_split.json").read_text(encoding="utf-8"))
    changed["handovers"][0]["amount"]=1
    with pytest.raises(ValueError,match="handover_identity_conflict"):
        record_handovers(db,write(tmp_path,"changed.json",changed))

def test_factory_cert_identity_conflict_on_changed_bytes(tmp_path):
    db=tmp_path/"state.db";base(db)
    record_handovers(db,REC/"handover_ab_split.json")
    record_handovers(db,REC/"handover_factory_7.json")
    record_factory_receipt(db,REC/"factory_receipt_station_7.json")
    cert=json.loads((REC/"factory_receipt_station_7.json").read_text(encoding="utf-8"))
    cert["lines"][0]["amount"]=2
    with pytest.raises(ValueError,match="factory_cert_identity_conflict"):
        record_factory_receipt(db,write(tmp_path,"cert.json",cert))

def test_report_totals_recompute_from_raw_evidence(tmp_path):
    db=tmp_path/"state.db";base(db)
    record_handovers(db,REC/"handover_ab_split.json")
    record_handovers(db,REC/"handover_factory_7.json")
    record_factory_receipt(db,REC/"factory_receipt_station_7.json")
    out=tmp_path/"report.json"
    report(db,out)
    doc=json.loads(out.read_text(encoding="utf-8"))
    rv=doc["recovery"]["case-ab"]
    # Recompute delivered and physical totals straight from the persisted evidence.
    delivered=sum(l["amount"] for c in rv["factory_receipts"]
                  for l in c["lines"] if l["verdict"]=="accepted")
    assert delivered==rv["totals"]["delivered"]==7
    assert len(rv["handovers"])==5
    assert rv["agreement"]["mode"]=="station"
