import json
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, HTTPException, Query, UploadFile

from backend.db import connect, init_db
from backend.agents.whatif import ScenarioError
from backend.graph import create_run, execute_run, fail_stale_runs, ranked_recommendations, run_whatif
from backend.ingest import CsvError, active_user_id, ingest_csv
from backend.llm.client import LLMUnavailable
from backend.agents.forecaster import MAX_HORIZON
from backend.models import (AuditRecord, Category, EvalSummary, Forecast, GoalAdvance, GoalCreate, GoalDeleted,
                            GoalList, GoalProgress, Month, PatternList, RecommendationList, RunCreate, RunCreated,
                            RunList, RunStatus, SimulateRequest, SimulateResponse, TransactionList, UploadResponse,
                            User, UserDeleted, UserList)
from backend import goals
from backend.users import delete_user

EVAL_RESULTS = Path(__file__).resolve().parents[1] / "eval" / "results"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    conn = connect()
    init_db(conn)
    fail_stale_runs(conn)
    conn.close()
    yield


app = FastAPI(title="FinPilot", lifespan=lifespan)


def get_conn():
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


def resolve_user(conn: sqlite3.Connection, user_id: int | None) -> int:
    """GET endpoints take an optional ?user_id=; default is the active user (latest upload)."""
    uid = user_id if user_id is not None else active_user_id(conn)
    if uid is None or not conn.execute("SELECT 1 FROM users WHERE id = ?", (uid,)).fetchone():
        raise HTTPException(404, "no such user" if user_id is not None else "no data uploaded yet")
    return uid


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/api/transactions/upload", response_model=UploadResponse)
async def upload_transactions(
    file: UploadFile = File(...),
    user_id: int | None = Form(None),
    name: str | None = Form(None),
    opening_balance_cents: int = Form(0),
    positive_is_spend: bool = Form(False),
    date_order: Literal["mdy", "dmy"] | None = Form(None),
    conn: sqlite3.Connection = Depends(get_conn),
):
    try:
        text = (await file.read()).decode("utf-8")
    except UnicodeDecodeError:
        raise HTTPException(422, {"errors": ["file is not UTF-8 text"]})
    try:
        return ingest_csv(conn, file.filename or "upload.csv", text, user_id=user_id, name=name,
                          opening_balance_cents=opening_balance_cents, positive_is_spend=positive_is_spend,
                          date_order=date_order)
    except CsvError as e:
        raise HTTPException(422, {"errors": e.errors})
    except LookupError as e:
        raise HTTPException(404, str(e))


@app.get("/api/users", response_model=UserList)
def list_users(conn: sqlite3.Connection = Depends(get_conn)):
    active = active_user_id(conn)
    rows = conn.execute(
        "SELECT u.*, (SELECT COUNT(*) FROM transactions t WHERE t.user_id = u.id) AS txn_count "
        "FROM users u ORDER BY u.id"
    ).fetchall()
    return {"items": [{**dict(r), "is_active": r["id"] == active} for r in rows]}


@app.delete("/api/users/{user_id}", response_model=UserDeleted)
def delete_profile(user_id: int, conn: sqlite3.Connection = Depends(get_conn)):
    """Remove a profile and everything that belongs to it. The shared merchant cache is kept."""
    deleted = delete_user(conn, user_id)
    if deleted is None:
        raise HTTPException(404, "no such user")
    return {"user_id": user_id, "deleted_rows": deleted}


@app.post("/api/runs", response_model=RunCreated)
def start_run(body: RunCreate, background: BackgroundTasks, conn: sqlite3.Connection = Depends(get_conn)):
    try:
        run_id = create_run(conn, body.batch_id)
    except LookupError as e:
        raise HTTPException(404, str(e))
    background.add_task(execute_run, run_id)
    return {"run_id": run_id}


@app.get("/api/runs", response_model=RunList)
def list_runs(user_id: int | None = None, conn: sqlite3.Connection = Depends(get_conn)):
    """Runs of one user (default: active user), newest first."""
    uid = resolve_user(conn, user_id)
    rows = conn.execute(
        "SELECT r.*, r.id AS run_id, (SELECT COUNT(*) FROM agent_events e WHERE e.run_id = r.id) AS event_count "
        "FROM runs r WHERE r.user_id = ? ORDER BY r.started_at DESC, r.rowid DESC", (uid,)).fetchall()
    return {"items": [dict(r) for r in rows]}


@app.get("/api/runs/{run_id}", response_model=RunStatus)
def get_run(run_id: str, conn: sqlite3.Connection = Depends(get_conn)):
    run = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    if run is None:
        raise HTTPException(404, "run not found")
    events = conn.execute("SELECT * FROM agent_events WHERE run_id = ? ORDER BY seq", (run_id,)).fetchall()
    return {**dict(run), "run_id": run_id, "events": [dict(e) for e in events]}


@app.get("/api/transactions", response_model=TransactionList)
def list_transactions(month: Month | None = None, category: Category | None = None,
                      user_id: int | None = None, conn: sqlite3.Connection = Depends(get_conn)):
    """Each row carries its latest categorization (append-only table: highest id wins); NULLs if none."""
    uid = resolve_user(conn, user_id)
    sql = ("SELECT t.id, t.external_id, t.posted_date AS date, t.raw_description AS description, t.amount_cents, "
           "c.category, c.confidence, c.rationale, c.model, c.prompt_version, c.from_cache "
           "FROM transactions t LEFT JOIN categorizations c ON c.id = "
           "(SELECT MAX(id) FROM categorizations WHERE txn_id = t.id) WHERE t.user_id = ?")
    args: list = [uid]
    if month:
        sql += " AND substr(t.posted_date, 1, 7) = ?"
        args.append(month)
    if category:
        sql += " AND c.category = ?"
        args.append(category)
    rows = [dict(r) for r in conn.execute(sql + " ORDER BY t.posted_date DESC, t.id DESC", args)]
    for r in rows:
        r["from_cache"] = None if r["from_cache"] is None else bool(r["from_cache"])
    return {"items": rows, "total": len(rows)}


@app.get("/api/patterns", response_model=PatternList)
def list_patterns(user_id: int | None = None, conn: sqlite3.Connection = Depends(get_conn)):
    """Patterns from the user's latest finished main run (append-only table, so older runs stay queryable)."""
    uid = resolve_user(conn, user_id)
    run = conn.execute("SELECT id, as_of_month FROM runs WHERE user_id = ? AND kind = 'main' AND status = 'done' "
                       "ORDER BY started_at DESC, rowid DESC LIMIT 1", (uid,)).fetchone()
    if run is None:
        return {"items": []}
    rows = conn.execute("SELECT * FROM patterns WHERE run_id = ? ORDER BY id", (run["id"],)).fetchall()
    evidence: dict[int, list[int]] = {}
    for e in conn.execute("SELECT pe.pattern_id, pe.txn_id FROM pattern_evidence pe JOIN patterns p ON p.id = pe.pattern_id "
                          "WHERE p.run_id = ? ORDER BY pe.txn_id", (run["id"],)):
        evidence.setdefault(e["pattern_id"], []).append(e["txn_id"])
    items = [{"id": r["id"], "type": r["type"], "title": r["title"], "narration": r["narration"],
              "params": json.loads(r["params"]), "evidence_txn_ids": evidence.get(r["id"], [])} for r in rows]
    return {"items": items, "run_id": run["id"], "as_of_month": run["as_of_month"]}


@app.get("/api/forecast", response_model=Forecast)
def get_forecast(horizon_months: int = Query(6, ge=1, le=MAX_HORIZON), user_id: int | None = None,
                 conn: sqlite3.Connection = Depends(get_conn)):
    """Latest baseline forecast for the user (stored at the full horizon), cut to horizon_months."""
    uid = resolve_user(conn, user_id)
    row = conn.execute("SELECT * FROM forecasts WHERE user_id = ? AND scenario IS NULL ORDER BY id DESC LIMIT 1",
                       (uid,)).fetchone()
    if row is None:
        raise HTTPException(404, "no forecast yet; start a run")
    params = json.loads(row["params"])
    return {"id": row["id"], "as_of_month": row["as_of_month"], "horizon_months": horizon_months,
            "method": row["method"], "start_balance_cents": params["start_balance_cents"],
            "points": json.loads(row["output"])[:horizon_months],
            "insufficient_data": params.get("insufficient_data", False), "reason": params.get("reason")}


def latest_main_run(conn: sqlite3.Connection, uid: int):
    return conn.execute("SELECT id, as_of_month FROM runs WHERE user_id = ? AND kind = 'main' AND status = 'done' "
                        "ORDER BY started_at DESC, rowid DESC LIMIT 1", (uid,)).fetchone()


@app.get("/api/recommendations", response_model=RecommendationList)
def list_recommendations(user_id: int | None = None, conn: sqlite3.Connection = Depends(get_conn)):
    """Accepted recommendations from the user's latest finished main run, ranked by verifier-recomputed impact."""
    uid = resolve_user(conn, user_id)
    run = latest_main_run(conn, uid)
    if run is None:
        return {"items": []}
    return {"items": ranked_recommendations(conn, run["id"]), "run_id": run["id"], "as_of_month": run["as_of_month"]}


@app.get("/api/recommendations/{rec_id}/audit", response_model=AuditRecord)
def recommendation_audit(rec_id: int, conn: sqlite3.Connection = Depends(get_conn)):
    """Why this was recommended: the trigger pattern and every draft of the lineage with its verifier checks."""
    rec = conn.execute("SELECT * FROM recommendations WHERE id = ?", (rec_id,)).fetchone()
    if rec is None:
        raise HTTPException(404, "recommendation not found")
    rows = conn.execute(
        "SELECT r.*, a.verifier_verdict, a.reasons, a.checks, a.rule_checks, a.inputs, a.ts AS audit_ts "
        "FROM recommendations r JOIN audit_trail a ON a.rec_id = r.id WHERE r.lineage_id = ? ORDER BY r.attempt",
        (rec["lineage_id"],)).fetchall()
    trigger = None
    if rec["trigger_type"] == "pattern" and rec["trigger_id"].isdigit():
        p = conn.execute("SELECT title FROM patterns WHERE id = ? AND run_id = ?",
                         (int(rec["trigger_id"]), rec["run_id"])).fetchone()
        if p:
            ev = [r[0] for r in conn.execute("SELECT txn_id FROM pattern_evidence WHERE pattern_id = ? ORDER BY txn_id",
                                             (int(rec["trigger_id"]),))]
            trigger = {"title": p["title"], "evidence_txn_ids": ev}
    last = rows[-1]
    return {"recommendation_id": rec_id, "lineage_id": rec["lineage_id"], "run_id": rec["run_id"],
            "trigger_type": rec["trigger_type"], "trigger_id": rec["trigger_id"], "trigger": trigger,
            "inputs": json.loads(last["inputs"]), "model": last["model"], "prompt_version": last["prompt_version"],
            "attempts": [{"attempt": r["attempt"], "rec_id": r["id"], "status": r["status"], "action": r["action"],
                          "target": r["target"], "title": r["title"], "body": r["body"],
                          "claims": json.loads(r["claims"]), "verdict": r["verifier_verdict"],
                          "reasons": json.loads(r["reasons"]), "checks": json.loads(r["checks"]),
                          "rule_checks": json.loads(r["rule_checks"]), "ts": r["audit_ts"]} for r in rows]}


@app.post("/api/simulate", response_model=SimulateResponse)
def simulate(body: SimulateRequest, user_id: int | None = None, conn: sqlite3.Connection = Depends(get_conn)):
    """What-if on the user's latest baseline forecast. Synchronous: one parse call and up to two explain calls."""
    uid = resolve_user(conn, user_id)
    try:
        return run_whatif(conn, uid, text=body.text, scenario=body.scenario, horizon_months=body.horizon_months)
    except LookupError as e:
        raise HTTPException(404, str(e))
    except ScenarioError as e:
        raise HTTPException(422, {"errors": [str(e)]})
    except LLMUnavailable as e:
        raise HTTPException(503, {"errors": [str(e)]})


# ---- goals (one per profile; the goal owns the profile's clock while it exists) ----

def _goal_call(fn, *args, **kw):
    try:
        return fn(*args, **kw)
    except goals.GoalError as e:
        raise HTTPException(e.status, {"errors": [str(e)]})


def _goal_response(conn: sqlite3.Connection, background: BackgroundTasks, goal_id: int, run_id: str | None,
                   advice_pending: bool) -> dict:
    """Status is already computed (sync); the Advisor, when needed, continues the same run in the background."""
    if advice_pending:
        background.add_task(execute_run, run_id, entry="advise")
    return {"run_id": run_id, "progress": goals.progress(conn, goal_id)}


@app.post("/api/goals", response_model=GoalAdvance)
def create_goal(body: GoalCreate, background: BackgroundTasks, user_id: int | None = None,
                conn: sqlite3.Connection = Depends(get_conn)):
    """Create the profile's goal and check it at start_month (an earlier start_month rewinds the clock for replay)."""
    uid = resolve_user(conn, user_id)
    goal_id, run_id, pending = _goal_call(goals.create_goal, conn, uid, body)
    return _goal_response(conn, background, goal_id, run_id, pending)


@app.get("/api/goals", response_model=GoalList)
def list_goals(user_id: int | None = None, conn: sqlite3.Connection = Depends(get_conn)):
    return {"items": goals.list_goals(conn, resolve_user(conn, user_id))}


@app.get("/api/goals/{goal_id}/progress", response_model=GoalProgress)
def goal_progress(goal_id: int, conn: sqlite3.Connection = Depends(get_conn)):
    return _goal_call(goals.progress, conn, goal_id)


@app.post("/api/goals/{goal_id}/advance", response_model=GoalAdvance)
def advance_goal(goal_id: int, background: BackgroundTasks, conn: sqlite3.Connection = Depends(get_conn)):
    """Replay the next month: move the clock one month, re-run analyze and forecast, compare to the plan."""
    run_id, pending = _goal_call(goals.advance, conn, goal_id)
    return _goal_response(conn, background, goal_id, run_id, pending)


@app.post("/api/goals/{goal_id}/reset", response_model=GoalAdvance)
def reset_goal_clock(goal_id: int, background: BackgroundTasks, conn: sqlite3.Connection = Depends(get_conn)):
    """Reset to latest: jump the clock to the latest data month (one goal check there unless the goal is final)."""
    run_id, pending = _goal_call(goals.reset_to_latest, conn, goal_id)
    return _goal_response(conn, background, goal_id, run_id, pending)


@app.delete("/api/goals/{goal_id}", response_model=GoalDeleted)
def delete_goal(goal_id: int, conn: sqlite3.Connection = Depends(get_conn)):
    """Remove the goal and its checks; the profile's clock goes back to the latest data month."""
    return _goal_call(goals.delete_goal, conn, goal_id)


@app.get("/api/eval/latest", response_model=EvalSummary)
def eval_latest(version: str | None = Query(None, pattern=r"^cat-v\d+$"),
                forecast_version: str | None = Query(None, pattern=r"^fc-v\d+$")):
    """Serves the script-written eval/results/categorization.json (canonical; never typed by hand).

    ?version=cat-v1 serves the committed categorization_cat-v1.json instead. Prompt version, tokens and
    cost come from the same file's details block. Pattern and forecast metrics are merged in from
    patterns.json and forecast.json (eval/analysis.py); ?forecast_version=fc-v1 takes the forecast
    metrics from the committed forecast_fc-v1.json instead. Verifier loop metrics come from advice.json
    (eval/advice.py).
    """
    path = EVAL_RESULTS / ("categorization.json" if version is None else f"categorization_{version}.json")
    if not path.exists():
        raise HTTPException(404, "no eval results yet; run python -m eval.categorization" if version is None
                            else f"no committed eval results for {version}")
    res = json.loads(path.read_text())
    summary, details = res["summary"], res.get("details", {})
    usage = details.get("usage", {})
    if summary.get("categorization") is not None:
        summary["categorization"] |= {
            "prompt_version": details.get("prompt_version"),
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
            "cost_usd": usage.get("estimated_cost", {}).get("usd"),
        }
    # pattern and forecast metrics come from eval/analysis.py's own result files, when present
    forecast_file = "forecast" if forecast_version is None else f"forecast_{forecast_version}"
    if forecast_version and not (EVAL_RESULTS / f"{forecast_file}.json").exists():
        raise HTTPException(404, f"no committed forecast results for {forecast_version}")
    for name, keys in (("patterns", ("recurring", "unused", "anomaly", "pattern_types")), (forecast_file, ("forecast",)),
                       ("advice", ("verifier", "grounded_number_rate"))):
        extra = EVAL_RESULTS / f"{name}.json"
        if extra.exists():
            s = json.loads(extra.read_text())["summary"]
            summary |= {k: s[k] for k in keys if k in s}
            summary["models"] = s.get("models", {}) | summary.get("models", {})
    return summary
