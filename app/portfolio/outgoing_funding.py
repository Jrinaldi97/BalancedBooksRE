"""Source attribution for an already recorded withdrawal and obligation.

This operation never creates cash, principal or a journal. Replacements refer
to one immutable physical bank line; unsupported components keep their review.
"""

import json
import uuid
from collections import defaultdict
from datetime import date

from app.portfolio import models as m
from app.portfolio.allocations import DIMENSIONS

BASES = ("accrual", "modified_cash_bank_posting")


def _obligation_vector(rows, predicate):
    totals = defaultdict(int)
    for row in rows:
        if predicate(row):
            key = (
                row["entity_id"],
                row["account_id"],
                *(row.get(k) for k in DIMENSIONS),
            )
            totals[key] += row["debit_cents"] - row["credit_cents"]
    return {key: amount for key, amount in totals.items() if amount}


def outgoing_funding_assessment(db, observation_id):
    from app.portfolio.service import get, scope, values, digest, fail
    from app.portfolio.observation_state import observation_state
    from app.portfolio.reports import ledger, read_snapshot
    from app.models.transactions import TransactionLine

    read_snapshot(db)
    obs = get(db, m.BankObservation, observation_id)
    bank = scope(db, obs.account_id)
    if bank.role != "bank" or obs.amount_cents >= 0 or obs.status != "posted":
        fail("funding_scope", "Select a posted bank withdrawal")
    state = observation_state(db, obs.id)
    funds = (
        db.query(m.Fund)
        .filter_by(holder_entity_id=bank.entity_id)
        .order_by(m.Fund.id)
        .all()
    )
    parties = {
        f.beneficiary_id: values(get(db, m.Party, f.beneficiary_id)) for f in funds
    }
    rows = {
        basis: ledger(db, date.max, account_id=obs.account_id, basis=basis)
        for basis in BASES
    }
    account_lines = db.query(TransactionLine.id).filter(
        TransactionLine.account_id == obs.account_id
    )
    allocations = [
        values(a)
        for a in db.query(m.LineAllocation)
        .filter(m.LineAllocation.line_id.in_(account_lines))
        .order_by(m.LineAllocation.id)
    ]
    payload = {
        "state_digest": state["state_digest"],
        "account": values(bank),
        "funds": [values(f) for f in funds],
        "parties": parties,
        "ledger": rows,
        "allocations": allocations,
    }
    return {
        "observation_id": obs.id,
        "account_id": obs.account_id,
        "entity_id": bank.entity_id,
        "date": obs.date.isoformat(),
        "amount_cents": -obs.amount_cents,
        "state_digest": state["state_digest"],
        "funding_digest": digest(payload),
        "eligible_own_fund_ids": [
            f.id
            for f in funds
            if f.purpose != "unresolved"
            and f.restriction == "unrestricted"
            and parties[f.beneficiary_id]["entity_id"] == bank.entity_id
        ],
        "scope": "This withdrawal only. Fund ownership and source evidence must be supplied; account balances alone do not prove financing.",
    }


def resolve_outgoing_funding_source(db, data, actor):
    from app.portfolio.service import get, scope, fail, check_date
    from app.portfolio.observation_state import observation_state
    from app.portfolio.reports import ledger

    assessment = outgoing_funding_assessment(db, data.observation_id)
    if assessment["state_digest"] != data.expected_state_digest:
        fail("stale_state", "Refresh this withdrawal before resolving its source")
    if assessment["funding_digest"] != data.expected_funding_digest:
        fail(
            "stale_funding",
            "Funding availability or ownership changed; refresh the assessment",
        )
    obs = get(db, m.BankObservation, data.observation_id)
    bank = scope(db, obs.account_id)
    review = get(db, m.ReviewItem, data.review_id)
    event = get(db, m.Event, data.obligation_event_id)
    state = observation_state(db, obs.id)
    source = event.source or {}
    if (
        review.status != "open"
        or review.code != "funding_source"
        or review.observation_id != obs.id
        or review.event_id != event.id
        or review.entity_id != bank.entity_id
    ):
        fail("review_state", "Choose the exact withdrawal's open funding-source review")
    if event.id not in state["event_ids"] or any(state["suspense_residuals"].values()):
        fail(
            "funding_purpose",
            "Establish the complete obligation before resolving its source",
        )
    families = {
        basis: ledger(db, date.max, event_ids=state["event_ids"], basis=basis)
        for basis in BASES
    }
    obligations = {
        basis: [row for row in rows if row["event_id"] == event.id]
        for basis, rows in families.items()
    }
    family = families["accrual"]
    obligation = obligations["accrual"]
    if event.kind == "posted_party_balance":
        if (
            source.get("party_balance_kind") != "party_receivable"
            or source.get("observation_id") != obs.id
            or source.get("party_id") != data.party_id
            or data.counterparty_entity_id is not None
        ):
            fail(
                "funding_party",
                "Evidence must identify this receivable's actual debtor",
            )
        if get(db, m.Party, data.party_id).entity_id is not None:
            fail(
                "funding_party", "Use the intercompany workflow for a maintained entity"
            )
        item = (
            db.query(m.OpenItem)
            .filter_by(event_id=event.id, kind="party_receivable", code=data.item)
            .one_or_none()
        )
        if (
            not item
            or item.entity_id != bank.entity_id
            or item.amount_cents != -obs.amount_cents
            or scope(db, item.control_id, bank.entity_id).role != "receivable"
        ):
            fail(
                "funding_item",
                "The original receivable does not match this withdrawal and item",
            )
        if (
            sum(
                r["debit_cents"] - r["credit_cents"]
                for r in obligation
                if r["account_id"] == item.control_id and r["item"] == data.item
            )
            != -obs.amount_cents
        ):
            fail(
                "funding_item",
                "The original obligation amount differs from the withdrawal",
            )

        def obligation_scope(row):
            return row["account_id"] == item.control_id and row.get("item") == data.item

    elif event.kind == "posted_intercompany":
        if (
            source.get("purpose") != "advance"
            or source.get("item") != data.item
            or obs.id not in source.get("observation_ids", [])
            or data.party_id is not None
            or not data.counterparty_entity_id
            or data.counterparty_entity_id == bank.entity_id
        ):
            fail(
                "funding_party",
                "Evidence must identify this advance's actual borrower and item",
            )
        due_from = sum(
            r["debit_cents"] - r["credit_cents"]
            for r in obligation
            if r["role"] == "due_from"
            and r["entity_id"] == bank.entity_id
            and r["counterparty_id"] == data.counterparty_entity_id
            and r["item"] == data.item
        )
        due_to = sum(
            r["debit_cents"] - r["credit_cents"]
            for r in obligation
            if r["role"] == "due_to"
            and r["entity_id"] == data.counterparty_entity_id
            and r["counterparty_id"] == bank.entity_id
            and r["item"] == data.item
        )
        if due_from != -obs.amount_cents or due_to != obs.amount_cents:
            fail(
                "funding_item",
                "Both original intercompany obligations must match the actual withdrawal",
            )

        def obligation_scope(row):
            return row.get("item") == data.item and (
                (
                    row["role"] == "due_from"
                    and row["entity_id"] == bank.entity_id
                    and row.get("counterparty_id") == data.counterparty_entity_id
                )
                or (
                    row["role"] == "due_to"
                    and row["entity_id"] == data.counterparty_entity_id
                    and row.get("counterparty_id") == bank.entity_id
                )
            )

    else:
        fail(
            "funding_workflow",
            "Use this workflow for a typed party receivable or intercompany advance",
        )
    original_vector = _obligation_vector(obligation, obligation_scope)
    for basis in BASES:
        if (
            _obligation_vector(obligations[basis], obligation_scope) != original_vector
            or _obligation_vector(families[basis], obligation_scope) != original_vector
        ):
            fail(
                "funding_purpose",
                "The linked obligation has changed in scope or on a reporting basis; reassess its purpose first",
            )
    cash = [
        r for r in family if r["role"] == "bank" and r["account_id"] == obs.account_id
    ]
    if (
        not cash
        or len({r["line_id"] for r in cash}) != 1
        or any(
            r["date"] != obs.date.isoformat()
            or r["event_id"] != obs.event_id
            or r["debit_cents"]
            or r["credit_cents"] <= 0
            for r in cash
        )
        or sum(r["credit_cents"] for r in cash) != -obs.amount_cents
    ):
        fail(
            "cash_lineage",
            "Require one original withdrawal line with unchanged account, date and amount",
        )
    line_id = cash[0]["line_id"]
    revisions = (
        db.query(m.LineAllocation)
        .filter_by(line_id=line_id)
        .order_by(m.LineAllocation.date, m.LineAllocation.id)
        .all()
    )
    if any(r.date > obs.date for r in revisions):
        fail(
            "funding_timing",
            "Later attribution revisions require a separately reviewed historical plan",
        )
    # Fund attribution is backdated to the actual withdrawal. Preserve the
    # classification effective then; later CashClassification records continue
    # to take effect on their actual dates.
    cash = [
        row
        for row in ledger(db, obs.date, event_ids=[obs.event_id])
        if row["line_id"] == line_id
    ]
    if sum(c.amount_cents for c in data.components) != -obs.amount_cents:
        fail(
            "funding_total",
            "Components, including unsupported remainder, must equal the exact withdrawal",
        )
    keys = [(c.source_fund_id, c.own_fund_id) for c in data.components]
    if len(set(keys)) != len(keys):
        fail(
            "duplicate_component", "Combine duplicate source and target fund components"
        )
    available = [{**r, "remaining": r["credit_cents"]} for r in cash]
    wanted = []
    unsupported = 0
    for component in data.components:
        original_fund = (
            get(db, m.Fund, component.source_fund_id)
            if component.source_fund_id
            else None
        )
        if original_fund and (
            original_fund.holder_entity_id != bank.entity_id
            or original_fund.restriction != "unrestricted"
            or (
                original_fund.purpose != "unresolved"
                and original_fund.id not in assessment["eligible_own_fund_ids"]
            )
        ):
            fail(
                "client_financing",
                "Client or restricted funding cannot be certified as company financing",
            )
        if component.own_fund_id:
            if component.own_fund_id not in assessment["eligible_own_fund_ids"]:
                fail(
                    "funding_owner",
                    "The source must be a real unrestricted fund owned by the paying entity",
                )
            if (
                original_fund
                and original_fund.purpose != "unresolved"
                and original_fund.id != component.own_fund_id
            ):
                fail(
                    "funding_scope",
                    "Existing identified ownership requires its own correction workflow",
                )
        else:
            unsupported += component.amount_cents
        remaining = component.amount_cents
        candidates = [
            row
            for row in available
            if row.get("fund_id") == component.source_fund_id and row["remaining"]
        ]
        if (
            remaining < sum(row["remaining"] for row in candidates)
            and len({tuple(row.get(k) for k in DIMENSIONS) for row in candidates}) > 1
        ):
            fail(
                "funding_scope",
                "Partial funding spans different existing dimensions; supply a separately scoped attribution plan",
            )
        for row in available:
            if row.get("fund_id") != component.source_fund_id or not row["remaining"]:
                continue
            amount = min(remaining, row["remaining"])
            if amount:
                wanted.append(
                    {
                        "amount_cents": -amount,
                        **{k: row.get(k) for k in DIMENSIONS},
                        "fund_id": component.own_fund_id or row.get("fund_id"),
                    }
                )
                row["remaining"] -= amount
                remaining -= amount
        if remaining:
            fail(
                "funding_scope",
                "A component exceeds its exact existing source attribution",
            )
    if any(r["remaining"] for r in available):
        fail(
            "funding_scope", "Every existing withdrawal component must be accounted for"
        )
    old_totals, new_totals = defaultdict(int), defaultdict(int)
    for row in cash:
        old_totals[tuple(row.get(k) for k in DIMENSIONS)] -= row["credit_cents"]
    for row in wanted:
        new_totals[tuple(row.get(k) for k in DIMENSIONS)] += row["amount_cents"]
    changed = dict(old_totals) != dict(new_totals)
    if changed:
        check_date(db, bank.entity_id, obs.date)
    # Bind the attestation to the original purpose and source; it contains no journal.
    key = db.info.get("portfolio_command_key", uuid.uuid4().hex)
    attestation = m.Event(
        code="RE-" + uuid.uuid5(uuid.NAMESPACE_URL, key + ":funding").hex[:16],
        kind="outgoing_funding_attestation",
        date=obs.date,
        description="Withdrawal funding-source evidence",
        rationale=data.evidence,
        actor=actor,
        corrects_id=obs.event_id,
        source={
            "observation_id": obs.id,
            "obligation_event_id": event.id,
            "party_id": data.party_id,
            "counterparty_entity_id": data.counterparty_entity_id,
            "item": data.item,
            "components": [c.model_dump() for c in data.components],
            "assessment_digest": data.expected_funding_digest,
            "unsupported_cents": unsupported,
        },
    )
    db.add(attestation)
    db.flush()
    if changed:
        db.add(
            m.LineAllocation(
                line_id=line_id,
                event_id=attestation.id,
                date=obs.date,
                components=wanted,
                evidence=data.evidence,
                supersedes_id=revisions[-1].id if revisions else None,
            )
        )
        db.flush()
    # Check every transaction/allocation boundary, not merely the final balance.
    from app.models.transactions import Transaction, TransactionLine

    days = {obs.date} | {
        d
        for (d,) in db.query(Transaction.date)
        .join(TransactionLine, TransactionLine.transaction_id == Transaction.id)
        .filter(
            TransactionLine.account_id == obs.account_id, Transaction.date >= obs.date
        )
        .distinct()
    }
    account_line_ids = db.query(TransactionLine.id).filter(
        TransactionLine.account_id == obs.account_id
    )
    days |= {
        d
        for (d,) in db.query(m.LineAllocation.date)
        .filter(
            m.LineAllocation.line_id.in_(account_line_ids),
            m.LineAllocation.date >= obs.date,
        )
        .distinct()
    }
    days |= {
        adjustment.date
        for adjustment in db.query(m.BasisAdjustment).filter(
            m.BasisAdjustment.entity_id == bank.entity_id,
            m.BasisAdjustment.date >= obs.date,
        )
        if any(row["account_id"] == obs.account_id for row in adjustment.lines)
    }
    own_ids = {c.own_fund_id for c in data.components if c.own_fund_id}
    for day in sorted(days):
        for basis in BASES:
            totals = defaultdict(int)
            for row in ledger(db, day, account_id=obs.account_id, basis=basis):
                if row["virtual"]:
                    fail(
                        "funding_basis",
                        "Source bank movements must agree on both bases; review cash basis adjustments first",
                    )
                totals[row.get("fund_id")] += row["debit_cents"] - row["credit_cents"]
            if any(totals[ident] < 0 for ident in own_ids):
                fail(
                    "funding_availability",
                    "The identified company fund lacks supported availability at a relevant date",
                )
    after = observation_state(db, obs.id)
    if (
        after["bank_movements"] != state["bank_movements"]
        or after["suspense_residuals"] != state["suspense_residuals"]
    ):
        fail(
            "funding_invariant",
            "Funding attribution changed cash movements or purpose classification",
        )
    review.resolution = json.dumps(
        {
            "reason": "identified_outgoing_source",
            "attestation_event_id": attestation.id,
            "obligation_event_id": event.id,
            "unsupported_cents": unsupported,
            "evidence": data.evidence,
            "scope": "This withdrawal only; no certification of unrelated cash",
        },
        sort_keys=True,
    )
    if not unsupported:
        review.status = "resolved"
        review.resolved_event_id = attestation.id
    return {
        "event_id": attestation.id,
        "observation_id": obs.id,
        "resolved_review_ids": [review.id] if not unsupported else [],
        "open_funding_review_ids": [review.id] if unsupported else [],
        "remaining_cents": unsupported,
        "bank_cash_delta_cents": 0,
        "financial_entries_added": False,
        "cash_attribution_unchanged": not changed,
        "external_evidence_verification": "not_attested",
    }
