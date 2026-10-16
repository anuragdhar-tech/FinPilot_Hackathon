"""eval/advice.py scoring on the fixture ledger: a scripted bad-then-good run is counted as a first-draft
failure, the planted unused membership is matched by evidence, the used control passes."""
from backend.db import connect
from eval.advice import score_loop, to_md, whatif_sanity
from tests.ledger_fixture import ScriptedAdvisor, build_ledger, final, run_main, uber_rec


def test_score_loop_counts_first_draft_failure_and_planted_match(tmp_path, monkeypatch):
    path = tmp_path / "e.db"
    uid = build_ledger(path)
    adv = ScriptedAdvisor(path, [lambda m, ctx: final(uber_rec(ctx, cost=1299)), lambda m, ctx: final(uber_rec(ctx))])
    run_id = run_main(path, adv)
    conn = connect(path)
    ext = lambda like: [r[0] for r in conn.execute(  # noqa: E731
        "SELECT external_id FROM transactions WHERE raw_description LIKE ? ORDER BY posted_date", (like,))]
    rec = {"price_change": None, "membership_of": ["x"]}
    truth = {"personas": {"p": {"trends": [], "recurring": [
        rec | {"merchant": "Uber One", "unused": True, "txn_ids": ext("UBER *ONE%")},
        rec | {"merchant": "DashPass", "unused": False, "txn_ids": ext("DOORDASH DASHPASS%")}]}}}
    monkeypatch.setenv("ADVISOR_MODEL", "openai/gpt-4o")
    res = score_loop(conn, {"p": [run_id]}, truth)
    v, d = res["verifier"], res["details"]
    assert (v["lineages"], v["drafts"], v["rejected"], v["first_draft_fail_rate"], v["accepted"]) == (1, 2, 1, 1.0, 1)
    assert v["by_check"] == {"claim:recurring_cost": 1}
    assert d["unused_recall"] == 1.0 and d["controls_passed"] == "1/1" and d["cancel_precision"] == 1.0
    assert d["first_draft_claims_ok"] == 1 and d["first_draft_claims"] == 2
    assert d["accepted_grounded_number_rate"] == 1.0
    assert d["failures"][0]["outcome"] == "accepted"
    sanity = whatif_sanity(conn, {"p": uid})
    assert all(c["ok"] for c in sanity), sanity
    md = to_md({"summary": {"run_at": "t", "git_sha": "s", "dataset": "d", "seed": 1, "models": {"advisor": "m"},
                            **{k: res[k] for k in ("verifier", "grounded_number_rate")}},
                "details": d | {"repeats": 1, "prompt_version": "adv-v1", "whatif_sanity": sanity}})
    assert "First-draft failure rate | 100.0%" in md
