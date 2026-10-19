"""The advise <-> verify loop with a scripted Advisor: a bad first draft is rejected with structured feedback and
revised; a draft that never passes is dropped after 3 attempts; a withdrawn redraft ends the lineage. All drafts
are stored with one lineage_id, each with its audit row, and the API serves accepted ones plus the full audit."""
import json

import pytest
from fastapi.testclient import TestClient

from backend.agents import verifier
from backend.db import connect
from tests.ledger_fixture import (ScriptedAdvisor, build_ledger, call_tool, figure, final, pattern_id, run_main,
                                  uber_rec)


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "app.db"
    build_ledger(path)
    monkeypatch.setenv("FINPILOT_DB", str(path))
    return path


def rows(path, run_id, sql, *args):
    conn = connect(path)
    out = [dict(r) for r in conn.execute(sql, (run_id, *args))]
    conn.close()
    return out


def test_bad_first_draft_is_rejected_then_revised_and_accepted(db):
    adv = ScriptedAdvisor(db, [
        call_tool("list_patterns", type="recurring"),
        lambda m, ctx: call_tool("get_pattern", pattern_id=pattern_id(ctx, "UBER"))(m, ctx),
        lambda m, ctx: final(uber_rec(ctx, cost=1299, extra_text=" That is $150 a year.")),  # wrong + unclaimed
        lambda m, ctx: final(uber_rec(ctx)),
    ])
    run_id = run_main(db, adv)

    recs = rows(db, run_id, "SELECT * FROM recommendations WHERE run_id = ? ORDER BY id")
    assert [(r["attempt"], r["status"]) for r in recs] == [(1, "rejected"), (2, "accepted")]
    assert recs[0]["lineage_id"] == recs[1]["lineage_id"]
    audit = rows(db, run_id, "SELECT * FROM audit_trail WHERE run_id = ? ORDER BY id")
    assert [a["verifier_verdict"] for a in audit] == ["reject", "pass"]
    checks = json.loads(audit[0]["checks"])
    assert checks[0]["recomputed"] == 999 and not checks[0]["ok"]
    assert not next(r for r in json.loads(audit[0]["rule_checks"]) if r["rule"] == "numbers")["ok"]

    # the redraft prompt carries the structured feedback, including the recomputed value
    redraft_prompt = adv.calls[-1][-1]["content"]
    assert '"recomputed": 999' in redraft_prompt and "'$150'" in redraft_prompt

    events = rows(db, run_id, "SELECT node, attempt, status, summary FROM agent_events WHERE run_id = ? ORDER BY seq")
    nodes = [(e["node"], e["attempt"], e["status"]) for e in events]
    assert nodes[nodes.index(("advise", 1, "ok")) - 2:] == [
        ("advise.tool", 1, "ok"), ("advise.tool", 1, "ok"), ("advise", 1, "ok"), ("verify", 1, "rejected"),
        ("advise", 2, "ok"), ("verify", 2, "ok"), ("persist", 1, "ok")]
    assert "1 of 1 recommendation(s) accepted; 1 draft(s) rejected" in events[-1]["summary"]

    with TestClient(__import__("backend.main", fromlist=["app"]).app) as api:
        items = api.get("/api/recommendations").json()
        assert items["run_id"] == run_id and len(items["items"]) == 1
        rec = items["items"][0]
        assert (rec["action"], rec["rank"], rec["impact_annual_cents"], rec["attempts"]) == ("cancel", 1, 11988, 2)
        audit = api.get(f"/api/recommendations/{rec['id']}/audit").json()
        assert [(a["attempt"], a["status"], a["verdict"]) for a in audit["attempts"]] == [
            (1, "rejected", "reject"), (2, "accepted", "pass")]
        assert audit["trigger"]["title"].startswith("UBER*ONE MEMBERSHIP") and len(audit["trigger"]["evidence_txn_ids"]) == 12
        assert audit["inputs"]["forecast_id"] is not None and audit["inputs"]["as_of_month"] == "2026-09"
        assert any("recomputed 999" in r for r in audit["attempts"][0]["reasons"])
        assert api.get("/api/recommendations/9999/audit").status_code == 404


def test_draft_that_never_passes_is_dropped_after_three_attempts(db):
    bad = lambda m, ctx: final(uber_rec(ctx, cost=1299))  # noqa: E731
    run_id = run_main(db, ScriptedAdvisor(db, [bad, bad, bad]))
    recs = rows(db, run_id, "SELECT attempt, status FROM recommendations WHERE run_id = ? ORDER BY id")
    assert [(r["attempt"], r["status"]) for r in recs] == [(1, "rejected"), (2, "rejected"), (3, "dropped")]
    assert len(rows(db, run_id, "SELECT * FROM audit_trail WHERE run_id = ?")) == 3
    persist = rows(db, run_id, "SELECT summary FROM agent_events WHERE run_id = ? AND node = 'persist'")[0]
    assert "0 of 1 recommendation(s) accepted" in persist["summary"] and "1 lineage(s) dropped" in persist["summary"]
    with TestClient(__import__("backend.main", fromlist=["app"]).app) as api:
        assert api.get("/api/recommendations").json()["items"] == []


def test_unsupported_action_withdrawn_on_redraft(db):
    def cancel_dashpass(m, ctx):
        pid = pattern_id(ctx, "DASHPASS")
        return final({"action": "cancel", "target": "DashPass", "trigger_type": "pattern", "trigger_id": str(pid),
                      "title": "Cancel DashPass", "body": "You may not need it.",
                      "claims": [figure(ctx, pid, "recurring_cost", period="month")]})

    adv = ScriptedAdvisor(db, [cancel_dashpass, lambda m, ctx: final()])
    run_id = run_main(db, adv)
    recs = rows(db, run_id, "SELECT status FROM recommendations WHERE run_id = ?")
    assert [r["status"] for r in recs] == ["rejected"]  # one draft; the lineage ended by withdrawal
    assert "possibly_unused=False" in adv.calls[-1][-1]["content"]
    events = rows(db, run_id, "SELECT node, summary FROM agent_events WHERE run_id = ? AND node = 'advise'")
    assert "withdrawn by the Advisor" in events[-1]["summary"]


def test_ranked_by_recomputed_impact_and_one_per_trigger_action(db):
    def three(m, ctx):
        sp = pattern_id(ctx, "SPOTIFY")
        price = {"action": "review_price_increase", "target": "Spotify", "trigger_type": "pattern",
                 "trigger_id": str(sp), "title": "Spotify went up $1/month", "body": "Check the plan.",
                 "claims": [figure(ctx, sp, "price_change", period="month")]}
        return final(price, uber_rec(ctx), uber_rec(ctx))  # a repeated (trigger, action) is ignored

    run_id = run_main(db, ScriptedAdvisor(db, [three]))
    assert len(rows(db, run_id, "SELECT * FROM recommendations WHERE run_id = ?")) == 2
    with TestClient(__import__("backend.main", fromlist=["app"]).app) as api:
        items = api.get("/api/recommendations").json()["items"]
    assert [(i["rank"], i["action"], i["impact_annual_cents"]) for i in items] == [
        (1, "cancel", 11988), (2, "review_price_increase", 1200)]


def test_old_db_gets_new_columns(tmp_path):
    from backend.db import ADDED_COLUMNS, SCHEMA_PATH, init_db
    lines, table = [], None
    for line in SCHEMA_PATH.read_text().splitlines():  # the schema without the added columns, table by table
        if line.startswith("CREATE TABLE IF NOT EXISTS "):
            table = line.split()[5]
        if not (line.split() and line.split()[0] in {n for n, _ in ADDED_COLUMNS.get(table, [])}):
            lines.append(line)
    old = "\n".join(lines)
    conn = connect(tmp_path / "old.db")
    conn.executescript(old)
    assert "action" not in {r["name"] for r in conn.execute("PRAGMA table_info(recommendations)")}
    init_db(conn)
    assert {"action", "target", "impact_annual_cents"} <= {r["name"] for r in conn.execute("PRAGMA table_info(recommendations)")}
    assert "rule_checks" in {r["name"] for r in conn.execute("PRAGMA table_info(audit_trail)")}
    assert "goal_id" in {r["name"] for r in conn.execute("PRAGMA table_info(runs)")}
    init_db(conn)  # idempotent
    assert verifier.MAX_ATTEMPTS == 3


def test_v2_tool_text_has_no_numbers_and_v1_is_still_selectable(db):
    from backend.agents import advisor
    from backend.agents.verifier import extract_numbers

    seen = {}

    def capture(m, ctx):
        seen["ctx"] = ctx
        return final()
    run_main(db, ScriptedAdvisor(db, [capture]))
    ctx = seen["ctx"]
    for pid in ctx.patterns:
        out = advisor.get_pattern(ctx, pid, "adv-v2")
        if "error" in out:
            continue
        words = [v for k, v in out.items() if isinstance(v, str)] + [f["meaning"] for f in out["figures"]]
        assert not [n for w in words for n in extract_numbers(w)], words
        assert "title" in advisor.get_pattern(ctx, pid, "adv-v1")  # v1 kept the Analyzer title (with numbers)
    listed = json.dumps(advisor.list_patterns(ctx, None, "adv-v2"))
    assert not [n for n in extract_numbers(listed) if n.kind != "month"]  # only as_of_month itself
    assert "nearest whole dollar" in advisor.SYSTEMS["adv-v2"] and "nearest whole dollar" not in advisor.SYSTEMS["adv-v1"]


def test_v1_run_uses_v1_prompt_and_single_numbers_feedback(db):
    adv = ScriptedAdvisor(db, [lambda m, ctx: final(uber_rec(ctx, extra_text=" Up 12%.")),
                               lambda m, ctx: final(uber_rec(ctx))])
    run_id = run_main(db, adv, advisor_version="adv-v1")
    assert adv.calls[0][0]["content"].startswith("You are FinPilot") and "you may round dollars" in adv.calls[0][0]["content"]
    assert '"check": "numbers", "note"' in adv.calls[-1][-1]["content"]
    assert {r["prompt_version"] for r in rows(db, run_id, "SELECT prompt_version FROM recommendations WHERE run_id = ?")} == {"adv-v1"}


def test_v3_prompt_says_reduce_gain_comes_from_the_rise_stopping():
    from backend.agents import advisor
    v3, v2 = advisor.SYSTEMS["adv-v3"], advisor.SYSTEMS["adv-v2"]
    assert advisor.PROMPT_VERSION == "adv-v3"
    assert "stopping the rise is worth (the category stops rising" in v3 and "never as savings from cutting" in v3
    added = v3.split("(excess_spend > 0).", 1)[1].split("\n\nEach recommendation", 1)[0]
    assert "stops rising" not in v2 and v3.replace(added, "", 1) == v2  # v3 = v2 + the reduce_spending lines only
    assert "stops rising" in advisor.MEANINGS["adv-v3"]["delta_reduce"] and "delta_reduce" not in advisor.MEANINGS["adv-v2"]
