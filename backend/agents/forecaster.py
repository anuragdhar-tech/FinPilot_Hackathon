"""Forecaster agent: deterministic month-by-month balance projection with a p10-p90 band.

Two parts:
- build_inputs(): turns history (transactions on or before as_of) and the Analyzer's patterns into
  a start balance plus a list of Flows: active recurring charges at their current price, variable
  spend per category (3-month trailing mean, or a continued trend line for trending categories),
  income (6-month trailing mean of all inflows) and, from fc-v2, a one-off allowance (6-month
  trailing mean of the spend the Analyzer flagged as anomalies, which the variable flows leave out).
- project(): pure engine over those Flows. A what-if scenario later is just more Flows.

Band: each flow has a monthly standard deviation; months are assumed independent and roughly normal,
so the balance spread after k months is sqrt(sum of variances) and p10/p90 = p50 -/+ 1.2816 sigma.
"""
from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import asdict, dataclass
from typing import Literal

from backend.agents.analyzer import Detected
from backend.ledger import Txn, add_months, month_end, months_between
from backend.stats import sample_sd, theil_sen

MAX_HORIZON = 12
VARIABLE_MONTHS = 3  # (J)
INCOME_MONTHS = 6  # approved
ONE_OFF_MONTHS = 6  # (J) one-offs are rare; a shorter window is mostly zeros
Z80 = 1.2816  # p10/p90 of a normal
MIN_HISTORY_MONTHS = 3  # fewer complete months: no projection and no advice (approved Oct 16)
EDGE_SLACK_DAYS = 7  # an edge month counts as complete if the data reaches within a week of its start/end
# fc-v1: Day 3 baseline. fc-v2: adds the one-off allowance flow (added after seeing the fc-v1 backtest).
VERSIONS = ("fc-v1", "fc-v2")
METHOD_VERSION = "fc-v2"
_METHODS = {
    "fc-v1": "recurring at current price + 3-mo trailing variable spend (trend line where flagged) + 6-mo trailing income",
    "fc-v2": "recurring at current price + 3-mo trailing variable spend (trend line where flagged) + 6-mo trailing "
             "income + 6-mo trailing one-off allowance",
}
METHOD = _METHODS[METHOD_VERSION]


@dataclass(frozen=True)
class Flow:
    label: str
    kind: Literal["income", "recurring", "variable", "one_off"]
    category: str | None
    source_ref: str
    means: tuple[int, ...]  # signed cents per projected month (negative = outflow)
    sd_cents: int


@dataclass(frozen=True)
class ForecastInputs:
    as_of_month: str
    start_balance_cents: int
    flows: tuple[Flow, ...]


@dataclass
class ForecastResult:
    inputs: ForecastInputs
    horizon_months: int
    points: list[dict]  # ForecastPoint shape
    trend_adjusted: list[str]
    version: str = METHOD_VERSION

    def params(self) -> dict:
        return {"version": self.version, "as_of_month": self.inputs.as_of_month,
                "start_balance_cents": self.inputs.start_balance_cents, "variable_months": VARIABLE_MONTHS,
                "income_months": INCOME_MONTHS, "one_off_months": ONE_OFF_MONTHS,
                "trend_adjusted": self.trend_adjusted, "flows": [asdict(f) for f in self.inputs.flows]}


def project(inputs: ForecastInputs, horizon: int) -> list[dict]:
    if not 1 <= horizon <= MAX_HORIZON:
        raise ValueError(f"horizon must be 1..{MAX_HORIZON}")
    points, balance, var = [], inputs.start_balance_cents, 0.0
    for k in range(horizon):
        net = sum(f.means[k] for f in inputs.flows)
        var += sum(f.sd_cents ** 2 for f in inputs.flows)
        balance += net
        sigma = math.sqrt(var)
        by_cat: dict[str, int] = defaultdict(int)
        for f in inputs.flows:
            if f.kind != "income":
                by_cat[f.category or "uncategorized"] += -f.means[k]
        points.append({
            "month": add_months(inputs.as_of_month, k + 1),
            "balance_p10": round(balance - Z80 * sigma), "balance_p50": balance,
            "balance_p90": round(balance + Z80 * sigma), "net_flow_cents": net,
            "income_cents": sum(f.means[k] for f in inputs.flows if f.kind == "income"),
            "spend_cents": -sum(f.means[k] for f in inputs.flows if f.kind != "income"),
            "by_category": dict(sorted(by_cat.items())),
        })
    return points


def inputs_from_params(params: dict) -> ForecastInputs:
    """Rebuild the engine inputs from a stored forecasts.params row (what-if and the Verifier re-run project())."""
    flows = tuple(Flow(f["label"], f["kind"], f["category"], f["source_ref"], tuple(f["means"]), f["sd_cents"])
                  for f in params["flows"])
    return ForecastInputs(params["as_of_month"], params["start_balance_cents"], flows)


def complete_months(txns: list[Txn], as_of_month: str) -> int:
    """Calendar months on or before as_of fully covered by the data. The first month counts if the first
    transaction is in its first week, the last month if the last one is in its last week (bank exports start
    and end mid-month); every month in between counts, months after the last transaction do not."""
    txns = [t for t in txns if t.day <= month_end(as_of_month)]
    if not txns:
        return 0
    first, last = min(t.day for t in txns), max(t.day for t in txns)
    n = (len(months_between(f"{first:%Y-%m}", f"{last:%Y-%m}")) - (first.day > EDGE_SLACK_DAYS)
         - ((month_end(f"{last:%Y-%m}") - last).days >= EDGE_SLACK_DAYS))
    return max(0, n)


def insufficient_reason(txns: list[Txn], as_of_month: str) -> str | None:
    n = complete_months(txns, as_of_month)
    if n >= MIN_HISTORY_MONTHS:
        return None
    return (f"Not enough history yet, need at least {MIN_HISTORY_MONTHS} months "
            f"(have {n} complete month{'' if n == 1 else 's'} up to {as_of_month}).")


def _monthly(txns: list[Txn], months: list[str]) -> list[int]:
    totals = dict.fromkeys(months, 0)
    for t in txns:
        if t.month in totals:
            totals[t.month] += t.amount_cents
    return [totals[m] for m in months]


def build_inputs(txns: list[Txn], patterns: list[Detected], opening_balance_cents: int, as_of_month: str,
                 horizon: int = MAX_HORIZON, trend_adjust: bool = True,
                 version: str = METHOD_VERSION) -> tuple[ForecastInputs, list[str]]:
    if version not in VERSIONS:
        raise ValueError(f"unknown forecast version {version}")
    txns = [t for t in txns if t.day <= month_end(as_of_month)]
    start = opening_balance_cents + sum(t.amount_cents for t in txns)
    hist = months_between(txns[0].month, as_of_month) if txns else [as_of_month]
    flows: list[Flow] = []

    def ref(i: int, p: Detected) -> str:
        return f"pattern:{p.id if p.id is not None else i}"

    excluded: set[int] = set()
    one_off_ids: set[int] = set()
    for i, p in enumerate(patterns):
        if p.type == "anomaly":
            excluded.add(p.params["txn_id"])
            one_off_ids.add(p.params["txn_id"])
        if p.type != "recurring":
            continue
        excluded.update(p.evidence)
        if not p.params["active"]:
            continue
        if p.params["amount_varies"]:
            last3 = [t.amount_cents for t in txns if t.id in set(p.evidence)][-3:]
            mean, sd = round(sum(last3) / len(last3)), round(sample_sd(last3))
        else:
            mean, sd = p.params["amount_cents"], 0
        flows.append(Flow(p.params["merchant"], "recurring", p.params["category"], ref(i, p), (mean,) * horizon, sd))

    var_months = hist[-VARIABLE_MONTHS:]
    by_cat: dict[str, list[Txn]] = defaultdict(list)
    for t in txns:
        if t.amount_cents < 0 and t.id not in excluded:
            by_cat[t.category or "uncategorized"].append(t)
    trends = {p.params["category"]: (i, p) for i, p in enumerate(patterns) if p.type == "trend"} if trend_adjust else {}
    adjusted = []
    for cat in sorted(by_cat):
        recent = _monthly(by_cat[cat], var_months)
        sd = round(sample_sd(recent))
        if cat in trends:
            i, p = trends[cat]
            window = [m for m in hist if m in p.params["monthly_totals"]]
            slope, intercept = theil_sen([-v for v in _monthly(by_cat[cat], window)])
            level = intercept + slope * (len(window) - 1)
            means = tuple(-round(max(0.0, level + slope * k)) for k in range(1, horizon + 1))
            flows.append(Flow(f"{cat} (trend)", "variable", cat, ref(i, p), means, sd))
            adjusted.append(cat)
        else:
            mean = round(sum(recent) / len(recent))
            flows.append(Flow(cat, "variable", None if cat == "uncategorized" else cat, f"category:{cat}",
                              (mean,) * horizon, sd))

    if version != "fc-v1":  # spend the variable flows leave out still recurs as *some* one-off most months
        one_offs = _monthly([t for t in txns if t.id in one_off_ids and t.amount_cents < 0], hist[-ONE_OFF_MONTHS:])
        flows.append(Flow("one-off allowance", "one_off", "one_off", "anomalies",
                          (round(sum(one_offs) / len(one_offs)),) * horizon, round(sample_sd(one_offs))))

    inc = _monthly([t for t in txns if t.amount_cents > 0], hist[-INCOME_MONTHS:])
    flows.append(Flow("income", "income", "income", "inflows", (round(sum(inc) / len(inc)),) * horizon,
                      round(sample_sd(inc))))
    return ForecastInputs(as_of_month, start, tuple(flows)), adjusted


def forecast(txns: list[Txn], patterns: list[Detected], opening_balance_cents: int, as_of_month: str,
             horizon: int = MAX_HORIZON, trend_adjust: bool = True, version: str = METHOD_VERSION) -> ForecastResult:
    inputs, adjusted = build_inputs(txns, patterns, opening_balance_cents, as_of_month, horizon, trend_adjust, version)
    return ForecastResult(inputs, horizon, project(inputs, horizon), adjusted, version)
