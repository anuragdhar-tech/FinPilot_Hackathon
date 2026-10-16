"""What-if: scenario flows move the balance by exactly the amount, the verdict flips at the computed threshold,
parsing is validated (one retry), and the explanation's numbers are checked (rewrite once, then template)."""
import json

import pytest
from fastapi.testclient import TestClient

import backend.graph as graph
from backend.agents import whatif
from backend.agents.forecaster import Flow, ForecastInputs
from backend.models import Scenario
from tests.ledger_fixture import build_ledger, completion, final, run_main

BASE = ForecastInputs("2026-09", 100000, (
    Flow("income", "income", "income", "inflows", (50000,) * 12, 10000),
    Flow("rent", "recurring", "housing", "pattern:1", (-45000,) * 12, 0),
))


def one_off(cents: int, month: str) -> Scenario:
    return Scenario(items=[{"kind": "one_off", "label": "trip", "amount_cents": cents, "start_month": month}])


def test_one_off_moves_balance_by_exactly_the_amount_from_its_month():
    sim = whatif.simulate(BASE, one_off(-200000, "2026-11"), 6)
    d50 = [p["balance_p50"] - b["balance_p50"] for p, b in zip(sim.projected, sim.baseline)]
    d10 = [p["balance_p10"] - b["balance_p10"] for p, b in zip(sim.projected, sim.baseline)]
    assert d50 == d10 == [0] + [-200000] * 5
    assert sim.end_delta_cents == -200000


def test_recurring_change_accumulates_for_its_months():
    sc = Scenario(items=[{"kind": "recurring_change", "label": "gym", "amount_cents": -4000,
                          "start_month": "2026-10", "months": 3}])
    sim = whatif.simulate(BASE, sc, 6)
    assert [p["balance_p50"] - b["balance_p50"] for p, b in zip(sim.projected, sim.baseline)] == [
        -4000, -8000, -12000, -12000, -12000, -12000]


def test_verdict_flips_at_the_computed_thresholds():
    month = "2026-10"
    base = whatif.simulate(BASE, one_off(-1, month), 6).baseline
    x10 = min(p["balance_p10"] for p in base)  # all months are at or after the purchase month
    x50 = min(p["balance_p50"] for p in base)
    assert whatif.simulate(BASE, one_off(-x10, month), 6).verdict == "affordable"
    assert whatif.simulate(BASE, one_off(-(x10 + 1), month), 6).verdict == "tight"
    assert whatif.simulate(BASE, one_off(-x50, month), 6).verdict == "tight"
    assert whatif.simulate(BASE, one_off(-(x50 + 1), month), 6).verdict == "not_affordable"
    sim = whatif.simulate(BASE, one_off(-(x50 + 1), month), 6)
    assert sim.lowest_p50 == {"month": month, "cents": -1}


def test_validation_and_horizon():
    assert whatif.validate(one_off(-1, "2026-09"), "2026-09") == ["item 1: start_month 2026-09 must be between 2026-10 and 2027-09"]
    assert whatif.validate(one_off(0, "2026-10"), "2026-09") == ["item 1: amount_cents must not be 0"]
    assert whatif.horizon_for(one_off(-1, "2027-05"), "2026-09", 6) == 8


def fake_parser(*outs):
    outs = list(outs)
    return lambda messages, **kw: completion(json.dumps(outs.pop(0)))


def item(**kw):
    return {"kind": "one_off", "label": "trip", "amount_cents": -200000, "start_month": "2026-10", "months": None} | kw


def test_parse_retries_once_with_validation_errors():
    calls = []
    llm = fake_parser({"supported": True, "reason": "", "items": [item(start_month="2026-08")]},
                      {"supported": True, "reason": "", "items": [item()]})
    res = whatif.parse("Can I afford a $2,000 trip next month?", "2026-09", lambda m, **kw: calls.append(m) or llm(m))
    assert res.attempts == 2 and res.scenario.items[0].amount_cents == -200000
    assert "must be between 2026-10 and 2027-09" in calls[1][-1]["content"]
    with pytest.raises(whatif.ScenarioError, match="not a what-if"):
        whatif.parse("hello", "2026-09", fake_parser({"supported": False, "reason": "greeting", "items": []}))
    bad = {"supported": True, "reason": "", "items": [item(amount_cents=0)]}
    with pytest.raises(whatif.ScenarioError, match="must not be 0"):
        whatif.parse("x", "2026-09", fake_parser(bad, bad))


@pytest.fixture
def api(tmp_path, monkeypatch):
    path = tmp_path / "app.db"
    build_ledger(path)
    run_main(path, lambda m, **kw: final())
    monkeypatch.setenv("FINPILOT_DB", str(path))
    from backend.main import app
    with TestClient(app) as c:
        yield c


def explainer(*texts):
    texts, calls = list(texts), []

    def llm(messages, **kw):
        calls.append(messages)
        return completion(texts.pop(0))
    llm.calls = calls
    return llm


def test_simulate_api_text_path_and_explanation_rewrite(api, monkeypatch):
    monkeypatch.setattr(graph, "default_parser_llm", fake_parser({"supported": True, "reason": "", "items": [item()]}))
    ex = explainer("Affordable. It costs $2,500.", "Affordable: the trip of $2,000.00 in 2026-10 keeps you above $0.")
    monkeypatch.setattr(graph, "default_explainer_llm", ex)
    fc = api.get("/api/forecast").json()
    r = api.post("/api/simulate", json={"text": "Can I afford a $2,000 trip next month?"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["baseline"] == fc["points"] and body["baseline_forecast_id"] == fc["id"]
    assert [p["balance_p50"] - b["balance_p50"] for p, b in zip(body["projected"], body["baseline"])] == [-200000] * 6
    assert body["verdict"] == "affordable" and body["explanation_source"] == "llm"
    assert body["number_check"] == {"passed": True, "issues": ["attempt 1: '$2,500' matches no fact"]}
    assert "'$2,500' matches no fact" in ex.calls[1][-1]["content"]
    run = api.get(f"/api/runs/{body['run_id']}").json()
    assert run["kind"] == "whatif" and run["status"] == "done"
    assert [(e["node"], e["status"]) for e in run["events"]] == [
        ("parse", "ok"), ("simulate", "ok"), ("explain", "ok"), ("number_check", "rejected"), ("explain", "ok"),
        ("number_check", "ok")]


def test_simulate_api_template_after_two_failures_and_structured_scenario(api, monkeypatch):
    monkeypatch.setattr(graph, "default_explainer_llm", explainer("Up 12%.", "Fine by 2027-12."))
    sc = {"items": [item(amount_cents=-5000000, months=None, start_month="2026-11")]}
    body = api.post("/api/simulate", json={"scenario": sc, "horizon_months": 3}).json()
    assert body["explanation_source"] == "template" and body["number_check"]["passed"] is False
    assert body["number_check"]["issues"] == ["attempt 1: '12%' matches no fact", "attempt 2: '2027-12' matches no fact"]
    assert body["verdict"] == "not_affordable" and body["explanation"].startswith("Scenario (trip: $50,000.00 out")
    assert len(body["projected"]) == 3


def test_template_numbers_are_all_facts():
    sc = one_off(-200000, "2026-11")
    sim = whatif.simulate(BASE, sc, 6)
    assert whatif.check(whatif.template(sc, sim), whatif.facts(sc, sim), "2026-09") == []


def test_simulate_api_errors(api, monkeypatch):
    monkeypatch.setattr(graph, "default_parser_llm", fake_parser({"supported": False, "reason": "no", "items": []}))
    r = api.post("/api/simulate", json={"text": "what is the weather"})
    assert r.status_code == 422 and "not a what-if" in r.json()["detail"]["errors"][0]
    r = api.post("/api/simulate", json={"scenario": {"items": [item(start_month="2030-01")]}})
    assert r.status_code == 422
    assert api.post("/api/simulate?user_id=99", json={"text": "x"}).status_code == 404
