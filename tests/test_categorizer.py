"""Categorizer with the LLM mocked: batching, retries, cache, duplicates, closed taxonomy."""
import json
import threading

import pytest

from backend.agents import categorizer as cat
from backend.agents.categorizer import MerchantCache, TxnIn, categorize, merchant_key
from backend.llm.client import Completion
from backend.models import CATEGORIES

MODEL = "openai/gpt-4o-mini"


class FakeLLM:
    """Answers from a descriptor-substring rule table. `script` lets a test misbehave per call."""

    def __init__(self, rules=None, confidence=0.95, script=None, echo=None):
        self.rules = rules or {"UBER *TRIP": "transport", "UBER *EATS": "food_delivery",
                               "DOORDASH": "food_delivery", "SWEETGREEN": "dining", "NETFLIX": "entertainment"}
        self.confidence = confidence
        self.echo = echo  # descriptor -> merchant echo; default echoes the descriptor (always aligned)
        self.script = list(script or [])
        self.calls: list[list[dict]] = []
        self.lock = threading.Lock()

    def answer(self, item):
        cat_ = next((c for k, c in self.rules.items() if k in item["descriptor"]), "other")
        conf = self.confidence(item) if callable(self.confidence) else self.confidence
        out = {"id": item["id"], "category": cat_, "confidence": conf, "rationale": "rule"}
        if self.with_merchant:
            out["merchant"] = self.echo(item["descriptor"]) if self.echo else item["descriptor"]
        return out

    def __call__(self, messages, response_format=None):
        props = (response_format or {}).get("json_schema", {}).get("schema", {}).get("properties", {})
        self.with_merchant = "merchant" in props.get("results", {}).get("items", {}).get("properties", {})
        items = json.loads(messages[1]["content"])
        with self.lock:
            self.calls.append(items)
            hook = self.script.pop(0) if self.script else None
        results = [self.answer(i) for i in items]
        text = hook(results) if hook else json.dumps({"results": results})
        return Completion(text=text, model="gpt-4o-mini-2024-07-18", prompt_tokens=100,
                          completion_tokens=10 * len(items), fallback_triggered=False)


def txns(*descs, start=1):
    return [TxnIn(start + i, d, -1000) for i, d in enumerate(descs)]


def run(conn, items, llm, **kw):
    """Core tests pin cat-v1 so its behaviour stays reproducible; cat-v2 tests are at the bottom."""
    kw.setdefault("version", "cat-v1")
    return categorize(items, MerchantCache(conn), llm, model=MODEL, **kw)


# ---- merchant key ----

@pytest.mark.parametrize("a,b", [
    ("CHECKCARD 1002 UBER *TRIP JW25MAZ4AA", "UBER *TRIP Q8OTTJ2X"),
    ("POS PURCHASE SAFEWAY STORE 1211", "SAFEWAY STORE 4009"),
    ("AMAZON.COM*KF9JL9D2", "Amazon.com*ELH3MTSP"),
    ("COMCAST CABLE COMM 800-266-2278", "COMCAST CABLE COMM"),
])
def test_key_strips_noise(a, b):
    assert merchant_key(a, -100) == merchant_key(b, -100)


@pytest.mark.parametrize("a,b", [
    ("UBER *TRIP JW25MAZ4AA", "UBER *EATS J4FVBHHI"),
    ("CHECKCARD 1031 DOORDASH SWEETGREEN 855-973-1040", "SWEETGREEN OAKLAND"),
    ("STARBUCKS 66812", "WALGREENS #36330"),  # long merchant words are never dropped
])
def test_key_keeps_context(a, b):
    assert merchant_key(a, -100) != merchant_key(b, -100)


def test_key_sign_and_too_short():
    assert merchant_key("VENMO PAYMENT 96014", 500) != merchant_key("VENMO PAYMENT 96014", -500)
    assert merchant_key("AB 12345", -100) is None


# ---- batching ----

def test_batches_and_maps_back_by_local_id(conn):
    llm = FakeLLM()
    items = txns(*[f"NETFLIX {chr(65 + i)}{chr(65 + i)}XQ" for i in range(7)], "UBER *TRIP X1")
    res = run(conn, items, llm, batch_size=3)
    assert [len(c) for c in sorted(llm.calls, key=len)] == [2, 3, 3]
    assert {c["id"] for c in llm.calls[0]} <= {"t01", "t02", "t03"}
    got = {lb.txn_id: lb.category for lb in res.labels}
    assert got[8] == "transport" and all(got[i] == "entertainment" for i in range(1, 8))
    assert all("descriptor" in i and "external_id" not in i for c in llm.calls for i in c)


def test_amount_sent_as_exact_dollars(conn):
    llm = FakeLLM()
    run(conn, [TxnIn(1, "NETFLIX", -1549), TxnIn(2, "PAYROLL", 218540)], llm)
    assert sorted(i["amount_usd"] for i in llm.calls[0]) == ["-15.49", "2185.40"]


# ---- retries: invalid output is never accepted ----

def test_unparseable_response_retries_whole_batch(conn):
    llm = FakeLLM(script=[lambda r: "not json"])
    res = run(conn, txns("NETFLIX", "UBER *TRIP"), llm)
    assert len(llm.calls) == 2 and len(res.labels) == 2
    assert res.batches[0].attempts == 2 and "unparseable response" in res.batches[0].problems


def test_dropped_item_is_retried_alone(conn):
    llm = FakeLLM(script=[lambda r: json.dumps({"results": r[:1]})])
    res = run(conn, txns("NETFLIX", "UBER *TRIP"), llm)
    assert [len(c) for c in llm.calls] == [2, 1] and len(res.labels) == 2
    assert res.batches[0].retried_items == 1


def test_category_outside_taxonomy_rejected(conn):
    def bad(r):
        r[0]["category"] = "subscriptions"
        return json.dumps({"results": r})
    llm = FakeLLM(script=[bad])
    res = run(conn, txns("NETFLIX"), llm)
    assert len(llm.calls) == 2 and res.labels[0].category == "entertainment"
    assert {lb.category for lb in res.labels} <= set(CATEGORIES)


def test_confidence_out_of_range_rejected(conn):
    llm = FakeLLM(script=[lambda r: json.dumps({"results": [{**r[0], "confidence": 1.7}]})])
    res = run(conn, txns("NETFLIX"), llm)
    assert len(llm.calls) == 2 and res.labels[0].confidence == 0.95


def test_duplicate_ids_agreeing_kept_once_conflicting_retried(conn):
    llm = FakeLLM(script=[lambda r: json.dumps({"results": r + [r[0]]})])
    res = run(conn, txns("NETFLIX", "UBER *TRIP"), llm)
    assert len(llm.calls) == 1 and len(res.labels) == 2

    def conflict(r):
        return json.dumps({"results": r + [{**r[0], "category": "shopping"}]})
    llm = FakeLLM(script=[conflict])
    res = run(conn, txns("NETFLIX X", "UBER *TRIP X", start=10), llm)
    assert [len(c) for c in llm.calls] == [2, 1]
    assert {lb.category for lb in res.labels} == {"entertainment", "transport"}


def test_unknown_ids_ignored(conn):
    llm = FakeLLM(script=[lambda r: json.dumps({"results": r + [{**r[0], "id": "t99"}]})])
    res = run(conn, txns("NETFLIX"), llm)
    assert len(res.labels) == 1 and "unknown id t99" in res.batches[0].problems


def test_gives_up_after_max_attempts_never_defaults(conn):
    llm = FakeLLM(script=[lambda r: "{}"] * 3)
    res = run(conn, txns("NETFLIX"), llm)
    assert len(llm.calls) == cat.MAX_ATTEMPTS
    assert res.labels == [] and res.failed_txn_ids == [1] and res.batches[0].failed_items == 1


def test_llm_exception_counts_as_attempt(conn):
    def boom(r):
        raise TimeoutError
    llm = FakeLLM(script=[boom])
    res = run(conn, txns("NETFLIX"), llm)
    assert len(res.labels) == 1 and "llm error: TimeoutError" in res.batches[0].problems


# ---- cache ----

def test_one_llm_look_per_key_rest_from_cache(conn):
    llm = FakeLLM()
    res = run(conn, txns("NETFLIX.COM", "NETFLIX.COM", "UBER *TRIP A1", "UBER *TRIP B2"), llm)
    assert sum(len(c) for c in llm.calls) == 2
    assert [lb.from_cache for lb in res.labels] == [False, True, False, True]
    assert (res.cache_hits, res.llm_items) == (2, 2)


def test_cache_persists_across_runs_and_records_from_cache(conn):
    run(conn, txns("NETFLIX.COM"), FakeLLM())
    llm = FakeLLM()
    res = run(conn, txns("NETFLIX.COM", start=5), llm)
    assert llm.calls == [] and res.labels[0].from_cache and res.labels[0].category == "entertainment"


def test_low_confidence_not_cached_members_get_own_look(conn):
    llm = FakeLLM(confidence=0.6)
    res = run(conn, txns("MYSTERY SHOP", "MYSTERY SHOP", "MYSTERY SHOP"), llm)
    assert sum(len(c) for c in llm.calls) == 3 and not any(lb.from_cache for lb in res.labels)
    assert conn.execute("SELECT COUNT(*) FROM merchant_cache").fetchone()[0] == 0


def test_cache_ignored_for_other_prompt_version_or_model(conn):
    run(conn, txns("NETFLIX.COM", "NETFLIX.COM"), FakeLLM())
    llm = FakeLLM()
    categorize(txns("NETFLIX.COM"), MerchantCache(conn), llm, model="openai/gpt-4o", version="cat-v1")
    assert len(llm.calls) == 1
    llm = FakeLLM()
    run(conn, txns("NETFLIX.COM"), llm, version="cat-v2")
    assert len(llm.calls) == 1


def test_keyless_descriptors_never_cached(conn):
    llm = FakeLLM()
    res = run(conn, txns("AB 1", "AB 1"), llm)
    assert sum(len(c) for c in llm.calls) == 2 and not any(lb.from_cache for lb in res.labels)


def test_failed_representative_members_still_tried(conn):
    llm = FakeLLM(script=[lambda r: "{}"] * 3)
    res = run(conn, txns("NETFLIX.COM", "NETFLIX.COM"), llm)
    assert res.failed_txn_ids == [1] and [lb.txn_id for lb in res.labels] == [2]
    assert not res.labels[0].from_cache


def test_no_cache_mode_every_txn_gets_own_look(conn):
    llm = FakeLLM()
    res = categorize(txns("NETFLIX.COM", "NETFLIX.COM"), None, llm, model=MODEL)
    assert sum(len(c) for c in llm.calls) == 2 and not any(lb.from_cache for lb in res.labels)
    assert conn.execute("SELECT COUNT(*) FROM merchant_cache").fetchone()[0] == 0


# ---- cat-v2: merchant echo check + confirm-before-cache ----

def v2(conn, items, llm, **kw):
    return categorize(items, MerchantCache(conn), llm, model=MODEL, version="cat-v2", **kw)


def test_v2_prompt_differs_from_v1_only_by_logged_changes():
    v1, v2_ = cat.VERSIONS["cat-v1"].system_prompt, cat.VERSIONS["cat-v2"].system_prompt
    assert "e.g. software subscriptions" in v1 and "e.g. software subscriptions" not in v2_
    assert "streaming and memberships are never other" in v2_
    assert "SQ*/TST* indicate the card processor only, not delivery or a category" in v2_
    assert "8. Merchant: copy the merchant words exactly" in v2_ and "9. Rationale" in v2_
    item = cat.VERSIONS["cat-v2"].response_format["json_schema"]["schema"]["properties"]["results"]["items"]
    assert item["required"] == ["id", "merchant", "category", "confidence", "rationale"]


@pytest.mark.parametrize("merchant,desc,ok", [
    ("UBER *TRIP", "CHECKCARD 1002 UBER *TRIP JW25MAZ4AA", True),
    ("Trader Joe's", "TRADER JOE S #501 OAKLAND CA", True),
    ("BLUE BTL COF", "SQ *BLUE BTL COF 6979", True),
    ("PGANDE", "PGANDE WEB ONLINE 10186876", True),
    ("LAKEVIEW LN SRV", "COSTCO WHSE #7574 ROUND ROCK", False),  # neighbour's merchant
    ("POS PURCHASE", "POS PURCHASE CHIPOTLE ONLINE 6560", False),  # generic words don't count
    ("", "NETFLIX.COM", False),
])
def test_merchant_in_descriptor(merchant, desc, ok):
    assert cat.merchant_in_descriptor(merchant, desc) is ok


def test_v2_misaligned_answer_rejected_and_retried(conn):
    calls = {"n": 0}

    def swap_first_call(desc):  # first call echoes a neighbour's merchant for the Costco row
        return "LAKEVIEW LN SRV" if "COSTCO" in desc and calls["n"] == 0 else desc

    llm = FakeLLM(echo=swap_first_call, script=[lambda r: (calls.__setitem__("n", 1), json.dumps({"results": r}))[1]])
    res = v2(conn, txns("COSTCO WHSE #7574", "LAKEVIEW LN SRV MTG PYMT"), llm)
    assert [len(c) for c in llm.calls] == [2, 1] and len(res.labels) == 2
    b = res.batches[0]
    assert (b.echo_checked, b.echo_rejected) == (3, 1) and "echo mismatch t01" in b.problems


def test_v2_second_look_in_separate_shuffled_batch_then_cached(conn):
    llm = FakeLLM()
    items = txns("NETFLIX.COM", "NETFLIX.COM 2", "NETFLIX.COM", "UBER *TRIP A1", "UBER *TRIP B2", "SWEETGREEN")
    res = v2(conn, items, llm)
    reps, confirms = llm.calls
    assert sorted(i["descriptor"] for i in reps) == ["NETFLIX.COM", "SWEETGREEN", "UBER *TRIP A1"]
    # confirmer = another member, preferring a different descriptor variant
    assert sorted(i["descriptor"] for i in confirms) == ["NETFLIX.COM 2", "UBER *TRIP B2"]
    assert (res.confirmed_keys, res.disagreed_keys) == (2, 0)
    assert [lb.from_cache for lb in res.labels] == [False, False, True, False, False, False]
    keys = {r[0] for r in conn.execute("SELECT merchant_key FROM merchant_cache")}
    assert keys == {"out|NETFLIX.COM", "out|UBER*TRIP"}  # singleton SWEETGREEN not cached


def test_v2_disagreement_not_cached_members_get_own_look(conn):
    llm = FakeLLM(rules={"222": "other", "SPOTIFY": "entertainment"})  # same key, answers differ
    res = v2(conn, txns("SPOTIFY 111", "SPOTIFY 222", "SPOTIFY 111", "SPOTIFY 111"), llm)
    assert (res.confirmed_keys, res.disagreed_keys) == (0, 1)
    assert sum(len(c) for c in llm.calls) == 4 and not any(lb.from_cache for lb in res.labels)
    assert conn.execute("SELECT COUNT(*) FROM merchant_cache").fetchone()[0] == 0


def test_v2_confirmer_order_shuffled_with_fixed_seed(conn):
    names = [f"SHOP{chr(65 + i // 26)}{chr(65 + i % 26)}" for i in range(30)]
    descs = [d for n in names for d in (f"{n} 1", f"{n} 2")]  # two variants of one key each
    llm = FakeLLM()
    v2(conn, txns(*descs), llm, batch_size=100, workers=1)
    reps, confirms = llm.calls
    rep_order = [i["descriptor"].rsplit(" ", 1)[0] for i in reps]
    conf_order = [i["descriptor"].rsplit(" ", 1)[0] for i in confirms]
    assert sorted(rep_order) == sorted(conf_order) and rep_order != conf_order
