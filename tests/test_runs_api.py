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
    assert nodes == ["ingest", "categorize.batch", "categorize"]
    assert [e["seq"] for e in run["events"]] == [1, 2, 3]
    assert "4/4 categorized (3 by LLM, 1 from cache" in run["events"][-1]["summary"]
    assert run["events"][1]["prompt_tokens"] == 100


def test_transactions_carry_categorization_and_filter(api, llm):
    _upload_and_run(api)
    items = api.get("/api/transactions").json()["items"]
    assert len(items) == 4
    row = next(i for i in items if i["external_id"] == "a4")
    assert (row["category"], row["model"], row["prompt_version"], row["from_cache"]) == \
        ("food_delivery", "openai/gpt-4o-mini", "cat-v1", False)
    assert row["amount_cents"] == -2340 and row["rationale"] == "rule"
    assert {i["external_id"] for i in api.get("/api/transactions?month=2026-01").json()["items"]} == {"a1", "a2"}
    ent = api.get("/api/transactions?category=entertainment").json()["items"]
    assert [i["from_cache"] for i in sorted(ent, key=lambda i: i["date"])] == [False, True]
    assert api.get("/api/transactions?user_id=99").status_code == 404
    assert api.get("/api/transactions?category=subscriptions").status_code == 422


def test_uncategorized_rows_visible_and_event_flags_error(api, monkeypatch):
    monkeypatch.setattr(graph, "default_categorizer_llm", FakeLLM(script=[lambda r: "{}"] * 99))
    monkeypatch.setenv("CATEGORIZER_MODEL", "openai/gpt-4o-mini")
    _, run_id = _upload_and_run(api)
    run = api.get(f"/api/runs/{run_id}").json()
    assert run["status"] == "done" and run["events"][-1]["status"] == "error"
    assert "4 left uncategorized" in run["events"][-1]["summary"]
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
