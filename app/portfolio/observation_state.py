"""Read-only correction lineage and dimensional residuals for one observation."""

from collections import defaultdict
from datetime import date
from sqlalchemy import or_, exists, select, func
from app.portfolio import models as m
from app.portfolio.allocations import DIMENSIONS
from app.portfolio.service import get, values, digest
from app.portfolio.reports import read_snapshot
from app.portfolio.ledger_snapshot import LedgerSnapshot

SOURCE_MOVEMENT_KINDS = {
    "bank": "bank_cash",
    "credit_card": "card_liability",
    "bridge": "clearing_transit",
}


def observation_state(db, observation_id, through=None):
    return observation_states(db, [observation_id], through)[0]


def observation_states(db, observation_ids, through=None):
    """Exact per-observation packets sharing one ephemeral database read snapshot."""
    from app.portfolio.service import fail

    if (
        not 1 <= len(observation_ids) <= 200
        or len(set(observation_ids)) != len(observation_ids)
        or any(type(i) is not int or i <= 0 for i in observation_ids)
    ):
        fail("observation_ids", "Choose 1 to 200 unique positive observation IDs")
    read_snapshot(db)
    through = through or date.max
    observations = {
        obs.id: obs
        for obs in db.query(m.BankObservation).filter(
            m.BankObservation.id.in_(observation_ids)
        )
    }
    for ident in observation_ids:
        if ident not in observations:
            get(db, m.BankObservation, ident)
    linked_ids = func.json_each(m.Event.source, "$.observation_ids").table_valued(
        "value"
    )
    linked = exists(
        select(1)
        .select_from(linked_ids)
        .where(linked_ids.c.value == m.BankObservation.id)
    )
    roots = (
        db.query(m.BankObservation.id, m.Event)
        .join(
            m.Event,
            or_(
                m.Event.id == m.BankObservation.event_id,
                m.Event.source["observation_id"].as_integer() == m.BankObservation.id,
                linked,
            ),
        )
        .filter(m.BankObservation.id.in_(observation_ids))
        .all()
    )
    per_observation = defaultdict(set)
    events = {}
    for ident, event in roots:
        per_observation[ident].add(event.id)
        events[event.id] = event
    frontier = set(events)
    while frontier:
        source_ids = {
            ident
            for event_id in frontier
            for ident in (events[event_id].source or {}).get("source_event_ids", [])
            if type(ident) is int and ident > 0
        } - events.keys()
        children = (
            db.query(m.Event)
            .filter(or_(m.Event.corrects_id.in_(frontier), m.Event.id.in_(source_ids)))
            .all()
        )
        frontier = {event.id for event in children} - events.keys()
        events.update({event.id: event for event in children})
    edges = defaultdict(set)
    for event in events.values():
        if event.corrects_id in events:
            edges[event.corrects_id].add(event.id)
        edges[event.id].update(
            ident
            for ident in (event.source or {}).get("source_event_ids", [])
            if type(ident) is int and ident in events
        )
    for identifiers in per_observation.values():
        frontier = set(identifiers)
        while frontier:
            frontier = set().union(*(edges[ident] for ident in frontier)) - identifiers
            identifiers.update(frontier)
    active_union = {
        ident
        for identifiers in per_observation.values()
        for ident in identifiers
        if events[ident].date <= through
    }
    # Cash lineage uses the immutable observation date even when the state is
    # inspected later. Preserve complete effective revision history for both.
    max_cutoff = max([through, *(obs.date for obs in observations.values())])
    ledger_input = LedgerSnapshot(db, max_cutoff, event_ids=active_union)
    reviews = defaultdict(list)
    for review in (
        db.query(m.ReviewItem)
        .filter(m.ReviewItem.observation_id.in_(observation_ids))
        .order_by(m.ReviewItem.id)
    ):
        reviews[review.observation_id].append(values(review))
    sources = list(
        db.query(m.BankObservation, m.AccountScope)
        .join(m.AccountScope, m.AccountScope.account_id == m.BankObservation.account_id)
        .filter(m.BankObservation.event_id.in_(active_union))
        .order_by(m.BankObservation.id)
    )
    entities = {
        scope.account_id: scope.entity_id
        for scope in db.query(m.AccountScope).filter(
            m.AccountScope.account_id.in_(
                {obs.account_id for obs in observations.values()}
            )
        )
    }
    result = []
    for ident in observation_ids:
        obs = observations[ident]
        active = sorted(i for i in per_observation[ident] if events[i].date <= through)
        rows_by_basis = ledger_input.project_bases(through, event_ids=active)
        result.append(
            _assemble_state(
                db,
                obs,
                through,
                active,
                events,
                rows_by_basis,
                reviews[ident],
                [
                    (original, account)
                    for original, account in sources
                    if original.event_id in active
                ],
                ledger_input.project(obs.date, event_ids=active),
                entities.get(obs.account_id),
            )
        )
    return result


def _assemble_state(
    db,
    obs,
    through,
    active,
    events,
    rows_by_basis,
    reviews,
    source_observations,
    cash_rows,
    entity_id,
):
    residuals = {}
    cash = defaultdict(int)
    movements = defaultdict(int)
    for basis in ("accrual", "modified_cash_bank_posting"):
        totals = defaultdict(int)
        for row in rows_by_basis[basis]:
            amount = row["debit_cents"] - row["credit_cents"]
            if row["role"].startswith("suspense"):
                key = (
                    row["entity_id"],
                    row["account_id"],
                    *(row.get(k) for k in DIMENSIONS),
                )
                totals[key] += amount
            if basis == "accrual" and row["role"] == "bank":
                cash[(row["account_id"], row["date"])] += amount
            if basis == "accrual" and row["role"] in SOURCE_MOVEMENT_KINDS:
                movements[
                    (
                        SOURCE_MOVEMENT_KINDS[row["role"]],
                        row["event_id"],
                        row["account_id"],
                        row["date"],
                    )
                ] += amount
        residuals[basis] = [
            {
                "entity_id": key[0],
                "account_id": key[1],
                **dict(zip(DIMENSIONS, key[2:])),
                "amount_cents": amount,
            }
            for key, amount in sorted(totals.items(), key=lambda pair: repr(pair[0]))
            if amount
        ]
    state = {
        "observation": values(obs),
        "as_of": through.isoformat(),
        "event_ids": active,
        "suspense_residuals": residuals,
        "bank_movements": [
            {"account_id": key[0], "date": key[1], "amount_cents": amount}
            for key, amount in sorted(cash.items())
            if amount
        ],
        # These distinguish liability and transit activity from bank cash. The
        # immutable originals remain explicit even when two legs net to zero.
        "source_observations": [
            {
                "observation_id": original.id,
                "event_id": original.event_id,
                "kind": SOURCE_MOVEMENT_KINDS[account.role],
                "account_id": original.account_id,
                "date": original.date.isoformat(),
                "amount_cents": original.amount_cents,
            }
            for original, account in source_observations
            if account.role in SOURCE_MOVEMENT_KINDS
        ],
        "source_movements": [
            {
                "kind": key[0],
                "event_id": key[1],
                "account_id": key[2],
                "date": key[3],
                "amount_cents": amount,
            }
            for key, amount in sorted(movements.items())
            if amount
        ],
        # Bind the caller's state guard to the complete effective economics,
        # including non-suspense allocation and basis changes.
        "effective_ledger_digest": digest(rows_by_basis),
        "reviews": reviews,
        "scope": "Explicit observation references and correction descendants; unrelated journals are not inferred.",
    }
    from app.portfolio.cash_lineage import normalized_cash
    from app.portfolio.service import Problem

    if obs.event_id:
        try:
            normalized, physical = normalized_cash(
                db,
                obs,
                state,
                rows=cash_rows,
                events={ident: events[ident] for ident in active},
                entity_id=entity_id,
            )
            state["cash_lineage"] = {
                "supported": True,
                "physical_line_ids": sorted(r["line_id"] for r in physical),
                "net_amount_cents": sum(
                    r["debit_cents"] - r["credit_cents"] for r in normalized
                ),
                "effective_components": normalized,
            }
        except Problem as exc:
            state["cash_lineage"] = {
                "supported": False,
                "code": exc.code,
                "message": str(exc),
            }
    state["state_digest"] = digest(state)
    return state
