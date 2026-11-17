# ruff: noqa: F811
from datetime import date
from io import BytesIO
from openpyxl import load_workbook
import pytest
from app.portfolio import models as m
from app.portfolio.observation_state import observation_state
from app.portfolio.reports import ledger
from app.portfolio.service import Problem
from app.portfolio.transaction_register import (
    register_page,
    write_register,
    receipt_candidates,
    mappings,
    movement_check,
)
from tests.test_balancedbooks_portfolio import portfolio  # noqa: F401
from tests.test_balancedbooks_portfolio import (
    run,
    bank_rows,
    account,
    ln,
    journal,
    entity,
)  # noqa: F401
from tests.test_portfolio_card_workflows import cards, issuer_rows  # noqa: F401


def movement(db, bank, n, day="2026-03-01"):
    oid = bank_rows(
        db, bank, [dict(date=day, amount_cents=n, description="=A1")], str(n) + ".csv"
    )[0]
    run(
        db,
        "post_bank",
        observation_id=oid,
        rationale="Observed on statement",
        duplicate_resolution="distinct",
    )
    return (
        oid,
        db.query(m.ReviewItem).filter_by(observation_id=oid, code="suspense").one(),
    )


def document(db):
    doc = run(
        db,
        "attach_document",
        code="PROOF",
        title="Statement",
        locator="/tmp/synthetic",
        sha256="1" * 64,
    )
    return dict(
        document_id=doc["document_id"],
        sha256="1" * 64,
        explanation="Issuer statement, row 3",
    )


def test_link_card_existing_identity_and_guard(cards):
    c = cards
    db = c["db"]
    oid = issuer_rows(
        c, [{"date": "2026-03-01", "amount_cents": -1200, "external_id": "provider1"}]
    )["observation_ids"][0]
    event = journal(
        db,
        c["holder"],
        "2026-03-01",
        [ln(c["card_account"], -1200), ln(c["expense"], 1200)],
        source={"id": "provider1", "status": "settled"},
    )["event_id"]
    jid = db.query(m.JournalScope.transaction_id).filter_by(event_id=event).scalar()
    data = dict(
        observation_id=oid,
        expected_state_digest=observation_state(db, oid)["state_digest"],
        event_id=event,
        journal_id=jid,
        provider_transaction_id="provider1",
        evidence="Independent issuer evidence",
    )
    with pytest.raises(Problem, match="provider identity"):
        run(
            db,
            "link_existing_card_journal",
            **{**data, "provider_transaction_id": "different"}
        )
    second = journal(
        db,
        c["holder"],
        "2026-03-01",
        [ln(c["card_account"], -1200), ln(c["expense"], 1200)],
        source={"id": "other"},
    )
    assert second["event_id"] != event
    before = ledger(db, date.max)
    run(db, "link_existing_card_journal", **data)
    assert (
        db.get(m.BankObservation, oid).event_id == event
        and ledger(db, date.max) == before
    )
    check = movement_check(
        db, c["card_account"], date(2026, 3, 1), date(2026, 3, 1), 0, 10, None
    )
    assert (
        check["discrepancies"] == 1
        and check["rows"][0]["event_id"] == second["event_id"]
    )


def test_review_workpaper_history_workbook_and_retyping(portfolio):
    db, e, bank, expense, _ = portfolio
    oid, r = movement(db, bank, -3000)
    base = dict(
        observation_id=oid,
        kind="missing_source",
        description="Missing front",
        confidence="unknown",
        sources=[dict(kind="stub", reference="File A page 3")],
    )
    note = run(db, "record_transaction_research", **base)["record"]["id"]
    long = "Exact Unicode proof 🏘 " * 2500
    run(
        db,
        "record_transaction_research",
        **{
            **base,
            "supersedes_id": note,
            "kind": "confirmed_fact",
            "description": long,
            "sources": [dict(kind="check_front", reference="File B page 2")],
        }
    )
    run(
        db,
        "retype_review",
        review_id=r.id,
        expected_code="suspense",
        new_code="software_limitation",
        evidence="Prior validation returned an error",
        source="review log",
        error_code="expected_balance",
    )
    assert r.status == "open"
    with pytest.raises(Problem):
        run(db, "resolve_review", review_id=r.id, evidence="unsupported")
    reg = register_page(db, ["software_limitation"], "open", 0, 10, None)
    assert reg["flags"] == reg["distinct_transactions"] == 1
    buf = BytesIO()
    write_register(db, buf, ["software_limitation"])
    rows = list(load_workbook(buf, read_only=True).active.values)
    assert [x[1] for x in rows if x[0] == "Transaction"] == [oid]
    assert any("continued" in x[0] for x in rows)
    run(
        db,
        "retype_review",
        review_id=r.id,
        expected_code=r.code,
        new_code="suspense",
        evidence="Ready to retry",
        source="review log",
    )
    with pytest.raises(Problem, match="current book"):
        register_page(db, None, "open", oid, 10, reg["book_digest"])


def test_component_then_remaining_classification(portfolio):
    db, e, bank, expense, _ = portfolio
    oid, review = movement(db, bank, -3000)

    def part(n):
        return run(
            db,
            "record_transaction_research",
            observation_id=oid,
            kind="proposed_component",
            description="Source item",
            confidence="confirmed",
            sources=[dict(kind="invoice", reference="Invoice line")],
            amount_cents=n,
            natural_account_id=expense,
            entity_id=e,
        )["record"]["id"]

    a, b = part(1000), part(2000)

    def apply(ident):
        return run(
            db,
            "correct_posted_components",
            observation_id=oid,
            review_id=review.id,
            expected_state_digest=observation_state(db, oid)["state_digest"],
            component_ids=[ident],
            evidence="Apply supported amount",
        )

    assert apply(a)["amount_requiring_evidence_cents"] == 2000
    assert review.status == "open"
    with pytest.raises(Problem):
        apply(a)
    assert apply(b)["amount_requiring_evidence_cents"] == 0
    assert review.status == "resolved"
    for basis in ("accrual", "modified_cash_bank_posting"):
        assert (
            sum(
                l["debit_cents"] - l["credit_cents"]
                for l in ledger(db, date.max, basis=basis, account_id=expense)
            )
            == 3000
        )


def test_processor_settlement_capacity_and_identity(portfolio):
    db, e, bank, _, _ = portfolio
    src, _ = movement(db, bank, -900)
    dst, _ = movement(db, bank, 800)
    proof = document(db)
    data = dict(
        source_observation_id=src,
        settlement_observation_id=dst,
        kind="refund",
        amount_cents=800,
        evidence=proof,
    )
    linked = run(db, "link_observation_application", **data)["record"]
    with pytest.raises(Problem):
        run(db, "link_observation_application", **data)
    run(
        db,
        "link_observation_application",
        **{**data, "supersedes_id": linked["id"], "reverse": True}
    )
    batch = dict(
        observation_id=dst,
        processor="Example",
        batch_id="batch1",
        receipts=[
            dict(
                provider_transaction_id="r1",
                gross_cents=900,
                fee_cents=100,
                beneficiary_entity_id=e,
                tenant_reference="tenant",
                source_row="3",
            )
        ],
        evidence=proof,
    )
    assert run(db, "record_processor_batch", **batch)["net_cents"] == 800
    with pytest.raises(Problem):
        run(db, "record_processor_batch", **batch)
    alt = account(db, e, "NewBank", "asset", "bank")
    run(
        db,
        "record_identity_mapping",
        account_id=bank,
        legal_entity_id=e,
        replacement_account_id=alt,
        alias="1234",
        start="2026-01-01",
        evidence=proof,
    )
    assert mappings(db, alias="1234")[0]["payload"]["replacement_account_id"] == alt
    with pytest.raises(Problem, match="cycle"):
        run(
            db,
            "record_identity_mapping",
            account_id=alt,
            legal_entity_id=e,
            replacement_account_id=bank,
            start="2026-03-01",
            evidence=proof,
        )
    result = receipt_candidates(db, bank, date(2026, 3, 1), date(2026, 3, 1), -900)
    assert result["candidates"][0]["assessment"] == "needs_more_evidence"


def test_routed_validation_requires_all_three_legs_and_each_basis(portfolio):
    from app.portfolio.service import control
    from app.portfolio.correction_validation import BASES

    db, holder, bank, _, _ = portfolio
    landlord = entity(db, "OWNER")
    target = account(db, landlord, "checking", "asset", "bank")
    rent = account(db, landlord, "rent", "income", "income")
    alternate = account(db, landlord, "fees", "income", "income")
    a, ra = movement(db, bank, 5000)
    b, _ = movement(db, bank, -5000)
    c, rc = movement(db, target, 5000)
    day = "2026-03-01"
    # A dated intermediary bridge clears in full. Both original cash legs persist.
    bridge = control(db, holder, "bridge")
    for oid, lines in [
        (a, [ln(control(db, holder, "suspense_credit"), 5000), ln(bridge, -5000)]),
        (b, [ln(control(db, holder, "suspense_debit"), -5000), ln(bridge, 5000)]),
    ]:
        journal(
            db,
            holder,
            day,
            lines,
            corrects_id=db.get(m.BankObservation, oid).event_id,
            source={"observation_id": oid, "related_observation_ids": [a, b, c]},
        )
    application = journal(
        db,
        landlord,
        day,
        [ln(control(db, landlord, "suspense_credit"), 5000), ln(rent, -5000)],
        corrects_id=db.get(m.BankObservation, c).event_id,
        source={"observation_id": c},
        adjustments=[
            {
                "entity_id": landlord,
                "date": day,
                "reason": "Supported basis reclassification",
                "lines": [ln(rent, 1200), ln(alternate, -1200)],
            }
        ],
    )["event_id"]
    legs = [
        dict(observation_id=i, account_id=acct, date=day, amount_cents=v)
        for i, acct, v in [(a, bank, 5000), (b, bank, -5000), (c, target, 5000)]
    ]
    decl = dict(
        accrual=[dict(account_id=rent, amount_cents=-5000)],
        modified_cash_bank_posting=[
            dict(account_id=rent, amount_cents=-3800),
            dict(account_id=alternate, amount_cents=-1200),
        ],
    )
    for oid, review in [(a, ra), (c, rc)]:
        ref = dict(
            observation_id=oid,
            review_id=review.id,
            expected_state_digest=observation_state(db, oid)["state_digest"],
        )
        data = dict(
            observation=ref,
            corrective_event_id=application,
            evidence="Settled receipt and complete transfer evidence",
            balances_by_basis=decl,
            movement_manifest={"kind": "routed_receipt", "observations": legs},
        )
        if oid == a:
            with pytest.raises(Problem, match="withdrawal"):
                run(
                    db,
                    "validate_existing_correction",
                    **{
                        **data,
                        "movement_manifest": {
                            "kind": "routed_receipt",
                            "observations": legs[::2],
                        },
                    }
                )
            with pytest.raises(Problem, match="expected balance"):
                run(
                    db,
                    "validate_existing_correction",
                    **{
                        **data,
                        "balances_by_basis": dict.fromkeys(BASES, decl["accrual"]),
                    }
                )
            assert review.status == "open"
        result = run(db, "validate_existing_correction", **data)
        assert result["new_financial_events"] == 0 and review.status == "resolved"
