import json
from pathlib import Path
from recallcase.adoption import adopt
from recallcase.assessment import assess
from recallcase.settlement import settle
from recallcase.report import report
from recallcase.stock import load_stock,transfer

FIX=Path(__file__).resolve().parent.parent/'fixtures'

def changed(tmp_path,name,mutate):
    doc=json.loads((FIX/name).read_text(encoding='utf-8'));mutate(doc)
    path=tmp_path/(str(len(list(tmp_path.glob('*.json'))))+'.json')
    path.write_text(json.dumps(doc),encoding='utf-8');return path

def test_mixed_pack_quarantines_all_units_and_dag_is_not_cycle(tmp_path):
    db=tmp_path/'state.db';adopt(db,FIX/'recall_normal.json')
    mixed=changed(tmp_path,'lineage_normal.json',lambda d:(d.update(case_id='mixed'),d['facts'].update(lot='MIX',components=['L-100-A','L-200'],parents={'L-100-A':['L-100'],'L-200':[]},units=8)))
    result=assess(db,mixed)
    assert result['state']=='quarantine' and result['obligations'][0]['required']==8
    diamond=changed(tmp_path,'lineage_normal.json',lambda d:(d.update(case_id='diamond'),d['facts'].update(lot='MIX',components=['A'],parents={'A':['B','C'],'B':['L-100'],'C':['L-100']},units=3)))
    assert assess(db,diamond)['state']=='quarantine'

def test_unknown_origin_and_wrong_channel_receipt_do_not_close_recall(tmp_path):
    db=tmp_path/'state.db';adopt(db,FIX/'recall_normal.json')
    unknown=changed(tmp_path,'lineage_normal.json',lambda d:(d.update(case_id='unknown'),d['facts'].update(lineage_complete=False)))
    assert assess(db,unknown)['outcome']=='lot_origin_unproven'
    assess(db,FIX/'lineage_normal.json')
    assert settle(db,FIX/'withdraw_missing.json')['status']=='overdue'
    assert next(x for x in report(db)['cases'] if x['case']=='inventory-1')['state']=='quarantine'

def test_stock_transfer_hold_then_cancel_and_new_recall_moves_obligation(tmp_path):
    stock=json.loads((FIX/'stock_normal.json').read_text(encoding='utf-8'))
    move=json.loads((FIX/'stock_conflict.json').read_text(encoding='utf-8'))
    args={k:v for k,v in move.items() if k not in ('expected_during_recall','expected_after_cancel')}
    db=tmp_path/'state.db';adopt(db,FIX/'recall_normal.json');load_stock(db,**stock)
    assert transfer(db,**args)['state']=='held'
    cancel=changed(tmp_path,'recall_normal.json',lambda d:(d.update(revision=2),d['payload'].update(action='cancel')))
    adopt(db,cancel)
    args['order_id']='TR-2'
    assert transfer(db,**args)['state']=='accepted'
    restore=changed(tmp_path,'recall_normal.json',lambda d:d.update(revision=3))
    adopt(db,restore)
    destination=changed(tmp_path,'lineage_normal.json',lambda d:(d.update(case_id='at-D2'),d['facts'].update(distributor='D2')))
    result=assess(db,destination)
    assert result['outcome']=='lot_requires_withdrawal'
    assert result['obligations'][0]['target']=='D2'

def test_recall_correction_and_receipt_version_change_result(tmp_path):
    db=tmp_path/'state.db';adopt(db,FIX/'recall_normal.json');assess(db,FIX/'lineage_normal.json')
    corrected=changed(tmp_path,'recall_normal.json',lambda d:(d.update(revision=2),d['payload'].update(lots=['L-101'])))
    adopt(db,corrected)
    assert report(db)['cases'][0]['stale'] is True
    assert assess(db,FIX/'lineage_normal.json')['outcome']=='lot_outside_recall'

def test_overlapping_public_recall_expands_other_notice_scope(tmp_path):
    db=tmp_path/'state.db';adopt(db,FIX/'recall_normal.json')
    separate=changed(tmp_path,'lineage_normal.json',lambda d:(d.update(case_id='lot-200'),d['facts'].update(lot='L-200',parents={},units=2)))
    assert assess(db,separate)['outcome']=='lot_outside_recall'
    adopt(db,FIX/'recall_overlap.json')
    assert report(db)['cases'][0]['stale'] is True
    merged=assess(db,separate)
    assert merged['outcome']=='lot_requires_withdrawal'
    assert merged['detail']['merged_sources']==['recall-1','recall-2']

def test_multiple_distributors_require_independent_withdrawal_proofs(tmp_path):
    db=tmp_path/'state.db';adopt(db,FIX/'recall_normal.json')
    distributed=changed(tmp_path,'lineage_normal.json',lambda d:(d.update(case_id='multi-channel'),d['facts'].update(channels=[{'distributor':'D1','units':3},{'distributor':'D2','units':2}])))
    decision=assess(db,distributed)
    assert {x['target'] for x in decision['obligations']}=={'D1','D2'}
    first=changed(tmp_path,'withdraw_normal.json',lambda d:(d.update(case_id='multi-channel'),d['receipts'][0].update(amount=3)))
    assert settle(db,first)['status']=='partial'
    second=changed(tmp_path,'withdraw_normal.json',lambda d:(d.update(case_id='multi-channel'),d['receipts'][0].update(event_key='withdraw-D2',target='D2',amount=2)))
    assert settle(db,second)['status']=='complete'
