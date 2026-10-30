"""Recoverable agent outputs and evidence checks, using synthetic books only."""

import asyncio
from datetime import date
import hashlib
import json

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from app.models.audit import AuditLog
from app.portfolio import models as m, routes, runtime, mcp_server
from app.portfolio.agent_exports import review_register, proof_page, run_manifest
from app.portfolio.evidence_plans import (
    document_state,
    local_source,
    coverage_plan,
    evidence_plan,
)
from app.portfolio.storage_diagnostics import storage_diagnostics
from app.portfolio.service import Problem, digest, control
from tests.test_balancedbooks_portfolio import run, entity, journal, ln
from tests.test_portfolio_runtime import synthetic_observations
from tests import test_balancedbooks_portfolio as fixtures
from tests import test_portfolio_card_workflows as card_fixtures
from tests.test_portfolio_card_workflows import coverage_data
from tests.test_portfolio_posted_mortgage import payment_case
from tests.test_portfolio_transfer_components import pair, request
from app.portfolio.mortgages import mortgage_state
from app.portfolio.card_workflows import card_state

portfolio = fixtures.portfolio
cards = card_fixtures.cards


def test_revision_updates_keep_guards_and_financial_audit_without_fanout(db_session):
    db = db_session
    owner, bank, ids = synthetic_observations(db)
    entity(db, "SECOND")
    db.flush()
    before = dict(db.query(m.Entity.id, m.Entity.revision))
    audit_id = db.query(AuditLog.id).order_by(AuditLog.id.desc()).first()[0]
    journal(
        db, owner, "2026-02-01", [ln(bank, 100), ln(control(db, owner, "equity"), -100)]
    )
    db.flush()
    assert dict(db.query(m.Entity.id, m.Entity.revision)) == {
        k: v + 1 for k, v in before.items()
    }
    new = db.query(AuditLog).filter(AuditLog.id > audit_id).all()
    assert not [
        r for r in new if r.table_name == "re_entities" and r.action == "UPDATE"
    ]
    assert any(r.table_name == "re_events" for r in new)
    assert (
        db.query(AuditLog).filter(AuditLog.table_name == "transaction_lines").count()
        > 0
    )
    assert evidence_plan(db, ids[0])["financial_entries_added"] is False


def test_register_and_unicode_proof_paging_are_exact_and_stale_safe(db_session):
    db = db_session
    synthetic_observations(db, 3)
    review = db.query(m.ReviewItem).first()
    review.resolution = json.dumps({"proof": "é🐈" * 6000}, ensure_ascii=False)
    db.flush()
    full = review.resolution
    page = review_register(db, limit=1)
    assert not page["complete"] and page["rows"][0]["excerpt_only"]
    assert page["rows"][0]["resolution_bytes"] > 30000
    assert "resolution" not in page["rows"][0]
    rows = page["rows"]
    while not page["complete"]:
        page = review_register(
            db,
            after_id=page["next_after_id"],
            limit=1,
            expected_book_digest=page["book_digest"],
        )
        rows += page["rows"]
    assert len(rows) == db.query(m.ReviewItem).count()
    with pytest.raises(Problem, match="Restart"):
        review_register(
            db, after_id=rows[0]["review_id"], expected_book_digest="0" * 64
        )
    chunks = []
    offset = 0
    guard = None
    while True:
        proof = proof_page(db, "review", str(review.id), offset, 101, guard)
        chunks.append(proof["text"])
        guard = proof["sha256"]
        if proof["complete"]:
            break
        offset = proof["next_offset"]
    assert "".join(chunks) == full
    assert guard == hashlib.sha256(full.encode()).hexdigest()
    with pytest.raises(Problem, match="unbound"):
        proof_page(db, "review", str(review.id), 1)
    review.resolution = "changed"
    db.flush()
    with pytest.raises(Problem, match="changed"):
        proof_page(db, "review", str(review.id), 1, expected_digest=guard)


def test_compact_proofs_keep_exact_effect_and_recovery_reference():
    result = {
        "key": "proof/?#",
        "assessment": {
            "supported": True,
            "rows": [{"line": i, "text": "x" * 400} for i in range(100)],
        },
    }
    short = runtime.operation_result(result, projection="compact")
    assert short["effect_digest"] == runtime.operation_result(result)["effect_digest"]
    assert short["proof_summaries"]["assessment"]["digest"] == digest(
        result["assessment"]
    )
    assert short["proof_summaries"]["assessment"]["summary"] == {"supported": True}
    assert "assessment" not in short and "assessment" in result
    assert "%3F%23" in short["full_result_ref"]


def test_manifest_distinguishes_previews_commits_unknown_and_attestation(
    client, db_session
):
    db = db_session
    command = {
        "operation": "create_entity",
        "key": "manifest/one",
        "data": {"code": "MANIFEST", "name": "Synthetic"},
    }
    prepared = client.post("/api/portfolio/proposals", json=command)
    assert prepared.status_code == 200, prepared.text
    p = prepared.json()
    state = run_manifest(db, [command["key"], "missing"])
    assert [r["state"] for r in state["operations"]] == ["prepared", "not_found"]
    committed = client.post(
        f"/api/portfolio/proposals/{p['proposal_id']}/commit",
        json={"digest": p["digest"]},
    )
    assert committed.status_code == 200, committed.text
    row = run_manifest(db, [command["key"]])["operations"][0]
    assert row["state"] == "committed" and row["attestations"] == []
    assert row["body_digest"] == db.get(m.RequestRecord, command["key"]).digest
    proof = run(
        db,
        "attach_document",
        code="PROOF",
        title="Synthetic",
        locator="/synthetic/proof",
        sha256="a" * 64,
    )
    run(
        db,
        "record_operation_verification",
        request_key=command["key"],
        effect_digest=row["effect_digest"],
        scope="accounting_effects",
        status="passed",
        source_documents=[dict(document_id=proof["document_id"], sha256="a" * 64)],
        proof_reference="Synthetic independent audit",
        summary="Exact effects reviewed",
    )
    row = run_manifest(db, [command["key"]])["operations"][0]
    assert row["attestations"][0]["scope"] == "accounting_effects"
    assert row["attestations"][0]["status"] == "passed"
    another = {**command, "key": "manifest/two", "data": {"code": "M2", "name": "Two"}}
    assert client.post("/api/portfolio/proposals", json=another).status_code == 200
    entity(db, "OTHER")
    assert run_manifest(db, ["manifest/two"])["operations"][0]["state"] == "stale"
    for invalid in ([], ["same", "same"], ["x" * 121]):
        with pytest.raises(Problem):
            run_manifest(db, invalid)


def test_document_location_verifies_bytes_and_preserves_original(portfolio, tmp_path):
    db = portfolio[0]
    content = b"original synthetic bank statement\n"
    sha = hashlib.sha256(content).hexdigest()
    doc = run(
        db,
        "attach_document",
        code="MISSING",
        title="Original",
        locator=str(tmp_path / "missing.pdf"),
        sha256=sha,
    )["document_id"]
    assert (
        document_state(db, doc, True)["locations"][0]["inspection"]["status"]
        == "unavailable"
    )
    relocated = tmp_path / "found.pdf"
    relocated.write_bytes(content)
    before = db.query(m.Event).count()
    result = run(
        db,
        "relocate_document",
        document_id=doc,
        locator=str(relocated),
        expected_sha256=sha,
        evidence="Exact relocated original",
    )
    assert result["content_verified"] and result["original_locations_preserved"]
    state = document_state(db, doc, True)
    assert (
        len(state["locations"]) == 2
        and state["locations"][1]["inspection"]["status"] == "exact_hash_verified"
    )
    assert db.query(m.Event).count() == before
    relocated.write_bytes(b"different export")
    assert (
        document_state(db, doc, True)["locations"][1]["inspection"]["status"]
        == "different_bytes"
    )
    with pytest.raises(Problem, match="exactly"):
        run(
            db,
            "relocate_document",
            document_id=doc,
            locator=str(relocated),
            expected_sha256=sha,
            evidence="Different",
        )
    assert db.query(m.DocumentLocation).filter_by(document_id=doc).count() == 2
    link = tmp_path / "link"
    link.symlink_to(relocated)
    assert not local_source(str(link), True)["bytes_verified"]
    assert not local_source(str(tmp_path), True)["bytes_verified"]
    assert (
        local_source("https://example.com/secret", True)["status"]
        == "remote_not_fetched"
    )


def test_coverage_plans_show_trailing_and_stale_inventory(portfolio):
    db, _, bank, _, _ = portfolio
    run(
        db,
        "reconcile",
        account_id=bank,
        start="2026-01-01",
        end="2026-01-31",
        opening_cents=100000,
        ending_cents=100000,
    )
    plan = coverage_plan(db, bank, date(2026, 1, 1), date(2026, 2, 28))
    assert plan["missing_intervals"] == [
        {"start": "2026-02-01", "end": "2026-02-28", "kind": "trailing"}
    ]
    from tests.test_balancedbooks_portfolio import bank_rows

    bank_rows(db, bank, [{"date": "2026-01-20", "amount_cents": 100}])
    changed = coverage_plan(db, bank, date(2026, 1, 1), date(2026, 2, 28))
    assert not changed["intervals"][0]["valid_against_current_book"]
    assert "stale_source_inventory" in changed["intervals"][0]["blockers"]


def test_zero_card_balance_does_not_certify_uncovered_trailing_period(cards):
    run(cards["db"], "record_card_coverage", **coverage_data(cards))
    state = card_state(cards["db"], cards["card"], date(2026, 3, 31))
    assert state["book_liability_cents"] == 0 and state["coverage_gaps"] == []
    assert state["historical_coverage_valid"] and not state["current_balance_certified"]
    assert state["uncovered_intervals"] == [
        {"kind": "trailing", "start": "2026-03-01", "end": "2026-03-31"}
    ]


def test_alias_replacement_requires_same_account_and_registered_issuer_evidence(cards):
    db = cards["db"]
    prior = db.query(m.CardAlias).one()
    doc = run(
        db,
        "attach_document",
        code="REPLACEMENT",
        title="Issuer replacement",
        locator="/synthetic/issuer",
        sha256="f" * 64,
    )
    data = dict(
        card_id=cards["card"],
        alias="NEW",
        start="2026-02-01",
        replaces_alias_id=prior.id,
        replacement_evidence=dict(
            document_id=doc["document_id"],
            sha256="f" * 64,
            explanation="Issuer confirms same account",
        ),
        evidence="Replacement",
    )
    result = run(db, "add_card_alias", **data)
    row = db.get(m.CardAlias, result["card_alias_id"])
    assert (
        row.replaces_alias_id == prior.id
        and row.replacement_evidence["sha256"] == "f" * 64
    )
    with pytest.raises(Problem):
        run(
            db,
            "add_card_alias",
            **{
                **data,
                "alias": "OTHER",
                "replacement_evidence": {
                    **data["replacement_evidence"],
                    "sha256": "e" * 64,
                },
            },
        )


def test_mortgage_correction_cannot_consume_debt_already_repaid_later(portfolio):
    db, owner, _, expense, _ = portfolio
    data, _ = payment_case(portfolio, opening=15000)
    journal(
        db,
        owner,
        "2026-03-01",
        [ln(data["debt_account_id"], 10000, item="LOAN-1"), ln(expense, -10000)],
    )
    state = mortgage_state(
        db, data["observation_id"], data["debt_account_id"], "LOAN-1"
    )
    assert state["supported_liability_cents"]["accrual"] == 15000
    assert state["remaining_principal_capacity_cents"]["accrual"] == 5000
    data["expected_debt_digest"] = state["state_digest"]
    with pytest.raises(Problem, match="supported debt"):
        run(db, "correct_posted_mortgage_payment", **data)


def test_transfer_association_history_is_immutable(portfolio):
    db = portfolio[0]
    ids = pair(portfolio)
    from app.portfolio.guards import install

    install(db.connection())
    run(db, "associate_transfer_component", **request(db, ids))
    for sql in (
        "UPDATE re_transfer_associations SET amount_cents=1",
        "DELETE FROM re_transfer_associations",
        "INSERT OR REPLACE INTO re_transfer_associations SELECT * FROM re_transfer_associations",
    ):
        with db.begin_nested():
            with pytest.raises(IntegrityError, match="immutable"):
                db.execute(text(sql))


def test_compact_storage_never_scans_proofs_or_dbstat(db_session, monkeypatch):
    connection = db_session.connection()
    original = connection.exec_driver_sql

    def checked(sql, *args, **kwargs):
        assert "dbstat" not in sql and "length(CAST" not in sql
        return original(sql, *args, **kwargs)

    monkeypatch.setattr(connection, "exec_driver_sql", checked)
    state = storage_diagnostics(db_session, "compact")
    assert state["projection"] == "compact" and state["payloads"] == []
    assert state["dbstat_available"] is None


def test_schema_filters_and_native_parameter_forwarding(monkeypatch):
    all_schemas = routes.commands()
    assert routes.commands("relocate_document,associate_transfer_component") == {
        k: all_schemas[k] for k in ("relocate_document", "associate_transfer_component")
    }
    assert routes.commands() is all_schemas
    captured = []

    async def capture(method, path, **kwargs):
        captured.append((method, path, kwargs))
        return {}

    monkeypatch.setattr(mcp_server, "request", capture)
    asyncio.run(
        mcp_server.proof_page("review", "5", limit=100, expected_digest="a" * 64)
    )
    assert captured[-1][2]["params"] == dict(
        kind="review", identity="5", offset=0, limit=100, expected_digest="a" * 64
    )
    asyncio.run(mcp_server.storage_diagnostics())
    assert captured[-1][2]["params"] == {"projection": "compact"}
