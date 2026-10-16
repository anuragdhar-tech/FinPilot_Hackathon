"""Categorizer agent: one category from the closed taxonomy + confidence + short rationale per transaction.

Plain function with typed input/output; the LangGraph node (backend/graph.py) persists results and
writes agent_events. The LLM is injected so tests can mock it.

Flow:
1. Merchant-cache hits are labelled without an LLM call (from_cache=True).
2. Misses are grouped by merchant key; one representative per key goes to the LLM, in batches.
3. cat-v1: a confident representative (>= CACHE_MIN_CONFIDENCE) is cached and its label reused for
   the rest of its group (from_cache=True).
   cat-v2: a second member of the group is looked at in a separately composed, shuffled batch; only
   if both looks agree (and are confident) is the key cached and reused. Single-member keys are not
   cached. Otherwise every group member gets its own LLM look.
cat-v2 also asks the model to echo the merchant words it read; an answer whose merchant does not
appear in that item's descriptor is treated as misaligned (answer attached to the wrong id) and retried.
Every change is logged in eval/PROMPT_CHANGELOG.md.
Invalid model output is retried (whole batch if unparseable, else only the bad/missing items) and
never accepted; items still invalid after MAX_ATTEMPTS stay uncategorized.
"""
from __future__ import annotations

import json
import random
import re
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Callable

from pydantic import BaseModel, Field, ValidationError

from backend.llm.client import Completion, LLMUnavailable
from backend.models import CATEGORIES, Category

PROMPT_VERSION = "cat-v2"  # active version; see VERSIONS below
BATCH_SIZE = 25
MAX_ATTEMPTS = 3
WORKERS = 4
CACHE_MIN_CONFIDENCE = 0.8
MIN_KEY_LETTERS = 3
CONFIRM_SHUFFLE_SEED = 7  # fixed so runs are reproducible

SYSTEM_PROMPT_V1 = """You categorize bank transactions. Each item has an id, a raw bank descriptor, and a signed amount in US dollars (negative = money out, positive = money in). Return exactly one result per id, using only these categories:
- income: payroll, direct deposit, freelance/marketplace payouts, interest earned
- housing: rent, mortgage, property management, renters/home insurance
- utilities: electricity, gas, water, internet/cable, phone plans
- groceries: supermarkets and grocery stores
- dining: restaurants, cafes, coffee shops, bars, eaten at or picked up from the venue
- food_delivery: orders through a delivery platform, plus delivery-only memberships
- transport: rideshare rides, transit, fuel, parking, tolls, auto insurance
- shopping: retail, online marketplaces, electronics, clothing, home goods, store memberships
- entertainment: streaming, music, games, movies, event tickets
- health: pharmacy, medical providers, health insurance
- fitness: gyms, fitness classes and memberships
- travel: airlines, hotels, short-term rentals, car rental
- transfers: moves between the user's own accounts; person-to-person payments
- fees: bank, maintenance, foreign-transaction, overdraft and ATM fees
- other: fits none of the above (e.g. software subscriptions, postage)

Rules:
1. Find the merchant first. Ignore card prefixes (CHECKCARD 1031, POS PURCHASE, PURCHASE AUTHORIZED ON 10/02), store numbers, phone numbers and reference codes. Processor prefixes such as SQ*, TST* and DD* are hints, not the merchant.
2. A delivery platform plus a restaurant name is food_delivery; the restaurant on its own is dining.
3. When one company sells several services, classify by the service in the descriptor (UBER *TRIP = transport, UBER *EATS = food_delivery).
4. A membership takes the category of the service it is for.
5. Insurance takes the category of what it insures.
6. A refund takes the category of the original purchase, not income.
7. Confidence: 0.9 or above when merchant and category are both clear; 0.5-0.8 when the merchant is clear but the category is a judgment call; below 0.5 when you cannot identify the merchant.
8. Rationale: at most 12 words, naming the merchant and the deciding cue."""

# cat-v2 (Oct 15): exact wording changes are listed in eval/PROMPT_CHANGELOG.md.
SYSTEM_PROMPT_V2 = (
    SYSTEM_PROMPT_V1
    .replace("- other: fits none of the above (e.g. software subscriptions, postage)",
             "- other: only for things that fit no category above; streaming and memberships are never other")
    .replace("Processor prefixes such as SQ*, TST* and DD* are hints, not the merchant.",
             "SQ*/TST* indicate the card processor only, not delivery or a category; classify by the merchant.")
    .replace("8. Rationale: at most 12 words, naming the merchant and the deciding cue.",
             "8. Merchant: copy the merchant words exactly as they appear in that item's descriptor "
             "(not an expanded or corrected name).\n"
             "9. Rationale: at most 12 words, naming the merchant and the deciding cue.")
)


def _response_format(with_merchant: bool) -> dict:
    props = {"id": {"type": "string"}}
    if with_merchant:
        props["merchant"] = {"type": "string"}
    props |= {"category": {"type": "string", "enum": list(CATEGORIES)},
              "confidence": {"type": "number"}, "rationale": {"type": "string"}}
    return {"type": "json_schema", "json_schema": {"name": "categorizations", "strict": True, "schema": {
        "type": "object", "additionalProperties": False, "required": ["results"],
        "properties": {"results": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": list(props), "properties": props}}},
    }}}


@dataclass(frozen=True)
class Version:
    name: str
    system_prompt: str
    response_format: dict
    echo_check: bool  # reject answers whose echoed merchant is not in the item's descriptor
    confirm_before_cache: bool  # cache only after two independent looks agree


VERSIONS = {
    "cat-v1": Version("cat-v1", SYSTEM_PROMPT_V1, _response_format(False), False, False),
    "cat-v2": Version("cat-v2", SYSTEM_PROMPT_V2, _response_format(True), True, True),
}

LLM = Callable[[list[dict], dict], Completion]  # (messages, response_format) -> Completion


class ItemOut(BaseModel):
    """What one model result must look like before we accept it (re-checked in code, not trusted)."""
    id: str
    merchant: str | None = Field(default=None, max_length=120)  # cat-v2 echo
    category: Category
    confidence: float = Field(ge=0, le=1)
    rationale: str = Field(max_length=200)


@dataclass(frozen=True)
class TxnIn:
    txn_id: int
    description: str
    amount_cents: int


@dataclass(frozen=True)
class Label:
    txn_id: int
    category: str
    confidence: float
    rationale: str
    model: str
    prompt_version: str
    from_cache: bool


@dataclass
class BatchStat:
    """One LLM batch (all its attempts). Becomes one agent_event."""
    phase: str  # 'representatives' | 'confirmations' | 'unsure_members'
    items: int
    echo_checked: int = 0
    echo_rejected: int = 0  # answers whose merchant echo did not match the item (misaligned)
    attempts: int = 0
    retried_items: int = 0
    failed_items: int = 0
    problems: list[str] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    duration_ms: int = 0
    served_model: str | None = None
    fallback_triggered: bool = False


@dataclass
class CategorizeResult:
    labels: list[Label]
    failed_txn_ids: list[int]
    batches: list[BatchStat]
    cache_hits: int  # labels taken from the merchant cache (pre-existing or written this run)
    llm_items: int  # labels that came from a dedicated LLM look
    confirmed_keys: int = 0  # cat-v2: second look agreed -> cached
    disagreed_keys: int = 0  # cat-v2: second look disagreed -> not cached, members looked at individually


# ---- merchant key ----

_PREFIX = re.compile(r"^(CHECKCARD \d{4}\s+|POS PURCHASE\s+|PURCHASE AUTHORIZED ON \d\d/\d\d\s+)")
_PHONE = re.compile(r"\b\d{3}-\d{3}-\d{4}\b")


def merchant_key(description: str, amount_cents: int) -> str | None:
    """Normalized descriptor used as the cache key, or None if too little is left to trust.

    Keeps every alphabetic token: dropping "reference-looking" words also drops merchant names
    (DOORDASH, STARBUCKS) and merges unrelated merchants into one key. Tokens with digits
    (store numbers, ref codes, dates) are dropped. The sign is part of the key so a refund or
    payout never shares a label with a purchase.
    """
    d = _PREFIX.sub("", description.upper().strip())
    d = _PHONE.sub(" ", d).replace("*", " * ")
    tokens = [t for t in d.split() if not any(ch.isdigit() for ch in t)]
    key = " ".join(tokens).replace(" * ", "*").replace(" *", "*").replace("* ", "*").strip(" *#-")
    if sum(ch.isalpha() for ch in key) < MIN_KEY_LETTERS:
        return None
    return ("in|" if amount_cents > 0 else "out|") + key


class MerchantCache:
    """merchant_cache table. A hit requires the same model and prompt_version as the current run."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def get(self, key: str, model: str, prompt_version: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT category, confidence, rationale FROM merchant_cache "
            "WHERE merchant_key = ? AND model = ? AND prompt_version = ?",
            (key, model, prompt_version),
        ).fetchone()

    def put(self, key: str, label: Label) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO merchant_cache (merchant_key, category, confidence, rationale, model, prompt_version) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (key, label.category, label.confidence, label.rationale, label.model, label.prompt_version),
        )
        self.conn.commit()


# ---- merchant echo check (cat-v2) ----

_ECHO_STOP = {"POS", "PURCHASE", "CHECKCARD", "AUTHORIZED", "ONLINE", "WEB", "PAYMENT", "PAYMENTS", "PMT",
              "PMTS", "INC", "STORE", "THE", "AND", "COM", "USA", "LLC", "SERVICE", "CARD"}


def _alnum(text: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", text.upper())


def merchant_in_descriptor(merchant: str | None, descriptor: str, min_token: int = 3) -> bool:
    """True if the model's merchant echo plausibly comes from this descriptor.

    Catches answers attached to the wrong id (the echo names a neighbour's merchant). It cannot catch
    a swap between two items of the same merchant, and it is deliberately loose (any distinctive
    token) so a correct answer is not rejected for spelling.
    """
    if not merchant:
        return False
    d = _alnum(descriptor)
    tokens = [t for t in (_alnum(x) for x in re.split(r"[\s*/.\-]+", merchant)) if t and t not in _ECHO_STOP]
    joined = "".join(tokens)  # "TRADER JOE'S" -> TRADERJOES matches "TRADER JOE S"
    if len(joined) >= min_token and joined in d:
        return True
    return any(len(t) >= min_token and t in d for t in tokens)


# ---- one LLM batch with retries ----

def _amount(cents: int) -> str:
    return f"{'-' if cents < 0 else ''}{abs(cents) // 100}.{abs(cents) % 100:02d}"


def _messages(items: list[TxnIn], version: Version) -> tuple[list[dict], dict[str, TxnIn]]:
    # Short local ids: models mis-copy long random external ids; code maps back.
    local = {f"t{i + 1:02d}": t for i, t in enumerate(items)}
    payload = [{"id": lid, "descriptor": t.description, "amount_usd": _amount(t.amount_cents)}
               for lid, t in local.items()]
    return [{"role": "system", "content": version.system_prompt},
            {"role": "user", "content": json.dumps(payload)}], local


def _parse(text: str, local: dict[str, TxnIn], problems: list[str], version: Version,
           stat: BatchStat) -> dict[int, ItemOut]:
    """Accept only valid, unambiguous results for ids we sent. Everything else is left pending."""
    try:
        raw = json.loads(text)["results"]
        assert isinstance(raw, list)
    except (ValueError, KeyError, TypeError, AssertionError):
        problems.append("unparseable response")
        return {}
    by_id: dict[str, list[ItemOut]] = {}
    for r in raw:
        try:
            item = ItemOut.model_validate(r)
        except ValidationError:
            problems.append(f"invalid item {r.get('id') if isinstance(r, dict) else '?'}")
            continue
        if item.id not in local:
            problems.append(f"unknown id {item.id}")
            continue
        if version.echo_check:
            stat.echo_checked += 1
            if not merchant_in_descriptor(item.merchant, local[item.id].description):
                stat.echo_rejected += 1
                problems.append(f"echo mismatch {item.id}")
                continue
        by_id.setdefault(item.id, []).append(item)
    out = {}
    for lid, copies in by_id.items():
        if len({c.category for c in copies}) > 1:
            problems.append(f"conflicting duplicates for {lid}")
            continue
        if len(copies) > 1:
            problems.append(f"duplicate {lid}")
        out[local[lid].txn_id] = copies[0]
    missing = len(local) - len(out)
    if missing:
        problems.append(f"{missing} missing/invalid")
    return out


def run_batch(items: list[TxnIn], llm: LLM, model: str, phase: str, version: Version,
              max_attempts: int = MAX_ATTEMPTS) -> tuple[list[Label], list[int], BatchStat]:
    stat = BatchStat(phase=phase, items=len(items))
    started = time.monotonic()
    pending, labels = list(items), []
    while pending and stat.attempts < max_attempts:
        stat.attempts += 1
        if stat.attempts > 1:
            stat.retried_items += len(pending)
        messages, local = _messages(pending, version)
        try:
            comp = llm(messages, version.response_format)
        except LLMUnavailable:  # already retried by the SDK: fail the run instead of waiting through more attempts
            raise
        except Exception as e:
            stat.problems.append(f"llm error: {type(e).__name__}")
            continue
        stat.prompt_tokens += comp.prompt_tokens
        stat.completion_tokens += comp.completion_tokens
        stat.served_model = comp.model
        stat.fallback_triggered |= comp.fallback_triggered
        accepted = _parse(comp.text, local, stat.problems, version, stat)
        for txn_id, item in accepted.items():
            labels.append(Label(txn_id, item.category, item.confidence, item.rationale,
                                comp.model if comp.fallback_triggered else model, version.name, False))
        pending = [t for t in pending if t.txn_id not in accepted]
    stat.failed_items = len(pending)
    stat.duration_ms = int((time.monotonic() - started) * 1000)
    return labels, [t.txn_id for t in pending], stat


# ---- the agent ----

def categorize(txns: list[TxnIn], cache: MerchantCache | None, llm: LLM, *, model: str,
               version: str = PROMPT_VERSION, batch_size: int = BATCH_SIZE, workers: int = WORKERS,
               on_batch: Callable[[BatchStat], None] | None = None) -> CategorizeResult:
    """cache=None disables the cache and in-run reuse: every transaction gets its own LLM look
    (the eval's --no-cache mode, which measures raw model accuracy)."""
    v = VERSIONS[version]
    labels: dict[int, Label] = {}
    batches: list[BatchStat] = []
    failed: list[int] = []
    cache_hits = confirmed = disagreed = 0

    def run_all(items: list[TxnIn], phase: str) -> None:
        chunks = [items[i:i + batch_size] for i in range(0, len(items), batch_size)]
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(run_batch, c, llm, model, phase, v) for c in chunks]
            for f in as_completed(futures):  # results handled on this thread (DB/callback safe)
                got, bad, stat = f.result()
                labels.update({lb.txn_id: lb for lb in got})
                failed.extend(bad)
                batches.append(stat)
                if on_batch:
                    on_batch(stat)

    def confident(lb: Label | None) -> bool:
        # never cache a Mesh-fallback answer (its model differs from the configured one)
        return lb is not None and lb.confidence >= CACHE_MIN_CONFIDENCE and lb.model == model

    def reuse(key: str, src: Label, members: list[TxnIn]) -> None:
        nonlocal cache_hits
        cache.put(key, src)
        for t in members:
            labels[t.txn_id] = Label(t.txn_id, src.category, src.confidence, src.rationale,
                                     model, v.name, True)
            cache_hits += 1

    # 1. cache hits; 2. group misses by key
    groups: dict[str | int, list[TxnIn]] = {}
    for t in txns:
        key = merchant_key(t.description, t.amount_cents) if cache else None
        hit = cache.get(key, model, v.name) if key else None
        if hit:
            labels[t.txn_id] = Label(t.txn_id, hit["category"], hit["confidence"], hit["rationale"],
                                     model, v.name, True)
            cache_hits += 1
        else:
            groups.setdefault(key if key else t.txn_id, []).append(t)

    # 3. one representative per key
    run_all([g[0] for g in groups.values()], "representatives")

    unsure: list[TxnIn] = []
    if not v.confirm_before_cache:  # cat-v1: one confident look is enough to cache
        for gkey, members in groups.items():
            rep = labels.get(members[0].txn_id)
            if isinstance(gkey, str) and confident(rep):
                reuse(gkey, rep, members[1:])
            else:
                unsure.extend(members[1:])
    else:
        # cat-v2: a second, independent look before caching. The confirmer is another member of the
        # group (a different descriptor variant when there is one); confirmers are shuffled and batched
        # on their own, so each sits among different neighbours, at a different position, under
        # different batch boundaries than its representative.
        to_confirm: dict[str, TxnIn] = {}
        for gkey, members in groups.items():
            if isinstance(gkey, str) and len(members) > 1 and confident(labels.get(members[0].txn_id)):
                rest = members[1:]
                to_confirm[gkey] = next((t for t in rest if t.description != members[0].description), rest[0])
            else:
                unsure.extend(members[1:])  # singletons have no members left; unsure reps are not reused
        confirmers = list(to_confirm.values())
        random.Random(CONFIRM_SHUFFLE_SEED).shuffle(confirmers)
        run_all(confirmers, "confirmations")
        for gkey, second in to_confirm.items():
            members = groups[gkey]
            rep, check = labels[members[0].txn_id], labels.get(second.txn_id)
            rest = [t for t in members[1:] if t.txn_id != second.txn_id]
            if confident(check) and check.category == rep.category:
                confirmed += 1
                reuse(gkey, rep, rest)
            else:
                disagreed += 1
                unsure.extend(rest)

    # 4. members of unsure groups each get their own look
    if unsure:
        run_all(unsure, "unsure_members")

    ordered = [labels[t.txn_id] for t in txns if t.txn_id in labels]
    return CategorizeResult(
        labels=ordered,
        failed_txn_ids=sorted(set(failed) - set(labels)),
        batches=batches,
        cache_hits=cache_hits,
        llm_items=len(ordered) - cache_hits,
        confirmed_keys=confirmed,
        disagreed_keys=disagreed,
    )
