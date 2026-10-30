"""Associations consume physical capacity without changing financial history."""

from copy import deepcopy
import uuid

import pytest
from pydantic import ValidationError

from app.portfolio import models as m
from app.portfolio.observation_state import observation_state
from app.portfolio.service import Problem, execute
from tests.test_balancedbooks_portfolio import account, bank_rows, run
from tests.test_portfolio_card_workflows import reference
from tests.test_portfolio_correction_validation import financial_snapshot
from tests import test_balancedbooks_portfolio as fixtures

portfolio = fixtures.portfolio


def pair(portfolio, outgoing=2600, incoming=2800):
    db, owner, bank, _, income = portfolio
    receiving = account(db, owner, uuid.uuid4().hex[:12], "asset", "bank")
    ids = [
        bank_rows(
            db, target, [dict(date="2026-02-02", amount_cents=amount)], uuid.uuid4().hex
        )[0]
        for target, amount in ((bank, -outgoing), (receiving, incoming))
    ]
    for ident in ids:
        run(
            db,
            "post_bank",
            observation_id=ident,
            rationale="Synthetic physical movement",
            duplicate_resolution="distinct",
            offset_account_id=income if ident == ids[1] else None,
        )
    return ids


def request(db, ids, amount=2600, **extra):
    return dict(
        withdrawal=reference(db, ids[0]),
        deposit=reference(db, ids[1]),
        amount_cents=amount,
        evidence="User identified the deposited check; remaining deposit is unknown",
        evidence_kind="user_confirmed",
        **extra,
    )


def test_component_residual_supersession_reversal_and_retry(portfolio):
    db = portfolio[0]
    ids = pair(portfolio)
    before = financial_snapshot(db)
    command = dict(
        operation="associate_transfer_component",
        key=uuid.uuid4().hex,
        data=request(db, ids),
    )
    result = execute(db, command)
    assert result["remaining_unassociated_cents"] == {str(ids[0]): 0, str(ids[1]): 200}
    assert result["resolved_review_ids"] == []
    assert result["association"]["evidence"]["kind"] == "user_confirmed"
    assert execute(db, command)["replayed"]
    changed = deepcopy(command)
    changed["data"]["amount_cents"] -= 1
    with pytest.raises(Problem, match="different contents"):
        execute(db, changed)
    with pytest.raises(Problem, match="Refresh"):
        execute(db, {**command, "key": uuid.uuid4().hex})
    with pytest.raises(Problem) as error:
        run(db, "associate_transfer_component", **request(db, ids, 1))
    assert error.value.code == "association_capacity"
    replacement = run(
        db,
        "associate_transfer_component",
        **request(db, ids, 2500, supersedes_id=result["association"]["id"]),
    )
    assert replacement["remaining_unassociated_cents"][str(ids[1])] == 300
    reversed_link = run(
        db,
        "reverse_transfer_component",
        withdrawal=reference(db, ids[0]),
        deposit=reference(db, ids[1]),
        association_id=replacement["association"]["id"],
        evidence="Corrected source evidence",
    )
    assert reversed_link["remaining_unassociated_cents"] == {
        str(ids[0]): 2600,
        str(ids[1]): 2800,
    }
    assert len(observation_state(db, ids[0])["transfer_associations"]) == 3
    with pytest.raises(Problem, match="already reversed"):
        run(
            db,
            "reverse_transfer_component",
            withdrawal=reference(db, ids[0]),
            deposit=reference(db, ids[1]),
            association_id=replacement["association"]["id"],
            evidence="Duplicate reversal",
        )
    assert financial_snapshot(db) == before


@pytest.mark.parametrize("many_to_one", [False, True])
def test_multiple_components_share_capacity(portfolio, many_to_one):
    db = portfolio[0]
    first, second = pair(portfolio, 500, 1000), pair(portfolio, 1000, 500)
    pairs = (
        [[first[0], first[1]], [second[0], first[1]]]
        if many_to_one
        else [[second[0], first[1]], [second[0], second[1]]]
    )
    before = financial_snapshot(db)
    for ids in pairs:
        run(db, "associate_transfer_component", **request(db, ids, 500))
    with pytest.raises(Problem) as error:
        run(db, "associate_transfer_component", **request(db, pairs[-1], 1))
    assert error.value.code == "association_capacity"
    assert financial_snapshot(db) == before


@pytest.mark.parametrize("amount", [0, -1, 2601, 2801])
def test_invalid_or_excess_capacity_is_rejected(portfolio, amount):
    db = portfolio[0]
    ids = pair(portfolio)
    with pytest.raises((ValidationError, Problem)):
        run(db, "associate_transfer_component", **request(db, ids, amount))
    assert db.query(m.TransferAssociation).count() == 0


def test_legacy_whole_link_and_components_cannot_reuse_sources(portfolio):
    db = portfolio[0]
    ids = pair(portfolio, 2600, 2600)
    run(
        db,
        "link_classified_transfer",
        withdrawal=reference(db, ids[0]),
        deposit=reference(db, ids[1]),
        evidence="Exact whole transfer",
    )
    with pytest.raises(Problem) as error:
        run(db, "associate_transfer_component", **request(db, ids, 1))
    assert error.value.code == "association_capacity"
    other = pair(portfolio, 2600, 2600)
    run(db, "associate_transfer_component", **request(db, other, 100))
    with pytest.raises(Problem, match="component associations"):
        run(
            db,
            "link_classified_transfer",
            withdrawal=reference(db, other[0]),
            deposit=reference(db, other[1]),
            evidence="Whole reuse",
        )


def test_bank_itemization_requires_registered_hash(portfolio):
    db = portfolio[0]
    ids = pair(portfolio)
    data = request(db, ids)
    data["evidence_kind"] = "bank_itemized"
    with pytest.raises(Problem, match="registered source"):
        run(db, "associate_transfer_component", **data)
    proof = run(
        db,
        "attach_document",
        code="ITEMIZED",
        title="Deposit itemization",
        locator="/synthetic/deposit.pdf",
        sha256="a" * 64,
    )
    data["source_documents"] = [
        dict(
            document_id=proof["document_id"],
            sha256="a" * 64,
            explanation="Exact deposited check",
        )
    ]
    result = run(db, "associate_transfer_component", **data)
    assert (
        result["association"]["evidence"]["source_documents"]
        == data["source_documents"]
    )


def test_concurrent_component_claims_cannot_overdraw_source(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from app.database import Base, enable_sqlite_tuning
    from app.portfolio.guards import install
    from app.portfolio.routes import serialize_write
    from tests.test_balancedbooks_portfolio import entity

    engine = create_engine(
        f"sqlite:///{tmp_path/'claims.db'}", connect_args={"check_same_thread": False}
    )
    enable_sqlite_tuning(engine)
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        install(connection)
    factory = sessionmaker(bind=engine, autoflush=False)
    with factory() as db:
        owner = entity(db, "CONCURRENT")
        bank = account(db, owner, "bank", "asset", "bank")
        expense = account(db, owner, "expense", "expense", "expense")
        income = account(db, owner, "income", "income", "income")
        ids = pair((db, owner, bank, expense, income), 1000, 1000)
        payload = request(db, ids, 700)
        db.commit()

    def claim(number):
        with factory() as db:
            try:
                serialize_write(db)
                result = execute(
                    db,
                    {
                        "key": f"claim/{number}",
                        "operation": "associate_transfer_component",
                        "data": payload,
                    },
                )
                db.commit()
                return result["association"]["amount_cents"]
            except Problem as exc:
                db.rollback()
                return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, [1, 2]))
    assert sorted(results, key=str) == sorted([700, "stale_state"], key=str)
    with factory() as db:
        assert db.query(m.TransferAssociation).count() == 1
    engine.dispose()


def test_component_migration_repeatable_and_nonempty_downgrade_refused(
    portfolio, monkeypatch
):
    import importlib

    db = portfolio[0]
    ids = pair(portfolio)
    run(db, "associate_transfer_component", **request(db, ids))
    migration = importlib.import_module(
        "migrations.versions.bb0005_transfer_associations"
    )
    monkeypatch.setattr(migration.op, "get_bind", lambda: db.connection())
    migration.upgrade()
    migration.upgrade()
    with pytest.raises(RuntimeError, match="cannot be discarded"):
        migration.downgrade()
    assert db.query(m.TransferAssociation).count() == 1
