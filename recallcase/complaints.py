"""Offline quality complaint intake, separate from official recall notices.

Also collects the offline sales-exposure and close-out evidence the complaint
rate monitor relies on.  All three evidence kinds keep source identity, exact
bytes and per-record identity digests; re-importing the same file replays
without growth and same-version different-bytes is preserved as a conflict.
"""
import hashlib
import json
from datetime import date
from pathlib import Path

from .source import digest, utc
from .store import connect

# The two complaint sources whose close-out credentials complete a sale day.
CHANNELS = ("hotline", "retailer")


def _sale_date(value):
    if not isinstance(value, str):
        raise ValueError("sale_date_required")
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        raise ValueError("sale_date_required") from None


def import_complaints(db_path, input_path):
    raw = Path(input_path).read_bytes()
    doc = json.loads(raw.decode("utf-8"))
    source_id = doc.get("source_id")
    version = doc.get("version")
    records = doc.get("records")
    if not source_id or not isinstance(version, int) or version < 1 or not isinstance(records, list):
        raise ValueError("complaint_source_required")
    sha = hashlib.sha256(raw).hexdigest()
    db = connect(db_path)
    conflict = None
    try:
        db.execute("BEGIN IMMEDIATE")
        old_source = db.execute(
            "SELECT digest FROM complaint_source WHERE source_id=? AND version=?",
            (source_id, version),
        ).fetchone()
        if old_source:
            if old_source["digest"] == sha:
                db.commit()
                return {"source": source_id, "version": version, "imported": 0, "replayed": True}
            conflict = "complaint_source_conflict"
            raise ValueError(conflict)
        prepared = []
        for item in records:
            required = ("id", "channel", "reported_at", "product", "lot_text", "defect_code", "case_ref")
            if any(not item.get(key) for key in required):
                raise ValueError("complaint_record_required")
            utc(item["reported_at"])
            sold_on = _sale_date(item["sold_on"]) if item.get("sold_on") else None
            row = (
                item["id"], source_id, version, item["channel"], item["reported_at"],
                item["product"], item["lot_text"], item["defect_code"], item["case_ref"],
                sold_on, digest(item),
            )
            old = db.execute("SELECT digest FROM complaint_record WHERE id=?", (item["id"],)).fetchone()
            if old:
                if old["digest"] != row[-1]:
                    conflict = "complaint_identity_conflict"
                    raise ValueError(conflict)
                continue
            prepared.append(row)
        db.execute(
            "INSERT INTO complaint_source(source_id,version,digest,raw) VALUES (?,?,?,?)",
            (source_id, version, sha, raw),
        )
        db.executemany(
            "INSERT INTO complaint_record VALUES (?,?,?,?,?,?,?,?,?,?,?)", prepared
        )
        db.commit()
        return {"source": source_id, "version": version, "imported": len(prepared), "replayed": False}
    except Exception:
        db.rollback()
        if conflict:
            with db:
                db.execute(
                    "INSERT INTO complaint_conflict(source_id,version,digest,reason) VALUES (?,?,?,?)",
                    (source_id, version, sha, conflict),
                )
        raise
    finally:
        db.close()


def import_sales(db_path, input_path):
    """Import an offline sales-exposure file: actual units sold per
    product/lot/sale day plus the source summary.  A sale day is identified by
    (product, lot_text, sale_date); same identity with different content is a
    recorded conflict, never a rewrite."""
    raw = Path(input_path).read_bytes()
    doc = json.loads(raw.decode("utf-8"))
    source_id = doc.get("source_id")
    version = doc.get("version")
    days = doc.get("days")
    if not source_id or not isinstance(version, int) or version < 1 or not isinstance(days, list):
        raise ValueError("sales_source_required")
    sha = hashlib.sha256(raw).hexdigest()
    db = connect(db_path)
    conflict = None
    try:
        db.execute("BEGIN IMMEDIATE")
        old_source = db.execute(
            "SELECT digest FROM sales_source WHERE source_id=? AND version=?",
            (source_id, version),
        ).fetchone()
        if old_source:
            if old_source["digest"] == sha:
                db.commit()
                return {"source": source_id, "version": version, "imported": 0, "replayed": True}
            conflict = "sales_source_conflict"
            raise ValueError(conflict)
        prepared = []
        for item in days:
            if any(not item.get(key) for key in ("product", "lot_text", "sale_date", "summary")):
                raise ValueError("sales_day_required")
            units = item.get("units")
            if not isinstance(units, int) or isinstance(units, bool) or units < 0:
                raise ValueError("sales_units_invalid")
            row = (
                item["product"], item["lot_text"], _sale_date(item["sale_date"]), units,
                item["summary"], source_id, version, digest(item),
            )
            old = db.execute(
                "SELECT digest FROM sales_day WHERE product=? AND lot_text=? AND sale_date=?",
                row[:3],
            ).fetchone()
            if old:
                if old["digest"] != row[-1]:
                    conflict = "sales_day_conflict"
                    raise ValueError(conflict)
                continue
            prepared.append(row)
        db.execute(
            "INSERT INTO sales_source(source_id,version,digest,raw) VALUES (?,?,?,?)",
            (source_id, version, sha, raw),
        )
        db.executemany("INSERT INTO sales_day VALUES (?,?,?,?,?,?,?,?)", prepared)
        db.commit()
        return {"source": source_id, "version": version, "imported": len(prepared), "replayed": False}
    except Exception:
        db.rollback()
        if conflict:
            with db:
                db.execute(
                    "INSERT INTO sales_conflict(source_id,version,digest,reason) VALUES (?,?,?,?)",
                    (source_id, version, sha, conflict),
                )
        raise
    finally:
        db.close()


def import_closeouts(db_path, input_path):
    """Import close-out credentials: each credential attests that one complaint
    channel finished intake for one product/lot/sale day at ``closed_at``.  A
    sale day is only fully closed when every channel in CHANNELS has one."""
    raw = Path(input_path).read_bytes()
    doc = json.loads(raw.decode("utf-8"))
    source_id = doc.get("source_id")
    version = doc.get("version")
    credentials = doc.get("credentials")
    if not source_id or not isinstance(version, int) or version < 1 or not isinstance(credentials, list):
        raise ValueError("closeout_source_required")
    sha = hashlib.sha256(raw).hexdigest()
    db = connect(db_path)
    conflict = None
    try:
        db.execute("BEGIN IMMEDIATE")
        old_source = db.execute(
            "SELECT digest FROM closeout_source WHERE source_id=? AND version=?",
            (source_id, version),
        ).fetchone()
        if old_source:
            if old_source["digest"] == sha:
                db.commit()
                return {"source": source_id, "version": version, "imported": 0, "replayed": True}
            conflict = "closeout_source_conflict"
            raise ValueError(conflict)
        prepared = []
        for item in credentials:
            if any(not item.get(key) for key in ("id", "product", "lot_text", "sale_date", "channel", "closed_at")):
                raise ValueError("closeout_credential_required")
            if item["channel"] not in CHANNELS:
                raise ValueError("closeout_channel_unknown")
            closed_at = utc(item["closed_at"]).isoformat()
            row = (
                item["product"], item["lot_text"], _sale_date(item["sale_date"]), item["channel"],
                item["id"], closed_at, source_id, version, digest(item),
            )
            old = db.execute(
                "SELECT digest FROM closeout_credential WHERE product=? AND lot_text=? AND sale_date=? AND channel=?",
                row[:4],
            ).fetchone()
            if old:
                if old["digest"] != row[-1]:
                    conflict = "closeout_identity_conflict"
                    raise ValueError(conflict)
                continue
            other = db.execute(
                "SELECT 1 FROM closeout_credential WHERE credential_id=?", (item["id"],)
            ).fetchone()
            if other:
                conflict = "closeout_identity_conflict"
                raise ValueError(conflict)
            prepared.append(row)
        db.execute(
            "INSERT INTO closeout_source(source_id,version,digest,raw) VALUES (?,?,?,?)",
            (source_id, version, sha, raw),
        )
        db.executemany("INSERT INTO closeout_credential VALUES (?,?,?,?,?,?,?,?,?)", prepared)
        db.commit()
        return {"source": source_id, "version": version, "imported": len(prepared), "replayed": False}
    except Exception:
        db.rollback()
        if conflict:
            with db:
                db.execute(
                    "INSERT INTO closeout_conflict(source_id,version,digest,reason) VALUES (?,?,?,?)",
                    (source_id, version, sha, conflict),
                )
        raise
    finally:
        db.close()
