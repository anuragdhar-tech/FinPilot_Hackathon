import sqlite3

import pytest

from backend.db import connect, init_db

TABLES = {
    "users", "batches", "transactions", "runs", "agent_events", "merchant_cache",
    "categorizations", "patterns", "pattern_evidence", "forecasts", "goals",
    "goal_snapshots", "recommendations", "audit_trail", "eval_runs",
}


@pytest.fixture
def conn():
    c = connect(":memory:")
    init_db(c)
    yield c
    c.close()


def _user(conn):
    return conn.execute(
        "INSERT INTO users (name, opening_date, as_of_month) VALUES ('u', '2025-10-01', '2026-09')"
    ).lastrowid


def test_all_tables_created(conn):
    names = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert TABLES <= names


def test_init_is_idempotent(conn):
    init_db(conn)


def test_foreign_keys_enforced(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO pattern_evidence (pattern_id, txn_id) VALUES (999, 999)")


def test_duplicate_external_id_rejected(conn):
    uid = _user(conn)
    conn.execute("INSERT INTO batches (id, user_id, filename, row_count) VALUES ('b1', ?, 'f.csv', 2)", (uid,))
    row = (uid, "b1", "ext-1", "2026-01-02", "DESC", -500)
    sql = ("INSERT INTO transactions (user_id, batch_id, external_id, posted_date, raw_description, amount_cents) "
           "VALUES (?, ?, ?, ?, ?, ?)")
    conn.execute(sql, row)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(sql, row)


def test_enum_checks(conn):
    uid = _user(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO runs (id, user_id, kind, status, as_of_month) VALUES ('r', ?, 'bogus', 'pending', '2026-09')",
            (uid,),
        )
