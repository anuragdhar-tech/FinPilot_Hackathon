"""Read-side helpers shared by the Analyzer and Forecaster: the as_of cut-off, month arithmetic and
the merchant root used to group a merchant's transactions.

Agents never read transactions after the end of users.as_of_month; load_txns is the one place that
enforces it.
"""
from __future__ import annotations

import calendar
import re
import sqlite3
from dataclasses import dataclass
from datetime import date

from backend.agents.categorizer import merchant_key


@dataclass(frozen=True)
class Txn:
    id: int
    day: date
    description: str
    amount_cents: int  # negative = outflow
    category: str | None  # latest categorization; None if uncategorized
    root: str | None  # merchant root; None if the descriptor has too little text

    @property
    def month(self) -> str:
        return f"{self.day:%Y-%m}"


def month_end(month: str) -> date:
    y, m = map(int, month.split("-"))
    return date(y, m, calendar.monthrange(y, m)[1])


def add_months(month: str, n: int) -> str:
    y, m = map(int, month.split("-"))
    y, m0 = divmod(y * 12 + m - 1 + n, 12)
    return f"{y:04d}-{m0 + 1:02d}"


def months_between(first: str, last: str) -> list[str]:
    """Inclusive list of 'YYYY-MM' from first to last."""
    out, m = [], first
    while m <= last:
        out.append(m)
        m = add_months(m, 1)
    return out


# ---- merchant root ----

# Card-network and bank boilerplate that can lead a descriptor without naming the merchant.
ROOT_STOP = {"POS", "PURCHASE", "CHECKCARD", "AUTHORIZED", "DEBIT", "CARD", "RECURRING", "ONLINE", "WEB",
             "PAYMENT", "PAYMENTS", "PMT", "PMTS", "ACH", "THE"}
_PROCESSOR = re.compile(r"^(SQ|TST|DD)\*")
_PIECE = re.compile(r"[*./]")


def merchant_root(description: str, amount_cents: int) -> str | None:
    """First meaningful word of the categorizer's merchant key: UBER*ONE MEMBERSHIP, UBER*TRIP -> UBER.

    Processor prefixes (SQ*, TST*, DD*) are dropped; punctuation inside the word is removed
    (WAL-MART, WALMART+ and WALMART.COM -> WALMART). Aliases are not merged (AMZN vs AMAZON).
    """
    key = merchant_key(description, amount_cents)
    if key is None:
        return None
    body = _PROCESSOR.sub("", key.split("|", 1)[1])
    for tok in body.split():
        pieces = [p for p in _PIECE.split(tok) if p]
        word = re.sub(r"[^A-Z]", "", pieces[0]) if pieces else ""
        if len(word) >= 3 and word not in ROOT_STOP:
            return word
    return None


def display_name(description: str, amount_cents: int) -> str:
    key = merchant_key(description, amount_cents)
    return key.split("|", 1)[1] if key else description.strip()


# ---- loading ----

def load_txns(conn: sqlite3.Connection, user_id: int, as_of_month: str) -> list[Txn]:
    """The user's transactions on or before the last day of as_of_month, with their latest category."""
    rows = conn.execute(
        "SELECT t.id, t.posted_date, t.raw_description, t.amount_cents, c.category FROM transactions t "
        "LEFT JOIN categorizations c ON c.id = (SELECT MAX(id) FROM categorizations WHERE txn_id = t.id) "
        "WHERE t.user_id = ? AND t.posted_date <= ? ORDER BY t.posted_date, t.id",
        (user_id, month_end(as_of_month).isoformat())).fetchall()
    return [Txn(r["id"], date.fromisoformat(r["posted_date"]), r["raw_description"], r["amount_cents"],
                r["category"], merchant_root(r["raw_description"], r["amount_cents"])) for r in rows]


def count_after(conn: sqlite3.Connection, user_id: int, as_of_month: str) -> int:
    return conn.execute("SELECT COUNT(*) FROM transactions WHERE user_id = ? AND posted_date > ?",
                        (user_id, month_end(as_of_month).isoformat())).fetchone()[0]
