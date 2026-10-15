"""Profile (user) management beyond upload."""
import sqlite3

# Rows reachable from a user, without needing user_id on every table. Subqueries are evaluated at
# the moment each DELETE runs, so the order of DELETE_STEPS below matters.
_RUNS = "SELECT id FROM runs WHERE user_id = :u"
_TXNS = "SELECT id FROM transactions WHERE user_id = :u"
_GOALS = "SELECT id FROM goals WHERE user_id = :u"
_FORECASTS = "SELECT id FROM forecasts WHERE user_id = :u"
_PATTERNS = "SELECT id FROM patterns WHERE user_id = :u"
_RECS = f"SELECT id FROM recommendations WHERE run_id IN ({_RUNS}) OR goal_id IN ({_GOALS})"

# The schema has no ON DELETE CASCADE, so children are deleted before the rows they reference.
# merchant_cache (shared by every profile) and eval_runs (global) are deliberately absent.
DELETE_STEPS: list[tuple[str, str]] = [
    ("audit_trail", f"run_id IN ({_RUNS}) OR rec_id IN ({_RECS})"),
    ("recommendations", f"id IN ({_RECS})"),
    ("goal_snapshots", f"goal_id IN ({_GOALS}) OR run_id IN ({_RUNS}) OR forecast_id IN ({_FORECASTS})"),
    ("pattern_evidence", f"pattern_id IN ({_PATTERNS}) OR txn_id IN ({_TXNS})"),
    ("patterns", "user_id = :u"),
    ("categorizations", f"txn_id IN ({_TXNS}) OR run_id IN ({_RUNS})"),
    ("agent_events", f"run_id IN ({_RUNS})"),
    ("forecasts", "user_id = :u"),
    ("goals", "user_id = :u"),
    ("runs", "user_id = :u"),
    ("transactions", "user_id = :u"),
    ("batches", "user_id = :u"),
    ("users", "id = :u"),
]


def delete_user(conn: sqlite3.Connection, user_id: int) -> dict[str, int] | None:
    """Delete a user and everything that belongs to them, all or nothing.

    Returns the number of rows removed per table, or None if there is no such user. Any failure
    rolls the whole thing back.
    """
    if conn.execute("SELECT 1 FROM users WHERE id = ?", (user_id,)).fetchone() is None:
        return None
    deleted: dict[str, int] = {}
    with conn:  # one transaction: commit on success, rollback on any error
        for table, where in DELETE_STEPS:
            deleted[table] = conn.execute(f"DELETE FROM {table} WHERE {where}", {"u": user_id}).rowcount
    return deleted
