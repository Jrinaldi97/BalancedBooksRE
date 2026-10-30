"""Evidence-only transfer components, with serialized capacity and immutable history."""

from collections import defaultdict
from sqlalchemy import or_, func, exists, select
from app.portfolio import models as m


def association_packets(db, observation_ids):
    """One batched indexed read. Include inactive history in concurrency guards."""
    from app.portfolio.service import values

    result = defaultdict(list)
    rows = (
        db.query(m.TransferAssociation)
        .filter(
            or_(
                m.TransferAssociation.withdrawal_id.in_(observation_ids),
                m.TransferAssociation.deposit_id.in_(observation_ids),
            )
        )
        .order_by(m.TransferAssociation.id)
        .all()
    )
    superseded = {row.supersedes_id for row in rows if row.supersedes_id}
    for row in rows:
        packet = {
            **values(row),
            "active": row.id not in superseded and row.amount_cents > 0,
        }
        for ident in (row.withdrawal_id, row.deposit_id):
            result[ident].append(packet)
    return result


def whole_claims(db, observation_id):
    """Legacy whole links and typed financial transfer matches consume capacity."""
    receipts = (
        db.query(m.RequestRecord.key)
        .filter(
            m.RequestRecord.operation == "link_classified_transfer",
            or_(
                m.RequestRecord.result["withdrawal_id"].as_integer() == observation_id,
                m.RequestRecord.result["deposit_id"].as_integer() == observation_id,
            ),
        )
        .all()
    )
    source_ids = func.json_each(m.Event.source, "$.observation_ids").table_valued(
        "value"
    )
    events = (
        db.query(m.Event.id)
        .filter(
            m.Event.kind.in_(
                ["transfer", "posted_internal_transfer", "posted_intercompany"]
            ),
            or_(
                m.Event.source["observation_id"].as_integer() == observation_id,
                m.Event.source["paired_observation_id"].as_integer() == observation_id,
                exists(
                    select(1)
                    .select_from(source_ids)
                    .where(source_ids.c.value == observation_id)
                ),
            ),
        )
        .all()
    )
    return {
        "request_keys": [row[0] for row in receipts],
        "event_ids": [row[0] for row in events],
    }


def require_no_components(db, observation_ids):
    from app.portfolio.service import fail

    if any(
        row["active"]
        for rows in association_packets(db, observation_ids).values()
        for row in rows
    ):
        fail(
            "transfer_link",
            "A source has active component associations; inspect its capacity before whole-source matching",
        )


def remaining_capacity(db, obs, excluding=None):
    claims = whole_claims(db, obs.id)
    if claims["request_keys"] or claims["event_ids"]:
        return 0
    return abs(obs.amount_cents) - sum(
        row["amount_cents"]
        for row in association_packets(db, [obs.id])[obs.id]
        if row["active"] and row["id"] != excluding
    )


def associate(db, data, actor, reverse=False):
    from app.portfolio.service import Problem, fail, get, scope, values
    from app.portfolio.observation_state import observation_states
    from app.portfolio.cash_lineage import normalized_cash

    refs = [data.withdrawal, data.deposit]
    if refs[0].observation_id == refs[1].observation_id:
        fail("transfer_source", "Choose two distinct physical observations")
    states = observation_states(db, [ref.observation_id for ref in refs])
    observations = []
    for ref, state in zip(refs, states):
        obs = get(db, m.BankObservation, ref.observation_id)
        if state["state_digest"] != ref.expected_state_digest:
            fail(
                "stale_state",
                "Refresh both transfer observations and their association capacity",
            )
        if obs.status != "posted" or scope(db, obs.account_id).role != "bank":
            fail("transfer_source", "Associate posted bank observations only")
        normalized_cash(db, obs, state)
        observations.append(obs)
    out, inc = observations
    if (
        out.amount_cents >= 0
        or inc.amount_cents <= 0
        or out.account_id == inc.account_id
        or scope(db, out.account_id).entity_id != scope(db, inc.account_id).entity_id
    ):
        fail(
            "transfer_source",
            "Choose opposite movements in distinct banks of the same entity",
        )
    old_id = data.association_id if reverse else data.supersedes_id
    if old_id:
        old = get(db, m.TransferAssociation, old_id)
        if (old.withdrawal_id, old.deposit_id) != (out.id, inc.id):
            fail(
                "association_identity",
                "A successor must name the original physical pair",
            )
        if (
            old.amount_cents == 0
            or db.query(m.TransferAssociation).filter_by(supersedes_id=old.id).first()
        ):
            fail(
                "association_superseded",
                "This association is already reversed or superseded",
            )
    amount = 0 if reverse else data.amount_cents
    capacities = {
        str(obs.id): remaining_capacity(db, obs, old_id) for obs in observations
    }
    if any(amount > capacity for capacity in capacities.values()):
        raise Problem(
            "association_capacity",
            "Component exceeds the remaining physical source capacity",
            {
                "requested_cents": amount,
                "available_cents": capacities,
            },
        )
    documents = [] if reverse else data.source_documents
    if not reverse and data.evidence_kind == "bank_itemized" and not documents:
        fail(
            "association_evidence",
            "Bank itemization requires a registered source version",
        )
    for proof in documents:
        if (
            not db.query(m.DocumentLocation)
            .filter_by(document_id=proof.document_id, sha256=proof.sha256)
            .first()
        ):
            fail(
                "association_evidence",
                "Register the exact supporting source version first",
            )
    row = m.TransferAssociation(
        withdrawal_id=out.id,
        deposit_id=inc.id,
        amount_cents=amount,
        supersedes_id=old_id,
        evidence={
            "kind": "reversal" if reverse else data.evidence_kind,
            "explanation": data.evidence,
            "source_documents": [proof.model_dump() for proof in documents],
        },
        actor=actor,
    )
    db.add(row)
    db.flush()
    return {
        "association": values(row),
        "remaining_unassociated_cents": {
            ident: capacity - amount for ident, capacity in capacities.items()
        },
        "financial_entries_added": False,
        "resolved_review_ids": [],
        "source_movement_delta_cents": 0,
        "scope": "Association evidence only. Residual purpose, cash ownership and custody are unchanged; registered sources are not independently certified by the application.",
    }
