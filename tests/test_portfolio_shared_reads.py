"""Exact output parity against the frozen pre-optimization implementation."""

from datetime import date
import pytest
from sqlalchemy import event
from app.portfolio import models as m
from app.portfolio.observation_state import observation_state, observation_states
from app.portfolio.ledger_snapshot import LedgerSnapshot
from app.portfolio.reports import ledger
from app.portfolio.service import control
from tests.reference_portfolio_readers import (
    baseline_observation_state,
    ledger as baseline_ledger,
)
from tests.test_portfolio_runtime import synthetic_observations
from tests.test_portfolio_historical_proofs import receipt_case, correction_set_case
from tests.test_balancedbooks_portfolio import (
    portfolio as _portfolio,
    correction_pool as _correction_pool,
)

portfolio = _portfolio
correction_pool = _correction_pool


def test_shared_batch_matches_every_full_state_and_digest(db_session):
    _, _, ids = synthetic_observations(db_session, 30)
    # Unposted sources and a reordered request must keep their original semantics.
    ids.reverse()
    expected = [baseline_observation_state(db_session, ident) for ident in ids]
    queries = []

    def count(*args):
        queries.append(args[2])

    event.listen(db_session.get_bind(), "before_cursor_execute", count)
    try:
        actual = observation_states(db_session, ids)
    finally:
        event.remove(db_session.get_bind(), "before_cursor_execute", count)
    assert actual == expected
    assert len(queries) <= 12


@pytest.mark.parametrize("matched", [False, True])
def test_shared_receipt_ancestry_keeps_legacy_pointer_guard(portfolio, matched):
    db, _, _, obs_id, _ = receipt_case(portfolio, matched=matched)
    assert observation_state(db, obs_id) == baseline_observation_state(db, obs_id)


def test_shared_allocation_chain_and_both_bases_match_reference(correction_pool):
    db, bank, _, _ = correction_set_case(correction_pool)
    observations = list(db.query(m.BankObservation).filter_by(account_id=bank))
    e = db.get(m.AccountScope, bank).entity_id
    obs = observations[0]
    db.add(
        m.BasisAdjustment(
            event_id=obs.event_id,
            entity_id=e,
            date=date(2026, 1, 5),
            reason="Synthetic basis-only scope",
            lines=[
                {
                    "account_id": control(db, e, "suspense_debit"),
                    "debit_cents": 25,
                    "credit_cents": 0,
                    "fund_id": None,
                },
                {
                    "account_id": control(db, e, "equity"),
                    "debit_cents": 0,
                    "credit_cents": 25,
                    "fund_id": None,
                },
            ],
        )
    )
    db.flush()
    for cutoff in (date(2025, 12, 31), date(2026, 1, 1), date(2026, 1, 15), date.max):
        ids = [o.id for o in observations]
        assert observation_states(db, ids, cutoff) == [
            baseline_observation_state(db, ident, cutoff) for ident in ids
        ]
        for basis in ("accrual", "modified_cash_bank_posting"):
            assert ledger(db, cutoff, account_id=bank, basis=basis) == baseline_ledger(
                db, cutoff, account_id=bank, basis=basis
            )


def test_earlier_cutoff_never_inherits_later_classification_or_allocation(db_session):
    _, bank, ids = synthetic_observations(db_session, 1)
    original = observation_state(db_session, ids[0])
    event_id = original["event_ids"][0]
    row = next(r for r in ledger(db_session, date.max) if r["account_id"] == bank)
    db_session.add(
        m.CashClassification(
            line_id=row["line_id"],
            event_id=event_id,
            date=date(2026, 2, 1),
            cash_flow="financing",
            reason="Later effective classification",
        )
    )
    db_session.add(
        m.LineAllocation(
            line_id=row["line_id"],
            event_id=event_id,
            date=date(2026, 3, 1),
            components=[
                {
                    "amount_cents": 100,
                    "property_id": None,
                    "unit_id": None,
                    "fund_id": None,
                    "counterparty_id": None,
                    "item": None,
                    "cash_flow": "investing",
                }
            ],
            evidence="Later allocation",
        )
    )
    db_session.flush()
    packet = LedgerSnapshot(db_session, date.max)
    for cutoff in (date(2026, 1, 2), date(2026, 2, 1), date(2026, 3, 1)):
        assert packet.project(cutoff) == baseline_ledger(db_session, cutoff)
    before = packet.project(date(2026, 1, 2))
    before[0]["debit_cents"] = 999999
    assert packet.project(date(2026, 1, 2)) == baseline_ledger(
        db_session, date(2026, 1, 2)
    )


def test_shared_batch_uses_one_wal_snapshot(tmp_path, monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from app.database import Base
    import app.portfolio.observation_state as state_module

    engine = create_engine(f"sqlite:///{tmp_path / 'shared_snapshot.db'}")
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA journal_mode=WAL")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    with factory() as writer:
        _, _, ids = synthetic_observations(writer, 2)
        review = (
            writer.query(m.ReviewItem)
            .filter_by(observation_id=ids[1], code="suspense")
            .one()
        )
        review_id, original = review.id, review.detail
        writer.commit()
    original_loader = state_module.LedgerSnapshot
    writes = []

    def concurrent_load(db, *args, **kwargs):
        result = original_loader(db, *args, **kwargs)
        with factory() as writer:
            writer.get(m.ReviewItem, review_id).detail = (
                "Committed after reader pinned its snapshot"
            )
            writer.commit()
        writes.append(True)
        return result

    monkeypatch.setattr(state_module, "LedgerSnapshot", concurrent_load)
    with factory() as reader:
        result = observation_states(reader, ids)
        assert (
            next(r for r in result[1]["reviews"] if r["id"] == review_id)["detail"]
            == original
        )
    with factory() as reader:
        assert reader.get(m.ReviewItem, review_id).detail != original
    assert writes == [True]
    engine.dispose()


def test_revision_packet_keeps_same_day_order_and_owned_json(db_session):
    _, bank, ids = synthetic_observations(db_session, 1)
    event_id = db_session.get(m.BankObservation, ids[0]).event_id
    physical = next(
        row for row in ledger(db_session, date.max) if row["account_id"] == bank
    )
    evidence = "Synthetic lengthy evidence retained in its source table. " * 100
    revisions = []
    for day, item in (
        (date(2026, 2, 1), "first"),
        (date(2026, 2, 1), "same-day-last"),
        (date(2026, 3, 1), "later"),
    ):
        revision = m.LineAllocation(
            line_id=physical["line_id"],
            event_id=event_id,
            date=day,
            components=[{"amount_cents": 100, "item": item, "cash_flow": "investing"}],
            evidence=evidence,
        )
        revisions.append(revision)
        db_session.add(revision)
    db_session.add_all(
        [
            m.CashClassification(
                line_id=physical["line_id"],
                event_id=event_id,
                date=date(2026, 1, 15),
                cash_flow="operating",
                reason=evidence,
            ),
            m.CashClassification(
                line_id=physical["line_id"],
                event_id=event_id,
                date=date(2026, 1, 15),
                cash_flow="financing",
                reason=evidence,
            ),
        ]
    )
    db_session.flush()
    packet = LedgerSnapshot(db_session, date.max)
    cutoffs = [date(2026, 1, 2), date(2026, 1, 15), date(2026, 2, 1), date(2026, 3, 1)]
    expected = {cutoff: baseline_ledger(db_session, cutoff) for cutoff in cutoffs}
    for cutoff in cutoffs:
        assert packet.project(cutoff) == expected[cutoff]
    february = next(
        row
        for row in packet.project(date(2026, 2, 1))
        if row["line_id"] == physical["line_id"]
    )
    assert february["item"] == "same-day-last"
    # Neither an already-loaded ORM JSON object nor returned rows own the
    # packet's independent decoded values, even after the session goes away.
    revisions[1].components[0]["item"] = "changed-in-ORM"
    february["item"] = "changed-in-output"
    db_session.expunge_all()
    for cutoff in cutoffs:
        assert packet.project(cutoff) == expected[cutoff]
