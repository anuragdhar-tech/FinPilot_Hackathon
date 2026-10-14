import pytest

from backend.ingest import CsvError, active_user_id, ingest_csv, parse_csv, to_cents

CSV = """external_id,date,description,amount
a1,2026-01-03,NETFLIX.COM,-15.49
a2,2026-01-05,ACME ANALYTICS INC PAYROLL,2185.40
a3,2026-02-01,AMAZON.COM*KF9JL9D2,-89.99
a4,2026-02-01,AMAZON.COM*KF9JL9D2,-89.99
"""


@pytest.mark.parametrize("raw,cents", [("-15.49", -1549), ("2185.4", 218540), ("0.10", 10),
                                       ("-2,150.00", -215000), ("7", 700)])
def test_to_cents_exact(raw, cents):
    assert to_cents(raw) == cents


def test_parse_rejects_bad_rows_atomically():
    bad = CSV + "a5,2026-13-01,X,-1.00\na6,2026-02-02,Y,-1.005\n,2026-02-02,Z,-1.00\n"
    with pytest.raises(CsvError) as e:
        parse_csv(bad)
    assert [err.split(":")[0] for err in e.value.errors] == ["line 6", "line 7", "line 8"]


def test_parse_requires_columns():
    with pytest.raises(CsvError, match="missing columns: amount"):
        parse_csv("external_id,date,description\nx,2026-01-01,Y\n")


def test_new_user_then_append_skips_duplicates(conn):
    first = ingest_csv(conn, "maya.csv", CSV)
    assert (first["count"], first["duplicates_skipped"]) == (4, 0)
    user = conn.execute("SELECT * FROM users WHERE id = ?", (first["user_id"],)).fetchone()
    assert (user["name"], user["opening_date"], user["as_of_month"]) == ("maya", "2026-01-03", "2026-02")

    more = CSV + "a7,2026-03-04,SPOTIFY USA,-11.99\na7,2026-03-04,SPOTIFY USA,-11.99\n"
    second = ingest_csv(conn, "maya2.csv", more, user_id=first["user_id"])
    assert (second["count"], second["duplicates_skipped"]) == (1, 5)  # 4 stored + 1 repeated in-file
    assert conn.execute("SELECT as_of_month FROM users").fetchone()[0] == "2026-03"


def test_identical_rows_with_distinct_ids_both_kept(conn):
    """a3/a4 look like the same charge; only external_id decides duplicates."""
    out = ingest_csv(conn, "f.csv", CSV)
    n = conn.execute("SELECT COUNT(*) FROM transactions WHERE raw_description LIKE 'AMAZON%'").fetchone()[0]
    assert out["duplicates_skipped"] == 0 and n == 2


def test_append_never_moves_clock_back(conn):
    first = ingest_csv(conn, "f.csv", CSV)
    ingest_csv(conn, "old.csv", "external_id,date,description,amount\nz,2025-06-01,X,-1.00\n",
               user_id=first["user_id"])
    assert conn.execute("SELECT as_of_month FROM users").fetchone()[0] == "2026-02"


def test_same_external_id_ok_across_users(conn):
    ingest_csv(conn, "a.csv", CSV)
    second = ingest_csv(conn, "b.csv", CSV)
    assert second["duplicates_skipped"] == 0
    assert active_user_id(conn) == second["user_id"]


def test_upload_endpoint_and_users(api):
    r = api.post("/api/transactions/upload", files={"file": ("maya.csv", CSV, "text/csv")},
                 data={"opening_balance_cents": "420000"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["count"], body["duplicates_skipped"]) == (4, 0)

    r = api.post("/api/transactions/upload", files={"file": ("again.csv", CSV, "text/csv")},
                 data={"user_id": str(body["user_id"])})
    assert r.json()["duplicates_skipped"] == 4

    users = api.get("/api/users").json()["items"]
    assert len(users) == 1 and users[0]["is_active"] and users[0]["txn_count"] == 4
    assert users[0]["opening_balance_cents"] == 420000


def test_upload_rejects_malformed_csv(api):
    r = api.post("/api/transactions/upload",
                 files={"file": ("x.csv", "external_id,date,description,amount\nx,nope,Y,1\n", "text/csv")})
    assert r.status_code == 422 and "line 2" in r.json()["detail"]["errors"][0]
    assert api.get("/api/users").json()["items"] == []


def test_upload_unknown_user_404(api):
    r = api.post("/api/transactions/upload", files={"file": ("x.csv", CSV, "text/csv")}, data={"user_id": "99"})
    assert r.status_code == 404
