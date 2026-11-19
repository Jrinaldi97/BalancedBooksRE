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


def outgoing_fund_availability(packet, withdrawal_days):
    """Check historical closes and a withdrawal-day floor independent of bank ordering.

    Each physical line enters on its posting date. Later replacements contribute
    only the change in that line's fund balance on their effective date. A day's
    negative changes bound the cash needed if every debit precedes every credit.
    """
    from app.portfolio.service import fail

    if packet.adjustments:
        fail(
            "funding_basis",
            "Review bank basis adjustments before certifying source cash",
        )
    changes = {fund: defaultdict(list) for fund in withdrawal_days}
    for line in packet.rows:
        posted = line["date"]
        amounts = {line["fund_id"]: line["debit_cents"] - line["credit_cents"]}
        by_day = {posted: amounts}
        for day, _, components in packet.allocations.get(line["line_id"], ()):
            amounts = defaultdict(int)
            for component in components:
                amounts[component.get("fund_id")] += component["amount_cents"]
            by_day[max(posted, day.isoformat())] = amounts
        previous = {}
        for day, amounts in sorted(by_day.items()):
            for fund in withdrawal_days.keys() & (amounts.keys() | previous.keys()):
                delta = amounts.get(fund, 0) - previous.get(fund, 0)
                if delta:
                    changes[fund][day].append(delta)
            previous = amounts

    proofs = []
    for fund, days in sorted(withdrawal_days.items()):
        start = min(days)
        floor_by_day = {}
        closing_minimum = None
        balance = 0
        for day in sorted(changes[fund].keys() | days):
            movements = changes[fund][day]
            floor = balance + sum(value for value in movements if value < 0)
            balance += sum(movements)
            if day < start:
                continue
            if day in days:
                floor_by_day[day] = floor
            if balance < 0 or (day in days and floor < 0):
                fail(
                    "funding_availability",
                    "The identified fund lacks supported cash at a historical boundary",
                    {
                        "fund_id": fund,
                        "date": day,
                        "closing_cents": balance,
                        "withdrawal_day_floor_cents": floor if day in days else None,
                    },
                )
            closing_minimum = (
                balance if closing_minimum is None else min(closing_minimum, balance)
            )
        proofs.append(
            {
                "fund_id": fund,
                "minimum_closing_cents": closing_minimum,
                "withdrawal_day_floor_cents": floor_by_day,
            }
        )
    return proofs
