"""Applied payment corrections retain original cash and require recorded debt."""

from copy import deepcopy
from datetime import date
import uuid

import pytest

from app.portfolio import models as m
from app.portfolio.mortgages import mortgage_state
from app.portfolio.observation_state import observation_state
from app.portfolio.reports import statements
from app.portfolio.service import Problem, balance, control, execute, values
from tests.test_balancedbooks_portfolio import account, bank_rows, journal, ln, run
from tests import test_balancedbooks_portfolio as fixtures

portfolio = fixtures.portfolio


def payment_case(portfolio, opening=500000):
    db, owner, bank, expense, _ = portfolio
    debt = account(db, owner, "mortgage", "liability", "debt")
    escrow = account(db, owner, "escrow", "asset", "escrow")
    if opening:
        journal(
            db,
            owner,
            "2026-01-01",
            [
                ln(control(db, owner, "equity"), opening),
                ln(debt, -opening, item="LOAN-1"),
            ],
        )
    obs = bank_rows(db, bank, [dict(date="2026-02-02", amount_cents=-20000)])[0]
    run(db, "post_bank", observation_id=obs, rationale="Actual payment")
    state = observation_state(db, obs)
    proof = run(
        db,
        "attach_document",
        code="APPLICATION",
        title="Applied payment",
        locator="/synthetic/payment.pdf",
        sha256="c" * 64,
    )
    data = dict(
        observation_id=obs,
        review_id=next(r["id"] for r in state["reviews"] if r["code"] == "suspense"),
        expected_state_digest=state["state_digest"],
        debt_account_id=debt,
        expected_debt_digest=mortgage_state(db, obs, debt, "LOAN-1")["state_digest"],
        instrument="LOAN-1",
        interest_account_id=expense,
        escrow_account_id=escrow,
        principal_cents=10000,
        interest_cents=7000,
        escrow_cents=3000,
        application_date="2026-02-01",
        application_kind="actual_payment_applied",
        lender_evidence=dict(
            document_id=proof["document_id"],
            sha256="c" * 64,
            explanation="Applied row, not future schedule",
        ),
        evidence="Exact lender application and source movement",
    )
    return data, escrow


def test_posted_payment_preserves_source_and_balances_both_bases(portfolio):
    db, owner, bank, _, _ = portfolio
    data, escrow = payment_case(portfolio)
    obs = db.get(m.BankObservation, data["observation_id"])
    before = deepcopy(values(obs))
    original = deepcopy(values(db.get(m.Event, obs.event_id)))
    bank_before = balance(db, bank)
    command = dict(
        operation="correct_posted_mortgage_payment", key=uuid.uuid4().hex, data=data
    )
    result = execute(db, command)
    assert result["bank_cash_delta_cents"] == 0
    assert result["resolved_review_ids"] == [data["review_id"]]
    assert balance(db, data["debt_account_id"]) == -490000
    assert balance(db, escrow) == 3000
    assert values(obs) == before
    assert values(db.get(m.Event, obs.event_id)) == original
    assert balance(db, bank) == bank_before
    assert execute(db, command)["replayed"]
    for basis in ("accrual", "modified_cash_bank_posting"):
        report = statements(
            db, date(2026, 2, 1), date(2026, 2, 28), [owner], basis=basis
        )
        assert report["income_statement"]["net_income_cents"] == -7000
        assert report["cash_flows"]["sections_cents"] == {
            "financing": -10000,
            "operating": -10000,
        }
        assert all(value == 0 for value in report["checks"].values())


def test_partial_payment_leaves_principal_open(portfolio):
    db = portfolio[0]
    data, escrow = payment_case(portfolio, opening=0)
    data["principal_cents"] = 0
    result = run(db, "correct_posted_mortgage_payment", **data)
    assert result["resolved_review_ids"] == []
    assert result["remaining_cents"] == -10000
    assert db.get(m.ReviewItem, data["review_id"]).status == "open"
    assert balance(db, escrow) == 3000


@pytest.mark.parametrize(
    "variant",
    [
        "no_debt",
        "exceeds_debt",
        "wrong_instrument",
        "stale_source",
        "stale_debt",
        "wrong_hash",
        "closed",
        "future_application",
    ],
)
def test_mortgage_rejects_unsupported_evidence_or_state(portfolio, variant):
    db, owner, bank, _, _ = portfolio
    data, _ = payment_case(
        portfolio,
        opening=(
            0 if variant == "no_debt" else 5000 if variant == "exceeds_debt" else 500000
        ),
    )
    if variant == "wrong_instrument":
        data["instrument"] = "DIFFERENT"
        data["expected_debt_digest"] = mortgage_state(
            db, data["observation_id"], data["debt_account_id"], "DIFFERENT"
        )["state_digest"]
    elif variant == "stale_source":
        data["expected_state_digest"] = "0" * 64
    elif variant == "stale_debt":
        journal(
            db,
            owner,
            "2026-01-02",
            [
                ln(control(db, owner, "equity"), 100),
                ln(data["debt_account_id"], -100, item="LOAN-1"),
            ],
        )
    elif variant == "wrong_hash":
        data["lender_evidence"]["sha256"] = "b" * 64
    elif variant == "closed":
        db.get(m.Entity, owner).closed_through = date(2026, 2, 2)
        db.flush()
    elif variant == "future_application":
        data["application_date"] = "2026-03-01"
    before = balance(db, bank), db.query(m.Event).count()
    with pytest.raises(Problem):
        run(db, "correct_posted_mortgage_payment", **data)
    assert (balance(db, bank), db.query(m.Event).count()) == before


@pytest.mark.parametrize(
    "allocation_day, allowed",
    [("2026-02-01", True), ("2026-02-03", False), ("2026-03-01", False)],
)
def test_future_debt_allocation_cannot_supply_a_past_opening(
    portfolio, allocation_day, allowed
):
    from app.portfolio.reports import ledger

    db, owner, _, _, _ = portfolio
    data, _ = payment_case(portfolio, opening=0)
    origin = journal(
        db,
        owner,
        "2026-01-01",
        [
            ln(data["debt_account_id"], -10000, item="OTHER-LOAN"),
            ln(control(db, owner, "equity"), 10000),
        ],
    )
    row = next(
        r
        for r in ledger(db, date.max, event_ids=[origin["event_id"]])
        if r["account_id"] == data["debt_account_id"]
    )
    db.add(
        m.LineAllocation(
            line_id=row["line_id"],
            event_id=origin["event_id"],
            date=date.fromisoformat(allocation_day),
            components=[
                dict(
                    property_id=None,
                    unit_id=None,
                    fund_id=None,
                    counterparty_id=None,
                    cash_flow=row["cash_flow"],
                    item="LOAN-1",
                    amount_cents=-10000,
                )
            ],
            evidence="Synthetic dated instrument attribution",
        )
    )
    db.flush()
    state = mortgage_state(
        db, data["observation_id"], data["debt_account_id"], "LOAN-1"
    )
    assert state["supported_liability_cents"]["accrual"] == (10000 if allowed else 0)
    data["expected_debt_digest"] = state["state_digest"]
    if allowed:
        run(db, "correct_posted_mortgage_payment", **data)
    else:
        with pytest.raises(Problem, match="supported debt"):
            run(db, "correct_posted_mortgage_payment", **data)
