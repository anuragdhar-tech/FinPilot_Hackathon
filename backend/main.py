import json
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, HTTPException, Query, UploadFile

from backend.db import connect, init_db
from backend.graph import create_run, execute_run
from backend.ingest import CsvError, active_user_id, ingest_csv
from backend.agents.forecaster import MAX_HORIZON
from backend.models import (Category, EvalSummary, Forecast, Month, PatternList, RunCreate, RunCreated, RunList,
                            RunStatus, TransactionList, UploadResponse, User, UserDeleted, UserList)
from backend.users import delete_user

EVAL_RESULTS = Path(__file__).resolve().parents[1] / "eval" / "results"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    conn = connect()
    init_db(conn)
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
    conn: sqlite3.Connection = Depends(get_conn),
):
    try:
        text = (await file.read()).decode("utf-8")
    except UnicodeDecodeError:
        raise HTTPException(422, {"errors": ["file is not UTF-8 text"]})
    try:
        return ingest_csv(conn, file.filename or "upload.csv", text, user_id=user_id, name=name,
                          opening_balance_cents=opening_balance_cents)
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
            "points": json.loads(row["output"])[:horizon_months]}


@app.get("/api/eval/latest", response_model=EvalSummary)
def eval_latest(version: str | None = Query(None, pattern=r"^cat-v\d+$"),
                forecast_version: str | None = Query(None, pattern=r"^fc-v\d+$")):
    """Serves the script-written eval/results/categorization.json (canonical; never typed by hand).

    ?version=cat-v1 serves the committed categorization_cat-v1.json instead. Prompt version, tokens and
    cost come from the same file's details block. Pattern and forecast metrics are merged in from
    patterns.json and forecast.json (eval/analysis.py); ?forecast_version=fc-v1 takes the forecast
    metrics from the committed forecast_fc-v1.json instead.
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
    for name, keys in (("patterns", ("recurring", "unused", "anomaly", "pattern_types")), (forecast_file, ("forecast",))):
        extra = EVAL_RESULTS / f"{name}.json"
        if extra.exists():
            s = json.loads(extra.read_text())["summary"]
            summary |= {k: s[k] for k in keys if k in s}
            summary["models"] = s.get("models", {}) | summary.get("models", {})
    return summary
