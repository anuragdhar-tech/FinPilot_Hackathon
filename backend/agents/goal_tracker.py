"""Goal tracker: code-only comparison of a savings goal with this month's forecast (the compare_to_plan step). No LLM.

Savings = growth of this account's balance since the end of the goal's start month, from transactions plus the
opening balance (a transfer to a savings account elsewhere counts as spend). Status at the clock month:
- ahead: even the low end (p10) of the projection reaches the target by target_month
- on_track: the expected (p50) projection reaches it, p10 does not
- behind: p50 falls short; gap = target - projected p50 savings
Same p10/p50 rule as the what-if verdict. Once the clock reaches target_month the goal is final: achieved or missed
from the actual balance at the end of target_month, no forecast.

The advice signature (status + the actions the patterns support) decides whether a check calls the Advisor again.
"""
from __future__ import annotations

from dataclasses import dataclass

from backend.ledger import Txn, months_between

STATUSES = ("ahead", "on_track", "behind", "achieved", "missed")
MAX_TARGET_MONTHS = 12  # the forecast horizon: the target month must be projectable from the start month


@dataclass(frozen=True)
class Goal:
    id: int
    name: str
    target_cents: int
    start_month: str
    target_month: str


@dataclass(frozen=True)
class Check:
    month: str
    status: str
    saved_cents: int
    planned_cents: int
    projected_p50_cents: int | None  # projected savings at target_month; None once final
    projected_p10_cents: int | None
    gap_cents: int  # target - projected p50 savings (final: target - saved); negative = ahead by that much
    projected_completion: str | None  # first month p50 savings reach the target; None if not within the horizon

    @property
    def final(self) -> bool:
        return self.status in ("achieved", "missed")


def balance_at(txns: list[Txn], opening_balance_cents: int, month: str) -> int:
    """Balance at the end of month: opening balance plus every transaction up to then."""
    return opening_balance_cents + sum(t.amount_cents for t in txns if t.month <= month)


def planned(goal: Goal, month: str) -> int:
    """Straight-line plan: 0 at the end of start_month, the full target at the end of target_month."""
    n = len(months_between(goal.start_month, goal.target_month)) - 1
    k = min(max(len(months_between(goal.start_month, month)) - 1, 0), n)
    return round(goal.target_cents * k / n)


def projected_savings(goal: Goal, base_cents: int, points: list[dict], key: str = "balance_p50") -> int:
    """Savings at target_month on a projection (forecaster.project() points)."""
    pt = next(p for p in points if p["month"] == goal.target_month)
    return pt[key] - base_cents


def status_for(target_cents: int, p50_saved: int, p10_saved: int) -> str:
    if p10_saved >= target_cents:
        return "ahead"
    return "on_track" if p50_saved >= target_cents else "behind"


def check(goal: Goal, txns: list[Txn], opening_balance_cents: int, points: list[dict], as_of_month: str) -> Check:
    """Status of the goal at as_of_month. txns are the user's transactions on or before as_of_month; points are this
    month's baseline forecast."""
    base = balance_at(txns, opening_balance_cents, goal.start_month)
    target = goal.target_cents
    if as_of_month >= goal.target_month:
        saved = balance_at(txns, opening_balance_cents, goal.target_month) - base
        return Check(as_of_month, "achieved" if saved >= target else "missed", saved, target, None, None,
                     target - saved, None)
    saved = balance_at(txns, opening_balance_cents, as_of_month) - base
    p50, p10 = projected_savings(goal, base, points), projected_savings(goal, base, points, "balance_p10")
    completion = as_of_month if saved >= target else next(
        (p["month"] for p in points if p["balance_p50"] - base >= target), None)
    return Check(as_of_month, status_for(target, p50, p10), saved, planned(goal, as_of_month), p50, p10,
                 target - p50, completion)


# ---- when to call the Advisor again ----

def actionable(patterns: list[tuple[str, dict]]) -> list[str]:
    """The actions the stored pattern params support, as 'action target' keys (pattern ids change every run).
    Mirrors the Verifier's action rule; the Verifier still rechecks each draft against the transactions."""
    out = set()
    for type_, pr in patterns:
        if type_ == "recurring" and pr["active"]:
            if pr["possibly_unused"] and not pr["amount_varies"]:
                out.add(f"cancel {pr['merchant']}")
            if pr["price_changed"] and pr["prev_amount_cents"] is not None and pr["amount_cents"] < pr["prev_amount_cents"]:
                out.add(f"review_price_increase {pr['merchant']}")
        elif type_ == "trend" and pr["slope_cents_per_month"] > 0 and pr["latest_month_cents"] > pr["prev_3mo_mean_cents"]:
            out.add(f"reduce_spending {pr['category']}")
    return sorted(out)


def signature(status: str, patterns: list[tuple[str, dict]]) -> dict:
    return {"status": status, "actions": actionable(patterns)}


def decide(c: Check, sig: dict, prev_sig: dict | None, prev_month: str | None) -> tuple[str, str]:
    """('advise' | 'carry' | 'final', reason). prev_sig is the signature of the last check whose advice run finished."""
    if c.final:
        return "final", f"target month reached: goal {c.status}; no advice"
    if prev_sig is None:
        return "advise", "first check of this goal"
    if prev_sig["status"] != sig["status"]:
        return "advise", f"status changed: {prev_sig['status']} -> {sig['status']}"
    added = [a for a in sig["actions"] if a not in prev_sig["actions"]]
    gone = [a for a in prev_sig["actions"] if a not in sig["actions"]]
    if added or gone:
        return "advise", "patterns changed: " + "; ".join([f"+{a}" for a in added] + [f"-{a}" for a in gone])
    return "carry", f"status and actionable patterns unchanged since {prev_month}; Advisor not called"
