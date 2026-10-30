"""Atomic Account.balance updates.

The posting path used to read an Account into the ORM and do
`account.balance += delta` in Python. On Postgres two sessions posting to the
same account could both read the same starting balance and one overwrite the
other — the journal lines stayed correct, but accounts.balance drifted. The
fix accumulates a per-account delta and applies it as an atomic
`UPDATE accounts SET balance = balance + delta`, so the database does the add.

These SQLite tests pin the arithmetic on every balance path and prove an
in-session Account object is not left stale after a post. The lost-update
race itself only reproduces on Postgres, so that test is gated on
SLOWBOOKS_TEST_POSTGRES_URL and skips when no Postgres is configured.
"""

import os
import threading
import uuid
from datetime import date
from decimal import Decimal

import pytest

from app.models.accounts import Account, AccountType


@pytest.fixture
def postgres_url():
    """A scratch PostgreSQL database for one test, created and dropped here.

    Same convention as test_company_files_upgrade.py: SLOWBOOKS_TEST_POSTGRES_URL
    is an admin URL on a server the test can CREATE/DROP DATABASE against. Skips
    when it is unset or no server is reachable.
    """
    import sqlalchemy as sa

    admin_url = os.environ.get("SLOWBOOKS_TEST_POSTGRES_URL")
    if not admin_url:
        pytest.skip("SLOWBOOKS_TEST_POSTGRES_URL is not set")
    pytest.importorskip("psycopg2")
    name = "slowbooks_balance99_" + uuid.uuid4().hex[:12]
    admin = sa.create_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            conn.execute(sa.text(f'CREATE DATABASE "{name}"'))
    except Exception as exc:  # pragma: no cover - depends on the machine
        admin.dispose()
        pytest.skip(f"cannot create a scratch PostgreSQL database: {exc}")
    url = sa.engine.make_url(admin_url).set(database=name)
    try:
        yield url.render_as_string(hide_password=False)
    finally:
        with admin.connect() as conn:
            conn.execute(
                sa.text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :n AND pid <> pg_backend_pid()"
                ),
                {"n": name},
            )
            conn.execute(sa.text(f'DROP DATABASE IF EXISTS "{name}"'))
        admin.dispose()


def _post(db, lines, txn_date=date(2026, 1, 1)):
    from app.services.accounting import create_journal_entry

    return create_journal_entry(db, txn_date, "test JE", lines)


def test_posting_sets_natural_direction_balances(db_session, seed_accounts):
    """Debit-normal and credit-normal accounts still move the right way."""
    cash = seed_accounts["1000"]  # asset (debit-normal)
    revenue = seed_accounts["4000"]  # income (credit-normal)

    _post(
        db_session,
        [
            {"account_id": cash.id, "debit": Decimal("250.00"), "credit": Decimal("0")},
            {
                "account_id": revenue.id,
                "debit": Decimal("0"),
                "credit": Decimal("250.00"),
            },
        ],
    )
    db_session.flush()

    assert cash.balance == Decimal("250.00")
    assert revenue.balance == Decimal("250.00")


def test_in_session_object_reflects_new_balance(db_session, seed_accounts):
    """No stale read: the same in-session Account the caller holds shows the
    posted balance afterward, so an API response built from it is correct."""
    cash = seed_accounts["1000"]
    revenue = seed_accounts["4000"]
    # Touch the balances so the objects are live with the pre-post value.
    assert cash.balance == Decimal("0")
    assert revenue.balance == Decimal("0")

    _post(
        db_session,
        [
            {"account_id": cash.id, "debit": Decimal("40.00"), "credit": Decimal("0")},
            {
                "account_id": revenue.id,
                "debit": Decimal("0"),
                "credit": Decimal("40.00"),
            },
        ],
    )

    # Read WITHOUT flushing/refetching ourselves — the fix expired these.
    assert cash.balance == Decimal("40.00")
    assert revenue.balance == Decimal("40.00")


def test_multiple_lines_to_same_account_aggregate(db_session, seed_accounts):
    """Two lines hitting one account net to one atomic update, not two."""
    cash = seed_accounts["1000"]
    revenue = seed_accounts["4000"]

    _post(
        db_session,
        [
            {"account_id": cash.id, "debit": Decimal("30.00"), "credit": Decimal("0")},
            {"account_id": cash.id, "debit": Decimal("70.00"), "credit": Decimal("0")},
            {
                "account_id": revenue.id,
                "debit": Decimal("0"),
                "credit": Decimal("100.00"),
            },
        ],
    )
    db_session.flush()

    assert cash.balance == Decimal("100.00")
    assert revenue.balance == Decimal("100.00")


def test_null_starting_balance_is_coalesced(db_session):
    """A NULL balance column must behave as zero, not blow up the update."""
    a = Account(
        account_number="9001",
        name="Null Asset",
        account_type=AccountType("asset"),
        balance=None,
    )
    b = Account(
        account_number="9002",
        name="Null Income",
        account_type=AccountType("income"),
        balance=None,
    )
    db_session.add_all([a, b])
    db_session.commit()

    _post(
        db_session,
        [
            {"account_id": a.id, "debit": Decimal("15.00"), "credit": Decimal("0")},
            {"account_id": b.id, "debit": Decimal("0"), "credit": Decimal("15.00")},
        ],
    )
    db_session.flush()

    assert a.balance == Decimal("15.00")
    assert b.balance == Decimal("15.00")


def test_reverse_and_delete_restores_balances(db_session, seed_accounts):
    """The invoice/bill rebuild path reverses balances atomically too."""
    from app.routes.invoices.helpers import _reverse_and_delete_journal

    cash = seed_accounts["1000"]
    revenue = seed_accounts["4000"]

    txn = _post(
        db_session,
        [
            {"account_id": cash.id, "debit": Decimal("90.00"), "credit": Decimal("0")},
            {
                "account_id": revenue.id,
                "debit": Decimal("0"),
                "credit": Decimal("90.00"),
            },
        ],
    )
    db_session.flush()
    assert cash.balance == Decimal("90.00")

    _reverse_and_delete_journal(db_session, txn.id)
    db_session.flush()

    assert cash.balance == Decimal("0.00")
    assert revenue.balance == Decimal("0.00")


def test_concurrent_postings_do_not_lose_updates_on_postgres(postgres_url):
    """The regression itself: many sessions posting to one account land every
    delta. A read-modify-write in Python loses all but one; the atomic UPDATE
    serializes on the row lock and sums them."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.database import Base
    from app.services.accounting import apply_balance_deltas

    engine = create_engine(postgres_url)
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)

    setup = Session()
    acct = Account(
        account_number="TEST-99",
        name="Concurrency Probe",
        account_type=AccountType("asset"),
        balance=Decimal("0"),
    )
    setup.add(acct)
    setup.commit()
    acct_id = acct.id
    setup.close()

    n_threads = 8
    per_thread = Decimal("10.00")
    barrier = threading.Barrier(n_threads)
    errors = []

    def worker():
        s = Session()
        try:
            barrier.wait()  # all threads contend for the same row at once
            apply_balance_deltas(s, {acct_id: per_thread})
            s.commit()
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
            s.rollback()
        finally:
            s.close()

    threads = [threading.Thread(target=worker) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    check = Session()
    final = check.get(Account, acct_id).balance
    check.close()
    engine.dispose()  # the scratch database is dropped by the fixture

    assert not errors, errors
    assert (
        final == per_thread * n_threads
    ), f"lost update: expected {per_thread * n_threads}, got {final}"
