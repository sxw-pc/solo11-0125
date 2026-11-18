"""Recall scope, lot lineage and channel withdrawal rules."""
TABLES={"notice":"recall_notice","revision":"lot_scope","case":"inventory_case",
"assessment":"lineage_assessment","obligation":"withdrawal_obligation","receipt":"withdrawal_receipt"}
DEMO={"adopt":"recall_normal.json","assess":"lineage_normal.json","settle":"withdraw_normal.json"}
EXTRA_TABLES=["stock_lot","transfer_order","complaint_conflict","complaint_record"]
def validate_notice(doc):
    p=doc["payload"]
    if p.get("authority")!="regulator":return "invalid","untrusted_recall_authority"
    if p.get("action")=="cancel":return "cancelled","recall_withdrawn"
    if p.get("action")!="recall" or not p.get("lots"):
        return "invalid","empty_recall_scope"
    return "active","accepted_recall_scope"
def assess_case(doc,p):
    f=doc["facts"];lot=f.get("lot");parents=f.get("parents",{})
    if not lot or f.get("units",0)<1:return {"state":"uncertain","outcome":"inventory_identity_missing","detail":{},"obligations":[]}
    if not f.get('lineage_complete',True):
        return {"state":"uncertain","outcome":"lot_origin_unproven","detail":{"lot":lot},"obligations":[]}
    visited=set();active=set();cycle=False
    def walk(item):
        nonlocal cycle
        if item in active:cycle=True;return
        if item in visited:return
        active.add(item);visited.add(item)
        for parent in parents.get(item,[]):walk(parent)
        active.remove(item)
    for component in f.get('components',[lot]):walk(component)
    if cycle:return {"state":"uncertain","outcome":"lot_lineage_cycle","detail":{"visited":sorted(visited)},"obligations":[]}
    affected=bool(visited.intersection(p["lots"]))
    if not affected:return {"state":"clear","outcome":"lot_outside_recall","detail":{"visited":sorted(visited)},"obligations":[]}
    channels=f.get('channels') or [{"distributor":f.get("distributor"),"units":f["units"]}]
    if (any(not x.get('distributor') or x.get('units',0)<1 for x in channels)
        or len({x['distributor'] for x in channels})!=len(channels)
        or sum(x['units'] for x in channels)!=f['units']):
        return {"state":"uncertain","outcome":"distribution_scope_unknown","detail":{"visited":sorted(visited)},"obligations":[]}
    obligations=[{"target":x['distributor'],"deadline":p["withdraw_by"],"required":x['units'],
        "meta":{"lot":lot,"scope":sorted(visited.intersection(p["lots"]))}} for x in channels]
    return {"state":"quarantine","outcome":"lot_requires_withdrawal",
        "detail":{"visited":sorted(visited),"affected_scope":obligations[0]["meta"]["scope"],
                  "mixed_components":f.get('components',[lot]),"channels":[x['target'] for x in obligations]},"obligations":obligations}
def receipt_verdict(r,ob,version):
    import json
    meta=json.loads(ob["meta"])
    if r["version"]!=version:return "invalid","stale_recall_version"
    if r.get("lot")!=meta["lot"]:return "invalid","wrong_inventory_lot"
    if not r.get("seal"):return "invalid","warehouse_seal_missing"
    if r["amount"]<1:return "invalid","empty_withdrawal"
    return "valid","withdrawal_proven"

