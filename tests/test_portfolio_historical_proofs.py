"""Synthetic acceptance tests for receipt, correction-set and statement proofs."""

from datetime import date

import pytest
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from app.models.transactions import TransactionLine
from app.portfolio import models as m
from app.portfolio.service import Problem, balance, control
from app.portfolio.assessments import shortage_assessment
from app.portfolio.reconciliation_evidence import (
    reconciliation_state,
    validate_coverage,
)
from tests.test_balancedbooks_portfolio import (
    run,
    account,
    entity,
    bank_rows,
    journal,
    ln,
    pool_correction,
)
from tests import test_balancedbooks_portfolio as fixtures

portfolio = fixtures.portfolio
correction_pool = fixtures.correction_pool

CUTOFF = date(2026, 2, 28)


def financial_snapshot(db):
    return {
        "events": db.query(m.Event).count(),
        "lines": [
            (r.id, r.account_id, str(r.debit), str(r.credit))
            for r in db.query(TransactionLine).order_by(TransactionLine.id)
        ],
        "classifications": [
            (r.id, r.line_id, r.event_id)
            for r in db.query(m.CashClassification).order_by(m.CashClassification.id)
        ],
        "observations": [
            (o.id, o.event_id)
            for o in db.query(m.BankObservation).order_by(m.BankObservation.id)
        ],
    }


def resolution_data(
    db,
    alert,
    correction,
    reason="supported_restoration",
    cutoff=CUTOFF,
    correction_set=None,
):
    assessment = shortage_assessment(db, alert.id, cutoff, correction_set)
    return {
        "review_id": alert.id,
        "as_of": cutoff.isoformat(),
        "expected_assessment_digest": assessment["assessment_digest"],
        "reason": reason,
        "evidence": "Reviewed exact original statement and reciprocal evidence",
        "coverage_evidence": "Complete supported statement interval and opening",
        **(
            {"corrective_event_ids": correction_set}
            if correction_set
            else {"corrective_event_id": correction}
        ),
    }


def receipt_case(
    portfolio, *, matched=False, amount=100000, reconcile=True, extra_alert=False
):
    db, e, bank, expense, income = portfolio
    rows = [{"date": "2026-02-01", "amount_cents": -150000}]
    if extra_alert:
        rows += [{"date": "2026-02-02", "amount_cents": -1000}]
    rows += [{"date": "2026-02-03", "amount_cents": amount}]
    observations = bank_rows(db, bank, rows)
    run(
        db,
        "post_bank",
        observation_id=observations[0],
        offset_account_id=expense,
        rationale="Actual debit",
    )
    alert = db.query(m.ReviewItem).filter_by(code="actual_shortage").one()
    if extra_alert:
        run(
            db,
            "post_bank",
            observation_id=observations[1],
            offset_account_id=expense,
            rationale="Further actual debit",
        )
    receipt = run(
        db,
        "post_bank",
        observation_id=observations[-1],
        rationale="Actual original receipt",
    )
    if matched:
        lender = entity(db, "LENDER")
        source_bank = account(db, lender, "source-bank", "asset", "bank")
        journal(
            db,
            lender,
            "2026-01-01",
            [ln(source_bank, amount), ln(control(db, lender, "equity"), -amount)],
            kind="opening",
        )
        withdrawal = bank_rows(
            db,
            source_bank,
            [{"date": "2026-02-03", "amount_cents": -amount}],
            name="lender.csv",
        )[0]
        run(db, "post_bank", observation_id=withdrawal, rationale="Actual sender debit")
        run(
            db,
            "match_transfer",
            withdrawal_id=withdrawal,
            deposit_id=observations[-1],
            purpose="advance",
            item="documented advance",
            evidence="Reciprocal statement records",
        )
    if reconcile:
        run(
            db,
            "reconcile",
            account_id=bank,
            start="2026-01-01",
            end=CUTOFF.isoformat(),
            opening_cents=100000,
            ending_cents=balance(db, bank, CUTOFF),
        )
    return db, bank, alert, observations[-1], receipt["event_id"]


@pytest.mark.parametrize("matched", [False, True])
def test_restoration_uses_immutable_receipt_and_preserves_classification(
    portfolio, matched
):
    db, bank, alert, obs_id, receipt = receipt_case(portfolio, matched=matched)
    before = financial_snapshot(db)
    current = db.get(m.BankObservation, obs_id).event_id
    assert (receipt != current) is matched
    result = run(
        db, "resolve_historical_shortage", **resolution_data(db, alert, receipt)
    )
    assert result["financial_entries_added"] is False
    assert result["restoration_receipt"]["physical_event_id"] == receipt
    assert result["restoration_receipt"]["classification_event_id"] == current
    assert result["restoration_receipt"]["balance_before_receipt_cents"] == -50000
    assert result["restoration_receipt"]["balance_after_receipt_cents"] == 50000
    assert financial_snapshot(db) == before


def test_restoration_classification_is_not_new_funding(portfolio):
    db, _, alert, obs_id, _ = receipt_case(portfolio, matched=True)
    current = db.get(m.BankObservation, obs_id).event_id
    with pytest.raises(Problem) as error:
        run(db, "resolve_historical_shortage", **resolution_data(db, alert, current))
    assert error.value.code == "restoration_evidence"


@pytest.mark.parametrize("missing", [False, True])
def test_restoration_needs_full_coverage_and_actual_crossing(portfolio, missing):
    db, _, alert, _, receipt = receipt_case(
        portfolio, amount=20000 if not missing else 100000, reconcile=not missing
    )
    with pytest.raises(Problem) as error:
        run(db, "resolve_historical_shortage", **resolution_data(db, alert, receipt))
    assert error.value.code == (
        "missing_reconciliation" if missing else "unresolved_shortage"
    )


def test_restoration_models_shared_episode_without_duplicate_cash(portfolio):
    db, _, alert, _, receipt = receipt_case(portfolio, matched=True, extra_alert=True)
    other = (
        db.query(m.ReviewItem)
        .filter(m.ReviewItem.code == "actual_shortage", m.ReviewItem.id != alert.id)
        .one()
    )
    before = financial_snapshot(db)
    first = run(
        db, "resolve_historical_shortage", **resolution_data(db, alert, receipt)
    )
    second = run(
        db, "resolve_historical_shortage", **resolution_data(db, other, receipt)
    )
    assert (
        first["restoration_receipt"]["episode_id"]
        == second["restoration_receipt"]["episode_id"]
    )
    assert second["restoration_receipt"]["same_episode_review_ids"] == [alert.id]
    assert financial_snapshot(db) == before


def test_restoration_rejects_unrelated_same_amount_and_ambiguous_ancestry(portfolio):
    db, bank, alert, obs_id, receipt = receipt_case(portfolio)
    unrelated = journal(
        db,
        portfolio[1],
        "2026-02-03",
        [ln(control(db, portfolio[1], "equity"), 100000), ln(portfolio[4], -100000)],
    )
    with pytest.raises(Problem) as error:
        run(
            db,
            "resolve_historical_shortage",
            **resolution_data(db, alert, unrelated["event_id"]),
        )
    assert error.value.code == "restoration_evidence"
    journal(
        db,
        portfolio[1],
        "2026-02-03",
        [ln(control(db, portfolio[1], "equity"), 1), ln(portfolio[4], -1)],
        source={"observation_id": obs_id},
    )
    with pytest.raises(Problem) as error:
        run(db, "resolve_historical_shortage", **resolution_data(db, alert, receipt))
    assert error.value.code == "restoration_lineage"


def test_restoration_does_not_reopen_an_ended_episode(portfolio):
    db, e, bank, expense, income = portfolio
    observations = bank_rows(
        db,
        bank,
        [
            {"date": "2026-02-01", "amount_cents": -150000},
            {"date": "2026-02-02", "amount_cents": 100000},
            {"date": "2026-02-04", "amount_cents": -100000},
            {"date": "2026-02-05", "amount_cents": 100000},
        ],
    )
    for obs in observations:
        run(db, "post_bank", observation_id=obs, rationale="Actual statement")
    alert = (
        db.query(m.ReviewItem)
        .filter_by(code="actual_shortage")
        .order_by(m.ReviewItem.id)
        .first()
    )
    run(
        db,
        "reconcile",
        account_id=bank,
        start="2026-01-01",
        end=CUTOFF.isoformat(),
        opening_cents=100000,
        ending_cents=50000,
    )
    receipt = db.get(m.BankObservation, observations[-1]).event_id
    with pytest.raises(Problem) as error:
        run(db, "resolve_historical_shortage", **resolution_data(db, alert, receipt))
    assert error.value.code == "restoration_episode"


def correction_set_case(correction_pool, *, hidden_deficit=False):
    db, holder, bank, (unknown, second, client) = correction_pool
    observations = bank_rows(
        db,
        bank,
        [
            {"date": "2026-01-01", "amount_cents": 10000},
            {"date": "2026-01-01", "amount_cents": 20000},
            {"date": "2026-01-10", "amount_cents": -8000},
            {"date": "2026-01-20", "amount_cents": -8000},
        ],
    )
    run(
        db,
        "post_bank",
        observation_id=observations[0],
        fund_id=unknown,
        rationale="Actual statement",
    )
    run(
        db,
        "pool_activity",
        observation_id=observations[1],
        fund_id=client,
        evidence="Client cash",
    )
    for obs in observations[2:]:
        run(
            db,
            "post_bank",
            observation_id=obs,
            fund_id=unknown,
            rationale="Actual debit, beneficiary unresolved",
        )
    alert = db.query(m.ReviewItem).filter_by(code="actual_shortage").one()
    if hidden_deficit:
        extras = bank_rows(
            db,
            bank,
            [
                {"date": "2026-01-15", "amount_cents": -15000},
                {"date": "2026-01-18", "amount_cents": 12000},
            ],
            name="later-import.csv",
        )
        for obs in extras:
            run(
                db,
                "pool_activity",
                observation_id=obs,
                fund_id=client,
                evidence="Other confirmed client activity",
            )
    corrections = [
        run(db, "correct_posted_pool", **pool_correction(db, obs, client))["event_id"]
        for obs in observations[2:]
    ]
    run(
        db,
        "reconcile",
        account_id=bank,
        start="2026-01-01",
        end=CUTOFF.isoformat(),
        opening_cents=0,
        ending_cents=balance(db, bank, CUTOFF),
    )
    return db, bank, alert, corrections


def test_combined_allocation_causal_set_is_metadata_only(correction_pool):
    db, _, alert, corrections = correction_set_case(correction_pool)
    for correction in corrections:
        with pytest.raises(Problem) as error:
            run(
                db,
                "resolve_historical_shortage",
                **resolution_data(db, alert, correction, "source_allocation_corrected"),
            )
        assert error.value.code == "unrelated_correction"
    before = financial_snapshot(db)
    result = run(
        db,
        "resolve_historical_shortage",
        **resolution_data(
            db,
            alert,
            None,
            "source_allocation_set_corrected",
            correction_set=list(reversed(corrections)),
        ),
    )
    proof = result["allocation_causal_proof"]
    assert proof["corrective_event_ids"] == corrections
    assert proof["combined_scope_delta_cents"] == 16000
    assert proof["counterfactual_balance_cents"] == -6000
    assert proof["replacement_balances"]
    assert db.get(m.ReviewItem, alert.id).resolved_event_id is None
    assert financial_snapshot(db) == before


def test_correction_set_digest_binds_complete_set_and_rejects_duplicate(
    correction_pool,
):
    db, _, alert, corrections = correction_set_case(correction_pool)
    data = resolution_data(
        db, alert, None, "source_allocation_set_corrected", correction_set=corrections
    )
    with pytest.raises(Problem) as error:
        run(
            db,
            "resolve_historical_shortage",
            **{**data, "corrective_event_ids": corrections[:1]},
        )
    assert error.value.code == "stale_assessment"
    with pytest.raises(ValidationError):
        run(
            db,
            "resolve_historical_shortage",
            **{**data, "corrective_event_ids": corrections * 2},
        )
    with pytest.raises(Problem) as error:
        shortage_assessment(db, alert.id, CUTOFF, corrections * 2)
    assert error.value.code == "correction_set"


def test_combined_set_cannot_hide_intermediate_replacement_deficit(correction_pool):
    db, _, alert, corrections = correction_set_case(
        correction_pool, hidden_deficit=True
    )
    with pytest.raises(Problem) as error:
        run(
            db,
            "resolve_historical_shortage",
            **resolution_data(
                db,
                alert,
                None,
                "source_allocation_set_corrected",
                correction_set=corrections,
            ),
        )
    assert error.value.code == "replacement_deficit"


def test_allocation_shortage_cannot_escape_via_import_order(correction_pool):
    db, _, alert, _ = correction_set_case(correction_pool)
    with pytest.raises(Problem) as error:
        run(
            db,
            "resolve_historical_shortage",
            **resolution_data(db, alert, alert.event_id, "import_order_artifact"),
        )
    assert error.value.code == "allocation_evidence_required"


def revision_case(portfolio):
    db, e, bank, expense, income = portfolio
    withdrawal, receipt = bank_rows(
        db,
        bank,
        [
            {"date": "2026-02-01", "amount_cents": -150000},
            {"date": "2026-02-03", "amount_cents": 100000},
        ],
    )
    for obs in [withdrawal, receipt]:
        run(db, "post_bank", observation_id=obs, rationale="Initial export")
    alert = db.query(m.ReviewItem).filter_by(code="actual_shortage").one()
    original = run(
        db,
        "reconcile",
        account_id=bank,
        start="2026-01-01",
        end=CUTOFF.isoformat(),
        opening_cents=100000,
        ending_cents=50000,
    )["reconciliation_id"]
    extra = bank_rows(
        db,
        bank,
        [{"date": "2026-02-03", "amount_cents": -1000}],
        name="complete-export.csv",
    )[0]
    run(
        db,
        "post_bank",
        observation_id=extra,
        rationale="Genuine additional same-day debit",
    )
    document = run(
        db,
        "attach_document",
        code="COMPLETE",
        title="Complete statement",
        locator="file:///synthetic/statement.pdf",
        sha256="a" * 64,
    )["document_id"]
    state = reconciliation_state(db, original)
    data = {
        "supersedes_id": original,
        "expected_reconciliation_digest": state["reconciliation_digest"],
        "opening_cents": 100000,
        "ending_cents": 49000,
        "observation_ids": [o["id"] for o in state["current_observations"]],
        "source_documents": [{"document_id": document, "sha256": "a" * 64}],
        "reason": "Complete export includes additional genuine debit",
    }
    return db, bank, alert, db.get(m.BankObservation, receipt).event_id, original, data


def test_reviewed_revision_restores_coverage_and_invalidates_old_assessment(portfolio):
    db, bank, alert, receipt, original, data = revision_case(portfolio)
    before = financial_snapshot(db)
    old = resolution_data(db, alert, receipt)
    with pytest.raises(Problem) as error:
        run(db, "resolve_historical_shortage", **old)
    assert error.value.code == "bank_reconciliation_difference"
    result = run(db, "revise_reconciliation", **data)
    assert db.get(m.Reconciliation, original).ending_cents == 50000
    assert db.get(m.Reconciliation, result["reconciliation_id"]).ending_cents == 49000
    assert db.query(m.Reconciliation).count() == 2
    with pytest.raises(Problem) as error:
        run(db, "resolve_historical_shortage", **old)
    assert error.value.code == "stale_assessment"
    resolved = run(
        db, "resolve_historical_shortage", **resolution_data(db, alert, receipt)
    )
    assert resolved["reconciliation_ids"] == [result["reconciliation_id"]]
    assert financial_snapshot(db) == before
    revision = db.get(m.ReconciliationRevision, result["revision_id"])
    assert revision.reason == data["reason"]
    assert revision.source_documents == data["source_documents"]


@pytest.mark.parametrize(
    "fault,expected",
    [
        ("ending", "reconciliation_difference"),
        ("opening", "unsupported_opening"),
        ("missing", "reconciliation_inventory"),
        ("extra", "reconciliation_inventory"),
        ("duplicate", "reconciliation_inventory"),
        ("hash", "reconciliation_source"),
        ("stale", "stale_reconciliation_revision"),
    ],
)
def test_reconciliation_revision_rejects_incomplete_or_wrong_proof(
    portfolio, fault, expected
):
    db, _, _, _, _, data = revision_case(portfolio)
    if fault == "ending":
        data["ending_cents"] += 1
    if fault == "opening":
        data["opening_cents"] -= 1
    if fault == "missing":
        data["observation_ids"].pop()
    if fault == "extra":
        data["observation_ids"].append(99999)
    if fault == "duplicate":
        data["observation_ids"] *= 2
    if fault == "hash":
        data["source_documents"][0]["sha256"] = "b" * 64
    if fault == "stale":
        data["expected_reconciliation_digest"] = "stale"
    before = financial_snapshot(db)
    with pytest.raises(Problem) as error:
        run(db, "revise_reconciliation", **data)
    assert error.value.code == expected
    assert db.query(m.ReconciliationRevision).count() == 0
    assert financial_snapshot(db) == before


def test_reconciliation_revisions_are_immutable_and_cannot_fork(portfolio):
    db, _, _, _, original, data = revision_case(portfolio)
    result = run(db, "revise_reconciliation", **data)
    with pytest.raises(Problem) as error:
        run(db, "revise_reconciliation", **data)
    assert error.value.code == "stale_reconciliation_revision"
    refreshed = {
        **data,
        "expected_reconciliation_digest": reconciliation_state(db, original)[
            "reconciliation_digest"
        ],
    }
    with pytest.raises(Problem) as error:
        run(db, "revise_reconciliation", **refreshed)
    assert error.value.code == "superseded_reconciliation"
    from app.portfolio.guards import install

    install(db.connection())
    for query in [
        "UPDATE re_reconciliation_revisions SET reason='rewritten' WHERE id=:id",
        "DELETE FROM re_reconciliation_revisions WHERE id=:id",
        "UPDATE re_reconciliations SET ending_cents=0 WHERE id=:id",
    ]:
        with pytest.raises(IntegrityError), db.begin_nested():
            db.execute(
                text(query),
                {"id": result["revision_id"] if "revisions" in query else original},
            )


@pytest.mark.parametrize("matched", [False, True])
def test_whole_account_fast_path_has_exact_assessment_and_digest_parity(
    portfolio, monkeypatch, matched
):
    import app.portfolio.assessments as assessments

    db, _, alert, _, _ = receipt_case(portfolio, matched=matched)
    fast = shortage_assessment(db, alert.id, CUTOFF)
    native = assessments.legacy_allocation_candidates

    def full(db, account_id, fund_id, rows, event):
        return native(db, account_id, fund_id, rows, event, fast_path=False)

    monkeypatch.setattr(assessments, "legacy_allocation_candidates", full)
    assert shortage_assessment(db, alert.id, CUTOFF) == fast


@pytest.mark.parametrize(
    "field,value",
    [("amount_cents", 100001), ("date", date(2026, 2, 4)), ("account_id", 99999)],
)
def test_restoration_rejects_changed_source_facts(portfolio, field, value):
    from app.portfolio.shortage_lineage import restoration_receipt

    db, _, alert, obs_id, receipt = receipt_case(portfolio)
    assessment = shortage_assessment(db, alert.id, CUTOFF)
    setattr(db.get(m.BankObservation, obs_id), field, value)
    with pytest.raises(Problem):
        restoration_receipt(db, assessment, db.get(m.Event, receipt))


def test_restoration_rejects_wrong_fund(correction_pool):
    db, _, bank, (unknown, _, client) = correction_pool
    opening, debit, incoming = bank_rows(
        db,
        bank,
        [
            {"date": "2026-01-01", "amount_cents": 10000},
            {"date": "2026-02-01", "amount_cents": -5000},
            {"date": "2026-02-03", "amount_cents": 10000},
        ],
    )
    run(
        db,
        "pool_activity",
        observation_id=opening,
        fund_id=client,
        evidence="Client cash",
    )
    run(
        db,
        "post_bank",
        observation_id=debit,
        fund_id=unknown,
        rationale="Unresolved beneficiary",
    )
    receipt = run(
        db,
        "pool_activity",
        observation_id=incoming,
        fund_id=client,
        evidence="Other beneficiary receipt",
    )
    alert = db.query(m.ReviewItem).filter_by(code="actual_shortage").one()
    run(
        db,
        "reconcile",
        account_id=bank,
        start="2026-01-01",
        end=CUTOFF.isoformat(),
        opening_cents=0,
        ending_cents=15000,
    )
    with pytest.raises(Problem) as error:
        run(
            db,
            "resolve_historical_shortage",
            **resolution_data(db, alert, receipt["event_id"]),
        )
    assert error.value.code == "restoration_evidence"


def test_overwritten_allocation_is_not_counted_twice(correction_pool):
    db, _, alert, corrections = correction_set_case(correction_pool)
    old_data = resolution_data(
        db, alert, None, "source_allocation_set_corrected", correction_set=corrections
    )
    prior = db.query(m.LineAllocation).filter_by(event_id=corrections[0]).one()
    original = db.get(m.Event, corrections[0])
    replacement = journal(
        db,
        correction_pool[1],
        original.date.isoformat(),
        [
            ln(control(db, correction_pool[1], "equity"), 1),
            ln(control(db, correction_pool[1], "suspense_credit"), -1),
        ],
        source=original.source,
        corrects_id=original.corrects_id,
    )
    db.add(
        m.LineAllocation(
            line_id=prior.line_id,
            event_id=replacement["event_id"],
            date=prior.date,
            components=prior.components,
            evidence="Superseding reviewed allocation",
            supersedes_id=prior.id,
        )
    )
    db.flush()
    with pytest.raises(Problem) as error:
        run(db, "resolve_historical_shortage", **old_data)
    assert error.value.code == "stale_assessment"
    for selected in [corrections, corrections + [replacement["event_id"]]]:
        with pytest.raises(Problem) as error:
            run(
                db,
                "resolve_historical_shortage",
                **resolution_data(
                    db,
                    alert,
                    None,
                    "source_allocation_set_corrected",
                    correction_set=selected,
                ),
            )
        assert error.value.code == "inactive_correction"


def test_unrelated_event_cannot_join_sufficient_correction_set(correction_pool):
    db, _, alert, corrections = correction_set_case(correction_pool)
    unrelated = db.query(m.Event).filter_by(kind="custody").first().id
    with pytest.raises(Problem) as error:
        run(
            db,
            "resolve_historical_shortage",
            **resolution_data(
                db,
                alert,
                None,
                "source_allocation_set_corrected",
                correction_set=corrections + [unrelated],
            ),
        )
    assert error.value.code == "unrelated_correction"


def test_revision_requires_prior_continuous_coverage(portfolio):
    db, _, bank, _, _ = portfolio
    original = run(
        db,
        "reconcile",
        account_id=bank,
        start="2026-02-01",
        end=CUTOFF.isoformat(),
        opening_cents=100000,
        ending_cents=100000,
    )["reconciliation_id"]
    document = run(
        db,
        "attach_document",
        code="FEB",
        title="February statement",
        locator="file:///synthetic/feb.pdf",
        sha256="a" * 64,
    )["document_id"]
    state = reconciliation_state(db, original)
    with pytest.raises(Problem) as error:
        run(
            db,
            "revise_reconciliation",
            supersedes_id=original,
            expected_reconciliation_digest=state["reconciliation_digest"],
            opening_cents=100000,
            ending_cents=100000,
            observation_ids=[],
            source_documents=[{"document_id": document, "sha256": "a" * 64}],
            reason="Reviewed February statement",
        )
    assert error.value.code == "missing_reconciliation"


def test_coverage_rejects_ambiguous_active_versions(portfolio):
    db, bank, _, _, original, data = revision_case(portfolio)
    run(db, "revise_reconciliation", **data)
    obsolete = db.get(m.Reconciliation, original)
    # Simulate a second legacy snapshot of the same interval, whose retirement
    # also requires an explicit revision instead of silently choosing latest.
    db.add(
        m.Reconciliation(
            account_id=bank,
            start=obsolete.start,
            end=obsolete.end,
            opening_cents=obsolete.opening_cents,
            ending_cents=obsolete.ending_cents,
            result=obsolete.result,
            actor="legacy",
        )
    )
    db.flush()
    with pytest.raises(Problem) as error:
        validate_coverage(db, db.get(m.AccountScope, bank), CUTOFF)
    assert error.value.code == "ambiguous_reconciliation"


def test_statement_after_cutoff_does_not_certify_midmonth(portfolio):
    db, _, alert, _, receipt = receipt_case(portfolio)
    data = resolution_data(db, alert, receipt, cutoff=date(2026, 2, 15))
    with pytest.raises(Problem) as error:
        run(db, "resolve_historical_shortage", **data)
    assert error.value.code == "missing_reconciliation"


def test_revision_migration_preserves_history_and_is_repeatable(portfolio, monkeypatch):
    import importlib

    db, _, _, _, _, data = revision_case(portfolio)
    run(db, "revise_reconciliation", **data)
    migration = importlib.import_module(
        "migrations.versions.bb0003_reconciliation_revisions"
    )
    monkeypatch.setattr(migration.op, "get_bind", lambda: db.connection())
    migration.upgrade()
    migration.upgrade()
    with pytest.raises(RuntimeError, match="cannot be discarded"):
        migration.downgrade()
    assert db.query(m.ReconciliationRevision).count() == 1


def test_whole_account_fast_path_skips_only_empty_legacy_discovery(
    correction_pool, monkeypatch
):
    import app.portfolio.assessments as assessments
    import app.portfolio.cash_lineage as lineage

    db, holder, bank, (unknown, _, client) = correction_pool
    small, out, funding = bank_rows(
        db,
        bank,
        [
            {"date": "2026-01-01", "amount_cents": 10000},
            {"date": "2026-02-01", "amount_cents": -30000},
            {"date": "2026-01-01", "amount_cents": 50000},
        ],
    )
    run(
        db, "post_bank", observation_id=small, fund_id=unknown, rationale="First export"
    )
    original = run(
        db, "post_bank", observation_id=out, fund_id=unknown, rationale="First export"
    )
    fund_alert = (
        db.query(m.ReviewItem)
        .filter_by(code="actual_shortage", event_id=original["event_id"])
        .filter(m.ReviewItem.detail.contains(" fund "))
        .one()
    )
    whole = m.ReviewItem(
        code="actual_shortage",
        entity_id=holder,
        event_id=original["event_id"],
        detail=fund_alert.detail.replace(f" fund {unknown}", ""),
        status="open",
    )
    db.add(whole)
    db.flush()
    run(
        db,
        "pool_activity",
        observation_id=funding,
        fund_id=client,
        evidence="Complete earlier cash",
    )
    journal(
        db,
        holder,
        "2026-02-01",
        [
            ln(bank, 30000, fund_id=unknown),
            ln(bank, -30000, fund_id=client),
            ln(control(db, holder, "custody_liability"), 30000, fund_id=client),
            ln(control(db, holder, "suspense_debit"), -30000),
        ],
        corrects_id=original["event_id"],
        source={"observation_id": out},
    )
    calls = []
    normalize = lineage.normalized_cash

    def counted(*args, **kwargs):
        calls.append(1)
        return normalize(*args, **kwargs)

    monkeypatch.setattr(lineage, "normalized_cash", counted)
    fast = shortage_assessment(db, whole.id, CUTOFF)
    assert calls == []
    fund = shortage_assessment(db, fund_alert.id, CUTOFF)
    assert fund["legacy_allocation_corrections"] and calls
    calls.clear()
    native = assessments.legacy_allocation_candidates
    monkeypatch.setattr(
        assessments,
        "legacy_allocation_candidates",
        lambda db, account_id, fund_id, rows, event: native(
            db, account_id, fund_id, rows, event, fast_path=False
        ),
    )
    assert shortage_assessment(db, whole.id, CUTOFF) == fast
    assert calls


def test_removed_receipt_fund_is_checked_even_when_absent_from_replacement(
    correction_pool,
):
    db, _, bank, (before_fund, target, other) = correction_pool
    observations = bank_rows(
        db,
        bank,
        [
            {"date": "2026-01-01", "amount_cents": 5000},
            {"date": "2026-01-01", "amount_cents": 10000},
            {"date": "2026-01-01", "amount_cents": 8000},
            {"date": "2026-01-10", "amount_cents": -10000},
            {"date": "2026-01-20", "amount_cents": -6000},
        ],
    )
    for obs, fund in zip(
        observations, [before_fund, other, before_fund, before_fund, target]
    ):
        if fund == other:
            run(
                db,
                "pool_activity",
                observation_id=obs,
                fund_id=fund,
                evidence="Other client cash",
            )
        else:
            run(
                db,
                "post_bank",
                observation_id=obs,
                fund_id=fund,
                rationale="Original source allocation",
            )
    alert = db.query(m.ReviewItem).filter_by(code="actual_shortage").one()
    correction = run(
        db, "correct_posted_pool", **pool_correction(db, observations[2], target)
    )
    run(
        db,
        "reconcile",
        account_id=bank,
        start="2026-01-01",
        end=CUTOFF.isoformat(),
        opening_cents=0,
        ending_cents=7000,
    )
    assert balance(db, bank, CUTOFF, target) == 2000
    assert balance(db, bank, CUTOFF, before_fund) == -5000
    with pytest.raises(Problem) as error:
        run(
            db,
            "resolve_historical_shortage",
            **resolution_data(
                db, alert, correction["event_id"], "source_allocation_corrected"
            ),
        )
    assert error.value.code == "replacement_deficit"


def test_legacy_unassigned_replacement_cannot_hide_behind_whole_account(
    correction_pool,
):
    db, holder, _, (unknown, client, _) = correction_pool
    bank = account(
        db, holder, "legacy-unpooled", "asset", "bank", coverage_start="2026-01-01"
    )
    funding, debit = bank_rows(
        db,
        bank,
        [
            {"date": "2026-01-01", "amount_cents": 50000},
            {"date": "2026-02-01", "amount_cents": -30000},
        ],
    )
    run(
        db,
        "post_bank",
        observation_id=funding,
        fund_id=client,
        rationale="Other unresolved cash",
    )
    original = run(
        db,
        "post_bank",
        observation_id=debit,
        fund_id=unknown,
        rationale="Unresolved payment",
    )
    alert = db.query(m.ReviewItem).filter_by(code="actual_shortage").one()
    correction = journal(
        db,
        holder,
        "2026-02-01",
        [
            ln(bank, 30000, fund_id=unknown),
            ln(bank, -30000),
        ],
        corrects_id=original["event_id"],
        source={"observation_id": debit},
    )
    run(
        db,
        "reconcile",
        account_id=bank,
        start="2026-01-01",
        end=CUTOFF.isoformat(),
        opening_cents=0,
        ending_cents=20000,
    )
    assessment = shortage_assessment(db, alert.id, CUTOFF)
    assert assessment["legacy_allocation_corrections"][0]["replacement_fund_ids"] == [
        None
    ]
    with pytest.raises(Problem) as error:
        run(
            db,
            "resolve_historical_shortage",
            **resolution_data(
                db, alert, correction["event_id"], "source_allocation_corrected"
            ),
        )
    assert error.value.code == "replacement_deficit"
