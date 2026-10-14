"""Bank CSV ingestion: parse, validate, store. Columns: external_id, date, description, amount.

Amounts arrive as dollars ("-15.22") and are stored as integer cents via Decimal, never float.
Duplicates are detected by external_id only: two identical-looking charges with different ids
are both real (a double charge is exactly what the Analyzer must later flag).
"""
import csv
import io
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation

REQUIRED_COLUMNS = ("external_id", "date", "description", "amount")


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


def to_cents(amount: str) -> int:
    d = Decimal(amount.strip().replace(",", ""))
    cents = d * 100
    if cents != cents.to_integral_value():
        raise InvalidOperation(f"more than 2 decimal places: {amount!r}")
    return int(cents)


def parse_csv(text: str) -> list[Row]:
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    missing = [c for c in REQUIRED_COLUMNS if c not in (reader.fieldnames or [])]
    if missing:
        raise CsvError([f"missing columns: {', '.join(missing)}"])
    rows, errors = [], []
    for line, r in enumerate(reader, start=2):
        ext, desc = (r["external_id"] or "").strip(), (r["description"] or "").strip()
        try:
            day = date.fromisoformat((r["date"] or "").strip()).isoformat()
            cents = to_cents(r["amount"] or "")
        except (ValueError, InvalidOperation) as e:
            errors.append(f"line {line}: {e}")
            continue
        if not ext or not desc:
            errors.append(f"line {line}: empty external_id or description")
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
        # Appending newer data moves the clock forward, never back.
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
    """The active user is the owner of the most recent upload (PLAN.md section 5)."""
    row = conn.execute("SELECT user_id FROM batches ORDER BY uploaded_at DESC, rowid DESC LIMIT 1").fetchone()
    return row["user_id"] if row else None
