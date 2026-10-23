"""Causal proofs for one allocation or an explicitly assessed correction set."""

from datetime import date
from app.portfolio import models as m
from app.portfolio.allocations import effective_map
from app.portfolio.reports import ledger
from app.models.transactions import TransactionLine, Transaction


def canonical_event_ids(ids):
    from app.portfolio.service import fail

    if ids is None:
        return None
    if (
        not ids
        or len(ids) > 100
        or any(type(i) is not int or i <= 0 for i in ids)
        or len(set(ids)) != len(ids)
    ):
        fail(
            "correction_set",
            "Choose one to 100 unique positive correction event identities",
        )
    return sorted(ids)


def allocation_evidence(db, event_ids, through):
    from app.portfolio.service import get, values, digest, scope
    from app.portfolio.reconciliation_evidence import coverage_snapshot

    events = [get(db, m.Event, i) for i in sorted(set(event_ids))]
    allocations = (
        db.query(m.LineAllocation)
        .filter(m.LineAllocation.event_id.in_(event_ids))
        .order_by(m.LineAllocation.id)
        .all()
    )
    active = effective_map(db, through, [a.line_id for a in allocations])
    obs_ids = set()
    for event in events:
        source = event.source or {}
        if type(source.get("observation_id")) is int:
            obs_ids.add(source["observation_id"])
        obs_ids.update(i for i in source.get("observation_ids", []) if type(i) is int)
    affected_accounts = {
        get(db, TransactionLine, a.line_id).account_id for a in allocations
    }
    return {
        "event_ids": sorted(set(event_ids)),
        "events": [values(e) for e in events],
        "allocations": [values(a) for a in allocations],
        "effective_revisions": [values(active[i]) for i in sorted(active)],
        "source_observations": [
            values(get(db, m.BankObservation, i)) for i in sorted(obs_ids)
        ],
        "affected_scope_state": [
            {
                "account_id": account_id,
                "ledger_digest": digest(ledger(db, through, account_id=account_id)),
                "allocation_history_digest": digest(
                    [
                        values(a)
                        for a in db.query(m.LineAllocation)
                        .join(
                            TransactionLine,
                            TransactionLine.id == m.LineAllocation.line_id,
                        )
                        .filter(
                            TransactionLine.account_id == account_id,
                            m.LineAllocation.date <= through,
                        )
                        .order_by(m.LineAllocation.date, m.LineAllocation.id)
                    ]
                ),
                "coverage": coverage_snapshot(db, scope(db, account_id), through),
            }
            for account_id in sorted(affected_accounts)
        ],
    }


def validate_allocations(db, assessment, event_ids):
    from app.portfolio.service import get, fail, cents, scope

    alert_date = date.fromisoformat(assessment["alert_date"])
    account_id, fund_id = assessment["account_id"], assessment["fund_id"]
    historical = assessment["effective_historical_balance_cents"]
    if historical < 0:
        fail(
            "unrelated_correction",
            "Require an allocation that disproves the original dated shortage",
        )
    selected = set(event_ids)
    allocations = (
        db.query(m.LineAllocation).filter(m.LineAllocation.event_id.in_(selected)).all()
    )
    effective = effective_map(db, alert_date, [a.line_id for a in allocations])
    impact = {ident: 0 for ident in selected}
    replacements = set()
    starts = []
    proofs = []
    line_ids = set()
    for allocation in allocations:
        active = effective.get(allocation.line_id)
        if not active or active.id != allocation.id or allocation.line_id in line_ids:
            fail(
                "inactive_correction",
                "Use only the effective correction revision for each physical source line",
            )
        line_ids.add(allocation.line_id)
        physical = get(db, TransactionLine, allocation.line_id)
        transaction = get(db, Transaction, physical.transaction_id)
        journal = get(db, m.JournalScope, transaction.id)
        original = get(db, m.Event, journal.event_id)
        correction = get(db, m.Event, allocation.event_id)
        bank = scope(db, physical.account_id)
        if bank.role != "bank":
            fail(
                "allocation_lineage",
                "Historical bank shortages require bank allocation sources",
            )
        source = correction.source or {}
        obs_ids = set(source.get("observation_ids", []))
        if source.get("observation_id"):
            obs_ids.add(source["observation_id"])
        obs_id = (original.source or {}).get("observation_id")
        obs = db.get(m.BankObservation, obs_id) if type(obs_id) is int else None

        def ancestors(event):
            seen = set()
            while event:
                if event.id in seen:
                    fail("allocation_lineage", "Correction ancestry contains a cycle")
                seen.add(event.id)
                event = (
                    get(db, m.Event, event.corrects_id) if event.corrects_id else None
                )
            return seen

        physical_ancestors = ancestors(original)
        correction_ancestors = ancestors(correction) - {correction.id}
        explicit_origin = bool(
            physical_ancestors
            & (correction_ancestors | set(source.get("source_event_ids", [])))
        )
        if (
            not explicit_origin
            or not obs
            or obs.id not in obs_ids
            or obs.account_id != physical.account_id
            or obs.date != transaction.date
            or obs.status != "posted"
            or allocation.date != transaction.date
            or correction.date != allocation.date
            or correction.date > alert_date
        ):
            fail(
                "allocation_lineage",
                "Allocation must retain explicit source observation and correction ancestry",
            )
        amount = cents(physical.debit) - cents(physical.credit)
        if sum(c["amount_cents"] for c in allocation.components) != amount:
            fail(
                "allocation_lineage",
                "Replacement allocation must preserve the physical source amount",
            )
        prior = (
            db.get(m.LineAllocation, allocation.supersedes_id)
            if allocation.supersedes_id
            else None
        )
        if prior and (prior.line_id != allocation.line_id or prior.id >= allocation.id):
            fail(
                "allocation_lineage",
                "The allocation predecessor belongs to another source",
            )
        before = (
            prior.components
            if prior
            else [
                {
                    "amount_cents": amount,
                    "fund_id": get(db, m.LineScope, allocation.line_id).fund_id,
                }
            ]
        )
        delta = sum(
            c["amount_cents"]
            for c in allocation.components
            if fund_id is None or c.get("fund_id") == fund_id
        ) - sum(
            c["amount_cents"]
            for c in before
            if fund_id is None or c.get("fund_id") == fund_id
        )
        if physical.account_id == account_id:
            impact[allocation.event_id] += delta
        # Removing a positive receipt can exhaust its former beneficiary even
        # when that fund disappears completely from the replacement components.
        for component in [*before, *allocation.components]:
            replacement_fund = component.get("fund_id")
            if (
                replacement_fund is not None
                and get(db, m.Fund, replacement_fund).holder_entity_id != bank.entity_id
            ):
                fail(
                    "allocation_lineage",
                    "Replacement fund belongs to another bank holder",
                )
            replacements.add((physical.account_id, replacement_fund))
        starts.append(transaction.date)
        proofs.append(
            {
                "event_id": correction.id,
                "allocation_id": allocation.id,
                "supersedes_allocation_id": allocation.supersedes_id,
                "line_id": physical.id,
                "source_event_id": original.id,
                "observation_id": obs.id,
                "account_id": physical.account_id,
                "before_components": before,
                "after_components": allocation.components,
            }
        )
    legacy_by_id = {
        c["event_id"]: c for c in assessment["legacy_allocation_corrections"]
    }
    for ident in selected - {a.event_id for a in allocations}:
        legacy = legacy_by_id.get(ident)
        if not legacy:
            fail(
                "unrelated_correction",
                "Require an allocation that disproves the original dated shortage",
            )
        correction = get(db, m.Event, ident)
        impact[ident] = legacy["scope_delta_cents"]
        replacements.update(
            (account_id, fund) for fund in legacy["replacement_fund_ids"]
        )
        starts.append(correction.date)
        proofs.append({"event_id": ident, "legacy": True, **legacy})
    if any(n <= 0 for n in impact.values()) or historical - sum(impact.values()) >= 0:
        fail(
            "unrelated_correction",
            "The complete allocation set must causally disprove the selected dated shortage",
        )
    if not starts or not replacements:
        fail("unrelated_correction", "Require a source-supported allocation correction")
    start = min(starts)
    checked = []
    for bank_id in sorted({a for a, _ in replacements}):
        if bank_id != account_id:
            from app.portfolio.reconciliation_evidence import validate_coverage

            validate_coverage(
                db, scope(db, bank_id), date.fromisoformat(assessment["as_of"])
            )
        rows = ledger(db, alert_date, account_id=bank_id)
        days = sorted(
            {start, alert_date}
            | {
                date.fromisoformat(r["date"])
                for r in rows
                if r["date"] >= start.isoformat()
            }
        )
        # Allocation effective dates can change holdings on a day with no new
        # physical transaction, so include them as well.
        days = sorted(
            set(days)
            | {
                a.date
                for a in db.query(m.LineAllocation)
                .join(TransactionLine, TransactionLine.id == m.LineAllocation.line_id)
                .filter(
                    TransactionLine.account_id == bank_id,
                    m.LineAllocation.date >= start,
                    m.LineAllocation.date <= alert_date,
                )
            }
        )
        for day in days:
            dated = ledger(db, day, account_id=bank_id)
            for _, replacement_fund in sorted(
                (p for p in replacements if p[0] == bank_id), key=repr
            ):
                # None is the unassigned scope, not the whole-account balance.
                amount = sum(
                    r["debit_cents"] - r["credit_cents"]
                    for r in dated
                    if r["fund_id"] == replacement_fund
                )
                if amount < 0:
                    fail(
                        "replacement_deficit",
                        "A replacement scope has a historical shortfall during the corrected interval",
                    )
                checked.append(
                    {
                        "account_id": bank_id,
                        "fund_id": replacement_fund,
                        "date": day.isoformat(),
                        "balance_cents": amount,
                    }
                )
    return {
        "corrective_event_ids": sorted(selected),
        "combined_scope_delta_cents": sum(impact.values()),
        "counterfactual_balance_cents": historical - sum(impact.values()),
        "corrections": proofs,
        "replacement_balances": checked,
        "corrected_interval": {
            "start": start.isoformat(),
            "end": alert_date.isoformat(),
        },
    }
