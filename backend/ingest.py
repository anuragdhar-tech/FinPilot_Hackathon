"""Bank CSV ingestion: parse, validate, store. Columns: external_id, date, description, amount.

Amounts arrive as dollars ("-15.22") and are stored as integer cents via Decimal, never float.
Duplicates are detected by external_id only: two identical-looking charges with different ids
are both real (a double charge is exactly what the Analyzer must later flag).

Validation is strict: a date must be written YYYY-MM-DD and be a real calendar day (Python's own
date.fromisoformat also accepts 20260115 and 2026-W03-4, which would be silently rewritten), an
amount must be a plain decimal, and external_id and description must not be blank. Any bad row
rejects the whole upload and every bad line is listed.
"""
import csv
import io
import re
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

REQUIRED_COLUMNS = ("external_id", "date", "description", "amount")

ISO_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
PLAIN_DECIMAL = re.compile(r"[+-]?([0-9]+(\.[0-9]*)?|\.[0-9]+)")  # no NaN, Infinity or exponents


class CsvError(ValueError):
    """The upload is rejected as a whole; `errors` lists every bad row."""

    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors[:5]))
        self.errors = errors


@dataclass(frozen=True)
class Row:
    external_id: str
    posted_date: str
    description: str
    amount_cents: int


def parse_date(value: str) -> str:
    """Strict YYYY-MM-DD, and a real calendar day."""
    s = value.strip()
    if not s:
        raise ValueError("date is blank")
    if not ISO_DATE.fullmatch(s):
        raise ValueError(f"date {value!r} is not in YYYY-MM-DD form")
    try:
        return date.fromisoformat(s).isoformat()
    except ValueError:
        raise ValueError(f"date {value!r} is not a real calendar date") from None


def to_cents(amount: str) -> int:
    s = amount.strip().replace(",", "")
    if not s:
        raise ValueError("amount is blank")
    if not PLAIN_DECIMAL.fullmatch(s):
        raise ValueError(f"amount {amount!r} is not a number")
    cents = Decimal(s) * 100
    if cents != cents.to_integral_value():
        raise ValueError(f"amount {amount!r} has more than 2 decimal places")
    return int(cents)


def parse_csv(text: str) -> list[Row]:
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    missing = [c for c in REQUIRED_COLUMNS if c not in (reader.fieldnames or [])]
    if missing:
        raise CsvError([f"missing columns: {', '.join(missing)}"])
    rows, errors = [], []
    for line, r in enumerate(reader, start=2):
        ext, desc = (r["external_id"] or "").strip(), (r["description"] or "").strip()
        problems, day, cents = [], "", 0
        if not ext:
            problems.append("external_id is blank")
        if not desc:
            problems.append("description is blank")
        try:
            day = parse_date(r["date"] or "")
        except ValueError as e:
            problems.append(str(e))
        try:
            cents = to_cents(r["amount"] or "")
        except ValueError as e:
            problems.append(str(e))
        if problems:
            errors.append(f"line {line}: {'; '.join(problems)}")
            continue
        rows.append(Row(ext, day, desc, cents))
    if errors:
        raise CsvError(errors)
    if not rows:
        raise CsvError(["no transactions in file"])
    return rows


def ingest_csv(conn: sqlite3.Connection, filename: str, text: str, *, user_id: int | None = None,
               name: str | None = None, opening_balance_cents: int = 0,
               persona: str | None = None) -> dict:
    """Store a CSV as a new batch. New user unless user_id is given (then append + dedupe)."""
    rows = parse_csv(text)
    latest_month = max(r.posted_date for r in rows)[:7]
    if user_id is None:
        user_id = conn.execute(
            "INSERT INTO users (name, persona, opening_balance_cents, opening_date, as_of_month) "
            "VALUES (?, ?, ?, ?, ?)",
            (name or filename.rsplit(".", 1)[0], persona, opening_balance_cents,
             min(r.posted_date for r in rows), latest_month),
        ).lastrowid
    else:
        found = conn.execute("SELECT as_of_month FROM users WHERE id = ?", (user_id,)).fetchone()
        if found is None:
            raise LookupError(f"user {user_id} not found")
        # Appending newer data moves the clock forward, never back. While a goal is active it owns the clock
        # (backend/goals.py): the new months become replayable by advance instead of being skipped over.
        if not conn.execute("SELECT 1 FROM goals WHERE user_id = ?", (user_id,)).fetchone():
            conn.execute("UPDATE users SET as_of_month = ? WHERE id = ?",
                         (max(found["as_of_month"], latest_month), user_id))

    existing = {r[0] for r in conn.execute(
        "SELECT external_id FROM transactions WHERE user_id = ?", (user_id,))}
    new, dups = [], 0
    for r in rows:
        if r.external_id in existing:
            dups += 1
            continue
        existing.add(r.external_id)
        new.append(r)

    batch_id = uuid.uuid4().hex
    conn.execute("INSERT INTO batches (id, user_id, filename, row_count, dup_count) VALUES (?, ?, ?, ?, ?)",
                 (batch_id, user_id, filename, len(new), dups))
    conn.executemany(
        "INSERT INTO transactions (user_id, batch_id, external_id, posted_date, raw_description, amount_cents) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [(user_id, batch_id, r.external_id, r.posted_date, r.description, r.amount_cents) for r in new],
    )
    conn.commit()
    return {"batch_id": batch_id, "user_id": user_id, "count": len(new), "duplicates_skipped": dups}


def active_user_id(conn: sqlite3.Connection) -> int | None:
    """The active user is the owner of the most recent upload."""
    row = conn.execute("SELECT user_id FROM batches ORDER BY uploaded_at DESC, rowid DESC LIMIT 1").fetchone()
    return row["user_id"] if row else None
