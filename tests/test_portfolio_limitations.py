"""Regression cases from the October limitations handoff, synthetic books only."""

import uuid
from datetime import date
import pytest
from app.portfolio import models as m
from app.portfolio.service import execute, Problem, control, balance
from tests.test_balancedbooks_portfolio import run, account, journal, ln
from tests.test_portfolio_card_workflows import purchase, payment
from tests import test_portfolio_card_workflows as card_fixtures
from tests import test_portfolio_historical_proofs as historical_fixtures
from tests import test_portfolio_correction_validation as correction_fixtures
from tests import test_balancedbooks_portfolio as pool_fixtures
from tests.test_portfolio_correction_validation import historical_correction
from tests.test_balancedbooks_portfolio import bank_rows, pool_correction
from tests.test_portfolio_historical_proofs import (
    receipt_case,
    resolution_data,
    financial_snapshot,
)

cards = card_fixtures.cards
portfolio = historical_fixtures.portfolio
historical = correction_fixtures.historical
correction_pool = pool_fixtures.correction_pool


def test_audited_reopening_preserves_financial_history_and_replays(portfolio):
    db, bank, alert, obs, receipt = receipt_case(portfolio)
    run(db, "resolve_historical_shortage", **resolution_data(db, alert, receipt))
    before = financial_snapshot(db)
    prior = alert.resolution
    command = dict(
        operation="reopen_review",
        key=uuid.uuid4().hex,
        data=dict(
            review_id=alert.id,
            expected_resolution=prior,
            expected_resolved_event_id=alert.resolved_event_id,
            reason="Independent audit failed",
            evidence="Synthetic audit findings",
            corrective_event_id=receipt,
        ),
    )
    result = execute(db, command)
    assert result["previous_resolution"]["resolution"] == prior
    assert result["review_lifecycle"]["reopened_ids"] == [alert.id]
    assert alert.status == "open"
    assert financial_snapshot(db) == before
    assert execute(db, command)["replayed"]
    with pytest.raises(Problem, match="Only a resolved"):
        execute(db, {**command, "key": uuid.uuid4().hex})


def test_reopening_rejects_stale_resolution(portfolio):
    db, bank, alert, obs, receipt = receipt_case(portfolio)
    run(db, "resolve_historical_shortage", **resolution_data(db, alert, receipt))
    with pytest.raises(Problem) as error:
        run(
            db,
            "reopen_review",
            review_id=alert.id,
            expected_resolution="outdated",
            expected_resolved_event_id=receipt,
            reason="audit",
            evidence="audit",
        )
    assert error.value.code == "stale_state"
    assert alert.status == "resolved"


def test_card_project_keeps_pooled_property_unallocated(cards):
    db = cards["db"]
    project = account(db, cards["borrower"], "renovation", "asset", "project")
    obs, original, result = purchase(
        cards,
        posted=True,
        components=[
            dict(
                key="project",
                amount_cents=10000,
                beneficiary_kind="entity",
                entity_id=cards["borrower"],
                account_id=project,
                evidence="Project budget supports purpose but no property split",
            )
        ],
    )
    assert balance(db, project, date.max) == 10000
    assert balance(db, control(db, cards["borrower"], "due_to"), date.max) == -10000
    assert db.get(m.BankObservation, obs).event_id == original
    assert (
        db.query(m.ReviewItem)
        .filter_by(code="unallocated_property", status="open")
        .count()
    )


@pytest.mark.parametrize("posted", [False, True])
def test_cross_entity_card_payment_return_retry(cards, posted):
    db = cards["db"]
    payer = cards["borrower"]
    bank = account(db, payer, "payer-bank", "asset", "bank")
    journal(
        db,
        payer,
        "2026-01-01",
        [ln(bank, 20000), ln(control(db, payer, "equity"), -20000)],
    )
    c = {**cards, "bank": bank}
    paid, observations, _ = payment(c, posted=posted, payer_entity_id=payer)
    assert balance(db, control(db, payer, "due_from"), date.max) == 10000
    assert balance(db, control(db, cards["holder"], "due_to"), date.max) == -10000
    assert (
        balance(db, control(db, cards["holder"], "bridge"), date(2026, 2, 6)) == 10000
    )
    assert balance(db, control(db, cards["holder"], "bridge"), date.max) == 0
    returned, _, _ = payment(
        c,
        operation="return_card_repayment",
        code="RETURN",
        bank_date="2026-02-10",
        issuer_date="2026-02-11",
        item="PAYMENT",
        original_payment_id=paid["card_activity_id"],
        payer_entity_id=payer,
    )
    assert balance(db, control(db, payer, "due_from"), date.max) == 0
    assert balance(db, control(db, cards["holder"], "due_to"), date.max) == 0
    payment(
        c,
        code="RETRY",
        bank_date="2026-02-12",
        issuer_date="2026-02-13",
        retry_of_return_id=returned["card_activity_id"],
        payer_entity_id=payer,
    )
    assert balance(db, bank, date.max) == 10000


def test_zero_net_receipt_reclassification_is_not_new_funding(portfolio):
    db, bank, alert, obs, receipt = receipt_case(portfolio)
    journal(
        db,
        portfolio[1],
        "2026-02-03",
        [
            ln(bank, 100000, cash_flow="financing"),
            ln(bank, -100000, cash_flow="operating"),
        ],
        source={"observation_id": obs},
        corrects_id=receipt,
    )
    before = financial_snapshot(db)
    run(db, "resolve_historical_shortage", **resolution_data(db, alert, receipt))
    assert financial_snapshot(db) == before


def test_export_pages_are_complete_and_reject_changed_book(portfolio):
    from app.portfolio.verification_export import verification_export
    from tests.test_balancedbooks_portfolio import bank_rows

    db, e, bank, _, _ = portfolio
    ids = bank_rows(
        db, bank, [dict(date="2026-03-01", amount_cents=n) for n in range(1, 6)]
    )
    first = verification_export(db, limit=2)
    second = verification_export(db, first["next_after_id"], 2, first["book_digest"])
    third = verification_export(db, second["next_after_id"], 2, first["book_digest"])
    assert [
        r["observation_id"] for page in (first, second, third) for r in page["rows"]
    ] == ids
    assert third["complete"] and third["next_after_id"] is None
    run(db, "post_bank", observation_id=ids[0], rationale="New activity")
    with pytest.raises(Problem) as error:
        verification_export(db, first["next_after_id"], 2, first["book_digest"])
    assert error.value.code == "stale_export"


def test_evidenced_beneficiary_association_preserves_original_metadata(historical):
    db = historical[0]
    data = historical_correction(
        historical, source={"observation_id": historical[2].id}, kind="legacy"
    )
    original_source = dict(db.get(m.Event, data["corrective_event_id"]).source)
    with pytest.raises(Problem) as error:
        run(db, "validate_existing_correction", **data)
    assert error.value.code == "missing_beneficiary"
    document = m.Document(code="PROOF", title="Beneficiary evidence")
    db.add(document)
    db.flush()
    db.add(
        m.DocumentLocation(
            document_id=document.id, locator="synthetic://proof", sha256="a" * 64
        )
    )
    db.add(
        m.DocumentLink(document_id=document.id, event_id=data["corrective_event_id"])
    )
    db.flush()
    data["balances"][0]["beneficiary_evidence"] = dict(
        document_id=document.id,
        sha256="a" * 64,
        explanation="Identifies exact obligation and party",
    )
    before = financial_snapshot(db)
    result = run(db, "validate_existing_correction", **data)
    assert result["new_financial_events"] == 0
    assert financial_snapshot(db) == before
    assert db.get(m.Event, data["corrective_event_id"]).source == original_source


def test_independent_merchant_credit_has_no_purchase_parent(cards):
    from tests.test_portfolio_card_workflows import issuer_rows, reference

    db = cards["db"]
    obs = issuer_rows(
        cards, [dict(date="2026-03-01", amount_cents=500, external_id="credit")]
    )["observation_ids"][0]
    run(db, "post_bank", observation_id=obs, rationale="Actual credit")
    result = run(
        db,
        "classify_card_merchant_credit",
        code="CREDIT",
        card_id=cards["card"],
        observation=reference(db, obs),
        components=[
            dict(
                key="cost",
                amount_cents=500,
                beneficiary_kind="company",
                account_id=cards["expense"],
                evidence="Merchant memo",
            )
        ],
        evidence="Merchant credit memo",
    )
    assert balance(db, cards["expense"], date.max) == -500
    assert result["cost_cents"] == -500
    assert db.get(m.CardActivity, result["card_activity_id"]).parent_id is None


def test_failed_issuer_payment_pair_never_becomes_successful_repayment(cards):
    from tests.test_portfolio_card_workflows import issuer_rows, reference

    db = cards["db"]
    ids = issuer_rows(
        cards,
        [
            dict(date="2026-03-01", amount_cents=500, external_id="attempt"),
            dict(date="2026-03-03", amount_cents=-500, external_id="returned"),
        ],
    )["observation_ids"]
    for obs in ids:
        run(db, "post_bank", observation_id=obs, rationale="Actual issuer movement")
    result = run(
        db,
        "classify_card_failed_pair",
        code="FAILED",
        card_id=cards["card"],
        payment=reference(db, ids[0]),
        returned=reference(db, ids[1]),
        evidence="Issuer return reference",
    )
    assert result["successful_repayment"] is False
    assert balance(db, cards["card_account"], date.max) == 0
    assert balance(db, control(db, cards["holder"], "bridge"), date.max) == 0
    assert db.query(m.CardActivity).filter_by(kind="repayment").count() == 0


@pytest.mark.parametrize("reverse", [False, True])
def test_joint_same_date_allocation_is_order_independent(correction_pool, reverse):
    db, holder, bank, (unknown, second, client) = correction_pool
    ids = bank_rows(
        db,
        bank,
        [
            dict(date="2026-02-01", amount_cents=33000),
            dict(date="2026-02-01", amount_cents=-20000),
        ],
    )
    for obs in ids:
        run(
            db,
            "post_bank",
            observation_id=obs,
            fund_id=unknown,
            rationale="Unidentified",
        )
    original = [(o.id, o.event_id, o.amount_cents) for o in db.query(m.BankObservation)]
    parts = [pool_correction(db, obs, client) for obs in ids]
    result = run(
        db,
        "correct_posted_pool_joint",
        corrections=parts[::-1] if reverse else parts,
        evidence="Both sides supported by the same remittance evidence",
    )
    assert result["bank_cash_delta_cents"] == 0
    assert balance(db, bank, date.max, client) == 13000
    assert balance(db, bank, date.max, unknown) == 0
    assert [
        (o.id, o.event_id, o.amount_cents) for o in db.query(m.BankObservation)
    ] == original


def test_joint_deficit_rolls_back_every_leg(correction_pool):
    db, holder, bank, (unknown, second, client) = correction_pool
    ids = bank_rows(
        db,
        bank,
        [
            dict(date="2026-02-01", amount_cents=10000),
            dict(date="2026-02-01", amount_cents=-20000),
        ],
    )
    for obs in ids:
        run(
            db,
            "post_bank",
            observation_id=obs,
            fund_id=unknown,
            rationale="Unidentified",
        )
    parts = [pool_correction(db, obs, client) for obs in ids]
    before = financial_snapshot(db)
    with pytest.raises(Problem) as error:
        run(
            db, "correct_posted_pool_joint", corrections=parts, evidence="Pair evidence"
        )
    assert error.value.code == "allocation_source_deficit"
    assert financial_snapshot(db) == before
    assert "joint_pool_scopes" not in db.info


def test_adopt_classified_bank_payment_does_not_repeat_cash(cards):
    from tests.test_portfolio_card_workflows import issuer_rows, reference

    db = cards["db"]
    obs = bank_rows(db, cards["bank"], [dict(date="2026-03-01", amount_cents=-500)])[0]
    original = run(
        db, "post_bank", observation_id=obs, rationale="Actual bank payment"
    )["event_id"]
    journal(
        db,
        cards["holder"],
        "2026-03-01",
        [
            ln(control(db, cards["holder"], "suspense_debit"), -500),
            ln(control(db, cards["holder"], "bridge"), 500, item="LEGACY"),
        ],
        source={"observation_id": obs},
        corrects_id=original,
    )
    issuer = issuer_rows(
        cards, [dict(date="2026-03-03", amount_cents=500, external_id="paid")]
    )["observation_ids"][0]
    before = balance(db, cards["bank"], date.max)
    result = run(
        db,
        "match_card_repayment",
        code="ADOPT",
        card_id=cards["card"],
        bank_observation=reference(db, obs),
        issuer_observation=reference(db, issuer),
        item="LEGACY",
        adopt_classified_bank=True,
        evidence="Exact repayment references",
    )
    assert result["bank_cash_delta_cents"] == 0
    assert balance(db, cards["bank"], date.max) == before
    assert db.get(m.BankObservation, obs).event_id == original
    assert balance(db, control(db, cards["holder"], "bridge"), date.max) == 0


def test_existing_fully_offsetting_return_requires_source_proof(portfolio):
    from tests.test_portfolio_card_workflows import reference

    db, entity, bank, _, _ = portfolio
    ids = bank_rows(
        db,
        bank,
        [
            dict(date="2026-03-01", amount_cents=-500),
            dict(date="2026-03-01", amount_cents=500),
        ],
    )
    events = [
        run(db, "post_bank", observation_id=i, rationale="Actual bank entry")[
            "event_id"
        ]
        for i in ids
    ]
    correction = journal(
        db,
        entity,
        "2026-03-01",
        [
            ln(control(db, entity, "suspense_debit"), -500),
            ln(control(db, entity, "suspense_credit"), 500),
        ],
        source={"observation_ids": ids, "source_event_ids": events},
        corrects_id=events[0],
    )["event_id"]
    proof = run(
        db,
        "attach_document",
        code="RETURN-EVIDENCE",
        title="Bank return record",
        locator="/tmp/synthetic-return-proof.pdf",
        sha256="a" * 64,
        event_id=correction,
    )
    review = (
        db.query(m.ReviewItem).filter_by(observation_id=ids[0], code="suspense").one()
    )
    data = dict(
        observation={**reference(db, ids[0]), "review_id": review.id},
        corrective_event_id=correction,
        balances=[],
        evidence="Exact returned payment",
        movement_manifest=dict(
            kind="bank_return",
            observations=[
                dict(
                    observation_id=i,
                    account_id=bank,
                    date="2026-03-01",
                    amount_cents=amount,
                )
                for i, amount in zip(ids, (-500, 500))
            ],
        ),
    )
    with pytest.raises(Problem):
        run(db, "validate_existing_correction", **data)
    data["offsetting_evidence"] = dict(
        document_id=proof["document_id"],
        sha256="a" * 64,
        explanation="Original bank return reference",
    )
    assert run(db, "validate_existing_correction", **data)["new_financial_events"] == 0


def test_link_classified_counterparty_preserves_purpose_and_funds(portfolio):
    from tests.test_portfolio_card_workflows import reference
    from app.portfolio.observation_state import observation_state

    db, e, bank, expense, income = portfolio
    receiving = account(db, e, "receiving", "asset", "bank")
    outgoing = bank_rows(db, bank, [dict(date="2026-04-01", amount_cents=-700)])[0]
    incoming = bank_rows(db, receiving, [dict(date="2026-04-02", amount_cents=700)])[0]
    run(db, "post_bank", observation_id=outgoing, rationale="Actual outgoing")
    run(
        db,
        "post_bank",
        observation_id=incoming,
        offset_account_id=income,
        rationale="Prior classification",
    )
    before = financial_snapshot(db)
    result = run(
        db,
        "link_classified_transfer",
        withdrawal=reference(db, outgoing),
        deposit=reference(db, incoming),
        evidence="Bank trace identifies both legs",
    )
    assert result["resolved_review_ids"] == []
    assert financial_snapshot(db) == before
    assert (
        observation_state(db, outgoing)["counterparty_links"][0]["deposit_id"]
        == incoming
    )
    assert observation_state(db, outgoing)["suspense_residuals"]["accrual"]
    with pytest.raises(Problem) as error:
        run(
            db,
            "link_classified_transfer",
            withdrawal=reference(db, outgoing),
            deposit=reference(db, incoming),
            evidence="Duplicate link",
        )
    assert error.value.code == "transfer_link"


@pytest.mark.parametrize(
    "limit, cursor, revision",
    [(0, 0, None), (201, 0, None), (1, -1, None), (1, 1, None)],
)
def test_export_rejects_invalid_or_unbound_pages(portfolio, limit, cursor, revision):
    from app.portfolio.verification_export import verification_export

    with pytest.raises(Problem):
        verification_export(portfolio[0], cursor, limit, revision)


@pytest.mark.parametrize("fault", ["hash", "link", "conflicting_party"])
def test_beneficiary_evidence_cannot_override_existing_identity(historical, fault):
    db = historical[0]
    source = {"observation_id": historical[2].id}
    if fault == "conflicting_party":
        source["party_id"] = historical[6]
    data = historical_correction(historical, source=source, kind="legacy")
    doc = m.Document(code="BAD-PROOF", title="Evidence")
    db.add(doc)
    db.flush()
    db.add(
        m.DocumentLocation(
            document_id=doc.id, locator="/tmp/proof.pdf", sha256="a" * 64
        )
    )
    if fault != "link":
        db.add(m.DocumentLink(document_id=doc.id, event_id=data["corrective_event_id"]))
    db.flush()
    data["balances"][0]["beneficiary_evidence"] = dict(
        document_id=doc.id,
        sha256=("b" if fault == "hash" else "a") * 64,
        explanation="Purported identity",
    )
    before = financial_snapshot(db)
    with pytest.raises(Problem):
        run(db, "validate_existing_correction", **data)
    assert financial_snapshot(db) == before


def test_plain_text_rejection_preserves_diagnostic(monkeypatch):
    import asyncio
    import http.client
    import json
    from mcp.server.fastmcp.exceptions import ToolError
    from app.portfolio import mcp_server

    class Response:
        status = 409

        def read(self):
            return b"allocation_source_deficit: dated fund holdings insufficient"

    class Connection:
        def __init__(self, *args, **kwargs):
            pass

        def request(self, *args, **kwargs):
            pass

        def getresponse(self):
            return Response()

        def close(self):
            pass

    monkeypatch.setattr(http.client, "HTTPConnection", Connection)
    monkeypatch.setattr(
        mcp_server, "configuration", lambda: ("http://127.0.0.1:3001", "synthetic")
    )
    with pytest.raises(ToolError) as caught:
        asyncio.run(mcp_server.request("POST", "/proposals", body={"key": "preview"}))
    result = json.loads(str(caught.value))
    assert result["execution_state"] == "rejected"
    assert "allocation_source_deficit" in result["detail"]
