"""Read-only correction lineage and dimensional residuals for one observation."""

from collections import defaultdict
from datetime import date
from sqlalchemy import or_, exists, select, func
from app.portfolio import models as m
from app.portfolio.allocations import DIMENSIONS
from app.portfolio.service import get, values, digest
from app.portfolio.reports import ledger, read_snapshot

SOURCE_MOVEMENT_KINDS = {
    "bank": "bank_cash",
    "credit_card": "card_liability",
    "bridge": "clearing_transit",
}


def observation_state(db, observation_id, through=None):
    read_snapshot(db)
    obs = get(db, m.BankObservation, observation_id)
    through = through or date.max
    # Only explicit observation references or corrects_id links establish
    # lineage. Similar amounts/descriptions do not establish relationships.
    linked_ids = func.json_each(m.Event.source, "$.observation_ids").table_valued(
        "value"
    )
    linked = exists(
        select(1).select_from(linked_ids).where(linked_ids.c.value == obs.id)
    )
    roots = (
        db.query(m.Event)
        .filter(
            or_(
                m.Event.id == obs.event_id,
                m.Event.source["observation_id"].as_integer() == obs.id,
                linked,
            )
        )
        .all()
    )
    # A coordinated correction is assessed with all explicitly linked original
    # legs, so its paired clearing lines are never mistaken for new suspense.
    events = {event.id: event for event in roots}
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
    active = sorted(event.id for event in events.values() if event.date <= through)
    residuals = {}
    cash = defaultdict(int)
    movements = defaultdict(int)
    rows_by_basis = {}
    for basis in ("accrual", "modified_cash_bank_posting"):
        totals = defaultdict(int)
        rows_by_basis[basis] = ledger(db, through, basis=basis, event_ids=active)
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
    reviews = [
        values(r)
        for r in db.query(m.ReviewItem)
        .filter_by(observation_id=obs.id)
        .order_by(m.ReviewItem.id)
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
            for original, account in db.query(m.BankObservation, m.AccountScope)
            .join(
                m.AccountScope,
                m.AccountScope.account_id == m.BankObservation.account_id,
            )
            .filter(m.BankObservation.event_id.in_(active))
            .order_by(m.BankObservation.id)
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
            normalized, physical = normalized_cash(db, obs, state)
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
