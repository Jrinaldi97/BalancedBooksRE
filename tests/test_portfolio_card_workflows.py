"""Card issuer movements are liabilities, never synthetic bank cash or repeat costs."""

import hashlib
import uuid
from datetime import date
import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from app.portfolio import models as m
from app.portfolio.service import Problem, balance, control, execute
from app.portfolio.reports import ledger
from app.portfolio.observation_state import observation_state
from app.portfolio.card_workflows import card_state
from tests.test_balancedbooks_portfolio import (
    run,
    account,
    entity,
    bank_rows,
    journal,
    ln,
)


def reference(db, observation_id):
    return {
        "observation_id": observation_id,
        "expected_state_digest": observation_state(db, observation_id)["state_digest"],
    }


@pytest.fixture
def cards(db_session):
    db = db_session
    holder = entity(db, "CARDCO")
    bank = account(db, holder, "checking", "asset", "bank", coverage_start="2026-01-01")
    card_account = account(
        db, holder, "amex", "liability", "credit_card", coverage_start="2026-01-01"
    )
    expense = account(db, holder, "repairs", "expense", "expense")
    borrower = entity(db, "BENEFICIARY")
    borrower_expense = account(db, borrower, "repairs", "expense", "expense")
    party = run(db, "create_party", code="CLIENT", name="External client")["party_id"]
    journal(
        db,
        holder,
        "2026-01-01",
        [ln(bank, 100000), ln(control(db, holder, "equity"), -100000)],
        kind="opening",
    )
    card = run(
        db,
        "register_card_identity",
        code="RPC-AMEX",
        account_id=card_account,
        entity_id=holder,
        issuer="Amex",
        evidence="Verified company card ownership",
    )["card_id"]
    run(
        db,
        "add_card_alias",
        card_id=card,
        alias="72006",
        start="2026-01-01",
        evidence="Historical card number",
    )
    return dict(
        db=db,
        holder=holder,
        bank=bank,
        card_account=card_account,
        expense=expense,
        borrower=borrower,
        borrower_expense=borrower_expense,
        party=party,
        card=card,
    )


def issuer_rows(c, rows, alias="72006", name="issuer.csv"):
    return run(
        c["db"],
        "import_card_activity",
        card_id=c["card"],
        alias=alias,
        source_name=name,
        source_sha256=hashlib.sha256(name.encode()).hexdigest(),
        rows=[{"activity_status": "posted", **r} for r in rows],
    )


def purchase(c, posted=False, components=None):
    db = c["db"]
    obs = issuer_rows(
        c, [{"date": "2026-02-01", "amount_cents": -10000, "external_id": "charge-1"}]
    )["observation_ids"][0]
    if posted:
        run(db, "post_bank", observation_id=obs, rationale="Original issuer debit")
    original_event = db.get(m.BankObservation, obs).event_id
    result = run(
        db,
        "classify_card_purchase",
        code="PURCHASE",
        card_id=c["card"],
        observation=reference(db, obs),
        components=components
        or [
            {
                "key": "own",
                "amount_cents": 6000,
                "beneficiary_kind": "company",
                "account_id": c["expense"],
                "evidence": "Own-company invoice",
            },
            {
                "key": "entity",
                "amount_cents": 3000,
                "beneficiary_kind": "entity",
                "entity_id": c["borrower"],
                "account_id": c["borrower_expense"],
                "evidence": "Beneficiary invoice",
            },
            {
                "key": "party",
                "amount_cents": 1000,
                "beneficiary_kind": "external_party",
                "party_id": c["party"],
                "evidence": "Actual client's reimbursable charge",
            },
        ],
        evidence="Three exact invoice components",
    )
    return obs, original_event, result


def basis_total(db, account_id, basis, through=date(2026, 12, 31)):
    return sum(
        r["debit_cents"] - r["credit_cents"]
        for r in ledger(db, through, account_id=account_id, basis=basis)
    )


def payment(
    c,
    *,
    amount=10000,
    bank_date="2026-02-05",
    issuer_date="2026-02-08",
    code="PAYMENT",
    posted=False,
    operation="match_card_repayment",
    **extra
):
    db = c["db"]
    sign = -1 if operation == "return_card_repayment" else 1
    bank = bank_rows(
        db,
        c["bank"],
        [{"date": bank_date, "amount_cents": -sign * amount}],
        name=code + ".csv",
    )[0]
    issuer = issuer_rows(
        c,
        [{"date": issuer_date, "amount_cents": sign * amount, "external_id": code}],
        name=code + "-issuer.csv",
    )["observation_ids"][0]
    if posted:
        for obs in (bank, issuer):
            run(
                db,
                "post_bank",
                observation_id=obs,
                rationale="Original actual settlement",
            )
    data = dict(
        code=code,
        card_id=c["card"],
        bank_observation=reference(db, bank),
        issuer_observation=reference(db, issuer),
        item=extra.pop("item", code),
        evidence="Exact issuer/bank payment references",
        **extra
    )
    return run(db, operation, **data), (bank, issuer), data


@pytest.mark.parametrize("posted", [False, True])
def test_card_cost_components_preserve_original_liability_and_both_bases(cards, posted):
    db = cards["db"]
    obs, original, result = purchase(cards, posted)
    for basis in ("accrual", "modified_cash_bank_posting"):
        assert basis_total(db, cards["bank"], basis) == 100000
        assert basis_total(db, cards["card_account"], basis) == -10000
        assert basis_total(db, cards["expense"], basis) == 6000
        assert basis_total(db, cards["borrower_expense"], basis) == 3000
        assert basis_total(db, control(db, cards["holder"], "due_from"), basis) == 3000
        assert basis_total(db, control(db, cards["borrower"], "due_to"), basis) == -3000
        assert (
            basis_total(db, control(db, cards["holder"], "receivable"), basis) == 1000
        )
    state = observation_state(db, obs)
    assert state["bank_movements"] == []
    assert [r for r in state["source_movements"] if r["kind"] == "card_liability"] == [
        {
            "kind": "card_liability",
            "event_id": original or result["event_id"],
            "account_id": cards["card_account"],
            "date": "2026-02-01",
            "amount_cents": -10000,
        }
    ]
    assert not any(state["suspense_residuals"].values())
    if posted:
        assert db.get(m.BankObservation, obs).event_id == original
        assert result["resolved_review_ids"]
    assert result["bank_cash_delta_cents"] == 0


def test_partial_refunds_reverse_only_original_components(cards):
    db = cards["db"]
    _, _, charge = purchase(cards, posted=True)
    obs = issuer_rows(
        cards, [{"date": "2026-02-03", "amount_cents": 1700, "external_id": "refund"}]
    )["observation_ids"][0]
    run(db, "post_bank", observation_id=obs, rationale="Original issuer refund credit")
    result = run(
        db,
        "record_card_refund",
        code="REFUND",
        card_id=cards["card"],
        observation=reference(db, obs),
        purchase_id=charge["card_activity_id"],
        components=[
            {"key": "own", "amount_cents": 1000},
            {"key": "entity", "amount_cents": 500},
            {"key": "party", "amount_cents": 200},
        ],
        evidence="Partial refunds by original invoice",
    )
    assert result["refunded_cents"] == 1700
    for basis in ("accrual", "modified_cash_bank_posting"):
        assert basis_total(db, cards["expense"], basis) == 5000
        assert basis_total(db, cards["borrower_expense"], basis) == 2500
        assert basis_total(db, cards["card_account"], basis) == -8300
        assert basis_total(db, cards["bank"], basis) == 100000
    item = db.query(m.OpenItem).one()
    assert (
        item.amount_cents
        - sum(
            a.amount_cents
            for a in db.query(m.ItemApplication).filter_by(item_id=item.id)
        )
        == 800
    )
    over = issuer_rows(
        cards,
        [{"date": "2026-02-04", "amount_cents": 5001, "external_id": "over-refund"}],
    )["observation_ids"][0]
    events = db.query(m.Event).count()
    with pytest.raises(Problem) as error:
        run(
            db,
            "record_card_refund",
            code="OVER",
            card_id=cards["card"],
            observation=reference(db, over),
            purchase_id=charge["card_activity_id"],
            components=[{"key": "own", "amount_cents": 5001}],
            evidence="Invalid excess refund",
        )
    assert error.value.code == "card_refund_limit"
    assert db.query(m.Event).count() == events
    assert db.get(m.BankObservation, over).event_id is None


@pytest.mark.parametrize("posted", [False, True])
def test_repayment_uses_actual_separate_dates_and_never_repeats_cost(cards, posted):
    db = cards["db"]
    purchase(cards, posted=True)
    result, (bank, issuer), _ = payment(cards, posted=posted)
    assert result["bank_date"] == "2026-02-05"
    assert result["issuer_date"] == "2026-02-08"
    assert result["expense_delta_cents"] == 0
    assert result["issuer_balance_certified_by_payment"] is False
    assert result["open_coverage_review_ids"]
    assert (
        balance(
            db, control(db, cards["holder"], "bridge"), date(2026, 2, 6), item="PAYMENT"
        )
        == 10000
    )
    assert (
        balance(
            db, control(db, cards["holder"], "bridge"), date(2026, 2, 8), item="PAYMENT"
        )
        == 0
    )
    for basis in ("accrual", "modified_cash_bank_posting"):
        assert basis_total(db, cards["bank"], basis) == 90000
        assert basis_total(db, cards["card_account"], basis) == 0
        assert basis_total(db, cards["expense"], basis) == 6000
        assert basis_total(db, cards["borrower_expense"], basis) == 3000
    assert not any(observation_state(db, bank)["suspense_residuals"].values())
    assert not any(observation_state(db, issuer)["suspense_residuals"].values())
    assert result["bank_cash_delta_cents"] == (0 if posted else -10000)


def test_card_return_and_retry_preserve_costs_and_original_source_history(cards):
    db = cards["db"]
    purchase(cards)
    paid, _, _ = payment(cards)
    returned, _, _ = payment(
        cards,
        code="RETURN",
        amount=3000,
        bank_date="2026-02-10",
        issuer_date="2026-02-11",
        operation="return_card_repayment",
        original_payment_id=paid["card_activity_id"],
        item="PAYMENT",
    )
    assert balance(db, cards["bank"]) == 93000
    assert balance(db, cards["card_account"]) == -3000
    retried, _, data = payment(
        cards,
        code="RETRY",
        amount=3000,
        bank_date="2026-02-13",
        issuer_date="2026-02-14",
        retry_of_return_id=returned["card_activity_id"],
    )
    assert balance(db, cards["bank"]) == 90000
    assert balance(db, cards["card_account"]) == 0
    assert balance(db, cards["expense"]) == 6000
    assert db.query(m.CardActivity).count() == 4
    with pytest.raises(Problem) as error:
        run(db, "match_card_repayment", **{**data, "code": "DUPLICATE"})
    assert error.value.code == "card_source_used"
    assert retried["expense_delta_cents"] == 0


@pytest.mark.parametrize("status", ["pending", "declined"])
def test_pending_and_declined_rows_never_enter_posted_issuer_inventory(cards, status):
    db = cards["db"]
    with pytest.raises(Problem) as error:
        issuer_rows(
            cards,
            [
                {"date": "2026-02-01", "amount_cents": -100, "external_id": "valid"},
                {
                    "date": "2026-02-01",
                    "amount_cents": -200,
                    "external_id": "invalid",
                    "activity_status": status,
                },
            ],
        )
    assert error.value.code == "card_activity_status"
    assert db.query(m.BankObservation).count() == 0


def test_number_change_aliases_share_source_identity_without_merging_another_card(
    cards,
):
    db = cards["db"]
    run(
        db,
        "add_card_alias",
        card_id=cards["card"],
        alias="73004",
        start="2026-01-01",
        evidence="Verified same Amex account after number change",
    )
    rows = [{"date": "2026-02-01", "amount_cents": -100, "external_id": "same-source"}]
    original = issuer_rows(cards, rows)
    duplicate = issuer_rows(cards, rows, alias="73004", name="new-number.csv")
    assert duplicate["observation_ids"] == []
    assert duplicate["duplicate_ids"] == original["observation_ids"]
    other_account = account(
        db, cards["borrower"], "other-card", "liability", "credit_card"
    )
    other_card = run(
        db,
        "register_card_identity",
        code="RF-AMEX",
        account_id=other_account,
        entity_id=cards["borrower"],
        issuer="Amex",
        evidence="Separate real card",
    )["card_id"]
    with pytest.raises(Problem) as error:
        run(
            db,
            "add_card_alias",
            card_id=other_card,
            alias="73004",
            start="2026-01-01",
            evidence="Conflicting alias",
        )
    assert error.value.code == "card_alias"
    assert card_state(db, cards["card"], date(2026, 2, 28))["coverage_current"] is False


def test_alias_dates_and_wrong_identity_reject(cards):
    run(
        cards["db"],
        "add_card_alias",
        card_id=cards["card"],
        alias="NEW",
        start="2026-03-01",
        evidence="New number effective March",
    )
    with pytest.raises(Problem) as error:
        issuer_rows(
            cards,
            [{"date": "2026-02-01", "amount_cents": -100, "external_id": "old"}],
            alias="NEW",
        )
    assert error.value.code == "card_alias"


def coverage_data(c, opening=0):
    db = c["db"]
    doc = run(
        db,
        "attach_document",
        code="ISSUERSTATEMENT",
        title="Issuer statement",
        locator="file:///synthetic/issuer.pdf",
        sha256="a" * 64,
    )["document_id"]
    return dict(
        card_id=c["card"],
        expected_state_digest=card_state(db, c["card"], date(2026, 2, 28))[
            "state_digest"
        ],
        start="2026-01-01",
        end="2026-02-28",
        opening_liability_cents=opening,
        ending_liability_cents=-balance(db, c["card_account"], date(2026, 2, 28)),
        observation_ids=[
            o.id
            for o in db.query(m.BankObservation).filter_by(account_id=c["card_account"])
        ],
        document_id=doc,
        sha256="a" * 64,
        evidence="Complete posted issuer statement and supported opening",
    )


def test_issuer_coverage_is_inventory_and_opening_proof_not_alias_or_payment(cards):
    db = cards["db"]
    purchase(cards)
    payment(cards)
    data = coverage_data(cards)
    result = run(db, "record_card_coverage", **data)
    state = card_state(db, cards["card"], date(2026, 2, 28))
    assert state["coverage_current"] is True
    assert state["coverage_ids"] == [result["card_coverage_id"]]
    assert card_state(db, cards["card"], date(2026, 2, 15))["coverage_current"] is False
    extra = issuer_rows(
        cards,
        [
            {
                "date": "2026-02-20",
                "amount_cents": -100,
                "external_id": "late-genuine-charge",
            }
        ],
    )["observation_ids"][0]
    assert card_state(db, cards["card"], date(2026, 2, 28))["invalid_coverage_ids"] == [
        result["card_coverage_id"]
    ]
    assert db.get(m.BankObservation, extra).event_id is None


@pytest.mark.parametrize("fault", ["opening", "missing", "extra", "ending", "hash"])
def test_incomplete_or_wrong_issuer_coverage_rejects(cards, fault):
    purchase(cards)
    data = coverage_data(cards)
    if fault == "opening":
        data["opening_liability_cents"] = 5000
    if fault == "missing":
        data["observation_ids"] = []
    if fault == "extra":
        data["observation_ids"].append(99999)
    if fault == "ending":
        data["ending_liability_cents"] += 1
    if fault == "hash":
        data["sha256"] = "b" * 64
    with pytest.raises(Problem):
        run(cards["db"], "record_card_coverage", **data)
    assert cards["db"].query(m.CardCoverage).count() == 0


def test_payment_second_leg_failure_rolls_back_first_leg(cards):
    db = cards["db"]
    purchase(cards)
    bank_obs = bank_rows(
        db, cards["bank"], [{"date": "2026-02-08", "amount_cents": -10000}]
    )[0]
    issuer_obs = issuer_rows(
        cards, [{"date": "2026-02-05", "amount_cents": 10000, "external_id": "payment"}]
    )["observation_ids"][0]
    db.get(m.Entity, cards["holder"]).closed_through = date(2026, 2, 6)
    db.flush()
    before = db.query(m.Event).count()
    with pytest.raises(Problem) as error:
        run(
            db,
            "match_card_repayment",
            code="FAIL",
            card_id=cards["card"],
            bank_observation=reference(db, bank_obs),
            issuer_observation=reference(db, issuer_obs),
            item="FAIL",
            evidence="Actual payment, closed issuer period",
        )
    assert error.value.code == "closed_period"
    assert db.query(m.Event).count() == before
    assert balance(db, cards["bank"]) == 100000
    assert db.get(m.BankObservation, bank_obs).event_id is None
    assert db.get(m.BankObservation, issuer_obs).event_id is None


def test_typed_purchase_request_replay_is_stable(cards):
    db = cards["db"]
    obs = issuer_rows(
        cards, [{"date": "2026-02-01", "amount_cents": -100, "external_id": "small"}]
    )["observation_ids"][0]
    command = {
        "operation": "classify_card_purchase",
        "key": uuid.uuid4().hex,
        "data": {
            "code": "SMALL",
            "card_id": cards["card"],
            "observation": reference(db, obs),
            "components": [
                {
                    "key": "only",
                    "amount_cents": 100,
                    "beneficiary_kind": "company",
                    "account_id": cards["expense"],
                    "evidence": "Exact invoice",
                }
            ],
            "evidence": "Exact source",
        },
    }
    first = execute(db, command)
    count = db.query(m.Event).count()
    assert execute(db, command)["replayed"]
    assert db.query(m.Event).count() == count
    assert db.query(m.CardActivity).one().id == first["card_activity_id"]


def test_card_evidence_tables_are_immutable(cards):
    from app.portfolio.card_models import install_card_guards

    db = cards["db"]
    purchase(cards)
    install_card_guards(db.connection())
    for query in [
        "UPDATE re_card_identities SET issuer='changed'",
        "DELETE FROM re_card_aliases",
        "UPDATE re_card_activities SET amount_cents=1",
    ]:
        with pytest.raises(IntegrityError), db.begin_nested():
            db.execute(text(query))


@pytest.mark.parametrize("fault", ["company", "card", "amount", "client_fund"])
def test_wrong_repayment_source_company_card_amount_and_fund_reject(cards, fault):
    db = cards["db"]
    purchase(cards)
    bank_account = cards["bank"]
    if fault == "company":
        bank_account = account(db, cards["borrower"], "foreign-bank", "asset", "bank")
    bank_obs = bank_rows(
        db, bank_account, [{"date": "2026-02-05", "amount_cents": -10000}]
    )[0]
    card_obs = issuer_rows(
        cards,
        [
            {
                "date": "2026-02-08",
                "amount_cents": 9999 if fault == "amount" else 10000,
                "external_id": "wrong-source",
            }
        ],
    )["observation_ids"][0]
    card_id = cards["card"]
    extra = {}
    if fault == "card":
        other_account = account(
            db, cards["holder"], "another-card", "liability", "credit_card"
        )
        card_id = run(
            db,
            "register_card_identity",
            code="OTHER",
            account_id=other_account,
            entity_id=cards["holder"],
            issuer="Amex",
            evidence="Separate actual issuer account",
        )["card_id"]
    if fault == "client_fund":
        extra["bank_fund_id"] = run(
            db,
            "create_fund",
            code="CLIENT-CASH",
            holder_entity_id=cards["holder"],
            beneficiary_id=cards["party"],
            purpose="general",
        )["fund_id"]
    before = db.query(m.Event).count()
    with pytest.raises(Problem):
        run(
            db,
            "match_card_repayment",
            code="INVALID",
            card_id=card_id,
            bank_observation=reference(db, bank_obs),
            issuer_observation=reference(db, card_obs),
            item="INVALID",
            evidence="Wrong mapping",
            **extra
        )
    assert db.query(m.Event).count() == before
    assert db.get(m.BankObservation, bank_obs).event_id is None
    assert db.get(m.BankObservation, card_obs).event_id is None


@pytest.mark.parametrize("fault", ["return_limit", "wrong_item", "retry_limit"])
def test_returns_and_retries_cannot_exceed_or_change_original_payment(cards, fault):
    db = cards["db"]
    purchase(cards)
    paid, _, _ = payment(cards)
    if fault == "retry_limit":
        returned, _, _ = payment(
            cards,
            amount=3000,
            code="RETURN",
            bank_date="2026-02-10",
            issuer_date="2026-02-11",
            operation="return_card_repayment",
            original_payment_id=paid["card_activity_id"],
            item="PAYMENT",
        )
        options = dict(
            amount=3001,
            code="BAD-RETRY",
            bank_date="2026-02-15",
            issuer_date="2026-02-16",
            retry_of_return_id=returned["card_activity_id"],
        )
    else:
        options = dict(
            amount=10001 if fault == "return_limit" else 1000,
            code="BAD-RETURN",
            bank_date="2026-02-10",
            issuer_date="2026-02-11",
            operation="return_card_repayment",
            original_payment_id=paid["card_activity_id"],
            item="UNRELATED" if fault == "wrong_item" else "PAYMENT",
        )
    events = db.query(m.Event).count()
    with pytest.raises(Problem) as error:
        payment(cards, **options)
    assert (
        error.value.code
        == {
            "return_limit": "card_return_limit",
            "wrong_item": "card_return",
            "retry_limit": "card_retry_limit",
        }[fault]
    )
    assert db.query(m.Event).count() == events


def test_already_classified_card_cost_is_not_posted_again(cards):
    db = cards["db"]
    obs = issuer_rows(
        cards,
        [
            {
                "date": "2026-02-01",
                "amount_cents": -10000,
                "external_id": "already-classified",
            }
        ],
    )["observation_ids"][0]
    run(
        db,
        "post_bank",
        observation_id=obs,
        offset_account_id=cards["expense"],
        rationale="Existing valid card expense",
    )
    before = db.query(m.Event).count()
    with pytest.raises(Problem) as error:
        run(
            db,
            "classify_card_purchase",
            code="NO-DOUBLE-COST",
            card_id=cards["card"],
            observation=reference(db, obs),
            components=[
                {
                    "key": "own",
                    "amount_cents": 10000,
                    "beneficiary_kind": "company",
                    "account_id": cards["expense"],
                    "evidence": "Previously classified charge",
                }
            ],
            evidence="No remaining suspense",
        )
    assert error.value.code == "card_source_residual"
    assert db.query(m.Event).count() == before
    assert balance(db, cards["expense"]) == 10000


def test_full_property_unit_item_vectors_match_on_both_bases(cards):
    from collections import defaultdict

    db = cards["db"]
    prop = run(db, "create_property", code="BUILDING", name="Building")["property_id"]
    unit = run(db, "create_unit", property_id=prop, code="1")["unit_id"]
    obs, original, result = purchase(
        cards,
        posted=True,
        components=[
            {
                "key": "unit",
                "amount_cents": 10000,
                "beneficiary_kind": "company",
                "account_id": cards["expense"],
                "property_id": prop,
                "unit_id": unit,
                "evidence": "Invoice identifies exact property and unit",
            }
        ],
    )
    for basis in ("accrual", "modified_cash_bank_posting"):
        totals = defaultdict(int)
        for row in ledger(
            db,
            date(2026, 2, 28),
            basis=basis,
            event_ids=observation_state(db, obs)["event_ids"],
        ):
            totals[
                (
                    row["entity_id"],
                    row["account_id"],
                    row["property_id"],
                    row["unit_id"],
                    row["item"],
                )
            ] += (
                row["debit_cents"] - row["credit_cents"]
            )
        assert {key: amount for key, amount in totals.items() if amount} == {
            (
                cards["holder"],
                cards["card_account"],
                prop,
                unit,
                "card:PURCHASE:unit",
            ): -10000,
            (
                cards["holder"],
                cards["expense"],
                prop,
                unit,
                "card:PURCHASE:unit",
            ): 10000,
        }
    assert db.get(m.BankObservation, obs).event_id == original
    assert result["original_liability_preserved"] is True


def test_external_refund_cannot_reverse_already_reimbursed_debt(cards):
    db = cards["db"]
    _, _, charge = purchase(cards)
    item = db.query(m.OpenItem).one()
    reimbursement = bank_rows(
        db, cards["bank"], [{"date": "2026-02-03", "amount_cents": 1000}]
    )[0]
    run(
        db,
        "settle",
        item_id=item.id,
        observation_id=reimbursement,
        evidence="Actual client reimbursement",
    )
    refund = issuer_rows(
        cards,
        [{"date": "2026-02-04", "amount_cents": 100, "external_id": "refund-settled"}],
    )["observation_ids"][0]
    with pytest.raises(Problem) as error:
        run(
            db,
            "record_card_refund",
            code="SETTLED-REFUND",
            card_id=cards["card"],
            observation=reference(db, refund),
            purchase_id=charge["card_activity_id"],
            components=[{"key": "party", "amount_cents": 100}],
            evidence="Client obligation was already repaid",
        )
    assert error.value.code == "card_refund_settled"
    assert db.get(m.BankObservation, refund).event_id is None


def test_coverage_revision_requires_fresh_state_and_preserves_old_statement(cards):
    db = cards["db"]
    purchase(cards)
    payment_result, _, _ = payment(cards)
    data = coverage_data(cards)
    first = run(db, "record_card_coverage", **data)
    assert first["resolved_review_ids"] == payment_result["open_coverage_review_ids"]
    original = db.get(m.CardCoverage, first["card_coverage_id"])
    saved_inventory = list(original.observation_ids)
    extra = issuer_rows(
        cards,
        [{"date": "2026-02-20", "amount_cents": -100, "external_id": "later-import"}],
    )["observation_ids"][0]
    run(
        db,
        "classify_card_purchase",
        code="LATER",
        card_id=cards["card"],
        observation=reference(db, extra),
        components=[
            {
                "key": "own",
                "amount_cents": 100,
                "beneficiary_kind": "company",
                "account_id": cards["expense"],
                "evidence": "Genuine late-imported purchase",
            }
        ],
        evidence="Complete source export",
    )
    with pytest.raises(Problem) as error:
        run(db, "record_card_coverage", **{**data, "supersedes_id": original.id})
    assert error.value.code == "stale_state"
    revised = {
        **data,
        "supersedes_id": original.id,
        "ending_liability_cents": 100,
        "observation_ids": saved_inventory + [extra],
        "expected_state_digest": card_state(db, cards["card"], date(2026, 2, 28))[
            "state_digest"
        ],
    }
    second = run(db, "record_card_coverage", **revised)
    assert original.observation_ids == saved_inventory
    assert original.ending_liability_cents == 0
    state = card_state(db, cards["card"], date(2026, 2, 28))
    assert state["coverage_current"] is True
    assert state["coverage_ids"] == [second["card_coverage_id"]]
    with pytest.raises(Problem) as error:
        run(
            db,
            "record_card_coverage",
            **{**revised, "expected_state_digest": state["state_digest"]}
        )
    assert error.value.code == "card_coverage_revision"


@pytest.mark.parametrize("basis", ["accrual", "modified_cash_bank_posting"])
def test_net_zero_unobserved_card_activity_cannot_certify_coverage(cards, basis):
    db = cards["db"]
    for day, amount in (("2026-02-03", -100), ("2026-02-04", 100)):
        lines = [ln(cards["card_account"], amount), ln(cards["expense"], -amount)]
        if basis == "accrual":
            journal(db, cards["holder"], day, lines)
        else:
            journal(
                db,
                cards["holder"],
                day,
                [
                    ln(cards["expense"], 1),
                    ln(control(db, cards["holder"], "equity"), -1),
                ],
                adjustments=[
                    {
                        "entity_id": cards["holder"],
                        "date": day,
                        "lines": lines,
                        "reason": "Unobserved card basis movement",
                    }
                ],
            )
    assert basis_total(db, cards["card_account"], basis) == 0
    data = coverage_data(cards)
    assert data["opening_liability_cents"] == data["ending_liability_cents"] == 0
    assert data["observation_ids"] == []
    with pytest.raises(Problem) as error:
        run(db, "record_card_coverage", **data)
    assert error.value.code == "card_coverage_difference"
    assert db.query(m.CardCoverage).count() == 0
    assert card_state(db, cards["card"], date(2026, 2, 28))["coverage_current"] is False


def test_explicit_start_opening_is_covered_but_later_unobserved_opening_is_not(cards):
    db = cards["db"]
    journal(
        db,
        cards["holder"],
        "2026-01-01",
        [
            ln(cards["card_account"], -500),
            ln(control(db, cards["holder"], "equity"), 500),
        ],
        kind="opening",
    )
    data = coverage_data(cards, opening=500)
    first = run(db, "record_card_coverage", **data)
    assert card_state(db, cards["card"], date(2026, 2, 28))["coverage_current"] is True
    journal(
        db,
        cards["holder"],
        "2026-02-02",
        [
            ln(cards["card_account"], -100),
            ln(control(db, cards["holder"], "equity"), 100),
        ],
        kind="opening",
    )
    journal(
        db,
        cards["holder"],
        "2026-02-04",
        [
            ln(cards["card_account"], 100),
            ln(control(db, cards["holder"], "equity"), -100),
        ],
        kind="opening",
    )
    state = card_state(db, cards["card"], date(2026, 2, 28))
    assert state["coverage_current"] is False
    assert state["invalid_coverage_ids"] == [first["card_coverage_id"]]


@pytest.mark.parametrize("settlement_scope", ["both", "holder", "beneficiary"])
@pytest.mark.parametrize("settlement_day", ["2026-02-02", "2026-02-10"])
def test_refund_preserves_both_counterparty_balances_through_later_recorded_history(
    cards, settlement_scope, settlement_day
):
    db = cards["db"]
    _, _, charge = purchase(cards)
    item = "card:PURCHASE:entity"
    scopes = [
        (cards["holder"], "due_from", cards["borrower"], -3000, "holder"),
        (cards["borrower"], "due_to", cards["holder"], 3000, "beneficiary"),
    ]
    journals = [
        {
            "entity_id": entity_id,
            "lines": [
                ln(
                    control(db, entity_id, role),
                    amount,
                    item=item,
                    counterparty_id=counterparty,
                ),
                ln(control(db, entity_id, "equity"), -amount),
            ],
        }
        for entity_id, role, counterparty, amount, scope in scopes
        if settlement_scope in {"both", scope}
    ]
    options = {}
    if settlement_scope != "both":
        # The generic posting primitive accepts basis workpapers independently.
        # Even one-sided legacy basis history must not be silently over-refunded.
        options["adjustments"] = [
            {
                **journal,
                "date": settlement_day,
                "reason": "Previously recorded basis workpaper",
            }
            for journal in journals
        ]
        journals = [
            {
                "entity_id": journal["entity_id"],
                "lines": [
                    ln(
                        (
                            cards["expense"]
                            if journal["entity_id"] == cards["holder"]
                            else cards["borrower_expense"]
                        ),
                        1,
                    ),
                    ln(control(db, journal["entity_id"], "equity"), -1),
                ],
            }
            for journal in journals
        ]
    run(
        db,
        "post",
        date=settlement_day,
        description="Previously recorded beneficiary settlement",
        rationale="Existing reciprocal application",
        journals=journals,
        **options
    )
    refund = issuer_rows(
        cards,
        [
            {
                "date": "2026-02-03",
                "amount_cents": 500,
                "external_id": "backdated-refund",
            }
        ],
    )["observation_ids"][0]
    events = db.query(m.Event).count()
    with pytest.raises(Problem) as error:
        run(
            db,
            "record_card_refund",
            code="BACKDATED-REFUND",
            card_id=cards["card"],
            observation=reference(db, refund),
            purchase_id=charge["card_activity_id"],
            components=[{"key": "entity", "amount_cents": 500}],
            evidence="Issuer refund imported after recorded settlement",
        )
    assert error.value.code == "card_refund_settled"
    assert db.get(m.BankObservation, refund).event_id is None
    assert db.query(m.Event).count() == events
    assert db.query(m.CardActivity).filter_by(kind="refund").count() == 0


def test_backdated_refund_keeps_later_partially_settled_reciprocal_balance_nonnegative(
    cards,
):
    db = cards["db"]
    _, _, charge = purchase(cards)
    item = "card:PURCHASE:entity"
    run(
        db,
        "post",
        date="2026-02-10",
        description="Partial beneficiary settlement",
        rationale="Exact earlier reimbursement",
        journals=[
            {
                "entity_id": entity_id,
                "lines": [
                    ln(
                        control(db, entity_id, role),
                        amount,
                        item=item,
                        counterparty_id=counterparty,
                    ),
                    ln(control(db, entity_id, "equity"), -amount),
                ],
            }
            for entity_id, role, counterparty, amount in (
                (cards["holder"], "due_from", cards["borrower"], -2000),
                (cards["borrower"], "due_to", cards["holder"], 2000),
            )
        ],
    )
    refund = issuer_rows(
        cards,
        [
            {
                "date": "2026-02-03",
                "amount_cents": 500,
                "external_id": "supported-backdated-refund",
            }
        ],
    )["observation_ids"][0]
    run(
        db,
        "record_card_refund",
        code="SUPPORTED",
        card_id=cards["card"],
        observation=reference(db, refund),
        purchase_id=charge["card_activity_id"],
        components=[{"key": "entity", "amount_cents": 500}],
        evidence="Refund leaves both original counterparties unoverpaid",
    )
    for basis in ("accrual", "modified_cash_bank_posting"):
        assert basis_total(db, control(db, cards["holder"], "due_from"), basis) == 500
        assert basis_total(db, control(db, cards["borrower"], "due_to"), basis) == -500


@pytest.mark.parametrize("allocation_day", ["2026-02-03", "2026-02-11", "2026-03-01"])
def test_refund_headroom_respects_allocation_effective_dates(cards, allocation_day):
    db = cards["db"]
    _, _, charge = purchase(cards)
    original_item = "card:PURCHASE:entity"
    scopes = (
        (cards["holder"], "due_from", cards["borrower"], 1),
        (cards["borrower"], "due_to", cards["holder"], -1),
    )
    unrelated = run(
        db,
        "post",
        date="2026-01-15",
        description="Separate reciprocal charge",
        rationale="This charge originally belongs to another item",
        journals=[
            {
                "entity_id": entity_id,
                "lines": [
                    ln(
                        control(db, entity_id, role),
                        sign * 1000,
                        item="unrelated",
                        counterparty_id=counterparty,
                    ),
                    ln(control(db, entity_id, "equity"), -sign * 1000),
                ],
            }
            for entity_id, role, counterparty, sign in scopes
        ],
    )
    run(
        db,
        "post",
        date="2026-02-10",
        description="Full original settlement",
        rationale="Beneficiary settled the exact original charge",
        journals=[
            {
                "entity_id": entity_id,
                "lines": [
                    ln(
                        control(db, entity_id, role),
                        -sign * 3000,
                        item=original_item,
                        counterparty_id=counterparty,
                    ),
                    ln(control(db, entity_id, "equity"), sign * 3000),
                ],
            }
            for entity_id, role, counterparty, sign in scopes
        ],
    )
    marker = journal(
        db,
        cards["holder"],
        allocation_day,
        [ln(cards["expense"], 1), ln(control(db, cards["holder"], "equity"), -1)],
    )
    for row in ledger(db, date.max, event_ids=[unrelated["event_id"]]):
        if row["role"] not in {"due_from", "due_to"}:
            continue
        db.add(
            m.LineAllocation(
                line_id=row["line_id"],
                event_id=marker["event_id"],
                date=date.fromisoformat(allocation_day),
                components=[
                    {
                        **{
                            key: row.get(key)
                            for key in (
                                "property_id",
                                "unit_id",
                                "fund_id",
                                "counterparty_id",
                                "cash_flow",
                            )
                        },
                        "item": original_item,
                        "amount_cents": row["debit_cents"] - row["credit_cents"],
                    }
                ],
                evidence="Allocation applies only from its explicit effective date",
            )
        )
    db.flush()
    due_from = control(db, cards["holder"], "due_from")
    supported = allocation_day == "2026-02-03"
    assert balance(db, due_from, date(2026, 2, 10), item=original_item) == (
        1000 if supported else 0
    )
    assert balance(db, due_from, date(2026, 3, 1), item=original_item) == 1000
    refund = issuer_rows(
        cards,
        [
            {
                "date": "2026-02-03",
                "amount_cents": 500,
                "external_id": "historical-allocation-refund",
            }
        ],
    )["observation_ids"][0]
    events = db.query(m.Event).count()
    data = dict(
        code="HISTORICAL-ALLOCATION",
        card_id=cards["card"],
        observation=reference(db, refund),
        purchase_id=charge["card_activity_id"],
        components=[{"key": "entity", "amount_cents": 500}],
        evidence="Late issuer refund cannot borrow future allocation",
    )
    if supported:
        run(db, "record_card_refund", **data)
        assert balance(db, due_from, date(2026, 2, 10), item=original_item) == 500
        assert db.get(m.BankObservation, refund).event_id is not None
    else:
        with pytest.raises(Problem) as error:
            run(db, "record_card_refund", **data)
        assert error.value.code == "card_refund_settled"
        assert db.query(m.Event).count() == events
        assert db.get(m.BankObservation, refund).event_id is None
        assert balance(db, due_from, date(2026, 2, 10), item=original_item) == 0
