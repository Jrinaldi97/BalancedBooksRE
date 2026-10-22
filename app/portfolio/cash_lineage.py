"""Strict net-cash normalization for an immutable observation correction family."""

from collections import defaultdict
from app.portfolio import models as m
from app.portfolio.allocations import DIMENSIONS


def normalized_cash(db, obs, state):
    from app.portfolio.service import fail, get, scope
    from app.portfolio.reports import ledger

    rows = [
        r
        for r in ledger(db, obs.date, event_ids=state["event_ids"])
        if r["role"] == "bank"
    ]
    if not rows:
        fail("cash_lineage", "No original bank movement was found")
    entity = scope(db, obs.account_id).entity_id
    events = {i: get(db, m.Event, i) for i in state["event_ids"]}
    descendants = {obs.event_id}
    while True:
        expanded = descendants | {
            i for i, e in events.items() if e.corrects_id in descendants
        }
        if expanded == descendants:
            break
        descendants = expanded
    for event in events.values():
        source = event.source or {}
        if (
            event.id not in descendants
            or source.get("observation_id", obs.id) != obs.id
            or any(i != obs.id for i in source.get("observation_ids", []))
            or any(i not in descendants for i in source.get("source_event_ids", []))
        ):
            fail(
                "cash_lineage",
                "Correction ancestry includes another observation or source family",
            )
    totals = defaultdict(int)
    physical = {}
    scopes = defaultdict(int)
    for r in rows:
        e = events[r["event_id"]]
        source = e.source or {}
        if (
            r["account_id"] != obs.account_id
            or r["entity_id"] != entity
            or r["date"] != obs.date.isoformat()
            or e.id not in descendants
            or source.get("observation_id", obs.id) != obs.id
            or any(i != obs.id for i in source.get("observation_ids", []))
            or any(i not in descendants for i in source.get("source_event_ids", []))
        ):
            fail(
                "cash_lineage",
                "Cash must belong to the same observation, account, date and correction ancestry",
            )
        amount = r["debit_cents"] - r["credit_cents"]
        totals[e.id] += amount
        key = tuple(r.get(k) for k in DIMENSIONS)
        scopes[key] += amount
        if r["line_id"] not in physical:
            physical[r["line_id"]] = {**r, "debit_cents": 0, "credit_cents": 0}
        physical[r["line_id"]]["debit_cents"] += r["debit_cents"]
        physical[r["line_id"]]["credit_cents"] += r["credit_cents"]
    originals = [r for r in physical.values() if r["event_id"] == obs.event_id]
    if (
        len(originals) != 1
        or totals[obs.event_id] != obs.amount_cents
        or any(n for i, n in totals.items() if i != obs.event_id)
    ):
        fail(
            "cash_lineage",
            "Require one original movement and independently zero-net correcting events",
        )
    normalized = [
        {
            **originals[0],
            **dict(zip(DIMENSIONS, key)),
            "debit_cents": max(n, 0),
            "credit_cents": max(-n, 0),
        }
        for key, n in scopes.items()
        if n
    ]
    return normalized, list(physical.values())
