import json
import subprocess
import sys
from pathlib import Path

import pytest

from recallcase.adoption import adopt
from recallcase.complaints import import_complaints, import_closeouts, import_sales
from recallcase.report import report
from recallcase.signals import binom_tail, complaint_watch, define_charter, evaluate_watch
from recallcase.store import connect

FIX = Path(__file__).resolve().parents[1] / "fixtures"
T0 = "2026-05-31T00:00:00Z"


def load(name):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


def write(tmp_path, name, doc):
    path = tmp_path / name
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def base_series(db, tmp_path, sales=None, complaints=None, closeouts=None):
    define_charter(db, "P1", 0.001, 0.05, T0)
    import_sales(db, write(tmp_path, "sales.json", sales if sales is not None else load("sales_normal.json")))
    import_complaints(db, write(tmp_path, "complaints.json",
                                complaints if complaints is not None else load("complaint_rated.json")))
    import_closeouts(db, write(tmp_path, "closeouts.json",
                               closeouts if closeouts is not None else load("closeout_normal.json")))


def test_binom_tail_reference_values():
    assert binom_tail(1000, 0.001, 5) == pytest.approx(0.00364, abs=1e-4)
    assert binom_tail(7000, 0.001, 5) == pytest.approx(0.827, abs=1e-3)
    assert binom_tail(1000, 0.001, 0) == 1.0
    assert binom_tail(5, 0.5, 6) == 0.0
    assert binom_tail(10, 0.5, 10) == pytest.approx(0.5 ** 10)


def test_early_charter_opens_day_one_and_week_charter_does_not(tmp_path):
    db = tmp_path / "watch.db"
    define_charter(db, "P1", 0.001, 0.05, T0)
    import_sales(db, FIX / "sales_normal.json")
    import_complaints(db, FIX / "complaint_rated.json")
    closeouts = load("closeout_normal.json")
    day_one = write(tmp_path, "co1.json", {**closeouts, "credentials": closeouts["credentials"][:2]})
    import_closeouts(db, day_one)

    first = evaluate_watch(db, "2026-06-02T00:00:00Z")
    assert [(d["charter"], d["day_index"], d["decision"]) for d in first["decisions"]] == [
        ("early", 1, "open")]
    day1 = first["decisions"][0]
    assert day1["numerator"] == 5  # six records, five independent case_refs
    assert day1["denominator"] == 1000
    assert day1["tail_probability"] == pytest.approx(0.00364, abs=1e-4)
    assert day1["budget_share"] == pytest.approx(0.05 / 2)
    assert day1["budget_consumed"] == pytest.approx(0.05 / 2)
    assert first["review"] == []
    assert len(first["investigations"]) == 1
    assert first["investigations"][0]["charter"] == "early"
    assert first["investigations"][0]["day_index"] == 1
    assert first["investigations"][0]["issued_at"] == "2026-06-02T00:00:00+00:00"
    pending = {p["sale_date"]: p["reason"] for p in first["pending"]}
    assert pending["2026-06-02"] == "awaiting_closeout:hotline,retailer"
    assert pending["2026-06-07"] == "blocked_by_prior_day"

    rest = write(tmp_path, "co2.json",
                 {**closeouts, "version": 2, "credentials": closeouts["credentials"][2:]})
    import_closeouts(db, rest)
    second = evaluate_watch(db, "2026-06-08T00:00:00Z")
    assert len(second["decisions"]) == 7  # early days 2..7 plus the pooled week decision
    early = {d["day_index"]: d for d in second["decisions"] if d["charter"] == "early"}
    assert set(early) == {2, 3, 4, 5, 6, 7}
    assert all(d["decision"] == "no_anomaly" for d in early.values())
    assert early[7]["numerator"] == 5 and early[7]["denominator"] == 7000
    assert early[7]["tail_probability"] == pytest.approx(0.827, abs=1e-3)
    assert early[7]["budget_share"] == pytest.approx(0.05 / 56)
    assert early[7]["budget_consumed"] == pytest.approx(0.05 * 7 / 8)
    week = next(d for d in second["decisions"] if d["charter"] == "week")
    assert week["decision"] == "no_anomaly"
    assert (week["numerator"], week["denominator"]) == (5, 7000)
    assert week["tail_probability"] == pytest.approx(0.827, abs=1e-3)
    assert week["budget_share"] == 0.05 and week["budget_consumed"] == 0.05
    # Later sales dilute the cumulative rate, but the issued investigation stands.
    assert second["investigations"] == first["investigations"]
    assert second["pending"] == []

    third = evaluate_watch(db, "2026-06-09T00:00:00Z")
    assert third["decisions"] == []
    assert third["investigations"] == first["investigations"]

    view = report(db)
    assert len(view["watch_decisions"]) == 8
    stored_day1 = next(d for d in view["watch_decisions"]
                       if d["charter"] == "early" and d["day_index"] == 1)
    assert stored_day1["numerator"] == 5 and stored_day1["denominator"] == 1000
    assert stored_day1["tail_probability"] == pytest.approx(0.00364, abs=1e-4)
    assert stored_day1["baseline_rate"] == 0.001
    assert stored_day1["evaluated_at"] == "2026-06-02T00:00:00+00:00"
    assert stored_day1["evidence"]["sales_sources"] == [
        {"source_id": "offline-sales-2026-06", "version": 1}]
    assert stored_day1["evidence"]["closeout_credentials"] == ["CO-0601-hotline", "CO-0601-retailer"]
    assert stored_day1["evidence"]["days"][0]["cases"] == ["ref-a", "ref-b", "ref-c", "ref-d", "ref-e"]
    assert view["watch_investigations"][0]["charter"] == "early"
    assert view["watch_charters"][0]["baseline_rate"] == 0.001
    days = {d["sale_date"]: d for d in view["watch_days"]}
    assert days["2026-06-01"]["signed"] is True and days["2026-06-01"]["signed_cases"] == 5
    assert days["2026-06-07"]["signed"] is True and days["2026-06-07"]["signed_cases"] == 0

    con = connect(db)
    assert con.execute("SELECT COUNT(*) FROM watch_decision").fetchone()[0] == 8
    assert con.execute("SELECT COUNT(*) FROM watch_investigation").fetchone()[0] == 1
    con.close()


def test_case_ref_deduped_across_channels_in_both_views(tmp_path):
    db = tmp_path / "watch.db"
    base_series(db, tmp_path)
    signal = complaint_watch(db, threshold=5)["signals"][0]
    assert signal["independent"] == 5  # ref-a arrived via hotline and retailer
    result = evaluate_watch(db, "2026-06-08T00:00:00Z")
    day1 = next(d for d in result["decisions"] if d["charter"] == "early" and d["day_index"] == 1)
    assert day1["numerator"] == 5


def test_missing_one_channel_closeout_blocks_day_and_week(tmp_path):
    db = tmp_path / "watch.db"
    closeouts = load("closeout_normal.json")
    kept = [c for c in closeouts["credentials"]
            if not (c["sale_date"] == "2026-06-02" and c["channel"] == "retailer")]
    base_series(db, tmp_path, closeouts={**closeouts, "credentials": kept})
    result = evaluate_watch(db, "2026-06-08T00:00:00Z")
    # Only the fully closed day 1 is judged; the week charter stays silent.
    assert [(d["charter"], d["day_index"]) for d in result["decisions"]] == [("early", 1)]
    assert result["decisions"][0]["decision"] == "open"
    pending = {(p["sale_date"], p["reason"]) for p in result["pending"]}
    assert ("2026-06-02", "awaiting_closeout:retailer") in pending
    assert ("2026-06-03", "blocked_by_prior_day") in pending
    view = report(db)
    day2 = next(d for d in view["watch_days"] if d["sale_date"] == "2026-06-02")
    assert day2["channels"]["hotline"]["closed"] is True
    assert day2["channels"]["retailer"]["closed"] is False
    assert day2["signed"] is False and day2["signed_cases"] is None
    assert not any(d["charter"] == "week" for d in view["watch_decisions"])

    late = write(tmp_path, "co2.json", {**closeouts, "version": 2, "credentials": [
        c for c in closeouts["credentials"]
        if c["sale_date"] == "2026-06-02" and c["channel"] == "retailer"]})
    import_closeouts(db, late)
    unblocked = evaluate_watch(db, "2026-06-09T00:00:00Z")
    week = next(d for d in unblocked["decisions"] if d["charter"] == "week")
    assert week["decision"] == "no_anomaly"
    assert unblocked["pending"] == []


def test_late_complaint_is_held_not_counted(tmp_path):
    db = tmp_path / "watch.db"
    base_series(db, tmp_path)
    evaluate_watch(db, "2026-06-08T00:00:00Z")
    late = {"source_id": "offline-complaints-2026-06-rated", "version": 2, "records": [
        {"id": "R-301", "channel": "hotline", "reported_at": "2026-06-02T10:00:00+08:00",
         "product": "P1", "lot_text": "L100", "defect_code": "seal",
         "case_ref": "ref-late", "sold_on": "2026-06-01"}]}
    import_complaints(db, write(tmp_path, "late.json", late))
    result = evaluate_watch(db, "2026-06-10T00:00:00Z")
    assert result["decisions"] == []
    holds = [r for r in result["review"] if r["reason"] == "late_after_closeout"]
    assert len(holds) == 1
    assert holds[0]["record_id"] == "R-301" and holds[0]["case_ref"] == "ref-late"
    view = report(db)
    day1 = next(d for d in view["watch_decisions"] if d["charter"] == "early" and d["day_index"] == 1)
    assert day1["numerator"] == 5
    assert view["watch_days"][0]["signed_cases"] == 5
    again = evaluate_watch(db, "2026-06-11T00:00:00Z")
    assert [r for r in again["review"] if r["reason"] == "late_after_closeout"] == holds


def test_zero_sales_day_and_attribution_are_held(tmp_path):
    db = tmp_path / "watch.db"
    sales = load("sales_normal.json")
    sales["days"][1]["units"] = 0
    complaints = load("complaint_rated.json")
    complaints["records"].append(
        {"id": "R-401", "channel": "hotline", "reported_at": "2026-06-02T09:00:00+08:00",
         "product": "P1", "lot_text": "L100", "defect_code": "seal",
         "case_ref": "ref-zero", "sold_on": "2026-06-02"})
    base_series(db, tmp_path, sales=sales, complaints=complaints)
    result = evaluate_watch(db, "2026-06-08T00:00:00Z")
    assert [(d["charter"], d["day_index"]) for d in result["decisions"]] == [("early", 1)]
    reasons = {r["reason"] for r in result["review"]}
    assert "zero_sales_day" in reasons and "zero_sales_attribution" in reasons
    pending = {p["sale_date"]: p["reason"] for p in result["pending"]}
    assert pending["2026-06-02"] == "held:zero_sales_day"
    assert not any(d["charter"] == "week" for d in result["decisions"])
    view = report(db)
    day2 = next(d for d in view["watch_days"] if d["sale_date"] == "2026-06-02")
    assert day2["hold"] == "zero_sales_day" and day2["signed"] is False


def test_complaints_exceeding_sales_hold_the_day(tmp_path):
    db = tmp_path / "watch.db"
    sales = load("sales_normal.json")
    sales["days"][0]["units"] = 3  # five independent cases cannot come from three units
    base_series(db, tmp_path, sales=sales)
    result = evaluate_watch(db, "2026-06-08T00:00:00Z")
    assert result["decisions"] == []
    holds = [r for r in result["review"] if r["reason"] == "complaints_exceed_sales"]
    assert len(holds) == 1 and holds[0]["sale_date"] == "2026-06-01"
    pending = {p["sale_date"]: p["reason"] for p in result["pending"]}
    assert pending["2026-06-01"] == "held:complaints_exceed_sales"


def test_lot_attribution_conflict_holds_the_case(tmp_path):
    db = tmp_path / "watch.db"
    complaints = load("complaint_rated.json")
    complaints["records"].append(
        {"id": "R-501", "channel": "retailer", "reported_at": "2026-06-01T10:15:00+08:00",
         "product": "P1", "lot_text": "L200", "defect_code": "seal",
         "case_ref": "ref-b", "sold_on": "2026-06-01"})
    base_series(db, tmp_path, complaints=complaints)
    result = evaluate_watch(db, "2026-06-08T00:00:00Z")
    holds = [r for r in result["review"] if r["reason"] == "lot_attribution_conflict"]
    assert len(holds) == 1 and holds[0]["case_ref"] == "ref-b"
    day1 = next(d for d in result["decisions"] if d["charter"] == "early" and d["day_index"] == 1)
    assert day1["numerator"] == 4  # ref-b quarantined, not counted


def test_on_time_record_arriving_after_signoff_is_held(tmp_path):
    db = tmp_path / "watch.db"
    closeouts = load("closeout_normal.json")
    base_series(db, tmp_path, closeouts={**closeouts, "credentials": closeouts["credentials"][:2]})
    first = evaluate_watch(db, "2026-06-02T00:00:00Z")
    assert first["decisions"][0]["numerator"] == 5
    extra = {"source_id": "offline-complaints-2026-06-rated", "version": 2, "records": [
        {"id": "R-601", "channel": "retailer", "reported_at": "2026-06-01T10:00:00+08:00",
         "product": "P1", "lot_text": "L100", "defect_code": "seal",
         "case_ref": "ref-f", "sold_on": "2026-06-01"}]}
    import_complaints(db, write(tmp_path, "extra.json", extra))
    result = evaluate_watch(db, "2026-06-03T00:00:00Z")
    holds = [r for r in result["review"] if r["reason"] == "signed_off_before_import"]
    assert len(holds) == 1 and holds[0]["case_ref"] == "ref-f"
    view = report(db)
    day1 = next(d for d in view["watch_decisions"] if d["charter"] == "early" and d["day_index"] == 1)
    assert day1["numerator"] == 5  # signed-off statistic is frozen


def test_charter_preregistration_rules(tmp_path):
    db = tmp_path / "late.db"
    import_sales(db, FIX / "sales_normal.json")
    with pytest.raises(ValueError, match="watch_charter_after_sales"):
        define_charter(db, "P1", 0.001, 0.05, T0)

    db2 = tmp_path / "ok.db"
    charter = define_charter(db2, "P1", 0.001, 0.05, T0)
    assert charter["replayed"] is False
    assert define_charter(db2, "P1", 0.001, 0.05, T0)["replayed"] is True
    with pytest.raises(ValueError, match="watch_charter_conflict"):
        define_charter(db2, "P1", 0.002, 0.05, T0)


def test_file_replay_and_evaluation_idempotency(tmp_path):
    db = tmp_path / "watch.db"
    define_charter(db, "P1", 0.001, 0.05, T0)
    for path, importer in ((FIX / "sales_normal.json", import_sales),
                           (FIX / "complaint_rated.json", import_complaints),
                           (FIX / "closeout_normal.json", import_closeouts)):
        assert importer(db, path)["replayed"] is False
        assert importer(db, path)["replayed"] is True
        assert importer(db, path)["imported"] == 0
    first = evaluate_watch(db, "2026-06-08T00:00:00Z")
    second = evaluate_watch(db, "2026-06-08T00:00:00Z")
    assert second["decisions"] == []
    assert second["investigations"] == first["investigations"]
    assert second["review"] == first["review"]
    con = connect(db)
    assert con.execute("SELECT COUNT(*) FROM sales_day").fetchone()[0] == 7
    assert con.execute("SELECT COUNT(*) FROM closeout_credential").fetchone()[0] == 14
    assert con.execute("SELECT COUNT(*) FROM complaint_record").fetchone()[0] == 6
    con.close()


def test_unattributed_records_are_held_and_watch_view_untouched(tmp_path):
    db = tmp_path / "watch.db"
    define_charter(db, "P1", 0.001, 0.05, T0)
    import_sales(db, FIX / "sales_normal.json")
    import_complaints(db, FIX / "complaint_normal.json")  # no sold_on on these records
    import_closeouts(db, FIX / "closeout_normal.json")
    result = evaluate_watch(db, "2026-06-08T00:00:00Z")
    held = {r["case_ref"] for r in result["review"] if r["reason"] == "sale_day_unattributed"}
    assert held == {"ref-a", "ref-b"}
    day1 = next(d for d in result["decisions"] if d["charter"] == "early" and d["day_index"] == 1)
    assert day1["numerator"] == 0 and day1["decision"] == "no_anomaly"
    signal = complaint_watch(db, threshold=2)["signals"][0]
    assert signal["independent"] == 2 and signal["watch"] is True


def test_monitoring_does_not_touch_recall_pipeline(tmp_path):
    db = tmp_path / "state.db"
    assert adopt(db, FIX / "recall_normal.json")["state"] == "active"
    base_series(db, tmp_path)
    evaluate_watch(db, "2026-06-08T00:00:00Z")
    con = connect(db)
    assert con.execute("SELECT state FROM recall_notice").fetchone()[0] == "active"
    assert con.execute("SELECT COUNT(*) FROM lineage_assessment").fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM withdrawal_obligation").fetchone()[0] == 0
    con.close()


def test_cli_watch_flow(tmp_path):
    repo = Path(__file__).resolve().parents[1]
    db = tmp_path / "cli.db"

    def run(*args):
        out = subprocess.run([sys.executable, "-m", "recallcase", *args],
                             cwd=repo, capture_output=True, text=True)
        assert out.returncode == 0, out.stderr
        return json.loads(out.stdout)

    run("watch-charter", "--db", str(db), "--product", "P1",
        "--baseline", "0.001", "--budget", "0.05", "--at", T0)
    run("sales-import", "--db", str(db), "--input", str(FIX / "sales_normal.json"))
    run("complaint-import", "--db", str(db), "--input", str(FIX / "complaint_rated.json"))
    run("closeout-import", "--db", str(db), "--input", str(FIX / "closeout_normal.json"))
    result = run("watch-evaluate", "--db", str(db), "--at", "2026-06-08T00:00:00Z")
    day1 = next(d for d in result["decisions"] if d["charter"] == "early" and d["day_index"] == 1)
    assert day1["decision"] == "open"
    week = next(d for d in result["decisions"] if d["charter"] == "week")
    assert week["decision"] == "no_anomaly"
    view = run("report", "--db", str(db))
    assert view["watch_investigations"][0]["charter"] == "early"
    assert len(view["watch_decisions"]) == 8
