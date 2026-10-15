"""Analyzer detectors on small hand-built histories, the as_of cut-off, and the planted synthetic facts."""
import csv
import json
import random
from datetime import date, timedelta
from pathlib import Path

import pytest

from backend.agents.analyzer import analyze
from backend.ingest import ingest_csv
from backend.ledger import Txn, load_txns, merchant_root
from eval.analysis import score_persona

ROOT = Path(__file__).resolve().parents[1]
SYN = ROOT / "data" / "synthetic"


class Book:
    """Builds Txn lists with sequential ids."""

    def __init__(self):
        self.txns: list[Txn] = []

    def add(self, day: date, desc: str, cents: int, category: str | None = "other") -> Txn:
        t = Txn(len(self.txns) + 1, day, desc, cents, category, merchant_root(desc, cents))
        self.txns.append(t)
        return t

    def monthly(self, desc: str, dom: int, cents, months=range(12), category="entertainment") -> list[Txn]:
        amount = cents if callable(cents) else (lambda m: cents)
        return [self.add(_month(m).replace(day=dom), desc, amount(m), category) for m in months]


def _month(m: int) -> date:
    y, mo = divmod(9 + m, 12)  # m = 0 -> 2025-10
    return date(2025 + y, mo + 1, 1)


AS_OF = "2026-09"


def _of(res, type_, **params):
    return [p for p in res.patterns if p.type == type_ and all(p.params.get(k) == v for k, v in params.items())]


def test_merchant_root_groups_brand_variants():
    assert merchant_root("UBER *ONE MEMBERSHIP", -999) == merchant_root("CHECKCARD 1002 UBER *TRIP X1", -1200) == "UBER"
    assert merchant_root("DD *DOORDASH PHO 88", -2500) == merchant_root("DOORDASH DASHPASS 855-973-1040", -999) == "DOORDASH"
    assert merchant_root("WAL-MART #1234 AUSTIN TX", -5000) == merchant_root("WALMART+ MEMBERSHIP X", -1295) == "WALMART"
    assert merchant_root("THE HOME DEPOT #1234", -5000) == "HOME"
    assert merchant_root("AMZN Mktp US*X1", -100) != merchant_root("Amazon.com*X1", -100)  # alias: known limitation


def test_fixed_monthly_series_and_price_change():
    b = Book()
    b.monthly("NETFLIX.COM", 12, lambda m: -1549 if m < 6 else -1799)
    res = analyze(b.txns, AS_OF)
    [p] = _of(res, "recurring")
    assert p.params["price_changed"] and not p.params["amount_varies"] and p.params["active"]
    assert (p.params["prev_amount_cents"], p.params["amount_cents"]) == (-1549, -1799)
    assert p.params["price_change_date"] == "2026-04-12" and len(p.evidence) == 12
    assert not p.params["possibly_unused"]  # no other NETFLIX spend: streaming is never "unused"


def test_variable_bill_is_recurring():
    b = Book()
    rng = random.Random(1)
    b.monthly("PGANDE WEB ONLINE", 18, lambda m: -rng.randint(4500, 9500), category="utilities")
    [p] = _of(_res := analyze(b.txns, AS_OF), "recurring")
    assert p.params["amount_varies"] and p.params["n"] == 12


def test_random_purchases_are_not_recurring():
    b = Book()
    rng = random.Random(2)
    for d in range(0, 360, 3):
        b.add(date(2025, 10, 1) + timedelta(days=d), "STARBUCKS STORE 123", -rng.randint(400, 900), "dining")
    assert not _of(analyze(b.txns, AS_OF), "recurring")


def test_unused_membership_vs_used():
    b = Book()
    b.monthly("UBER *ONE MEMBERSHIP", 7, -999, category="transport")
    rng = random.Random(6)
    for m in range(3):  # several rides a month, stopping after 2025-12
        for day in sorted(rng.sample(range(1, 29), 4)):
            b.add(_month(m).replace(day=day), "UBER *TRIP X1", -rng.randint(900, 2800), "transport")
    b.monthly("DOORDASH DASHPASS", 20, -999, category="food_delivery")
    for m in range(12):  # orders every month
        for day in sorted(rng.sample(range(1, 29), 3)):
            b.add(_month(m).replace(day=day), "DOORDASH*PHO 88", -rng.randint(2000, 4500), "food_delivery")
    res = analyze(b.txns, AS_OF)
    [uber] = [p for p in _of(res, "recurring") if "ONE" in p.params["merchant"]]
    [dash] = [p for p in _of(res, "recurring") if "DASHPASS" in p.params["merchant"]]
    assert uber.params["possibly_unused"] and uber.params["days_since_parent_spend"] >= 90
    assert uber.params["parent_last_spend"] == max(t.day for t in b.txns if "TRIP" in t.description).isoformat()
    assert not dash.params["possibly_unused"]


def test_duplicate_charge_of_a_series_is_not_parent_spend():
    b = Book()
    geico = b.monthly("GEICO *AUTO X1", 11, -18240, category="transport")
    b.add(geico[8].day, "GEICO *AUTO X1", -18240, "transport")
    res = analyze(b.txns, AS_OF)
    [p] = _of(res, "recurring")
    assert not p.params["possibly_unused"] and p.params["days_since_parent_spend"] is None
    [d] = _of(res, "anomaly", kind="duplicate")
    assert d.params["related_txn_id"] == geico[8].id


def test_spike_and_duplicate_floor():
    b = Book()
    rng = random.Random(3)
    for m in range(10):  # several shops a month on random days
        for day in rng.sample(range(1, 29), 3):
            b.add(_month(m).replace(day=day), "TRADER JOE S #123 OAKLAND CA", -rng.randint(4000, 7000), "groceries")
    big = b.add(date(2026, 6, 13), "TRADER JOE S #123 OAKLAND CA", -41237, "groceries")
    b.add(date(2026, 3, 3), "BLUE BOTTLE COFFEE", -450, "dining")
    b.add(date(2026, 3, 3), "BLUE BOTTLE COFFEE", -450, "dining")  # under $10: not a duplicate
    res = analyze(b.txns, AS_OF)
    assert [p.params["txn_id"] for p in _of(res, "anomaly", kind="spike")] == [big.id]
    assert not _of(res, "anomaly", kind="duplicate")


def test_new_large_merchant_needs_warmup():
    b = Book()
    rng = random.Random(4)
    for d in range(0, 360, 2):
        b.add(date(2025, 10, 1) + timedelta(days=d), f"SHOP{d % 7} STORE", -rng.randint(1000, 6000), "shopping")
    early = b.add(date(2025, 10, 20), "SOFA WORLD", -150000, "shopping")  # inside the 60-day warm-up
    late = b.add(date(2026, 8, 21), "BESTBUY 123", -124999, "shopping")
    flagged = [p.params["txn_id"] for p in _of(analyze(b.txns, AS_OF), "anomaly", kind="new_merchant")]
    assert late.id in flagged and early.id not in flagged


def test_trend_rising_and_flat_control():
    b = Book()
    rng = random.Random(5)
    for m in range(12):
        for _ in range(5 + m):  # rising order count
            b.add(_month(m).replace(day=rng.randint(1, 28)), "DOORDASH*PHO", -rng.randint(2000, 4000), "food_delivery")
        for _ in range(6):  # flat
            b.add(_month(m).replace(day=rng.randint(1, 28)), "SAFEWAY 1", -rng.randint(3000, 6000), "groceries")
    res = analyze(b.txns, AS_OF)
    [t] = _of(res, "trend")
    assert t.params["category"] == "food_delivery" and t.params["slope_cents_per_month"] > 0
    assert t.params["latest_month_cents"] == t.params["monthly_totals"]["2026-09"]
    assert t.params["prev_3mo_mean_cents"] == round(sum(t.params["monthly_totals"][m]
                                                        for m in ("2026-06", "2026-07", "2026-08")) / 3)


def test_detectors_respect_as_of(conn):
    rows = ["external_id,date,description,amount"]
    rows += [f"r{m}{d},{_month(m).replace(day=d)},TRADER JOE S #1 OAKLAND CA,-{50 + m + d}.00"
             for m in range(12) for d in (3, 11, 24)]
    rows += ["late,2026-09-20,TRADER JOE S #1 OAKLAND CA,-900.00"]  # a spike, but after as_of 2026-08
    batch = ingest_csv(conn, "t.csv", "\n".join(rows) + "\n", name="t")
    txns = load_txns(conn, batch["user_id"], "2026-08")
    assert max(t.day for t in txns) <= date(2026, 8, 31) and len(txns) == 33
    late_id = conn.execute("SELECT id FROM transactions WHERE external_id = 'late'").fetchone()[0]
    full = load_txns(conn, batch["user_id"], "2026-09")
    for res in (analyze(txns, "2026-08"), analyze(full, "2026-08")):  # the function also cuts at as_of
        assert all(late_id not in p.evidence for p in res.patterns)
        assert res.n_txns == 33
    assert any(p.params.get("txn_id") == late_id for p in analyze(full, "2026-09").patterns)


def test_no_ground_truth_in_backend_code():
    for path in (ROOT / "backend").rglob("*.py"):
        text = path.read_text()
        assert "ground_truth" not in text and "data/synthetic" not in text, path


# ---- planted facts on the synthetic data (ground-truth labels stand in for the LLM categories) ----

def _persona(key: str):
    truth = json.loads((SYN / "ground_truth.json").read_text())["personas"][key]
    txns, ext = [], {}
    with open(SYN / f"{key}.csv") as f:
        for i, r in enumerate(csv.DictReader(f), start=1):
            cents = round(float(r["amount"]) * 100)
            txns.append(Txn(i, date.fromisoformat(r["date"]), r["description"], cents,
                            truth["labels"][r["external_id"]]["category"], merchant_root(r["description"], cents)))
            ext[i] = r["external_id"]
    return score_persona(key, analyze(txns, AS_OF).patterns, ext, truth)


@pytest.fixture(scope="module")
def synthetic():
    return {k: _persona(k) for k in ("maya_renter", "ortiz_family", "sam_freelancer")}


@pytest.mark.parametrize("kind", ["price_change", "unused", "trend", "spike"])
def test_planted_must_haves_found(synthetic, kind):
    assert not [x for r in synthetic.values() for x in r["types"][kind]["fn"]]


def test_planted_recurring_mostly_found(synthetic):
    tp = sum(len(r["types"]["recurring"]["tp"]) for r in synthetic.values())
    fn = sum(len(r["types"]["recurring"]["fn"]) for r in synthetic.values())
    assert tp / (tp + fn) >= 0.9


def test_used_memberships_and_flat_delivery_not_flagged(synthetic):
    controls = [c for r in synthetic.values() for c in r["controls"]]
    assert len(controls) == 4 and all(c["passed"] for c in controls)
