import pytest

from backend.ingest import CsvError, active_user_id, ingest_csv, parse_csv, to_cents

CSV = """external_id,date,description,amount
a1,2026-01-03,NETFLIX.COM,-15.49
a2,2026-01-05,ACME ANALYTICS INC PAYROLL,2185.40
a3,2026-02-01,AMAZON.COM*KF9JL9D2,-89.99
a4,2026-02-01,AMAZON.COM*KF9JL9D2,-89.99
"""


@pytest.mark.parametrize("raw,cents", [("-15.49", -1549), ("2185.4", 218540), ("0.10", 10),
                                       ("-2,150.00", -215000), ("7", 700),
                                       ("$5.00", 500), ("-$5.00", -500), ("$-5.00", -500), ("(5.00)", -500),
                                       ("($1,234.50)", -123450), ("+3", 300)])
def test_to_cents_exact(raw, cents):
    assert to_cents(raw) == cents


def test_parse_rejects_bad_rows_atomically():
    bad = CSV + "a5,2026-13-01,X,-1.00\na6,2026-02-02,Y,-1.005\n,2026-02-02,Z,-1.00\n"
    with pytest.raises(CsvError) as e:
        parse_csv(bad)
    assert [err.split(":")[0] for err in e.value.errors] == ["line 6", "line 7", "line 8"]


def test_parse_requires_columns():
    with pytest.raises(CsvError, match="could not match columns: amount"):
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
    pytest.param("01-15-2026", "YYYY-MM-DD", id="date-us-dashes"),
    pytest.param("01/15/26", "YYYY-MM-DD", id="date-two-digit-year"),
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
    assert parse_csv(_one_row(day="2024-02-29")).rows[0].posted_date == "2024-02-29"


@pytest.mark.parametrize("amount,why", [
    pytest.param("", "blank", id="amount-blank"),
    pytest.param("   ", "blank", id="amount-whitespace-only"),
    pytest.param("abc", "not a number", id="amount-non-numeric"),
    pytest.param("12abc", "not a number", id="amount-trailing-letters"),
    pytest.param('"12,50"', "not a number", id="amount-decimal-comma-not-read-as-1250"),
    pytest.param('"1,5"', "not a number", id="amount-misplaced-comma"),
    pytest.param("$-$5", "not a number", id="amount-two-currency-symbols"),
    pytest.param("-(5.00)", "not a number", id="amount-sign-and-parentheses"),
    pytest.param("()", "not a number", id="amount-empty-parentheses"),
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


# ---- flexible import: header aliases, layouts, signs, date orders, generated ids ----

def _rows(text, **kw):
    return [(r.external_id, r.posted_date, r.description, r.amount_cents) for r in parse_csv(text, **kw).rows]


def test_current_exact_format_is_unchanged():
    parsed = parse_csv(CSV)
    assert [(r.external_id, r.posted_date, r.description, r.amount_cents) for r in parsed.rows] == [
        ("a1", "2026-01-03", "NETFLIX.COM", -1549), ("a2", "2026-01-05", "ACME ANALYTICS INC PAYROLL", 218540),
        ("a3", "2026-02-01", "AMAZON.COM*KF9JL9D2", -8999), ("a4", "2026-02-01", "AMAZON.COM*KF9JL9D2", -8999)]
    m = parsed.mapping
    assert (m["external_id"], m["ids_generated"], m["date_format"], m["warnings"]) == ("external_id", False, "YYYY-MM-DD", [])
    assert m["sign_rule"] == "negative amounts are money out"


def test_synthetic_personas_parse_exactly_as_a_plain_reader_sees_them():
    import csv
    from pathlib import Path

    for f in sorted(Path("data/synthetic").glob("*.csv")):
        text = f.read_text()
        expected = [(r["external_id"], r["date"], r["description"], int(round(float(r["amount"]) * 100)))
                    for r in csv.DictReader(text.splitlines())]
        assert _rows(text) == expected and len(expected) > 500


def test_header_aliases_are_case_and_whitespace_insensitive_and_extra_columns_ignored():
    text = "  POSTING   date ,Payee,Transaction_Amount,Balance,Category\n2026-01-03,Cafe,-4.50,100.00,Food\n"
    parsed = parse_csv(text)
    assert [(r.posted_date, r.description, r.amount_cents) for r in parsed.rows] == [("2026-01-03", "Cafe", -450)]
    assert parsed.mapping["date"] == "POSTING   date" and parsed.mapping["amount"] == "Transaction_Amount"


def test_two_columns_for_one_field_use_the_earlier_alias_and_say_so():
    text = "Date,Posting Date,Memo,Description,Amount\n2026-01-03,2026-01-04,memo text,Cafe,-4.50\n"
    parsed = parse_csv(text)
    assert parsed.rows[0].description == "Cafe" and parsed.rows[0].posted_date == "2026-01-03"
    assert len(parsed.mapping["ignored_matches"]) == 2
    assert "'Posting Date'" in parsed.mapping["ignored_matches"][0] or "'Posting Date'" in parsed.mapping["ignored_matches"][1]


def test_no_confident_match_lists_found_and_expected_columns():
    with pytest.raises(CsvError) as e:
        parse_csv("When,Who,How much\n2026-01-03,Cafe,-4.50\n")
    text = "\n".join(e.value.errors)
    assert "could not match columns: date, description, amount (or both debit and credit)" in text
    assert "columns found: When, Who, How much" in text
    assert "Posting Date" in text and "Merchant" in text and "debit and credit" in text


def test_amount_and_debit_credit_together_is_ambiguous():
    with pytest.raises(CsvError, match="ambiguous columns"):
        parse_csv("Date,Description,Amount,Debit,Credit\n2026-01-03,Cafe,-4.50,4.50,\n")


def test_only_one_of_debit_credit_is_a_missing_column():
    with pytest.raises(CsvError, match="could not match columns: credit"):
        parse_csv("Date,Description,Debit\n2026-01-03,Cafe,4.50\n")


DEBIT_CREDIT = """Date,Description,Debit,Credit
2026-01-03,COFFEE,"$1,234.50",
2026-01-04,PAYROLL,,$2000.00
2026-01-05,REFUND,0.00,12.00
"""


def test_debit_credit_columns_debits_are_outflows():
    parsed = parse_csv(DEBIT_CREDIT)
    assert [r.amount_cents for r in parsed.rows] == [-123450, 200000, 1200]
    assert parsed.mapping["sign_rule"] == "debits are money out, credits are money in"
    assert (parsed.mapping["amount"], parsed.mapping["debit"], parsed.mapping["credit"]) == (None, "Debit", "Credit")
    assert parsed.mapping["warnings"] == []


def test_debit_credit_negative_numbers_are_read_by_absolute_value_and_reported():
    parsed = parse_csv("Date,Description,Debit,Credit\n2026-01-03,A,-5.00,\n2026-01-04,B,(6.00),\n"
                       "2026-01-05,C,,-7.00\n")
    assert [r.amount_cents for r in parsed.rows] == [-500, -600, 700]
    assert "3 negative number(s)" in parsed.mapping["warnings"][0]


@pytest.mark.parametrize("debit,credit,why", [("5.00", "5.00", "both filled"), ("", "", "both blank"),
                                              ("", "  ", "both blank")])
def test_debit_credit_row_needs_exactly_one_side(debit, credit, why):
    with pytest.raises(CsvError) as e:
        parse_csv(f"Date,Description,Debit,Credit\n2026-01-03,A,{debit},{credit}\n")
    assert e.value.errors[0].startswith("line 2:") and why in e.value.errors[0]


def test_positive_is_spend_flips_a_single_signed_column():
    text = "Date,Description,Amount\n2026-01-03,Cafe,4.50\n2026-01-04,Refund,(10.00)\n"
    assert [r.amount_cents for r in parse_csv(text).rows] == [450, -1000]  # default: negative = money out
    flipped = parse_csv(text, positive_is_spend=True)
    assert [r.amount_cents for r in flipped.rows] == [-450, 1000]
    assert flipped.mapping["sign_rule"] == "positive amounts are purchases (money out)"


def test_positive_is_spend_with_debit_credit_columns_is_rejected():
    with pytest.raises(CsvError, match="single amount column"):
        parse_csv(DEBIT_CREDIT, positive_is_spend=True)


def test_mostly_positive_amounts_get_a_hint_but_parse_the_same():
    text = "Date,Description,Amount\n" + "".join(f"2026-01-{d:02d},Shop,{d}.00\n" for d in range(1, 10)) \
        + "2026-01-20,Payroll,-100.00\n"
    parsed = parse_csv(text)
    assert [r.amount_cents for r in parsed.rows][:2] == [100, 200]  # no parsing change
    assert parsed.mapping["warnings"] == [
        "most amounts are positive; if these are purchases, tick 'positive amounts are purchases'"]
    assert parse_csv(text, positive_is_spend=True).mapping["warnings"] == []  # already told
    exactly_80 = "Date,Description,Amount\n" + "2026-01-01,A,1\n" * 4 + "2026-01-02,B,-1\n"
    assert parse_csv(exactly_80).mapping["warnings"] == []  # "more than 80%"
    assert parse_csv(CSV).mapping["warnings"] == []


def test_mm_dd_yyyy_is_inferred_when_a_day_is_over_12():
    text = "Date,Description,Amount\n03/04/2026,A,-1\n01/15/2026,B,-2\n1/5/2026,C,-3\n"
    parsed = parse_csv(text)
    assert [r.posted_date for r in parsed.rows] == ["2026-03-04", "2026-01-15", "2026-01-05"]
    assert parsed.mapping["date_format"] == "MM/DD/YYYY (from the file)"


def test_dd_mm_yyyy_is_inferred_when_the_second_part_is_over_12():
    parsed = parse_csv("Date,Description,Amount\n03/04/2026,A,-1\n15/01/2026,B,-2\n")
    assert [r.posted_date for r in parsed.rows] == ["2026-04-03", "2026-01-15"]
    assert parsed.mapping["date_format"] == "DD/MM/YYYY (from the file)"


def test_mixed_day_first_and_month_first_is_rejected_even_with_an_order_given_it_is_row_errors():
    text = "Date,Description,Amount\n15/01/2026,A,-1\n01/20/2026,B,-2\n"
    with pytest.raises(CsvError, match="mix day-first"):
        parse_csv(text)
    with pytest.raises(CsvError) as e:  # a stated order makes the other row an ordinary bad row
        parse_csv(text, date_order="dmy")
    assert len(e.value.errors) == 1 and e.value.errors[0].startswith("line 3:") and "DD/MM/YYYY" in e.value.errors[0]


def test_ambiguous_slash_dates_need_an_order_and_never_guess():
    text = "Date,Description,Amount\n03/04/2026,A,-1\n12/11/2026,B,-2\n"
    with pytest.raises(CsvError, match="cannot be told apart"):
        parse_csv(text)
    assert [r.posted_date for r in parse_csv(text, date_order="mdy").rows] == ["2026-03-04", "2026-12-11"]
    parsed = parse_csv(text, date_order="dmy")
    assert [r.posted_date for r in parsed.rows] == ["2026-04-03", "2026-11-12"]
    assert parsed.mapping["date_format"] == "DD/MM/YYYY (chosen by you)"


def test_iso_dates_ignore_the_date_order():
    assert parse_csv(CSV, date_order="dmy").mapping["date_format"] == "YYYY-MM-DD"


def test_impossible_slash_date_is_a_row_error():
    with pytest.raises(CsvError) as e:
        parse_csv("Date,Description,Amount\n02/30/2026,A,-1\n15/01/2026,B,-2\n", date_order="mdy")
    assert "not a real calendar date as MM/DD/YYYY" in e.value.errors[0] and e.value.errors[0].startswith("line 2:")


NO_ID = """Date,Description,Amount
2026-01-03,COFFEE,-4.50
2026-01-03,COFFEE,-4.50
2026-01-04,COFFEE,-4.50
"""


def test_generated_ids_are_deterministic_unique_per_occurrence_and_marked():
    parsed = parse_csv(NO_ID)
    ids = [r.external_id for r in parsed.rows]
    assert len(set(ids)) == 3 and all(i.startswith("g-") for i in ids)
    assert ids == [r.external_id for r in parse_csv(NO_ID).rows]
    assert parsed.mapping["external_id"] is None and parsed.mapping["ids_generated"] is True
    # the same multiset of rows in another order gets the same set of ids
    shuffled = "Date,Description,Amount\n2026-01-04,COFFEE,-4.50\n2026-01-03,COFFEE,-4.50\n2026-01-03,COFFEE,-4.50\n"
    assert set(ids) == {r.external_id for r in parse_csv(shuffled).rows}


def test_no_id_file_reupload_dedupes_and_same_day_identical_charges_are_both_kept(conn):
    first = ingest_csv(conn, "bank.csv", NO_ID)
    assert (first["count"], first["duplicates_skipped"]) == (3, 0)  # two same-day $4.50 charges both stored
    n = conn.execute("SELECT COUNT(*) FROM transactions WHERE posted_date = '2026-01-03'").fetchone()[0]
    assert n == 2
    again = ingest_csv(conn, "bank.csv", NO_ID, user_id=first["user_id"])
    assert (again["count"], again["duplicates_skipped"]) == (0, 3)
    # a later export that repeats the old rows and adds new ones stores only the new ones
    later = ingest_csv(conn, "bank2.csv", NO_ID + "2026-01-09,COFFEE,-4.50\n2026-01-09,COFFEE,-4.50\n",
                       user_id=first["user_id"])
    assert (later["count"], later["duplicates_skipped"]) == (2, 3)
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 5


def test_external_id_column_present_but_blank_is_still_an_error():
    with pytest.raises(CsvError) as e:
        parse_csv("Date,Description,Amount,External ID\n2026-01-03,A,-1,\n")
    assert e.value.errors == ["line 2: external_id is blank"]


def test_semicolon_and_tab_delimiters_bom_and_blank_lines():
    semi = "﻿Date;Description;Amount\n\n2026-01-03;Cafe, bar;-1234.50\n;;\n2026-01-04;Shop;-2.00\n"
    assert _rows(semi) == [(r.external_id, "2026-01-03", "Cafe, bar", -123450) if i == 0
                           else (r.external_id, "2026-01-04", "Shop", -200)
                           for i, r in enumerate(parse_csv(semi).rows)]
    tab = "Date\tDescription\tAmount\n2026-01-03\tCafe\t-1.00\n"
    assert [(d, s, c) for _, d, s, c in _rows(tab)] == [("2026-01-03", "Cafe", -100)]


def test_error_line_numbers_follow_the_file_when_blank_lines_are_skipped():
    with pytest.raises(CsvError) as e:
        parse_csv("Date,Description,Amount\n\n2026-01-03,A,-1\n\n2026-01-04,B,nope\n")
    assert e.value.errors[0].startswith("line 5:")


def test_empty_file_and_header_only_file():
    with pytest.raises(CsvError, match="file is empty"):
        parse_csv("\n \n")
    with pytest.raises(CsvError, match="no transactions"):
        parse_csv("Date,Description,Amount\n")


def test_upload_endpoint_returns_the_mapping_and_takes_the_two_new_fields(api):
    text = "Date,Description,Amount\n03/04/2026,Cafe,4.50\n12/11/2026,Shop,6.00\n"
    r = api.post("/api/transactions/upload", files={"file": ("bank.csv", text, "text/csv")})
    assert r.status_code == 422 and "cannot be told apart" in r.json()["detail"]["errors"][0]
    assert api.get("/api/users").json()["items"] == []

    r = api.post("/api/transactions/upload", files={"file": ("bank.csv", text, "text/csv")},
                 data={"date_order": "dmy", "positive_is_spend": "true"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["count"] == 2 and body["mapping"]["date_format"] == "DD/MM/YYYY (chosen by you)"
    assert body["mapping"]["ids_generated"] is True and body["mapping"]["warnings"] == []
    stored = api.get("/api/transactions", params={"user_id": body["user_id"]}).json()["items"]
    assert sorted((t["date"], t["amount_cents"]) for t in stored) == [("2026-04-03", -450), ("2026-11-12", -600)]

    r = api.post("/api/transactions/upload", files={"file": ("bank.csv", text, "text/csv")},
                 data={"date_order": "ydm"})
    assert r.status_code == 422


def test_upload_endpoint_unmatched_header_is_422_with_found_and_expected(api):
    r = api.post("/api/transactions/upload", files={"file": ("x.csv", "A,B\n1,2\n", "text/csv")})
    assert r.status_code == 422 and "columns found: A, B" in r.json()["detail"]["errors"][1]


def test_generated_ids_do_not_dedupe_against_an_earlier_upload_that_had_real_ids(conn):
    first = ingest_csv(conn, "a.csv", CSV)
    again = ingest_csv(conn, "b.csv", "Date,Description,Amount\n2026-01-03,NETFLIX.COM,-15.49\n", user_id=first["user_id"])
    assert (again["count"], again["duplicates_skipped"]) == (1, 0)


def test_non_utf8_upload_is_422(api):
    r = api.post("/api/transactions/upload", files={"file": ("x.csv", "Date,Description,Amount\n2026-01-03,Caf\xe9,-1\n".encode("latin-1"), "text/csv")})
    assert r.status_code == 422 and r.json()["detail"]["errors"] == ["file is not UTF-8 text"]
