from eval.metrics import Rec, calibration, confusions, lenient_accuracy, macro_f1, per_category, source_split, strict_accuracy


def r(truth, pred, alts=(), conf=0.9, cache=False):
    return Rec(truth, frozenset({truth, *alts}), pred, conf if pred else None, cache if pred else None)


RECS = [
    r("dining", "dining"), r("dining", "food_delivery"), r("food_delivery", "food_delivery", cache=True),
    r("other", "shopping", alts=["shopping"], conf=0.6), r("transport", None),
]


def test_strict_and_lenient():
    assert strict_accuracy(RECS) == 0.4
    assert lenient_accuracy(RECS) == 0.6  # Adobe-style alternate forgiven; uncategorized never is


def test_per_category_and_macro_f1():
    s = per_category(RECS)
    assert set(s) == {"dining", "food_delivery", "other", "shopping", "transport"}
    assert s["dining"] == {"precision": 1.0, "recall": 0.5, "f1": 0.6667, "support": 2}
    assert s["food_delivery"]["precision"] == 0.5 and s["food_delivery"]["recall"] == 1.0
    assert s["transport"] == {"precision": 0.0, "recall": 0.0, "f1": 0.0, "support": 1}
    assert s["shopping"]["support"] == 0
    assert macro_f1(s) == round((0.6667 + 0.6667 + 0 + 0 + 0) / 5, 4)


def test_confusions_sorted_with_lenient_flag():
    c = confusions(RECS + [r("dining", "food_delivery")])
    assert c[0] == {"true": "dining", "predicted": "food_delivery", "count": 2, "lenient_ok": 0}
    assert {"true": "other", "predicted": "shopping", "count": 1, "lenient_ok": 1} in c
    assert all(x["predicted"] is not None for x in c)


def test_calibration_buckets():
    b = {x["bucket"]: x for x in calibration(RECS)}
    assert b["[0.90, 0.95)"]["n"] == 3 and b["[0.90, 0.95)"]["accuracy"] == round(2 / 3, 4)
    assert b["[0.50, 0.70)"]["accuracy"] == 0.0 and b["[0.95, 1.00]"]["n"] == 0


def test_source_split():
    s = source_split(RECS)
    assert s["cache_hit_rate"] == 0.25 and s["from_cache"]["n"] == 1 and s["from_llm"]["n"] == 3
