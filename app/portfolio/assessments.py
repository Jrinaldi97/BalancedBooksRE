"""Evidence-bounded read-only investigation of historical shortage alerts."""

import re
from datetime import date
from app.models.transactions import TransactionLine, Transaction
from app.portfolio import models as m
from app.portfolio.reports import ledger, read_snapshot
from app.portfolio.service import get, fail, scope, cents, digest


def shortage_assessment(db, review_id, through=None):
    read_snapshot(db)
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
    historical = sum(
        r["debit_cents"] - r["credit_cents"]
        for r in scoped
        if r["date"] <= event.date.isoformat()
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
            for r in rows
            if r.get("allocation_event_id") and r["date"] <= event.date.isoformat()
        }
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
        "later_receipts": later_receipts,
        "read_only": True,
        "limitations": [
            "End-of-day balances do not establish intraday availability.",
            "Coverage start does not prove complete statements or an accurate opening balance.",
            "Later positivity and matching amounts do not prove the purpose or source of restoration.",
            "Allocation candidates require review of replacement scopes and correction evidence; no alert is resolved by this assessment.",
        ],
    }
    result["assessment_digest"] = digest(result)
    return result
