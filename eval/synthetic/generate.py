"""Seeded synthetic transactions with planted ground truth (PLAN.md section 7).

    python -m eval.synthetic.generate [--seed 42] [--out data/synthetic]

Writes:
- <persona>.csv       what the app ingests: external_id, date, description, amount (dollars, negative = outflow)
- personas.json       input metadata the app may read (opening balance, as-of month)
- ground_truth.json   labels and planted facts; only the eval reads it, never the app DB

Planted facts are derived from the generated rows, not asserted, and the generator
fails loudly if a plant did not land (see _Gen.finalize).
"""
from __future__ import annotations

import argparse
import calendar
import csv
import json
import math
import random
import string
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from statistics import median
from typing import Callable

from backend.models import CATEGORIES

GENERATOR_VERSION = 2
DEFAULT_SEED = 42
N_MONTHS = 12
START = date(2025, 10, 1)
END = date(2026, 9, 30)
UNUSED_WINDOW_DAYS = 90
OUT_DIR = Path(__file__).resolve().parents[2] / "data" / "synthetic"


def _add_months(d: date, n: int) -> date:
    y, m = divmod(d.month - 1 + n, 12)
    return date(d.year + y, m + 1, 1)


MONTHS = [_add_months(START, i) for i in range(N_MONTHS)]


def _on_day(month_start: date, day: int) -> date:
    last = calendar.monthrange(month_start.year, month_start.month)[1]
    return month_start.replace(day=min(day, last))


def _month_end(month_start: date) -> date:
    return _on_day(month_start, 31)


def _c(dollars: float) -> int:
    return round(dollars * 100)


# Canonical merchant -> (category, descriptor templates). Templates are messy on purpose
# but always resolvable by a careful human: no descriptor is genuinely ambiguous.
# Per-location placeholders: {n3} {n4} {n5} {n6} {n8} {n10} {city} {st}
# Per-transaction placeholders: {ref} {mmdd} {rest} {dow} {hr}
MERCHANTS: dict[str, tuple[str, list[str]]] = {
    # income
    "Acme Analytics": ("income", ["ACME ANALYTICS INC PAYROLL PPD ID: 9{n6}", "DIRECT DEP ACME ANALYTICS"]),
    "Northwind Health": ("income", ["NORTHWIND HEALTH SYS DIR DEP", "NORTHWIND HLTH PAYROLL PPD ID: 4{n6}"]),
    "Austin ISD": ("income", ["AUSTIN ISD PAYROLL", "AISD DIRECT DEPOSIT {n6}"]),
    "Stripe": ("income", ["STRIPE TRANSFER ST-{ref}", "STRIPE PAYMENTS TRANSFER {ref}"]),
    "Upwork": ("income", ["UPWORK ESCROW INC {ref}", "UPWORK -{n6} WITHDRAWAL"]),
    "Bank interest": ("income", ["INTEREST PAYMENT", "INTEREST PAID THIS PERIOD"]),
    # housing
    "Elm Street Property Mgmt": ("housing", ["ELM ST PROPERTY MGMT WEB PMTS {n6}", "ELM STREET PROP MGT ACH"]),
    "Lakeview Loan Servicing": ("housing", ["LAKEVIEW LN SRV MTG PYMT {n6}", "LAKEVIEW LOAN SERVIC MTG PMT"]),
    "Harbor Properties": ("housing", ["APPFOLIO HARBOR PROPERTI WEB PMTS", "HARBOR PROPERTIES APPFOLIO {n6}"]),
    "Lemonade": ("housing", ["LEMONADE INSURANCE CO", "LEMONADE I* {ref} NY"]),
    # utilities
    "PG&E": ("utilities", ["PGANDE WEB ONLINE {mmdd}{n4}", "PG&E/EZ-PAY {ref}"]),
    "Comcast": ("utilities", ["COMCAST CABLE COMM 800-266-2278", "COMCAST XFINITY {ref}"]),
    "Verizon": ("utilities", ["VERIZON WRLS P{n4}-01", "VZWRLSS*APOCC VISN {ref}"]),
    "City of Austin Utilities": ("utilities", ["CITY OF AUSTIN UTIL WEB PMT", "COA UTILITIES {n6}"]),
    "Spectrum": ("utilities", ["SPECTRUM {n4} {ref}", "CHARTER COMM SPECTRUM"]),
    "T-Mobile": ("utilities", ["T-MOBILE*AUTO PAY {ref}", "TMOBILE POSTPAID WEB"]),
    "Xcel Energy": ("utilities", ["XCEL ENERGY-PSCO {n6}", "XCELENERGY WEB PMT"]),
    "Visible": ("utilities", ["VISIBLE SERVICE {ref}", "VISIBLE*WIRELESS"]),
    # groceries
    "Trader Joe's": ("groceries", ["TRADER JOE S #{n3} {city} {st}", "TRADER JOES #{n3} QPS"]),
    "Safeway": ("groceries", ["SAFEWAY #{n4} {city} {st}", "SAFEWAY STORE {n4}"]),
    "Costco": ("groceries", ["COSTCO WHSE #{n4} {city} {st}", "COSTCO WHOLESALE #{n4}"]),
    "H-E-B": ("groceries", ["H-E-B #{n3} {city} {st}", "HEB ONLINE {ref}"]),
    "King Soopers": ("groceries", ["KING SOOPERS #{n4} {city} {st}", "KING SOOP {n4}"]),
    "Whole Foods": ("groceries", ["WHOLEFDS DNV {n5}", "WHOLE FOODS MKT {n5}"]),
    # dining
    "Starbucks": ("dining", ["STARBUCKS STORE {n5}", "STARBUCKS {n5} {city} {st}"]),
    "Chipotle": ("dining", ["CHIPOTLE {n4}", "CHIPOTLE ONLINE {n4}"]),
    "Sweetgreen": ("dining", ["SWEETGREEN {city}", "SWEETGREEN #{n3}"]),
    "Blue Bottle Coffee": ("dining", ["SQ *BLUE BOTTLE COFFEE {city}", "SQ *BLUE BTL COF {n4}"]),
    "Local restaurant": ("dining", ["TST* {rest} {city} {st}", "SQ *{rest}", "{rest} {city} {st}"]),
    # food delivery
    "DoorDash": ("food_delivery", ["DOORDASH*{rest}", "DD *DOORDASH {rest}", "DOORDASH {rest} 855-973-1040"]),
    "Uber Eats": ("food_delivery", ["UBER *EATS {ref}", "UBER EATS {ref} HELP.UBER.COM"]),
    "Grubhub": ("food_delivery", ["GRUBHUB*{rest}", "GRUBHUBFOOD {ref}"]),
    "DashPass": ("food_delivery", ["DOORDASH DASHPASS 855-973-1040", "DD *DASHPASS SUBSCRIPTION"]),
    # transport
    "Uber": ("transport", ["UBER *TRIP {ref}", "UBER   *TRIP HELP.UBER.COM"]),
    "Uber One": ("transport", ["UBER *ONE MEMBERSHIP", "UBER ONE {ref} HELP.UBER.COM"]),
    "Lyft": ("transport", ["LYFT   *RIDE {dow} {hr}", "LYFT *RIDE {dow} {hr}"]),
    "Clipper": ("transport", ["CLIPPER SERVICE {n6}", "CLIPPER CARD AUTOLOAD"]),
    "RTD": ("transport", ["RTD MOBILE TICKET {ref}", "RTD DENVER FARE"]),
    "Shell": ("transport", ["SHELL OIL {n8}", "SHELL SERVICE STATION {city}"]),
    "Chevron": ("transport", ["CHEVRON {n6} {city} {st}", "CHEVRON {n4}"]),
    "GEICO": ("transport", ["GEICO *AUTO {ref}", "GEICO  *AUTO 800-841-3000 DC"]),
    "Parking": ("transport", ["PAYBYPHONE {city}", "PARKMOBILE {n6}"]),
    # shopping
    "Amazon": ("shopping", ["AMZN Mktp US*{ref}", "Amazon.com*{ref}", "AMAZON MKTPL*{ref}"]),
    "Amazon Prime": ("shopping", ["Amazon Prime*{ref}", "AMAZON PRIME MEMBERSHIP"]),
    "Target": ("shopping", ["TARGET {n8} {city} {st}", "TARGET.COM * {n6}"]),
    "Walmart": ("shopping", ["WAL-MART #{n4} {city} {st}", "WALMART.COM {ref}"]),
    "Walmart+": ("shopping", ["WALMART+ MEMBERSHIP {ref}", "WMT PLUS {ref}"]),
    "Best Buy": ("shopping", ["BESTBUY {n8} {city} {st}", "BEST BUY {n5}"]),
    "Home Depot": ("shopping", ["THE HOME DEPOT #{n4}", "HOMEDEPOT.COM {ref}"]),
    "IKEA": ("shopping", ["IKEA {city} {st}", "IKEA US {ref}"]),
    "Uniqlo": ("shopping", ["UNIQLO USA {n4}", "UNIQLO.COM {ref}"]),
    "Apple Store": ("shopping", ["APPLE STORE #R{n3}", "APPLE STORE {city} {st}"]),
    # entertainment
    "Netflix": ("entertainment", ["NETFLIX.COM", "Netflix.com 866-579-7172", "NETFLIX INC LOS GATOS"]),
    "Spotify": ("entertainment", ["SPOTIFY USA", "Spotify P{ref}", "SPOTIFY.COM {n6}"]),
    "Disney+": ("entertainment", ["DISNEY PLUS", "DISNEYPLUS {ref} BURBANK CA"]),
    "Hulu": ("entertainment", ["HULU {n3}-{n3}-{n4}", "HULU.COM/BILL"]),
    "AMC Theatres": ("entertainment", ["AMC {n4} ONLINE", "AMC THEATRES {city}"]),
    "Steam": ("entertainment", ["STEAMGAMES.COM {n10}", "STEAM PURCHASE {ref}"]),
    "Ticketmaster": ("entertainment", ["TICKETMASTER {ref}", "TM *TICKETMASTER"]),
    # health
    "CVS": ("health", ["CVS/PHARMACY #{n5}", "CVS PHARMACY {n5} {city}"]),
    "Walgreens": ("health", ["WALGREENS #{n5}", "WALGREENS STORE {city}"]),
    "St. David's Medical": ("health", ["ST DAVIDS MEDICAL CTR {ref}", "STDAVIDS HLTHCARE"]),
    "Oscar Health": ("health", ["OSCAR HEALTH INS PREM {ref}", "OSCAR INSURANCE ACH"]),
    # fitness
    "ClassPass": ("fitness", ["CLASSPASS*MONTHLY", "CLASSPASS INC {ref}"]),
    "YMCA": ("fitness", ["YMCA OF AUSTIN DUES", "YMCA AUSTIN {n6}"]),
    "Planet Fitness": ("fitness", ["PLANET FIT CLUB #{n4}", "PLANET FITNESS {n4}"]),
    # travel
    "United Airlines": ("travel", ["UNITED {n10}", "UNITED AIRLINES {ref}"]),
    "Delta": ("travel", ["DELTA AIR {n10}", "DELTA {n10} ATLANTA"]),
    "Southwest": ("travel", ["SOUTHWES {n10}", "SOUTHWEST AIR {ref}"]),
    "Airbnb": ("travel", ["AIRBNB * HM{ref}", "AIRBNB HMQ{ref}"]),
    "Marriott": ("travel", ["MARRIOTT {city} {st}", "MARRIOTT HOTELS {n6}"]),
    "Hertz": ("travel", ["HERTZ RENT-A-CAR {n6}", "HERTZ {city}"]),
    # transfers
    "Savings transfer": ("transfers", ["ONLINE TRANSFER TO SAV XXXXXX", "TRANSFER TO SAVINGS {mmdd}"]),
    "Venmo": ("transfers", ["VENMO PAYMENT {n10}", "VENMO *{ref}"]),
    # fees
    "Bank fee": ("fees", ["MONTHLY MAINTENANCE FEE", "SERVICE FEE"]),
    "Foreign transaction fee": ("fees", ["FOREIGN TRANSACTION FEE", "INTL TXN FEE"]),
    # other (no closer category in the taxonomy)
    "Adobe": ("other", ["ADOBE *CREATIVE CLD {ref}", "ADOBE  *ADOBE 408-536-6000"]),
    "USPS": ("other", ["USPS PO {n8}", "USPS.COM CLICKNSHIP"]),
}

# Merchants whose category is a genuine judgment call even with the taxonomy in hand
# (mixed-use memberships, software, postage). The eval scores these leniently: any listed
# label counts. Strict accuracy still uses only the primary label. Approved Oct 15 (Tier A).
ACCEPTABLE_LABELS: dict[str, list[str]] = {
    "Adobe": ["other", "shopping"],
    "USPS": ["other", "shopping"],
    "Uber One": ["transport", "food_delivery"],
    "Amazon Prime": ["shopping", "entertainment"],
    "Walmart+": ["shopping", "groceries"],
}

RESTAURANTS = ["CHIPOTLE", "SWEETGREEN", "THAI BASIL", "PIZZERIA ROMA", "HALAL CART", "SUSHI ZONE",
               "TAQUERIA EL SOL", "PHO 88", "BURGER BARN", "CURRY HOUSE"]
LOCAL_RESTAURANTS = RESTAURANTS[2:]  # Chipotle and Sweetgreen have their own merchants
_PER_TXN = ("ref", "mmdd", "rest", "dow", "hr")
_ALNUM = string.ascii_uppercase + string.digits


@dataclass
class Txn:
    day: date
    merchant: str
    amount_cents: int
    description: str
    ext_id: str = ""

    @property
    def category(self) -> str:
        return MERCHANTS[self.merchant][0]


@dataclass
class _Series:
    merchant: str
    subtype: str
    txns: list[Txn]
    membership_of: list[str] | None = None
    intended_unused: bool | None = None


@dataclass
class _Anomaly:
    kind: str
    flagged: Txn
    evidence: list[Txn]


@dataclass
class _Gen:
    key: str
    rng: random.Random
    cities: list[tuple[str, str]]
    txns: list[Txn] = field(default_factory=list)
    series_list: list[_Series] = field(default_factory=list)
    income_streams: list[tuple[str, str, list[Txn]]] = field(default_factory=list)
    anomalies: list[_Anomaly] = field(default_factory=list)
    trend_categories: list[str] = field(default_factory=list)
    _variants: dict[str, list[str]] = field(default_factory=dict)

    # ---- descriptors ----

    def _locations(self, merchant: str) -> list[str]:
        """Each merchant gets fixed 'locations' (store numbers, cities), like a real statement."""
        if merchant not in self._variants:
            r = self.rng
            out = []
            for tpl in MERCHANTS[merchant][1]:
                for _ in range(r.randint(1, 2)):
                    city, st = r.choice(self.cities)
                    vals = {f"n{k}": "".join(r.choices(string.digits, k=k)) for k in (3, 4, 5, 6, 8, 10)}
                    vals.update(city=city, st=st, **{k: "{" + k + "}" for k in _PER_TXN})
                    out.append(tpl.format(**vals))
            self._variants[merchant] = out
        return self._variants[merchant]

    def describe(self, merchant: str, day: date, *, stable: bool = False) -> str:
        r = self.rng
        locs = self._locations(merchant)
        tpl = locs[0] if stable and r.random() < 0.9 else r.choice(locs)
        rest = r.choice(LOCAL_RESTAURANTS if merchant == "Local restaurant" else RESTAURANTS)
        return tpl.format(
            ref="".join(r.choices(_ALNUM, k=r.randint(8, 10))),
            mmdd=f"{day:%m%d}",
            rest=rest,
            dow=f"{day:%a}".upper(),
            hr=f"{r.randint(1, 12)}{r.choice(['AM', 'PM'])}",
        )

    def _mess(self, desc: str, day: date) -> str:
        """Card-network noise: POS prefixes, auth dates, truncation."""
        r = self.rng
        roll = r.random()
        if roll < 0.15:
            return "POS PURCHASE " + desc
        if roll < 0.25:
            return f"CHECKCARD {day:%m%d} " + desc
        if roll < 0.32:
            return f"PURCHASE AUTHORIZED ON {day:%m/%d} " + desc
        if roll < 0.50 and len(desc) > 30:
            return desc[: r.randint(26, 30)].rstrip()
        return desc

    # ---- building blocks ----

    def add(self, day: date, merchant: str, cents: int, *, card: bool = True,
            stable: bool = False, desc: str | None = None) -> Txn:
        if desc is None:
            desc = self.describe(merchant, day, stable=stable)
            if card:
                desc = self._mess(desc, day)
        t = Txn(day, merchant, cents, desc)
        self.txns.append(t)
        return t

    def series(self, merchant: str, dom: int, cents: int | Callable[[int], int], *, subtype: str,
               months: range = range(N_MONTHS), membership_of: list[str] | None = None,
               unused: bool | None = None) -> list[Txn]:
        """A monthly recurring charge on day-of-month `dom`."""
        amount = cents if callable(cents) else (lambda m: cents)
        txns = [self.add(_on_day(MONTHS[m], dom), merchant, amount(m), card=False, stable=True)
                for m in months]
        self.series_list.append(_Series(merchant, subtype, txns, membership_of, unused))
        return txns

    def scatter(self, merchant: str, m: int, count: int, lo: float, hi: float, *, sign: int = -1) -> None:
        """`count` card purchases on random days of month `m`, amounts in [lo, hi] dollars."""
        ms = MONTHS[m]
        last = calendar.monthrange(ms.year, ms.month)[1]
        for _ in range(count):
            day = ms.replace(day=self.rng.randint(1, last))
            dollars = self.rng.triangular(lo, hi, lo + (hi - lo) * 0.35)
            self.add(day, merchant, sign * _c(dollars))

    def income(self, merchant: str, days: list[date], cents: Callable[[], int], cadence: str) -> None:
        txns = [self.add(d, merchant, cents(), card=False, stable=True) for d in days if d <= END]
        self.income_streams.append((merchant, cadence, txns))

    def spike(self, day: date, merchant: str, cents: int) -> None:
        t = self.add(day, merchant, cents)
        self.anomalies.append(_Anomaly("spike", t, [t]))

    def duplicate(self, original: Txn) -> None:
        t = self.add(original.day, original.merchant, original.amount_cents, desc=original.description)
        self.anomalies.append(_Anomaly("duplicate", t, [original, t]))

    def new_merchant(self, day: date, merchant: str, cents: int) -> None:
        t = self.add(day, merchant, cents)
        self.anomalies.append(_Anomaly("new_merchant", t, [t]))

    # ---- output ----

    def finalize(self) -> dict:
        """Assign ids, derive ground truth from the rows, and check every plant landed."""
        self.txns.sort(key=lambda t: (t.day, t.merchant, t.amount_cents, t.description))
        seen: set[str] = set()
        for t in self.txns:
            eid = f"{self.rng.getrandbits(40):010x}"
            while eid in seen:
                eid = f"{self.rng.getrandbits(40):010x}"
            seen.add(eid)
            t.ext_id = eid
        assert all(t.day <= END for t in self.txns), f"{self.key}: transaction after END"

        recurring = [self._series_truth(s) for s in self.series_list]
        return {
            "labels": {t.ext_id: _label(t) for t in self.txns},
            "recurring": recurring,
            "unused_subscriptions": [r["merchant"] for r in recurring if r["unused"]],
            "anomalies": [self._anomaly_truth(a) for a in self.anomalies],
            "trends": [self._trend_truth(c) for c in self.trend_categories],
            "income_streams": [{"merchant": m, "cadence": c, "txn_ids": [t.ext_id for t in ts]}
                               for m, c, ts in self.income_streams],
        }

    def _series_truth(self, s: _Series) -> dict:
        amounts = [t.amount_cents for t in s.txns]
        changes = [i for i in range(1, len(amounts)) if amounts[i] != amounts[i - 1]]
        price_change = None
        if len(changes) == 1:
            i = changes[0]
            price_change = {"effective": s.txns[i].day.isoformat(),
                            "from_cents": amounts[i - 1], "to_cents": amounts[i]}
        unused = last_parent = None
        if s.membership_of:
            parent_spend = [t.day for t in self.txns if t.merchant in s.membership_of and t.amount_cents < 0]
            last_parent = max(parent_spend) if parent_spend else None
            unused = last_parent is None or (END - last_parent).days > UNUSED_WINDOW_DAYS
            assert unused == s.intended_unused, f"{self.key}: {s.merchant} unused={unused}, planned {s.intended_unused}"
        return {
            "merchant": s.merchant,
            "category": MERCHANTS[s.merchant][0],
            "cadence": "monthly",
            "subtype": s.subtype,
            "amount_cents": amounts[-1],
            "amount_varies": len(changes) > 1,
            "price_change": price_change,
            "first_seen": s.txns[0].day.isoformat(),
            "last_seen": s.txns[-1].day.isoformat(),
            "membership_of": s.membership_of,
            "unused": unused,
            "last_parent_spend": last_parent.isoformat() if last_parent else None,
            "txn_ids": [t.ext_id for t in s.txns],
        }

    def _anomaly_truth(self, a: _Anomaly) -> dict:
        t = a.flagged
        prior = [abs(x.amount_cents) for x in self.txns
                 if x.merchant == t.merchant and x.day < t.day and x is not t]
        baseline = round(median(prior)) if prior else None
        if a.kind == "spike":
            assert len(prior) >= 4 and abs(t.amount_cents) >= 3 * baseline, f"{self.key}: weak spike {t}"
        if a.kind == "new_merchant":
            assert not prior, f"{self.key}: {t.merchant} is not new"
        return {
            "kind": a.kind,
            "date": t.day.isoformat(),
            "merchant": t.merchant,
            "category": t.category,
            "amount_cents": t.amount_cents,
            "baseline_cents": -baseline if baseline and a.kind == "spike" else None,
            "flagged_txn_id": t.ext_id,
            "txn_ids": [x.ext_id for x in a.evidence],
        }

    def _trend_truth(self, category: str) -> dict:
        totals = monthly_totals(self.txns, category)
        vals = list(totals.values())
        slope = _slope(vals)
        assert slope > 0 and sum(vals[-3:]) >= 1.8 * sum(vals[:3]), f"{self.key}: {category} trend too weak"
        return {"category": category, "direction": "rising",
                "slope_cents_per_month": round(slope), "monthly_totals_cents": totals}


def monthly_totals(txns: list[Txn], category: str) -> dict[str, int]:
    """Outflow per month for a category, as positive cents."""
    totals = {f"{ms:%Y-%m}": 0 for ms in MONTHS}
    for t in txns:
        if t.category == category and t.amount_cents < 0:
            totals[f"{t.day:%Y-%m}"] += -t.amount_cents
    return totals


def _slope(ys: list[float]) -> float:
    n = len(ys)
    xbar, ybar = (n - 1) / 2, sum(ys) / n
    return sum((x - xbar) * (y - ybar) for x, y in enumerate(ys)) / sum((x - xbar) ** 2 for x in range(n))


def _every(first: date, step_days: int) -> list[date]:
    out, d = [], first
    while d <= END:
        out.append(d)
        d += timedelta(days=step_days)
    return out


def _seasonal(rng: random.Random, base: float, amp: float, peak_month_idx: int) -> Callable[[int], int]:
    return lambda m: -_c(base + amp * math.cos(2 * math.pi * (m - peak_month_idx) / 12) + rng.uniform(-6, 6))


def _monthly_days(dom: int) -> list[date]:
    return [_on_day(ms, dom) for ms in MONTHS]


# ---------------------------------------------------------------------------------------
# Personas. Month index m: 0 = 2025-10 ... 11 = 2026-09.
# Every unused membership has a "switched apps" story, so total spend in that category
# stays stable and the plant does not create an accidental extra trend.
# ---------------------------------------------------------------------------------------

def _maya(g: _Gen) -> None:
    """Young renter in Oakland. Planted: rising food-delivery spend; unused Uber One (moved to Lyft)."""
    r = g.rng
    g.income("Acme Analytics", _every(date(2025, 10, 3), 14), lambda: _c(2185.40), "biweekly")
    g.income("Bank interest", _monthly_days(28), lambda: _c(r.uniform(0.3, 2.1)), "monthly")
    g.series("Elm Street Property Mgmt", 1, -_c(2150), subtype="housing")
    g.series("Lemonade", 14, -_c(12.50), subtype="insurance")
    g.series("PG&E", 18, _seasonal(r, 70, 25, 3), subtype="bill")
    g.series("Comcast", 9, -_c(70), subtype="bill")
    g.series("Verizon", 22, -_c(65), subtype="bill")
    g.series("Netflix", 12, -_c(15.49), subtype="subscription")
    g.series("Spotify", 3, lambda m: -_c(11.99 if m < 5 else 12.99), subtype="subscription")
    g.series("ClassPass", 5, -_c(49), subtype="subscription")
    g.series("DashPass", 20, -_c(9.99), subtype="membership", membership_of=["DoorDash"], unused=False)
    g.series("Uber One", 7, -_c(9.99), subtype="membership", membership_of=["Uber", "Uber Eats"], unused=True)
    g.series("Savings transfer", 16, -_c(300), subtype="transfer")
    g.trend_categories.append("food_delivery")

    for m in range(N_MONTHS):
        # planted trend: order count and basket size both grow each month
        for _ in range(5 + round(m * 1.1) + r.randint(-1, 1)):
            g.scatter("DoorDash" if r.random() < 0.7 else "Grubhub", m, 1, 20 * (1 + 0.03 * m), 42 * (1 + 0.03 * m))
        if m < 3:  # used Uber early, then switched to Lyft but kept Uber One
            g.scatter("Uber", m, r.randint(3, 5), 9, 28)
            g.scatter("Uber Eats", m, 1, 18, 35)
        else:
            g.scatter("Lyft", m, r.randint(4, 7), 9, 32)
        g.scatter("Clipper", m, r.randint(1, 3), 20, 40)
        g.scatter("Trader Joe's", m, r.randint(3, 5), 25, 95)
        g.scatter("Safeway", m, r.randint(1, 2), 15, 70)
        g.scatter("Blue Bottle Coffee", m, r.randint(3, 6), 4.5, 9)
        g.scatter("Local restaurant", m, r.randint(2, 4), 18, 75)
        g.scatter("Sweetgreen", m, r.randint(1, 3), 13, 19)
        g.scatter("Amazon", m, r.randint(2, 4), 12, 85)
        g.scatter("Target", m, r.randint(0, 2), 20, 110)
        g.scatter("CVS", m, r.randint(0, 2), 8, 40)
        g.scatter("Venmo", m, r.randint(1, 2), 12, 60)
        if m % 3 == 0:
            g.scatter("Venmo", m, 1, 15, 45, sign=1)
        if m % 4 == 1:
            g.scatter("Uniqlo", m, 1, 30, 120)
        if m % 2 == 0:
            g.scatter("AMC Theatres", m, 1, 14, 32)
            g.scatter("Steam", m, 1, 10, 60)

    # trip in 2026-05
    g.add(date(2026, 4, 2), "United Airlines", -_c(389.40))
    g.add(date(2026, 5, 9), "Airbnb", -_c(640.00))
    g.add(date(2026, 5, 11), "Foreign transaction fee", -_c(3.12), card=False)
    g.add(date(2026, 5, 13), "Foreign transaction fee", -_c(1.87), card=False)

    g.spike(date(2026, 6, 13), "Trader Joe's", -_c(412.37))
    g.duplicate(g.add(date(2026, 7, 9), "Amazon", -_c(89.99)))
    g.new_merchant(date(2026, 8, 21), "Best Buy", -_c(1249.99))


def _ortiz(g: _Gen) -> None:
    """Family with a mortgage in Austin. Planted: Netflix price hike; unused Walmart+ (moved to Target)."""
    r = g.rng
    semimonthly = sorted([_on_day(ms, 15) for ms in MONTHS] + [_month_end(ms) for ms in MONTHS])
    g.income("Northwind Health", semimonthly, lambda: _c(2720.00), "semimonthly")
    g.income("Austin ISD", _monthly_days(25), lambda: _c(2450.00), "monthly")
    g.income("Bank interest", _monthly_days(28), lambda: _c(r.uniform(1.0, 6.0)), "monthly")
    g.series("Lakeview Loan Servicing", 1, -_c(2640.18), subtype="housing")
    g.series("City of Austin Utilities", 20, _seasonal(r, 185, 60, 10), subtype="bill")
    g.series("Spectrum", 12, -_c(79.99), subtype="bill")
    g.series("T-Mobile", 18, -_c(140.00), subtype="bill")
    geico = g.series("GEICO", 11, -_c(182.40), subtype="insurance")
    g.series("YMCA", 2, -_c(89.00), subtype="subscription")
    g.series("Netflix", 12, lambda m: -_c(15.49 if m < 6 else 17.99), subtype="subscription")
    g.series("Disney+", 8, -_c(13.99), subtype="subscription")
    g.series("Amazon Prime", 4, -_c(14.99), subtype="membership", membership_of=["Amazon"], unused=False)
    g.series("Walmart+", 23, -_c(12.95), subtype="membership", membership_of=["Walmart"], unused=True)
    g.series("Savings transfer", 26, -_c(500), subtype="transfer")

    for m in range(N_MONTHS):
        g.scatter("Costco", m, r.randint(2, 3), 110, 320)
        g.scatter("H-E-B", m, r.randint(4, 6), 45, 180)
        g.scatter("Chipotle", m, r.randint(1, 2), 11, 42)
        g.scatter("Local restaurant", m, r.randint(2, 4), 18, 95)
        g.scatter("Starbucks", m, r.randint(2, 4), 5, 14)
        g.scatter("DoorDash", m, r.randint(1, 3), 25, 55)  # steady: control for the delivery trend
        g.scatter("Shell", m, r.randint(2, 3), 35, 70)
        g.scatter("Chevron", m, r.randint(2, 3), 35, 70)
        g.scatter("Parking", m, r.randint(0, 2), 3, 15)
        g.scatter("Amazon", m, r.randint(5, 9), 10, 150)
        if m < 5:  # shopped at Walmart, then switched to Target but kept Walmart+
            g.scatter("Walmart", m, r.randint(2, 3), 20, 110)
            g.scatter("Target", m, r.randint(1, 2), 15, 140)
        else:
            g.scatter("Target", m, r.randint(3, 5), 15, 140)
        g.scatter("Home Depot", m, r.randint(0, 2), 15, 220)
        g.scatter("CVS", m, r.randint(1, 2), 8, 45)
        g.scatter("St. David's Medical", m, r.randint(0, 1), 25, 80)
        g.scatter("AMC Theatres", m, r.randint(0, 1), 40, 75)
    g.add(date(2025, 12, 6), "Ticketmaster", -_c(212.50))
    g.add(date(2026, 2, 17), "Amazon", _c(34.99), desc="AMZN Mktp US RFND " + "".join(r.choices(_ALNUM, k=9)))

    # summer trip 2026-06
    g.add(date(2026, 4, 20), "Delta", -_c(1248.60))
    g.add(date(2026, 6, 12), "Marriott", -_c(980.22))
    g.add(date(2026, 6, 12), "Hertz", -_c(410.75))

    g.spike(date(2026, 3, 14), "Costco", -_c(1184.56))
    g.duplicate(geico[8])
    g.new_merchant(date(2026, 8, 2), "IKEA", -_c(1450.00))


def _sam(g: _Gen) -> None:
    """Freelancer in Denver, irregular income. Planted: unused DashPass (moved to Grubhub)."""
    r = g.rng
    g.income("Bank interest", _monthly_days(28), lambda: _c(r.uniform(0.5, 4.0)), "monthly")
    for m in range(N_MONTHS):
        ms = MONTHS[m]
        last = calendar.monthrange(ms.year, ms.month)[1]
        payouts = 0 if m == 4 else r.choice([1, 2, 2, 3, 3, 4])  # 2026-02 is a dry month
        for _ in range(payouts):
            day = ms.replace(day=r.randint(1, last))
            amount = _c(r.uniform(700, 3400))
            g.add(day, "Stripe", amount, card=False, stable=True)
            if amount > _c(2800) and day + timedelta(days=2) <= END:  # set aside for taxes
                g.add(day + timedelta(days=2), "Savings transfer", -_c(1000), card=False)
        if r.random() < 0.4:
            g.add(ms.replace(day=r.randint(1, last)), "Upwork", _c(r.uniform(300, 1200)), card=False)
    stripe = [t for t in g.txns if t.merchant == "Stripe"]
    g.income_streams.append(("Stripe", "irregular", stripe))

    g.series("Harbor Properties", 1, -_c(1780.00), subtype="housing")
    g.series("Oscar Health", 1, -_c(412.00), subtype="insurance")
    g.series("Xcel Energy", 15, _seasonal(r, 75, 40, 3), subtype="bill")
    g.series("Comcast", 10, -_c(80.00), subtype="bill")
    g.series("Visible", 21, -_c(25.00), subtype="bill")
    g.series("Spotify", 3, -_c(11.99), subtype="subscription")
    g.series("Hulu", 17, -_c(7.99), subtype="subscription")
    adobe = g.series("Adobe", 6, -_c(59.99), subtype="subscription")
    g.series("Planet Fitness", 17, -_c(24.99), subtype="subscription")
    g.series("DashPass", 9, -_c(9.99), subtype="membership", membership_of=["DoorDash"], unused=True)

    for m in range(N_MONTHS):
        # used DoorDash, then switched to Grubhub but kept DashPass; delivery spend stays flat
        g.scatter("DoorDash" if m < 6 else "Grubhub", m, r.randint(1, 3), 18, 45)
        g.scatter("King Soopers", m, r.randint(4, 7), 20, 95)
        g.scatter("Whole Foods", m, r.randint(1, 2), 25, 80)
        g.scatter("Starbucks", m, r.randint(5, 9), 4, 9)
        g.scatter("Local restaurant", m, r.randint(3, 6), 14, 70)
        g.scatter("RTD", m, r.randint(3, 6), 3, 10.5)
        g.scatter("Lyft", m, r.randint(1, 3), 10, 35)
        g.scatter("Shell", m, r.randint(1, 2), 30, 55)
        g.scatter("Amazon", m, r.randint(2, 4), 12, 80)
        g.scatter("Walgreens", m, r.randint(1, 2), 6, 35)
        g.scatter("USPS", m, r.randint(0, 2), 8, 25)
        if m in (4, 5):
            g.add(_on_day(MONTHS[m], 28), "Bank fee", -_c(12.00), card=False)
        if m % 4 == 2:
            g.scatter("Uniqlo", m, 1, 25, 90)

    g.add(date(2025, 11, 18), "Southwest", -_c(248.00))
    g.add(date(2025, 12, 20), "Airbnb", -_c(420.00))

    g.spike(date(2026, 5, 22), "Lyft", -_c(187.40))
    g.duplicate(adobe[7])
    g.new_merchant(date(2026, 7, 15), "Apple Store", -_c(2399.00))


PERSONAS = {
    "maya_renter": dict(
        build=_maya, display_name="Maya (synthetic)", opening_balance_cents=_c(4200),
        cities=[("OAKLAND", "CA"), ("SAN FRANCISCO", "CA"), ("BERKELEY", "CA")],
        description="Young professional renting in Oakland; biweekly salary.",
    ),
    "ortiz_family": dict(
        build=_ortiz, display_name="Ortiz family (synthetic)", opening_balance_cents=_c(12500),
        cities=[("AUSTIN", "TX"), ("ROUND ROCK", "TX")],
        description="Two-income family with a mortgage in Austin.",
    ),
    "sam_freelancer": dict(
        build=_sam, display_name="Sam (synthetic)", opening_balance_cents=_c(18000),
        cities=[("DENVER", "CO"), ("LAKEWOOD", "CO"), ("BOULDER", "CO")],
        description="Freelancer in Denver with irregular client payouts.",
    ),
}


def _label(t) -> dict:
    label = {"category": t.category, "merchant": t.merchant}
    if t.merchant in ACCEPTABLE_LABELS:
        label["acceptable_labels"] = ACCEPTABLE_LABELS[t.merchant]
    return label


def _fmt_amount(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    return f"{sign}{abs(cents) // 100}.{abs(cents) % 100:02d}"


def generate(seed: int = DEFAULT_SEED, out_dir: Path = OUT_DIR) -> dict:
    """Write CSVs, personas.json and ground_truth.json. Returns the ground truth."""
    out_dir.mkdir(parents=True, exist_ok=True)
    personas_meta, truth_personas = {}, {}
    for idx, (key, spec) in enumerate(PERSONAS.items()):
        g = _Gen(key, random.Random(seed * 1000 + idx), spec["cities"])
        spec["build"](g)
        truth = g.finalize()
        assert set(t["category"] for t in truth["labels"].values()) <= set(CATEGORIES)

        csv_name = f"{key}.csv"
        with open(out_dir / csv_name, "w", newline="") as f:
            w = csv.writer(f, lineterminator="\n")
            w.writerow(["external_id", "date", "description", "amount"])
            for t in g.txns:
                w.writerow([t.ext_id, t.day.isoformat(), t.description, _fmt_amount(t.amount_cents)])

        personas_meta[key] = {
            "display_name": spec["display_name"],
            "description": spec["description"],
            "csv": csv_name,
            "opening_balance_cents": spec["opening_balance_cents"],
            "opening_date": START.isoformat(),
            "as_of_month": f"{END:%Y-%m}",
        }
        truth_personas[key] = truth

    ground_truth = {
        "generator_version": GENERATOR_VERSION,
        "seed": seed,
        "period": {"start": START.isoformat(), "end": END.isoformat()},
        "unused_window_days": UNUSED_WINDOW_DAYS,
        "personas": truth_personas,
    }
    (out_dir / "personas.json").write_text(json.dumps(personas_meta, indent=2) + "\n")
    (out_dir / "ground_truth.json").write_text(json.dumps(ground_truth, indent=2) + "\n")
    return ground_truth


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--seed", type=int, default=DEFAULT_SEED)
    p.add_argument("--out", type=Path, default=OUT_DIR)
    args = p.parse_args()
    truth = generate(args.seed, args.out)
    for key, t in truth["personas"].items():
        print(f"{key}: {len(t['labels'])} txns, {len(t['recurring'])} recurring, "
              f"unused={t['unused_subscriptions']}, {len(t['anomalies'])} anomalies, "
              f"trends={[x['category'] for x in t['trends']]}")
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
