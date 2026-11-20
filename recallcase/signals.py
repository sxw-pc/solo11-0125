"""Exact-reference complaint counts for an offline early-warning view, plus
sales-exposure complaint-rate monitoring under pre-registered charters.

Two independent charters share one pre-registered baseline rate and one
whole-period false-alarm budget, each spending it its own way:

- ``early`` looks once per sale day.  Day ``d`` of the period may spend only
  ``budget / (d * (d + 1))`` on an exact one-sided binomial tail test over the
  cumulative closed evidence, so the whole period never exceeds the budget.
  A day is only judged once days 1..d are fully closed by both complaint
  channels, and an issued investigation is never retracted when later sales
  dilute the cumulative rate.
- ``week`` waits until every sale day of the period is fully closed and then
  spends the whole budget on a single pooled test; it never picks the best
  looking day after the fact.

Both only decide whether a quality investigation opens; they never touch
recall notices, inventory quarantine or manufacturer acknowledgements.
"""
import json
from math import exp, lgamma, log, log1p

from .complaints import CHANNELS
from .source import canonical, digest, utc
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


def binom_tail(n, p, k):
    """Exact one-sided upper tail P[X >= k] for X ~ Binomial(n, p)."""
    if k <= 0:
        return 1.0
    if k > n:
        return 0.0
    if p <= 0.0:
        return 0.0
    if p >= 1.0:
        return 1.0
    if k > n * p:
        return _upper_tail(n, p, k)
    return min(1.0, max(0.0, 1.0 - _lower_tail(n, p, k - 1)))


def _log_pmf(n, p, i):
    return (lgamma(n + 1) - lgamma(i + 1) - lgamma(n - i + 1)
            + i * log(p) + (n - i) * log1p(-p))


def _upper_tail(n, p, k):
    term = exp(_log_pmf(n, p, k))
    total = term
    i = k
    while i < n and term > 0.0:
        term *= (n - i) / (i + 1) * (p / (1.0 - p))
        i += 1
        total += term
        if term < total * 1e-17:
            break
    return min(total, 1.0)


def _lower_tail(n, p, k):
    """P[X <= k] summed upward from zero."""
    if k < 0:
        return 0.0
    term = exp(_log_pmf(n, p, 0))
    total = term
    for i in range(k):
        term *= (n - i) / (i + 1) * (p / (1.0 - p))
        total += term
    return min(total, 1.0)


def define_charter(db_path, product, baseline_rate, budget, at, period_days=7):
    """Pre-register the monitoring charter for one product: baseline complaint
    rate and whole-period false-alarm budget.  Must exist before the first
    sales day of the product; rewriting it afterwards is a conflict."""
    if not product or not isinstance(product, str):
        raise ValueError("charter_product_required")
    if not isinstance(baseline_rate, float) or not 0.0 < baseline_rate < 1.0:
        raise ValueError("charter_baseline_invalid")
    if not isinstance(budget, float) or not 0.0 < budget < 1.0:
        raise ValueError("charter_budget_invalid")
    if not isinstance(period_days, int) or isinstance(period_days, bool) or period_days < 1:
        raise ValueError("charter_period_invalid")
    defined_at = utc(at).isoformat()
    fingerprint = digest({"product": product, "baseline_rate": baseline_rate, "budget": budget,
                          "period_days": period_days, "defined_at": defined_at})
    db = connect(db_path)
    try:
        db.execute("BEGIN IMMEDIATE")
        old = db.execute("SELECT * FROM watch_charter WHERE product=?", (product,)).fetchone()
        if old:
            if (old["baseline_rate"] != baseline_rate or old["budget"] != budget
                    or old["period_days"] != period_days):
                raise ValueError("watch_charter_conflict")
            db.commit()
            return {"product": product, "baseline_rate": old["baseline_rate"],
                    "budget": old["budget"], "period_days": old["period_days"],
                    "defined_at": old["defined_at"], "replayed": True}
        if db.execute("SELECT 1 FROM sales_day WHERE product=? LIMIT 1", (product,)).fetchone():
            raise ValueError("watch_charter_after_sales")
        db.execute(
            "INSERT INTO watch_charter VALUES (?,?,?,?,?,?)",
            (product, baseline_rate, budget, period_days, defined_at, fingerprint),
        )
        db.commit()
        return {"product": product, "baseline_rate": baseline_rate, "budget": budget,
                "period_days": period_days, "defined_at": defined_at, "replayed": False}
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def evaluate_watch(db_path, at):
    """Run one monitoring pass at decision time ``at``.

    Refreshes the review queue (held records/cases/days), then appends
    decisions for newly judgeable days.  Stored decisions and issued
    investigations are never rewritten; rerunning on unchanged evidence
    appends nothing.
    """
    moment = utc(at).isoformat()
    db = connect(db_path)
    try:
        db.execute("BEGIN IMMEDIATE")
        charters = {row["product"]: dict(row) for row in db.execute("SELECT * FROM watch_charter")}
        sales = {(r["product"], r["lot_text"], r["sale_date"]): dict(r)
                 for r in db.execute("SELECT * FROM sales_day")}
        closeouts = {(r["product"], r["lot_text"], r["sale_date"], r["channel"]): dict(r)
                     for r in db.execute("SELECT * FROM closeout_credential")}
        signed = _signed_cases(db)
        holds, case_info, by_case = _refresh_holds(db, charters, sales, closeouts, signed)
        db.executemany("INSERT OR IGNORE INTO watch_review VALUES (?,?,?,?,?,?,?,?)", holds)
        held_cases, held_records, held_days = _held_sets(db)
        decisions = []
        pending = []
        for product in sorted(charters):
            charter = charters[product]
            period = charter["period_days"]
            lots = sorted({lot for (p, lot, _date) in sales if p == product})
            for lot in lots:
                days = sorted((d for (p, l, _date), d in sales.items() if p == product and l == lot),
                              key=lambda d: d["sale_date"])[:period]
                valid_by_date = {}
                for case_ref, info in case_info.items():
                    p, l, sold_on = info
                    if p != product or l != lot or case_ref in held_cases:
                        continue
                    if not any(r["id"] not in held_records for r in by_case[case_ref]):
                        continue
                    valid_by_date.setdefault(sold_on, []).append(case_ref)
                prefix = 0
                for day in days:
                    if _day_blocker(product, lot, day, closeouts, held_days):
                        break
                    prefix += 1
                existing = {(r["charter"], r["day_index"]) for r in db.execute(
                    "SELECT charter,day_index FROM watch_decision WHERE product=? AND lot_text=?",
                    (product, lot))}
                for d in range(1, prefix + 1):
                    if ("early", d) in existing:
                        continue
                    decisions.append(_judge(
                        db, charter, product, lot, days[:d], d, valid_by_date, closeouts,
                        share=charter["budget"] / (d * (d + 1)),
                        consumed=charter["budget"] * d / (d + 1),
                        charter_kind="early", moment=moment))
                if period <= prefix and ("week", period) not in existing:
                    decisions.append(_judge(
                        db, charter, product, lot, days[:period], period, valid_by_date, closeouts,
                        share=charter["budget"], consumed=charter["budget"],
                        charter_kind="week", moment=moment))
                for idx in range(prefix + 1, len(days) + 1):
                    day = days[idx - 1]
                    reason = _day_blocker(product, lot, day, closeouts, held_days) if idx == prefix + 1 \
                        else "blocked_by_prior_day"
                    pending.append({"product": product, "lot": lot, "sale_date": day["sale_date"],
                                    "day_index": idx, "reason": reason})
                if len(days) < period:
                    pending.append({"product": product, "lot": lot, "sale_date": None,
                                    "day_index": len(days) + 1, "reason": "sales_day_missing"})
        db.commit()
        investigations = [dict(r) for r in db.execute(
            "SELECT * FROM watch_investigation ORDER BY product,lot_text,charter")]
        review = [dict(r) for r in db.execute("SELECT * FROM watch_review ORDER BY hold_key")]
        return {"evaluated_at": moment, "decisions": decisions, "investigations": investigations,
                "review": review, "pending": pending}
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _day_blocker(product, lot, day, closeouts, held_days):
    """Why a sale day cannot be judged yet, or None when it is judgeable."""
    key = (product, lot, day["sale_date"])
    missing = [c for c in CHANNELS if key + (c,) not in closeouts]
    if missing:
        return "awaiting_closeout:" + ",".join(missing)
    if key in held_days:
        return "held:" + held_days[key]
    return None


def _judge(db, charter, product, lot, window, day_index, valid_by_date, closeouts,
           share, consumed, charter_kind, moment):
    numerator = sum(len(valid_by_date.get(day["sale_date"], ())) for day in window)
    denominator = sum(day["units"] for day in window)
    tail = binom_tail(denominator, charter["baseline_rate"], numerator)
    decision = "open" if tail <= share else "no_anomaly"
    evidence = {
        "charter_digest": charter["digest"],
        "baseline_rate": charter["baseline_rate"],
        "budget": charter["budget"],
        "sales_sources": [{"source_id": s, "version": v} for s, v in sorted(
            {(day["source_id"], day["source_version"]) for day in window})],
        "closeout_credentials": sorted(
            closeouts[(product, lot, day["sale_date"], channel)]["credential_id"]
            for day in window for channel in CHANNELS),
        "days": [{"sale_date": day["sale_date"], "units": day["units"],
                  "cases": sorted(valid_by_date.get(day["sale_date"], ()))} for day in window],
    }
    cur = db.execute(
        "INSERT INTO watch_decision(product,lot_text,charter,day_index,numerator,denominator,"
        "tail_probability,budget_share,budget_consumed,baseline_rate,decision,evaluated_at,evidence)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (product, lot, charter_kind, day_index, numerator, denominator, tail, share, consumed,
         charter["baseline_rate"], decision, moment, canonical(evidence)))
    if decision == "open":
        db.execute(
            "INSERT OR IGNORE INTO watch_investigation VALUES (?,?,?,?,?,?)",
            (product, lot, charter_kind, moment, day_index, cur.lastrowid))
    return {"product": product, "lot": lot, "charter": charter_kind, "day_index": day_index,
            "numerator": numerator, "denominator": denominator, "tail_probability": tail,
            "budget_share": share, "budget_consumed": consumed, "decision": decision,
            "evaluated_at": moment}


def _hold(reason, product, lot, sale_date, case_ref, record_id, detail):
    key = "|".join([reason, record_id, case_ref, product, lot, sale_date])
    return (key, product, lot, sale_date, case_ref, record_id, reason, canonical(detail))


def _refresh_holds(db, charters, sales, closeouts, signed):
    """Derive the review queue from current evidence.

    Case-level holds quarantine the whole case_ref; record-level holds
    quarantine single records (a case still counts while one clean record
    remains); day-level holds block the sale day from signed-off statistics.
    """
    by_case = {}
    for row in db.execute("SELECT * FROM complaint_record"):
        by_case.setdefault(row["case_ref"], []).append(dict(row))
    holds = []
    case_info = {}
    for case_ref in sorted(by_case):
        recs = by_case[case_ref]
        monitored = sorted({r["product"] for r in recs} & set(charters))
        if not monitored:
            continue
        variants = sorted({(r["product"], r["lot_text"]) for r in recs})
        sold_ons = sorted({r["sold_on"] or "" for r in recs})
        if len(variants) > 1 or len(sold_ons) > 1:
            holds.append(_hold("lot_attribution_conflict", monitored[0], "", "", case_ref, "",
                               {"variants": [list(v) for v in variants], "sold_on": sold_ons}))
            continue
        product, lot = variants[0]
        sold_on = sold_ons[0] or None
        if product not in charters:
            continue
        if not sold_on:
            holds.append(_hold("sale_day_unattributed", product, lot, "", case_ref, "",
                               {"cause": "missing sold_on"}))
            continue
        day = sales.get((product, lot, sold_on))
        if day is None:
            holds.append(_hold("sale_day_unattributed", product, lot, sold_on, case_ref, "",
                               {"cause": "no matching sales day"}))
            continue
        case_info[case_ref] = (product, lot, sold_on)
        if day["units"] == 0:
            holds.append(_hold("zero_sales_attribution", product, lot, sold_on, case_ref, "",
                               {"units": 0}))
            continue
        for r in recs:
            credential = closeouts.get((product, lot, sold_on, r["channel"]))
            if credential and utc(r["reported_at"]) > utc(credential["closed_at"]):
                holds.append(_hold("late_after_closeout", product, lot, sold_on, case_ref, r["id"],
                                   {"channel": r["channel"], "reported_at": r["reported_at"],
                                    "closed_at": credential["closed_at"]}))
            elif (product, lot, sold_on) in signed and case_ref not in signed[(product, lot, sold_on)]:
                holds.append(_hold("signed_off_before_import", product, lot, sold_on, case_ref, r["id"],
                                   {"channel": r["channel"]}))
    held_cases = {h[4] for h in holds if not h[5] and h[4]}
    held_records = {h[5] for h in holds if h[5]}
    for (product, lot, sale_date), day in sorted(sales.items()):
        if product not in charters:
            continue
        if day["units"] == 0:
            holds.append(_hold("zero_sales_day", product, lot, sale_date, "", "", {"units": 0}))
            continue
        valid = [case_ref for case_ref, info in case_info.items()
                 if info == (product, lot, sale_date) and case_ref not in held_cases
                 and any(r["id"] not in held_records for r in by_case[case_ref])]
        if len(valid) > day["units"]:
            holds.append(_hold("complaints_exceed_sales", product, lot, sale_date, "", "",
                               {"units": day["units"], "cases": sorted(valid)}))
    return holds, case_info, by_case


def _held_sets(db):
    held_cases, held_records, held_days = set(), set(), {}
    for row in db.execute("SELECT * FROM watch_review"):
        if row["record_id"]:
            held_records.add(row["record_id"])
        elif row["case_ref"]:
            held_cases.add(row["case_ref"])
        else:
            held_days[(row["product"], row["lot_text"], row["sale_date"])] = row["reason"]
    return held_cases, held_records, held_days


def _signed_cases(db):
    """Case_refs frozen into signed-off statistics per sale day, first
    decision to cover the day wins."""
    signed = {}
    for row in db.execute("SELECT product,lot_text,evidence FROM watch_decision ORDER BY id"):
        for day in json.loads(row["evidence"])["days"]:
            key = (row["product"], row["lot_text"], day["sale_date"])
            if key not in signed:
                signed[key] = set(day["cases"])
    return signed


def watch_days_view(db):
    """Read-only per-sale-day status for the report.  A day that is not fully
    closed shows ``signed_cases=None``: unobserved complaints are not proven
    zero complaints."""
    closeouts = {(r["product"], r["lot_text"], r["sale_date"], r["channel"]): dict(r)
                 for r in db.execute("SELECT * FROM closeout_credential")}
    signed = _signed_cases(db)
    held_days = {}
    for row in db.execute("SELECT * FROM watch_review WHERE record_id='' AND case_ref=''"):
        held_days[(row["product"], row["lot_text"], row["sale_date"])] = row["reason"]
    view = []
    for day in db.execute("SELECT * FROM sales_day ORDER BY product,lot_text,sale_date"):
        key = (day["product"], day["lot_text"], day["sale_date"])
        channels = {}
        for channel in CHANNELS:
            credential = closeouts.get(key + (channel,))
            channels[channel] = {
                "closed": credential is not None,
                "closed_at": credential["closed_at"] if credential else None,
                "credential_id": credential["credential_id"] if credential else None,
            }
        cases = signed.get(key)
        view.append({
            "product": day["product"], "lot": day["lot_text"], "sale_date": day["sale_date"],
            "units": day["units"], "channels": channels,
            "fully_closed": all(c["closed"] for c in channels.values()),
            "hold": held_days.get(key),
            "signed": key in signed,
            "signed_cases": len(cases) if cases is not None else None,
        })
    return view
