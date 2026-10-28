"""Bank CSV ingestion: parse, validate, store.

Columns are matched by header alias (date, description, amount or separate debit and credit, optional
external_id); see ALIASES. Delimiter (comma, semicolon, tab) and a leading BOM are handled.

Amounts arrive as dollars ("-15.22", "$1,234.50", "(5.00)") and are stored as integer cents via Decimal, never
float. A single amount column has negative = money out unless the caller says positive amounts are purchases.
Duplicates are detected by external_id only: two identical-looking charges with different ids are both real (a
double charge is exactly what the Analyzer must later flag). A file without an external_id column gets
deterministic ids (see _generated_ids), which keeps that property.

Validation is strict: a date must be YYYY-MM-DD, MM/DD/YYYY or DD/MM/YYYY and a real calendar day (Python's own
date.fromisoformat also accepts 20260115 and 2026-W03-4, which would be silently rewritten); a slash-date order
is inferred only when the file proves it, otherwise the caller must give it; an amount must be a plain decimal;
description (and external_id, when the column exists) must not be blank. Any bad row rejects the whole upload
and every bad line is listed.
"""
import csv
import hashlib
import io
import re
import sqlite3
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

ISO_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
SLASH_DATE = re.compile(r"([0-9]{1,2})/([0-9]{1,2})/([0-9]{4})")
# Optional "$", at most one sign, commas only as proper thousands groups (so "12,50" is rejected, not read as 1250),
# no NaN, Infinity or exponents.
_NUMBER = r"(?:[0-9]{1,3}(?:,[0-9]{3})+(?:\.[0-9]*)?|[0-9]+(?:\.[0-9]*)?|\.[0-9]+)"
MONEY = re.compile(rf"([+-]?)\$?([+-]?)({_NUMBER})")

ORDER_LABEL = {"mdy": "MM/DD/YYYY", "dmy": "DD/MM/YYYY"}

# Header aliases, matched after lowercasing and collapsing whitespace, "_" and "-". Earlier alias wins a tie.
ALIASES = {
    "external_id": ("external id",),
    "date": ("date", "posting date", "transaction date", "trans date"),
    "description": ("description", "details", "memo", "payee", "merchant", "name"),
    "amount": ("amount", "transaction amount"),
    "debit": ("debit",),
    "credit": ("credit",),
}


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


@dataclass(frozen=True)
class Parsed:
    rows: list[Row]
    mapping: dict  # what was read from where (backend.models.CsvMapping)


def parse_date(value: str, order: str | None = None) -> str:
    """YYYY-MM-DD, or D/M/YYYY read as `order` ("mdy" or "dmy"); always a real calendar day."""
    s = value.strip()
    if not s:
        raise ValueError("date is blank")
    slash = SLASH_DATE.fullmatch(s)
    if slash:
        if order is None:
            raise ValueError(f"date {value!r} needs a date order (mdy or dmy)")
        a, b, year = (int(g) for g in slash.groups())
        month, day = (a, b) if order == "mdy" else (b, a)
        try:
            return date(year, month, day).isoformat()
        except ValueError:
            raise ValueError(f"date {value!r} is not a real calendar date as {ORDER_LABEL[order]}") from None
    if not ISO_DATE.fullmatch(s):
        raise ValueError(f"date {value!r} is not in YYYY-MM-DD, MM/DD/YYYY or DD/MM/YYYY form")
    try:
        return date.fromisoformat(s).isoformat()
    except ValueError:
        raise ValueError(f"date {value!r} is not a real calendar date") from None


def to_cents(amount: str) -> int:
    """Signed cents. Accepts "$", thousands commas and (parentheses) as negative."""
    s = amount.strip()
    if not s:
        raise ValueError("amount is blank")
    parens = s.startswith("(") and s.endswith(")")
    if parens:
        s = s[1:-1].strip()
    m = MONEY.fullmatch(s)
    if not m or (m[1] and m[2]) or (parens and (m[1] or m[2])):
        raise ValueError(f"amount {amount!r} is not a number")
    cents = Decimal(m[3].replace(",", "")) * 100
    if cents != cents.to_integral_value():
        raise ValueError(f"amount {amount!r} has more than 2 decimal places")
    return -int(cents) if parens or "-" in (m[1], m[2]) else int(cents)


def _norm(header: str) -> str:
    return re.sub(r"[\s_-]+", " ", header.strip().lower())


def _match_columns(header: list[str]) -> tuple[dict[str, int], list[str]]:
    """Field -> column index, plus a note for every extra column that matched an already-taken field."""
    norm = [_norm(h) for h in header]
    found, ignored = {}, []
    for field, aliases in ALIASES.items():
        hits = sorted((aliases.index(n), i) for i, n in enumerate(norm) if n in aliases)
        if hits:
            found[field] = hits[0][1]
            ignored += [f"{header[i].strip()!r} also matches {field}; using {header[hits[0][1]].strip()!r}"
                        for _, i in hits[1:]]
    return found, ignored


def _check_columns(header: list[str], found: dict[str, int]) -> None:
    def shown(field: str) -> str:
        return f"{field} ({', '.join(a.title() for a in ALIASES[field])})"

    missing = [f for f in ("date", "description") if f not in found]
    split = [f for f in ("debit", "credit") if f in found]
    if "amount" in found and split:
        raise CsvError([f"ambiguous columns: both an amount column ({header[found['amount']].strip()!r}) and "
                        f"{' / '.join(repr(header[found[f]].strip()) for f in split)}; keep one layout"])
    if len(split) == 1:
        missing.append("credit" if split == ["debit"] else "debit")
    elif "amount" not in found and not split:
        missing.append("amount (or both debit and credit)")
    if missing:
        raise CsvError([
            f"could not match columns: {', '.join(missing)}",
            f"columns found: {', '.join(h.strip() or '(blank)' for h in header)}",
            f"expected: {shown('date')}; {shown('description')}; {shown('amount')} or both "
            f"debit and credit; optional: external_id",
        ])


def _date_order(values: list[str], requested: str | None) -> tuple[str | None, str]:
    """(order, where it came from) for D/M/YYYY dates; (None, "") when the file has none."""
    slash = [m for v in values if (m := SLASH_DATE.fullmatch(v.strip()))]
    if not slash:
        return None, ""
    if requested:
        return requested, "chosen by you"
    day_first = next((m[0] for m in slash if int(m[1]) > 12), None)
    month_first = next((m[0] for m in slash if int(m[2]) > 12), None)
    if day_first and month_first:
        raise CsvError([f"dates mix day-first ({day_first}) and month-first ({month_first}) values; "
                        "one file must use one order"])
    if not day_first and not month_first:
        raise CsvError(["every slash date has both parts 12 or below, so DD/MM/YYYY and MM/DD/YYYY cannot be told "
                        "apart; choose the date order (mdy or dmy) and upload again"])
    return ("dmy", "from the file") if day_first else ("mdy", "from the file")


def _generated_ids(rows: list[tuple[str, str, int]]) -> list[str]:
    """Deterministic ids from (date, description, amount, nth identical row in this file).

    The same file always yields the same ids (a re-upload dedupes), while identical same-day charges get
    different occurrence numbers and so different ids (both are kept).
    """
    seen: Counter = Counter()
    ids = []
    for key in rows:
        n = seen[key]
        seen[key] += 1
        ids.append("g-" + hashlib.sha256("|".join((*key[:2], str(key[2]), str(n))).encode()).hexdigest()[:16])
    return ids


def parse_csv(text: str, *, positive_is_spend: bool = False, date_order: str | None = None) -> Parsed:
    text = text.lstrip("﻿")
    first = next((ln for ln in text.splitlines() if ln.strip()), "")
    delimiter = max(",;\t", key=lambda d: len(next(csv.reader([first], delimiter=d), [])))
    header, raw = None, []
    try:
        reader = csv.reader(io.StringIO(text), delimiter=delimiter)
        for cells in reader:
            if not any(c.strip() for c in cells):
                continue
            if header is None:
                header = cells
            else:
                raw.append((reader.line_num, cells))
    except csv.Error as e:
        raise CsvError([f"not readable as CSV: {e}"]) from None
    if header is None:
        raise CsvError(["file is empty"])

    found, ignored = _match_columns(header)
    _check_columns(header, found)
    split = "debit" in found
    if split and positive_is_spend:
        raise CsvError(["'positive amounts are purchases' applies to a single amount column, "
                        "not to separate debit and credit columns"])

    def cell(cells: list[str], field: str) -> str:
        i = found[field]
        return cells[i] if i < len(cells) else ""

    order, order_source = _date_order([cell(c, "date") for _, c in raw], date_order)
    parsed, errors, negative_split = [], [], 0
    for line, cells in raw:
        desc = cell(cells, "description").strip()
        ext = cell(cells, "external_id").strip() if "external_id" in found else ""
        problems, day, cents = [], "", 0
        if "external_id" in found and not ext:
            problems.append("external_id is blank")
        if not desc:
            problems.append("description is blank")
        try:
            day = parse_date(cell(cells, "date"), order)
        except ValueError as e:
            problems.append(str(e))
        try:
            if split:
                d, c = cell(cells, "debit").strip(), cell(cells, "credit").strip()
                dv, cv = (to_cents(d) if d else None), (to_cents(c) if c else None)
                if dv is None and cv is None:
                    raise ValueError("debit and credit are both blank")
                if dv and cv:
                    raise ValueError("debit and credit are both filled")
                negative_split += (dv or 0) < 0 or (cv or 0) < 0
                cents = -abs(dv) if dv else abs(cv or 0)  # sign comes from the column, not the number
            else:
                cents = to_cents(cell(cells, "amount"))
        except ValueError as e:
            problems.append(str(e))
        if problems:
            errors.append(f"line {line}: {'; '.join(problems)}")
        else:
            parsed.append((ext, day, desc, cents))
    if errors:
        raise CsvError(errors)
    if not parsed:
        raise CsvError(["no transactions in file"])

    warnings = []
    if negative_split:
        warnings.append(f"{negative_split} negative number(s) in the debit/credit columns were read by absolute "
                        "value: debits are money out, credits are money in")
    if not split and not positive_is_spend and sum(p[3] > 0 for p in parsed) > 0.8 * len(parsed):
        warnings.append("most amounts are positive; if these are purchases, tick 'positive amounts are purchases'")
    if positive_is_spend:
        parsed = [(e, d, s, -c) for e, d, s, c in parsed]
    generated = "external_id" not in found
    ids = _generated_ids([(d, s, c) for _, d, s, c in parsed]) if generated else [p[0] for p in parsed]
    def name(field: str) -> str | None:
        return header[found[field]].strip() if field in found else None

    mapping = {
        "date": name("date"), "description": name("description"), "amount": name("amount"),
        "debit": name("debit"), "credit": name("credit"), "external_id": name("external_id"),
        "ignored_matches": ignored,
        "date_format": ("YYYY-MM-DD" if order is None else f"{ORDER_LABEL[order]} ({order_source})"),
        "sign_rule": ("debits are money out, credits are money in" if split else
                      "positive amounts are purchases (money out)" if positive_is_spend else
                      "negative amounts are money out"),
        "ids_generated": generated,
        "warnings": warnings,
    }
    return Parsed([Row(i, d, s, c) for i, (_, d, s, c) in zip(ids, parsed)], mapping)


def ingest_csv(conn: sqlite3.Connection, filename: str, text: str, *, user_id: int | None = None,
               name: str | None = None, opening_balance_cents: int = 0,
               persona: str | None = None, positive_is_spend: bool = False,
               date_order: str | None = None) -> dict:
    """Store a CSV as a new batch. New user unless user_id is given (then append + dedupe)."""
    parsed = parse_csv(text, positive_is_spend=positive_is_spend, date_order=date_order)
    rows = parsed.rows
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
    return {"batch_id": batch_id, "user_id": user_id, "count": len(new), "duplicates_skipped": dups,
            "mapping": parsed.mapping}


def active_user_id(conn: sqlite3.Connection) -> int | None:
    """The active user is the owner of the most recent upload."""
    row = conn.execute("SELECT user_id FROM batches ORDER BY uploaded_at DESC, rowid DESC LIMIT 1").fetchone()
    return row["user_id"] if row else None
