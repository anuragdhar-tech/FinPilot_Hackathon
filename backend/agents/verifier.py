"""Verifier: code-only checks of an Advisor draft against the DB. No LLM.

For each draft:
- schema: action and claims are well formed (Claim model, units and fields per kind)
- trigger: the cited pattern exists in this run
- target: the target names the trigger's merchant or category
- action: the pattern supports the action (cancel only an active, possibly-unused fixed-price membership; price
  review only after a price rise; reduce only a rising trend whose latest month is above the prior 3 months)
- required_claims: the action's primary claims are present
- each claim recomputes within tolerance from transactions, pattern evidence and the stored forecast flows
- numbers: every money amount, percent, month/date and "N days/months" in title + body matches a claim

Recurring and trend claims are recomputed from the raw transactions (pattern evidence and the latest
categorization), not from the stored pattern params, so the check also catches Analyzer bugs. Forecast
claims re-run the engine (project()) over the stored flows. Goal claims (goal_check runs) come from the goals
row, the transactions (savings = balance growth since the start month) and project(), with the draft's action
applied for goal_gap_after.
"""
from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from statistics import median

from pydantic import ValidationError

from backend.agents import goal_tracker
from backend.agents.forecaster import MAX_HORIZON, ForecastInputs, inputs_from_params, project
from backend.ledger import Txn, add_months, load_txns, month_end, months_between
from backend.models import Claim

MAX_ATTEMPTS = 3  # first draft + 2 redrafts
ACTIONS = ("cancel", "review_price_increase", "reduce_spending")
REQUIRED = {
    "cancel": ("recurring_cost", "projected_balance_delta"),
    "review_price_increase": ("price_change",),
    "reduce_spending": ("excess_spend", "projected_balance_delta"),
}
PRIMARY = {"cancel": "recurring_cost", "review_price_increase": "price_change", "reduce_spending": "excess_spend"}
RECURRING_KINDS = {"recurring_cost", "prev_recurring_cost", "price_change", "days_since_parent_spend"}
TREND_KINDS = {"category_spend", "excess_spend", "percent_change"}
FORECAST_KINDS = {"forecast_balance", "forecast_balance_p10", "projected_balance_delta"}
GOAL_KINDS = {"goal_target", "goal_saved", "goal_gap", "goal_gap_after"}
UNIT = {k: "cents" for k in RECURRING_KINDS | TREND_KINDS | FORECAST_KINDS | GOAL_KINDS} | {
    "days_since_parent_spend": "days", "percent_change": "percent"}
NEEDS_MONTH = {"category_spend"} | FORECAST_KINDS | GOAL_KINDS
YEARLY_OK = {"recurring_cost", "price_change", "excess_spend"}
# rounding of a recomputed mean or median; engine re-runs and plain sums are exact
TOL = {"recurring_cost": 1, "prev_recurring_cost": 1, "price_change": 1, "days_since_parent_spend": 0,
       "category_spend": 0, "excess_spend": 1, "percent_change": 1, "forecast_balance": 0,
       "forecast_balance_p10": 0, "projected_balance_delta": 0, "goal_target": 0, "goal_saved": 0, "goal_gap": 0,
       "goal_gap_after": 0}


@dataclass
class Draft:
    lineage_id: str
    attempt: int
    action: str
    target: str
    title: str
    body: str
    trigger_type: str
    trigger_id: str
    claims: list[dict]  # raw, as the Advisor wrote them
    model: str = ""
    prompt_version: str = ""
    lineage_trigger: str | None = None  # a redraft must keep its lineage's trigger

    def as_json(self) -> dict:
        return {"action": self.action, "target": self.target, "title": self.title, "body": self.body,
                "trigger_type": self.trigger_type, "trigger_id": self.trigger_id, "claims": self.claims}


@dataclass
class PatternRow:
    id: int
    type: str
    title: str
    params: dict
    evidence: list[int]


@dataclass
class Context:
    """Everything the Verifier (and the Advisor's tools) read for one run."""
    run_id: str
    as_of_month: str
    patterns: dict[int, PatternRow]
    txns: list[Txn]
    forecast_id: int | None
    forecast_params: dict | None
    goal: goal_tracker.Goal | None = None  # goal_check runs only
    goal_status: str | None = None  # this run's goal_snapshots status (shown to the Advisor; not a claim)
    opening_balance_cents: int = 0
    _base: list[dict] | None = field(default=None, repr=False)

    @classmethod
    def load(cls, conn: sqlite3.Connection, run_id: str) -> Context:
        run = conn.execute("SELECT r.user_id, r.as_of_month, r.goal_id, u.opening_balance_cents FROM runs r "
                           "JOIN users u ON u.id = r.user_id WHERE r.id = ?", (run_id,)).fetchone()
        ev: dict[int, list[int]] = {}
        for r in conn.execute("SELECT pe.pattern_id, pe.txn_id FROM pattern_evidence pe JOIN patterns p "
                              "ON p.id = pe.pattern_id WHERE p.run_id = ?", (run_id,)):
            ev.setdefault(r["pattern_id"], []).append(r["txn_id"])
        patterns = {r["id"]: PatternRow(r["id"], r["type"], r["title"], json.loads(r["params"]), ev.get(r["id"], []))
                    for r in conn.execute("SELECT * FROM patterns WHERE run_id = ? ORDER BY id", (run_id,))}
        fc = conn.execute("SELECT id, params FROM forecasts WHERE run_id = ? AND scenario IS NULL ORDER BY id DESC "
                          "LIMIT 1", (run_id,)).fetchone()
        goal, status = None, None
        if run["goal_id"] is not None:
            g = conn.execute("SELECT * FROM goals WHERE id = ?", (run["goal_id"],)).fetchone()
            goal = goal_tracker.Goal(g["id"], g["name"], g["target_cents"], g["start_month"], g["target_month"])
            snap = conn.execute("SELECT status FROM goal_snapshots WHERE run_id = ?", (run_id,)).fetchone()
            status = snap["status"] if snap else None
        return cls(run_id, run["as_of_month"], patterns, load_txns(conn, run["user_id"], run["as_of_month"]),
                   fc["id"] if fc else None, json.loads(fc["params"]) if fc else None, goal, status,
                   run["opening_balance_cents"])

    @property
    def inputs(self) -> ForecastInputs | None:
        return inputs_from_params(self.forecast_params) if self.forecast_params else None

    def base_points(self) -> list[dict]:
        if self._base is None:
            self._base = project(self.inputs, MAX_HORIZON)
        return self._base


# ---- recomputation from raw data ----

def _evidence(ctx: Context, p: PatternRow) -> list[Txn]:
    ids = set(p.evidence)
    return [t for t in ctx.txns if t.id in ids]  # ctx.txns is sorted by date, id


def recurring_cost(ctx: Context, p: PatternRow) -> int:
    ev = _evidence(ctx, p)
    if p.params["amount_varies"]:
        last3 = [-t.amount_cents for t in ev[-3:]]
        return round(sum(last3) / len(last3))
    since = p.params.get("price_change_date")
    return round(median(-t.amount_cents for t in ev if since is None or t.day.isoformat() >= since))


def prev_recurring_cost(ctx: Context, p: PatternRow) -> int | None:
    since = p.params.get("price_change_date")
    before = [-t.amount_cents for t in _evidence(ctx, p) if since and t.day.isoformat() < since]
    return round(median(before)) if before else None


def days_since_parent_spend(ctx: Context, p: PatternRow) -> int | None:
    ev = _evidence(ctx, p)
    ids, amounts = {t.id for t in ev}, {t.amount_cents for t in ev}
    parent = [t for t in ctx.txns if t.root == p.params["merchant_root"] and t.amount_cents < 0
              and t.id not in ids and t.amount_cents not in amounts]
    if not parent:
        return None
    return (month_end(ctx.as_of_month) - max(t.day for t in parent)).days


def category_spend(ctx: Context, category: str, month: str) -> int:
    return -sum(t.amount_cents for t in ctx.txns if t.category == category and t.amount_cents < 0 and t.month == month)


def trend_levels(ctx: Context, p: PatternRow) -> tuple[int, int]:
    """(latest month spend, mean of the 3 months before it), from transactions."""
    cat, m = p.params["category"], ctx.as_of_month
    prev = [category_spend(ctx, cat, add_months(m, -k)) for k in (1, 2, 3)]
    return category_spend(ctx, cat, m), round(sum(prev) / 3)


def acted_inputs(inputs: ForecastInputs, trigger_id: str, action: str) -> ForecastInputs | None:
    """The forecast inputs if the user takes the action: cancel drops the recurring flow; reduce_spending holds
    the trend flow flat at its first projected month (the rise stops). None if the trigger has no such flow."""
    ref = f"pattern:{trigger_id}"
    want = {"cancel": "recurring", "reduce_spending": "variable"}.get(action)
    flows = [f for f in inputs.flows if f.source_ref == ref and f.kind == want]
    if not flows:
        return None
    f = flows[0]
    if action == "cancel":
        new = tuple(x for x in inputs.flows if x is not f)
    else:
        held = type(f)(f.label, f.kind, f.category, f.source_ref, (f.means[0],) * len(f.means), f.sd_cents)
        new = tuple(held if x is f else x for x in inputs.flows)
    return ForecastInputs(inputs.as_of_month, inputs.start_balance_cents, new)


def balance_delta(ctx: Context, trigger_id: str, action: str, month: str) -> int | None:
    acted = acted_inputs(ctx.inputs, trigger_id, action)
    if acted is None:
        return None
    base = {p["month"]: p["balance_p50"] for p in ctx.base_points()}
    after = {p["month"]: p["balance_p50"] for p in project(acted, MAX_HORIZON)}
    return after[month] - base[month] if month in base else None


def goal_base(ctx: Context) -> int:
    """Balance at the end of the goal's start month: savings are measured from here."""
    return goal_tracker.balance_at(ctx.txns, ctx.opening_balance_cents, ctx.goal.start_month)


def goal_gap(ctx: Context, inputs: ForecastInputs) -> int:
    """target - projected p50 savings at the target month, on the given forecast inputs."""
    return ctx.goal.target_cents - goal_tracker.projected_savings(ctx.goal, goal_base(ctx), project(inputs, MAX_HORIZON))


def _recompute_goal(c: Claim, d: Draft, ctx: Context) -> tuple[int | None, str | None]:
    g = ctx.goal
    if g is None or c.source_ref != f"goal:{g.id}":
        return None, "this run tracks no goal" if g is None else f"must cite this run's goal (goal:{g.id})"
    if c.kind == "goal_saved":
        if c.month != ctx.as_of_month:
            return None, f"goal_saved is as of {ctx.as_of_month}"
        return goal_tracker.balance_at(ctx.txns, ctx.opening_balance_cents, ctx.as_of_month) - goal_base(ctx), None
    if c.month != g.target_month:
        return None, f"month must be the goal's target month {g.target_month}"
    if c.kind == "goal_target":
        return g.target_cents, None
    if ctx.inputs is None or g.target_month <= ctx.as_of_month:
        return None, "no projection to the target month in this run"
    if c.kind == "goal_gap":
        return goal_gap(ctx, ctx.inputs), None
    if d.action not in ("cancel", "reduce_spending"):
        return None, "goal_gap_after only applies to cancel or reduce_spending"
    acted = acted_inputs(ctx.inputs, d.trigger_id, d.action)
    return (goal_gap(ctx, acted), None) if acted else (None, "the trigger has no matching flow in the forecast")


def _recompute(c: Claim, d: Draft, ctx: Context) -> tuple[int | None, str | None]:
    """Recomputed value for one claim, or (None, why not)."""
    kind = c.kind
    if UNIT[kind] != c.unit:
        return None, f"unit must be {UNIT[kind]}"
    if kind in NEEDS_MONTH and c.month is None:
        return None, "month is required for this kind"
    if c.period == "year" and kind not in YEARLY_OK:
        return None, "period 'year' is not allowed for this kind"
    if kind in GOAL_KINDS:
        return _recompute_goal(c, d, ctx)
    if kind in FORECAST_KINDS:
        if ctx.forecast_id is None or c.source_ref != f"forecast:{ctx.forecast_id}":
            return None, f"must cite this run's forecast (forecast:{ctx.forecast_id})"
        if kind == "projected_balance_delta":
            if d.action not in ("cancel", "reduce_spending"):
                return None, "projected_balance_delta only applies to cancel or reduce_spending"
            v = balance_delta(ctx, d.trigger_id, d.action, c.month)
            return v, None if v is not None else "the trigger has no matching flow in the forecast, or month out of range"
        pt = next((p for p in ctx.base_points() if p["month"] == c.month), None)
        if pt is None:
            return None, "month is outside the forecast horizon"
        return (pt["balance_p50"] if kind == "forecast_balance" else pt["balance_p10"]), None

    if c.source_ref != f"pattern:{d.trigger_id}":
        return None, f"must cite the trigger pattern:{d.trigger_id}"
    p = ctx.patterns.get(int(d.trigger_id)) if d.trigger_id.isdigit() else None
    if p is None:
        return None, "trigger pattern not found in this run"
    mult = 12 if c.period == "year" else 1
    if kind in RECURRING_KINDS:
        if p.type != "recurring":
            return None, "needs a recurring pattern"
        if kind == "days_since_parent_spend":
            v = days_since_parent_spend(ctx, p)
            return v, None if v is not None else "no other spend at this merchant"
        if kind == "recurring_cost":
            return mult * recurring_cost(ctx, p), None
        prev = prev_recurring_cost(ctx, p)
        if prev is None:
            return None, "no price change in this series"
        return (prev, None) if kind == "prev_recurring_cost" else (mult * (recurring_cost(ctx, p) - prev), None)
    if p.type != "trend":
        return None, "needs a trend pattern"
    if kind == "category_spend":
        if c.month > ctx.as_of_month:
            return None, "month is after as_of"
        return category_spend(ctx, p.params["category"], c.month), None
    latest, prev3 = trend_levels(ctx, p)
    if kind == "excess_spend":
        return mult * (latest - prev3), None
    if not prev3:
        return None, "no spend in the prior 3 months"
    return round(100 * (latest - prev3) / prev3), None


# ---- numbers in text ----

_MONTH_NAMES = {m: i + 1 for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"))}
_ISO = re.compile(r"\b(\d{4})-(\d{2})(?:-(\d{2}))?\b")
_NAMED = re.compile(r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+(?:(\d{1,2}),?\s+)?(\d{4})\b")
_MONEY = re.compile(r"\$\s?(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d{1,2}))?")
_PCT = re.compile(r"(\d+(?:\.\d+)?)\s?(?:%|percent\b)")
_SPAN = re.compile(r"\b(\d+)[\s-](days?|months?)\b")


@dataclass(frozen=True)
class Number:
    text: str
    kind: str  # money | percent | month | date | days | months
    value: int | float | str
    has_cents: bool = False


def extract_numbers(text: str) -> list[Number]:
    """Money, percents, months/dates and 'N days/months' in text. Other bare integers are ignored (approved)."""
    out: list[Number] = []
    buf = text

    def take(rx: re.Pattern, make) -> None:
        nonlocal buf
        for m in rx.finditer(buf):
            out.append(make(m))
        buf = rx.sub(lambda m: " " * len(m.group(0)), buf)

    take(_ISO, lambda m: Number(m.group(0), "date" if m.group(3) else "month", m.group(0)))
    take(_NAMED, lambda m: Number(m.group(0), "date" if m.group(2) else "month",
                                  f"{m.group(3)}-{_MONTH_NAMES[m.group(1).lower()]:02d}"))
    take(_MONEY, lambda m: Number(m.group(0), "money", int(m.group(1).replace(",", "")) * 100
                                  + (int(m.group(2).ljust(2, "0")) if m.group(2) else 0), m.group(2) is not None))
    take(_PCT, lambda m: Number(m.group(0), "percent", float(m.group(1))))
    take(_SPAN, lambda m: Number(m.group(0), "days" if m.group(2).startswith("day") else "months", int(m.group(1))))
    return out


def matches(n: Number, f, as_of_month: str) -> bool:
    """Does number n (from text) state fact f (a Claim, or anything with value/unit/month/period)?"""
    if n.kind == "money":
        if f.unit != "cents" or not isinstance(f.value, int):
            return False
        v = abs(f.value)
        return v == n.value if n.has_cents else (v + 50) // 100 == n.value // 100
    if n.kind == "percent":
        return f.unit == "percent" and abs(abs(f.value) - n.value) <= 0.5
    if n.kind == "month":
        return getattr(f, "month", None) == n.value
    if n.kind == "days":
        return f.unit == "days" and f.value == n.value
    if n.kind == "months":
        return (getattr(f, "month", None) == add_months(as_of_month, n.value)
                or (n.value == 12 and getattr(f, "period", None) == "year")
                or (f.unit == "months" and f.value == n.value))
    return False  # full dates: no claim kind carries a day


def ungrounded(text: str, facts: list, as_of_month: str) -> list[Number]:
    return [n for n in extract_numbers(text) if not any(matches(n, f, as_of_month) for f in facts)]


def number_report(text: str, claims: list[Claim], ok: list[bool], as_of_month: str) -> tuple[int, int]:
    """(numbers in text, numbers that match a claim whose recompute passed). For the grounded-number rate."""
    nums = extract_numbers(text)
    good = [c for c, k in zip(claims, ok) if k]
    return len(nums), sum(any(matches(n, c, as_of_month) for c in good) for n in nums)


# ---- verdict ----

@dataclass
class Report:
    verdict: str  # pass | reject
    reasons: list[str]
    checks: list[dict]  # ClaimCheck shape
    rule_checks: list[dict]  # RuleCheck shape
    claims: list[Claim]  # the well-formed claims
    impact_annual_cents: int | None
    loose: list[Number] = field(default_factory=list)  # numbers in the text no claim backs
    as_of_month: str = ""

    def feedback(self, per_number: bool = False) -> list[dict]:
        """Structured failures for the Advisor's redraft. per_number (adv-v2): one entry per unbacked number with
        the nearest claim of the same unit, or an instruction to remove the phrase."""
        out = [{"check": f"claim {c['claim']['kind']} ({c['claim']['source_ref']}"
                         + (f", {c['claim']['month']}" if c['claim'].get('month') else "") + ")",
                "claimed": c["claim"]["value"], "recomputed": c["recomputed"], "note": c["note"]}
               for c in self.checks if not c["ok"]]
        for r in self.rule_checks:
            if r["ok"]:
                continue
            if r["rule"] == "numbers" and per_number:
                out += [number_hint(n, self.claims, self.as_of_month) for n in self.loose]
            else:
                out.append({"check": r["rule"], "note": r["note"]})
        return out


def _money(cents: int) -> str:
    return f"${abs(cents) / 100:,.2f}"


def number_hint(n: Number, claims: list[Claim], as_of_month: str) -> dict:
    """What to do about one number in the text that no claim backs."""
    out = {"check": "numbers", "text": n.text}
    remove = f"remove the phrase containing {n.text!r}: none of your claims backs it"
    if n.kind in ("money", "percent", "days"):
        unit = {"money": "cents", "percent": "percent", "days": "days"}[n.kind]
        same = [c for c in claims if c.unit == unit]
        if not same:
            return out | {"instruction": remove}
        c = min(same, key=lambda c: abs(abs(c.value) - n.value))
        allowed = ({"money": f"{_money(c.value)} or ${(abs(c.value) + 50) // 100:,}",
                    "percent": f"{abs(c.value)}%", "days": f"{c.value} days"}[n.kind])
        return out | {"nearest_claim": {"kind": c.kind, "value": c.value, "month": c.month, "period": c.period},
                      "instruction": f"if you meant this claim, write it exactly as {allowed}; otherwise {remove}"}
    months = sorted({c.month for c in claims if c.month})
    if not months:
        return out | {"instruction": remove}
    if n.kind == "months":
        spans = [f"{len(months_between(as_of_month, m)) - 1} months" for m in months if m > as_of_month]
        allowed = ", ".join(spans) if spans else "none"
        return out | {"instruction": f"only these spans are backed by your claims' months: {allowed}; otherwise {remove}"}
    return out | {"instruction": f"write a month only as one of your claims' months ({', '.join(months)}), "
                                 f"never a full date; otherwise {remove}"}


def _norm(s: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", s.upper())


def _target_ok(target: str, p: PatternRow) -> bool:
    words = [_norm(w) for w in re.split(r"[\s_\-/]+", target)]
    words = [w for w in words if len(w) >= 3]
    if p.type == "recurring":
        hay = _norm(p.params["merchant"]) + " " + _norm(p.params["merchant_root"])
    else:
        hay = _norm(p.params["category"])
    return bool(words) and any(w in hay for w in words)


def _action_supported(d: Draft, p: PatternRow, ctx: Context) -> tuple[bool, str]:
    pr = p.params
    if d.action == "cancel":
        if p.type != "recurring":
            return False, "cancel needs a recurring pattern"
        if not (pr["active"] and pr["possibly_unused"] and not pr["amount_varies"]):
            return False, (f"{pr['merchant']} is not an active, possibly-unused membership "
                           f"(active={pr['active']}, possibly_unused={pr['possibly_unused']}, "
                           f"amount_varies={pr['amount_varies']})")
        return True, ""
    if d.action == "review_price_increase":
        if p.type != "recurring" or not pr.get("price_changed"):
            return False, "review_price_increase needs a recurring pattern with a price change"
        prev = prev_recurring_cost(ctx, p)
        if prev is None or recurring_cost(ctx, p) <= prev or not pr["active"]:
            return False, "the price did not go up, or the series is no longer active"
        return True, ""
    if p.type != "trend":
        return False, "reduce_spending needs a trend pattern"
    latest, prev3 = trend_levels(ctx, p)
    if pr["slope_cents_per_month"] <= 0 or latest <= prev3:
        return False, (f"{pr['category']} is not rising above its recent level "
                       f"(slope {pr['slope_cents_per_month']}, latest {latest} vs prior-3 mean {prev3})")
    return True, ""


def _impact(d: Draft, p: PatternRow, ctx: Context) -> int:
    if d.action == "cancel":
        return 12 * recurring_cost(ctx, p)
    if d.action == "review_price_increase":
        return 12 * (recurring_cost(ctx, p) - prev_recurring_cost(ctx, p))
    latest, prev3 = trend_levels(ctx, p)
    return 12 * (latest - prev3)


def verify(d: Draft, ctx: Context) -> Report:
    rules: list[dict] = []
    reasons: list[str] = []

    def rule(name: str, ok: bool, note: str = "") -> None:
        rules.append({"rule": name, "ok": ok, "note": note})
        if not ok:
            reasons.append(f"{name}: {note}")

    claims, bad = [], []
    for i, raw in enumerate(d.claims):
        try:
            claims.append(Claim.model_validate(raw))
        except ValidationError as e:
            bad.append(f"claim {i + 1} invalid ({e.errors()[0]['loc'][-1]}: {e.errors()[0]['msg']})")
    problems = bad + ([] if d.action in ACTIONS else [f"unknown action {d.action!r}"]) \
        + ([] if d.trigger_type == "pattern" else [f"trigger_type must be 'pattern', got {d.trigger_type!r}"]) \
        + ([] if d.claims else ["no claims"])
    rule("schema", not problems, "; ".join(problems))

    p = ctx.patterns.get(int(d.trigger_id)) if d.trigger_id.isdigit() else None
    if d.lineage_trigger is not None and d.trigger_id != d.lineage_trigger:
        rule("trigger", False, f"a redraft must keep trigger pattern {d.lineage_trigger}, got {d.trigger_id}")
    else:
        rule("trigger", p is not None, "" if p else f"pattern {d.trigger_id} is not in this run")

    supported = False
    if p is not None:
        named = _target_ok(d.target, p)
        rule("target", named, "" if named else f"target {d.target!r} does not name "
             + (p.params["merchant"] if p.type == "recurring" else p.params["category"]))
        if d.action in ACTIONS:
            supported, why = _action_supported(d, p, ctx)
            rule("action", supported, why)
    if d.action in ACTIONS:
        missing = [k for k in REQUIRED[d.action] if k not in {c.kind for c in claims}]
        rule("required_claims", not missing, f"{d.action} needs claims: {', '.join(missing)}" if missing else "")

    checks = []
    for c in claims:
        v, note = _recompute(c, d, ctx)
        tol = TOL[c.kind] * (12 if c.period == "year" else 1)
        ok = v is not None and abs(c.value - v) <= tol
        if v is not None and not ok:
            note = f"claimed {c.value}, recomputed {v}"
        checks.append({"claim": c.model_dump(), "recomputed": v, "tolerance": tol, "ok": ok, "note": note})
        if not ok:
            reasons.append(f"claim {c.kind} ({c.source_ref}): {note}")

    loose = ungrounded(f"{d.title}\n{d.body}", claims, ctx.as_of_month)
    rule("numbers", not loose, "; ".join(f"'{n.text}' in the text matches no claim" for n in loose))

    passed = all(r["ok"] for r in rules) and all(c["ok"] for c in checks)
    impact = _impact(d, p, ctx) if passed else None
    return Report("pass" if passed else "reject", reasons, checks, rules, claims, impact, loose, ctx.as_of_month)
