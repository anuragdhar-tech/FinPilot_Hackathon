"""Deleting a profile removes everything that belongs to it, atomically, and nothing else."""
import sqlite3

import pytest

import backend.graph as graph
from backend.users import DELETE_STEPS, delete_user
from tests.test_categorizer import FakeLLM

OWNED_TABLES = [t for t, _ in DELETE_STEPS]  # every table that holds a profile's rows


def populate(conn, tag: str) -> int:
    """One user with at least one row in every per-user table (respecting every foreign key)."""
    uid = conn.execute(
        "INSERT INTO users (name, opening_date, as_of_month) VALUES (?, '2025-10-01', '2026-09')", (tag,)
    ).lastrowid
    conn.execute("INSERT INTO batches (id, user_id, filename, row_count) VALUES (?, ?, 'f.csv', 2)", (f"b-{tag}", uid))
    txns = [
        conn.execute(
            "INSERT INTO transactions (user_id, batch_id, external_id, posted_date, raw_description, amount_cents) "
            "VALUES (?, ?, ?, '2026-01-02', 'NETFLIX.COM', -1549)", (uid, f"b-{tag}", f"{tag}-{i}")
        ).lastrowid
        for i in range(2)
    ]
    run = f"r-{tag}"
    conn.execute("INSERT INTO runs (id, user_id, batch_id, kind, status, as_of_month) VALUES (?, ?, ?, 'main', 'done', '2026-09')",
                 (run, uid, f"b-{tag}"))
    conn.execute("INSERT INTO agent_events (run_id, seq, node, status) VALUES (?, 1, 'ingest', 'ok')", (run,))
    for t in txns:
        conn.execute("INSERT INTO categorizations (txn_id, run_id, category, confidence, model, prompt_version, from_cache) "
                     "VALUES (?, ?, 'entertainment', 0.9, 'm', 'v', 0)", (t, run))
    pat = conn.execute("INSERT INTO patterns (run_id, user_id, type, title, params) VALUES (?, ?, 'recurring', 'Netflix', '{}')",
                       (run, uid)).lastrowid
    conn.execute("INSERT INTO pattern_evidence (pattern_id, txn_id) VALUES (?, ?)", (pat, txns[0]))
    fc = conn.execute("INSERT INTO forecasts (run_id, user_id, as_of_month, horizon_months, method, params, output) "
                      "VALUES (?, ?, '2026-09', 3, 'm', '{}', '[]')", (run, uid)).lastrowid
    goal = conn.execute("INSERT INTO goals (user_id, name, target_cents, target_date, monthly_contribution_cents, start_month) "
                        "VALUES (?, 'g', 100000, '2027-01-01', 5000, '2026-09')", (uid,)).lastrowid
    conn.execute("INSERT INTO goal_snapshots (goal_id, run_id, month, planned_cents, actual_cents, on_track, forecast_id) "
                 "VALUES (?, ?, '2026-09', 1, 1, 1, ?)", (goal, run, fc))
    conn.execute("INSERT INTO goal_snapshots (goal_id, month, planned_cents, actual_cents, on_track) "  # no run, no forecast
                 "VALUES (?, '2026-10', 1, 1, 0)", (goal,))
    rec = conn.execute("INSERT INTO recommendations (run_id, lineage_id, attempt, status, trigger_type, trigger_id, goal_id, "
                       "title, body, claims, model, prompt_version) VALUES (?, ?, 1, 'accepted', 'goal', '1', ?, 't', 'b', '[]', 'm', 'v')",
                       (run, f"lin-{tag}", goal)).lastrowid
    conn.execute("INSERT INTO audit_trail (rec_id, lineage_id, run_id, trigger_type, trigger_id, attempt, verifier_verdict, "
                 "reasons, checks, inputs) VALUES (?, ?, ?, 'goal', '1', 1, 'pass', '[]', '[]', '{}')", (rec, f"lin-{tag}", run))
    conn.commit()
    return uid


def dump(conn) -> dict[str, list[tuple]]:
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")]
    return {t: sorted(tuple(r) for r in conn.execute(f"SELECT * FROM {t}")) for t in tables}


def seed_globals(conn):
    conn.execute("INSERT INTO merchant_cache (merchant_key, category, confidence, model, prompt_version) "
                 "VALUES ('out|NETFLIX.COM', 'entertainment', 0.9, 'm', 'v')")
    conn.execute("INSERT INTO eval_runs (git_sha, dataset, models, metrics, results_path) VALUES ('x', 'd', '{}', '{}', 'p')")
    conn.commit()


def test_delete_removes_all_of_a_users_rows_and_nothing_else(conn):
    seed_globals(conn)
    b = populate(conn, "B")
    only_b = dump(conn)  # B's rows plus the global tables
    a = populate(conn, "A")
    with_a = dump(conn)
    # not vacuous: A has rows in every per-user table, and every table is covered by the delete
    assert all(len(with_a[t]) > len(only_b[t]) for t in OWNED_TABLES)
    assert set(OWNED_TABLES) | {"merchant_cache", "eval_runs"} == set(with_a)

    deleted = delete_user(conn, a)

    assert dump(conn) == only_b  # every table is exactly as it was before A existed
    assert deleted == {t: len(with_a[t]) - len(only_b[t]) for t in OWNED_TABLES}
    assert conn.execute("SELECT COUNT(*) FROM users WHERE id = ?", (b,)).fetchone()[0] == 1
    assert len(only_b["merchant_cache"]) == 1  # the shared cache survives


def test_unknown_user_returns_none_and_changes_nothing(conn):
    populate(conn, "B")
    before = dump(conn)
    assert delete_user(conn, 9999) is None
    assert dump(conn) == before


def test_delete_is_all_or_nothing(conn):
    """A failure at the very last step (deleting the user row) must roll back every earlier delete."""
    seed_globals(conn)
    populate(conn, "B")
    a = populate(conn, "A")
    before = dump(conn)
    conn.execute("CREATE TRIGGER block_user_delete BEFORE DELETE ON users BEGIN SELECT RAISE(ABORT, 'boom'); END")
    with pytest.raises(sqlite3.Error):
        delete_user(conn, a)
    assert dump(conn) == before


# ---- through the API ----

CSV_A = "external_id,date,description,amount\na1,2026-01-03,NETFLIX.COM,-15.49\na2,2026-02-03,NETFLIX.COM,-15.49\n"
CSV_B = "external_id,date,description,amount\na1,2026-01-04,UBER *TRIP X1,-12.10\n"


def upload(api, name, text):
    r = api.post("/api/transactions/upload", files={"file": (f"{name}.csv", text, "text/csv")}, data={"name": name})
    assert r.status_code == 200, r.text
    return r.json()


def test_delete_endpoint(api):
    a, b = upload(api, "a", CSV_A), upload(api, "b", CSV_B)
    r = api.delete(f"/api/users/{a['user_id']}")
    assert r.status_code == 200
    body = r.json()
    assert body["user_id"] == a["user_id"]
    assert body["deleted_rows"]["transactions"] == 2 and body["deleted_rows"]["users"] == 1

    assert [u["id"] for u in api.get("/api/users").json()["items"]] == [b["user_id"]]
    assert api.get(f"/api/transactions?user_id={a['user_id']}").status_code == 404
    assert api.get(f"/api/transactions?user_id={b['user_id']}").json()["total"] == 1  # same external_id, other user: untouched


def test_delete_unknown_user_is_404(api):
    assert api.delete("/api/users/999").status_code == 404
    a = upload(api, "a", CSV_A)
    assert api.delete(f"/api/users/{a['user_id']}").status_code == 200
    assert api.delete(f"/api/users/{a['user_id']}").status_code == 404  # already gone


def test_delete_after_a_categorization_run_keeps_the_shared_cache(api, monkeypatch):
    monkeypatch.setattr(graph, "default_categorizer_llm", FakeLLM())
    monkeypatch.setenv("CATEGORIZER_MODEL", "openai/gpt-4o-mini")
    a, b = upload(api, "a", CSV_A), upload(api, "b", CSV_B)
    run_a = api.post("/api/runs", json={"batch_id": a["batch_id"]}).json()["run_id"]
    api.post("/api/runs", json={"batch_id": b["batch_id"]})
    assert api.get(f"/api/runs/{run_a}").json()["status"] == "done"

    from backend.db import connect
    conn = connect()
    cache_before = [tuple(r) for r in conn.execute("SELECT * FROM merchant_cache ORDER BY merchant_key")]
    assert cache_before  # the runs wrote to the shared cache

    deleted = api.delete(f"/api/users/{a['user_id']}").json()["deleted_rows"]
    assert deleted["runs"] == 1 and deleted["categorizations"] == 2 and deleted["agent_events"] > 0
    assert api.get(f"/api/runs/{run_a}").status_code == 404

    assert [tuple(r) for r in conn.execute("SELECT * FROM merchant_cache ORDER BY merchant_key")] == cache_before
    items = api.get(f"/api/transactions?user_id={b['user_id']}").json()["items"]
    assert len(items) == 1 and items[0]["category"] == "transport"  # B's categorization survived
    conn.close()
