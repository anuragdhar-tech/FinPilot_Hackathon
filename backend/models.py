"""API contract. These models are the source of truth for the frontend types.

Money is integer cents (negative = outflow). Dates are ISO 'YYYY-MM-DD'; months are 'YYYY-MM'.
"""
from typing import Annotated, Literal

from pydantic import BaseModel, Field, model_validator

Month = Annotated[str, Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")]
IsoDate = Annotated[str, Field(pattern=r"^\d{4}-\d{2}-\d{2}$")]

CATEGORIES = (
    "income", "housing", "utilities", "groceries", "dining", "food_delivery", "transport",
    "shopping", "entertainment", "health", "fitness", "travel", "transfers", "fees", "other",
)
Category = Literal[
    "income", "housing", "utilities", "groceries", "dining", "food_delivery", "transport",
    "shopping", "entertainment", "health", "fitness", "travel", "transfers", "fees", "other",
]

RunKind = Literal["main", "whatif", "goal_check"]
RunState = Literal["pending", "running", "done", "failed"]
EventStatus = Literal["ok", "rejected", "error"]
TriggerType = Literal["pattern", "forecast", "goal", "scenario"]


# ---- ingest / runs ----

class UploadResponse(BaseModel):
    batch_id: str
    user_id: int
    count: int  # rows stored (new)
    duplicates_skipped: int  # same external_id already stored for this user, or repeated in the file


class User(BaseModel):
    id: int
    name: str
    opening_balance_cents: int
    opening_date: IsoDate
    as_of_month: Month
    txn_count: int
    is_active: bool  # owner of the latest upload; GET endpoints default to this user


class UserList(BaseModel):
    items: list[User]


class UserDeleted(BaseModel):
    user_id: int
    deleted_rows: dict[str, int]  # rows removed per table; merchant_cache is shared and never touched


class RunCreate(BaseModel):
    batch_id: str


class RunCreated(BaseModel):
    run_id: str


class AgentEvent(BaseModel):
    seq: int
    node: str
    attempt: int = 1
    status: EventStatus
    summary: str = ""
    input_ref: str | None = None
    output_ref: str | None = None
    model: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    duration_ms: int | None = None
    ts: str


class RunStatus(BaseModel):
    run_id: str
    kind: RunKind
    status: RunState
    as_of_month: Month
    started_at: str
    finished_at: str | None = None
    error: str | None = None
    events: list[AgentEvent]


class RunSummary(BaseModel):
    run_id: str
    user_id: int
    batch_id: str | None = None
    kind: RunKind
    status: RunState
    as_of_month: Month
    started_at: str
    finished_at: str | None = None
    error: str | None = None
    event_count: int


class RunList(BaseModel):
    items: list[RunSummary]  # newest first


# ---- transactions ----

class Transaction(BaseModel):
    id: int
    external_id: str
    date: IsoDate
    description: str
    amount_cents: int
    category: Category | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)
    rationale: str | None = None
    model: str | None = None
    prompt_version: str | None = None
    from_cache: bool | None = None  # all category fields are None when uncategorized


class TransactionList(BaseModel):
    items: list[Transaction]
    total: int


# ---- patterns (discriminated on `type`) ----

class RecurringParams(BaseModel):
    merchant: str
    merchant_root: str  # grouping key; parent spend for "unused" is other spend at the same root
    category: Category | None = None
    cadence: Literal["monthly"]
    n: int
    period_days: float
    amount_cents: int  # current price (mean of the last 3 charges when amount_varies)
    monthly_cents: int
    amount_varies: bool
    first_seen: IsoDate
    last_seen: IsoDate
    active: bool  # last charge within 1.5 periods of the end of as_of_month
    price_changed: bool = False
    prev_amount_cents: int | None = None
    price_change_date: IsoDate | None = None  # first charge at the new price
    possibly_unused: bool = False
    parent_last_spend: IsoDate | None = None
    parent_last_txn_id: int | None = None
    days_since_parent_spend: int | None = None  # counted to the end of as_of_month


class AnomalyParams(BaseModel):
    txn_id: int
    kind: Literal["spike", "duplicate", "new_merchant"]
    date: IsoDate
    amount_cents: int
    merchant_root: str | None = None
    baseline_cents: int | None = None  # spike: merchant median; new_merchant: typical purchase
    mad_cents: int | None = None
    robust_z: float | None = None  # None when the baseline has no spread
    n_baseline: int | None = None
    related_txn_id: int | None = None  # duplicate: the earlier charge


class TrendParams(BaseModel):
    category: Category
    months: int
    slope_cents_per_month: int  # Theil-Sen, on positive monthly outflow
    start_cents: int  # fitted value, first month of the window
    end_cents: int  # fitted value, last month of the window
    latest_month_cents: int
    prev_3mo_mean_cents: int  # mean of the 3 months before the latest
    mk_p: float  # Mann-Kendall two-sided p-value
    monthly_totals: dict[Month, int]


class _PatternBase(BaseModel):
    id: int
    title: str
    narration: str = ""
    evidence_txn_ids: list[int]


class RecurringPattern(_PatternBase):
    type: Literal["recurring"] = "recurring"
    params: RecurringParams


class AnomalyPattern(_PatternBase):
    type: Literal["anomaly"] = "anomaly"
    params: AnomalyParams


class TrendPattern(_PatternBase):
    type: Literal["trend"] = "trend"
    params: TrendParams


Pattern = Annotated[RecurringPattern | AnomalyPattern | TrendPattern, Field(discriminator="type")]


class PatternList(BaseModel):
    items: list[Pattern]
    run_id: str | None = None  # latest finished run for the user; None if none yet
    as_of_month: Month | None = None


# ---- forecast / what-if ----

class ForecastPoint(BaseModel):
    month: Month
    balance_p10: int
    balance_p50: int
    balance_p90: int
    net_flow_cents: int
    income_cents: int = 0
    spend_cents: int = 0  # positive = money out
    by_category: dict[str, int] = {}  # projected spend per category (p50, positive), incl. "uncategorized", "one_off"


class Forecast(BaseModel):
    id: int
    as_of_month: Month
    horizon_months: int = Field(gt=0)
    method: str
    start_balance_cents: int  # balance at the end of as_of_month
    points: list[ForecastPoint]  # empty when insufficient_data
    insufficient_data: bool = False  # under 3 complete months on or before as_of: no projection, no advice
    reason: str | None = None


class ScenarioItem(BaseModel):
    kind: Literal["one_off", "recurring_change"]
    label: str
    amount_cents: int
    start_month: Month
    months: int | None = Field(default=None, gt=0)  # recurring_change only; None = open-ended


class Scenario(BaseModel):
    items: list[ScenarioItem] = Field(min_length=1)


class SimulateRequest(BaseModel):
    text: str | None = None
    scenario: Scenario | None = None
    horizon_months: int = Field(default=6, ge=1, le=12)  # extended to cover the scenario's start month

    @model_validator(mode="after")
    def exactly_one(self):
        if (self.text is None) == (self.scenario is None):
            raise ValueError("provide exactly one of 'text' or 'scenario'")
        return self


class NumberCheck(BaseModel):
    passed: bool  # the LLM explanation's numbers all match code-computed facts
    issues: list[str] = []  # every failed attempt's unmatched numbers, also when the template was used


class LowPoint(BaseModel):
    month: Month
    cents: int


class SimulateResponse(BaseModel):
    scenario: Scenario  # echoed back so the UI shows what the parser understood
    baseline: list[ForecastPoint]
    projected: list[ForecastPoint]
    end_delta_cents: int
    # code rule over the scenario trajectory: p10 >= $0 every month -> affordable; else p50 >= $0 -> tight;
    # else not_affordable
    verdict: Literal["affordable", "tight", "not_affordable"]
    lowest_p50: LowPoint
    lowest_p10: LowPoint
    baseline_forecast_id: int
    scenario_forecast_id: int
    run_id: str
    explanation: str
    explanation_source: Literal["llm", "template"]  # template = the LLM text failed the number check twice
    number_check: NumberCheck


# ---- recommendations / audit ----

ClaimKind = Literal[
    "recurring_cost", "prev_recurring_cost", "price_change", "days_since_parent_spend",  # recurring pattern
    "category_spend", "excess_spend", "percent_change",  # trend pattern
    "forecast_balance", "forecast_balance_p10", "projected_balance_delta",  # the run's forecast
    "goal_target", "goal_saved", "goal_gap", "goal_gap_after",  # the goal of a goal_check run
]
Action = Literal["cancel", "review_price_increase", "reduce_spending"]


class Claim(BaseModel):
    """One number in a recommendation; the Verifier recomputes it from the DB (backend/agents/verifier.py)."""
    kind: ClaimKind
    value: int  # cents, days or integer percent (see unit)
    unit: Literal["cents", "days", "percent"]
    source_ref: Annotated[str, Field(pattern=r"^(pattern|forecast|goal|scenario|txn):\d+$")]
    month: Month | None = None  # category_spend, forecast_balance*, projected_balance_delta, goal_* (target month;
    # goal_saved: as_of)
    period: Literal["month", "year"] | None = None  # year = 12 x the monthly figure


class Recommendation(BaseModel):
    id: int
    lineage_id: str
    action: Action
    target: str
    title: str
    body: str
    claims: list[Claim]
    trigger_type: TriggerType
    trigger_id: str
    goal_id: int | None = None
    rank: int  # 1 = largest verifier-recomputed annual impact
    impact_annual_cents: int  # recomputed by the Verifier, not taken from the LLM
    attempts: int  # drafts in this lineage (2+ = revised after a rejection)
    created_at: str


class RecommendationList(BaseModel):
    items: list[Recommendation]  # accepted only, ranked
    run_id: str | None = None
    as_of_month: Month | None = None


class ClaimCheck(BaseModel):
    claim: Claim
    recomputed: int | None
    tolerance: int
    ok: bool
    note: str | None = None


class RuleCheck(BaseModel):
    rule: Literal["schema", "trigger", "target", "action", "required_claims", "numbers"]
    ok: bool
    note: str = ""


class AuditAttempt(BaseModel):
    attempt: int
    rec_id: int
    status: Literal["draft", "rejected", "accepted", "dropped"]
    action: str
    target: str
    title: str
    body: str
    claims: list[Claim]
    verdict: Literal["pass", "reject"]
    reasons: list[str]
    checks: list[ClaimCheck]
    rule_checks: list[RuleCheck]
    ts: str


class AuditInputs(BaseModel):
    pattern_ids: list[int] = []
    forecast_id: int | None = None
    as_of_month: Month
    goal_id: int | None = None  # goal_check runs


class AuditTrigger(BaseModel):
    title: str
    evidence_txn_ids: list[int] = []


class AuditRecord(BaseModel):
    recommendation_id: int
    lineage_id: str
    run_id: str
    trigger_type: TriggerType
    trigger_id: str
    trigger: AuditTrigger | None = None
    inputs: AuditInputs
    model: str
    prompt_version: str
    attempts: list[AuditAttempt]  # every draft of the lineage, rejected ones included


# ---- goals ----

GoalStatus = Literal["ahead", "on_track", "behind", "achieved", "missed"]


class GoalCreate(BaseModel):
    """Save target_cents (balance growth of this account) between the end of start_month and the end of
    target_month. start_month defaults to the profile's as_of; an earlier one rewinds the clock for replay."""
    name: str = Field(min_length=1, max_length=80)
    target_cents: int = Field(gt=0)
    target_month: Month
    start_month: Month | None = None


class Goal(BaseModel):
    id: int
    user_id: int
    name: str
    target_cents: int
    start_month: Month
    target_month: Month
    created_at: str


class GoalList(BaseModel):
    items: list[Goal]  # at most one: one active goal per profile


class GoalMonth(BaseModel):
    """One goal check (one replayed month). Status and figures are computed in code from that month's forecast."""
    month: Month
    status: GoalStatus
    saved_cents: int
    planned_cents: int  # straight-line plan
    projected_p50_cents: int | None  # projected savings at target_month; None once final
    projected_p10_cents: int | None
    gap_cents: int  # target - projected p50 savings (final: target - saved); negative = ahead
    projected_completion: Month | None
    run_id: str
    run_status: RunState
    decision: Literal["advise", "carry", "final"]  # advise = Advisor called this month; carry = unchanged
    reason: str  # why the Advisor was or was not called
    advice_from_month: Month | None  # month whose advice applies (this month, or the one carried from)
    recommendations: list[Recommendation]  # accepted, ranked, from advice_from_month's run


class GoalProgress(BaseModel):
    goal: Goal
    as_of_month: Month  # the profile's clock
    latest_data_month: Month  # last month with transactions; replay cannot pass it
    next_month: Month | None  # what advance would check; None when it cannot
    advance_blocked: str | None  # why advance is not possible, if it is not
    current: GoalMonth | None  # latest check
    months: list[GoalMonth]  # oldest first


class GoalAdvance(BaseModel):
    run_id: str | None  # the goal_check run; advice arrives via this run (None: nothing was checked)
    progress: GoalProgress


class GoalDeleted(BaseModel):
    goal_id: int
    as_of_month: Month  # the profile's clock after the delete (restored)
    deleted_rows: dict[str, int]


# ---- eval ----

class PrecisionRecall(BaseModel):
    precision: float | None  # None when there is nothing to divide by (no detections / nothing planted)
    recall: float | None


class Confusion(BaseModel):
    true: Category
    predicted: Category
    count: int


class CategoryScore(BaseModel):
    precision: float
    recall: float
    f1: float
    support: int


class CategorizationMetrics(BaseModel):
    accuracy: float  # strict: primary label only
    macro_f1: float
    top_confusions: list[Confusion]
    lenient_accuracy: float | None = None  # any of the label's acceptable_labels counts
    n: int | None = None
    uncategorized: int | None = None
    cache_hit_rate: float | None = None
    per_category: dict[str, CategoryScore] | None = None
    per_persona: dict[str, float] | None = None  # strict accuracy per persona
    # from the result file's details block (prompt version, usage)
    prompt_version: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    cost_usd: float | None = None  # estimate at list prices, see eval/pricing.json


class PatternTypeScore(BaseModel):
    precision: float | None
    recall: float | None
    tp: int
    fp: int
    fn: int


class CategoryForecastScore(BaseModel):
    mape: float | None
    naive_mape: float | None
    naive_mean_mape: float | None
    n: int


class ForecastMetrics(BaseModel):
    mape: float  # next-month total spend, rolling origin
    naive_mape: float  # last month's spend
    naive_mean_mape: float | None = None  # 3-month trailing mean
    no_trend_mape: float | None = None  # engine without the trend-line adjustment
    mean_error_cents: int | None = None  # engine forecast minus actual, averaged (negative = under-forecast)
    baseline_mean_error_cents: dict[str, int] | None = None  # same, for no_trend, naive_last, naive_mean
    method_version: str | None = None
    coverage_80: float | None = None  # share of actual next-month balances inside p10..p90
    per_category: dict[str, CategoryForecastScore] | None = None
    origins: int | None = None  # forecasts scored (personas x origin months)
    horizon: int | None = None


class VerifierMetrics(BaseModel):
    first_draft_fail_rate: float  # lineages rejected on attempt 1 / lineages
    drafts: int
    rejected: int
    lineages: int | None = None
    accepted: int | None = None
    dropped: int | None = None  # 3 rejected attempts, or withdrawn by the Advisor after a rejection
    by_check: dict[str, int] | None = None  # first-draft failures per check


class EvalSummary(BaseModel):
    run_at: str
    git_sha: str
    dataset: str
    seed: int | None
    models: dict[str, str]
    categorization: CategorizationMetrics | None = None
    recurring: PrecisionRecall | None = None
    unused: PrecisionRecall | None = None
    anomaly: PrecisionRecall | None = None  # spike + duplicate + new_merchant pooled
    pattern_types: dict[str, PatternTypeScore] | None = None
    forecast: ForecastMetrics | None = None
    verifier: VerifierMetrics | None = None
    grounded_number_rate: float | None = None
