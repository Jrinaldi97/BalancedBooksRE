"""Bank-only rails, migration and metadata edits preserve accounting."""

import importlib
import uuid
from datetime import date
import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from app.portfolio import models as m, routes, schemas
from app.portfolio.imports import parse_bank
from app.portfolio.payment_rails import PAYMENT_RAILS
from app.portfolio.service import Problem, execute, balance
from tests import test_balancedbooks_portfolio as fixtures
from tests.test_balancedbooks_portfolio import run, bank_rows, account

portfolio = fixtures.portfolio
EXPECTED = [
    "Cash",
    "Check",
    "Internal-Bank",
    "ACH",
    "Wire",
    "Zelle",
    "Debit Card",
    "FedNow",
    "RTP",
    "Other/Unknown",
]


def test_taxonomy_context_schema(portfolio):
    db, *_ = portfolio
    assert PAYMENT_RAILS == EXPECTED
    assert [r["value"] for r in routes.context(db)["payment_rails"]] == EXPECTED
    assert (
        schemas.PaymentRailInput.model_json_schema()["properties"]["payment_rail"][
            "enum"
        ]
        == EXPECTED
    )
    assert "set_payment_rail" in routes.commands()


def test_import_defaults_options_and_nonbank_exclusion(portfolio):
    db, e, bank, *_ = portfolio
    ids = bank_rows(
        db,
        bank,
        [
            dict(date="2026-02-01", amount_cents=i + 1, payment_rail=rail)
            for i, rail in enumerate(EXPECTED)
        ],
    )
    assert [db.get(m.BankObservation, i).payment_rail for i in ids] == EXPECTED
    ident = bank_rows(
        db, bank, [dict(date="2026-02-02", amount_cents=100)], "default.csv"
    )[0]
    assert db.get(m.BankObservation, ident).payment_rail == "Other/Unknown"
    card = account(db, e, "card", "liability", "credit_card")
    cid = bank_rows(db, card, [dict(date="2026-02-01", amount_cents=-100)], "card.csv")[
        0
    ]
    assert db.get(m.BankObservation, cid).payment_rail is None
    with pytest.raises(Problem, match="only to bank"):
        bank_rows(
            db,
            card,
            [dict(date="2026-02-02", amount_cents=-100, payment_rail="ACH")],
            "bad-card.csv",
        )
    with pytest.raises(Problem, match="only to bank"):
        run(
            db,
            "set_payment_rail",
            observation_id=cid,
            payment_rail="Debit Card",
            expected_payment_rail="Other/Unknown",
            evidence="test",
        )
    for invalid in ["Credit Card", "ach", "", None]:
        with pytest.raises(ValidationError):
            run(
                db,
                "set_payment_rail",
                observation_id=ident,
                payment_rail=invalid,
                expected_payment_rail="Other/Unknown",
                evidence="test",
            )


def test_edit_posted_closed_inactive_is_audited_retryable_and_nonfinancial(portfolio):
    db, e, bank, expense, _ = portfolio
    ident = bank_rows(db, bank, [dict(date="2026-02-01", amount_cents=-100)])[0]
    run(
        db,
        "post_bank",
        observation_id=ident,
        offset_account_id=expense,
        rationale="test evidence",
    )
    obs = db.get(m.BankObservation, ident)
    event_id, cash = obs.event_id, balance(db, bank)
    counts = (db.query(m.Event).count(), db.query(m.ReviewItem).count())
    db.get(m.Entity, e).closed_through = date(2026, 2, 28)
    from app.models.accounts import Account

    db.get(Account, bank).is_active = False
    command = dict(
        operation="set_payment_rail",
        key=uuid.uuid4().hex,
        data=dict(
            observation_id=ident,
            payment_rail="Check",
            expected_payment_rail="Other/Unknown",
            evidence="Check image",
        ),
    )
    result = execute(db, command, actor="reviewer")
    assert result["financial_effects"] is False
    assert result["previous_payment_rail"] == "Other/Unknown"
    assert result["actor"] == "reviewer"
    assert execute(db, command)["replayed"]
    assert obs.event_id == event_id and obs.payment_rail == "Check"
    assert balance(db, bank) == cash
    assert counts == (db.query(m.Event).count(), db.query(m.ReviewItem).count())
    assert db.get(m.RequestRecord, command["key"]).result["evidence"] == "Check image"
    with pytest.raises(Problem, match="refresh"):
        run(db, "set_payment_rail", **command["data"])


def test_reimport_preserves_existing_rail(portfolio):
    db, _, bank, *_ = portfolio
    rows = [dict(date="2026-02-01", amount_cents=-100)]
    ident = bank_rows(db, bank, rows)[0]
    run(
        db,
        "set_payment_rail",
        observation_id=ident,
        payment_rail="ACH",
        expected_payment_rail="Other/Unknown",
        evidence="Bank detail",
    )
    assert bank_rows(db, bank, rows) == []
    with pytest.raises(Problem, match="set_payment_rail"):
        bank_rows(db, bank, [{**rows[0], "payment_rail": "Check"}])
    assert db.get(m.BankObservation, ident).payment_rail == "ACH"


def test_parser_exact_column():
    data = b"Date,Amount,Rail\n2026-02-01,10,Wire\n"
    mapping = dict(date="Date", amount="Amount", payment_rail="Rail")
    assert parse_bank(data, "bank.csv", mapping)["rows"][0]["payment_rail"] == "Wire"
    with pytest.raises(Problem, match="exact Payment Rail"):
        parse_bank(data.replace(b"Wire", b"wire"), "bank.csv", mapping)
    assert (
        "payment_rail"
        not in parse_bank(data, "bank.csv", dict(date="Date", amount="Amount"))["rows"][
            0
        ]
    )


def test_migration_and_database_guards():
    engine = create_engine("sqlite://")
    migration = importlib.import_module("migrations.versions.bb0007_payment_rails")
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "CREATE TABLE re_account_scopes (account_id INTEGER PRIMARY KEY, role TEXT)"
        )
        conn.exec_driver_sql(
            "INSERT INTO re_account_scopes VALUES (1,'bank'),(2,'credit_card'),(3,'expense')"
        )
        conn.exec_driver_sql(
            "CREATE TABLE re_bank_observations (id INTEGER PRIMARY KEY, account_id INTEGER, amount_cents INTEGER)"
        )
        conn.exec_driver_sql(
            "INSERT INTO re_bank_observations VALUES (1,1,100),(2,2,-50),(3,3,10)"
        )
        with Operations.context(MigrationContext.configure(conn)):
            migration.upgrade()
            migration.upgrade()
        assert conn.exec_driver_sql(
            "SELECT payment_rail FROM re_bank_observations ORDER BY id"
        ).fetchall() == [("Other/Unknown",), (None,), (None,)]
        for sql in [
            "UPDATE re_bank_observations SET payment_rail=NULL WHERE id=1",
            "UPDATE re_bank_observations SET payment_rail='ACH' WHERE id=2",
            "UPDATE re_bank_observations SET payment_rail='bad' WHERE id=1",
            "INSERT INTO re_bank_observations VALUES (4,2,10,'ACH')",
        ]:
            with pytest.raises(IntegrityError):
                conn.exec_driver_sql(sql)
        conn.exec_driver_sql(
            "INSERT INTO re_bank_observations (id,account_id,amount_cents) VALUES (4,1,20)"
        )
        assert (
            conn.exec_driver_sql(
                "SELECT payment_rail FROM re_bank_observations WHERE id=4"
            ).scalar()
            == "Other/Unknown"
        )
        assert (
            conn.exec_driver_sql(
                "SELECT SUM(amount_cents) FROM re_bank_observations WHERE id<=3"
            ).scalar()
            == 60
        )


def test_http_preview_commit_and_state_visibility(client, portfolio):
    db, _, bank, *_ = portfolio
    ident = bank_rows(db, bank, [dict(date="2026-02-01", amount_cents=-100)])[0]
    db.commit()
    payload = dict(
        operation="set_payment_rail",
        key=uuid.uuid4().hex,
        data=dict(
            observation_id=ident,
            payment_rail="Zelle",
            expected_payment_rail="Other/Unknown",
            evidence="Bank export detail",
        ),
    )
    preview = client.post("/api/portfolio/proposals", json=payload)
    assert preview.status_code == 200, preview.text
    proposal = preview.json()
    db.expire_all()
    assert db.get(m.BankObservation, ident).payment_rail == "Other/Unknown"
    saved = client.post(
        f"/api/portfolio/proposals/{proposal['proposal_id']}/commit",
        json={"digest": proposal["digest"]},
    )
    assert saved.status_code == 200, saved.text
    state = client.get(
        f"/api/portfolio/observations/{ident}/state", params={"projection": "compact"}
    ).json()
    assert state["observation"]["payment_rail"] == "Zelle"
    listed = client.get(
        "/api/portfolio/records/observations", params={"account_id": bank}
    ).json()
    assert listed["rows"][0]["payment_rail"] == "Zelle"
