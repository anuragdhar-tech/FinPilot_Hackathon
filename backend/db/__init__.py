"""SQLite access: one connection helper and schema init. No ORM by design: plain SQL is easy to read and audit."""
import os
import sqlite3
from pathlib import Path

SCHEMA_PATH = Path(__file__).with_name("schema.sql")
DEFAULT_DB_PATH = Path(__file__).resolve().parents[2] / "data" / "finpilot.db"


def db_path() -> Path:
    return Path(os.environ.get("FINPILOT_DB", DEFAULT_DB_PATH))


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    """Open a connection with foreign keys on and dict-like rows. Pass ':memory:' in tests."""
    target = str(path if path is not None else db_path())
    if target != ":memory:":
        Path(target).parent.mkdir(parents=True, exist_ok=True)
    # check_same_thread=False: FastAPI may open a request's connection in one worker thread
    # and use it in another. Each request/run still gets its own connection.
    conn = sqlite3.connect(target, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """Create all tables. Idempotent."""
    conn.executescript(SCHEMA_PATH.read_text())
    conn.commit()
