"""eval/goals.py on the fixture ledger: realized savings come from the CSV alone, run_stats counts goal claims and
first-draft failures from the stored drafts, the gap-after consistency check holds, and a goal run leaves no rows."""
import json

from backend import goals
from backend.agents import verifier
from backend.db import connect
from backend.models import GoalCreate
from eval.goals import gap_after_consistency, no_advisor, realized_savings, run_stats
from tests.ledger_fixture import ScriptedAdvisor, build_ledger, final
from tests.test_goals import add_rent, goal_rec


def test_realized_savings_reads_only_the_csv(tmp_path):
    (tmp_path / "personas.json").write_text(json.dumps({"p": {"csv": "p.csv", "opening_balance_cents": 10000}}))
    (tmp_path / "p.csv").write_text("external_id,date,description,amount\na,2026-03-31,X,-10.00\n"
                                    "b,2026-04-01,PAY,100.25\nc,2026-05-31,X,-0.25\nd,2026-06-01,X,-999.00\n")
    assert realized_savings(tmp_path, "p", "2026-03", "2026-05") == 10000  # +100.25 - 0.25; June is after


def test_run_stats_and_consistency_on_a_goal_run(tmp_path):
    path = tmp_path / "e.db"
    add_rent(path, uid := build_ledger(path))
    conn = connect(path)
    adv = ScriptedAdvisor(path, [lambda m, ctx: final(goal_rec(ctx, gap_off=100)), lambda m, ctx: final(goal_rec(ctx))])
    body = GoalCreate(name="g", target_cents=1_000_000, target_month="2026-09", start_month="2026-07")
    gid, run_id, _ = goals.create_goal(conn, uid, body, defer_advice=False, advisor_llm=adv)
    st = run_stats(conn, run_id)
    assert (st["lineages"], st["first_rejected"]) == (1, 1)
    assert st["first_failed_checks"] == {"claim:goal_gap": 1}
    assert (st["goal_claims_first"], st["goal_claims_first_ok"]) == (2, 1)
    assert st["accepted"][0]["goal_kinds"] == ["goal_gap", "goal_gap_after"]
    checks = gap_after_consistency(verifier.Context.load(conn, run_id))
    assert checks and all(c["ok"] for c in checks)
    goals.delete_goal(conn, gid)

    gid, _, _ = goals.create_goal(conn, uid, body, defer_advice=False, advisor_llm=no_advisor)
    goals.delete_goal(conn, gid)
    assert conn.execute("SELECT COUNT(*) FROM runs WHERE kind = 'goal_check'").fetchone()[0] == 0
    assert conn.execute("SELECT as_of_month FROM users WHERE id = ?", (uid,)).fetchone()[0] == "2026-09"
    conn.close()
