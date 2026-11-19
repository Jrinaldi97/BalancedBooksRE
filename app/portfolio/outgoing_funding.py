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


def _funding_inputs(db, ids):
    from app.portfolio.service import scope, values, digest, fail
    from app.portfolio.observation_state import observation_states
    from app.portfolio.ledger_snapshot import LedgerSnapshot
    from app.portfolio.review_workspace import original_review_code

    states = observation_states(db, ids)
    account_ids = {state["observation"]["account_id"] for state in states}
    if len(account_ids) != 1:
        fail("funding_scope", "Select withdrawals from a single bank account")
    bank = scope(db, account_ids.pop())
    if bank.role != "bank" or any(
        s["observation"]["amount_cents"] >= 0 or s["observation"]["status"] != "posted"
        for s in states
    ):
        fail("funding_scope", "Select posted bank withdrawals")
    funds = (
        db.query(m.Fund, m.Party)
        .join(m.Party, m.Party.id == m.Fund.beneficiary_id)
        .filter(m.Fund.holder_entity_id == bank.entity_id)
        .order_by(m.Fund.id)
        .all()
    )
    own = []
    unresolved = []
    for fund, party in funds:
        holder_owned = party.entity_id == bank.entity_id
        if (
            fund.purpose in {"general", "reserve"}
            and holder_owned
            and party.kind != "allocation_pending"
            and fund.restriction == "unrestricted"
        ):
            own.append(fund.id)
        if fund.purpose != "unresolved" or fund.property_id or fund.unit_id:
            continue
        # The legacy pooled placeholder has its own non-owner party and an
        # allocation-status description. Other free-text restrictions remain binding.
        pending_party = party.kind == "allocation_pending" and party.entity_id is None
        if ((holder_owned or pending_party) and fund.restriction == "unrestricted") or (
            pending_party
            and fund.restriction.strip().casefold()
            in {"allocation pending", "client and operating allocation pending"}
        ):
            unresolved.append(fund.id)
    packet = LedgerSnapshot(db, date.max, account_id=bank.account_id)
    account_digest = digest(
        {
            "account": values(bank),
            "funds": [(values(f), values(p)) for f, p in funds],
            "lines": packet.rows,
            "allocations": packet.allocations,
            "classifications": packet.classifications,
            "basis_adjustments": packet.adjustments,
        }
    )
    result = []
    for state in states:
        obs = state["observation"]
        reviews = []
        for row in state["reviews"]:
            if row["status"] == "open" and row["code"] in {
                "funding_source",
                "software_limitation",
            }:
                review = db.get(m.ReviewItem, row["id"])
                if original_review_code(db, review) == "funding_source":
                    reviews.append(
                        {
                            "review_id": review.id,
                            "review_code": review.code,
                            "obligation_event_id": review.event_id,
                        }
                    )
        result.append(
            {
                "observation_id": obs["id"],
                "account_id": bank.account_id,
                "entity_id": bank.entity_id,
                "date": obs["date"],
                "amount_cents": -obs["amount_cents"],
                "state_digest": state["state_digest"],
                "funding_digest": digest(
                    [account_digest, state["state_digest"], reviews]
                ),
                "eligible_own_fund_ids": own,
                "unresolved_source_fund_ids": unresolved,
                "funding_reviews": reviews,
                "cash_lineage": state.get("cash_lineage"),
                "scope": "Supply evidence for this withdrawal's actual ownership. Eligibility alone does not establish financing.",
            }
        )
    return states, result


def outgoing_funding_assessments(db, ids):
    return {"assessments": _funding_inputs(db, ids)[1]}


def outgoing_funding_assessment(db, observation_id):
    return _funding_inputs(db, [observation_id])[1][0]


def resolve_outgoing_funding_source(db, data, actor):
    return _resolve_withdrawals(db, [data], actor, single=True)


def resolve_outgoing_funding_batch(db, data, actor):
    return _resolve_withdrawals(db, data.withdrawals, actor, single=False)


def _resolve_withdrawals(db, withdrawals, actor, *, single):
    from app.portfolio.service import fail
    from app.portfolio.ledger_snapshot import LedgerSnapshot
    from app.portfolio.dated_balances import outgoing_fund_availability
    from app.portfolio.observation_state import observation_states

    ids = [w.observation_id for w in withdrawals]
    states, assessments = _funding_inputs(db, ids)
    touched_events = set()
    days = defaultdict(set)
    results = []
    for w, state, assessment in zip(withdrawals, states, assessments):
        if touched_events.intersection(state["event_ids"]):
            fail(
                "cash_lineage",
                "Withdrawals in a batch must have independent correction families",
            )
        touched_events.update(state["event_ids"])
        results.append(_attribute_withdrawal(db, w, actor, state, assessment))
        for component in w.components:
            if component.own_fund_id:
                days[component.own_fund_id].add(assessment["date"])

    db.flush()
    packet = LedgerSnapshot(db, date.max, account_id=assessments[0]["account_id"])
    availability = outgoing_fund_availability(packet, days)
    for before, after, result in zip(states, observation_states(db, ids), results):
        obs = before["observation"]
        expected = {c["fund_id"]: c["amount_cents"] for c in result["effective_funds"]}
        # A reciprocal intercompany family legitimately contains a second bank.
        # Verify this observation's exact immutable physical source on both bases.
        projections = packet.project_bases(
            date.fromisoformat(obs["date"]), event_ids=[obs["event_id"]]
        )
        for rows in projections.values():
            totals = defaultdict(int)
            if not rows or any(
                row["line_id"] != result["physical_line_id"] for row in rows
            ):
                fail(
                    "funding_invariant",
                    "The original bank movement no longer has one physical source",
                )
            for row in rows:
                totals[row.get("fund_id")] += row["debit_cents"] - row["credit_cents"]
            if dict(totals) != expected:
                fail(
                    "funding_invariant",
                    "Effective cash funds differ from the proposed attribution",
                )
        if (
            after["bank_movements"] != before["bank_movements"]
            or after["suspense_residuals"] != before["suspense_residuals"]
        ):
            fail(
                "funding_invariant",
                "Funding attribution changed bank movements or classification",
            )
    if single:
        return {**results[0], "availability": availability}
    return {
        "withdrawals": results,
        "event_ids": [r["event_id"] for r in results],
        "resolved_review_ids": [i for r in results for i in r["resolved_review_ids"]],
        "open_funding_review_ids": [
            i for r in results for i in r["open_funding_review_ids"]
        ],
        "remaining_cents": sum(r["remaining_cents"] for r in results),
        "availability": availability,
        "atomic": True,
        "bank_cash_delta_cents": 0,
        "financial_entries_added": False,
    }


def _attribute_withdrawal(db, data, actor, state, assessment):
    from app.portfolio.service import get, scope, fail, check_date
    from app.portfolio.reports import ledger

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
    source = event.source or {}
    if (
        review.status != "open"
        or review.id not in {r["review_id"] for r in assessment["funding_reviews"]}
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
    if not data.evidence.strip() or any(
        not c.evidence.strip() for c in data.components
    ):
        fail(
            "funding_evidence",
            "Describe the source evidence for the withdrawal and each component",
        )
    if data.evidence_policy_id:
        policy = get(db, m.ReportingPolicy, data.evidence_policy_id)
        if not isinstance(policy.assumptions, dict):
            fail("funding_evidence", "The policy scope must be a structured record")
        listed = policy.assumptions.get("observation_ids")
        if (
            policy.entity_id != bank.entity_id
            or policy.start > obs.date
            or (policy.end and policy.end < obs.date)
            or (
                listed is not None
                and (not isinstance(listed, list) or obs.id not in listed)
            )
            or db.query(m.ReportingPolicy.id).filter_by(supersedes_id=policy.id).first()
        ):
            fail(
                "funding_evidence",
                "Use a current policy for this entity, date and withdrawal",
            )
    from app.portfolio.ledger_snapshot import LedgerSnapshot

    families = LedgerSnapshot(db, date.max, event_ids=state["event_ids"]).project_bases(
        date.max
    )
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
        (
            event.kind == "posted_party_balance"
            and not state["cash_lineage"]["supported"]
        )
        or len(
            [
                s
                for s in state["source_observations"]
                if s["account_id"] == obs.account_id and s["event_id"] == obs.event_id
            ]
        )
        != 1
        or not cash
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
        if original_fund and original_fund.id not in (
            assessment["eligible_own_fund_ids"]
            + assessment["unresolved_source_fund_ids"]
        ):
            fail(
                "client_financing",
                "Client or restricted funding cannot be certified as company financing",
            )
        target = None
        if component.own_fund_id:
            if component.own_fund_id not in assessment["eligible_own_fund_ids"]:
                fail(
                    "funding_owner",
                    "The source must be a real unrestricted fund owned by the paying entity",
                )
            target = get(db, m.Fund, component.own_fund_id)
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
                if target and any(
                    getattr(target, dim) and getattr(target, dim) != row.get(dim)
                    for dim in ("property_id", "unit_id")
                ):
                    fail(
                        "funding_scope",
                        "The target fund is limited to another property or unit",
                    )
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
        code="RE-"
        + uuid.uuid5(uuid.NAMESPACE_URL, key + f":funding:{obs.id}").hex[:16],
        kind="outgoing_funding_attestation",
        date=obs.date,
        description="Withdrawal funding-source evidence",
        rationale=data.evidence,
        actor=actor,
        corrects_id=obs.event_id,
        source={
            "observation_id": obs.id,
            "review_id": review.id,
            "physical_line_id": line_id,
            "evidence_policy_id": data.evidence_policy_id,
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
    effective = defaultdict(int)
    for part in wanted:
        effective[part["fund_id"]] += part["amount_cents"]
    return {
        "physical_line_id": line_id,
        "effective_funds": [
            {"fund_id": fid, "amount_cents": amount}
            for fid, amount in effective.items()
        ],
        "obligation_event_id": event.id,
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
