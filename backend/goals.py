"""Goal tracking with monthly replay. One active goal per profile; while it exists it owns the profile's clock
(users.as_of_month): uploads no longer move it, only these calls do.

- create: validates, rewinds the clock to start_month (if earlier) and runs the first goal check there
- advance: moves the clock one month and runs a goal check
- reset: jumps the clock to the latest data month, with one check there unless the goal is already final
- delete: removes the goal with its checks and restores the clock to the latest data month

A goal check runs analyze -> forecast -> compare_to_plan synchronously (code only, so the status is in the
response). When compare_to_plan decides to advise, the caller continues the same run from `advise` in the
background (graph.execute_run(run_id, entry="advise")); with defer_advice=False the whole run happens here.
"""
from __future__ import annotations

import sqlite3
import uuid

from backend.agents import advisor, forecaster
from backend.agents.goal_tracker import MAX_TARGET_MONTHS
from backend.graph import build_graph, ranked_recommendations
from backend.ledger import add_months, load_txns, months_between
from backend.models import GoalCreate


class GoalError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def latest_data_month(conn: sqlite3.Connection, user_id: int) -> str | None:
    row = conn.execute("SELECT MAX(posted_date) FROM transactions WHERE user_id = ?", (user_id,)).fetchone()[0]
    return row[:7] if row else None


def _goal(conn: sqlite3.Connection, goal_id: int) -> sqlite3.Row:
    g = conn.execute("SELECT * FROM goals WHERE id = ?", (goal_id,)).fetchone()
    if g is None:
        raise GoalError(404, "goal not found")
    return g


def _running(conn: sqlite3.Connection, goal_id: int) -> str | None:
    row = conn.execute("SELECT id FROM runs WHERE goal_id = ? AND status IN ('pending', 'running') ORDER BY rowid DESC "
                       "LIMIT 1", (goal_id,)).fetchone()
    return row["id"] if row else None


def _last_snapshot(conn: sqlite3.Connection, goal_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM goal_snapshots WHERE goal_id = ? ORDER BY month DESC LIMIT 1",
                        (goal_id,)).fetchone()


def goal_dict(g: sqlite3.Row) -> dict:
    return {k: g[k] for k in ("id", "user_id", "name", "target_cents", "start_month", "target_month", "created_at")}


def list_goals(conn: sqlite3.Connection, user_id: int) -> list[dict]:
    return [goal_dict(g) for g in conn.execute("SELECT * FROM goals WHERE user_id = ?", (user_id,))]


# ---- goal checks ----

def check_month(conn: sqlite3.Connection, goal_id: int, month: str, *, defer_advice: bool = True,
                advisor_llm: advisor.LLM | None = None,
                advisor_version: str = advisor.PROMPT_VERSION) -> tuple[str, bool]:
    """Move the clock to month and run a goal check there. Returns (run_id, advice_pending): advice_pending means the
    run stopped before advise and the caller must continue it from 'advise'. On failure the clock is put back."""
    g = _goal(conn, goal_id)
    uid = g["user_id"]
    before = conn.execute("SELECT as_of_month FROM users WHERE id = ?", (uid,)).fetchone()[0]
    run_id = uuid.uuid4().hex
    conn.execute("INSERT INTO runs (id, user_id, goal_id, kind, status, as_of_month) "
                 "VALUES (?, ?, ?, 'goal_check', 'running', ?)", (run_id, uid, goal_id, month))
    conn.execute("UPDATE users SET as_of_month = ? WHERE id = ?", (month, uid))
    conn.commit()
    try:
        out = build_graph(conn, advisor_llm=advisor_llm, entry="analyze", advisor_version=advisor_version,
                          kind="goal_check").invoke({"run_id": run_id, "user_id": uid, "as_of_month": month,
                                                     "audit_events": [], "defer_advice": defer_advice})
    except Exception as e:
        conn.rollback()
        conn.execute("UPDATE runs SET status = 'failed', finished_at = datetime('now'), error = ? WHERE id = ?",
                     (f"{type(e).__name__}: {e}"[:500], run_id))
        conn.execute("UPDATE users SET as_of_month = ? WHERE id = ?", (before, uid))
        conn.commit()
        raise
    pending = defer_advice and out["goal_decision"] == "advise"
    if not pending:
        conn.execute("UPDATE runs SET status = 'done', finished_at = datetime('now') WHERE id = ?", (run_id,))
        conn.commit()
    return run_id, pending


def create_goal(conn: sqlite3.Connection, user_id: int, body: GoalCreate, **kw) -> tuple[int, str, bool]:
    """Returns (goal_id, run_id, advice_pending). kw goes to check_month."""
    existing = conn.execute("SELECT id FROM goals WHERE user_id = ?", (user_id,)).fetchone()
    if existing:
        raise GoalError(409, f"This profile already has an active goal (id {existing['id']}). Delete it first "
                             f"(DELETE /api/goals/{existing['id']}); only one goal per profile is tracked.")
    as_of = conn.execute("SELECT as_of_month FROM users WHERE id = ?", (user_id,)).fetchone()["as_of_month"]
    if not conn.execute("SELECT 1 FROM runs WHERE user_id = ? AND kind = 'main' AND status = 'done'",
                        (user_id,)).fetchone():
        raise GoalError(422, "Run the pipeline first: the goal check needs categorized transactions.")
    start = body.start_month or as_of
    if start > as_of:
        raise GoalError(422, f"start_month {start} is after the profile's current month {as_of}.")
    reason = forecaster.insufficient_reason(load_txns(conn, user_id, start), start)
    if reason:
        raise GoalError(422, f"Cannot start a goal at {start}. {reason}")
    n = len(months_between(start, body.target_month)) - 1
    if not 1 <= n <= MAX_TARGET_MONTHS:
        raise GoalError(422, f"target_month must be 1 to {MAX_TARGET_MONTHS} months after the start month {start} "
                             f"(got {body.target_month}).")
    goal_id = conn.execute(
        "INSERT INTO goals (user_id, name, target_cents, start_month, target_month, restore_as_of_month) "
        "VALUES (?, ?, ?, ?, ?, ?)", (user_id, body.name, body.target_cents, start, body.target_month, as_of)).lastrowid
    conn.commit()
    try:
        run_id, pending = check_month(conn, goal_id, start, **kw)
    except Exception:
        delete_goal(conn, goal_id)  # no half-created goal; the clock goes back
        raise
    return goal_id, run_id, pending


def advance_state(conn: sqlite3.Connection, goal_id: int) -> tuple[str | None, str | None]:
    """(next month advance would check, why advance is blocked); exactly one is None."""
    g = _goal(conn, goal_id)
    running = _running(conn, goal_id)
    if running:
        return None, f"The previous goal check (run {running}) is still running."
    last = _last_snapshot(conn, goal_id)
    if last is not None and last["status"] in ("achieved", "missed"):
        return None, f"The goal reached its target month ({last['status']}); delete it to start a new one."
    as_of = conn.execute("SELECT as_of_month FROM users WHERE id = ?", (g["user_id"],)).fetchone()[0]
    nxt, latest = add_months(as_of, 1), latest_data_month(conn, g["user_id"])
    if latest is None or nxt > latest:
        return None, f"No transactions after {as_of} yet; upload newer data to continue."
    return nxt, None


def advance(conn: sqlite3.Connection, goal_id: int, **kw) -> tuple[str, bool]:
    nxt, blocked = advance_state(conn, goal_id)
    if blocked:
        raise GoalError(409, blocked)
    return check_month(conn, goal_id, nxt, **kw)


def reset_to_latest(conn: sqlite3.Connection, goal_id: int, **kw) -> tuple[str | None, bool]:
    """Jump the clock to the latest data month; one goal check there unless the goal is already final."""
    g = _goal(conn, goal_id)
    running = _running(conn, goal_id)
    if running:
        raise GoalError(409, f"The previous goal check (run {running}) is still running.")
    as_of = conn.execute("SELECT as_of_month FROM users WHERE id = ?", (g["user_id"],)).fetchone()[0]
    latest = latest_data_month(conn, g["user_id"])
    if latest is None or as_of >= latest:
        raise GoalError(409, f"Already at the latest data month ({as_of}).")
    last = _last_snapshot(conn, goal_id)
    if last is not None and last["status"] in ("achieved", "missed"):
        conn.execute("UPDATE users SET as_of_month = ? WHERE id = ?", (latest, g["user_id"]))
        conn.commit()
        return None, False
    return check_month(conn, goal_id, latest, **kw)


# ---- delete ----

_GOAL_RUNS = "SELECT id FROM runs WHERE goal_id = :g"
# children before the rows they reference (no ON DELETE CASCADE in the schema)
DELETE_STEPS: list[tuple[str, str]] = [
    ("audit_trail", f"run_id IN ({_GOAL_RUNS})"),
    ("recommendations", f"run_id IN ({_GOAL_RUNS}) OR goal_id = :g"),
    ("goal_snapshots", "goal_id = :g"),
    ("pattern_evidence", f"pattern_id IN (SELECT id FROM patterns WHERE run_id IN ({_GOAL_RUNS}))"),
    ("patterns", f"run_id IN ({_GOAL_RUNS})"),
    ("agent_events", f"run_id IN ({_GOAL_RUNS})"),
    ("forecasts", f"run_id IN ({_GOAL_RUNS})"),
    ("runs", "goal_id = :g"),
    ("goals", "id = :g"),
]


def delete_goal(conn: sqlite3.Connection, goal_id: int) -> dict:
    """Remove the goal, its checks and their runs, and restore the clock to the latest data month (the later of the
    clock before the goal and the newest transaction, since uploads during the goal did not move it)."""
    g = _goal(conn, goal_id)
    running = _running(conn, goal_id)
    if running:
        raise GoalError(409, f"A goal check (run {running}) is still running; delete the goal when it finishes.")
    restored = max(g["restore_as_of_month"], latest_data_month(conn, g["user_id"]) or g["restore_as_of_month"])
    deleted: dict[str, int] = {}
    with conn:
        for table, where in DELETE_STEPS:
            deleted[table] = conn.execute(f"DELETE FROM {table} WHERE {where}", {"g": goal_id}).rowcount
        conn.execute("UPDATE users SET as_of_month = ? WHERE id = ?", (restored, g["user_id"]))
    return {"goal_id": goal_id, "as_of_month": restored, "deleted_rows": deleted}


# ---- progress ----

def progress(conn: sqlite3.Connection, goal_id: int) -> dict:
    g = _goal(conn, goal_id)
    snaps = conn.execute("SELECT s.*, r.status AS run_status FROM goal_snapshots s JOIN runs r ON r.id = s.run_id "
                         "WHERE s.goal_id = ? ORDER BY s.month", (goal_id,)).fetchall()
    month_of_run = {s["run_id"]: s["month"] for s in snaps}
    months = [{
        **{k: s[k] for k in ("month", "status", "saved_cents", "planned_cents", "projected_p50_cents",
                             "projected_p10_cents", "gap_cents", "projected_completion", "run_id", "run_status",
                             "decision", "reason")},
        "advice_from_month": month_of_run.get(s["advice_run_id"]),
        "recommendations": ranked_recommendations(conn, s["advice_run_id"]) if s["advice_run_id"] else [],
    } for s in snaps]
    nxt, blocked = advance_state(conn, goal_id)
    return {"goal": goal_dict(g),
            "as_of_month": conn.execute("SELECT as_of_month FROM users WHERE id = ?", (g["user_id"],)).fetchone()[0],
            "latest_data_month": latest_data_month(conn, g["user_id"]), "next_month": nxt, "advance_blocked": blocked,
            "current": months[-1] if months else None, "months": months}
