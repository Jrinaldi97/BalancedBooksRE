"""Funding source is independent of established purpose; synthetic books only."""

import copy
from datetime import date
import uuid

import pytest

from app.portfolio import models as m
from app.portfolio.outgoing_funding import outgoing_funding_assessment
from app.portfolio.observation_state import observation_state
from app.portfolio.reports import ledger
from app.portfolio.service import execute, Problem, balance, control
from app.models.transactions import TransactionLine
from tests.test_balancedbooks_portfolio import (
    account,
    entity,
    run,
    bank_rows,
    journal,
    ln,
)


@pytest.fixture
def funded(db_session, request):
    db = db_session
    config = getattr(request, "param", {})
    e = entity(db, "FUNDING")
    bank = account(
        db, e, "bank", "asset", "bank", pooled=True, coverage_start="2026-01-01"
    )
    owner = db.query(m.Party).filter_by(entity_id=e).one().id
    debtor = run(
        db, "create_party", code="DEBTOR", name="Actual debtor", kind="individual"
    )["party_id"]
    unknown = run(
        db,
        "create_fund",
        code="UNKNOWN",
        holder_entity_id=e,
        beneficiary_id=owner,
        purpose="unresolved",
        restriction=config.get("restriction", "unrestricted"),
    )["fund_id"]
    own = run(
        db,
        "create_fund",
        code="OWN",
        holder_entity_id=e,
        beneficiary_id=owner,
        purpose="general",
    )["fund_id"]
    journal(
        db,
        e,
        "2026-01-01",
        [ln(bank, 10000, fund_id=own), ln(control(db, e, "equity"), -10000)],
        kind="opening",
    )
    obs = bank_rows(db, bank, [{"date": "2026-01-10", "amount_cents": -3000}])[0]
    run(
        db, "post_bank", observation_id=obs, fund_id=unknown, rationale="Unknown source"
    )
    state = observation_state(db, obs)
    review = next(r["id"] for r in state["reviews"] if r["code"] == "suspense")
    result = run(
        db,
        "correct_posted_party_balance",
        observation={
            "observation_id": obs,
            "review_id": review,
            "expected_state_digest": state["state_digest"],
        },
        party_id=debtor,
        code="LOAN",
        kind="party_receivable",
        date=config.get("correction_date", "2026-01-10"),
        evidence="Debtor and purpose proven",
    )
    return db, e, bank, unknown, own, obs, debtor, result


def command(funded, components=None):
    db, _, _, unknown, own, obs, debtor, result = funded
    assessment = outgoing_funding_assessment(db, obs)
    return {
        "operation": "resolve_outgoing_funding_source",
        "key": uuid.uuid4().hex,
        "data": {
            "review_id": result["open_funding_review_ids"][0],
            "observation_id": obs,
            "obligation_event_id": result["event_id"],
            "party_id": debtor,
            "item": "LOAN",
            "expected_state_digest": assessment["state_digest"],
            "expected_funding_digest": assessment["funding_digest"],
            "components": components
            or [
                {
                    "source_fund_id": unknown,
                    "own_fund_id": own,
                    "amount_cents": 3000,
                    "evidence": "Original company contribution and bank statement",
                }
            ],
            "evidence": "Verified exact source ownership for this withdrawal",
        },
    }


def test_outgoing_resolution_preserves_principal_and_physical_cash(funded):
    db, e, bank, _, own, obs, _, loan = funded
    cash_before = balance(db, bank)
    principal_before = balance(db, control(db, e, "receivable"))
    lines_before = db.query(TransactionLine).count()
    cmd = command(funded)
    result = execute(db, cmd)
    assert result["financial_entries_added"] is False
    assert result["remaining_cents"] == 0
    assert balance(db, bank) == cash_before == 7000
    assert balance(db, bank, fund_id=own) == 7000
    assert balance(db, control(db, e, "receivable")) == principal_before == 3000
    assert db.query(TransactionLine).count() == lines_before
    assert db.get(m.ReviewItem, loan["open_funding_review_ids"][0]).status == "resolved"
    assert execute(db, cmd)["replayed"] is True
    changed = copy.deepcopy(cmd)
    changed["data"]["evidence"] = "Changed evidence"
    with pytest.raises(Problem, match="retry key"):
        execute(db, changed)
    assert db.get(m.BankObservation, obs).amount_cents == -3000


def test_partial_evidence_keeps_remainder_then_completes(funded):
    db, _, _, unknown, own, _, _, _ = funded
    result = execute(
        db,
        command(
            funded,
            [
                {
                    "source_fund_id": unknown,
                    "own_fund_id": own,
                    "amount_cents": 1000,
                    "evidence": "Supported part",
                },
                {
                    "source_fund_id": unknown,
                    "amount_cents": 2000,
                    "evidence": "Still unknown",
                },
            ],
        ),
    )
    assert result["remaining_cents"] == 2000
    assert not result["resolved_review_ids"]
    result = execute(
        db,
        command(
            funded,
            [
                {
                    "source_fund_id": own,
                    "own_fund_id": own,
                    "amount_cents": 1000,
                    "evidence": "Prior source attestation",
                },
                {
                    "source_fund_id": unknown,
                    "own_fund_id": own,
                    "amount_cents": 2000,
                    "evidence": "Remaining source proven",
                },
            ],
        ),
    )
    assert result["remaining_cents"] == 0


@pytest.mark.parametrize(
    "change,code",
    [
        ({"party_id": 99999}, "funding_party"),
        ({"item": "OTHER"}, "funding_item"),
        ({"expected_state_digest": "0" * 64}, "stale_state"),
        ({"expected_funding_digest": "0" * 64}, "stale_funding"),
    ],
)
def test_wrong_identity_and_stale_state_reject(funded, change, code):
    db = funded[0]
    cmd = command(funded)
    cmd["data"].update(change)
    events = db.query(m.Event).count()
    with pytest.raises(Problem) as error:
        execute(db, cmd)
    assert error.value.code == code
    assert db.query(m.Event).count() == events


def test_insufficient_fund_at_future_boundary_rolls_back(funded):
    db, e, bank, _, own, _, _, _ = funded
    journal(
        db,
        e,
        "2026-02-01",
        [ln(bank, -8000, fund_id=own), ln(control(db, e, "equity"), 8000)],
        observed=True,
    )
    cmd = command(funded)
    events = db.query(m.Event).count()
    allocations = db.query(m.LineAllocation).count()
    with pytest.raises(Problem) as error:
        execute(db, cmd)
    assert error.value.code == "funding_availability"
    assert db.query(m.Event).count() == events
    assert db.query(m.LineAllocation).count() == allocations
    assert db.get(m.ReviewItem, cmd["data"]["review_id"]).status == "open"


def test_client_fund_and_mismatched_totals_reject(funded):
    db, e, _, unknown, _, _, debtor, _ = funded
    client = run(
        db,
        "create_fund",
        code="CLIENT",
        holder_entity_id=e,
        beneficiary_id=debtor,
        purpose="general",
    )["fund_id"]
    cmd = command(
        funded,
        [
            {
                "source_fund_id": unknown,
                "own_fund_id": client,
                "amount_cents": 3000,
                "evidence": "Unsupported client financing",
            }
        ],
    )
    with pytest.raises(Problem) as error:
        execute(db, cmd)
    assert error.value.code == "funding_owner"
    cmd = command(funded)
    cmd["data"]["components"][0]["amount_cents"] = 2999
    with pytest.raises(Problem) as error:
        execute(db, cmd)
    assert error.value.code == "funding_total"


def test_availability_change_outside_observation_invalidates_funding_digest(funded):
    db, e, bank, _, own, _, _, _ = funded
    cmd = command(funded)
    journal(
        db,
        e,
        "2026-03-01",
        [ln(bank, -100, fund_id=own), ln(control(db, e, "equity"), 100)],
    )
    with pytest.raises(Problem) as error:
        execute(db, cmd)
    assert error.value.code == "stale_funding"


def test_evidence_only_does_not_replace_existing_attribution(funded):
    db, _, bank, unknown, own, _, _, _ = funded
    # First record a partial attestation; the second can evidence the existing
    # own attribution without changing that component or generating principal.
    execute(
        db,
        command(
            funded,
            [
                {
                    "source_fund_id": unknown,
                    "own_fund_id": own,
                    "amount_cents": 3000,
                    "evidence": "Source attribution supplied",
                },
            ],
        ),
    )
    # Independent source review for the same typed obligation is possible.
    original = funded[-1]
    review = m.ReviewItem(
        code="funding_source",
        entity_id=funded[1],
        observation_id=funded[5],
        event_id=original["event_id"],
        detail="Evidence verification",
    )
    db.add(review)
    db.flush()
    values = (*funded[:-1], {**original, "open_funding_review_ids": [review.id]})
    count = db.query(m.LineAllocation).count()
    result = execute(
        db,
        command(
            values,
            [
                {
                    "source_fund_id": own,
                    "own_fund_id": own,
                    "amount_cents": 3000,
                    "evidence": "Existing company source confirmed",
                }
            ],
        ),
    )
    assert result["cash_attribution_unchanged"] is True
    assert db.query(m.LineAllocation).count() == count
    assert balance(db, bank) == 7000


def test_failed_atomic_batch_rolls_back_attestation_and_review(funded):
    db = funded[0]
    cmd = command(funded)
    events = db.query(m.Event).count()
    with pytest.raises(Problem):
        execute(
            db,
            {
                "operation": "batch",
                "key": uuid.uuid4().hex,
                "data": {
                    "operations": [
                        {"operation": cmd["operation"], "data": cmd["data"]},
                        {
                            "operation": "create_account",
                            "data": {
                                "entity_id": 99999,
                                "code": "invalid",
                                "name": "invalid",
                                "account_type": "asset",
                                "role": "bank",
                            },
                        },
                    ]
                },
            },
        )
    assert db.query(m.Event).count() == events
    assert db.query(m.LineAllocation).count() == 0
    assert db.get(m.ReviewItem, cmd["data"]["review_id"]).status == "open"


def test_intercompany_source_resolves_only_with_exact_reciprocal_item(db_session):
    db = db_session
    lender, borrower = entity(db, "LENDER"), entity(db, "BORROWER")
    banks = [
        account(db, e, "BANK", "asset", "bank", pooled=True) for e in (lender, borrower)
    ]
    own, unknown = [], []
    for e in (lender, borrower):
        party = db.query(m.Party).filter_by(entity_id=e).one().id
        own.append(
            run(
                db,
                "create_fund",
                code=f"OWN-{e}",
                holder_entity_id=e,
                beneficiary_id=party,
                purpose="general",
            )["fund_id"]
        )
        unknown.append(
            run(
                db,
                "create_fund",
                code=f"UNKNOWN-{e}",
                holder_entity_id=e,
                beneficiary_id=party,
                purpose="unresolved",
            )["fund_id"]
        )
    journal(
        db,
        lender,
        "2026-01-01",
        [
            ln(banks[0], 10000, fund_id=own[0]),
            ln(control(db, lender, "equity"), -10000),
        ],
        kind="opening",
    )
    legs = []
    for index in range(2):
        obs = bank_rows(
            db,
            banks[index],
            [{"date": "2026-01-10", "amount_cents": (-3000, 3000)[index]}],
            name=f"bank-{index}.csv",
        )[0]
        run(
            db,
            "post_bank",
            observation_id=obs,
            fund_id=unknown[index],
            rationale="Unknown source",
        )
        state = observation_state(db, obs)
        legs.append(
            {
                "observation_id": obs,
                "review_id": next(
                    r["id"] for r in state["reviews"] if r["code"] == "suspense"
                ),
                "expected_state_digest": state["state_digest"],
            }
        )
    result = run(
        db,
        "correct_posted_intercompany",
        withdrawal=legs[0],
        deposit=legs[1],
        purpose="advance",
        item="ADVANCE",
        date="2026-01-10",
        evidence="Exact bank transfer evidence",
    )
    assessment = outgoing_funding_assessment(db, legs[0]["observation_id"])
    cmd = {
        "operation": "resolve_outgoing_funding_source",
        "key": uuid.uuid4().hex,
        "data": {
            "observation_id": legs[0]["observation_id"],
            "review_id": result["open_funding_review_ids"][0],
            "obligation_event_id": result["event_id"],
            "counterparty_entity_id": borrower,
            "item": "ADVANCE",
            "expected_state_digest": assessment["state_digest"],
            "expected_funding_digest": assessment["funding_digest"],
            "components": [
                {
                    "source_fund_id": unknown[0],
                    "own_fund_id": own[0],
                    "amount_cents": 3000,
                    "evidence": "Lender's own contributed cash",
                }
            ],
            "evidence": "Exact outgoing source proved",
        },
    }
    wrong = copy.deepcopy(cmd)
    wrong["data"]["counterparty_entity_id"] = lender
    with pytest.raises(Problem) as error:
        execute(db, wrong)
    assert error.value.code == "funding_party"
    assert execute(db, cmd)["resolved_review_ids"] == [
        result["open_funding_review_ids"][0]
    ]
    assert db.get(m.ReviewItem, result["open_funding_review_ids"][1]).status == "open"
    assert balance(db, control(db, lender, "due_from")) == 3000
    assert balance(db, control(db, borrower, "due_to")) == -3000


@pytest.mark.parametrize("funded", [{"restriction": "restricted"}], indirect=True)
def test_unresolved_restriction_cannot_be_released_by_funding_attestation(funded):
    db = funded[0]
    cmd = command(funded)
    events = db.query(m.Event).count()
    with pytest.raises(Problem) as error:
        execute(db, cmd)
    assert error.value.code == "client_financing"
    assert db.query(m.Event).count() == events
    assert db.query(m.LineAllocation).count() == 0
    assert db.get(m.ReviewItem, cmd["data"]["review_id"]).status == "open"


@pytest.mark.parametrize("funded", [{"correction_date": "2026-02-01"}], indirect=True)
def test_source_attribution_preserves_later_cash_flow_classification_dates(funded):
    db, _, bank, _, _, _, _, _ = funded
    cutoffs = (date(2026, 1, 10), date(2026, 1, 31), date(2026, 2, 1))

    def projections():
        return {
            (cutoff, basis): [
                {
                    key: row.get(key)
                    for key in (
                        "account_id",
                        "date",
                        "property_id",
                        "unit_id",
                        "item",
                        "counterparty_id",
                        "debit_cents",
                        "credit_cents",
                        "cash_flow",
                    )
                }
                for row in ledger(db, cutoff, account_id=bank, basis=basis)
            ]
            for cutoff in cutoffs
            for basis in ("accrual", "modified_cash_bank_posting")
        }

    before = projections()
    execute(db, command(funded))
    assert projections() == before
    assert before[(cutoffs[0], "accrual")][-1]["cash_flow"] is None
    assert before[(cutoffs[-1], "accrual")][-1]["cash_flow"] == "investing"


def test_original_obligation_basis_override_cannot_certify_the_old_purpose(funded):
    db, e, _, _, _, _, _, loan = funded
    expense = account(db, e, "EXPENSE", "expense", "expense")
    db.add(
        m.BasisAdjustment(
            event_id=loan["event_id"],
            entity_id=e,
            date=date(2026, 1, 10),
            reason="Synthetic purpose difference",
            lines=[
                ln(control(db, e, "receivable"), -3000, item="LOAN"),
                ln(expense, 3000),
            ],
        )
    )
    db.flush()
    with pytest.raises(Problem) as error:
        execute(db, command(funded))
    assert error.value.code == "funding_purpose"


def test_later_purpose_reversal_requires_reassessment(funded):
    db, e, _, _, _, _, _, loan = funded
    expense = account(db, e, "EXPENSE", "expense", "expense")
    journal(
        db,
        e,
        "2026-02-01",
        [ln(control(db, e, "receivable"), -3000, item="LOAN"), ln(expense, 3000)],
        corrects_id=loan["event_id"],
    )
    with pytest.raises(Problem) as error:
        execute(db, command(funded))
    assert error.value.code == "funding_purpose"


def test_actual_repayment_does_not_invalidate_historical_source(funded):
    db, e, bank, _, own, _, _, loan = funded
    receipt = bank_rows(
        db, bank, [{"date": "2026-02-01", "amount_cents": 1000}], name="repayment.csv"
    )[0]
    run(
        db,
        "settle",
        item_id=loan["item_id"],
        observation_id=receipt,
        fund_id=own,
        evidence="Synthetic actual repayment",
    )
    principal = balance(db, control(db, e, "receivable"))
    assert principal == 2000
    execute(db, command(funded))
    assert balance(db, control(db, e, "receivable")) == principal


def test_bank_basis_adjustment_invalidates_funding_snapshot_and_rejects_fresh_attempt(
    funded,
):
    db, e, bank, _, own, _, _, loan = funded
    cmd = command(funded)
    # Independent adjustment outside this observation family is also part of
    # the availability dependency. Its date is not a transaction boundary.
    unrelated = journal(
        db,
        e,
        "2026-01-15",
        [ln(control(db, e, "equity"), 1), ln(control(db, e, "equity"), -1)],
    )
    db.add(
        m.BasisAdjustment(
            event_id=unrelated["event_id"],
            entity_id=e,
            date=date(2026, 2, 15),
            reason="Synthetic bank basis contradiction",
            lines=[ln(bank, -8000, fund_id=own), ln(control(db, e, "equity"), 8000)],
        )
    )
    db.flush()
    with pytest.raises(Problem) as error:
        execute(db, cmd)
    assert error.value.code == "stale_funding"
    events, allocations = db.query(m.Event).count(), db.query(m.LineAllocation).count()
    with pytest.raises(Problem) as error:
        execute(db, command(funded))
    assert error.value.code == "funding_basis"
    assert db.query(m.Event).count() == events
    assert db.query(m.LineAllocation).count() == allocations
    assert db.get(m.ReviewItem, loan["open_funding_review_ids"][0]).status == "open"


def test_partial_source_cannot_choose_between_different_existing_dimensions(funded):
    db, _, bank, unknown, own, obs_id, _, _ = funded
    prop = run(db, "create_property", code="PARTIAL", name="Synthetic other component")[
        "property_id"
    ]
    obs = db.get(m.BankObservation, obs_id)
    physical = next(
        row
        for row in ledger(db, date.max, event_ids=[obs.event_id])
        if row["account_id"] == bank
    )
    db.add(
        m.LineAllocation(
            line_id=physical["line_id"],
            event_id=obs.event_id,
            date=obs.date,
            evidence="Synthetic original dimensional components",
            components=[
                dict(amount_cents=-1500, fund_id=unknown, property_id=None),
                dict(amount_cents=-1500, fund_id=unknown, property_id=prop),
            ],
        )
    )
    db.flush()
    cmd = command(
        funded,
        [
            dict(
                source_fund_id=unknown,
                own_fund_id=own,
                amount_cents=1000,
                evidence="Amount only",
            ),
            dict(
                source_fund_id=unknown,
                amount_cents=2000,
                evidence="Unresolved remainder",
            ),
        ],
    )
    with pytest.raises(Problem) as error:
        execute(db, cmd)
    assert error.value.code == "funding_scope"
    assert db.query(m.LineAllocation).count() == 1
