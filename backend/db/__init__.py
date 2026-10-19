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


# Columns added after a table first shipped; CREATE TABLE IF NOT EXISTS does not add them to older DBs.
ADDED_COLUMNS = {
    "recommendations": [("action", "TEXT NOT NULL DEFAULT ''"), ("target", "TEXT NOT NULL DEFAULT ''"),
                        ("impact_annual_cents", "INTEGER")],
    "audit_trail": [("rule_checks", "TEXT NOT NULL DEFAULT '[]'")],
    "runs": [("goal_id", "INTEGER REFERENCES goals(id)")],
}


def _rebuild_unused_goal_tables(conn: sqlite3.Connection) -> None:
    """goals/goal_snapshots shipped as placeholders (target_date, on_track) and no endpoint ever wrote them; an old,
    empty pair is dropped and recreated in the goal-replay shape. Rows in them would be a surprise: refuse."""
    if "target_date" not in {r["name"] for r in conn.execute("PRAGMA table_info(goals)")}:
        return
    if any(conn.execute(f"SELECT 1 FROM {t} LIMIT 1").fetchone() for t in ("goals", "goal_snapshots")):
        raise RuntimeError("old-shape goals/goal_snapshots tables hold rows; migrate them by hand")
    conn.execute("DROP TABLE goal_snapshots")
    conn.execute("DROP TABLE goals")
    conn.executescript(SCHEMA_PATH.read_text())


def init_db(conn: sqlite3.Connection) -> None:
    """Create all tables and add any missing later columns. Idempotent."""
    conn.executescript(SCHEMA_PATH.read_text())
    _rebuild_unused_goal_tables(conn)
    for table, cols in ADDED_COLUMNS.items():
        have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        for name, ddl in cols:
            if name not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
    conn.commit()
