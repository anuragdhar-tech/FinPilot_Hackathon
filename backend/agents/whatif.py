"""What-if: free text -> validated Scenario (one LLM call) -> extra Flows on the stored baseline forecast ->
baseline vs scenario trajectories, a code-computed verdict, and an LLM explanation whose numbers are checked
against the code facts (same number check as the Verifier); after two failed explanations a code template.

Verdict rule over the scenario trajectory (approved Oct 15):
  affordable      p10 balance >= $0 in every month
  tight           p50 >= $0 in every month, but p10 dips below
  not_affordable  p50 dips below $0
p10 assumes independent normal monthly errors; the backtest's 80% band covered only 54% of actuals, so p10 is
optimistic, which is why 'tight' exists.
"""
from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass

from pydantic import ValidationError

from backend.agents.analyzer import money
from backend.agents.forecaster import MAX_HORIZON, Flow, ForecastInputs, project
from backend.agents.verifier import ungrounded
from backend.ledger import add_months, months_between
from backend.llm.client import Completion
from backend.models import Scenario

PARSE_VERSION = "wif-parse-v1"
EXPLAIN_VERSION = "wif-explain-v1"
MAX_EXPLAIN_ATTEMPTS = 2

LLM = Callable[..., Completion]

PARSE_SYSTEM = """You turn a user's what-if question about their money into a structured scenario.
Simulated today: end of {as_of}. "next month" = {next}. A month name without a year means its next
occurrence after {as_of}. Allowed start months: {next} to {last}.
Items:
- one_off: a single purchase or windfall in one month (months = null).
- recurring_change: a monthly change from start_month on, for `months` months (null = open-ended).
amount_cents is signed integer cents: negative = money out (purchase, higher bill), positive = money in.
If the text is not a question about a purchase, income or spending change, set supported = false and say why."""

PARSE_FORMAT = {"type": "json_schema", "json_schema": {"name": "scenario", "strict": True, "schema": {
    "type": "object", "additionalProperties": False, "required": ["supported", "reason", "items"],
    "properties": {
        "supported": {"type": "boolean"}, "reason": {"type": "string"},
        "items": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["kind", "label", "amount_cents", "start_month", "months"],
            "properties": {"kind": {"type": "string", "enum": ["one_off", "recurring_change"]},
                           "label": {"type": "string"}, "amount_cents": {"type": "integer"},
                           "start_month": {"type": "string"}, "months": {"type": ["integer", "null"]}}}}}}}}

EXPLAIN_SYSTEM = """You explain a what-if result to the user in 2-3 short plain sentences.
Use only the facts given; the verdict is already decided by code, state it and why. Every money amount,
month and "N months" you write must be one of the facts' values exactly as given in `display`. Write months as
YYYY-MM. Write no other numbers, no percentages and no full dates."""


class ScenarioError(ValueError):
    pass


@dataclass(frozen=True)
class Fact:
    """A code-computed number the explanation may state (duck-types the Claim fields number checks read)."""
    name: str
    value: int
    unit: str  # cents | months
    month: str | None = None
    period: str | None = None

    def display(self) -> str:
        return money(self.value) if self.unit == "cents" else f"{self.value} months"


# ---- parse ----

def validate(scenario: Scenario, as_of: str) -> list[str]:
    first, last = add_months(as_of, 1), add_months(as_of, MAX_HORIZON)
    errs = []
    for i, it in enumerate(scenario.items, 1):
        if it.amount_cents == 0:
            errs.append(f"item {i}: amount_cents must not be 0")
        if not first <= it.start_month <= last:
            errs.append(f"item {i}: start_month {it.start_month} must be between {first} and {last}")
        if it.kind == "one_off" and it.months is not None:
            errs.append(f"item {i}: one_off must have months = null")
    return errs


@dataclass
class ParseResult:
    scenario: Scenario
    model: str
    prompt_tokens: int
    completion_tokens: int
    attempts: int


def parse(text: str, as_of: str, llm: LLM) -> ParseResult:
    """One LLM call; one retry with the validation errors; then ScenarioError."""
    system = PARSE_SYSTEM.format(as_of=as_of, next=add_months(as_of, 1), last=add_months(as_of, MAX_HORIZON))
    messages = [{"role": "system", "content": system}, {"role": "user", "content": text}]
    pt = ct = 0
    for attempt in (1, 2):
        c = llm(messages, response_format=PARSE_FORMAT)
        pt, ct = pt + c.prompt_tokens, ct + c.completion_tokens
        try:
            out = json.loads(c.text)
        except ValueError:
            errs = ["response was not JSON"]
        else:
            if not out.get("supported"):
                raise ScenarioError(f"not a what-if scenario: {out.get('reason', '')}")
            try:
                scenario = Scenario.model_validate({"items": out["items"]})
                errs = validate(scenario, as_of)
            except ValidationError as e:
                errs = [f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors()]
            if not errs:
                return ParseResult(scenario, c.model, pt, ct, attempt)
        messages += [{"role": "assistant", "content": c.text},
                     {"role": "user", "content": "Fix these problems and answer again:\n- " + "\n- ".join(errs)}]
    raise ScenarioError("could not parse a valid scenario: " + "; ".join(errs))


# ---- simulate ----

def scenario_flows(scenario: Scenario, as_of: str) -> list[Flow]:
    """Each item as a Flow over the full horizon; sd 0 (a known purchase adds no uncertainty)."""
    months = [add_months(as_of, k) for k in range(1, MAX_HORIZON + 1)]
    flows = []
    for i, it in enumerate(scenario.items, 1):
        start = months.index(it.start_month)
        if it.kind == "one_off":
            on = {start}
        else:
            end = len(months) if it.months is None else min(len(months), start + it.months)
            on = set(range(start, end))
        flows.append(Flow(it.label, "one_off" if it.kind == "one_off" else "variable", "scenario", f"scenario:{i}",
                          tuple(it.amount_cents if k in on else 0 for k in range(len(months))), 0))
    return flows


def with_scenario(base: ForecastInputs, scenario: Scenario) -> ForecastInputs:
    return ForecastInputs(base.as_of_month, base.start_balance_cents,
                          base.flows + tuple(scenario_flows(scenario, base.as_of_month)))


def horizon_for(scenario: Scenario, as_of: str, requested: int) -> int:
    latest = max(it.start_month for it in scenario.items)
    return min(MAX_HORIZON, max(requested, len(months_between(as_of, latest)) - 1))


def verdict(points: list[dict]) -> tuple[str, dict, dict]:
    lo50 = min(points, key=lambda p: (p["balance_p50"], p["month"]))
    lo10 = min(points, key=lambda p: (p["balance_p10"], p["month"]))
    v = ("affordable" if lo10["balance_p10"] >= 0 else "tight" if lo50["balance_p50"] >= 0 else "not_affordable")
    return v, {"month": lo50["month"], "cents": lo50["balance_p50"]}, {"month": lo10["month"], "cents": lo10["balance_p10"]}


@dataclass
class Simulation:
    baseline: list[dict]
    projected: list[dict]
    inputs: ForecastInputs
    verdict: str
    lowest_p50: dict
    lowest_p10: dict
    end_delta_cents: int


def simulate(base: ForecastInputs, scenario: Scenario, horizon: int) -> Simulation:
    inputs = with_scenario(base, scenario)
    baseline, projected = project(base, MAX_HORIZON)[:horizon], project(inputs, MAX_HORIZON)[:horizon]
    v, lo50, lo10 = verdict(projected)
    return Simulation(baseline, projected, inputs, v, lo50, lo10,
                      projected[-1]["balance_p50"] - baseline[-1]["balance_p50"])


# ---- explain ----

def facts(scenario: Scenario, sim: Simulation) -> list[Fact]:
    out = []
    for i, it in enumerate(scenario.items, 1):
        out.append(Fact(f"item {i} amount ({it.label}, {'out' if it.amount_cents < 0 else 'in'}, "
                        f"{'once' if it.kind == 'one_off' else 'per month'}) starting", it.amount_cents, "cents",
                        it.start_month))
        if it.months:
            out.append(Fact(f"item {i} duration", it.months, "months"))
    end = sim.projected[-1]["month"]
    out += [Fact("verdict floor", 0, "cents"),
            Fact("lowest expected (p50) balance", sim.lowest_p50["cents"], "cents", sim.lowest_p50["month"]),
            Fact("lowest low-end (p10) balance", sim.lowest_p10["cents"], "cents", sim.lowest_p10["month"]),
            Fact("expected balance at the end", sim.projected[-1]["balance_p50"], "cents", end),
            Fact("change vs no scenario at the end", sim.end_delta_cents, "cents", end)]
    return out


def template(scenario: Scenario, sim: Simulation) -> str:
    items = "; ".join(f"{it.label}: {money(it.amount_cents)} {'out' if it.amount_cents < 0 else 'in'}"
                      f"{'' if it.kind == 'one_off' else ' per month'} from {it.start_month}" for it in scenario.items)
    why = {"affordable": "even the low-end projection stays above $0",
           "tight": "the expected balance stays above $0 but the low-end projection dips below it",
           "not_affordable": "the expected balance falls below $0"}[sim.verdict]
    return (f"Scenario ({items}). Verdict: {sim.verdict.replace('_', ' ')}: {why}. Lowest expected balance "
            f"{money(sim.lowest_p50['cents'])} in {sim.lowest_p50['month']}; lowest low-end balance "
            f"{money(sim.lowest_p10['cents'])} in {sim.lowest_p10['month']}. The low-end band has been optimistic "
            f"in backtests.")


@dataclass
class Explanation:
    text: str
    model: str
    prompt_tokens: int
    completion_tokens: int


def explain(scenario: Scenario, sim: Simulation, fs: list[Fact], llm: LLM, issues: list[str] | None = None) -> Explanation:
    payload = {"verdict": sim.verdict, "rule": "affordable: low-end (p10) balance stays >= $0 every month; tight: "
               "expected (p50) stays >= $0 but low-end dips below; not_affordable: expected dips below $0",
               "facts": [{"name": f.name, "display": f.display(), "month": f.month} for f in fs]}
    messages = [{"role": "system", "content": EXPLAIN_SYSTEM}, {"role": "user", "content": json.dumps(payload)}]
    if issues:
        messages.append({"role": "user", "content": "Your previous explanation had numbers that are not in the "
                                                    "facts: " + "; ".join(issues) + ". Write it again."})
    c = llm(messages)
    return Explanation(c.text.strip(), c.model, c.prompt_tokens, c.completion_tokens)


def check(text: str, fs: list[Fact], as_of: str) -> list[str]:
    return [f"'{n.text}' matches no fact" for n in ungrounded(text, fs, as_of)]
