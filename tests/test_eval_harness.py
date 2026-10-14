"""Eval harness end to end on a tiny dataset with the LLM mocked; no network."""
import json

import pytest

import backend.graph as graph
from eval import categorization as ev
from tests.test_categorizer import FakeLLM

CSV = """external_id,date,description,amount
e1,2026-01-03,NETFLIX.COM,-15.49
e2,2026-02-03,NETFLIX.COM,-15.49
e3,2026-01-04,UBER *TRIP X1,-12.10
e4,2026-01-05,DOORDASH*SWEETGREEN,-23.40
e5,2026-01-06,ADOBE *CREATIVE CLD,-59.99
"""
TRUTH = {"seed": 7, "personas": {"solo": {"labels": {
    "e1": {"category": "entertainment", "merchant": "Netflix"},
    "e2": {"category": "entertainment", "merchant": "Netflix"},
    "e3": {"category": "transport", "merchant": "Uber"},
    "e4": {"category": "dining", "merchant": "Sweetgreen"},  # deliberately 'wrong' truth -> a confusion
    "e5": {"category": "shopping", "merchant": "Adobe", "acceptable_labels": ["shopping", "other"]},
}}}}


@pytest.fixture
def dataset(tmp_path, monkeypatch):
    d = tmp_path / "tiny"
    d.mkdir()
    (d / "solo.csv").write_text(CSV)
    (d / "ground_truth.json").write_text(json.dumps(TRUTH))
    monkeypatch.setattr(graph, "default_categorizer_llm", FakeLLM())
    monkeypatch.setenv("CATEGORIZER_MODEL", "openai/gpt-4o-mini")
    return d


def test_harness_metrics(dataset, tmp_path):
    db = tmp_path / "eval.db"
    db.write_text("stale")  # must be replaced, never reused
    res = ev.run_eval(dataset, use_cache=True, limit=None, db_path=db, version="cat-v1")
    c, d = res["summary"]["categorization"], res["details"]
    assert d["misalignment"]["echo_checked"] is None and d["cache_confirmation"] is None
    assert c["n"] == 5 and c["uncategorized"] == 0
    assert c["accuracy"] == 0.6 and c["lenient_accuracy"] == 0.8  # Adobe 'other' forgiven
    assert c["top_confusions"][0] == {"true": "dining", "predicted": "food_delivery", "count": 1}
    assert c["cache_hit_rate"] == 0.2 and d["source_split"]["from_cache"]["n"] == 1
    assert d["usage"]["llm_batches"] == 1 and d["usage"]["prompt_tokens"] == 100
    assert d["usage"]["estimated_cost"]["usd"] is not None
    assert c["per_persona"] == {"solo": 0.6} and res["summary"]["seed"] == 7
    md = ev.to_markdown(res)
    assert "Strict accuracy | 60.0%" in md and "Do not edit by hand" in md


def test_harness_no_cache_and_limit(dataset, tmp_path):
    res = ev.run_eval(dataset, use_cache=False, limit=3, db_path=tmp_path / "e.db")
    c = res["summary"]["categorization"]
    assert c["n"] == 3 and c["cache_hit_rate"] == 0.0 and res["details"]["mode"] == "no_cache"


def test_harness_cat_v2_reports_echo_and_confirmation(dataset, tmp_path):
    res = ev.run_eval(dataset, use_cache=True, limit=None, db_path=tmp_path / "e.db", version="cat-v2")
    d = res["details"]
    assert d["prompt_version"] == "cat-v2"
    assert d["misalignment"]["echo_checked"] == 5 and d["misalignment"]["echo_rejected"] == 0
    assert d["cache_confirmation"] == {"agreed": 1, "disagreed": 0}  # NETFLIX.COM x2
    assert res["summary"]["categorization"]["cache_hit_rate"] == 0.0  # 2-member key: both looked at
    assert "Echo-check rejections" in ev.to_markdown(res)


def test_dev_subset_is_stratified_and_stable():
    data = ev.load_dataset(ev.ROOT / "data" / "synthetic")
    data.pop("_seed")
    sub = ev.dev_subset(data)
    assert len(sub) == ev.DEV_TARGET and sub == ev.dev_subset(data)
    assert {k for k, _ in sub} == set(data)
    merchants = {(k, lab["merchant"]) for k, d in data.items() for lab in d["labels"].values()}
    picked = {(k, data[k]["labels"][e]["merchant"]) for k, e in sub}
    assert picked == merchants  # every merchant x persona present
