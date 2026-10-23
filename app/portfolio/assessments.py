"""Evidence-bounded read-only investigation of historical shortage alerts."""

import re
from datetime import date
from app.models.transactions import TransactionLine, Transaction
from app.portfolio import models as m
from app.portfolio.reports import ledger, read_snapshot
from app.portfolio.service import get, fail, scope, cents, digest


def shortage_assessment(db, review_id, through=None, corrective_event_ids=None):
    read_snapshot(db)
    from app.portfolio.shortage_allocations import (
        canonical_event_ids,
        allocation_evidence,
    )
    from app.portfolio.reconciliation_evidence import coverage_snapshot

    selected = canonical_event_ids(corrective_event_ids)
    review = get(db, m.ReviewItem, review_id)
    if review.code != "actual_shortage" or not review.event_id:
        fail("review_type", "Choose an actual shortage alert with an originating event")
    event = get(db, m.Event, review.event_id)
    through = through or date.today()
    if through < event.date:
        fail("assessment_date", "Assessment cutoff precedes the original alert")
    # Existing alerts encode their precise triggering scope in this fixed text.
    # Refuse to guess from unrelated lines if older/free-form text differs.
    match = re.fullmatch(
        r"Actual bank activity leaves account (\d+)(?: fund (\d+))? with a shortage; verify coverage, opening balances and financing\. Obligations remain owed\.",
        review.detail,
    )
    if not match:
        return {
            "review_id": review.id,
            "classification": "unknown_alert_scope",
            "read_only": True,
            "reason": "Original alert scope cannot be identified without guessing",
        }
    account_id = int(match[1])
    fund_id = int(match[2]) if match[2] else None
    bank = scope(db, account_id)
    if bank.entity_id != review.entity_id:
        fail("alert_scope", "Alert account does not belong to the reviewed entity")
    rows = ledger(db, through, account_id=account_id)
    scoped = [r for r in rows if fund_id is None or r["fund_id"] == fund_id]
    historical_rows = ledger(db, event.date, account_id=account_id)
    historical = sum(
        r["debit_cents"] - r["credit_cents"]
        for r in historical_rows
        if fund_id is None or r["fund_id"] == fund_id
    )
    current = sum(r["debit_cents"] - r["credit_cents"] for r in scoped)
    originals = (
        db.query(TransactionLine, m.LineScope)
        .join(Transaction, Transaction.id == TransactionLine.transaction_id)
        .join(m.LineScope, m.LineScope.line_id == TransactionLine.id)
        .filter(
            TransactionLine.account_id == account_id, Transaction.date <= event.date
        )
    )
    if fund_id is not None:
        originals = originals.filter(m.LineScope.fund_id == fund_id)
    original_balance = sum(
        cents(line.debit) - cents(line.credit) for line, _ in originals
    )
    allocations = sorted(
        {
            r["allocation_event_id"]
            for r in historical_rows
            if r.get("allocation_event_id")
        }
    )
    legacy = legacy_allocation_candidates(
        db, account_id, fund_id, historical_rows, event
    )
    # coverage_start establishes the supported start, not proof that every
    # later statement is present or that an opening balance is correct.
    coverage_known = (
        bank.coverage_start is not None and bank.coverage_start <= event.date
    )
    if not coverage_known:
        classification = "insufficient_source_coverage"
    elif historical >= 0 and original_balance < 0 and allocations:
        classification = "allocation_artifact_candidate"
    elif historical < 0 and current >= 0:
        classification = "historical_shortfall_later_nonnegative"
    elif historical < 0:
        classification = "unresolved_shortfall"
    else:
        classification = "no_end_of_day_shortfall"
    later_receipts = [
        {
            "line_id": r["line_id"],
            "event_id": r["event_id"],
            "date": r["date"],
            "amount_cents": r["debit_cents"] - r["credit_cents"],
        }
        for r in scoped
        if r["date"] > event.date.isoformat() and r["debit_cents"] > r["credit_cents"]
    ]
    result = {
        "review_id": review.id,
        "review_status": review.status,
        "original_event_id": event.id,
        "account_id": account_id,
        "fund_id": fund_id,
        "alert_date": event.date.isoformat(),
        "as_of": through.isoformat(),
        "coverage_start": (
            bank.coverage_start.isoformat() if bank.coverage_start else None
        ),
        "original_scope_balance_cents": original_balance,
        "effective_historical_balance_cents": historical,
        "effective_current_balance_cents": current,
        "classification": classification,
        "allocation_event_ids": allocations,
        "legacy_allocation_corrections": legacy,
        "later_receipts": later_receipts,
        "read_only": True,
        "limitations": [
            "End-of-day balances do not establish intraday availability.",
            "Coverage start does not prove complete statements or an accurate opening balance.",
            "Later positivity and matching amounts do not prove the purpose or source of restoration.",
            "Allocation candidates require review of replacement scopes and correction evidence; no alert is resolved by this assessment.",
        ],
    }
    result["reconciliation_coverage"] = coverage_snapshot(db, bank, through)
    candidate_ids = sorted(
        set(allocations) | {c["event_id"] for c in legacy} | set(selected or [])
    )
    result["allocation_evidence"] = allocation_evidence(db, candidate_ids, through)
    if selected is not None:
        result["corrective_event_ids"] = selected
    result["assessment_digest"] = digest(result)
    return result


def legacy_allocation_candidates(
    db, account_id, fund_id, historical_rows, event, *, fast_path=True
):
    """Whole-account deltas cannot be both zero-net and positive."""
    legacy = []
    from app.portfolio.observation_state import observation_state
    from app.portfolio.cash_lineage import normalized_cash
    from app.portfolio.service import Problem

    if fund_id is None and fast_path:
        return legacy
    candidates = {r["event_id"] for r in historical_rows}
    for candidate in db.query(m.Event).filter(
        m.Event.id.in_(candidates), m.Event.corrects_id.isnot(None)
    ):
        obs_id = (candidate.source or {}).get("observation_id")
        obs = db.get(m.BankObservation, obs_id) if obs_id else None
        if not obs or obs.account_id != account_id:
            continue
        try:
            state = observation_state(db, obs.id, event.date)
            normalized_cash(db, obs, state)
        except Problem:
            continue
        selected = [r for r in historical_rows if r["event_id"] == candidate.id]
        delta = sum(
            r["debit_cents"] - r["credit_cents"]
            for r in selected
            if fund_id is None or r["fund_id"] == fund_id
        )
        if (
            selected
            and sum(r["debit_cents"] - r["credit_cents"] for r in selected) == 0
            and delta > 0
        ):
            legacy.append(
                {
                    "event_id": candidate.id,
                    "scope_delta_cents": delta,
                    "replacement_fund_ids": sorted(
                        {
                            r["fund_id"]
                            for r in selected
                            if r["credit_cents"] > r["debit_cents"]
                        },
                        key=repr,
                    ),
                }
            )
    return legacy


def resolve_historical(db, data, actor):
    """Resolve one dated alert using a fresh bounded proof, without posting."""
    import json
    from app.portfolio.reconciliation_evidence import validate_coverage
    from app.portfolio.shortage_allocations import validate_allocations
    from app.portfolio.shortage_lineage import restoration_receipt

    assessment = shortage_assessment(
        db, data.review_id, data.as_of, data.corrective_event_ids
    )
    if assessment.get("assessment_digest") != data.expected_assessment_digest:
        fail("stale_assessment", "Refresh the dated shortage assessment")
    review = get(db, m.ReviewItem, data.review_id)
    if review.status != "open":
        fail("review_state", "This alert is already resolved")
    bank = scope(db, assessment["account_id"])
    alert = get(db, m.Event, review.event_id)
    selected_ids = data.corrective_event_ids or [data.corrective_event_id]
    corrections = [get(db, m.Event, ident) for ident in selected_ids]
    if not bank.coverage_start or bank.coverage_start > alert.date:
        fail("incomplete_coverage", "A supported opening cutoff is required")
    if any(c.date > data.as_of for c in corrections):
        fail("correction_date", "Correction is after the selected historical cutoff")
    used = validate_coverage(db, bank, data.as_of)
    proof = {}
    if data.reason in {
        "source_allocation_corrected",
        "source_allocation_set_corrected",
    }:
        proof["allocation_causal_proof"] = validate_allocations(
            db, assessment, selected_ids
        )
    elif data.reason == "import_order_artifact":
        if (
            assessment["effective_historical_balance_cents"] < 0
            or corrections[0].id != alert.id
        ):
            fail(
                "historical_deficit",
                "An import-order assessment must refer to the original alert and nonnegative historical holdings",
            )
        if assessment["original_scope_balance_cents"] < 0 and (
            assessment["allocation_event_ids"]
            or assessment["legacy_allocation_corrections"]
        ):
            fail(
                "allocation_evidence_required",
                "Use the allocation proof for shortages removed by allocation corrections",
            )
    else:
        proof["restoration_receipt"] = restoration_receipt(
            db, assessment, corrections[0]
        )
    review.status = "resolved"
    # A combined proof has no invented single corrective journal.
    review.resolved_event_id = corrections[0].id if len(corrections) == 1 else None
    review.resolution = json.dumps(
        {
            "reason": data.reason,
            "evidence": data.evidence,
            "coverage_evidence": data.coverage_evidence,
            "assessment": assessment,
            "corrective_event_ids": selected_ids,
            "reconciliation_ids": used,
            "intraday_availability": "not_established",
            **proof,
        },
        sort_keys=True,
    )
    return {
        "review_id": review.id,
        "resolved_review_ids": [review.id],
        "assessment": assessment,
        "corrective_event_ids": selected_ids,
        "reconciliation_ids": used,
        "bank_cash_delta_cents": 0,
        "later_alerts_unchanged": True,
        "financial_entries_added": False,
        **proof,
    }
