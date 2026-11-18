"""Offline quality complaint intake, separate from official recall notices."""
import hashlib
import json
from pathlib import Path

from .source import digest, utc
from .store import connect


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
            row = (
                item["id"], source_id, version, item["channel"], item["reported_at"],
                item["product"], item["lot_text"], item["defect_code"], item["case_ref"], digest(item),
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
            "INSERT INTO complaint_record VALUES (?,?,?,?,?,?,?,?,?,?)", prepared
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
