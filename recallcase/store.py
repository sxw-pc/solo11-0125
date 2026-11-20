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
    CREATE TABLE IF NOT EXISTS complaint_source(
      source_id TEXT NOT NULL, version INTEGER NOT NULL, digest TEXT NOT NULL,
      raw BLOB NOT NULL, PRIMARY KEY(source_id,version));
    CREATE TABLE IF NOT EXISTS complaint_conflict(
      id INTEGER PRIMARY KEY, source_id TEXT NOT NULL, version INTEGER NOT NULL,
      digest TEXT NOT NULL, reason TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS complaint_record(
      id TEXT PRIMARY KEY, source_id TEXT NOT NULL, source_version INTEGER NOT NULL,
      channel TEXT NOT NULL, reported_at TEXT NOT NULL, product TEXT NOT NULL,
      lot_text TEXT NOT NULL, defect_code TEXT NOT NULL, case_ref TEXT NOT NULL,
      sold_on TEXT, digest TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS sales_source(
      source_id TEXT NOT NULL, version INTEGER NOT NULL, digest TEXT NOT NULL,
      raw BLOB NOT NULL, PRIMARY KEY(source_id,version));
    CREATE TABLE IF NOT EXISTS sales_conflict(
      id INTEGER PRIMARY KEY, source_id TEXT NOT NULL, version INTEGER NOT NULL,
      digest TEXT NOT NULL, reason TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS sales_day(
      product TEXT NOT NULL, lot_text TEXT NOT NULL, sale_date TEXT NOT NULL,
      units INTEGER NOT NULL, summary TEXT NOT NULL,
      source_id TEXT NOT NULL, source_version INTEGER NOT NULL, digest TEXT NOT NULL,
      PRIMARY KEY(product,lot_text,sale_date));
    CREATE TABLE IF NOT EXISTS closeout_source(
      source_id TEXT NOT NULL, version INTEGER NOT NULL, digest TEXT NOT NULL,
      raw BLOB NOT NULL, PRIMARY KEY(source_id,version));
    CREATE TABLE IF NOT EXISTS closeout_conflict(
      id INTEGER PRIMARY KEY, source_id TEXT NOT NULL, version INTEGER NOT NULL,
      digest TEXT NOT NULL, reason TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS closeout_credential(
      product TEXT NOT NULL, lot_text TEXT NOT NULL, sale_date TEXT NOT NULL,
      channel TEXT NOT NULL, credential_id TEXT NOT NULL UNIQUE,
      closed_at TEXT NOT NULL, source_id TEXT NOT NULL, source_version INTEGER NOT NULL,
      digest TEXT NOT NULL, PRIMARY KEY(product,lot_text,sale_date,channel));
    CREATE TABLE IF NOT EXISTS watch_charter(
      product TEXT PRIMARY KEY, baseline_rate REAL NOT NULL, budget REAL NOT NULL,
      period_days INTEGER NOT NULL, defined_at TEXT NOT NULL, digest TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS watch_decision(
      id INTEGER PRIMARY KEY, product TEXT NOT NULL, lot_text TEXT NOT NULL,
      charter TEXT NOT NULL, day_index INTEGER NOT NULL,
      numerator INTEGER NOT NULL, denominator INTEGER NOT NULL,
      tail_probability REAL NOT NULL, budget_share REAL NOT NULL,
      budget_consumed REAL NOT NULL, baseline_rate REAL NOT NULL,
      decision TEXT NOT NULL, evaluated_at TEXT NOT NULL, evidence TEXT NOT NULL,
      UNIQUE(product,lot_text,charter,day_index));
    CREATE TABLE IF NOT EXISTS watch_investigation(
      product TEXT NOT NULL, lot_text TEXT NOT NULL, charter TEXT NOT NULL,
      issued_at TEXT NOT NULL, day_index INTEGER NOT NULL, decision_id INTEGER NOT NULL,
      PRIMARY KEY(product,lot_text,charter));
    CREATE TABLE IF NOT EXISTS watch_review(
      hold_key TEXT PRIMARY KEY, product TEXT NOT NULL, lot_text TEXT NOT NULL,
      sale_date TEXT NOT NULL, case_ref TEXT NOT NULL, record_id TEXT NOT NULL,
      reason TEXT NOT NULL, detail TEXT NOT NULL);
    """)
    return db

