"""Planted facts must be verifiable from the output files alone (CSV + ground_truth.json)."""
import csv
import json
import re
from collections import defaultdict
from datetime import date
from pathlib import Path
from statistics import median

import pytest

from backend.models import CATEGORIES
from eval.synthetic.generate import DEFAULT_SEED, OUT_DIR, generate

ROOT = Path(__file__).resolve().parents[1]
END = date(2026, 9, 30)


@pytest.fixture(scope="module")
def out(tmp_path_factory):
    d = tmp_path_factory.mktemp("synthetic")
    generate(DEFAULT_SEED, d)
    return d


@pytest.fixture(scope="module")
def data(out):
    truth = json.loads((out / "ground_truth.json").read_text())
    meta = json.loads((out / "personas.json").read_text())
    rows = {}
    for key, m in meta.items():
        with open(out / m["csv"]) as f:
            rows[key] = [
                {**r, "date": date.fromisoformat(r["date"]), "cents": round(float(r["amount"]) * 100)}
                for r in csv.DictReader(f)
            ]
    return truth, rows


def _labelled(truth, rows, key):
    labels = truth["personas"][key]["labels"]
    return [{**r, **labels[r["external_id"]]} for r in rows[key]]


def _files(d):
    return {p.name: p.read_bytes() for p in sorted(d.iterdir())}


# ---- reproducibility ----

def test_same_seed_same_bytes(out, tmp_path):
    generate(DEFAULT_SEED, tmp_path)
    assert _files(tmp_path) == _files(out)


def test_different_seed_differs(out, tmp_path):
    generate(DEFAULT_SEED + 1, tmp_path)
    assert _files(tmp_path) != _files(out)


def test_committed_files_match_generator(out):
    """data/synthetic must be exactly what the script produces: nothing hand-edited."""
    assert _files(OUT_DIR) == _files(out)


# ---- labels ----

def test_every_row_labelled_with_valid_category(data):
    truth, rows = data
    seen = set()
    for key in rows:
        ids = [r["external_id"] for r in rows[key]]
        assert len(ids) == len(set(ids))
        assert set(ids) == set(truth["personas"][key]["labels"])
        seen |= {lab["category"] for lab in truth["personas"][key]["labels"].values()}
    assert seen == set(CATEGORIES), "every category should be exercised"


def test_descriptors_are_messy(data):
    truth, rows = data
    by_merchant = defaultdict(list)
    for key in rows:
        for r in _labelled(truth, rows, key):
            desc = r["description"].lower()
            assert not any(c in desc for c in ("food_delivery", "groceries", "entertainment")), desc
            assert not re.search(r"\d{13,}", desc), "looks like a card number"
            by_merchant[r["merchant"]].append(r["description"])
    frequent = {m: set(d) for m, d in by_merchant.items() if len(d) >= 10}
    varied = [m for m, d in frequent.items() if len(d) >= 2]
    assert len(varied) >= 0.8 * len(frequent)
    all_desc = [r["description"] for k in rows for r in rows[k]]
    assert any(d.startswith(("POS PURCHASE", "CHECKCARD", "PURCHASE AUTHORIZED")) for d in all_desc)


# ---- recurring + unused ----

def test_recurring_series_present_and_monthly(data):
    truth, rows = data
    for key in rows:
        by_id = {r["external_id"]: r for r in _labelled(truth, rows, key)}
        for s in truth["personas"][key]["recurring"]:
            txns = [by_id[i] for i in s["txn_ids"]]
            assert len(txns) == 12
            assert {t["merchant"] for t in txns} == {s["merchant"]}
            gaps = [(b["date"] - a["date"]).days for a, b in zip(txns, txns[1:])]
            assert all(27 <= g <= 35 for g in gaps), (s["merchant"], gaps)
            assert txns[-1]["cents"] == s["amount_cents"]


def test_price_changes_planted(data):
    truth, _ = data
    hikes = {(k, s["merchant"]): s["price_change"]
             for k, p in truth["personas"].items() for s in p["recurring"] if s["price_change"]}
    assert hikes[("maya_renter", "Spotify")] == {"effective": "2026-03-03", "from_cents": -1199, "to_cents": -1299}
    assert hikes[("ortiz_family", "Netflix")]["to_cents"] == -1799


def test_unused_memberships_have_no_recent_parent_spend(data):
    truth, rows = data
    window = truth["unused_window_days"]
    flags = []
    for key in rows:
        labelled = _labelled(truth, rows, key)
        for s in truth["personas"][key]["recurring"]:
            if not s["membership_of"]:
                continue
            parent = [r["date"] for r in labelled if r["merchant"] in s["membership_of"] and r["cents"] < 0]
            unused = not parent or (END - max(parent)).days > window
            assert unused == s["unused"], (key, s["merchant"])
            assert s["last_seen"] >= "2026-09-01", "still being charged at the end"
            flags.append(unused)
        assert truth["personas"][key]["unused_subscriptions"], f"{key} has no unused plant"
    assert True in flags and False in flags, "need used memberships as negatives too"


# ---- anomalies ----

def test_anomalies_present(data):
    truth, rows = data
    for key in rows:
        labelled = _labelled(truth, rows, key)
        by_id = {r["external_id"]: r for r in labelled}
        kinds = set()
        for a in truth["personas"][key]["anomalies"]:
            t = by_id[a["flagged_txn_id"]]
            assert t["date"].isoformat() == a["date"] and t["cents"] == a["amount_cents"]
            prior = [abs(r["cents"]) for r in labelled
                     if r["merchant"] == t["merchant"] and r["date"] < t["date"]]
            if a["kind"] == "spike":
                assert len(prior) >= 4 and abs(t["cents"]) >= 3 * median(prior)
            elif a["kind"] == "duplicate":
                orig, dup = (by_id[i] for i in a["txn_ids"])
                assert (orig["date"], orig["description"], orig["cents"]) == (dup["date"], dup["description"], dup["cents"])
            elif a["kind"] == "new_merchant":
                assert not prior
            kinds.add(a["kind"])
        assert kinds == {"spike", "duplicate", "new_merchant"}


# ---- trend ----

def _delivery_totals(truth, rows, key):
    totals = defaultdict(int)
    for r in _labelled(truth, rows, key):
        if r["category"] == "food_delivery" and r["cents"] < 0:
            totals[r["date"].strftime("%Y-%m")] -= r["cents"]
    return [totals[m] for m in sorted(totals)]


def test_rising_delivery_trend_only_in_maya(data):
    truth, rows = data
    trend = truth["personas"]["maya_renter"]["trends"][0]
    vals = _delivery_totals(truth, rows, "maya_renter")
    assert vals == list(trend["monthly_totals_cents"].values())
    assert sum(vals[-3:]) >= 1.8 * sum(vals[:3]) and trend["slope_cents_per_month"] > 0
    for control in ("ortiz_family", "sam_freelancer"):
        assert truth["personas"][control]["trends"] == []
        v = _delivery_totals(truth, rows, control)
        assert 0.5 <= sum(v[-3:]) / sum(v[:3]) <= 1.5, control


# ---- isolation ----

def test_backend_never_reads_ground_truth():
    for p in (ROOT / "backend").rglob("*.py"):
        text = p.read_text()
        assert "ground_truth" not in text and "eval.synthetic" not in text, p
