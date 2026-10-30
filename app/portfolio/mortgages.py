"""Dated debt state and a source-preserving correction of applied mortgage payments."""

from app.portfolio import models as m


def mortgage_state(db, observation_id, debt_account_id, instrument, property_id=None):
    from app.portfolio.service import digest, get, scope, fail
    from app.portfolio.reports import read_snapshot
    from app.portfolio.dated_balances import liability_capacity

    read_snapshot(db)
    obs = get(db, m.BankObservation, observation_id)
    bank = scope(db, obs.account_id)
    debt = scope(db, debt_account_id, bank.entity_id)
    if debt.role != "debt" or bank.role != "bank":
        fail("payment_mapping", "Choose a bank source and its entity's debt account")
    capacity = liability_capacity(
        db,
        debt_account_id,
        obs.date,
        lambda row: row.get("item") == instrument
        and row.get("property_id") == property_id,
    )
    result = {
        "observation_id": obs.id,
        "payment_date": obs.date.isoformat(),
        "entity_id": bank.entity_id,
        "debt_account_id": debt_account_id,
        "instrument": instrument,
        "property_id": property_id,
        "supported_liability_cents": capacity["current"],
        "remaining_principal_capacity_cents": capacity["minimum"],
        "debt_history_digest": capacity["history_digest"],
        "source_event_ids": capacity["source_event_ids"],
        "scope": "Recorded debt for this exact instrument/property on each basis. A lender's outstanding balance alone does not authorize an opening entry.",
    }
    result["state_digest"] = digest(result)
    return result


def correct_posted_mortgage_payment(db, data, actor):
    from app.portfolio.service import get, fail, mortgage_payment, Problem
    from app.portfolio.observation_state import observation_state

    obs = get(db, m.BankObservation, data.observation_id)
    review = get(db, m.ReviewItem, data.review_id)
    if (
        obs.status != "posted"
        or review.observation_id != obs.id
        or review.code != "suspense"
        or review.status != "open"
    ):
        fail("review_state", "Choose this posted withdrawal's open purpose review")
    state = observation_state(db, obs.id)
    if state["state_digest"] != data.expected_state_digest:
        fail("stale_state", "Refresh the posted mortgage source")
    debt = mortgage_state(
        db, obs.id, data.debt_account_id, data.instrument, data.property_id
    )
    if debt["state_digest"] != data.expected_debt_digest:
        fail("stale_debt", "Debt history changed; refresh the mortgage state")
    if any(
        data.principal_cents > available
        for available in debt["remaining_principal_capacity_cents"].values()
    ):
        raise Problem(
            "principal_balance",
            "Principal exceeds supported debt; establish the evidenced opening first",
            debt,
        )
    if data.application_date > obs.date:
        fail(
            "application_date",
            "The applied-payment evidence cannot post-date this bank payment; assess the timing separately",
        )
    proof = data.lender_evidence
    if (
        not db.query(m.DocumentLocation)
        .filter_by(document_id=proof.document_id, sha256=proof.sha256)
        .first()
    ):
        fail(
            "lender_evidence",
            "Register the exact actual-payment application source first",
        )
    result = mortgage_payment(db, data, actor, correction=(obs, review, state))
    db.add(m.DocumentLink(document_id=proof.document_id, event_id=result["event_id"]))
    db.flush()
    return {
        **result,
        "debt_state_before": debt,
        "application_date": data.application_date.isoformat(),
        "physical_source_preserved": True,
    }
