"""Exact-reference complaint counts for an offline early-warning view."""
from .store import connect


def complaint_watch(db_path, threshold=3):
    if not isinstance(threshold, int) or threshold < 1:
        raise ValueError("invalid_watch_threshold")
    db = connect(db_path)
    try:
        rows = db.execute(
            "SELECT product,lot_text,defect_code,COUNT(DISTINCT case_ref) AS independent "
            "FROM complaint_record GROUP BY product,lot_text,defect_code "
            "ORDER BY product,lot_text,defect_code"
        ).fetchall()
        signals = [
            {"product": row["product"], "lot": row["lot_text"], "defect": row["defect_code"],
             "independent": row["independent"], "watch": row["independent"] >= threshold}
            for row in rows
        ]
        return {"threshold": threshold, "signals": signals}
    finally:
        db.close()
