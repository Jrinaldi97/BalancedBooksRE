"""Read-only correction lineage and dimensional residuals for one observation."""

from collections import defaultdict
from datetime import date
from sqlalchemy import or_, exists, select, func
from app.portfolio import models as m
from app.portfolio.allocations import DIMENSIONS
from app.portfolio.service import get, values, digest
from app.portfolio.reports import ledger, read_snapshot


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
    source_ids = {
        ident
        for event in roots
        for ident in (event.source or {}).get("source_event_ids", [])
        if isinstance(ident, int)
    }
    if source_ids:
        roots.extend(db.query(m.Event).filter(m.Event.id.in_(source_ids)).all())
    events = {event.id: event for event in roots}
    frontier = set(events)
    while frontier:
        children = db.query(m.Event).filter(m.Event.corrects_id.in_(frontier)).all()
        frontier = {event.id for event in children} - events.keys()
        events.update({event.id: event for event in children})
    active = sorted(event.id for event in events.values() if event.date <= through)
    residuals = {}
    cash = defaultdict(int)
    for basis in ("accrual", "modified_cash_bank_posting"):
        totals = defaultdict(int)
        for row in ledger(db, through, basis=basis, event_ids=active):
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
        "reviews": reviews,
        "scope": "Explicit observation references and correction descendants; unrelated journals are not inferred.",
    }
    state["state_digest"] = digest(state)
    return state
