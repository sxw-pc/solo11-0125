"""SQLite state with separate entities for source adoption, business cases and fulfillment."""
import sqlite3
from pathlib import Path
from .policy import TABLES

def connect(path):
    path=Path(path)
    path.parent.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(path)
    db.row_factory=sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    t=TABLES
    db.executescript(f"""
    CREATE TABLE IF NOT EXISTS source_event(
      source_id TEXT, revision INTEGER, digest TEXT, raw TEXT, captured_at TEXT,
      PRIMARY KEY(source_id,revision));
    CREATE TABLE IF NOT EXISTS source_conflict(
      id INTEGER PRIMARY KEY, source_id TEXT, revision INTEGER, digest TEXT, reason TEXT);
    CREATE TABLE IF NOT EXISTS {t['notice']}(
      id TEXT PRIMARY KEY, current_revision INTEGER NOT NULL, state TEXT NOT NULL,
      changed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
    CREATE TABLE IF NOT EXISTS {t['revision']}(
      source_id TEXT, revision INTEGER, published_at TEXT, effective_from TEXT,
      effective_until TEXT, digest TEXT, state TEXT, reason TEXT, payload TEXT,
      PRIMARY KEY(source_id,revision));
    CREATE TABLE IF NOT EXISTS {t['case']}(
      id TEXT PRIMARY KEY, source_id TEXT NOT NULL, at TEXT NOT NULL,
      facts TEXT NOT NULL, input_hash TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS {t['assessment']}(
      id INTEGER PRIMARY KEY, case_id TEXT NOT NULL, source_revision INTEGER,
      state TEXT NOT NULL, outcome TEXT NOT NULL, detail TEXT NOT NULL,
      stale INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
    CREATE TABLE IF NOT EXISTS {t['obligation']}(
      id TEXT PRIMARY KEY, assessment_id INTEGER NOT NULL, target TEXT NOT NULL,
      deadline TEXT NOT NULL, required INTEGER NOT NULL, meta TEXT NOT NULL,
      state TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS {t['receipt']}(
      event_key TEXT PRIMARY KEY, obligation_id TEXT NOT NULL, at TEXT NOT NULL,
      version INTEGER, amount INTEGER NOT NULL, payload TEXT NOT NULL,
      digest TEXT NOT NULL, verdict TEXT NOT NULL, reason TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS {t['agreement']}(
      case_id TEXT PRIMARY KEY, mode TEXT NOT NULL CHECK(mode IN ('distributor','station')),
      stations TEXT NOT NULL, factory_custodian TEXT NOT NULL,
      source_revision INTEGER NOT NULL, payload TEXT NOT NULL, digest TEXT NOT NULL,
      created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
    CREATE TABLE IF NOT EXISTS {t['handover']}(
      event_key TEXT PRIMARY KEY, case_id TEXT NOT NULL, source_revision INTEGER NOT NULL,
      origin_distributor TEXT NOT NULL, from_custodian TEXT NOT NULL, to_custodian TEXT NOT NULL,
      lot TEXT NOT NULL, amount INTEGER NOT NULL, received_amount INTEGER NOT NULL,
      origin_seal TEXT NOT NULL, box_seal TEXT NOT NULL,
      transport_ref TEXT, released_at TEXT NOT NULL, received_at TEXT,
      release_ref TEXT NOT NULL, receive_ref TEXT, payload TEXT NOT NULL, digest TEXT NOT NULL,
      state TEXT NOT NULL CHECK(state IN ('in_transit','received','short','lost','corrected')));
    CREATE TABLE IF NOT EXISTS {t['recovery_exception']}(
      event_key TEXT PRIMARY KEY, handover_key TEXT NOT NULL, case_id TEXT NOT NULL,
      kind TEXT NOT NULL CHECK(kind IN ('loss','short','seal_change','late_correction')),
      amount INTEGER NOT NULL, prior_seal TEXT, new_seal TEXT, at TEXT NOT NULL,
      evidence_ref TEXT NOT NULL, payload TEXT NOT NULL, digest TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS {t['factory_receipt']}(
      cert_no TEXT PRIMARY KEY, case_id TEXT NOT NULL, source_revision INTEGER NOT NULL,
      submitter TEXT NOT NULL, at TEXT NOT NULL, payload TEXT NOT NULL, digest TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS {t['factory_receipt_line']}(
      id INTEGER PRIMARY KEY AUTOINCREMENT, cert_no TEXT NOT NULL, case_id TEXT NOT NULL,
      origin_distributor TEXT NOT NULL, custodian TEXT NOT NULL, lot TEXT NOT NULL, seal TEXT NOT NULL,
      amount INTEGER NOT NULL, responsible_party TEXT NOT NULL,
      verdict TEXT NOT NULL CHECK(verdict IN ('accepted','rejected')), reason TEXT NOT NULL);
    """)
    return db

