import json
from pathlib import Path

import pytest

from recallcase.complaints import import_complaints
from recallcase.signals import complaint_watch
from recallcase.report import report


FIXTURE = Path(__file__).parents[1] / "fixtures" / "complaint_normal.json"


def test_complaint_source_retry_and_exact_case_count(tmp_path):
    db = tmp_path / "complaints.db"
    assert import_complaints(db, FIXTURE)["imported"] == 3
    assert import_complaints(db, FIXTURE)["replayed"] is True
    signal = complaint_watch(db, threshold=2)["signals"][0]
    assert signal["independent"] == 2
    assert signal["watch"] is True
    view = report(db)
    assert len(view["complaint_evidence"]) == 1
    assert len(view["auxiliary"]["complaint_record"]) == 3


def test_conflicting_source_version_does_not_rewrite_complaints(tmp_path):
    db = tmp_path / "complaints.db"
    import_complaints(db, FIXTURE)
    changed = json.loads(FIXTURE.read_text(encoding="utf-8"))
    changed["records"][0]["lot_text"] = "L200"
    file = tmp_path / "changed.json"
    file.write_text(json.dumps(changed), encoding="utf-8")
    with pytest.raises(ValueError, match="complaint_source_conflict"):
        import_complaints(db, file)
    view = report(db)
    assert view["auxiliary"]["complaint_record"][0]["lot_text"] == "L100"
    assert view["auxiliary"]["complaint_conflict"][0]["reason"] == "complaint_source_conflict"
