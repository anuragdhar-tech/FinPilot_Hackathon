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
    cadence: Literal["weekly", "monthly", "annual"]
    amount_cents: int
    first_seen: IsoDate
    last_seen: IsoDate
    price_changed: bool = False
    possibly_unused: bool = False


class AnomalyParams(BaseModel):
    txn_id: int
    kind: Literal["spike", "duplicate", "new_merchant"]
    amount_cents: int
    baseline_cents: int | None = None


class ShiftParams(BaseModel):
    category: Category
    month: Month
    prev_cents: int
    cur_cents: int
    pct_change: float


class TrendParams(BaseModel):
    category: Category
    months: int
    slope_cents_per_month: int
    start_cents: int
    end_cents: int


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


class ShiftPattern(_PatternBase):
    type: Literal["mom_shift"] = "mom_shift"
    params: ShiftParams


class TrendPattern(_PatternBase):
    type: Literal["trend"] = "trend"
    params: TrendParams


Pattern = Annotated[
    RecurringPattern | AnomalyPattern | ShiftPattern | TrendPattern, Field(discriminator="type")
]


class PatternList(BaseModel):
    items: list[Pattern]


# ---- forecast / what-if ----

class ForecastPoint(BaseModel):
    month: Month
    balance_p10: int
    balance_p50: int
    balance_p90: int
    net_flow_cents: int


class Forecast(BaseModel):
    id: int
    as_of_month: Month
    horizon_months: int = Field(gt=0)
    method: str
    points: list[ForecastPoint]


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

    @model_validator(mode="after")
    def exactly_one(self):
        if (self.text is None) == (self.scenario is None):
            raise ValueError("provide exactly one of 'text' or 'scenario'")
        return self


class NumberCheck(BaseModel):
    passed: bool
    issues: list[str] = []


class SimulateResponse(BaseModel):
    scenario: Scenario  # echoed back so the UI shows what the parser understood
    baseline: list[ForecastPoint]
    projected: list[ForecastPoint]
    end_delta_cents: int
    explanation: str
    number_check: NumberCheck


# ---- recommendations / audit ----

class Claim(BaseModel):
    kind: Literal["monthly_savings", "annual_savings", "goal_date", "amount", "percent"]
    value: int | str  # int cents, ISO date string, or int percent
    unit: Literal["cents", "date", "percent"]
    source_ref: Annotated[str, Field(pattern=r"^(pattern|forecast|goal|scenario|txn):\d+$")]


class Recommendation(BaseModel):
    id: int
    lineage_id: str
    title: str
    body: str
    claims: list[Claim]
    trigger_type: TriggerType
    trigger_id: str
    goal_id: int | None = None
    created_at: str


class RecommendationList(BaseModel):
    items: list[Recommendation]


class ClaimCheck(BaseModel):
    claim: Claim
    recomputed: int | str | None
    tolerance: int | float
    ok: bool
    note: str | None = None


class AuditAttempt(BaseModel):
    attempt: int
    rec_id: int
    title: str
    body: str
    verdict: Literal["pass", "reject"]
    reasons: list[str]
    checks: list[ClaimCheck]
    ts: str


class AuditInputs(BaseModel):
    pattern_ids: list[int] = []
    forecast_id: int | None = None
    as_of_month: Month


class AuditRecord(BaseModel):
    recommendation_id: int
    lineage_id: str
    trigger_type: TriggerType
    trigger_id: str
    inputs: AuditInputs
    model: str
    prompt_version: str
    attempts: list[AuditAttempt]


# ---- goals ----

class GoalCreate(BaseModel):
    name: str
    target_cents: int = Field(gt=0)
    target_date: IsoDate
    monthly_contribution_cents: int = Field(ge=0)


class Goal(GoalCreate):
    id: int
    start_month: Month
    created_at: str


class GoalSnapshot(BaseModel):
    month: Month
    planned_cents: int
    actual_cents: int
    projected_completion: IsoDate | None
    on_track: bool


class GoalProgress(BaseModel):
    goal: Goal
    as_of_month: Month
    snapshots: list[GoalSnapshot]
    projected_completion: IsoDate | None
    on_track: bool
    recommendations: list[Recommendation]


class GoalAdvance(BaseModel):
    run_id: str  # advice for the new month arrives via this run
    progress: GoalProgress


# ---- eval ----

class PrecisionRecall(BaseModel):
    precision: float
    recall: float


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


class ForecastMetrics(BaseModel):
    mape: float
    naive_mape: float


class VerifierMetrics(BaseModel):
    first_draft_fail_rate: float
    drafts: int
    rejected: int


class EvalSummary(BaseModel):
    run_at: str
    git_sha: str
    dataset: str
    seed: int | None
    models: dict[str, str]
    categorization: CategorizationMetrics | None = None
    recurring: PrecisionRecall | None = None
    unused: PrecisionRecall | None = None
    anomaly: PrecisionRecall | None = None
    forecast: ForecastMetrics | None = None
    verifier: VerifierMetrics | None = None
    grounded_number_rate: float | None = None
