import json
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, HTTPException, UploadFile

from backend.db import connect, init_db
from backend.graph import create_run, execute_run
from backend.ingest import CsvError, active_user_id, ingest_csv
from backend.models import (Category, EvalSummary, Month, RunCreate, RunCreated, RunStatus, TransactionList,
                            UploadResponse, User, UserDeleted, UserList)
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


@app.get("/api/eval/latest", response_model=EvalSummary)
def eval_latest():
    """Serves the script-written eval/results/categorization.json (canonical; never typed by hand)."""
    path = EVAL_RESULTS / "categorization.json"
    if not path.exists():
        raise HTTPException(404, "no eval results yet; run python -m eval.categorization")
    return json.loads(path.read_text())["summary"]
