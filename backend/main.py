import sqlite3
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile

from backend.db import connect, init_db
from backend.ingest import CsvError, active_user_id, ingest_csv
from backend.models import UploadResponse, User, UserList


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
