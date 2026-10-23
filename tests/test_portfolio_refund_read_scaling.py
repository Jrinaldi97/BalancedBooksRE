"""Historical refund checks must not reload ledgers for each effective interval."""

from datetime import date, timedelta
from sqlalchemy import event
from app.portfolio import models as m
from app.portfolio.card_workflows import _reciprocal_refund_headroom
from app.portfolio.reports import ledger
from tests.test_portfolio_card_workflows import cards as _cards, purchase

cards = _cards


def test_refund_guard_query_count_is_independent_of_allocation_intervals(cards):
    db = cards["db"]
    _, _, purchased = purchase(cards)
    activity = db.get(m.CardActivity, purchased["card_activity_id"])
    card = db.get(m.CardIdentity, cards["card"])
    component = next(c for c in activity.components if c["key"] == "entity")
    rows = [
        row
        for row in ledger(db, date.max, event_ids=[activity.event_id])
        if row["role"] in {"due_from", "due_to"}
    ]
    assert len(rows) == 2

    def add_revisions(start, count):
        for offset in range(start, start + count):
            for row in rows:
                db.add(
                    m.LineAllocation(
                        line_id=row["line_id"],
                        event_id=activity.event_id,
                        date=date(2026, 2, 4) + timedelta(days=offset),
                        components=[
                            {
                                **{
                                    key: row.get(key)
                                    for key in (
                                        "property_id",
                                        "unit_id",
                                        "fund_id",
                                        "counterparty_id",
                                        "item",
                                        "cash_flow",
                                    )
                                },
                                "amount_cents": row["debit_cents"]
                                - row["credit_cents"],
                            }
                        ],
                        evidence="Independent synthetic effective revision; original reciprocal charge remains 3000",
                    )
                )
        db.flush()

    def queries():
        statements = []

        def count(connection, cursor, statement, parameters, context, many):
            statements.append(statement)

        event.listen(db.get_bind(), "before_cursor_execute", count)
        try:
            _reciprocal_refund_headroom(db, card, component, date(2026, 2, 3), 500)
        finally:
            event.remove(db.get_bind(), "before_cursor_execute", count)
        return statements

    add_revisions(0, 1)
    two_intervals = queries()
    add_revisions(1, 24)
    twenty_six_intervals = queries()
    # Same complete dated proof with 13 times as many intervals. Repeated
    # ledger loading at every cutoff would make this count grow with history.
    assert len(twenty_six_intervals) == len(two_intervals)
    assert len(twenty_six_intervals) <= 14
    assert (
        sum(
            "FROM re_line_allocations" in sql
            and "re_line_allocations.components" in sql
            for sql in twenty_six_intervals
        )
        == 2
    )
