"""Forecaster: engine arithmetic, current-price recurring, trend line floor, as_of cut-off."""
import math
from datetime import date

import pytest

from backend.agents.analyzer import Detected, analyze
from backend.agents.forecaster import Flow, ForecastInputs, forecast, project
from tests.test_analyzer import AS_OF, Book, _month


def test_engine_balance_and_band_grow_with_sqrt_months():
    inputs = ForecastInputs("2026-09", 100000, (
        Flow("income", "income", "income", "inflows", (50000,) * 4, 10000),
        Flow("rent", "recurring", "housing", "pattern:1", (-20000,) * 4, 0),
    ))
    pts = project(inputs, 4)
    assert [p["month"] for p in pts] == ["2026-10", "2026-11", "2026-12", "2027-01"]
    assert [p["balance_p50"] for p in pts] == [130000, 160000, 190000, 220000]
    assert pts[0]["balance_p90"] - pts[0]["balance_p50"] == round(1.2816 * 10000)
    assert pts[3]["balance_p50"] - pts[3]["balance_p10"] == round(1.2816 * 10000 * math.sqrt(4))
    assert (pts[0]["income_cents"], pts[0]["spend_cents"], pts[0]["by_category"]) == (50000, 20000, {"housing": 20000})
    with pytest.raises(ValueError):
        project(inputs, 13)


def test_recurring_projected_at_current_price_and_start_balance_respects_as_of():
    b = Book()
    b.monthly("NETFLIX.COM", 12, lambda m: -1549 if m < 6 else -1799)
    for m in range(12):
        b.add(_month(m).replace(day=1), "ACME PAYROLL", 300000, "income")
    b.add(date(2026, 10, 5), "LATE CHARGE", -99999, "shopping")  # after as_of: ignored
    res = forecast(b.txns, analyze(b.txns, AS_OF).patterns, 50000, AS_OF, horizon=3)
    assert res.inputs.start_balance_cents == 50000 + 12 * 300000 - 6 * 1549 - 6 * 1799
    netflix = next(f for f in res.inputs.flows if f.kind == "recurring")
    assert netflix.means == (-1799,) * 3 and netflix.sd_cents == 0
    assert res.points[0]["by_category"]["entertainment"] == 1799
    assert res.points[0]["income_cents"] == 300000 and "shopping" not in res.points[0]["by_category"]


def test_inactive_series_and_anomalies_are_not_projected():
    b = Book()
    b.monthly("GYM CLUB", 2, -4900, months=range(6), category="fitness")  # cancelled after 2026-03
    spike = b.add(date(2026, 9, 3), "COSTCO WHSE 1", -120000, "groceries")
    a = Detected("anomaly", "spike", {"txn_id": spike.id, "kind": "spike"}, [spike.id])
    res = forecast(b.txns, analyze(b.txns, AS_OF).patterns + [a], 0, AS_OF, horizon=1)
    assert not [f for f in res.inputs.flows if f.kind == "recurring"]
    assert res.points[0]["by_category"].get("groceries", 0) == 0


def test_trend_line_continues_and_is_floored_at_zero():
    b = Book()
    for m in range(12):  # falling delivery spend: 120 -> 10 dollars
        b.add(_month(m).replace(day=9), f"DOORDASH*PHO {m}", -(12000 - 1000 * m), "food_delivery")
    totals = {_month(m).strftime("%Y-%m"): 12000 - 1000 * m for m in range(12)}
    trend = Detected("trend", "t", {"category": "food_delivery", "monthly_totals": totals}, [])
    res = forecast(b.txns, [trend], 0, AS_OF, horizon=4)
    assert res.trend_adjusted == ["food_delivery"]
    assert [p["by_category"]["food_delivery"] for p in res.points] == [0, 0, 0, 0]  # line would go negative
    flat = forecast(b.txns, [trend], 0, AS_OF, horizon=1, trend_adjust=False)
    assert flat.points[0]["by_category"]["food_delivery"] == round((3000 + 2000 + 1000) / 3)


def test_one_off_allowance_is_trailing_mean_of_flagged_spend():
    b = Book()
    for m in range(12):
        b.add(_month(m).replace(day=3), "SAFEWAY 1", -5000, "groceries")
    one_offs = [b.add(date(2026, 5, 9), "AIRBNB X", -60000, "travel"), b.add(date(2026, 8, 21), "BESTBUY 1", -120000, "shopping")]
    pats = [Detected("anomaly", "x", {"txn_id": t.id, "kind": "new_merchant"}, [t.id]) for t in one_offs]
    v2 = forecast(b.txns, pats, 0, AS_OF, horizon=2)
    [flow] = [f for f in v2.inputs.flows if f.kind == "one_off"]
    window = [0, 0, -60000, 0, 0, -120000]  # 2026-04 .. 2026-09
    assert flow.means == (round(sum(window) / 6),) * 2 and flow.sd_cents > 0
    assert v2.points[0]["by_category"]["one_off"] == 30000 and v2.params()["version"] == "fc-v2"
    v1 = forecast(b.txns, pats, 0, AS_OF, horizon=2, version="fc-v1")
    assert not [f for f in v1.inputs.flows if f.kind == "one_off"]
    assert v1.points[0]["spend_cents"] == v2.points[0]["spend_cents"] - 30000
    with pytest.raises(ValueError):
        forecast(b.txns, pats, 0, AS_OF, version="fc-v9")


def test_first_charge_of_a_new_series_flagged_as_anomaly_is_not_in_the_one_off_allowance(tmp_path):
    """A new rent's first charge is flagged new_merchant and is also the start of a recurring series: the recurring
    flow already projects it, so the one-off allowance must not count it a second time (bug fix, no new version)."""
    from backend.db import connect
    from backend.ledger import load_txns
    from tests.ledger_fixture import build_ledger
    from tests.test_goals import add_rent

    path = tmp_path / "t.db"
    add_rent(path, uid := build_ledger(path))
    conn = connect(path)
    txns = load_txns(conn, uid, "2026-08")
    conn.close()
    res = analyze(txns, "2026-08")
    rent = next(p for p in res.patterns if p.type == "recurring" and "ELM" in p.params["merchant"])
    flagged = [p for p in res.patterns if p.type == "anomaly" and p.params["txn_id"] in rent.evidence]
    assert flagged and flagged[0].params["kind"] == "new_merchant"  # the case this test is about
    fc = forecast(txns, res.patterns, 100000, "2026-08")
    allowance = next(f for f in fc.inputs.flows if f.kind == "one_off")
    assert allowance.means[0] == 0 and allowance.sd_cents == 0  # no other anomalies in this ledger
    assert next(f for f in fc.inputs.flows if f.source_ref == f"pattern:{res.patterns.index(rent)}").means[0] == -350000
