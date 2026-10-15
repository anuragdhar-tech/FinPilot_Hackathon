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


# ---- strict row validation: each case is rejected on its own, naming the line ----

def _one_row(ext="r1", day="2026-01-15", desc="COFFEE", amount="-5.00"):
    return f"external_id,date,description,amount\n{ext},{day},{desc},{amount}\n"


@pytest.mark.parametrize("day,why", [
    pytest.param("20260115", "YYYY-MM-DD", id="date-iso-basic-form"),
    pytest.param("2026-W03-4", "YYYY-MM-DD", id="date-iso-week"),
    pytest.param("2026-1-5", "YYYY-MM-DD", id="date-not-zero-padded"),
    pytest.param("01/15/2026", "YYYY-MM-DD", id="date-us-slashes"),
    pytest.param("2026-01-15T10:00:00", "YYYY-MM-DD", id="date-with-time"),
    pytest.param("２０２６-０１-１５", "YYYY-MM-DD", id="date-non-ascii-digits"),
    pytest.param("2026-02-30", "real calendar", id="date-impossible-day"),
    pytest.param("2026-13-01", "real calendar", id="date-impossible-month"),
    pytest.param("2025-02-29", "real calendar", id="date-leap-day-in-non-leap-year"),
    pytest.param("", "blank", id="date-blank"),
    pytest.param("not-a-date", "YYYY-MM-DD", id="date-garbage"),
])
def test_bad_date_rejected(day, why):
    with pytest.raises(CsvError) as e:
        parse_csv(_one_row(day=day))
    assert len(e.value.errors) == 1 and e.value.errors[0].startswith("line 2:") and why in e.value.errors[0]


def test_valid_leap_day_and_padded_date_accepted():
    assert parse_csv(_one_row(day="2024-02-29"))[0].posted_date == "2024-02-29"


@pytest.mark.parametrize("amount,why", [
    pytest.param("", "blank", id="amount-blank"),
    pytest.param("   ", "blank", id="amount-whitespace-only"),
    pytest.param("abc", "not a number", id="amount-non-numeric"),
    pytest.param("12abc", "not a number", id="amount-trailing-letters"),
    pytest.param("$5.00", "not a number", id="amount-currency-symbol"),
    pytest.param("NaN", "not a number", id="amount-nan"),
    pytest.param("Infinity", "not a number", id="amount-infinity"),
    pytest.param("1e3", "not a number", id="amount-exponent"),
    pytest.param("--5", "not a number", id="amount-double-sign"),
    pytest.param("5.005", "2 decimal places", id="amount-three-decimals"),
])
def test_bad_amount_rejected(amount, why):
    with pytest.raises(CsvError) as e:
        parse_csv(_one_row(amount=amount))
    assert len(e.value.errors) == 1 and e.value.errors[0].startswith("line 2:") and why in e.value.errors[0]


@pytest.mark.parametrize("desc", [
    pytest.param("", id="description-blank"),
    pytest.param("   ", id="description-whitespace-only"),
])
def test_blank_description_rejected(desc):
    with pytest.raises(CsvError) as e:
        parse_csv(_one_row(desc=desc))
    assert e.value.errors == ["line 2: description is blank"]


def test_blank_external_id_rejected():
    with pytest.raises(CsvError) as e:
        parse_csv(_one_row(ext=""))
    assert e.value.errors == ["line 2: external_id is blank"]


def test_row_with_several_problems_lists_them_all_on_its_line():
    with pytest.raises(CsvError) as e:
        parse_csv(_one_row(day="20260115", desc="", amount="abc"))
    assert len(e.value.errors) == 1
    assert all(w in e.value.errors[0] for w in ("description is blank", "YYYY-MM-DD", "not a number"))


def test_upload_with_iso_basic_date_is_422_and_writes_nothing(api):
    """The reported bug: 20260115 used to be accepted and silently rewritten as 2026-01-15."""
    csv_text = ("external_id,date,description,amount\n"
                "ok1,2026-01-14,COFFEE,-5.00\n"
                "bad1,20260115,COFFEE,-5.00\n"
                "bad2,2026-W03-4,COFFEE,-5.00\n")
    r = api.post("/api/transactions/upload", files={"file": ("x.csv", csv_text, "text/csv")})
    assert r.status_code == 422
    assert [e.split(":")[0] for e in r.json()["detail"]["errors"]] == ["line 3", "line 4"]
    assert api.get("/api/users").json()["items"] == []  # no user, batch or transactions were created


def test_infinity_amount_is_422_not_a_server_error(api):
    csv_text = "external_id,date,description,amount\nx,2026-01-15,COFFEE,Infinity\n"
    r = api.post("/api/transactions/upload", files={"file": ("x.csv", csv_text, "text/csv")})
    assert r.status_code == 422 and "not a number" in r.json()["detail"]["errors"][0]
