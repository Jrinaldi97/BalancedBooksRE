"""Supplier invoice timing, partial payments and deposits in one card operation."""

from copy import deepcopy
from datetime import date
import uuid

import pytest

from app.portfolio import models as m
from app.portfolio.card_invoices import invoice_state
from app.portfolio.service import Problem, control, balance, execute
from app.portfolio.reports import ledger, statements
from tests.test_balancedbooks_portfolio import account, run
from tests.test_portfolio_card_workflows import issuer_rows, reference
from tests import test_portfolio_card_workflows as fixtures

cards = fixtures.cards


def invoice_component(
    cards, code="BILL-1", amount=10000, gross=10500, discount=500, beneficiary=False
):
    db = cards["db"]
    proof = run(
        db,
        "attach_document",
        code=code,
        title="Synthetic supplier invoice",
        locator="/synthetic/invoice.pdf",
        sha256="d" * 64,
    )
    return dict(
        key=code,
        amount_cents=amount,
        beneficiary_kind="entity" if beneficiary else "company",
        entity_id=cards["borrower"] if beneficiary else None,
        account_id=cards["borrower_expense"] if beneficiary else cards["expense"],
        recognition_policy="invoice_date",
        evidence="Completed utility service and actual earned discount",
        invoice=dict(
            code=code,
            supplier_id=cards["party"],
            invoice_date="2026-01-20",
            service_start="2025-12-20",
            service_end="2026-01-19",
            gross_cents=gross,
            earned_discount_cents=discount,
            forfeited_discount_cents=100,
            source=dict(
                document_id=proof["document_id"],
                sha256="d" * 64,
                explanation="Actual invoice; only earned discount deducted",
            ),
        ),
    )


def pay(cards, components, day="2026-02-01"):
    db = cards["db"]
    code = uuid.uuid4().hex[:16]
    obs = issuer_rows(
        cards,
        [
            dict(
                date=day,
                amount_cents=-sum(row["amount_cents"] for row in components),
                external_id=code,
            )
        ],
        name=code + ".csv",
    )["observation_ids"][0]
    run(
        db,
        "post_bank",
        observation_id=obs,
        rationale="Original issuer debit",
        duplicate_resolution="distinct",
    )
    original = db.get(m.BankObservation, obs).event_id
    command = dict(
        operation="classify_card_purchase",
        key=uuid.uuid4().hex,
        data=dict(
            code=code,
            card_id=cards["card"],
            observation=reference(db, obs),
            components=components,
            evidence="Actual issuer settlement",
        ),
    )
    result = execute(db, command)
    assert db.get(m.BankObservation, obs).event_id == original
    assert execute(db, command)["replayed"]
    return result


def expense(db, ident, through, basis):
    return sum(
        row["debit_cents"] - row["credit_cents"]
        for row in ledger(db, through, account_id=ident, basis=basis)
    )


@pytest.mark.parametrize("beneficiary", [False, True])
def test_invoice_accrual_and_card_cash_dates_with_paid_item(cards, beneficiary):
    db = cards["db"]
    component = invoice_component(cards, beneficiary=beneficiary)
    result = pay(cards, [component])
    owner = cards["borrower"] if beneficiary else cards["holder"]
    cost = cards["borrower_expense"] if beneficiary else cards["expense"]
    assert len(result["event_ids"]) == 2
    assert invoice_state(db, "BILL-1")["remaining_cents"] == 0
    assert balance(db, control(db, owner, "payable"), date(2026, 1, 31)) == -10000
    assert balance(db, control(db, owner, "payable")) == 0
    assert expense(db, cost, date(2026, 1, 31), "accrual") == 10000
    assert expense(db, cost, date(2026, 1, 31), "modified_cash_bank_posting") == 0
    assert expense(db, cost, date(2026, 2, 28), "modified_cash_bank_posting") == 10000
    if beneficiary:
        assert balance(db, control(db, cards["holder"], "due_from")) == 10000
        assert balance(db, control(db, owner, "due_to")) == -10000
    for basis in ("accrual", "modified_cash_bank_posting"):
        for end in (date(2026, 1, 31), date(2026, 2, 28)):
            report = statements(db, date(2026, 1, 1), end, [owner], basis=basis)
            assert all(value == 0 for value in report["checks"].values())


def test_multiple_invoices_and_funded_deposit_are_atomic(cards):
    db = cards["db"]
    asset = account(db, cards["holder"], "supplier-deposit", "asset", "asset")
    components = [
        invoice_component(cards, "BILL-A", 900, 900, 0),
        invoice_component(cards, "BILL-B", 1000, 1100, 100),
        dict(
            key="deposit",
            amount_cents=30000,
            beneficiary_kind="company",
            account_id=asset,
            recognition_policy="funded_deposit",
            evidence="Deposit actually funded",
        ),
    ]
    result = pay(cards, components)
    assert len(result["event_ids"]) == 3
    assert balance(db, asset, date(2026, 1, 31)) == 0
    assert balance(db, asset, date(2026, 2, 28)) == 30000
    assert balance(db, cards["expense"]) == 1900
    assert all(
        invoice_state(db, code)["remaining_cents"] == 0 for code in ("BILL-A", "BILL-B")
    )


def test_partial_invoice_requires_fresh_guard_and_keeps_one_item(cards):
    db = cards["db"]
    part = invoice_component(cards, amount=4000)
    pay(cards, [part])
    state = invoice_state(db, "BILL-1")
    assert state["remaining_cents"] == 6000
    assert state["effective_payable_cents"] == {
        "accrual": 6000,
        "modified_cash_bank_posting": 0,
    }
    second = deepcopy(part)
    second["amount_cents"] = 6000
    with pytest.raises(Problem, match="Read invoice_state"):
        pay(cards, [second], "2026-02-10")
    second["invoice"]["expected_invoice_digest"] = state["state_digest"]
    pay(cards, [second], "2026-02-10")
    assert db.query(m.OpenItem).filter_by(code="BILL-1").count() == 1
    assert invoice_state(db, "BILL-1")["remaining_cents"] == 0
    assert balance(db, cards["expense"]) == 10000


def test_refund_reverses_paid_cost_without_reopening_supplier_invoice(cards):
    db = cards["db"]
    result = pay(cards, [invoice_component(cards)])
    obs = issuer_rows(
        cards,
        [dict(date="2026-03-01", amount_cents=2000, external_id="refund")],
        name="refund.csv",
    )["observation_ids"][0]
    run(
        db,
        "record_card_refund",
        code="REFUND",
        card_id=cards["card"],
        observation=reference(db, obs),
        purchase_id=result["card_activity_id"],
        components=[dict(key="BILL-1", amount_cents=2000)],
        evidence="Supplier refunds paid cost; no unpaid invoice change",
    )
    assert invoice_state(db, "BILL-1")["remaining_cents"] == 0
    for basis in ("accrual", "modified_cash_bank_posting"):
        assert expense(db, cards["expense"], date(2026, 3, 31), basis) == 8000


@pytest.mark.parametrize(
    "variant", ["bad_hash", "overpay", "future_invoice", "deposit_expense"]
)
def test_bad_timing_or_evidence_rolls_back_all_invoice_effects(cards, variant):
    db = cards["db"]
    part = invoice_component(cards)
    if variant == "bad_hash":
        part["invoice"]["source"]["sha256"] = "e" * 64
    elif variant == "overpay":
        part["amount_cents"] = 10001
    elif variant == "future_invoice":
        part["invoice"]["invoice_date"] = "2026-03-01"
    else:
        part["recognition_policy"] = "funded_deposit"
        part.pop("invoice")
    with pytest.raises(Problem):
        pay(cards, [part])
    assert not invoice_state(db, "BILL-1")["exists"]
    assert balance(db, cards["expense"]) == 0
