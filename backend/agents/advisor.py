"""Advisor agent: one LLM with read-only tools over this run's patterns and forecast.

It drafts specific recommendations (cancel X, review X's price rise, reduce category Y), each with a trigger
pattern and structured claims. The tools hand it every figure it may use, already computed from the stored
pattern params and forecast flows, labelled with the claim fields to copy. The Verifier then recomputes each
claim independently from the raw transactions and the forecast engine (backend/agents/verifier.py).

The LLM is injected (an `LLM` callable) so the loop can be tested with scripted drafts.
"""
from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from typing import get_args

from backend.agents.forecaster import MAX_HORIZON, project
from backend.agents.verifier import ACTIONS, Context, PatternRow, acted_inputs
from backend.ledger import add_months
from backend.llm.client import Completion
from backend.models import ClaimKind

# adv-v1: Day 4 baseline. adv-v2 (made after seeing the adv-v1 eval): number-free tool text, rounding rule stated
# exactly as the Verifier checks it, and per-number redraft feedback (nearest claim or "remove the phrase").
# adv-v3: wording accuracy only: reduce_spending's balance gain is described as the rise stopping, not as cutting
# the excess (the delta holds the trend flow flat; it never cuts spending).
VERSIONS = ("adv-v1", "adv-v2", "adv-v3")
PROMPT_VERSION = "adv-v3"
MAX_RECS = 5
MAX_TOOL_ROUNDS = 8
DELTA_MONTHS = (3, 6, 12)  # months ahead for the projected_balance_delta figures the tools offer

LLM = Callable[..., Completion]  # (messages, **kwargs) -> Completion

SYSTEM_V1 = """You are FinPilot's Advisor. You write specific, actionable money recommendations for one user from the
patterns and forecast that the analysis tools return. A separate Verifier recomputes every number from the
user's transactions and rejects any draft that does not check out.

Use the tools: list_patterns first, then get_pattern for each candidate, get_forecast if you cite balances.

Allowed actions (nothing else):
- cancel: a recurring membership the tools flag possibly_unused=true (active, fixed price). Never cancel a
  membership that is in use or a charge that is not flagged.
- review_price_increase: a recurring charge whose price went up (price_changed=true, new price higher).
- reduce_spending: a spending category with a rising trend whose latest month is above its recent level
  (excess_spend > 0).

Each recommendation:
- target: the merchant or category, as the tools name it. title and body must name it too. Be specific: say
  what to do about which merchant and what it is worth. Never generic advice like "save more".
- trigger_type "pattern", trigger_id the pattern id (as a string).
- claims: copy figures exactly from the tools' `figures` (kind, value, unit, source_ref, month, period). Never
  compute a new number.
  Required: cancel -> recurring_cost and projected_balance_delta; reduce_spending -> excess_spend and
  projected_balance_delta; review_price_increase -> price_change.
- Every money amount, percentage, month and "N days"/"N months" you write in title or body must be one of
  your claims' values (you may round dollars, e.g. $119.88 as $120). Write months as YYYY-MM, never full
  dates. Write no other numbers.

Return at most {max_recs} recommendations, the most valuable first, at most one per pattern. Return an empty
list if nothing qualifies."""

V1_ROUNDING = "(you may round dollars, e.g. $119.88 as $120)"
V2_ROUNDING = ("(write an amount either exactly to the cent as the figure gives it, e.g. $121.80, or rounded to "
               "the nearest whole dollar, e.g. $122; no other rounding: not $120, not \"about $100\")")
assert V1_ROUNDING in SYSTEM_V1
SYSTEM_V2 = SYSTEM_V1.replace(V1_ROUNDING, V2_ROUNDING).replace(
    "Write no other numbers.",
    "Write no other numbers: the tools describe each figure in words, and the only numbers you may write are\n"
    "  the values of figures you claimed.")
V2_REDUCE = """- reduce_spending: a spending category with a rising trend whose latest month is above its recent level
  (excess_spend > 0)."""
assert V2_REDUCE in SYSTEM_V2
SYSTEM_V3 = SYSTEM_V2.replace(V2_REDUCE, V2_REDUCE + """
  Describe its projected_balance_delta as what stopping the rise is worth (the category stops rising and holds
  at next month's projected level), never as savings from cutting spending or the excess; excess_spend is how
  far the latest month is above the recent average, not a monthly saving.""")
SYSTEMS = {"adv-v1": SYSTEM_V1, "adv-v2": SYSTEM_V2, "adv-v3": SYSTEM_V3}

REDRAFT = """The Verifier rejected this draft (lineage {lineage}, attempt {attempt}):
{draft}

Failed checks:
{feedback}

Fix the draft using the tools: keep the same trigger pattern, correct or remove the failing claims and the
numbers in the text. Return exactly one recommendation, or an empty list if no allowed action fits this
pattern."""

_CLAIM_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["kind", "value", "unit", "source_ref", "month", "period"],
    "properties": {
        "kind": {"type": "string", "enum": list(get_args(ClaimKind))},
        "value": {"type": "integer"},
        "unit": {"type": "string", "enum": ["cents", "days", "percent"]},
        "source_ref": {"type": "string"},
        "month": {"type": ["string", "null"]},
        "period": {"type": ["string", "null"], "enum": ["month", "year", None]},
    },
}
RESPONSE_FORMAT = {"type": "json_schema", "json_schema": {"name": "recommendations", "strict": True, "schema": {
    "type": "object", "additionalProperties": False, "required": ["recommendations"],
    "properties": {"recommendations": {"type": "array", "items": {
        "type": "object", "additionalProperties": False,
        "required": ["action", "target", "title", "body", "trigger_type", "trigger_id", "claims"],
        "properties": {
            "action": {"type": "string", "enum": list(ACTIONS)},
            "target": {"type": "string"}, "title": {"type": "string"}, "body": {"type": "string"},
            "trigger_type": {"type": "string", "enum": ["pattern"]},
            "trigger_id": {"type": "string"},
            "claims": {"type": "array", "items": _CLAIM_SCHEMA},
        }}}}}}}


def _fn(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {"type": "function", "function": {"name": name, "description": description, "strict": True,
            "parameters": {"type": "object", "properties": properties, "required": required,
                           "additionalProperties": False}}}


def _tools(get_pattern_description: str) -> list[dict]:
    return [
        _fn("list_patterns", "Recurring charges and spending trends found in the user's transactions, with flags.",
            {"type": {"type": ["string", "null"], "enum": ["recurring", "trend", None]}}, ["type"]),
        _fn("get_pattern", get_pattern_description, {"pattern_id": {"type": "integer"}}, ["pattern_id"]),
        _fn("get_forecast", "This run's month-by-month balance forecast (p50 and low-end p10) as citable figures.",
            {}, []),
    ]


TOOLS_BY_VERSION = {
    "adv-v1": _tools("One pattern's details and the figures you may cite about it."),
    "adv-v2": _tools("One pattern's details and the figures you may cite about it. Its text describes the pattern "
                     "in words only; every number you write must be the value of one of these figures."),
}
TOOLS_BY_VERSION["adv-v3"] = TOOLS_BY_VERSION["adv-v2"]
TOOLS = TOOLS_BY_VERSION[PROMPT_VERSION]

# what each figure means; adv-v1 wording named counts ("3 months") that no claim backs
MEANINGS = {
    "adv-v1": {"excess": "latest month above the average of the 3 months before it",
               "percent": "latest month vs the 3 months before it",
               "delta": "balance gain by {m} if you {action}"},
    "adv-v2": {"excess": "how far the latest month is above your recent average",
               "percent": "latest month vs your recent average, in percent",
               "delta": "balance gain by this figure's month if you {action}"},
}
MEANINGS["adv-v3"] = MEANINGS["adv-v2"] | {
    "delta_reduce": "balance gain by this figure's month if this category stops rising and stays at next month's "
                    "projected level (not if you cut the excess)"}


# ---- tools (read-only, over stored params and forecast flows) ----

def _fig(kind: str, value: int, unit: str, ref: str, month: str | None = None, period: str | None = None,
         **note) -> dict:
    return {"kind": kind, "value": value, "unit": unit, "source_ref": ref, "month": month, "period": period} | note


def _summary(p: PatternRow, version: str = PROMPT_VERSION) -> dict:
    """adv-v2 replaces the Analyzer's title (full of amounts and dates no claim backs) with a number-free label."""
    pr = p.params
    if version == "adv-v1":
        head = {"id": p.id, "type": p.type, "title": p.title}
    elif p.type == "recurring":
        head = {"id": p.id, "type": "recurring", "label": f"recurring charge at {pr['merchant']}"}
    else:
        head = {"id": p.id, "type": "trend", "label": f"{pr['category'].replace('_', ' ')} spending trend"}
    if p.type == "recurring":
        return head | {"merchant": pr["merchant"],
                "category": pr["category"], "active": pr["active"], "possibly_unused": pr["possibly_unused"],
                "price_changed": pr["price_changed"], "amount_varies": pr["amount_varies"]}
    return head | {"category": pr["category"],
                   "direction": "rising" if pr["slope_cents_per_month"] > 0 else "falling"}


def _deltas(ctx: Context, p: PatternRow, action: str, version: str) -> list[dict]:
    if ctx.inputs is None:
        return []
    acted = acted_inputs(ctx.inputs, str(p.id), action)
    if acted is None:
        return []
    base, after = ctx.base_points(), project(acted, MAX_HORIZON)
    out = []
    for k in DELTA_MONTHS:
        m = add_months(ctx.as_of_month, k)
        i = next(j for j, pt in enumerate(base) if pt["month"] == m)
        out.append(_fig("projected_balance_delta", after[i]["balance_p50"] - base[i]["balance_p50"], "cents",
                        f"forecast:{ctx.forecast_id}", m,
                        meaning=MEANINGS[version].get("delta_reduce" if action == "reduce_spending" else "delta",
                                                      MEANINGS[version]["delta"]).format(
                            m=m, action=action.replace("_", " "))))
    return out


def get_pattern(ctx: Context, pattern_id: int, version: str = PROMPT_VERSION) -> dict:
    p = ctx.patterns.get(pattern_id)
    if p is None or p.type == "anomaly":
        return {"error": f"no recurring or trend pattern {pattern_id} in this run"}
    pr, ref = p.params, f"pattern:{p.id}"
    figs: list[dict] = []
    if p.type == "recurring":
        cost = -pr["amount_cents"]
        figs += [_fig("recurring_cost", cost, "cents", ref, period="month", meaning="current price per month"),
                 _fig("recurring_cost", 12 * cost, "cents", ref, period="year", meaning="per year")]
        if pr["price_changed"]:
            prev = -pr["prev_amount_cents"]
            figs += [_fig("prev_recurring_cost", prev, "cents", ref, meaning="price per month before the change"),
                     _fig("price_change", cost - prev, "cents", ref, period="month", meaning="change per month"),
                     _fig("price_change", 12 * (cost - prev), "cents", ref, period="year", meaning="change per year")]
        if pr["days_since_parent_spend"] is not None:
            figs.append(_fig("days_since_parent_spend", pr["days_since_parent_spend"], "days", ref,
                             meaning=f"days since any other {pr['merchant_root']} spend"))
        if pr["possibly_unused"] and pr["active"]:
            figs += _deltas(ctx, p, "cancel", version)
    else:
        latest, prev3 = pr["latest_month_cents"], pr["prev_3mo_mean_cents"]
        figs += [_fig("category_spend", latest, "cents", ref, ctx.as_of_month, meaning="spend in the latest month"),
                 _fig("excess_spend", latest - prev3, "cents", ref, period="month",
                      meaning=MEANINGS[version]["excess"]),
                 _fig("excess_spend", 12 * (latest - prev3), "cents", ref, period="year", meaning="same, per year")]
        if prev3:
            figs.append(_fig("percent_change", round(100 * (latest - prev3) / prev3), "percent", ref,
                             meaning=MEANINGS[version]["percent"]))
        if pr["slope_cents_per_month"] > 0:
            figs += _deltas(ctx, p, "reduce_spending", version)
    dates = {"first_seen": pr.get("first_seen"), "last_seen": pr.get("last_seen")} if version == "adv-v1" else {}
    return {**_summary(p, version), **dates, "figures": figs}


def list_patterns(ctx: Context, type: str | None = None, version: str = PROMPT_VERSION) -> dict:
    return {"as_of_month": ctx.as_of_month, "patterns": [
        _summary(p, version) for p in ctx.patterns.values() if p.type != "anomaly" and (type is None or p.type == type)]}


def get_forecast(ctx: Context) -> dict:
    if ctx.forecast_id is None:
        return {"error": "no forecast in this run"}
    ref = f"forecast:{ctx.forecast_id}"
    return {"forecast_id": ctx.forecast_id, "figures": [
        f for p in ctx.base_points() for f in (
            _fig("forecast_balance", p["balance_p50"], "cents", ref, p["month"], meaning="expected balance"),
            _fig("forecast_balance_p10", p["balance_p10"], "cents", ref, p["month"], meaning="low-end balance"))]}


def call_tool(ctx: Context, name: str, args: dict, version: str = PROMPT_VERSION) -> dict:
    if name == "list_patterns":
        return list_patterns(ctx, args.get("type"), version)
    if name == "get_pattern":
        return get_pattern(ctx, int(args["pattern_id"]), version)
    if name == "get_forecast":
        return get_forecast(ctx)
    return {"error": f"unknown tool {name}"}


# ---- session ----

class AdvisorError(RuntimeError):
    pass


@dataclass
class Session:
    recs: list[dict]
    model: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    tool_calls: list[str] = field(default_factory=list)
    duration_ms: int = 0


def run_session(ctx: Context, llm: LLM, user_msg: str,
                on_tool: Callable[[str, dict, dict], None] | None = None, version: str = PROMPT_VERSION) -> Session:
    if version not in VERSIONS:
        raise ValueError(f"unknown advisor prompt version {version}")
    t0 = time.monotonic()
    messages = [{"role": "system", "content": SYSTEMS[version].replace("{max_recs}", str(MAX_RECS))},
                {"role": "user", "content": user_msg}]
    s = Session([])
    for _ in range(MAX_TOOL_ROUNDS):
        c = llm(messages, tools=TOOLS_BY_VERSION[version], response_format=RESPONSE_FORMAT)
        s.model, s.prompt_tokens, s.completion_tokens = (c.model, s.prompt_tokens + c.prompt_tokens,
                                                         s.completion_tokens + c.completion_tokens)
        if c.tool_calls:
            messages.append({"role": "assistant", "content": c.text or None, "tool_calls": [
                {"id": t.id, "type": "function", "function": {"name": t.name, "arguments": t.arguments}}
                for t in c.tool_calls]})
            for t in c.tool_calls:
                try:
                    args = json.loads(t.arguments or "{}")
                    result = call_tool(ctx, t.name, args, version)
                except (ValueError, KeyError, TypeError) as e:
                    args, result = {}, {"error": f"bad arguments: {e}"}
                s.tool_calls.append(f"{t.name}({', '.join(f'{k}={v}' for k, v in args.items())})")
                if on_tool:
                    on_tool(t.name, args, result)
                messages.append({"role": "tool", "tool_call_id": t.id, "content": json.dumps(result)})
            continue
        try:
            s.recs = json.loads(c.text)["recommendations"]
        except (ValueError, KeyError, TypeError) as e:
            raise AdvisorError(f"advisor returned invalid JSON: {e}") from e
        s.duration_ms = int((time.monotonic() - t0) * 1000)
        return s
    raise AdvisorError(f"advisor made no final answer after {MAX_TOOL_ROUNDS} tool rounds")


def first_draft(ctx: Context, llm: LLM, on_tool=None, version: str = PROMPT_VERSION) -> Session:
    msg = (f"Simulated today: end of {ctx.as_of_month}. This run's forecast is forecast:{ctx.forecast_id}. "
           f"Draft the user's recommendations.")
    return run_session(ctx, llm, msg, on_tool, version)


def redraft(ctx: Context, llm: LLM, lineage: str, attempt: int, draft: dict, feedback: list[dict],
            on_tool=None, version: str = PROMPT_VERSION) -> Session:
    msg = REDRAFT.format(lineage=lineage, attempt=attempt, draft=json.dumps(draft, indent=1),
                         feedback="\n".join(f"- {json.dumps(f)}" for f in feedback))
    return run_session(ctx, llm, msg, on_tool, version)
