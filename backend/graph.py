"""LangGraph coordinator. Today: ingest -> categorize -> analyze -> forecast; advise and verify extend it later.

Agents are plain functions (backend/agents/*); nodes here are thin wrappers that read state, call
the agent, persist results and write agent_events. Persistence is our own runs + agent_events
tables; a LangGraph checkpointer is deferred to Oct 17 (advise <-> verify loop).
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from backend.agents import analyzer, categorizer, forecaster
from backend.agents.analyzer import Detected, money
from backend.agents.categorizer import LLM, BatchStat, MerchantCache, TxnIn
from backend.db import connect
from backend.ledger import count_after, load_txns
from backend.llm.client import complete, get_model


class FinPilotState(TypedDict, total=False):
    """Shared state for the whole pipeline. Later nodes fill the remaining fields."""
    run_id: str
    user_id: int
    batch_id: str
    as_of_month: str
    transactions: list[TxnIn]
    categorizations: list[categorizer.Label]
    patterns: list[Detected]
    forecast: int  # forecasts.id
    goal: Any
    scenario: Any
    drafts: list[Any]
    verifier_reports: list[Any]
    attempt: int
    final_recs: list[Any]
    audit_events: list[int]  # agent_events.seq written by this run


def emit(conn: sqlite3.Connection, run_id: str, node: str, *, status: str = "ok", summary: str = "",
         input_ref: str | None = None, output_ref: str | None = None, model: str | None = None,
         prompt_tokens: int | None = None, completion_tokens: int | None = None,
         duration_ms: int | None = None, attempt: int = 1) -> int:
    seq = conn.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM agent_events WHERE run_id = ?",
                       (run_id,)).fetchone()[0]
    conn.execute(
        "INSERT INTO agent_events (run_id, seq, node, attempt, status, summary, input_ref, output_ref, model, "
        "prompt_tokens, completion_tokens, duration_ms) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (run_id, seq, node, attempt, status, summary, input_ref, output_ref, model,
         prompt_tokens, completion_tokens, duration_ms),
    )
    conn.commit()
    return seq


def default_categorizer_llm(messages: list[dict], response_format: dict):
    return complete("categorizer", messages, temperature=0, response_format=response_format)


def build_graph(conn: sqlite3.Connection, categorizer_llm: LLM | None = None, use_cache: bool = True,
                categorizer_version: str = categorizer.PROMPT_VERSION):
    # late lookup of the default so tests can monkeypatch it
    llm = categorizer_llm or (lambda m, rf: default_categorizer_llm(m, rf))

    def ingest(state: FinPilotState) -> FinPilotState:
        t0 = time.monotonic()
        rows = conn.execute(
            "SELECT id, raw_description, amount_cents, posted_date FROM transactions "
            "WHERE batch_id = ? ORDER BY posted_date, id", (state["batch_id"],)).fetchall()
        txns = [TxnIn(r["id"], r["raw_description"], r["amount_cents"]) for r in rows]
        after = sum(r["posted_date"][:7] > state["as_of_month"] for r in rows)
        span = f"{rows[0]['posted_date']}..{rows[-1]['posted_date']}" if rows else "empty"
        seq = emit(conn, state["run_id"], "ingest",
                   summary=f"loaded {len(txns)} transactions ({span}); {after} after as_of {state['as_of_month']}",
                   input_ref=f"batch:{state['batch_id']}", duration_ms=int((time.monotonic() - t0) * 1000))
        return {"transactions": txns, "audit_events": [*state.get("audit_events", []), seq]}

    def categorize(state: FinPilotState) -> FinPilotState:
        t0 = time.monotonic()
        run_id, model = state["run_id"], get_model("categorizer")
        seqs = list(state.get("audit_events", []))

        def on_batch(s: BatchStat) -> None:
            note = "; ".join(dict.fromkeys(s.problems))[:300]
            summary = (f"{s.phase}: {s.items} items, {s.attempts} attempt(s), {s.retried_items} retried, "
                       f"{s.failed_items} failed"
                       + (f", echo_mismatch={s.echo_rejected}/{s.echo_checked}" if s.echo_checked else "")
                       + (f" | {note}" if note else "")
                       + (" | MESH FALLBACK MODEL" if s.fallback_triggered else ""))
            seqs.append(emit(conn, run_id, "categorize.batch", status="error" if s.failed_items else "ok",
                             summary=summary, model=s.served_model, prompt_tokens=s.prompt_tokens,
                             completion_tokens=s.completion_tokens, duration_ms=s.duration_ms,
                             attempt=s.attempts))

        cache = MerchantCache(conn) if use_cache else None
        res = categorizer.categorize(state["transactions"], cache, llm, model=model,
                                     version=categorizer_version, on_batch=on_batch)
        conn.executemany(
            "INSERT INTO categorizations (txn_id, run_id, category, confidence, rationale, model, prompt_version, "
            "from_cache) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [(lb.txn_id, run_id, lb.category, lb.confidence, lb.rationale, lb.model, lb.prompt_version,
              int(lb.from_cache)) for lb in res.labels],
        )
        conn.commit()
        n = len(state["transactions"])
        seqs.append(emit(
            conn, run_id, "categorize", status="error" if res.failed_txn_ids else "ok",
            summary=(f"{categorizer_version}: {len(res.labels)}/{n} categorized ({res.llm_items} by LLM, "
                     f"{res.cache_hits} from cache, {len(res.batches)} LLM batches); "
                     f"{len(res.failed_txn_ids)} left uncategorized after {categorizer.MAX_ATTEMPTS} attempts"
                     + (f"; confirm_agreed={res.confirmed_keys}, confirm_disagreed={res.disagreed_keys}"
                        if categorizer.VERSIONS[categorizer_version].confirm_before_cache and use_cache else "")),
            output_ref=f"run:{run_id}/categorizations", model=model,
            prompt_tokens=sum(b.prompt_tokens for b in res.batches),
            completion_tokens=sum(b.completion_tokens for b in res.batches),
            duration_ms=int((time.monotonic() - t0) * 1000)))
        return {"categorizations": res.labels, "audit_events": seqs}

    def analyze(state: FinPilotState) -> FinPilotState:
        t0 = time.monotonic()
        run_id, uid, as_of = state["run_id"], state["user_id"], state["as_of_month"]
        res = analyzer.analyze(load_txns(conn, uid, as_of), as_of)
        save_patterns(conn, run_id, uid, res.patterns)
        c = res.counts
        seq = emit(conn, run_id, "analyze",
                   summary=(f"as_of {as_of}: {res.n_txns} transactions read ({count_after(conn, uid, as_of)} after "
                            f"as_of not read, {res.n_uncategorized} uncategorized left out of trends); "
                            f"{c.get('recurring', 0)} recurring ({c['price_change']} price change, "
                            f"{c['possibly_unused']} possibly unused), {c.get('spike', 0)} spike, "
                            f"{c.get('duplicate', 0)} duplicate, {c.get('new_merchant', 0)} new merchant, "
                            f"{c.get('trend', 0)} trend"),
                   input_ref=f"user:{uid}/transactions?as_of={as_of}", output_ref=f"run:{run_id}/patterns",
                   duration_ms=int((time.monotonic() - t0) * 1000))
        return {"patterns": res.patterns, "audit_events": [*state.get("audit_events", []), seq]}

    def forecast(state: FinPilotState) -> FinPilotState:
        t0 = time.monotonic()
        run_id, uid, as_of = state["run_id"], state["user_id"], state["as_of_month"]
        opening = conn.execute("SELECT opening_balance_cents FROM users WHERE id = ?", (uid,)).fetchone()[0]
        fc = forecaster.forecast(load_txns(conn, uid, as_of), state["patterns"], opening, as_of)
        cur = conn.execute(
            "INSERT INTO forecasts (run_id, user_id, as_of_month, horizon_months, method, params, output) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (run_id, uid, as_of, fc.horizon_months, forecaster.METHOD, json.dumps(fc.params()), json.dumps(fc.points)))
        conn.commit()
        flows, nxt = fc.inputs.flows, fc.points[0]
        per_kind = {k: -sum(f.means[0] for f in flows if f.kind == k) for k in ("recurring", "variable", "one_off")}
        seq = emit(conn, run_id, "forecast",
                   summary=(f"{fc.version}, as_of {as_of}, horizon {fc.horizon_months}: start balance "
                            f"{money(fc.inputs.start_balance_cents)}; next month recurring {money(per_kind['recurring'])}, "
                            f"variable {money(per_kind['variable'])}, one-off allowance {money(per_kind['one_off'])}, "
                            f"income {money(nxt['income_cents'])}; "
                            f"{nxt['month']} balance p50 {money(nxt['balance_p50'])} "
                            f"(p10 {money(nxt['balance_p10'])}, p90 {money(nxt['balance_p90'])}); "
                            f"trend-adjusted: {', '.join(fc.trend_adjusted) or 'none'}"),
                   input_ref=f"run:{run_id}/patterns", output_ref=f"forecast:{cur.lastrowid}",
                   duration_ms=int((time.monotonic() - t0) * 1000))
        return {"forecast": cur.lastrowid, "audit_events": [*state.get("audit_events", []), seq]}

    g = StateGraph(FinPilotState)
    g.add_node("ingest", ingest)
    g.add_node("categorize", categorize)
    g.add_node("analyze", analyze)
    g.add_node("forecast", forecast)
    g.add_edge(START, "ingest")
    g.add_edge("ingest", "categorize")
    g.add_edge("categorize", "analyze")
    g.add_edge("analyze", "forecast")
    g.add_edge("forecast", END)
    return g.compile()


def save_patterns(conn: sqlite3.Connection, run_id: str, user_id: int, patterns: list[Detected]) -> None:
    """Append-only: one patterns row per detection plus its evidence rows; sets each Detected.id."""
    for p in patterns:
        cur = conn.execute("INSERT INTO patterns (run_id, user_id, type, title, params) VALUES (?, ?, ?, ?, ?)",
                           (run_id, user_id, p.type, p.title, json.dumps(p.params)))
        p.id = cur.lastrowid
        conn.executemany("INSERT INTO pattern_evidence (pattern_id, txn_id) VALUES (?, ?)",
                         [(p.id, t) for t in dict.fromkeys(p.evidence)])
    conn.commit()


# ---- runs ----

def create_run(conn: sqlite3.Connection, batch_id: str) -> str:
    batch = conn.execute(
        "SELECT b.user_id, u.as_of_month FROM batches b JOIN users u ON u.id = b.user_id WHERE b.id = ?",
        (batch_id,)).fetchone()
    if batch is None:
        raise LookupError(f"batch {batch_id} not found")
    run_id = uuid.uuid4().hex
    conn.execute("INSERT INTO runs (id, user_id, batch_id, kind, status, as_of_month) VALUES (?, ?, ?, 'main', 'pending', ?)",
                 (run_id, batch["user_id"], batch_id, batch["as_of_month"]))
    conn.commit()
    return run_id


def execute_run(run_id: str, *, db_path: str | None = None, categorizer_llm: LLM | None = None,
                use_cache: bool = True, categorizer_version: str = categorizer.PROMPT_VERSION) -> None:
    """Run the graph for a pending run. Called as a background task; opens its own connection."""
    conn = connect(db_path)
    try:
        run = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        conn.execute("UPDATE runs SET status = 'running' WHERE id = ?", (run_id,))
        conn.commit()
        try:
            build_graph(conn, categorizer_llm, use_cache, categorizer_version).invoke({
                "run_id": run_id, "user_id": run["user_id"], "batch_id": run["batch_id"],
                "as_of_month": run["as_of_month"], "audit_events": []})
            conn.execute("UPDATE runs SET status = 'done', finished_at = datetime('now') WHERE id = ?", (run_id,))
        except Exception as e:
            conn.rollback()
            emit(conn, run_id, "coordinator", status="error", summary=f"run failed: {type(e).__name__}: {e}"[:500])
            conn.execute("UPDATE runs SET status = 'failed', finished_at = datetime('now'), error = ? WHERE id = ?",
                         (f"{type(e).__name__}: {e}"[:500], run_id))
        conn.commit()
    finally:
        conn.close()
