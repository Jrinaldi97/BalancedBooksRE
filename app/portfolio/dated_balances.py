"""Dated liability headroom, including effective allocation boundaries."""

from collections import defaultdict
from datetime import date, timedelta
from app.models.transactions import TransactionLine
from app.portfolio import models as m
from app.portfolio.ledger_snapshot import LedgerSnapshot


def liability_capacity(db, account_id, start, matches):
    """Minimum exact-scope liability after start, on each reporting basis.

    A future allocation cannot retrospectively supply a missing opening.
    Reuse one account snapshot but project every interval under its own map.
    """
    from app.portfolio.service import digest

    line_ids = db.query(TransactionLine.id).filter_by(account_id=account_id)
    starts = [start] + sorted(
        {
            day
            for (day,) in db.query(m.LineAllocation.date)
            .filter(
                m.LineAllocation.line_id.in_(line_ids), m.LineAllocation.date > start
            )
            .distinct()
        }
    )
    ends = [day - timedelta(days=1) for day in starts[1:]] + [date.max]
    packet = LedgerSnapshot(db, date.max, account_id=account_id)
    current, minimum, history, events = {}, {}, [], set()
    for basis in ("accrual", "modified_cash_bank_posting"):
        original = [row for row in packet.project(start, basis=basis) if matches(row)]
        current[basis] = sum(
            row["credit_cents"] - row["debit_cents"] for row in original
        )
        minimum[basis] = current[basis]
        for lo, hi in zip(starts, ends):
            rows = [row for row in packet.project(hi, basis=basis) if matches(row)]
            history.append(
                {
                    "start": str(lo),
                    "end": str(hi),
                    "basis": basis,
                    "rows_digest": digest(rows),
                }
            )
            events.update(row["event_id"] for row in rows)
            changes = defaultdict(int)
            running = 0
            for row in rows:
                amount = row["credit_cents"] - row["debit_cents"]
                if row["date"] < lo.isoformat():
                    running += amount
                else:
                    changes[row["date"]] += amount
            changes[lo.isoformat()] += 0
            for day, amount in sorted(changes.items()):
                running += amount
                minimum[basis] = min(minimum[basis], running)
    return {
        "current": current,
        "minimum": minimum,
        "history_digest": digest(history),
        "source_event_ids": sorted(events),
    }
