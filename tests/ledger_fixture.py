"""A small hand-built ledger in a file DB, categorized directly (no Categorizer), for Advisor/Verifier/what-if tests.

12 months 2025-10 .. 2026-09, as_of 2026-09:
- payroll 3,000/month
- UBER*ONE MEMBERSHIP 9.99/month; Uber trips only in the first 2 months -> possibly unused
- DOORDASH DASHPASS 9.99/month with DoorDash orders every month (used), orders rising -> food_delivery trend
- SPOTIFY 11.99 -> 12.99 from the 7th charge -> price change
"""
import json
from datetime import date

from backend.agents import advisor, verifier
from backend.db import connect, init_db
from backend.graph import create_run, execute_run
from backend.llm.client import Completion, ToolCall

AS_OF = "2026-09"


def _day(m: int, dom: int) -> str:
    y, mo = divmod(9 + m, 12)
    return date(2025 + y, mo + 1, dom).isoformat()


def build_ledger(path) -> int:
    conn = connect(path)
    init_db(conn)
    uid = conn.execute("INSERT INTO users (name, opening_balance_cents, opening_date, as_of_month) "
                       "VALUES ('t', 100000, '2025-10-01', ?)", (AS_OF,)).lastrowid
    conn.execute("INSERT INTO batches (id, user_id, filename, row_count) VALUES ('b1', ?, 't.csv', 0)", (uid,))
    conn.execute("INSERT INTO runs (id, user_id, batch_id, kind, status, as_of_month) "
                 "VALUES ('labels', ?, 'b1', 'main', 'done', ?)", (uid, AS_OF))
    rows = []
    for m in range(12):
        rows.append((_day(m, 1), "ACME PAYROLL", 300000, "income"))
        rows.append((_day(m, 7), "UBER *ONE MEMBERSHIP", -999, "transport"))
        rows.append((_day(m, 20), "DOORDASH DASHPASS", -999, "food_delivery"))
        rows.append((_day(m, 3), "SPOTIFY USA", -1199 if m < 6 else -1299, "entertainment"))
        for dom in (5, 12, 19, 26):
            rows.append((_day(m, dom), "DOORDASH*THAI PLACE", -(2000 + 300 * m), "food_delivery"))
        if m < 2:
            rows.append((_day(m, 15), "UBER *TRIP HELP.UBER.COM", -1850, "transport"))
    for i, (d, desc, cents, cat) in enumerate(rows):
        tid = conn.execute("INSERT INTO transactions (user_id, batch_id, external_id, posted_date, raw_description, "
                           "amount_cents) VALUES (?, 'b1', ?, ?, ?, ?)", (uid, f"x{i}", d, desc, cents)).lastrowid
        conn.execute("INSERT INTO categorizations (txn_id, run_id, category, confidence, model, prompt_version, "
                     "from_cache) VALUES (?, 'labels', ?, 0.9, 'fixture', 'fixture', 0)", (tid, cat))
    conn.commit()
    conn.close()
    return uid


def run_main(path, advisor_llm, advisor_version: str = advisor.PROMPT_VERSION) -> str:
    conn = connect(path)
    run_id = create_run(conn, "b1")
    conn.close()
    execute_run(run_id, db_path=str(path), advisor_llm=advisor_llm, entry="analyze", advisor_version=advisor_version)
    return run_id


def pattern_id(ctx: verifier.Context, merchant_word: str) -> int:
    return next(p.id for p in ctx.patterns.values()
                if p.type == "recurring" and merchant_word in p.params["merchant"])


def trend_id(ctx: verifier.Context) -> int:
    return next(p.id for p in ctx.patterns.values() if p.type == "trend")


def figure(ctx: verifier.Context, pid: int, kind: str, **match) -> dict:
    """A claim copied from the Advisor tool's figures (what a well-behaved LLM would write)."""
    figs = advisor.get_pattern(ctx, pid)["figures"]
    f = next(f for f in figs if f["kind"] == kind and all(f.get(k) == v for k, v in match.items()))
    return {k: f[k] for k in ("kind", "value", "unit", "source_ref", "month", "period")}


# ---- scripted Advisor LLM ----

def completion(text: str = "", tool_calls=()) -> Completion:
    return Completion(text, "fake-advisor", 100, 20, False, tuple(tool_calls))


def final(*recs: dict) -> Completion:
    return completion(json.dumps({"recommendations": list(recs)}))


class ScriptedAdvisor:
    """Each step gets (messages, ctx-lookup helper) and returns a Completion. Records every prompt."""

    def __init__(self, db_path, steps):
        self.db_path, self.steps, self.calls = db_path, list(steps), []

    def __call__(self, messages, **kwargs):
        self.calls.append(messages)
        assert kwargs.get("tools") and kwargs.get("response_format"), "advisor must be called with tools"
        conn = connect(self.db_path)
        run_id = conn.execute("SELECT id FROM runs WHERE id != 'labels' ORDER BY rowid DESC LIMIT 1").fetchone()[0]
        ctx = verifier.Context.load(conn, run_id)
        conn.close()
        return self.steps.pop(0)(messages, ctx)


def call_tool(name: str, **args):
    return lambda messages, ctx: completion(tool_calls=[ToolCall(f"call_{name}", name, json.dumps(args))])


def uber_rec(ctx: verifier.Context, *, cost: int | None = None, extra_text: str = "") -> dict:
    """Cancel Uber One with figures from the tools; cost overrides the claimed monthly price (a bad draft)."""
    pid = pattern_id(ctx, "UBER")
    c = figure(ctx, pid, "recurring_cost", period="month")
    m6 = verifier.add_months(ctx.as_of_month, 6)
    delta = figure(ctx, pid, "projected_balance_delta", month=m6)
    if cost is not None:
        c["value"] = cost
    return {"action": "cancel", "target": "Uber One", "trigger_type": "pattern", "trigger_id": str(pid),
            "title": f"Cancel Uber One (${c['value'] / 100:.2f}/month)",
            "body": f"You pay for Uber One but have not used Uber since early on. Cancelling leaves you "
                    f"${delta['value'] / 100:,.2f} more by {m6}.{extra_text}",
            "claims": [c, delta]}
