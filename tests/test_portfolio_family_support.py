"""Multiple value-bearing corrections validate one source without reposting it."""

from copy import deepcopy
import json
import uuid

import pytest

from app.portfolio import models as m
from app.portfolio.service import Problem, control, execute
from app.portfolio.observation_state import observation_state
from tests.test_balancedbooks_portfolio import account, entity, journal, ln, run
from tests.test_portfolio_correction_validation import (
    posted,
    validation_request,
    financial_snapshot,
)


@pytest.fixture
def family(db_session):
    db = db_session
    owner = entity(db, "FAMILY")
    bank = account(db, owner, "bank", "asset", "bank")
    obs, original = posted(db, bank, -10175)
    receivable = control(db, owner, "receivable")
    corrections, balances = [], []
    for code, amount in (("PRINCIPAL", 10000), ("FEE", 175)):
        party = run(db, "create_party", code=code, name=code)["party_id"]
        correction = journal(
            db,
            owner,
            "2026-02-01",
            [
                ln(control(db, owner, "suspense_debit"), -amount),
                ln(receivable, amount, item=code),
            ],
            corrects_id=original,
            source={"observation_id": obs.id, "party_id": party},
        )
        corrections.append(correction["event_id"])
        balances.append(
            dict(
                account_id=receivable,
                entity_id=owner,
                amount_cents=amount,
                item=code,
                party_id=party,
            )
        )
    data = validation_request(db, obs, corrections[0], balances)
    data["supporting_corrective_event_ids"] = corrections
    return db, obs, data


def test_family_support_is_metadata_only_complete_and_retry_safe(family):
    db, obs, data = family
    before = financial_snapshot(db)
    command = dict(
        operation="validate_existing_correction", key=uuid.uuid4().hex, data=data
    )
    result = execute(db, command)
    assert result["new_financial_events"] == 0
    assert (
        result["supporting_corrective_event_ids"]
        == data["supporting_corrective_event_ids"]
    )
    assert [
        row["supporting_events"][0]["event_id"] for row in result["obligation_support"]
    ] == data["supporting_corrective_event_ids"]
    assert financial_snapshot(db) == before
    review = db.get(m.ReviewItem, data["observation"]["review_id"])
    assert review.resolved_event_id == data["corrective_event_id"]
    assert json.loads(review.resolution)["support_digest"] == result["support_digest"]
    assert execute(db, command)["replayed"]
    changed = deepcopy(command)
    changed["data"]["evidence"] = "Different evidence"
    with pytest.raises(Problem, match="different contents"):
        execute(db, changed)


@pytest.mark.parametrize("selected", [0, 1])
def test_either_correction_alone_cannot_validate_the_complete_family(family, selected):
    db, obs, data = family
    data["corrective_event_id"] = data["supporting_corrective_event_ids"][selected]
    data["supporting_corrective_event_ids"] = [data["corrective_event_id"]]
    before = financial_snapshot(db)
    with pytest.raises(Problem) as error:
        run(db, "validate_existing_correction", **data)
    assert error.value.code == "expected_balance"
    assert error.value.details["expected"]["item"] == (
        "FEE" if selected == 0 else "PRINCIPAL"
    )
    assert financial_snapshot(db) == before


@pytest.mark.parametrize(
    "variant",
    ["metadata", "unrelated", "duplicates", "missing_anchor", "wrong_item", "stale"],
)
def test_family_rejects_invalid_support(family, variant):
    db, obs, data = family
    if variant in {"metadata", "unrelated"}:
        owner = db.get(m.AccountScope, obs.account_id).entity_id
        extra = journal(
            db,
            owner,
            "2026-02-01",
            [
                ln(control(db, owner, "equity"), 1),
                ln(control(db, owner, "equity"), -1),
            ],
            corrects_id=obs.event_id if variant == "metadata" else None,
            source={"party_id": data["balances"][0]["party_id"]},
        )
        data["supporting_corrective_event_ids"].append(extra["event_id"])
        data["observation"]["expected_state_digest"] = observation_state(db, obs.id)[
            "state_digest"
        ]
    elif variant == "duplicates":
        data["supporting_corrective_event_ids"].append(data["corrective_event_id"])
    elif variant == "missing_anchor":
        data["supporting_corrective_event_ids"] = data[
            "supporting_corrective_event_ids"
        ][1:]
    elif variant == "wrong_item":
        data["balances"][0]["item"] = "WRONG"
    else:
        data["observation"]["expected_state_digest"] = "0" * 64
    with pytest.raises(Problem):
        run(db, "validate_existing_correction", **data)
    assert db.get(m.ReviewItem, data["observation"]["review_id"]).status == "open"
