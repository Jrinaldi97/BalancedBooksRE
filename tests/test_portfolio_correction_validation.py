"""Existing correction validation uses synthetic identities and source proofs only."""

from copy import deepcopy
from datetime import date
import json
import uuid

import pytest

from app.portfolio import models as m
from app.portfolio.observation_state import observation_state
from app.portfolio.reports import ledger
from app.portfolio.service import Problem, control, execute
from tests.test_balancedbooks_portfolio import (
    account,
    bank_rows,
    entity,
    journal,
    ln,
    run,
)


def posted(db, bank, amount):
    obs = bank_rows(
        db,
        bank,
        [{"date": "2026-02-01", "amount_cents": amount}],
        uuid.uuid4().hex + ".csv",
    )[0]
    result = run(
        db,
        "post_bank",
        observation_id=obs,
        rationale="Synthetic statement",
        duplicate_resolution="distinct",
    )
    return db.get(m.BankObservation, obs), result["event_id"]


def validation_request(db, obs, corrective_event, balances, manifest=None):
    review = (
        db.query(m.ReviewItem).filter_by(observation_id=obs.id, code="suspense").one()
    )
    data = dict(
        observation=dict(
            observation_id=obs.id,
            review_id=review.id,
            expected_state_digest=observation_state(db, obs.id)["state_digest"],
        ),
        corrective_event_id=corrective_event,
        balances=balances,
        evidence="Synthetic source documents and complete scope verification",
    )
    if manifest is not None:
        data["movement_manifest"] = manifest
    return data


def movement(obs):
    return dict(
        observation_id=obs.id,
        account_id=obs.account_id,
        date=obs.date.isoformat(),
        amount_cents=obs.amount_cents,
    )


def financial_snapshot(db):
    return {
        "event_count": db.query(m.Event).count(),
        "sources": [(row.id, deepcopy(row.source)) for row in db.query(m.Event)],
        "ledgers": {
            basis: ledger(db, date.max, basis=basis)
            for basis in ("accrual", "modified_cash_bank_posting")
        },
        "applications": db.query(m.ItemApplication).count(),
        "items": db.query(m.OpenItem).count(),
        "management": db.query(m.ManagementAllocation).count(),
    }


@pytest.fixture
def historical(db_session):
    db = db_session
    owner = entity(db, "HOLDER")
    bank = account(db, owner, "bank", "asset", "bank")
    party = run(db, "create_party", code="BENEFICIARY", name="Synthetic beneficiary")[
        "party_id"
    ]
    other = run(
        db, "create_party", code="OTHER", name="Different synthetic beneficiary"
    )["party_id"]
    obs, original = posted(db, bank, 11000)
    payable = control(db, owner, "payable")
    source = dict(
        observation_id=obs.id,
        beneficiary_party_id=party,
        item="HELD-SYNTHETIC-1",
        initial_distribution_cents=11000,
        exact_bank_pair_proof={"source_document_sha256": "a" * 64, "receipt_row": 2},
        direct_user_confirmation="Synthetic recorded beneficiary authority",
    )
    return db, owner, obs, original, payable, party, other, source


def historical_correction(
    fixture, source=None, kind="held_owner_distribution_classification"
):
    db, owner, obs, original, payable, party, _, default_source = fixture
    result = journal(
        db,
        owner,
        "2026-02-01",
        [
            ln(control(db, owner, "suspense_credit"), 11000),
            ln(payable, -11000, item=default_source["item"]),
        ],
        kind=kind,
        corrects_id=original,
        source=source if source is not None else default_source,
    )
    return validation_request(
        db,
        obs,
        result["event_id"],
        [
            dict(
                account_id=payable,
                entity_id=owner,
                amount_cents=-11000,
                item=default_source["item"],
                party_id=party,
            )
        ],
    )


def test_historical_beneficiary_validation_is_metadata_only_and_replays(historical):
    db = historical[0]
    data = historical_correction(historical)
    before = financial_snapshot(db)
    command = dict(
        operation="validate_existing_correction", key=uuid.uuid4().hex, data=data
    )
    result = execute(db, command)
    assert result["new_financial_events"] == 0
    assert result["source_movement_delta_cents"] == result["bank_cash_delta_cents"] == 0
    assert result["beneficiary_decoder"] == "held-owner-distribution/v1"
    assert execute(db, command)["replayed"]
    assert financial_snapshot(db) == before
    review = db.get(m.ReviewItem, data["observation"]["review_id"])
    assert review.status == "resolved"
    assert json.loads(review.resolution)["movement_manifest"]["observations"] == [
        movement(historical[2])
    ]
    with pytest.raises(Problem) as caught:
        execute(db, {**command, "data": {**data, "evidence": "Changed retry contents"}})
    assert caught.value.code == "idempotency_conflict"


@pytest.mark.parametrize(
    "variant",
    [
        "wrong_party",
        "conflict",
        "unsupported_kind",
        "unsupported_version",
        "missing_proof",
        "wrong_source_observation",
        "wrong_item",
        "stale",
        "nonexistent_party",
        "wrong_amount",
        "boolean_party",
        "empty_confirmation",
    ],
)
def test_historical_beneficiary_rejects_unsupported_or_conflicting_identity(
    historical, variant
):
    db, _, _, _, _, _, other, source = historical
    source = deepcopy(source)
    kind = "held_owner_distribution_classification"
    if variant == "conflict":
        source["party_id"] = other
    elif variant == "unsupported_kind":
        kind = "unrecognized_imported_correction"
    elif variant == "unsupported_version":
        source["beneficiary_schema_version"] = 2
    elif variant == "missing_proof":
        source.pop("exact_bank_pair_proof")
    elif variant == "wrong_source_observation":
        source["observation_id"] += 1000
    elif variant == "nonexistent_party":
        source["beneficiary_party_id"] = 999999
    elif variant == "wrong_amount":
        source["initial_distribution_cents"] += 1
    elif variant == "boolean_party":
        source["beneficiary_party_id"] = True
    elif variant == "empty_confirmation":
        source["direct_user_confirmation"] = " "
    data = historical_correction(historical, source, kind)
    if variant == "wrong_party":
        data["balances"][0]["party_id"] = other
    elif variant == "wrong_item":
        data["balances"][0]["item"] = "UNRELATED"
    elif variant == "stale":
        data["observation"]["expected_state_digest"] = "0" * 64
    before = financial_snapshot(db)
    with pytest.raises(Problem):
        run(db, "validate_existing_correction", **data)
    assert db.get(m.ReviewItem, data["observation"]["review_id"]).status == "open"
    assert financial_snapshot(db) == before


def test_historical_cohort_batch_resolves_only_metadata(historical):
    db, owner, first, _, payable, party, other, template = historical
    operations = [
        dict(
            operation="validate_existing_correction",
            data=historical_correction(historical),
        )
    ]
    for index in range(1, 63):
        obs, event = posted(db, first.account_id, 11000)
        source = {
            **template,
            "observation_id": obs.id,
            "item": f"HELD-SYNTHETIC-{index + 1}",
        }
        fixture = (db, owner, obs, event, payable, party, other, source)
        operations.append(
            dict(
                operation="validate_existing_correction",
                data=historical_correction(fixture),
            )
        )
    before = financial_snapshot(db)
    command = dict(
        operation="batch", key=uuid.uuid4().hex, data=dict(operations=operations)
    )
    result = execute(db, command)
    assert len(result["results"]) == 63
    assert all(row["new_financial_events"] == 0 for row in result["results"])
    assert execute(db, command)["replayed"]
    assert financial_snapshot(db) == before
    assert db.query(m.ReviewItem).filter_by(code="suspense", status="open").count() == 0


@pytest.mark.parametrize("legacy", [False, True])
def test_current_party_metadata_remains_supported(historical, legacy):
    source = deepcopy(historical[-1])
    source["party_id"] = source["beneficiary_party_id"]
    if not legacy:
        source.pop("beneficiary_party_id")
    data = historical_correction(historical, source)
    result = run(historical[0], "validate_existing_correction", **data)
    assert result["beneficiary_decoder"] == (
        "held-owner-distribution/v1" if legacy else None
    )


def test_beneficiary_metadata_must_belong_to_the_obligation_correction(historical):
    db, owner, obs, _, _, _, _, source = historical
    data = historical_correction(historical)
    equity = control(db, owner, "equity")
    unrelated = journal(
        db,
        owner,
        "2026-02-01",
        [ln(equity, 1), ln(equity, -1)],
        kind="held_owner_distribution_classification",
        source=source,
        corrects_id=data["corrective_event_id"],
    )
    data["corrective_event_id"] = unrelated["event_id"]
    data["observation"]["expected_state_digest"] = observation_state(db, obs.id)[
        "state_digest"
    ]
    with pytest.raises(Problem) as caught:
        run(db, "validate_existing_correction", **data)
    assert caught.value.code == "expected_balance"


@pytest.fixture
def multi_entity(db_session):
    db = db_session
    payer, recipient = [entity(db, code) for code in ("PAYER", "RECIPIENT")]
    bank = account(db, payer, "bank", "asset", "bank")
    expense = account(db, recipient, "expense", "expense", "expense")
    obs, original = posted(db, bank, -5200)
    due_from, due_to = control(db, payer, "due_from"), control(db, recipient, "due_to")
    result = run(
        db,
        "post",
        date="2026-02-01",
        description="Supported payment for another entity",
        rationale="Synthetic invoice and reciprocal agreement",
        corrects_id=original,
        journals=[
            dict(
                entity_id=payer,
                lines=[
                    ln(control(db, payer, "suspense_debit"), -5200),
                    ln(due_from, 5200, counterparty_id=recipient, item="RECIPROCAL-1"),
                ],
            ),
            dict(
                entity_id=recipient,
                lines=[
                    ln(expense, 5200),
                    ln(due_to, -5200, counterparty_id=payer, item="RECIPROCAL-1"),
                ],
            ),
        ],
    )
    balances = [
        dict(
            account_id=due_from,
            entity_id=payer,
            amount_cents=5200,
            counterparty_id=recipient,
            item="RECIPROCAL-1",
        ),
        dict(
            account_id=due_to,
            entity_id=recipient,
            amount_cents=-5200,
            counterparty_id=payer,
            item="RECIPROCAL-1",
        ),
        dict(account_id=expense, entity_id=recipient, amount_cents=5200),
    ]
    return db, obs, validation_request(db, obs, result["event_id"], balances)


def test_complete_multi_entity_vector_validates_without_new_economics(multi_entity):
    db, _, data = multi_entity
    before = financial_snapshot(db)
    assert run(db, "validate_existing_correction", **data)["new_financial_events"] == 0
    assert financial_snapshot(db) == before


@pytest.mark.parametrize(
    "variant",
    [
        "omit_expense",
        "omit_due_to",
        "wrong_entity",
        "wrong_item",
        "wrong_counterparty",
        "cash_flow",
        "duplicate",
    ],
)
def test_multi_entity_vector_requires_every_scope_and_reciprocal_item(
    multi_entity, variant
):
    db, _, data = multi_entity
    if variant == "omit_expense":
        data["balances"].pop(2)
    elif variant == "omit_due_to":
        data["balances"].pop(1)
    elif variant == "wrong_entity":
        data["balances"][1]["entity_id"] = data["balances"][0]["entity_id"]
    elif variant == "wrong_item":
        data["balances"][1]["item"] = "OTHER-ITEM"
    elif variant == "wrong_counterparty":
        data["balances"][1]["counterparty_id"] = data["balances"][1]["entity_id"]
    elif variant == "cash_flow":
        data["balances"][2]["cash_flow"] = "financing"
    else:
        data["balances"].append(data["balances"][2].copy())
    with pytest.raises(Problem):
        run(db, "validate_existing_correction", **data)


@pytest.fixture
def paired(db_session, request):
    db = db_session
    entities = [entity(db, code) for code in ("LENDER", "BORROWER")]
    banks = [account(db, owner, "bank", "asset", "bank") for owner in entities]
    first, first_event = posted(db, banks[0], -10000)
    second, second_event = posted(db, banks[1], 10000)
    due_from, due_to = control(db, entities[0], "due_from"), control(
        db, entities[1], "due_to"
    )
    source = {"source_event_ids": [first_event, second_event]}
    if getattr(request, "param", True):
        source["observation_ids"] = [first.id, second.id]
    result = run(
        db,
        "post",
        date="2026-02-01",
        description="Paired source classification",
        rationale="Synthetic paired bank evidence",
        corrects_id=first_event,
        source=source,
        journals=[
            dict(
                entity_id=entities[0],
                lines=[
                    ln(control(db, entities[0], "suspense_debit"), -10000),
                    ln(due_from, 10000, counterparty_id=entities[1], item="PAIR-1"),
                ],
            ),
            dict(
                entity_id=entities[1],
                lines=[
                    ln(control(db, entities[1], "suspense_credit"), 10000),
                    ln(due_to, -10000, counterparty_id=entities[0], item="PAIR-1"),
                ],
            ),
        ],
    )
    balances = [
        dict(
            account_id=due_from,
            entity_id=entities[0],
            amount_cents=10000,
            counterparty_id=entities[1],
            item="PAIR-1",
        ),
        dict(
            account_id=due_to,
            entity_id=entities[1],
            amount_cents=-10000,
            counterparty_id=entities[0],
            item="PAIR-1",
        ),
    ]
    manifest = dict(
        kind="paired_bank", observations=[movement(first), movement(second)]
    )
    return (
        db,
        first,
        second,
        validation_request(db, first, result["event_id"], balances, manifest),
    )


def test_paired_bank_manifest_preserves_both_actual_movements(paired):
    db, _, second, data = paired
    before = financial_snapshot(db)
    result = run(db, "validate_existing_correction", **data)
    assert result["movement_manifest"] == data["movement_manifest"]
    assert financial_snapshot(db) == before
    assert (
        db.query(m.ReviewItem)
        .filter_by(observation_id=second.id, code="suspense")
        .one()
        .status
        == "open"
    )


@pytest.mark.parametrize("paired", [False], indirect=True)
def test_explicit_source_events_are_followed_on_correction_descendants(paired):
    db, first, second, data = paired
    state = observation_state(db, first.id)
    assert second.event_id in state["event_ids"]
    assert {row["observation_id"] for row in state["source_observations"]} == {
        first.id,
        second.id,
    }
    assert run(db, "validate_existing_correction", **data)["new_financial_events"] == 0


@pytest.mark.parametrize(
    "variant",
    [
        "implicit_single",
        "missing",
        "duplicate",
        "wrong_date",
        "wrong_amount",
        "wrong_account",
        "unrelated_equal_amount",
    ],
)
def test_movement_manifest_rejects_missing_extra_or_substituted_identities(
    paired, variant
):
    db, first, second, data = paired
    rows = data["movement_manifest"]["observations"]
    if variant == "implicit_single":
        data.pop("movement_manifest")
    elif variant == "missing":
        rows.pop()
    elif variant == "duplicate":
        rows[1] = rows[0].copy()
    elif variant == "wrong_date":
        rows[1]["date"] = "2026-02-02"
    elif variant == "wrong_amount":
        rows[1]["amount_cents"] += 1
    elif variant == "wrong_account":
        rows[1]["account_id"] = first.account_id
    else:
        other, _ = posted(db, second.account_id, second.amount_cents)
        rows[1] = movement(other)
    with pytest.raises(Problem):
        run(db, "validate_existing_correction", **data)


def test_extra_cash_change_in_linked_correction_is_not_hidden_by_family_net(paired):
    db, first, second, data = paired
    owner = db.get(m.AccountScope, first.account_id).entity_id
    journal(
        db,
        owner,
        "2026-02-02",
        [ln(first.account_id, 100), ln(control(db, owner, "equity"), -100)],
        corrects_id=data["corrective_event_id"],
    )
    journal(
        db,
        owner,
        "2026-02-03",
        [ln(first.account_id, -100), ln(control(db, owner, "equity"), 100)],
        corrects_id=data["corrective_event_id"],
        observed=True,
    )
    data["observation"]["expected_state_digest"] = observation_state(db, first.id)[
        "state_digest"
    ]
    with pytest.raises(Problem) as caught:
        run(db, "validate_existing_correction", **data)
    assert caught.value.code == "cash_lineage"


def test_card_liability_requires_explicit_manifest_and_preserves_liability(db_session):
    db = db_session
    owner = entity(db, "CARDHOLDER")
    card = account(db, owner, "card", "liability", "credit_card")
    expense = account(db, owner, "repairs", "expense", "expense")
    obs, original = posted(db, card, -76500)
    result = journal(
        db,
        owner,
        "2026-02-01",
        [ln(control(db, owner, "suspense_debit"), -76500), ln(expense, 76500)],
        corrects_id=original,
    )
    data = validation_request(
        db, obs, result["event_id"], [dict(account_id=expense, amount_cents=76500)]
    )
    state = observation_state(db, obs.id)
    assert state["bank_movements"] == []
    assert state["cash_lineage"]["supported"] is False
    assert state["source_observations"] == [
        dict(kind="card_liability", event_id=original, **movement(obs))
    ]
    assert state["source_movements"] == [
        dict(
            kind="card_liability",
            event_id=original,
            account_id=card,
            date=obs.date.isoformat(),
            amount_cents=obs.amount_cents,
        )
    ]
    with pytest.raises(Problem) as caught:
        run(db, "validate_existing_correction", **data)
    assert caught.value.code == "movement_manifest"
    data["movement_manifest"] = dict(
        kind="card_liability", observations=[movement(obs)]
    )
    before = financial_snapshot(db)
    result = run(db, "validate_existing_correction", **data)
    assert result["source_movement_delta_cents"] == 0
    assert financial_snapshot(db) == before


def test_dimensional_residual_on_either_basis_rejects_validation(multi_entity):
    db, obs, data = multi_entity
    owner = db.get(m.AccountScope, obs.account_id).entity_id
    prop = run(db, "create_property", code="RESIDUAL", name="Synthetic dimension")[
        "property_id"
    ]
    suspense = control(db, owner, "suspense_debit")
    db.add(
        m.BasisAdjustment(
            event_id=data["corrective_event_id"],
            entity_id=owner,
            date=date(2026, 2, 1),
            reason="Synthetic dimensional residual",
            lines=[ln(suspense, 1), ln(suspense, -1, property_id=prop)],
        )
    )
    db.flush()
    data["observation"]["expected_state_digest"] = observation_state(db, obs.id)[
        "state_digest"
    ]
    with pytest.raises(Problem) as caught:
        run(db, "validate_existing_correction", **data)
    assert caught.value.code == "correction_residual"


def test_effective_economic_dimension_change_invalidates_state(multi_entity):
    db, obs, data = multi_entity
    prop = run(
        db, "create_property", code="NEW-SCOPE", name="Synthetic corrected property"
    )["property_id"]
    expense = data["balances"][2]["account_id"]
    row = next(row for row in ledger(db, date.max, account_id=expense))
    db.add(
        m.LineAllocation(
            line_id=row["line_id"],
            event_id=data["corrective_event_id"],
            date=obs.date,
            evidence="Synthetic scope correction",
            components=[dict(amount_cents=5200, property_id=prop)],
        )
    )
    db.flush()
    assert (
        observation_state(db, obs.id)["state_digest"]
        != data["observation"]["expected_state_digest"]
    )
    with pytest.raises(Problem) as caught:
        run(db, "validate_existing_correction", **data)
    assert caught.value.code == "stale_state"


def test_non_suspense_economics_must_match_on_both_bases(multi_entity):
    db, obs, data = multi_entity
    expense = data["balances"][2]
    prop = run(
        db, "create_property", code="BASIS-SCOPE", name="Synthetic modified basis scope"
    )["property_id"]
    db.add(
        m.BasisAdjustment(
            event_id=data["corrective_event_id"],
            entity_id=expense["entity_id"],
            date=obs.date,
            reason="Synthetic basis difference",
            lines=[
                ln(expense["account_id"], -1),
                ln(expense["account_id"], 1, property_id=prop),
            ],
        )
    )
    db.flush()
    data["observation"]["expected_state_digest"] = observation_state(db, obs.id)[
        "state_digest"
    ]
    with pytest.raises(Problem) as caught:
        run(db, "validate_existing_correction", **data)
    assert caught.value.code == "expected_balance"


def test_metadata_only_descendant_does_not_change_existing_correction(paired):
    db, first, _, data = paired
    metadata = m.Event(
        code="synthetic-metadata-1",
        kind="synthetic_metadata_attestation",
        date=first.date,
        description="Synthetic provenance",
        rationale="Synthetic evidence only",
        actor="test",
        corrects_id=data["corrective_event_id"],
        source={"observation_id": first.id},
    )
    db.add(metadata)
    db.flush()
    data["observation"]["expected_state_digest"] = observation_state(db, first.id)[
        "state_digest"
    ]
    before = financial_snapshot(db)
    assert run(db, "validate_existing_correction", **data)["new_financial_events"] == 0
    assert financial_snapshot(db) == before


@pytest.mark.parametrize(
    "contract", ["legacy", "manifest", "entity", "null_cash_flow", "explicit_cash_flow"]
)
def test_legacy_one_bank_balance_keeps_original_cash_flow_matching_contract(
    db_session, contract
):
    db = db_session
    owner = entity(db, "LEGACY")
    bank = account(db, owner, "BANK", "asset", "bank")
    expense = account(db, owner, "EXPENSE", "expense", "expense")
    obs, original = posted(db, bank, -100)
    correction = journal(
        db,
        owner,
        obs.date.isoformat(),
        [
            ln(control(db, owner, "suspense_debit"), -100),
            ln(expense, 100, cash_flow="financing"),
        ],
        corrects_id=original,
    )
    data = validation_request(
        db, obs, correction["event_id"], [dict(account_id=expense, amount_cents=100)]
    )
    if contract == "manifest":
        data["movement_manifest"] = dict(
            kind="single_bank", observations=[movement(obs)]
        )
    elif contract == "entity":
        data["balances"][0]["entity_id"] = owner
    elif contract == "null_cash_flow":
        data["balances"][0]["cash_flow"] = None
    elif contract == "explicit_cash_flow":
        data["balances"][0]["cash_flow"] = "financing"
    if contract in ("legacy", "explicit_cash_flow"):
        result = run(db, "validate_existing_correction", **data)
        assert ("cash_flow" in result["expected_dimensions"]) == (
            contract == "explicit_cash_flow"
        )
    else:
        with pytest.raises(Problem) as caught:
            run(db, "validate_existing_correction", **data)
        assert caught.value.code == "expected_balance"
