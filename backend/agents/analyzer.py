"""Analyzer agent: recurring charges (with price changes and possibly-unused memberships), anomalies
(spike, duplicate, new large merchant) and category trends, from the user's own transactions.

Plain function with typed input/output; no LLM. The LangGraph node (backend/graph.py) loads the
transactions (never after as_of), persists patterns + evidence and writes agent_events. Every number
in a pattern is computed here and stored in its params, with the evidence txn ids needed to recompute it.

Thresholds marked (J) are judgment calls; the rest are standard values or approved definitions.
"""
from __future__ import annotations

import math
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from statistics import median

from backend.ledger import Txn, display_name, month_end, months_between
from backend.stats import MAD_TO_SD, mad, mann_kendall_p, robust_z, theil_sen

# recurring (monthly cadence only: every planted series is monthly)
GAP_LO, GAP_HI = 26, 35  # days between monthly charges
DOUBLE_LO, DOUBLE_HI = 52, 70  # one missed charge; at most one such gap per series
MIN_SERIES = 3  # (J)
MIN_ON_CADENCE = 0.75  # share of gaps in [GAP_LO, GAP_HI] (J)
MAX_EXTRA_SHARE = 0.25  # same-amount charges at the merchant that are not part of the series (J)
AMOUNT_TOL_PCT, AMOUNT_TOL_CENTS = 0.02, 50  # "same amount" (J)
ONE_AND_A_HALF_PERIODS = 45  # days: a series is active, or a new price continues it (J)
PRICE_CHANGE_MIN_NEW = 2  # charges at the new price before we call it a change (J)
UNUSED_DAYS = 90  # approved definition of an unused membership
# anomalies
Z_CUT = 3.5  # Iglewicz-Hoaglin modified z-score cut-off
SPIKE_MIN_RATIO = 2.0  # practical floor: at least 2x the merchant's median (J)
SPIKE_MIN_BASELINE = 5  # other transactions at the merchant (J)
DUP_MAX_DAYS = 2  # (J)
DUP_MIN_CENTS = 1000  # approved: ignore duplicates under $10
NEW_MERCHANT_WARMUP_DAYS = 60  # (J)
# trends
TREND_MIN_MONTHS, TREND_MAX_MONTHS = 6, 12
TREND_P = 0.05
TREND_MIN_CHANGE = 0.20  # fitted change over the window vs median month (J)
TREND_MIN_LEVEL = 5000  # median month >= $50 (J)


@dataclass
class Detected:
    type: str  # recurring | anomaly | trend
    title: str
    params: dict
    evidence: list[int]
    id: int | None = None  # set once persisted


@dataclass
class AnalyzerResult:
    patterns: list[Detected]
    n_txns: int
    n_uncategorized: int
    counts: dict[str, int] = field(default_factory=dict)


def money(cents: int | float) -> str:
    return f"${abs(cents) / 100:,.2f}"


# ---- recurring ----

def _tol(cents: int) -> float:
    return max(AMOUNT_TOL_PCT * cents, AMOUNT_TOL_CENTS)


def _amount_clusters(txns: list[Txn]) -> list[list[Txn]]:
    """Outflows grouped by near-equal amount; a cluster spans at most the tolerance from its smallest member."""
    out: list[tuple[int, list[Txn]]] = []
    for t in sorted(txns, key=lambda t: (-t.amount_cents, t.day, t.id)):
        a = -t.amount_cents
        if out and a - out[-1][0] <= _tol(out[-1][0]):
            out[-1][1].append(t)
        else:
            out.append((a, [t]))
    return [sorted(ts, key=lambda t: (t.day, t.id)) for _, ts in out]


def _chain_from(txns: list[Txn], start: int) -> list[Txn]:
    chain, doubles = [txns[start]], 0
    for t in txns[start + 1:]:
        g = (t.day - chain[-1].day).days
        if g < GAP_LO:
            continue  # an extra charge inside the period (e.g. a duplicate)
        if GAP_LO <= g <= GAP_HI:
            chain.append(t)
        elif DOUBLE_LO <= g <= DOUBLE_HI and doubles == 0:
            doubles += 1
            chain.append(t)
        else:
            break
    return chain


def _on_cadence(chain: list[Txn]) -> bool:
    gaps = [(b.day - a.day).days for a, b in zip(chain, chain[1:])]
    return bool(gaps) and sum(GAP_LO <= g <= GAP_HI for g in gaps) / len(gaps) >= MIN_ON_CADENCE


def _best_chain(txns: list[Txn], min_len: int) -> list[Txn] | None:
    """Longest regular monthly chain in txns (sorted by day), if it is long enough and not swamped by extras."""
    best: list[Txn] = []
    for i in range(len(txns)):
        c = _chain_from(txns, i)
        if len(c) > len(best) and _on_cadence(c):
            best = c
    if len(best) < min_len or len(txns) - len(best) > max(1, MAX_EXTRA_SHARE * len(best)):
        return None
    return best


@dataclass
class _Series:
    segments: list[list[Txn]]  # fixed-price segments in time order; one segment for variable series
    clusters: list[list[Txn]]  # every same-amount charge that belongs to it (chain + extras)
    amount_varies: bool

    @property
    def txns(self) -> list[Txn]:
        return [t for s in self.segments for t in s]


def _series_for_root(outflows: list[Txn]) -> list[_Series]:
    fixed = []  # (chain, cluster)
    for cl in _amount_clusters(outflows):
        c = _best_chain(cl, min_len=min(MIN_SERIES, PRICE_CHANGE_MIN_NEW))
        if c:
            fixed.append((c, cl))
    fixed.sort(key=lambda x: x[0][0].day)

    series: list[_Series] = []
    used: set[int] = set()
    for chain, cl in fixed:
        prev = next((s for s in reversed(series) if not s.amount_varies), None)
        gap = (chain[0].day - prev.segments[-1][-1].day).days if prev else None
        if prev and GAP_LO <= gap <= ONE_AND_A_HALF_PERIODS and len(chain) >= PRICE_CHANGE_MIN_NEW:
            prev.segments.append(chain)  # same merchant, new price, picks up where the old one stopped
            prev.clusters.append(cl)
        elif len(chain) >= MIN_SERIES:
            series.append(_Series([chain], [cl], amount_varies=False))
        else:
            continue
        used.update(t.id for t in cl)

    rest = [t for t in outflows if t.id not in used]  # variable bills: the remainder holds one regular chain
    c = _best_chain(rest, min_len=MIN_SERIES) if rest else None
    if c:  # only the chain belongs to the series; off-cadence charges stay ordinary spend
        series.append(_Series([c], [c], amount_varies=True))
    return series


def _recurring(by_root: dict[str, list[Txn]], as_of_end) -> tuple[list[Detected], set[int]]:
    out, series_ids = [], set()
    for root in sorted(by_root):
        outflows = by_root[root]
        for s in _series_for_root(outflows):
            txns = s.txns
            in_clusters = {t.id for cl in s.clusters for t in cl}
            series_ids |= in_clusters
            last = s.segments[-1]
            if s.amount_varies:
                amount = round(sum(t.amount_cents for t in txns[-3:]) / len(txns[-3:]))
            else:
                amount = round(median(t.amount_cents for t in last))
            active = (as_of_end - txns[-1].day).days <= ONE_AND_A_HALF_PERIODS
            price_changed = len(s.segments) > 1
            prev_amount = round(median(t.amount_cents for t in s.segments[-2])) if price_changed else None
            parent = [t for t in outflows if t.id not in in_clusters]
            parent_last = max(parent, key=lambda t: (t.day, t.id)) if parent else None
            days_since = (as_of_end - parent_last.day).days if parent_last else None
            unused = bool(active and not s.amount_varies and days_since is not None and days_since >= UNUSED_DAYS)
            merchant = Counter(display_name(t.description, t.amount_cents) for t in txns).most_common(1)[0][0]
            cats = Counter(t.category for t in txns if t.category)
            params = {
                "merchant": merchant, "merchant_root": root,
                "category": cats.most_common(1)[0][0] if cats else None,
                "cadence": "monthly", "n": len(txns),
                "period_days": round(sum((b.day - a.day).days for a, b in zip(txns, txns[1:])) / (len(txns) - 1), 1),
                "amount_cents": amount, "monthly_cents": amount, "amount_varies": s.amount_varies,
                "first_seen": txns[0].day.isoformat(), "last_seen": txns[-1].day.isoformat(), "active": active,
                "price_changed": price_changed, "prev_amount_cents": prev_amount,
                "price_change_date": last[0].day.isoformat() if price_changed else None,
                "possibly_unused": unused,
                "parent_last_spend": parent_last.day.isoformat() if parent_last else None,
                "parent_last_txn_id": parent_last.id if parent_last else None,
                "days_since_parent_spend": days_since,
            }
            if unused:
                title = f"{merchant}: {money(amount)}/month, no other {root} spend for {days_since} days"
            elif price_changed:
                title = (f"{merchant}: price {'up' if amount < prev_amount else 'down'} from {money(prev_amount)} "
                         f"to {money(amount)} on {params['price_change_date']}")
            else:
                title = f"{merchant}: {money(amount)} monthly" + (" (amount varies)" if s.amount_varies else "")
            out.append(Detected("recurring", title, params, [t.id for t in txns]))
    return out, series_ids


# ---- anomalies ----

def _anomaly(t: Txn, kind: str, title: str, **extra) -> Detected:
    params = {"txn_id": t.id, "kind": kind, "date": t.day.isoformat(), "amount_cents": t.amount_cents,
              "merchant_root": t.root, "baseline_cents": None, "mad_cents": None, "robust_z": None,
              "n_baseline": None, "related_txn_id": None} | extra
    evidence = [t.id] + ([extra["related_txn_id"]] if extra.get("related_txn_id") else [])
    return Detected("anomaly", title, params, evidence)


def _spikes(by_root: dict[str, list[Txn]], series_ids: set[int], skip: set[int]) -> list[Detected]:
    out = []
    for root in sorted(by_root):
        pool = [t for t in by_root[root] if t.id not in series_ids]
        if len(pool) <= SPIKE_MIN_BASELINE:
            continue
        for t in pool:
            if t.id in skip:
                continue
            others = [-o.amount_cents for o in pool if o is not t]
            x, med = -t.amount_cents, median(others)
            z = robust_z(x, others)
            if z >= Z_CUT and x >= SPIKE_MIN_RATIO * med:
                name = display_name(t.description, t.amount_cents)
                out.append(_anomaly(t, "spike", f"Unusually large charge at {name}: {money(x)} vs typical {money(med)}",
                                    baseline_cents=-round(med), mad_cents=round(mad(others)),
                                    robust_z=None if math.isinf(z) else round(z, 2), n_baseline=len(others)))
    return out


def _duplicates(by_root: dict[str, list[Txn]]) -> list[Detected]:
    out = []
    for root in sorted(by_root):
        groups = defaultdict(list)
        for t in by_root[root]:
            if -t.amount_cents >= DUP_MIN_CENTS:
                groups[t.amount_cents].append(t)
        for ts in groups.values():
            ts.sort(key=lambda t: (t.day, t.id))
            for a, b in zip(ts, ts[1:]):
                days = (b.day - a.day).days
                if days <= DUP_MAX_DAYS:
                    name = display_name(b.description, b.amount_cents)
                    when = f"on {b.day}" if days == 0 else f"{days} day(s) apart ({a.day}, {b.day})"
                    out.append(_anomaly(b, "duplicate", f"Possible duplicate charge: {name} {money(b.amount_cents)} "
                                                        f"charged twice {when}", related_txn_id=a.id))
    return out


def _new_merchants(txns: list[Txn], by_root_all: dict[str, list[Txn]], series_ids: set[int]) -> list[Detected]:
    if not txns:
        return []
    start = txns[0].day
    pool = [t for t in txns if t.amount_cents < 0 and t.id not in series_ids]
    out = []
    for root in sorted(by_root_all):
        first = by_root_all[root][0]
        if first.amount_cents >= 0 or (first.day - start).days < NEW_MERCHANT_WARMUP_DAYS:
            continue
        others = [math.log(-o.amount_cents) for o in pool if o is not first]
        if len(others) <= SPIKE_MIN_BASELINE:
            continue
        z = robust_z(math.log(-first.amount_cents), others)
        if z >= Z_CUT:
            name = display_name(first.description, first.amount_cents)
            typical = math.exp(median(others))
            out.append(_anomaly(first, "new_merchant",
                                f"New merchant {name}: {money(first.amount_cents)} on {first.day}, "
                                f"far above your typical purchase of {money(typical)}",
                                baseline_cents=-round(typical), robust_z=None if math.isinf(z) else round(z, 2),
                                n_baseline=len(others)))
    return out


# ---- trends ----

def _trends(txns: list[Txn], as_of_month: str) -> list[Detected]:
    if not txns:
        return []
    months = months_between(txns[0].month, as_of_month)[-TREND_MAX_MONTHS:]
    if len(months) < TREND_MIN_MONTHS:
        return []
    totals: dict[str, dict[str, int]] = defaultdict(lambda: dict.fromkeys(months, 0))
    ev: dict[str, list[int]] = defaultdict(list)
    for t in txns:
        if t.amount_cents < 0 and t.category and t.category != "income" and t.month in months:
            totals[t.category][t.month] += -t.amount_cents
            ev[t.category].append(t.id)
    out = []
    for cat in sorted(totals):
        ys = [totals[cat][m] for m in months]
        level = median(ys)
        slope, intercept = theil_sen(ys)
        p = mann_kendall_p(ys)
        if level < TREND_MIN_LEVEL or p >= TREND_P or abs(slope) * (len(ys) - 1) < TREND_MIN_CHANGE * level:
            continue
        latest, prev3 = ys[-1], round(sum(ys[-4:-1]) / 3)
        params = {"category": cat, "months": len(ys), "slope_cents_per_month": round(slope),
                  "start_cents": round(intercept), "end_cents": round(intercept + slope * (len(ys) - 1)),
                  "latest_month_cents": latest, "prev_3mo_mean_cents": prev3, "mk_p": round(p, 4),
                  "monthly_totals": totals[cat]}
        direction = "rising" if slope > 0 else "falling"
        change = f"{(latest - prev3) / prev3:+.0%}" if prev3 else "n/a"
        title = (f"{cat.replace('_', ' ').capitalize()} spending {direction}: about {money(slope)} per month; "
                 f"{money(latest)} in {months[-1]} vs {money(prev3)} average of the prior 3 months ({change})")
        out.append(Detected("trend", title, params, ev[cat]))
    return out


# ---- entry point ----

def analyze(txns: list[Txn], as_of_month: str) -> AnalyzerResult:
    """All detectors over the user's transactions on or before the end of as_of_month."""
    as_of_end = month_end(as_of_month)
    txns = sorted((t for t in txns if t.day <= as_of_end), key=lambda t: (t.day, t.id))
    by_root_all: dict[str, list[Txn]] = defaultdict(list)
    for t in txns:
        if t.root:
            by_root_all[t.root].append(t)
    by_root = {r: [t for t in ts if t.amount_cents < 0] for r, ts in by_root_all.items()}
    by_root = {r: ts for r, ts in by_root.items() if ts}

    recurring, series_ids = _recurring(by_root, as_of_end)
    new = _new_merchants(txns, by_root_all, series_ids)
    dups = _duplicates(by_root)
    spikes = _spikes(by_root, series_ids, skip={d.params["txn_id"] for d in new + dups})
    patterns = recurring + spikes + dups + new + _trends(txns, as_of_month)
    counts = Counter(p.type if p.type != "anomaly" else p.params["kind"] for p in patterns)
    counts["price_change"] = sum(p.type == "recurring" and p.params["price_changed"] for p in patterns)
    counts["possibly_unused"] = sum(p.type == "recurring" and p.params["possibly_unused"] for p in patterns)
    return AnalyzerResult(patterns, len(txns), sum(t.category is None for t in txns), dict(counts))
