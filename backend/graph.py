"""LangGraph coordinator: ingest -> categorize -> analyze -> forecast -> advise -> verify -> persist, where
verify routes rejected drafts back to advise (at most MAX_ATTEMPTS drafts per recommendation lineage).

Goal check (one per replayed month): analyze -> forecast -> compare_to_plan -> advise <-> verify -> persist, where
compare_to_plan (code) stores the goal's status and ends the run without calling the Advisor when the status and
the actionable patterns are unchanged since the last advised month, or once the goal is final.

Agents are plain functions (backend/agents/*); nodes here are thin wrappers that read state, call
the agent, persist results and write agent_events. Persistence is our own tables (runs, agent_events,
recommendations, audit_trail); no LangGraph checkpointer: runs are short, need no pause/resume, and
every draft and verdict is already stored append-only.
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from backend.agents import advisor, analyzer, categorizer, forecaster, goal_tracker, verifier, whatif
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
    insufficient_data: bool  # under MIN_HISTORY_MONTHS complete months: no projection, advice skipped
    goal_decision: str  # goal_check: advise | carry | final (compare_to_plan)
    defer_advice: bool  # goal_check: stop after compare_to_plan; the caller continues from advise in the background
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


def default_advisor_llm(messages: list[dict], **kwargs):
    return complete("advisor", messages, temperature=0, **kwargs)


def build_graph(conn: sqlite3.Connection, categorizer_llm: LLM | None = None, use_cache: bool = True,
                categorizer_version: str = categorizer.PROMPT_VERSION, advisor_llm: advisor.LLM | None = None,
                entry: str = "ingest", advisor_version: str = advisor.PROMPT_VERSION, kind: str = "main"):
    """entry='analyze' starts from the stored categorizations (eval reuse; no Categorizer calls); entry='advise'
    continues a run whose forecast (and goal snapshot) are already stored. kind='goal_check' adds compare_to_plan."""
    # late lookup of the defaults so tests can monkeypatch them
    llm = categorizer_llm or (lambda m, rf: default_categorizer_llm(m, rf))
    adv_llm = advisor_llm or (lambda m, **kw: default_advisor_llm(m, **kw))

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
        txns = load_txns(conn, uid, as_of)
        fc = forecaster.forecast(txns, state["patterns"], opening, as_of)
        reason = forecaster.insufficient_reason(txns, as_of)
        params = fc.params() | ({"insufficient_data": True, "reason": reason} if reason else {})
        cur = conn.execute(
            "INSERT INTO forecasts (run_id, user_id, as_of_month, horizon_months, method, params, output) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (run_id, uid, as_of, fc.horizon_months, forecaster.METHOD, json.dumps(params),
             json.dumps([] if reason else fc.points)))
        conn.commit()
        if reason:
            seq = emit(conn, run_id, "forecast",
                       summary=f"{fc.version}, as_of {as_of}: insufficient data, no projection; advice skipped. {reason}",
                       input_ref=f"run:{run_id}/patterns", output_ref=f"forecast:{cur.lastrowid}",
                       duration_ms=int((time.monotonic() - t0) * 1000))
            return {"forecast": cur.lastrowid, "insufficient_data": True,
                    "audit_events": [*state.get("audit_events", []), seq]}
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
        return {"forecast": cur.lastrowid, "insufficient_data": False,
                "audit_events": [*state.get("audit_events", []), seq]}

    def advise(state: FinPilotState) -> FinPilotState:
        run_id, attempt = state["run_id"], state.get("attempt", 0) + 1
        ctx = verifier.Context.load(conn, run_id)
        seqs = list(state.get("audit_events", []))

        def on_tool(name: str, args: dict, result: dict) -> None:
            shown = ", ".join(f"{k}={v}" for k, v in args.items())
            n = len(result.get("patterns", result.get("figures", [])))
            seqs.append(emit(conn, run_id, "advise.tool", attempt=attempt,
                             status="error" if "error" in result else "ok",
                             summary=f"{name}({shown}) -> " + (result["error"] if "error" in result else f"{n} items"),
                             input_ref=f"run:{run_id}"))

        def log(sess: advisor.Session, summary: str) -> None:
            seqs.append(emit(conn, run_id, "advise", attempt=attempt, summary=summary, model=sess.model,
                             prompt_tokens=sess.prompt_tokens, completion_tokens=sess.completion_tokens,
                             duration_ms=sess.duration_ms, input_ref=f"run:{run_id}/patterns",
                             output_ref=f"forecast:{ctx.forecast_id}"))

        def draft(rec: dict, lineage: str, sess: advisor.Session, trigger: str | None = None) -> verifier.Draft:
            return verifier.Draft(lineage, attempt, rec.get("action", ""), rec.get("target", ""),
                                  rec.get("title", ""), rec.get("body", ""), rec.get("trigger_type", ""),
                                  str(rec.get("trigger_id", "")), rec.get("claims", []), sess.model,
                                  advisor.prompt_version(ctx, advisor_version), trigger)

        drafts: list[verifier.Draft] = []
        if attempt == 1:
            sess = advisor.first_draft(ctx, adv_llm, on_tool, advisor_version)
            seen, extra = set(), []
            for rec in sess.recs:
                key = (str(rec.get("trigger_id")), rec.get("action"))
                if key in seen or len(drafts) == advisor.MAX_RECS:
                    extra.append(key)
                    continue
                seen.add(key)
                drafts.append(draft(rec, uuid.uuid4().hex, sess))
            log(sess, f"{advisor.prompt_version(ctx, advisor_version)} first draft: {len(drafts)} recommendation(s) after "
                      f"{len(sess.tool_calls)} tool call(s)"
                      + (f"; ignored {len(extra)} repeated or over the cap of {advisor.MAX_RECS}" if extra else ""))
        else:
            for prev, feedback in state["verifier_reports"]:
                sess = advisor.redraft(ctx, adv_llm, prev.lineage_id, prev.attempt, prev.as_json(), feedback,
                                       on_tool, advisor_version)
                if not sess.recs:
                    log(sess, f"redraft of {prev.lineage_id[:8]} ({prev.action} {prev.target}): withdrawn by the "
                              f"Advisor, no allowed action fits")
                    continue
                drafts.append(draft(sess.recs[0], prev.lineage_id, sess, prev.lineage_trigger or prev.trigger_id))
                log(sess, f"redraft of {prev.lineage_id[:8]} ({prev.action} {prev.target}) after "
                          f"{len(feedback)} failed check(s), {len(sess.tool_calls)} tool call(s)"
                          + (f"; ignored {len(sess.recs) - 1} extra" if len(sess.recs) > 1 else ""))
        return {"drafts": drafts, "attempt": attempt, "audit_events": seqs}

    def verify(state: FinPilotState) -> FinPilotState:
        run_id = state["run_id"]
        ctx = verifier.Context.load(conn, run_id)
        seqs, pending = list(state.get("audit_events", [])), []
        for d in state.get("drafts", []):
            t0 = time.monotonic()
            rep = verifier.verify(d, ctx)
            status = ("accepted" if rep.verdict == "pass" else
                      "dropped" if d.attempt >= verifier.MAX_ATTEMPTS else "rejected")
            rec_id = save_draft(conn, run_id, d, rep, status, ctx)
            if status == "rejected":
                pending.append((d, rep.feedback(per_number=advisor_version != "adv-v1")))
            what = f"{d.action} {d.target!r} (lineage {d.lineage_id[:8]}, rec {rec_id})"
            summary = (f"{what}: pass, {len(rep.checks)} claim(s) recomputed, annual impact "
                       f"{analyzer.money(rep.impact_annual_cents)}" if rep.verdict == "pass" else
                       f"{what}: {status}; " + " | ".join(rep.reasons)[:400])
            seqs.append(emit(conn, run_id, "verify", attempt=d.attempt,
                             status="ok" if rep.verdict == "pass" else "rejected", summary=summary,
                             input_ref=f"rec:{rec_id}", output_ref=f"audit:{rec_id}",
                             duration_ms=int((time.monotonic() - t0) * 1000)))
        return {"drafts": [], "verifier_reports": pending, "audit_events": seqs}

    def persist(state: FinPilotState) -> FinPilotState:
        run_id = state["run_id"]
        ranked = ranked_recommendations(conn, run_id)
        counts = dict(conn.execute("SELECT status, COUNT(*) FROM recommendations WHERE run_id = ? GROUP BY status",
                                   (run_id,)).fetchall())
        lineages = conn.execute("SELECT COUNT(DISTINCT lineage_id) FROM recommendations WHERE run_id = ?",
                                (run_id,)).fetchone()[0]
        order = "; ".join(f"#{r['rank']} {r['action']} {r['target']} ({analyzer.money(r['impact_annual_cents'])}/yr)"
                          for r in ranked)
        seq = emit(conn, run_id, "persist",
                   summary=(f"{len(ranked)} of {lineages} recommendation(s) accepted; "
                            f"{counts.get('rejected', 0)} draft(s) rejected and revised, "
                            f"{lineages - len({r['lineage_id'] for r in ranked})} lineage(s) dropped or withdrawn"
                            + (f". Ranked: {order}" if order else "")),
                   output_ref=f"run:{run_id}/recommendations")
        return {"final_recs": [r["id"] for r in ranked], "audit_events": [*state.get("audit_events", []), seq]}

    def compare_to_plan(state: FinPilotState) -> FinPilotState:
        t0 = time.monotonic()
        run_id, uid, as_of = state["run_id"], state["user_id"], state["as_of_month"]
        if state.get("insufficient_data"):
            raise ValueError(f"goal check at {as_of}: not enough history for a forecast")
        g = conn.execute("SELECT g.* FROM goals g JOIN runs r ON r.goal_id = g.id WHERE r.id = ?", (run_id,)).fetchone()
        goal = goal_tracker.Goal(g["id"], g["name"], g["target_cents"], g["start_month"], g["target_month"])
        opening = conn.execute("SELECT opening_balance_cents FROM users WHERE id = ?", (uid,)).fetchone()[0]
        points = json.loads(conn.execute("SELECT output FROM forecasts WHERE id = ?", (state["forecast"],)).fetchone()[0])
        c = goal_tracker.check(goal, load_txns(conn, uid, as_of), opening, points, as_of)
        sig = goal_tracker.signature(c.status, [(p.type, p.params) for p in state["patterns"]])
        prev = conn.execute(
            "SELECT s.month, s.signature, s.advice_run_id FROM goal_snapshots s JOIN runs r ON r.id = s.run_id "
            "WHERE s.goal_id = ? AND s.decision = 'advise' AND r.status = 'done' ORDER BY s.month DESC LIMIT 1",
            (goal.id,)).fetchone()
        decision, reason = goal_tracker.decide(c, sig, json.loads(prev["signature"]) if prev else None,
                                               prev["month"] if prev else None)
        advice_run = run_id if decision == "advise" else prev["advice_run_id"] if decision == "carry" else None
        conn.execute(
            "INSERT INTO goal_snapshots (goal_id, run_id, month, status, saved_cents, planned_cents, projected_p50_cents, "
            "projected_p10_cents, gap_cents, projected_completion, forecast_id, signature, decision, reason, advice_run_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (goal.id, run_id, as_of, c.status, c.saved_cents, c.planned_cents, c.projected_p50_cents,
             c.projected_p10_cents, c.gap_cents, c.projected_completion, state["forecast"], json.dumps(sig),
             decision, reason, advice_run))
        conn.commit()
        gap = f"short {money(c.gap_cents)}" if c.gap_cents > 0 else f"ahead {money(c.gap_cents)}"
        figures = (f"saved {money(c.saved_cents)} of {money(goal.target_cents)} by {goal.target_month}, {gap}"
                   if c.final else
                   f"saved {money(c.saved_cents)} (plan {money(c.planned_cents)}); projected by {goal.target_month}: "
                   f"p50 {money(c.projected_p50_cents)}, p10 {money(c.projected_p10_cents)} of "
                   f"{money(goal.target_cents)}, {gap}")
        seq = emit(conn, run_id, "compare_to_plan",
                   summary=f"goal {goal.id} at {as_of}: {c.status}; {figures}. {decision}: {reason}",
                   input_ref=f"forecast:{state['forecast']}", output_ref=f"goal:{goal.id}/snapshot/{as_of}",
                   duration_ms=int((time.monotonic() - t0) * 1000))
        return {"goal_decision": decision, "audit_events": [*state.get("audit_events", []), seq]}

    def route(state: FinPilotState) -> str:
        return "advise" if state.get("verifier_reports") else "persist"

    def after_forecast(state: FinPilotState) -> str:
        return "persist" if state.get("insufficient_data") else "advise"

    def after_compare(state: FinPilotState) -> str:
        return "advise" if state["goal_decision"] == "advise" and not state.get("defer_advice") else END

    g = StateGraph(FinPilotState)
    g.add_node("ingest", ingest)
    g.add_node("categorize", categorize)
    g.add_node("analyze", analyze)
    g.add_node("forecast", forecast)
    g.add_node("advise", advise)
    g.add_node("verify", verify)
    g.add_node("persist", persist)
    g.add_edge(START, entry)
    g.add_edge("ingest", "categorize")
    g.add_edge("categorize", "analyze")
    g.add_edge("analyze", "forecast")
    if kind == "goal_check":
        g.add_node("compare_to_plan", compare_to_plan)
        g.add_edge("forecast", "compare_to_plan")
        g.add_conditional_edges("compare_to_plan", after_compare, {"advise": "advise", END: END})
    else:
        g.add_conditional_edges("forecast", after_forecast, {"advise": "advise", "persist": "persist"})
    g.add_edge("advise", "verify")
    g.add_conditional_edges("verify", route, {"advise": "advise", "persist": "persist"})
    g.add_edge("persist", END)
    return g.compile()


def save_draft(conn: sqlite3.Connection, run_id: str, d: verifier.Draft, rep: verifier.Report, status: str,
               ctx: verifier.Context) -> int:
    """Append-only: one recommendations row per draft (with its final status) and its audit_trail row."""
    cur = conn.execute(
        "INSERT INTO recommendations (run_id, lineage_id, attempt, status, trigger_type, trigger_id, goal_id, action, "
        "target, impact_annual_cents, title, body, claims, model, prompt_version) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (run_id, d.lineage_id, d.attempt, status, d.trigger_type if d.trigger_type in TRIGGER_TYPES else "pattern",
         d.trigger_id, ctx.goal.id if ctx.goal else None, d.action, d.target, rep.impact_annual_cents, d.title, d.body,
         json.dumps([c.model_dump() for c in rep.claims]), d.model, d.prompt_version))
    rec_id = cur.lastrowid
    cited = sorted({int(c.source_ref.split(":")[1]) for c in rep.claims if c.source_ref.startswith("pattern:")}
                   | ({int(d.trigger_id)} if d.trigger_id.isdigit() else set()))
    conn.execute(
        "INSERT INTO audit_trail (rec_id, lineage_id, run_id, trigger_type, trigger_id, attempt, verifier_verdict, "
        "reasons, checks, rule_checks, inputs) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (rec_id, d.lineage_id, run_id, d.trigger_type, d.trigger_id, d.attempt, rep.verdict, json.dumps(rep.reasons),
         json.dumps(rep.checks), json.dumps(rep.rule_checks),
         json.dumps({"pattern_ids": cited, "forecast_id": ctx.forecast_id, "as_of_month": ctx.as_of_month}
                   | ({"goal_id": ctx.goal.id} if ctx.goal else {}))))
    conn.commit()
    return rec_id


TRIGGER_TYPES = ("pattern", "forecast", "goal", "scenario")


def ranked_recommendations(conn: sqlite3.Connection, run_id: str) -> list[dict]:
    """Accepted recommendations of a run, largest verifier-recomputed annual impact first; one per (trigger, action)."""
    rows = conn.execute(
        "SELECT r.*, (SELECT COUNT(*) FROM recommendations x WHERE x.lineage_id = r.lineage_id) AS attempts "
        "FROM recommendations r WHERE r.run_id = ? AND r.status = 'accepted' "
        "ORDER BY r.impact_annual_cents DESC, r.id", (run_id,)).fetchall()
    out, seen = [], set()
    for r in rows:
        key = (r["trigger_type"], r["trigger_id"], r["action"])
        if key in seen:
            continue
        seen.add(key)
        out.append({**dict(r), "claims": json.loads(r["claims"]), "rank": len(out) + 1})
    return out


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
                use_cache: bool = True, categorizer_version: str = categorizer.PROMPT_VERSION,
                advisor_llm: advisor.LLM | None = None, entry: str = "ingest",
                advisor_version: str = advisor.PROMPT_VERSION) -> None:
    """Run the graph for a pending run (or continue a goal_check run from entry='advise'). Called as a background
    task; opens its own connection."""
    conn = connect(db_path)
    try:
        run = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        conn.execute("UPDATE runs SET status = 'running' WHERE id = ?", (run_id,))
        conn.commit()
        try:
            build_graph(conn, categorizer_llm, use_cache, categorizer_version, advisor_llm, entry,
                        advisor_version, run["kind"]).invoke({
                "run_id": run_id, "user_id": run["user_id"], "batch_id": run["batch_id"],
                "as_of_month": run["as_of_month"], "audit_events": []})
            conn.execute("UPDATE runs SET status = 'done', finished_at = datetime('now') WHERE id = ?", (run_id,))
        except Exception as e:
            conn.rollback()
            # status first, so a failing event write cannot leave the run in 'running'
            conn.execute("UPDATE runs SET status = 'failed', finished_at = datetime('now'), error = ? WHERE id = ?",
                         (f"{type(e).__name__}: {e}"[:500], run_id))
            conn.commit()
            emit(conn, run_id, "coordinator", status="error", summary=f"run failed: {type(e).__name__}: {e}"[:500])
        conn.commit()
    finally:
        conn.close()


STALE_RUN_MINUTES = 15


def fail_stale_runs(conn: sqlite3.Connection, minutes: int = STALE_RUN_MINUTES) -> int:
    """On startup: runs still 'running' after `minutes` died with an earlier server process."""
    cur = conn.execute(
        "UPDATE runs SET status = 'failed', finished_at = datetime('now'), error = ? "
        "WHERE status = 'running' AND started_at < datetime('now', ?)",
        (f"still running {minutes}+ minutes at server startup; the process that ran it is gone",
         f"-{minutes} minutes"))
    conn.commit()
    return cur.rowcount


# ---- what-if: parse -> simulate -> explain -> number_check (-> explain once more, then template) ----

class WhatIfState(TypedDict, total=False):
    run_id: str
    as_of_month: str
    text: str | None
    scenario: Any  # models.Scenario
    horizon: int
    base_forecast_id: int
    base_params: dict
    sim: Any  # whatif.Simulation
    scenario_forecast_id: int
    facts: list[Any]
    explanation: str
    explanation_source: str
    explain_attempts: int
    issues: list[str]
    passed: bool


def default_parser_llm(messages: list[dict], **kwargs):
    return complete("parser", messages, temperature=0, **kwargs)


def default_explainer_llm(messages: list[dict], **kwargs):
    return complete("advisor", messages, temperature=0, **kwargs)


def build_whatif_graph(conn: sqlite3.Connection, parser_llm: whatif.LLM | None = None,
                       explainer_llm: whatif.LLM | None = None):
    p_llm = parser_llm or (lambda m, **kw: default_parser_llm(m, **kw))
    e_llm = explainer_llm or (lambda m, **kw: default_explainer_llm(m, **kw))

    def parse(state: WhatIfState) -> WhatIfState:
        t0, as_of = time.monotonic(), state["as_of_month"]
        if state.get("scenario") is not None:
            errs = whatif.validate(state["scenario"], as_of)
            if errs:
                emit(conn, state["run_id"], "parse", status="error", summary="; ".join(errs))
                raise whatif.ScenarioError("; ".join(errs))
            emit(conn, state["run_id"], "parse", summary="structured scenario given; validated, no LLM call")
            return {}
        try:
            res = whatif.parse(state["text"], as_of, p_llm)
        except whatif.ScenarioError as e:
            emit(conn, state["run_id"], "parse", status="error", summary=str(e)[:500], input_ref="text")
            raise
        items = "; ".join(f"{i.kind} {i.label} {'-' if i.amount_cents < 0 else '+'}{money(i.amount_cents)} from "
                          f"{i.start_month}" + (f" for {i.months} months" if i.months else "") for i in res.scenario.items)
        emit(conn, state["run_id"], "parse", attempt=res.attempts, summary=f"{whatif.PARSE_VERSION}: {items}",
             input_ref="text", model=res.model, prompt_tokens=res.prompt_tokens,
             completion_tokens=res.completion_tokens, duration_ms=int((time.monotonic() - t0) * 1000))
        return {"scenario": res.scenario}

    def simulate(state: WhatIfState) -> WhatIfState:
        scenario, as_of = state["scenario"], state["as_of_month"]
        horizon = whatif.horizon_for(scenario, as_of, state["horizon"])
        sim = whatif.simulate(forecaster.inputs_from_params(state["base_params"]), scenario, horizon)
        params = forecaster.ForecastResult(sim.inputs, forecaster.MAX_HORIZON, [], []).params() | {
            "baseline_forecast_id": state["base_forecast_id"]}
        uid = conn.execute("SELECT user_id FROM runs WHERE id = ?", (state["run_id"],)).fetchone()[0]
        cur = conn.execute(
            "INSERT INTO forecasts (run_id, user_id, as_of_month, horizon_months, method, params, scenario, output) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (state["run_id"], uid, as_of, forecaster.MAX_HORIZON, forecaster.METHOD + " + scenario flows",
             json.dumps(params), scenario.model_dump_json(), json.dumps(forecaster.project(sim.inputs, forecaster.MAX_HORIZON))))
        conn.commit()
        emit(conn, state["run_id"], "simulate",
             summary=(f"{sim.verdict}: lowest p50 {money(sim.lowest_p50['cents'])} in {sim.lowest_p50['month']}, "
                      f"lowest p10 {money(sim.lowest_p10['cents'])} in {sim.lowest_p10['month']}; end delta "
                      f"{'-' if sim.end_delta_cents < 0 else '+'}{money(sim.end_delta_cents)} over {horizon} months"),
             input_ref=f"forecast:{state['base_forecast_id']}", output_ref=f"forecast:{cur.lastrowid}")
        return {"sim": sim, "scenario_forecast_id": cur.lastrowid, "horizon": horizon,
                "facts": whatif.facts(scenario, sim), "explain_attempts": 0, "issues": []}

    def explain(state: WhatIfState) -> WhatIfState:
        t0, attempt = time.monotonic(), state["explain_attempts"] + 1
        last = [i.split(": ", 1)[1] for i in state["issues"] if i.startswith(f"attempt {attempt - 1}: ")]
        ex = whatif.explain(state["scenario"], state["sim"], state["facts"], e_llm, last or None)
        emit(conn, state["run_id"], "explain", attempt=attempt, summary=ex.text[:300], model=ex.model,
             prompt_tokens=ex.prompt_tokens, completion_tokens=ex.completion_tokens,
             duration_ms=int((time.monotonic() - t0) * 1000))
        return {"explanation": ex.text, "explain_attempts": attempt}

    def number_check(state: WhatIfState) -> WhatIfState:
        attempt = state["explain_attempts"]
        bad = whatif.check(state["explanation"], state["facts"], state["as_of_month"])
        issues = state["issues"] + [f"attempt {attempt}: {b}" for b in bad]
        if not bad:
            emit(conn, state["run_id"], "number_check", attempt=attempt, summary="every number matches a code fact")
            return {"issues": issues, "passed": True, "explanation_source": "llm"}
        done = attempt >= whatif.MAX_EXPLAIN_ATTEMPTS
        emit(conn, state["run_id"], "number_check", attempt=attempt, status="rejected",
             summary="; ".join(bad)[:400] + ("; using the code template" if done else "; asking for a rewrite"))
        if done:
            return {"issues": issues, "passed": False, "explanation_source": "template",
                    "explanation": whatif.template(state["scenario"], state["sim"])}
        return {"issues": issues, "passed": False}

    def route(state: WhatIfState) -> str:
        return END if state["passed"] or state["explain_attempts"] >= whatif.MAX_EXPLAIN_ATTEMPTS else "explain"

    g = StateGraph(WhatIfState)
    g.add_node("parse", parse)
    g.add_node("simulate", simulate)
    g.add_node("explain", explain)
    g.add_node("number_check", number_check)
    g.add_edge(START, "parse")
    g.add_edge("parse", "simulate")
    g.add_edge("simulate", "explain")
    g.add_edge("explain", "number_check")
    g.add_conditional_edges("number_check", route, {"explain": "explain", END: END})
    return g.compile()


def run_whatif(conn: sqlite3.Connection, user_id: int, *, text: str | None = None, scenario=None,
               horizon_months: int = 6, parser_llm: whatif.LLM | None = None,
               explainer_llm: whatif.LLM | None = None) -> dict:
    """Synchronous what-if run on the user's latest baseline forecast (the one GET /api/forecast serves).

    Raises LookupError (no forecast yet) or whatif.ScenarioError (text is not a valid scenario)."""
    base = conn.execute("SELECT * FROM forecasts WHERE user_id = ? AND scenario IS NULL ORDER BY id DESC LIMIT 1",
                        (user_id,)).fetchone()
    if base is None:
        raise LookupError("no forecast yet; start a run")
    base_params = json.loads(base["params"])
    if base_params.get("insufficient_data"):
        raise whatif.ScenarioError(base_params["reason"])
    run_id = uuid.uuid4().hex
    conn.execute("INSERT INTO runs (id, user_id, kind, status, as_of_month) VALUES (?, ?, 'whatif', 'running', ?)",
                 (run_id, user_id, base["as_of_month"]))
    conn.commit()
    try:
        out = build_whatif_graph(conn, parser_llm, explainer_llm).invoke({
            "run_id": run_id, "as_of_month": base["as_of_month"], "text": text, "scenario": scenario,
            "horizon": horizon_months, "base_forecast_id": base["id"], "base_params": base_params})
    except Exception as e:
        conn.rollback()
        conn.execute("UPDATE runs SET status = 'failed', finished_at = datetime('now'), error = ? WHERE id = ?",
                     (f"{type(e).__name__}: {e}"[:500], run_id))
        conn.commit()
        raise
    conn.execute("UPDATE runs SET status = 'done', finished_at = datetime('now') WHERE id = ?", (run_id,))
    conn.commit()
    sim = out["sim"]
    return {"scenario": out["scenario"].model_dump(), "baseline": sim.baseline, "projected": sim.projected,
            "end_delta_cents": sim.end_delta_cents, "verdict": sim.verdict, "lowest_p50": sim.lowest_p50,
            "lowest_p10": sim.lowest_p10, "baseline_forecast_id": base["id"],
            "scenario_forecast_id": out["scenario_forecast_id"], "run_id": run_id,
            "explanation": out["explanation"], "explanation_source": out["explanation_source"],
            "number_check": {"passed": out["passed"], "issues": out["issues"]}}
