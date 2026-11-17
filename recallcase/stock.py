"""Distributor stock transfers under a public recall boundary."""
import json
from .store import connect
from .policy import TABLES,assess_case

SCHEMA="""
CREATE TABLE IF NOT EXISTS stock_lot(
 distributor TEXT NOT NULL, lot TEXT NOT NULL, units INTEGER NOT NULL,
 parents TEXT NOT NULL, PRIMARY KEY(distributor,lot));
CREATE TABLE IF NOT EXISTS transfer_order(
 id TEXT PRIMARY KEY, source_distributor TEXT NOT NULL, target_distributor TEXT NOT NULL,
 lot TEXT NOT NULL, units INTEGER NOT NULL, source_id TEXT NOT NULL,
 source_revision INTEGER NOT NULL, state TEXT NOT NULL
 CHECK(state IN ('accepted','held','blocked','refused')));
"""

def load_stock(db_path,distributor,lot,units,parents):
    if units<1 or not distributor or not lot:raise ValueError('invalid_stock_identity')
    db=connect(db_path);db.executescript(SCHEMA)
    old=db.execute('SELECT * FROM stock_lot WHERE distributor=? AND lot=?',(distributor,lot)).fetchone()
    body=json.dumps(parents,sort_keys=True)
    if old and (old['units'],old['parents'])!=(units,body):raise ValueError('stock_identity_conflict')
    with db:db.execute('INSERT OR IGNORE INTO stock_lot VALUES (?,?,?,?)',(distributor,lot,units,body))
    db.close();return {'distributor':distributor,'lot':lot,'units':units}

def transfer(db_path,order_id,source_id,source_distributor,target_distributor,lot,units):
    if units<1 or source_distributor==target_distributor:raise ValueError('invalid_transfer')
    db=connect(db_path);db.executescript(SCHEMA);db.execute('BEGIN IMMEDIATE')
    try:
        t=TABLES
        source=db.execute('SELECT * FROM stock_lot WHERE distributor=? AND lot=?',(source_distributor,lot)).fetchone()
        if not source:raise ValueError('stock_missing')
        notice=db.execute(f'SELECT * FROM {t["notice"]} WHERE id=?',(source_id,)).fetchone()
        if not notice:raise ValueError('adopt_recall_first')
        old=db.execute('SELECT * FROM transfer_order WHERE id=?',(order_id,)).fetchone()
        if old:
            if (old['source_id'],old['source_distributor'],old['target_distributor'],old['lot'],old['units'])!=(source_id,source_distributor,target_distributor,lot,units):
                raise ValueError('transfer_identity_conflict')
            state=old['state']
        else:
            revision=db.execute(f'SELECT payload FROM {t["revision"]} WHERE source_id=? AND revision=?',
                                (source_id,notice['current_revision'])).fetchone()
            if units>source['units']:state='refused'
            elif notice['state']=='conflict':state='blocked'
            elif notice['state']=='cancelled':state='accepted'
            else:
                facts={'lot':lot,'parents':json.loads(source['parents']),'units':units,'distributor':source_distributor}
                decision=assess_case({'facts':facts},json.loads(revision['payload']))
                state='held' if decision['state']=='quarantine' else ('blocked' if decision['state']=='uncertain' else 'accepted')
            db.execute('INSERT INTO transfer_order VALUES (?,?,?,?,?,?,?,?)',
                       (order_id,source_distributor,target_distributor,lot,units,source_id,notice['current_revision'],state))
            if state=='accepted':
                db.execute('UPDATE stock_lot SET units=units-? WHERE distributor=? AND lot=?',(units,source_distributor,lot))
                target=db.execute('SELECT * FROM stock_lot WHERE distributor=? AND lot=?',(target_distributor,lot)).fetchone()
                if target and target['parents']!=source['parents']:raise ValueError('target_lineage_conflict')
                if target:db.execute('UPDATE stock_lot SET units=units+? WHERE distributor=? AND lot=?',(units,target_distributor,lot))
                else:db.execute('INSERT INTO stock_lot VALUES (?,?,?,?)',(target_distributor,lot,units,source['parents']))
                _stale_stock_cases(db,lot,[source_distributor,target_distributor])
        db.commit()
    except Exception:
        db.rollback();raise
    finally:db.close()
    return {'order':order_id,'state':state,'lot':lot,'units':units}

def _stale_stock_cases(db,lot,distributors):
    t=TABLES
    for row in db.execute(f'SELECT id,facts FROM {t["case"]}'):
        facts=json.loads(row['facts'])
        if facts.get('lot')==lot and facts.get('distributor') in distributors:
            db.execute(f'UPDATE {t["assessment"]} SET stale=1 WHERE case_id=?',(row['id'],))
            db.execute(f"UPDATE {t['obligation']} SET state='stale' WHERE assessment_id IN "
                       f"(SELECT id FROM {t['assessment']} WHERE case_id=?)",(row['id'],))

def stock_for(db,distributor,lot):
    return db.execute('SELECT * FROM stock_lot WHERE distributor=? AND lot=?',(distributor,lot)).fetchone()
