"""Goal tracking with monthly replay: status from the forecast (p10/p50 rule, exact thresholds), the replay clock
(rewind, advance, reset, delete restores it, uploads do not move it), the Advisor called only when the status or the
actionable patterns change, goal claims recomputed by the Verifier, and the API's validation and 409s.

Ledger: tests/ledger_fixture.py plus a planted rent of $3,500/month from 2026-06 (a regime change), so a goal set in
2026-03 starts ahead and falls behind."""
import json
from datetime import date

import pytest
from fastapi.testclient import TestClient

import backend.graph as graph
from backend import goals
from backend.agents import advisor, goal_tracker, verifier
from backend.agents.goal_tracker import Goal, check, decide, planned, status_for
from backend.db import connect
from backend.ledger import Txn
from backend.models import GoalCreate
from tests.ledger_fixture import (ScriptedAdvisor, build_ledger, call_tool, figure, final, pattern_id, run_main,
                                  uber_rec)

RENT_MONTHS = ("2026-06", "2026-07", "2026-08", "2026-09")


def add_rent(path, uid: int) -> None:
    conn = connect(path)
    for i, m in enumerate(RENT_MONTHS):
        tid = conn.execute("INSERT INTO transactions (user_id, batch_id, external_id, posted_date, raw_description, "
                           "amount_cents) VALUES (?, 'b1', ?, ?, 'ELM ST PROPERTY MGMT', -350000)",
                           (uid, f"rent{i}", f"{m}-02")).lastrowid
        conn.execute("INSERT INTO categorizations (txn_id, run_id, category, confidence, model, prompt_version, "
                     "from_cache) VALUES (?, 'labels', 'housing', 0.9, 'fixture', 'fixture', 0)", (tid,))
    conn.commit()
    conn.close()


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "app.db"
    add_rent(path, build_ledger(path))
    monkeypatch.setenv("FINPILOT_DB", str(path))
    return path


@pytest.fixture
def api(db):
    from backend.main import app
    with TestClient(app) as c:
        yield c


@pytest.fixture
def advisor_calls(monkeypatch):
    """The default Advisor (no recommendations), counting first drafts."""
    calls = []

    def counting(messages, **kwargs):
        calls.append(messages)
        return final()
    monkeypatch.setattr(graph, "default_advisor_llm", counting)
    return calls


def as_of(path) -> str:
    conn = connect(path)
    m = conn.execute("SELECT as_of_month FROM users").fetchone()[0]
    conn.close()
    return m


GOAL = {"name": "Emergency fund", "target_cents": 1_000_000, "target_month": "2026-09", "start_month": "2026-03"}


# ---- status rule (code only) ----

POINTS = [{"month": "2026-04", "balance_p50": 150000, "balance_p10": 120000},
          {"month": "2026-05", "balance_p50": 200000, "balance_p10": 150000}]


def goal(target: int, target_month: str = "2026-05") -> Goal:
    return Goal(1, "g", target, "2026-03", target_month)


def txn(day: str, cents: int) -> Txn:
    return Txn(0, date.fromisoformat(day), "X", cents, None, None)


def test_status_flips_exactly_at_the_p10_and_p50_thresholds():
    # opening 1,000.00, no transactions: base = 100000; p50 savings at 2026-05 = 100000, p10 = 50000
    by = {t: check(goal(t), [], 100000, POINTS, "2026-03") for t in (50000, 50001, 100000, 100001)}
    assert [by[t].status for t in (50000, 50001, 100000, 100001)] == ["ahead", "on_track", "on_track", "behind"]
    assert by[100001].gap_cents == 1 and by[100000].gap_cents == 0 and by[50000].gap_cents == -50000
    assert (by[100000].projected_p50_cents, by[100000].projected_p10_cents) == (100000, 50000)
    assert by[50000].projected_completion == "2026-04" and by[100000].projected_completion == "2026-05"
    assert by[100001].projected_completion is None
    assert status_for(10, 10, 10) == "ahead" and status_for(10, 10, 9) == "on_track" and status_for(10, 9, 9) == "behind"


def test_final_status_from_actual_balance_at_the_target_month():
    txns = [txn("2026-03-15", -5000), txn("2026-04-10", 30000), txn("2026-05-10", 20000)]
    done = check(goal(50000), txns, 100000, [], "2026-05")
    assert (done.status, done.saved_cents, done.gap_cents, done.projected_p50_cents) == ("achieved", 50000, 0, None)
    assert check(goal(50001), txns, 100000, [], "2026-05").status == "missed"
    later = txns + [txn("2026-06-10", -99999)]  # a reset past the target month: judged at the target month
    assert check(goal(50000), later, 100000, [], "2026-06").status == "achieved"


def test_plan_is_a_straight_line_from_start_to_target():
    g = goal(600, "2026-09")
    assert [planned(g, m) for m in ("2026-03", "2026-06", "2026-09", "2026-11")] == [0, 300, 600, 600]


def test_decide_advises_only_on_a_status_or_pattern_change():
    c = check(goal(100001), [], 100000, POINTS, "2026-03")
    sig = {"status": "behind", "actions": ["cancel X"]}
    assert decide(c, sig, None, None)[0] == "advise"
    assert decide(c, sig, {"status": "on_track", "actions": ["cancel X"]}, "2026-02") == (
        "advise", "status changed: on_track -> behind")
    assert decide(c, sig, {"status": "behind", "actions": []}, "2026-02") == ("advise", "patterns changed: +cancel X")
    assert decide(c, sig, sig, "2026-02")[0] == "carry"
    assert decide(check(goal(1), [], 0, [], "2026-05"), sig, sig, "2026-02")[0] == "final"


def test_actionable_mirrors_the_verifier_action_rule():
    rec = {"active": True, "possibly_unused": True, "amount_varies": False, "price_changed": False,
           "prev_amount_cents": None, "amount_cents": -999, "merchant": "UBER ONE"}
    trend = {"category": "dining", "slope_cents_per_month": 100, "latest_month_cents": 500, "prev_3mo_mean_cents": 400}
    assert goal_tracker.actionable([("recurring", rec), ("trend", trend)]) == ["cancel UBER ONE", "reduce_spending dining"]
    assert goal_tracker.actionable([("recurring", rec | {"active": False}),
                                    ("trend", trend | {"latest_month_cents": 400})]) == []
    price_up = rec | {"possibly_unused": False, "price_changed": True, "prev_amount_cents": -899}
    assert goal_tracker.actionable([("recurring", price_up)]) == ["review_price_increase UBER ONE"]
    assert goal_tracker.actionable([("recurring", price_up | {"prev_amount_cents": -1099})]) == []  # price went down


# ---- replay through the API ----

def test_replay_rewinds_the_clock_flips_status_and_advises_only_on_change(api, db, advisor_calls):
    r = api.post("/api/goals", json=GOAL)
    assert r.status_code == 200, r.text
    body = r.json()
    gid, cur = body["progress"]["goal"]["id"], body["progress"]["current"]
    assert as_of(db) == "2026-03"
    assert (cur["month"], cur["status"], cur["decision"]) == ("2026-03", "ahead", "advise")
    assert cur["run_status"] == "running"  # status computed now; the Advisor continues in the background
    assert "still running" in body["progress"]["advance_blocked"]  # as of the response
    assert api.get(f"/api/runs/{body['run_id']}").json()["status"] == "done"
    assert api.get(f"/api/goals/{gid}/progress").json()["next_month"] == "2026-04"

    while True:
        r = api.post(f"/api/goals/{gid}/advance")
        if r.status_code != 200:
            break
    assert r.status_code == 409 and "reached its target month (missed)" in r.json()["detail"]["errors"][0]

    months = api.get(f"/api/goals/{gid}/progress").json()["months"]
    assert [(m["month"], m["status"], m["decision"]) for m in months] == [
        ("2026-03", "ahead", "advise"), ("2026-04", "ahead", "carry"), ("2026-05", "ahead", "advise"),
        ("2026-06", "on_track", "advise"), ("2026-07", "behind", "advise"), ("2026-08", "behind", "carry"),
        ("2026-09", "missed", "final")]
    assert months[2]["reason"] == "patterns changed: +review_price_increase SPOTIFY USA"
    assert months[4]["reason"] == "status changed: on_track -> behind"
    assert [m["advice_from_month"] for m in months] == [
        "2026-03", "2026-03", "2026-05", "2026-06", "2026-07", "2026-07", None]
    assert len(advisor_calls) == 4  # one first draft per advised month, none for carried or final months
    assert all(m["run_status"] == "done" for m in months)
    final_m = months[-1]
    assert final_m["projected_p50_cents"] is None and final_m["gap_cents"] == 1_000_000 - final_m["saved_cents"] > 0
    assert months[-2]["gap_cents"] > 0 and months[0]["gap_cents"] < 0
    assert as_of(db) == "2026-09"

    events = api.get(f"/api/runs/{months[4]['run_id']}").json()
    assert events["kind"] == "goal_check"
    assert [e["node"] for e in events["events"]][:3] == ["analyze", "forecast", "compare_to_plan"]
    assert "behind" in events["events"][2]["summary"] and "short $" in events["events"][2]["summary"]
    carried = api.get(f"/api/runs/{months[5]['run_id']}").json()["events"]
    assert [e["node"] for e in carried] == ["analyze", "forecast", "compare_to_plan"]  # Advisor not called
    assert "Advisor not called" in carried[-1]["summary"]


def test_advice_carried_forward_comes_from_the_earlier_run(db):
    one_rec = ScriptedAdvisor(db, [lambda m, ctx: final(uber_rec(ctx))])  # a verifiable rec; called once only
    conn = connect(db)
    gid, _, _ = goals.create_goal(conn, 1, GoalCreate(**GOAL), defer_advice=False, advisor_llm=one_rec)
    goals.advance(conn, gid, defer_advice=False, advisor_llm=one_rec)
    m03, m04 = goals.progress(conn, gid)["months"]
    assert m04["decision"] == "carry" and m04["advice_from_month"] == "2026-03"
    assert [r["id"] for r in m04["recommendations"]] == [r["id"] for r in m03["recommendations"]]
    assert len(m03["recommendations"]) == 1 and m03["recommendations"][0]["goal_id"] == gid
    conn.close()


# ---- goal-aware Advisor and goal claims ----

def goal_figure(ctx, kind: str) -> dict:
    f = next(f for f in advisor.get_goal(ctx)["figures"] if f["kind"] == kind)
    return {k: f[k] for k in ("kind", "value", "unit", "source_ref", "month", "period")}


def goal_rec(ctx, *, gap_off: int = 0) -> dict:
    rec = uber_rec(ctx)
    pid = pattern_id(ctx, "UBER")
    gap, after = goal_figure(ctx, "goal_gap"), figure(ctx, pid, "goal_gap_after")
    gap["value"] += gap_off
    rec["claims"] += [gap, after]
    rec["body"] += (f" Your goal is ${gap['value'] / 100:,.2f} short by {gap['month']}; cancelling leaves "
                    f"${after['value'] / 100:,.2f}.")
    return rec


def test_goal_claims_are_recomputed_and_a_wrong_gap_is_rejected_then_fixed(db):
    adv = ScriptedAdvisor(db, [call_tool("get_goal"), lambda m, ctx: final(goal_rec(ctx, gap_off=100)),
                               lambda m, ctx: final(goal_rec(ctx))])
    conn = connect(db)
    body = GoalCreate(name="g", target_cents=1_000_000, target_month="2026-09", start_month="2026-07")
    gid, run_id, pending = goals.create_goal(conn, 1, body, defer_advice=False, advisor_llm=adv)
    assert not pending and goals.progress(conn, gid)["current"]["status"] == "behind"

    system, user = adv.calls[0][0]["content"], adv.calls[0][1]["content"]
    assert "The user has a savings goal" in system and f"goal:{gid} (status behind)" in user
    recs = [dict(r) for r in conn.execute("SELECT * FROM recommendations WHERE run_id = ? ORDER BY id", (run_id,))]
    assert [(r["attempt"], r["status"]) for r in recs] == [(1, "rejected"), (2, "accepted")]
    assert {r["prompt_version"] for r in recs} == {"adv-v3+goal-v1"} and {r["goal_id"] for r in recs} == {gid}
    audit = [json.loads(a["checks"]) for a in conn.execute("SELECT checks FROM audit_trail WHERE run_id = ? ORDER BY id",
                                                          (run_id,))]
    bad = next(c for c in audit[0] if c["claim"]["kind"] == "goal_gap")
    assert not bad["ok"] and bad["recomputed"] == bad["claim"]["value"] - 100
    assert all(c["ok"] for c in audit[1]) and {"goal_gap", "goal_gap_after"} <= {c["claim"]["kind"] for c in audit[1]}
    inputs = json.loads(conn.execute("SELECT inputs FROM audit_trail WHERE run_id = ? LIMIT 1", (run_id,)).fetchone()[0])
    assert inputs["goal_id"] == gid
    conn.close()


def test_goal_figures_match_a_hand_recomputation(db):
    conn = connect(db)
    body = GoalCreate(name="g", target_cents=1_000_000, target_month="2026-09", start_month="2026-07")
    _, run_id, _ = goals.create_goal(conn, 1, body, defer_advice=False, advisor_llm=lambda m, **k: final())
    ctx = verifier.Context.load(conn, run_id)
    snap = conn.execute("SELECT * FROM goal_snapshots WHERE run_id = ?", (run_id,)).fetchone()
    figs = {f["kind"]: f["value"] for f in advisor.get_goal(ctx)["figures"]}
    assert figs == {"goal_target": 1_000_000, "goal_saved": 0, "goal_gap": snap["gap_cents"]}
    after = figure(ctx, pattern_id(ctx, "UBER"), "goal_gap_after")
    assert figs["goal_gap"] - after["value"] == 2 * 999  # cancelling a $9.99 charge for 2026-08 and 2026-09
    conn.close()


def test_goal_claim_in_a_main_run_is_rejected(db):
    ctx = None

    def capture(m, c):
        nonlocal ctx
        ctx = c
        return final()
    run_main(db, ScriptedAdvisor(db, [capture]))
    assert ctx.goal is None and "error" in advisor.get_goal(ctx)
    rec = uber_rec(ctx)
    rec["claims"].append({"kind": "goal_gap", "value": 1, "unit": "cents", "source_ref": "goal:1", "month": "2026-12",
                          "period": None})
    rep = verifier.verify(verifier.Draft("L", 1, **{k: rec[k] for k in (
        "action", "target", "title", "body", "trigger_type", "trigger_id", "claims")}), ctx)
    assert rep.verdict == "reject" and any("this run tracks no goal" in r for r in rep.reasons)
    assert "The user has a savings goal" not in advisor.SYSTEMS["adv-v3"]


# ---- validation, 409s, clock ----

def test_create_validation_is_readable(api, db):
    def post(**over):
        return api.post("/api/goals", json=GOAL | over)

    r = post(start_month="2025-11")
    assert r.status_code == 422 and "Not enough history yet, need at least 3 months" in r.json()["detail"]["errors"][0]
    assert post(target_month="2026-03").status_code == 422
    assert "1 to 12 months after" in post(target_month="2027-04").json()["detail"]["errors"][0]
    assert "after the profile's current month" in post(start_month="2026-10").json()["detail"]["errors"][0]
    assert as_of(db) == "2026-09"  # nothing changed
    assert api.post("/api/goals", json=GOAL).status_code == 200
    second = api.post("/api/goals", json=GOAL | {"name": "other"})
    assert second.status_code == 409 and "already has an active goal" in second.json()["detail"]["errors"][0]


def test_create_needs_a_finished_pipeline_run(api):
    csv = "external_id,date,description,amount\n" + "\n".join(
        f"n{i},2026-0{m}-0{d},SHOP,-10.00" for i, (m, d) in enumerate((m, d) for m in range(1, 7) for d in (1, 9)))
    uid = api.post("/api/transactions/upload", files={"file": ("n.csv", csv, "text/csv")}).json()["user_id"]
    r = api.post(f"/api/goals?user_id={uid}", json=GOAL | {"start_month": None, "target_month": "2026-09"})
    assert r.status_code == 422 and "Run the pipeline first" in r.json()["detail"]["errors"][0]


def test_advance_reset_and_delete_return_409_while_a_check_is_running(api, db):
    gid = api.post("/api/goals", json=GOAL).json()["progress"]["goal"]["id"]
    conn = connect(db)
    conn.execute("UPDATE runs SET status = 'running' WHERE goal_id = ?", (gid,))
    conn.commit()
    for r in (api.post(f"/api/goals/{gid}/advance"), api.post(f"/api/goals/{gid}/reset"),
              api.delete(f"/api/goals/{gid}")):
        assert r.status_code == 409 and "still running" in r.json()["detail"]["errors"][0]
    assert "still running" in api.get(f"/api/goals/{gid}/progress").json()["advance_blocked"]
    assert as_of(db) == "2026-03"
    conn.close()


def test_upload_after_a_rewind_keeps_the_clock_and_extends_the_replay(api, db):
    """While a goal is active it owns the clock: an upload with newer months does not move as_of (otherwise the
    replay would silently jump past months it has not checked); the new months become replayable by advance.
    Deleting the goal restores the clock to the newest data month."""
    gid = api.post("/api/goals", json=GOAL | {"start_month": "2026-07", "target_month": "2026-12"}).json()[
        "progress"]["goal"]["id"]
    api.post(f"/api/goals/{gid}/advance")
    api.post(f"/api/goals/{gid}/advance")
    blocked = api.post(f"/api/goals/{gid}/advance")
    assert blocked.status_code == 409 and "No transactions after 2026-09" in blocked.json()["detail"]["errors"][0]

    csv = ("external_id,date,description,amount\nnew1,2026-10-01,ACME PAYROLL,3000.00\n"
           "new2,2026-10-02,ELM ST PROPERTY MGMT,-3500.00\n")
    up = api.post("/api/transactions/upload", files={"file": ("oct.csv", csv, "text/csv")}, data={"user_id": "1"})
    assert up.status_code == 200 and up.json()["count"] == 2
    assert as_of(db) == "2026-09"  # the goal holds the clock
    progress = api.get(f"/api/goals/{gid}/progress").json()
    assert progress["latest_data_month"] == "2026-10" and progress["next_month"] == "2026-10"
    r = api.post(f"/api/goals/{gid}/advance")
    assert r.status_code == 200 and r.json()["progress"]["current"]["month"] == "2026-10"

    deleted = api.delete(f"/api/goals/{gid}").json()
    assert deleted["as_of_month"] == "2026-10" == as_of(db)  # restored to the newest data month, not 2026-09


def test_reset_to_latest_jumps_the_clock_and_checks_there(api, db):
    gid = api.post("/api/goals", json=GOAL).json()["progress"]["goal"]["id"]
    r = api.post(f"/api/goals/{gid}/reset")
    assert r.status_code == 200 and r.json()["run_id"]
    cur = r.json()["progress"]["current"]
    assert (cur["month"], cur["status"], cur["decision"]) == ("2026-09", "missed", "final")
    assert as_of(db) == "2026-09"
    assert [m["month"] for m in r.json()["progress"]["months"]] == ["2026-03", "2026-09"]  # skipped months unchecked
    again = api.post(f"/api/goals/{gid}/reset")
    assert again.status_code == 409 and "Already at the latest data month" in again.json()["detail"]["errors"][0]


def test_delete_goal_removes_its_checks_and_restores_the_clock(api, db):
    conn = connect(db)
    before_main = conn.execute("SELECT COUNT(*) FROM runs WHERE goal_id IS NULL").fetchone()[0]
    gid = api.post("/api/goals", json=GOAL).json()["progress"]["goal"]["id"]
    api.post(f"/api/goals/{gid}/advance")
    api.post(f"/api/goals/{gid}/advance")
    assert as_of(db) == "2026-05"

    r = api.delete(f"/api/goals/{gid}")
    assert r.status_code == 200
    rows = r.json()["deleted_rows"]
    assert (rows["goals"], rows["goal_snapshots"], rows["runs"]) == (1, 3, 3)
    assert rows["agent_events"] > 0 and rows["patterns"] > 0 and rows["forecasts"] == 3
    assert r.json()["as_of_month"] == "2026-09" == as_of(db)
    for table in ("goals", "goal_snapshots"):
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM runs WHERE kind = 'goal_check'").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM runs WHERE goal_id IS NULL").fetchone()[0] == before_main
    assert api.get(f"/api/goals/{gid}/progress").status_code == 404
    assert api.get("/api/goals").json()["items"] == []
    assert api.delete(f"/api/goals/{gid}").status_code == 404
    assert api.post("/api/goals", json=GOAL).status_code == 200  # a new goal can start
    conn.close()


OLD_GOAL_DDL = """
        CREATE TABLE goals (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, name TEXT NOT NULL,
            target_cents INTEGER NOT NULL, target_date TEXT NOT NULL, monthly_contribution_cents INTEGER NOT NULL,
            start_month TEXT NOT NULL, created_at TEXT);
        CREATE TABLE goal_snapshots (id INTEGER PRIMARY KEY, goal_id INTEGER NOT NULL REFERENCES goals(id),
            run_id TEXT, month TEXT NOT NULL, planned_cents INTEGER NOT NULL, actual_cents INTEGER NOT NULL,
            projected_completion TEXT, on_track INTEGER NOT NULL, forecast_id INTEGER);"""


def test_old_placeholder_goal_tables_are_rebuilt_when_empty(tmp_path):
    from backend.db import init_db
    conn = connect(tmp_path / "old.db")
    conn.executescript(OLD_GOAL_DDL)
    init_db(conn)
    assert {"target_month", "restore_as_of_month"} <= {r["name"] for r in conn.execute("PRAGMA table_info(goals)")}
    assert {"status", "decision", "reason"} <= {r["name"] for r in conn.execute("PRAGMA table_info(goal_snapshots)")}
    init_db(conn)  # idempotent

    old = connect(tmp_path / "used.db")
    old.executescript(OLD_GOAL_DDL + "INSERT INTO goals (user_id, name, target_cents, target_date, "
                      "monthly_contribution_cents, start_month) VALUES (1, 'g', 1, '2027-01-01', 0, '2026-09');")
    with pytest.raises(RuntimeError, match="migrate them by hand"):
        init_db(old)
