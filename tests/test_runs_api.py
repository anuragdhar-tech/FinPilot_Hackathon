"""Upload -> run (ingest -> categorize) -> transactions, through the API with the LLM mocked."""
import json

import pytest

import backend.graph as graph
import backend.main as main
from tests.test_categorizer import FakeLLM

CSV = """external_id,date,description,amount
a1,2026-01-03,NETFLIX.COM,-15.49
a2,2026-01-04,CHECKCARD 0104 UBER *TRIP JW25MAZ4AA,-12.10
a3,2026-02-03,NETFLIX.COM,-15.49
a4,2026-02-09,DOORDASH*SWEETGREEN,-23.40
"""


@pytest.fixture
def llm(monkeypatch):
    fake = FakeLLM()
    monkeypatch.setattr(graph, "default_categorizer_llm", fake)
    monkeypatch.setenv("CATEGORIZER_MODEL", "openai/gpt-4o-mini")
    return fake


def _upload_and_run(api):
    batch = api.post("/api/transactions/upload", files={"file": ("u.csv", CSV, "text/csv")}).json()
    run_id = api.post("/api/runs", json={"batch_id": batch["batch_id"]}).json()["run_id"]
    return batch, run_id  # TestClient runs background tasks before returning


def test_run_categorizes_and_logs_ordered_events(api, llm):
    _, run_id = _upload_and_run(api)
    run = api.get(f"/api/runs/{run_id}").json()
    assert run["status"] == "done" and run["kind"] == "main"
    nodes = [e["node"] for e in run["events"]]
    # cat-v2: representatives batch, then the NETFLIX.COM confirmation look in its own batch
    # 2 months of data: the forecast says insufficient data and the Advisor is skipped
    assert nodes == ["ingest", "categorize.batch", "categorize.batch", "categorize", "analyze", "forecast",
                     "persist"]
    assert [e["seq"] for e in run["events"]] == [1, 2, 3, 4, 5, 6, 7]
    assert run["events"][1]["summary"].startswith("representatives: 3 items")
    assert "echo_mismatch=0/3" in run["events"][1]["summary"]
    assert run["events"][2]["summary"].startswith("confirmations: 1 items")
    last = run["events"][3]["summary"]
    assert "cat-v2: 4/4 categorized (4 by LLM, 0 from cache" in last and "confirm_agreed=1" in last
    assert run["events"][1]["prompt_tokens"] == 100


def test_transactions_carry_categorization_and_filter(api, llm):
    _upload_and_run(api)
    items = api.get("/api/transactions").json()["items"]
    assert len(items) == 4
    row = next(i for i in items if i["external_id"] == "a4")
    assert (row["category"], row["model"], row["prompt_version"], row["from_cache"]) == \
        ("food_delivery", "openai/gpt-4o-mini", "cat-v2", False)
    assert row["amount_cents"] == -2340 and row["rationale"] == "rule"
    assert {i["external_id"] for i in api.get("/api/transactions?month=2026-01").json()["items"]} == {"a1", "a2"}
    ent = api.get("/api/transactions?category=entertainment").json()["items"]
    assert [i["from_cache"] for i in sorted(ent, key=lambda i: i["date"])] == [False, False]  # both looked at
    assert api.get("/api/transactions?user_id=99").status_code == 404
    assert api.get("/api/transactions?category=subscriptions").status_code == 422


def test_uncategorized_rows_visible_and_event_flags_error(api, monkeypatch):
    monkeypatch.setattr(graph, "default_categorizer_llm", FakeLLM(script=[lambda r: "{}"] * 99))
    monkeypatch.setenv("CATEGORIZER_MODEL", "openai/gpt-4o-mini")
    _, run_id = _upload_and_run(api)
    run = api.get(f"/api/runs/{run_id}").json()
    cat = next(e for e in run["events"] if e["node"] == "categorize")
    assert run["status"] == "done" and cat["status"] == "error"
    assert "4 left uncategorized" in cat["summary"]
    assert "4 uncategorized left out of trends" in next(e for e in run["events"] if e["node"] == "analyze")["summary"]
    items = api.get("/api/transactions").json()["items"]
    assert len(items) == 4 and all(i["category"] is None for i in items)


def test_run_crash_marks_failed(api, monkeypatch):
    def no_model(agent):
        raise RuntimeError("CATEGORIZER_MODEL is empty or missing in .env")
    monkeypatch.setattr(graph, "get_model", no_model)  # node itself crashes
    _, run_id = _upload_and_run(api)
    run = api.get(f"/api/runs/{run_id}").json()
    assert run["status"] == "failed" and "CATEGORIZER_MODEL" in run["error"]
    assert run["events"][-1]["node"] == "coordinator"


def test_run_unknown_batch_404(api):
    assert api.post("/api/runs", json={"batch_id": "nope"}).status_code == 404


def test_eval_latest_serves_script_output(api, tmp_path, monkeypatch):
    monkeypatch.setattr(main, "EVAL_RESULTS", tmp_path)
    assert api.get("/api/eval/latest").status_code == 404
    summary = {"run_at": "2026-10-15T00:00:00Z", "git_sha": "abc", "dataset": "synthetic", "seed": 42,
               "models": {"categorizer": "openai/gpt-4o-mini"},
               "categorization": {"accuracy": 0.9, "macro_f1": 0.8, "top_confusions": [], "lenient_accuracy": 0.92}}
    (tmp_path / "categorization.json").write_text(json.dumps({"summary": summary}))
    body = api.get("/api/eval/latest").json()
    assert body["categorization"]["lenient_accuracy"] == 0.92


def test_run_list_newest_first_and_per_user(api, llm):
    batch, first = _upload_and_run(api)
    second = api.post("/api/runs", json={"batch_id": batch["batch_id"]}).json()["run_id"]
    other = api.post("/api/transactions/upload", files={"file": ("o.csv", CSV, "text/csv")}).json()
    third = api.post("/api/runs", json={"batch_id": other["batch_id"]}).json()["run_id"]

    mine = api.get(f"/api/runs?user_id={batch['user_id']}").json()["items"]
    assert [r["run_id"] for r in mine] == [second, first]  # same-second start: insertion order breaks the tie
    assert mine[0]["status"] == "done" and mine[0]["batch_id"] == batch["batch_id"]
    assert mine[0]["event_count"] == len(api.get(f"/api/runs/{second}").json()["events"])
    assert [r["run_id"] for r in api.get("/api/runs").json()["items"]] == [third]  # default: active user
    assert api.get("/api/runs?user_id=99").status_code == 404


def _write_result(path, prompt_version, accuracy):
    summary = {"run_at": "2026-10-15T00:00:00Z", "git_sha": "abc", "dataset": "synthetic", "seed": 42,
               "models": {"categorizer": "openai/gpt-4o-mini"},
               "categorization": {"accuracy": accuracy, "macro_f1": 0.8, "top_confusions": []}}
    details = {"prompt_version": prompt_version,
               "usage": {"prompt_tokens": 1000, "completion_tokens": 500, "estimated_cost": {"usd": 0.0012}}}
    path.write_text(json.dumps({"summary": summary, "details": details}))


def test_eval_latest_usage_and_version(api, tmp_path, monkeypatch):
    monkeypatch.setattr(main, "EVAL_RESULTS", tmp_path)
    _write_result(tmp_path / "categorization.json", "cat-v2", 0.95)
    _write_result(tmp_path / "categorization_cat-v1.json", "cat-v1", 0.90)
    cat = api.get("/api/eval/latest").json()["categorization"]
    assert (cat["prompt_version"], cat["prompt_tokens"], cat["completion_tokens"], cat["cost_usd"]) == \
        ("cat-v2", 1000, 500, 0.0012)
    old = api.get("/api/eval/latest?version=cat-v1").json()["categorization"]
    assert (old["prompt_version"], old["accuracy"]) == ("cat-v1", 0.90)
    assert api.get("/api/eval/latest?version=cat-v9").status_code == 404
    assert api.get("/api/eval/latest?version=../secrets").status_code == 422


CSV_3MO = CSV + """a5,2026-03-03,NETFLIX.COM,-15.49
a6,2026-03-27,DOORDASH*SWEETGREEN,-23.40
"""


def test_patterns_and_forecast_endpoints_after_a_run(api, llm):
    assert api.get("/api/forecast").status_code == 404  # no data yet
    batch = api.post("/api/transactions/upload", files={"file": ("u.csv", CSV_3MO, "text/csv")}).json()
    run_id = api.post("/api/runs", json={"batch_id": batch["batch_id"]}).json()["run_id"]
    pats = api.get("/api/patterns").json()
    assert pats["run_id"] == run_id and pats["as_of_month"] == "2026-03"
    assert all(p["evidence_txn_ids"] for p in pats["items"])
    fc = api.get("/api/forecast?horizon_months=3").json()
    assert fc["as_of_month"] == "2026-03" and [p["month"] for p in fc["points"]] == ["2026-04", "2026-05", "2026-06"]
    assert fc["insufficient_data"] is False and fc["reason"] is None
    assert fc["start_balance_cents"] == -1549 - 1210 - 1549 - 2340 - 1549 - 2340  # opening 0 + the six outflows
    assert len(api.get("/api/forecast").json()["points"]) == 6  # default horizon
    assert api.get("/api/forecast?horizon_months=13").status_code == 422
    run = api.get(f"/api/runs/{run_id}").json()
    fc_event = next(e for e in run["events"] if e["node"] == "forecast")
    assert fc_event["node"] == "forecast" and fc_event["output_ref"] == f"forecast:{fc['id']}"


def test_eval_latest_merges_pattern_and_forecast_results(api, tmp_path, monkeypatch):
    monkeypatch.setattr(main, "EVAL_RESULTS", tmp_path)
    _write_result(tmp_path / "categorization.json", "cat-v2", 0.95)
    score = {"precision": 1.0, "recall": 0.5, "tp": 1, "fp": 0, "fn": 1}
    (tmp_path / "patterns.json").write_text(json.dumps({"summary": {
        "models": {"categorizer": "openai/gpt-4o-mini"}, "recurring": {"precision": 1.0, "recall": 0.5},
        "unused": {"precision": None, "recall": 0.0}, "anomaly": {"precision": 1.0, "recall": 1.0},
        "pattern_types": {"recurring": score}}}))
    (tmp_path / "forecast.json").write_text(json.dumps({"summary": {"forecast": {
        "mape": 0.1, "naive_mape": 0.2, "naive_mean_mape": 0.15, "no_trend_mape": 0.12, "coverage_80": 0.8,
        "method_version": "fc-v2", "mean_error_cents": -400,
        "baseline_mean_error_cents": {"no_trend": -410, "naive_last": -70, "naive_mean": -280},
        "origins": 24, "horizon": 1}}}))
    (tmp_path / "forecast_fc-v1.json").write_text(json.dumps({"summary": {"forecast": {
        "mape": 0.12, "naive_mape": 0.2, "method_version": "fc-v1", "mean_error_cents": -600}}}))
    body = api.get("/api/eval/latest").json()
    assert body["categorization"]["accuracy"] == 0.95 and body["unused"]["precision"] is None
    assert body["pattern_types"]["recurring"]["fn"] == 1 and body["forecast"]["coverage_80"] == 0.8
    assert body["forecast"]["baseline_mean_error_cents"]["naive_mean"] == -280
    old = api.get("/api/eval/latest?forecast_version=fc-v1").json()
    assert (old["forecast"]["method_version"], old["forecast"]["mean_error_cents"]) == ("fc-v1", -600)
    assert old["categorization"]["accuracy"] == 0.95  # everything else is unchanged
    assert api.get("/api/eval/latest?forecast_version=fc-v9").status_code == 404
    assert api.get("/api/eval/latest?forecast_version=latest").status_code == 422


def test_llm_timeout_fails_the_run_with_its_message(api, monkeypatch):
    import httpx
    import openai
    from types import SimpleNamespace

    from backend.llm import client

    def create(**kw):
        raise openai.APITimeoutError(request=httpx.Request("POST", client.MESH_BASE_URL))
    monkeypatch.setattr(client, "get_client", lambda: SimpleNamespace(chat=SimpleNamespace(
        completions=SimpleNamespace(create=create))))
    monkeypatch.setenv("CATEGORIZER_MODEL", "openai/gpt-4o-mini")
    _, run_id = _upload_and_run(api)
    run = api.get(f"/api/runs/{run_id}").json()
    assert run["status"] == "failed" and run["finished_at"]
    assert run["error"].startswith("LLMUnavailable: categorizer LLM call failed after") and "timed out" in run["error"]
    assert run["events"][-1]["node"] == "coordinator"


def test_startup_marks_stale_running_runs_failed(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from backend.db import connect, init_db
    path = tmp_path / "app.db"
    conn = connect(path)
    init_db(conn)
    uid = conn.execute("INSERT INTO users (name, opening_balance_cents, opening_date, as_of_month) "
                       "VALUES ('t', 0, '2026-01-01', '2026-02')").lastrowid
    for rid, age, status in (("old", 20, "running"), ("new", 5, "running"), ("pend", 20, "pending")):
        conn.execute("INSERT INTO runs (id, user_id, kind, status, as_of_month, started_at) "
                     f"VALUES (?, ?, 'main', ?, '2026-02', datetime('now', '-{age} minutes'))", (rid, uid, status))
    conn.commit()
    conn.close()
    monkeypatch.setenv("FINPILOT_DB", str(path))
    with TestClient(main.app) as c:
        runs = {r["run_id"]: r for r in c.get(f"/api/runs?user_id={uid}").json()["items"]}
    assert runs["old"]["status"] == "failed" and "still running 15+ minutes" in runs["old"]["error"]
    assert runs["new"]["status"] == "running" and runs["pend"]["status"] == "pending"


def test_under_3_complete_months_means_insufficient_data(api, llm, monkeypatch):
    def no_advisor(messages, **kw):
        raise AssertionError("Advisor called on insufficient data")
    monkeypatch.setattr(graph, "default_advisor_llm", no_advisor)
    _, run_id = _upload_and_run(api)  # 2026-01-03 .. 2026-02-09
    run = api.get(f"/api/runs/{run_id}").json()
    assert run["status"] == "done"
    fc_event = next(e for e in run["events"] if e["node"] == "forecast")
    assert "insufficient data" in fc_event["summary"] and "advice skipped" in fc_event["summary"]
    fc = api.get("/api/forecast").json()
    assert fc["insufficient_data"] is True and fc["points"] == []
    assert fc["reason"] == "Not enough history yet, need at least 3 months (have 1 complete month up to 2026-02)."
    assert api.get("/api/recommendations").json()["items"] == []
    r = api.post("/api/simulate", json={"text": "buy a $500 tv next month"})
    assert r.status_code == 422 and r.json()["detail"]["errors"] == [fc["reason"]]


def test_complete_months():
    from datetime import date

    from backend.agents.forecaster import complete_months
    from backend.ledger import Txn

    def span(first: str, last: str) -> list[Txn]:
        return [Txn(i, date.fromisoformat(d), "X", -100, None, None) for i, d in enumerate((first, last))]
    assert complete_months(span("2025-10-01", "2025-11-30"), "2025-11") == 2  # Maya's first two months
    assert complete_months(span("2025-10-07", "2025-12-25"), "2025-12") == 3  # a week of slack at each edge
    assert complete_months(span("2025-10-08", "2025-12-24"), "2025-12") == 1  # both edge months partial
    assert complete_months(span("2025-10-01", "2025-12-31"), "2026-01") == 3  # as_of month with no data
    assert complete_months([], "2025-12") == 0
