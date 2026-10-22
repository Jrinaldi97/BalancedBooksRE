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
    # Older releases used gross zero-net bank journals for attribution.
    # Recognize them only after the same strict ancestry validation as posting.
    legacy = []
    from app.portfolio.observation_state import observation_state
    from app.portfolio.cash_lineage import normalized_cash
    from app.portfolio.service import Problem

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
                            and r["fund_id"] is not None
                        }
                    ),
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
    result["assessment_digest"] = digest(result)
    return result


def resolve_historical(db, data, actor):
    """Resolve one dated alert; never infer restoration from later positivity."""
    import json
    from datetime import timedelta
    from app.portfolio.service import balance

    assessment = shortage_assessment(db, data.review_id, data.as_of)
    if assessment.get("assessment_digest") != data.expected_assessment_digest:
        fail("stale_assessment", "Refresh the dated shortage assessment")
    review = get(db, m.ReviewItem, data.review_id)
    if review.status != "open":
        fail("review_state", "This alert is already resolved")
    bank = scope(db, assessment["account_id"])
    alert = get(db, m.Event, review.event_id)
    correction = get(db, m.Event, data.corrective_event_id)
    if not bank.coverage_start or bank.coverage_start > alert.date:
        fail("incomplete_coverage", "A supported opening cutoff is required")
    if correction.date > data.as_of:
        fail("correction_date", "Correction is after the selected historical cutoff")
    # Require continuous reconciled coverage from the supported opening, not
    # merely the latest reconciliation (which may belong to a later year).
    reconciliations = (
        db.query(m.Reconciliation)
        .filter(
            m.Reconciliation.account_id == bank.account_id,
            m.Reconciliation.end <= data.as_of,
        )
        .order_by(m.Reconciliation.start, m.Reconciliation.end)
        .all()
    )
    covered = bank.coverage_start - timedelta(days=1)
    used = []
    for recon in reconciliations:
        if recon.start > covered + timedelta(days=1) or recon.end <= covered:
            continue
        if balance(db, bank.account_id, recon.end) != recon.ending_cents:
            fail(
                "bank_reconciliation_difference",
                f"Reconciliation {recon.id} no longer agrees with bank books",
            )
        if not recon.result or recon.result.get("difference_cents") != 0:
            fail(
                "bank_reconciliation_difference",
                f"Reconciliation {recon.id} is not balanced",
            )
        # A reconciliation predating imports cannot certify those new rows.
        active = (
            db.query(m.BankObservation)
            .filter(
                m.BankObservation.account_id == bank.account_id,
                m.BankObservation.date >= recon.start,
                m.BankObservation.date <= recon.end,
                m.BankObservation.status != "duplicate",
            )
            .all()
        )
        if (
            any(not o.event_id for o in active)
            or {o.id for o in active} != set(recon.result.get("observation_ids", []))
            or recon.ending_cents - recon.opening_cents
            != sum(o.amount_cents for o in active)
        ):
            fail(
                "stale_reconciliation",
                f"Reconciliation {recon.id} does not cover the current observations",
            )
        covered = recon.end
        used.append(recon.id)
    if covered < data.as_of:
        fail(
            "missing_reconciliation",
            "Continuous reconciled statements through the selected cutoff are required",
        )
    rows = ledger(db, data.as_of, account_id=bank.account_id)
    fund_id = assessment["fund_id"]
    scoped = [r for r in rows if fund_id is None or r["fund_id"] == fund_id]
    historical = assessment["effective_historical_balance_cents"]
    if data.reason == "source_allocation_corrected":
        legacy = next(
            (
                c
                for c in assessment["legacy_allocation_corrections"]
                if c["event_id"] == correction.id
            ),
            None,
        )
        if historical < 0 or (
            correction.id not in assessment["allocation_event_ids"] and not legacy
        ):
            fail(
                "unrelated_correction",
                "Require an allocation that disproves the original dated shortage",
            )
        allocations = db.query(m.LineAllocation).filter_by(event_id=correction.id).all()
        replacement_funds = {
            c.get("fund_id") for a in allocations for c in a.components
        }
        if allocations:
            from app.portfolio.allocations import effective_map

            effective = effective_map(db, alert.date, [a.line_id for a in allocations])
            impact = 0
            for allocation in allocations:
                physical = get(db, TransactionLine, allocation.line_id)
                active = effective.get(allocation.line_id)
                if (
                    physical.account_id != bank.account_id
                    or not active
                    or active.id != allocation.id
                ):
                    continue
                prior = (
                    db.get(m.LineAllocation, allocation.supersedes_id)
                    if allocation.supersedes_id
                    else None
                )
                original_scope = get(db, m.LineScope, allocation.line_id)
                before = (
                    prior.components
                    if prior
                    else [
                        {
                            "amount_cents": cents(physical.debit)
                            - cents(physical.credit),
                            "fund_id": original_scope.fund_id,
                        }
                    ]
                )
                impact += sum(
                    c["amount_cents"]
                    for c in allocation.components
                    if fund_id is None or c.get("fund_id") == fund_id
                )
                impact -= sum(
                    c["amount_cents"]
                    for c in before
                    if fund_id is None or c.get("fund_id") == fund_id
                )
            if impact <= 0 or historical - impact >= 0:
                fail(
                    "unrelated_correction",
                    "This allocation does not disprove the selected shortage",
                )
        if legacy:
            replacement_funds.update(legacy["replacement_fund_ids"])
            if historical - legacy["scope_delta_cents"] >= 0:
                fail(
                    "unrelated_correction",
                    "This correction did not disprove the selected alert; inspect import order separately",
                )
        if (not allocations and not legacy) or any(
            balance(db, bank.account_id, alert.date, f) < 0 for f in replacement_funds
        ):
            fail(
                "replacement_deficit", "Replacement beneficiary scopes remain deficient"
            )
        affected_dates = {
            date.fromisoformat(r["date"])
            for r in rows
            if correction.date.isoformat() <= r["date"] <= alert.date.isoformat()
        }
        affected_dates.update({correction.date, alert.date})
        if any(
            balance(db, bank.account_id, day, f) < 0
            for f in replacement_funds
            for day in affected_dates
        ):
            fail(
                "replacement_deficit",
                "A replacement scope has a historical shortfall during the corrected interval",
            )
    elif data.reason == "import_order_artifact":
        if historical < 0 or correction.id != alert.id:
            fail(
                "historical_deficit",
                "An import-order assessment must refer to the original alert and nonnegative historical holdings",
            )
    else:
        incoming = sum(
            r["debit_cents"] - r["credit_cents"]
            for r in scoped
            if r["event_id"] == correction.id
        )
        source_obs = (correction.source or {}).get("observation_id")
        obs = db.get(m.BankObservation, source_obs) if source_obs else None
        if (
            historical >= 0
            or correction.date <= alert.date
            or incoming <= 0
            or not obs
            or obs.event_id != correction.id
            or obs.account_id != bank.account_id
            or obs.amount_cents != incoming
        ):
            fail(
                "restoration_evidence",
                "Identify a later original receipt in the exact shortage scope; attribution-only corrections are not new funding",
            )
        before = balance(db, bank.account_id, correction.date, fund_id) - incoming
        if before >= 0 or before + incoming < 0:
            fail(
                "unresolved_shortage",
                "The identified receipt does not itself restore this shortfall",
            )
    review.status = "resolved"
    review.resolved_event_id = correction.id
    review.resolution = json.dumps(
        {
            "reason": data.reason,
            "evidence": data.evidence,
            "coverage_evidence": data.coverage_evidence,
            "assessment": assessment,
            "reconciliation_ids": used,
            "intraday_availability": "not_established",
        },
        sort_keys=True,
    )
    return {
        "review_id": review.id,
        "resolved_review_ids": [review.id],
        "assessment": assessment,
        "reconciliation_ids": used,
        "bank_cash_delta_cents": 0,
        "later_alerts_unchanged": True,
        "financial_entries_added": False,
    }
