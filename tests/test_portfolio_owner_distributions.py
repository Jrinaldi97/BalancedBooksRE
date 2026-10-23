"""Atomic owner equity/custody workflows on synthetic books only."""

from copy import deepcopy
from datetime import date
import uuid

import pytest

from app.models.transactions import TransactionLine
from app.portfolio import models as m
from app.portfolio.observation_state import observation_state
from app.portfolio.owner_distributions import owner_custody_state
from app.portfolio.reports import ledger
from app.portfolio.service import control, execute, Problem, balance
from tests.test_balancedbooks_portfolio import (
    entity,
    account,
    run,
    journal,
    ln,
    bank_rows,
)


def posted(db, bank, amount, day, fund=None):
    obs = bank_rows(
        db, bank, [{"date": day, "amount_cents": amount}], uuid.uuid4().hex + ".csv"
    )[0]
    run(
        db,
        "post_bank",
        observation_id=obs,
        fund_id=fund,
        rationale="Synthetic actual bank record",
        duplicate_resolution="distinct",
    )
    return obs


def spec(db, obs):
    state = observation_state(db, obs)
    return dict(
        observation_id=obs,
        expected_state_digest=state["state_digest"],
        review_id=next(
            row["id"] for row in state["reviews"] if row["code"] == "suspense"
        ),
    )


def snapshot(db):
    return {
        "events": [(row.id, deepcopy(row.source)) for row in db.query(m.Event)],
        "lines": db.query(TransactionLine).count(),
        "ledger": {
            basis: ledger(db, date.max, basis=basis)
            for basis in ("accrual", "modified_cash_bank_posting")
        },
        "allocations": db.query(m.LineAllocation).count(),
        "applications": db.query(m.ItemApplication).count(),
        "items": db.query(m.OpenItem).count(),
        "reviews": [
            (row.id, row.status, row.resolution) for row in db.query(m.ReviewItem)
        ],
    }


@pytest.fixture
def distribution(db_session, request):
    db = db_session
    config = getattr(request, "param", {})
    pooled = config.get("pooled", False)
    sender, holder = entity(db, "SENDER"), entity(db, "HOLDER")
    sender_bank = account(db, sender, "BANK", "asset", "bank", pooled=pooled)
    holder_bank = account(db, holder, "BANK", "asset", "bank", pooled=pooled)
    equity = account(db, sender, "OWNER-DRAWS", "equity", "distribution")
    custody = control(db, holder, "custody_liability")
    sender_party = db.query(m.Party).filter_by(entity_id=sender).one().id
    holder_party = db.query(m.Party).filter_by(entity_id=holder).one().id
    source_own = run(
        db,
        "create_fund",
        code="SOURCE-OWN",
        holder_entity_id=sender,
        beneficiary_id=sender_party,
        purpose="general",
    )["fund_id"]
    holder_own = run(
        db,
        "create_fund",
        code="HOLDER-OWN",
        holder_entity_id=holder,
        beneficiary_id=holder_party,
        purpose="general",
    )["fund_id"]
    unknown = run(
        db,
        "create_fund",
        code="UNKNOWN",
        holder_entity_id=holder,
        beneficiary_id=holder_party,
        purpose="unresolved",
    )["fund_id"]
    parties, funds, ownership = [], [], []
    shares = config.get("shares", [500000, 500000])
    for index, share in enumerate(shares):
        party = run(
            db,
            "create_party",
            code=f"OWNER-{index}",
            name=f"Actual owner {index}",
            kind="individual",
        )["party_id"]
        parties.append(party)
        funds.append(
            run(
                db,
                "create_fund",
                code=f"HELD-{index}",
                holder_entity_id=holder,
                beneficiary_id=party,
                purpose="general",
            )["fund_id"]
        )
        ownership.append(
            run(
                db,
                "record_ownership",
                owner_id=party,
                entity_id=sender,
                share_ppm=share,
                interest="economic",
                start="2025-01-01",
                evidence="Synthetic dated ownership authority",
            )["ownership_id"]
        )
    journal(
        db,
        sender,
        "2026-01-01",
        [
            ln(sender_bank, 1000000, fund_id=source_own if pooled else None),
            ln(control(db, sender, "equity"), -1000000),
        ],
        kind="opening",
    )
    amount = config.get("amount", 110001)
    withdrawal = (
        None
        if config.get("receipt_only")
        else posted(
            db, sender_bank, -amount, "2026-01-10", source_own if pooled else None
        )
    )
    receipt = posted(
        db,
        holder_bank,
        amount,
        "2026-01-12",
        holder_own if config.get("holder_own_source") else unknown if pooled else None,
    )
    allocated, cumulative, beneficiaries = 0, 0, []
    for index, share in enumerate(shares):
        cumulative += share
        value = (
            amount - allocated
            if index == len(shares) - 1
            else (amount * cumulative + 500000) // 1000000 - allocated
        )
        allocated += value
        beneficiaries.append(
            dict(
                party_id=parties[index],
                fund_id=funds[index],
                ownership_id=ownership[index],
                share_ppm=share,
                amount_cents=value,
            )
        )
    data = dict(
        mode="receipt_only" if withdrawal is None else "paired",
        receipt=spec(db, receipt),
        sender_entity_id=sender,
        holder_entity_id=holder,
        distribution_date="2026-01-10",
        custody_account_id=custody,
        code="DIST-1",
        beneficiaries=beneficiaries,
        source_evidence="Synthetic sender authorization and exact recipient bank record",
        funding_evidence="Synthetic documented company funds for this distribution",
        evidence="Synthetic ownership, distribution and custody authority",
    )
    if withdrawal is not None:
        data.update(withdrawal=spec(db, withdrawal), distribution_account_id=equity)
    else:
        data["coverage_gap"] = "Actual sender statement is outside imported coverage"
    return dict(
        db=db,
        data=data,
        sender=sender,
        holder=holder,
        sender_bank=sender_bank,
        holder_bank=holder_bank,
        equity=equity,
        custody=custody,
        source_own=source_own,
        holder_own=holder_own,
        unknown=unknown,
        pooled=pooled,
        parties=parties,
        funds=funds,
        receipt=receipt,
        withdrawal=withdrawal,
    )


def distribute(fixture):
    return run(
        fixture["db"], "correct_posted_owner_distribution_to_holder", **fixture["data"]
    )


def payout(fixture, item_state, amount, *, day="2026-02-01", source_fund=None):
    db = fixture["db"]
    obs = posted(
        db,
        fixture["holder_bank"],
        -amount,
        day,
        (
            source_fund
            if source_fund is not None
            else fixture["unknown"] if fixture["pooled"] else None
        ),
    )
    return dict(
        observation=spec(db, obs),
        components=[
            dict(
                item_id=item_state["item_id"],
                party_id=item_state["party_id"],
                fund_id=item_state["fund_id"],
                amount_cents=amount,
                expected_item_digest=item_state["item_digest"],
                recipient_evidence="Synthetic exact beneficiary payout record",
            )
        ],
        evidence="Synthetic custody release authority",
    )


@pytest.mark.parametrize("distribution", [{}, {"pooled": True}], indirect=True)
def test_atomic_distribution_has_equity_and_individual_custody_with_real_dates(
    distribution,
):
    f, db = distribution, distribution["db"]
    physical = [
        (
            row["line_id"],
            row["account_id"],
            row["date"],
            row["debit_cents"],
            row["credit_cents"],
        )
        for row in ledger(db, date.max)
        if row["role"] == "bank"
    ]
    cmd = dict(
        operation="correct_posted_owner_distribution_to_holder",
        key=uuid.uuid4().hex,
        data=f["data"],
    )
    result = execute(db, cmd)
    assert result["bank_cash_delta_cents"] == 0
    assert result["sender_posting_created"] is True
    assert [row["original_cents"] for row in result["custody_items"]] == [55001, 55000]
    for basis in ("accrual", "modified_cash_bank_posting"):
        rows = ledger(db, date.max, basis=basis)
        assert (
            sum(
                row["debit_cents"] - row["credit_cents"]
                for row in rows
                if row["account_id"] == f["equity"]
            )
            == 110001
        )
        assert (
            sum(
                row["debit_cents"] - row["credit_cents"]
                for row in rows
                if row["account_id"] == f["custody"]
            )
            == -110001
        )
        assert not [row for row in rows if row["type"] in ("income", "expense", "cogs")]
        original = defaultdict_amounts(physical)
        actual = defaultdict_amounts(
            [
                (
                    row["line_id"],
                    row["account_id"],
                    row["date"],
                    row["debit_cents"],
                    row["credit_cents"],
                )
                for row in rows
                if row["role"] == "bank"
            ]
        )
        assert actual == original
    assert db.query(m.ManagementAllocation).count() == 0
    assert db.query(m.ReviewItem).filter_by(code="suspense", status="open").count() == 0
    before = snapshot(db)
    assert execute(db, cmd)["replayed"]
    assert snapshot(db) == before
    with pytest.raises(Problem) as caught:
        execute(db, {**cmd, "data": {**f["data"], "evidence": "Changed request"}})
    assert caught.value.code == "idempotency_conflict"


def defaultdict_amounts(rows):
    result = {}
    for ident, account_id, day, debit, credit in rows:
        key = ident, account_id, day
        result[key] = result.get(key, 0) + debit - credit
    return result


@pytest.mark.parametrize(
    "distribution",
    [{"receipt_only": True}, {"receipt_only": True, "pooled": True}],
    indirect=True,
)
def test_receipt_only_creates_real_custody_and_visible_source_coverage_gap(
    distribution,
):
    f, db = distribution, distribution["db"]
    sender_before = ledger(db, date.max, entity_ids=[f["sender"]])
    result = distribute(f)
    assert result["receipt_only"] and not result["sender_posting_created"]
    assert ledger(db, date.max, entity_ids=[f["sender"]]) == sender_before
    assert balance(db, f["equity"]) == 0
    assert balance(db, f["custody"]) == -110001
    flag = db.get(m.ReviewItem, result["open_coverage_review_ids"][0])
    assert flag.code == "source_coverage" and flag.status == "open"
    assert flag.observation_id == f["receipt"]


@pytest.mark.parametrize(
    "change,code",
    [
        ("wrong_holder", "wrong_entity"),
        ("wrong_beneficiary", "ownership_mismatch"),
        ("wrong_fund", "wrong_beneficiary"),
        ("wrong_share", "ownership_mismatch"),
        ("wrong_cents", "distribution_amount"),
        ("missing_owner", "ownership_incomplete"),
        ("wrong_date", "distribution_pair"),
        ("stale_receipt", "stale_state"),
        ("wrong_equity", "distribution_account"),
        ("wrong_custody", "custody_account"),
        ("expired_ownership", "ownership_incomplete"),
        ("restricted_target", "restricted_funds"),
    ],
)
def test_invalid_distribution_inputs_leave_all_accounting_and_reviews_unchanged(
    distribution, change, code
):
    f, db = distribution, distribution["db"]
    data = deepcopy(f["data"])
    if change == "wrong_holder":
        data["holder_entity_id"] = entity(db, "OTHER")
    elif change == "wrong_beneficiary":
        data["beneficiaries"][0]["party_id"] = run(
            db, "create_party", code="UNRELATED", name="Different individual"
        )["party_id"]
    elif change == "wrong_fund":
        data["beneficiaries"][0]["fund_id"] = f["holder_own"]
    elif change == "wrong_share":
        data["beneficiaries"][0]["share_ppm"] -= 1
    elif change == "wrong_cents":
        data["beneficiaries"][0]["amount_cents"] -= 1
    elif change == "missing_owner":
        data["beneficiaries"].pop()
    elif change == "wrong_date":
        data["distribution_date"] = "2026-01-11"
    elif change == "stale_receipt":
        data["receipt"]["expected_state_digest"] = "0" * 64
    elif change == "wrong_equity":
        data["distribution_account_id"] = control(db, f["sender"], "equity")
    elif change == "wrong_custody":
        data["custody_account_id"] = control(db, f["holder"], "payable")
    elif change == "expired_ownership":
        run(
            db,
            "end_ownership",
            ownership_id=data["beneficiaries"][0]["ownership_id"],
            end="2026-01-01",
            evidence="Synthetic prior ownership end",
        )
    else:
        data["beneficiaries"][0]["fund_id"] = run(
            db,
            "create_fund",
            code="RESTRICTED",
            holder_entity_id=f["holder"],
            beneficiary_id=f["parties"][0],
            purpose="general",
            restriction="restricted",
        )["fund_id"]
    before = snapshot(db)
    with pytest.raises(Problem) as caught:
        run(db, "correct_posted_owner_distribution_to_holder", **data)
    assert caught.value.code == code
    assert snapshot(db) == before


@pytest.mark.parametrize(
    "distribution",
    [{"amount": 2, "shares": [300000, 300000, 300000, 100000]}],
    indirect=True,
)
def test_rounding_tiny_amounts_never_creates_negative_owner_share(distribution):
    result = distribute(distribution)
    assert [row["amount_cents"] for row in result["beneficiaries"]] == [1, 0, 1, 0]
    assert sum(row["original_cents"] for row in result["custody_items"]) == 2


@pytest.mark.parametrize("distribution", [{}, {"pooled": True}], indirect=True)
def test_partial_then_final_payout_applies_only_to_exact_owner_obligation(distribution):
    f, db = distribution, distribution["db"]
    first, second = distribute(f)["custody_items"]
    cmd = dict(
        operation="correct_posted_owner_custody_payout",
        key=uuid.uuid4().hex,
        data=payout(f, first, 10000),
    )
    result = execute(db, cmd)
    assert result["custody_items"][0]["remaining_cents"] == 45001
    assert owner_custody_state(db, second["item_id"])["remaining_cents"] == 55000
    assert execute(db, cmd)["replayed"]
    final = run(
        db,
        "correct_posted_owner_custody_payout",
        **payout(f, result["custody_items"][0], 45001, day="2026-03-01"),
    )
    assert final["custody_items"][0]["remaining_cents"] == 0
    assert balance(db, f["custody"]) == -55000
    assert balance(db, f["holder_bank"]) == 55000
    assert db.query(m.ItemApplication).count() == 2
    assert not [
        row
        for row in ledger(db, date.max)
        if row["type"] in ("income", "expense", "cogs")
    ]


@pytest.mark.parametrize(
    "change,code",
    [
        ("party", "wrong_beneficiary"),
        ("fund", "wrong_beneficiary"),
        ("amount", "custody_overpayment"),
        ("stale", "stale_custody_item"),
        ("early", "custody_overpayment"),
        ("client_source", "client_financing"),
    ],
)
def test_payout_rejects_wrong_owner_overpayment_stale_or_client_source(
    distribution, change, code
):
    f, db = distribution, distribution["db"]
    first, second = distribute(f)["custody_items"]
    data = payout(
        f,
        first,
        first["remaining_cents"] + 1 if change == "amount" else 10000,
        day="2026-01-11" if change == "early" else "2026-02-01",
    )
    if change == "party":
        data["components"][0]["party_id"] = second["party_id"]
    elif change == "fund":
        data["components"][0]["fund_id"] = second["fund_id"]
    elif change == "stale":
        data["components"][0]["expected_item_digest"] = "0" * 64
    elif change == "client_source":
        # Synthetic historical pre-attribution: an imported withdrawal is
        # assigned to the wrong client's cash before its purpose is known.
        obs = db.get(m.BankObservation, data["observation"]["observation_id"])
        cash = next(
            row
            for row in ledger(db, date.max, event_ids=[obs.event_id])
            if row["role"] == "bank"
        )
        db.add(
            m.LineAllocation(
                line_id=cash["line_id"],
                event_id=obs.event_id,
                date=obs.date,
                evidence="Synthetic historical source attribution",
                components=[dict(amount_cents=-10000, fund_id=second["fund_id"])],
            )
        )
        db.flush()
        data["observation"] = spec(db, obs.id)
    before = snapshot(db)
    with pytest.raises(Problem) as caught:
        run(db, "correct_posted_owner_custody_payout", **data)
    assert caught.value.code == code
    assert snapshot(db) == before


@pytest.mark.parametrize("distribution", [{"holder_own_source": True}], indirect=True)
def test_future_holder_shortfall_rolls_back_both_source_and_receipt_corrections(
    distribution,
):
    f, db = distribution, distribution["db"]
    journal(
        db,
        f["holder"],
        "2026-02-01",
        [
            ln(f["holder_bank"], -1000, fund_id=f["holder_own"]),
            ln(control(db, f["holder"], "equity"), 1000),
        ],
        observed=True,
    )
    before = snapshot(db)
    with pytest.raises(Problem) as caught:
        distribute(f)
    assert caught.value.code == "fund_shortfall"
    assert snapshot(db) == before


def test_batch_failure_rolls_back_custody_items_and_both_reviews(distribution):
    f, db = distribution, distribution["db"]
    before = snapshot(db)
    with pytest.raises(Problem):
        run(
            db,
            "batch",
            operations=[
                dict(
                    operation="correct_posted_owner_distribution_to_holder",
                    data=f["data"],
                ),
                dict(
                    operation="create_party",
                    data=dict(code="INVALID", name="Invalid entity", entity_id=999999),
                ),
            ],
        )
    assert snapshot(db) == before


def test_combined_payout_applies_exact_amount_to_each_beneficiary(distribution):
    f, db = distribution, distribution["db"]
    first, second = distribute(f)["custody_items"]
    data = payout(f, first, 3000)
    data["components"][0]["amount_cents"] = 1000
    data["components"].append(
        dict(
            item_id=second["item_id"],
            party_id=second["party_id"],
            fund_id=second["fund_id"],
            amount_cents=2000,
            expected_item_digest=second["item_digest"],
            recipient_evidence="Synthetic second beneficiary payment",
        )
    )
    result = run(db, "correct_posted_owner_custody_payout", **data)
    assert [row["remaining_cents"] for row in result["custody_items"]] == [54001, 53000]
    assert db.query(m.ItemApplication).count() == 2


def test_prior_actual_payout_invalidates_old_item_digest(distribution):
    f, db = distribution, distribution["db"]
    first = distribute(f)["custody_items"][0]
    run(db, "correct_posted_owner_custody_payout", **payout(f, first, 1000))
    data = payout(f, first, 1000, day="2026-03-01")
    before = snapshot(db)
    with pytest.raises(Problem) as caught:
        run(db, "correct_posted_owner_custody_payout", **data)
    assert caught.value.code == "stale_custody_item"
    assert snapshot(db) == before


def test_temporary_basis_removal_of_custody_cannot_be_hidden_by_current_net(
    distribution,
):
    f, db = distribution, distribution["db"]
    first = distribute(f)["custody_items"][0]
    item = db.get(m.OpenItem, first["item_id"])
    for day, amount in (
        (date(2026, 2, 1), first["original_cents"]),
        (date(2026, 3, 1), -first["original_cents"]),
    ):
        db.add(
            m.BasisAdjustment(
                event_id=item.event_id,
                entity_id=f["holder"],
                date=day,
                reason="Synthetic inconsistent historical custody",
                lines=[
                    ln(f["custody"], amount, fund_id=first["fund_id"], item=item.code),
                    ln(control(db, f["holder"], "equity"), -amount),
                ],
            )
        )
    db.flush()
    with pytest.raises(Problem) as caught:
        owner_custody_state(db, item.id)
    assert caught.value.code == "custody_item_state"


def test_receipt_only_variant_cannot_fabricate_sender_posting(distribution):
    from pydantic import ValidationError

    f, db = distribution, distribution["db"]
    data = {
        **f["data"],
        "mode": "receipt_only",
        "coverage_gap": "Missing sender coverage",
    }
    before = snapshot(db)
    with pytest.raises(ValidationError):
        run(db, "correct_posted_owner_distribution_to_holder", **data)
    assert snapshot(db) == before
