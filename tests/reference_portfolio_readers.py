"""Frozen pre-optimization reference for exact state/output performance parity.

Copied from source commit 342c2f56f7c9df4ba063a709adb9d15d5033aff7.
Test/benchmark only; never imported by application or shipped runtime entrypoints.
"""

from collections import defaultdict
from datetime import date
from sqlalchemy import or_, exists, select, func
from app.models.accounts import Account
from app.models.transactions import Transaction, TransactionLine
from app.portfolio import models as m
from app.portfolio.allocations import DIMENSIONS
from app.portfolio.service import cents, values, get, digest
from app.portfolio.reports import read_snapshot


def ledger(
    db,
    end,
    entity_ids=None,
    property_id=None,
    account_id=None,
    basis="accrual",
    event_ids=None,
):
    query = (
        db.query(
            TransactionLine,
            Transaction,
            m.JournalScope,
            m.LineScope,
            m.AccountScope,
            Account,
        )
        .join(Transaction, Transaction.id == TransactionLine.transaction_id)
        .join(m.JournalScope, m.JournalScope.transaction_id == Transaction.id)
        .join(m.LineScope, m.LineScope.line_id == TransactionLine.id)
        .join(m.AccountScope, m.AccountScope.account_id == TransactionLine.account_id)
        .join(Account, Account.id == TransactionLine.account_id)
        .filter(Transaction.date <= end)
    )
    if event_ids is not None:
        query = query.filter(m.JournalScope.event_id.in_(event_ids))
    if entity_ids:
        query = query.filter(m.JournalScope.entity_id.in_(entity_ids))
    if account_id:
        query = query.filter(Account.id == account_id)
    # Keep date ordering identical while restricting override reads to this
    # ledger's actual line scope. The subquery avoids SQLite parameter limits
    # and includes pre-allocation lines before property filtering.
    scoped_line_ids = query.with_entities(TransactionLine.id)
    overrides = {
        x.line_id: x
        for x in db.query(m.CashClassification)
        .filter(
            m.CashClassification.date <= end,
            m.CashClassification.line_id.in_(scoped_line_ids),
        )
        .order_by(m.CashClassification.date, m.CashClassification.id)
    }
    rows = []
    for ln, txn, js, dim, sc, acct in query.order_by(
        Transaction.date, Transaction.id, TransactionLine.id
    ):
        rows.append(
            {
                "line_id": ln.id,
                "classification_event_id": (
                    overrides[ln.id].event_id if ln.id in overrides else js.event_id
                ),
                "event_id": js.event_id,
                "entity_id": js.entity_id,
                "date": txn.date.isoformat(),
                "account_id": acct.id,
                "name": acct.name,
                "type": acct.account_type.value,
                "role": sc.role,
                "cash_flow": (
                    overrides[ln.id].cash_flow if ln.id in overrides else dim.cash_flow
                ),
                "debit_cents": cents(ln.debit),
                "credit_cents": cents(ln.credit),
                "unit_id": dim.unit_id,
                "property_id": dim.property_id,
                "fund_id": dim.fund_id,
                "counterparty_id": dim.counterparty_id,
                "item": dim.item,
                "opening": txn.source_type == "re_opening",
                "virtual": False,
            }
        )
    from app.portfolio.allocations import effective_map

    replacements = effective_map(db, end, [row["line_id"] for row in rows])
    effective_rows = []
    for row in rows:
        replacement = replacements.get(row["line_id"])
        if replacement:
            for component in replacement.components:
                amount = component["amount_cents"]
                classification = overrides.get(row["line_id"])
                later_classification = classification and (
                    classification.date,
                    classification.event_id,
                ) > (replacement.date, replacement.event_id)
                effective_rows.append(
                    {
                        **row,
                        **{k: v for k, v in component.items() if k != "amount_cents"},
                        "debit_cents": max(amount, 0),
                        "credit_cents": max(-amount, 0),
                        "allocation_event_id": replacement.event_id,
                        "cash_flow": (
                            classification.cash_flow
                            if later_classification
                            else component.get("cash_flow")
                        ),
                        "classification_event_id": (
                            classification.event_id
                            if later_classification
                            else replacement.event_id
                        ),
                    }
                )
        else:
            effective_rows.append(row)
    rows = [
        row
        for row in effective_rows
        if property_id is None or row.get("property_id") == property_id
    ]
    if basis == "modified_cash_bank_posting":
        query = db.query(m.BasisAdjustment).filter(m.BasisAdjustment.date <= end)
        if event_ids is not None:
            query = query.filter(m.BasisAdjustment.event_id.in_(event_ids))
        if entity_ids:
            query = query.filter(m.BasisAdjustment.entity_id.in_(entity_ids))
        for adjustment in query:
            for ln in adjustment.lines:
                if property_id and ln.get("property_id") != property_id:
                    continue
                if account_id and ln["account_id"] != account_id:
                    continue
                acct = get(db, Account, ln["account_id"])
                sc = get(db, m.AccountScope, acct.id)
                rows.append(
                    {
                        **ln,
                        "line_id": None,
                        "event_id": adjustment.event_id,
                        "entity_id": adjustment.entity_id,
                        "date": adjustment.date.isoformat(),
                        "name": acct.name,
                        "type": acct.account_type.value,
                        "role": sc.role,
                        "opening": False,
                        "virtual": True,
                        "reason": adjustment.reason,
                    }
                )
    return rows


SOURCE_MOVEMENT_KINDS = {
    "bank": "bank_cash",
    "credit_card": "card_liability",
    "bridge": "clearing_transit",
}


def baseline_observation_state(db, observation_id, through=None):
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
    from app.portfolio.service import Problem

    if obs.event_id:
        try:
            normalized, physical = baseline_normalized_cash(db, obs, state)
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


def baseline_normalized_cash(db, obs, state):
    from app.portfolio.service import fail, get, scope

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
