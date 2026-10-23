"""Synthetic agent runtime contracts: recovery, scopes and exact effect output."""

import asyncio
from datetime import date
import http.client
import json
import pytest
from sqlalchemy import event, create_engine
from sqlalchemy.orm import sessionmaker
from mcp.server.fastmcp.exceptions import ToolError
from app.database import Base
from app.models.settings import Settings
from app.models.transactions import TransactionLine
from app.portfolio import models as m, reports, routes, mcp_server
from app.portfolio.observation_state import observation_state
from app.portfolio.review_effects import ReviewEffects
from tests.test_balancedbooks_portfolio import account, bank_rows, entity, run


def synthetic_observations(db, count=2):
    e = entity(db, "RUNTIME")
    bank = account(db, e, "bank", "asset", "bank", coverage_start="2026-01-01")
    ids = bank_rows(
        db,
        bank,
        [
            {"date": "2026-01-02", "amount_cents": 100 + i, "description": f"Row {i}"}
            for i in range(count)
        ],
    )
    for ident in ids:
        run(db, "post_bank", observation_id=ident, rationale="Synthetic bank evidence")
    return e, bank, ids


def test_bounded_states_preserve_full_digest_and_financial_components(db_session):
    _, _, ids = synthetic_observations(db_session)
    full = routes.observation_states(ids=ids, db=db_session)
    compact = routes.observation_states(ids=ids, projection="compact", db=db_session)
    assert full["truncated"] is False
    assert full["snapshot_digest"] == compact["snapshot_digest"]
    for ident, state, short in zip(ids, full["states"], compact["states"]):
        assert state == observation_state(db_session, ident)
        for field in (
            "state_digest",
            "event_ids",
            "suspense_residuals",
            "bank_movements",
            "cash_lineage",
        ):
            assert state[field] == short[field]
        assert "source" not in short["observation"]
        assert "observation/source" in short["omitted_fields"]
        assert short["full_result_ref"].endswith("as_of=9999-12-31")


def test_bounded_states_http_limits_and_unknown_ids(client):
    for ids in ([], [0], [1, 1], list(range(1, 202))):
        assert (
            client.get(
                "/api/portfolio/observation-states", params={"ids": ids}
            ).status_code
            == 422
        )
    assert (
        client.get(
            "/api/portfolio/observation-states", params={"ids": [99999]}
        ).status_code
        == 404
    )


def test_batch_snapshot_survives_concurrent_writer(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'snapshot.db'}")
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA journal_mode=WAL")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    with factory() as writer:
        _, _, ids = synthetic_observations(writer)
        review = (
            writer.query(m.ReviewItem)
            .filter_by(observation_id=ids[1], code="suspense")
            .one()
        )
        review_id, original = review.id, review.detail
        writer.commit()
    import app.portfolio.observation_state as state_module

    original_reader = state_module.observation_state
    calls = []

    def concurrent_read(db, ident, through=None):
        result = original_reader(db, ident, through)
        calls.append(ident)
        if len(calls) == 1:
            with factory() as writer:
                writer.get(m.ReviewItem, review_id).detail = (
                    "Changed outside the read snapshot"
                )
                writer.commit()
        return result

    monkeypatch.setattr(state_module, "observation_state", concurrent_read)
    with factory() as reader:
        result = routes.observation_states(ids=ids, db=reader)
        assert (
            next(r for r in result["states"][1]["reviews"] if r["id"] == review_id)[
                "detail"
            ]
            == original
        )
    with factory() as reader:
        assert reader.get(m.ReviewItem, review_id).detail != original
    engine.dispose()


def test_scoped_override_output_matches_full_ledger_and_skips_other_lines(db_session):
    _, bank, ids = synthetic_observations(db_session, 3)
    events = [db_session.get(m.BankObservation, ident).event_id for ident in ids]
    lines = (
        db_session.query(TransactionLine)
        .filter_by(account_id=bank)
        .order_by(TransactionLine.id)
        .all()
    )
    for i, line in enumerate(lines):
        db_session.add_all(
            [
                m.CashClassification(
                    line_id=line.id,
                    event_id=events[i],
                    date=date(2026, 1, 2),
                    cash_flow="investing",
                    reason="First",
                ),
                m.CashClassification(
                    line_id=line.id,
                    event_id=events[i],
                    date=date(2026, 1, 2),
                    cash_flow="financing",
                    reason="Same-day later wins",
                ),
                m.CashClassification(
                    line_id=line.id,
                    event_id=events[i],
                    date=date(2026, 2, 2),
                    cash_flow="operating",
                    reason="Future excluded",
                ),
            ]
        )
    db_session.flush()
    full = reports.ledger(db_session, date(2026, 1, 31))
    loaded = []

    def capture(session, instance):
        if isinstance(instance, m.CashClassification):
            loaded.append(instance.line_id)

    event.listen(db_session, "loaded_as_persistent", capture)
    try:
        scoped = reports.ledger(db_session, date(2026, 1, 31), event_ids=[events[0]])
    finally:
        event.remove(db_session, "loaded_as_persistent", capture)
    assert scoped == [row for row in full if row["event_id"] == events[0]]
    assert set(loaded) <= {lines[0].id}
    assert [row["cash_flow"] for row in scoped if row["role"] == "bank"] == [
        "financing"
    ]


def test_review_effects_capture_exact_net_lifecycle_and_cleanup(db_session):
    old = m.ReviewItem(code="purpose", detail="Old", status="open")
    closed = m.ReviewItem(code="funding_source", detail="Closed", status="resolved")
    unrelated = [
        m.ReviewItem(code="purpose", detail="Unrelated", status="open")
        for _ in range(100)
    ]
    db_session.add_all([old, closed, *unrelated])
    db_session.flush()
    with ReviewEffects(db_session) as collector:
        old.status = "resolved"
        db_session.flush()
        old.status = "open"
        closed.status = "open"
        new = m.ReviewItem(code="purpose", detail="New")
        db_session.add(new)
        db_session.flush()
    result = collector.result()
    assert result["preexisting_closed_ids"] == []
    assert result["new_open_ids"] == [new.id]
    assert result["reopened_ids"] == [closed.id]
    assert result["net_open_reduction"] == -2
    assert result["by_code"]["funding_source"]["reopened_count"] == 1
    assert not event.contains(db_session, "before_flush", collector.capture)
    with pytest.raises(RuntimeError):
        with ReviewEffects(db_session) as failed:
            raise RuntimeError("rollback")
    assert not event.contains(db_session, "before_flush", failed.capture)


def test_metadata_only_batch_reports_exact_outer_effects(db_session):
    _, _, ids = synthetic_observations(db_session)
    # A duplicate-resolution batch has no new journal but closes its review.
    obs = db_session.get(m.BankObservation, ids[0])
    duplicate = bank_rows(
        db_session,
        obs.account_id,
        [{"date": "2026-01-02", "amount_cents": 100, "description": "Row 0"}],
        name="duplicate.csv",
    )[0]
    result = run(
        db_session,
        "batch",
        operations=[
            {
                "operation": "resolve_duplicate",
                "data": {
                    "observation_id": duplicate,
                    "original_id": ids[0],
                    "evidence": "Synthetic exact duplicate",
                },
            }
        ],
    )
    assert result["event_ids"] == []
    assert result["review_lifecycle"]["gross_closures"] == 1
    assert (
        result["review_lifecycle"]["by_code"]["possible_duplicate"]["gross_closures"]
        == 1
    )


def test_connection_preconditions_and_recoverable_prepared_commit_states(
    client, db_session
):
    company = db_session.query(Settings).filter_by(key="company_session_id").first()
    if company is None:
        company = Settings(key="company_session_id", value="synthetic-company-id")
        db_session.add(company)
    company.value = "synthetic-company-id"
    db_session.commit()
    context = client.get("/api/portfolio/context").json()
    identity = context["connection"]
    assert identity["company_session_id"] == "synthetic-company-id"
    assert len(identity["build"]["source_tree_sha256"]) == 64
    command = {
        "operation": "create_entity",
        "key": "runtime-create",
        "data": {"code": "NEW", "name": "New"},
        "expected_connection": {"company_session_id": "wrong-company"},
    }
    response = client.post("/api/portfolio/commands", json=command)
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "connection_precondition_failed"
    assert db_session.query(m.Entity).count() == 0
    command["expected_connection"] = {
        "company_session_id": identity["company_session_id"],
        "command_schema_digest": identity["command_schema_digest"],
        "source_tree_sha256": identity["build"]["source_tree_sha256"],
    }
    prepared = client.post(
        "/api/portfolio/proposals?projection=compact", json=command
    ).json()
    assert prepared["execution_state"] == "prepared"
    assert prepared["preview_ids_are_provisional"] is True
    assert prepared["preview"]["result"]["execution_state"] == "prepared"
    assert (
        prepared["preview"]["result"]["full_result_ref"]
        == f"/api/portfolio/proposals/{prepared['proposal_id']}"
    )
    assert (
        client.get(f"/api/portfolio/proposals/{prepared['proposal_id']}").json()[
            "committed"
        ]
        is False
    )
    missing = client.get("/api/portfolio/requests/runtime-create").json()
    assert missing["execution_state"] == "unknown"
    committed = client.post(
        f"/api/portfolio/proposals/{prepared['proposal_id']}/commit",
        json={"digest": prepared["digest"]},
    ).json()
    assert committed["execution_state"] == "committed"
    assert committed["verification"]["external_evidence"] == "not_recorded"
    recovered = client.get("/api/portfolio/requests/runtime-create").json()["result"]
    assert recovered == committed
    replay = client.post(
        "/api/portfolio/commands?projection=compact", json=command
    ).json()
    assert replay["replayed"] is True
    assert replay["effect_digest"] == committed["effect_digest"]
    assert db_session.query(m.Entity).count() == 1
    command["data"]["name"] = "Different content"
    assert client.post("/api/portfolio/commands", json=command).status_code == 409


@pytest.mark.parametrize("setting", ["0", "3601", "NaN", "Infinity", "invalid"])
def test_timeout_configuration_is_bounded(monkeypatch, setting):
    monkeypatch.setenv("BALANCEDBOOKS_TIMEOUT_SECONDS", setting)
    with pytest.raises(ValueError):
        mcp_server.request_timeout()


@pytest.mark.parametrize("failure", [TimeoutError, http.client.RemoteDisconnected])
def test_unknown_transport_outcome_keeps_key_and_never_reposts(monkeypatch, failure):
    calls = []

    class Connection:
        def __init__(self, *args, **kwargs):
            assert kwargs["timeout"] == 601

        def request(self, method, path, **kwargs):
            calls.append(json.loads(kwargs["body"]))

        def getresponse(self):
            raise failure("secret token must not leak")

        def close(self):
            pass

    monkeypatch.setattr(
        mcp_server,
        "configuration",
        lambda: ("http://127.0.0.1:3001", "synthetic-token"),
    )
    monkeypatch.setattr(http.client, "HTTPConnection", Connection)
    monkeypatch.setenv("BALANCEDBOOKS_TIMEOUT_SECONDS", "601")
    payload = {
        "operation": "create_entity",
        "key": "stable-retry",
        "data": {"code": "A", "name": "A"},
    }
    with pytest.raises(ToolError) as caught:
        asyncio.run(mcp_server.request("POST", "/commands", body=payload))
    error = json.loads(str(caught.value))
    assert error["code"] == "unknown_outcome"
    assert error["key"] == "stable-retry"
    assert "identical key and body" in error["recovery"]
    assert "secret token" not in str(caught.value)
    assert calls == [payload]


def test_mcp_batch_states_forwarding(monkeypatch):
    calls = []

    async def request(method, path, params=None, body=None):
        calls.append((method, path, params))
        return {"states": []}

    monkeypatch.setattr(mcp_server, "request", request)
    asyncio.run(
        mcp_server.observation_states([4, 3], as_of="2026-01-31", projection="compact")
    )
    assert calls == [
        (
            "GET",
            "/observation-states",
            {"ids": [4, 3], "as_of": "2026-01-31", "projection": "compact"},
        )
    ]
    for ids in ([], [1, 1], [True], list(range(1, 202))):
        with pytest.raises(ValueError):
            asyncio.run(mcp_server.observation_states(ids))


def test_committed_response_loss_recovers_once_with_original_key(
    client, db_session, monkeypatch
):
    payload = {
        "operation": "create_entity",
        "key": "lost-committed-response",
        "data": {"code": "LOST", "name": "Lost response"},
    }

    class Connection:
        def __init__(self, *args, **kwargs):
            pass

        def request(self, method, path, body=None, **kwargs):
            response = client.post(path, json=json.loads(body))
            assert response.status_code == 200

        def getresponse(self):
            raise TimeoutError("Committed response was lost")

        def close(self):
            pass

    monkeypatch.setattr(
        mcp_server,
        "configuration",
        lambda: ("http://127.0.0.1:3001", "synthetic-token"),
    )
    monkeypatch.setattr(http.client, "HTTPConnection", Connection)
    with pytest.raises(ToolError, match="unknown_outcome"):
        asyncio.run(mcp_server.request("POST", "/commands", body=payload))
    recovered = client.get("/api/portfolio/requests/lost-committed-response").json()
    assert recovered["execution_state"] == "committed"
    retried = client.post("/api/portfolio/commands", json=payload).json()
    assert retried["replayed"] is True
    assert retried["effect_digest"] == recovered["result"]["effect_digest"]
    assert db_session.query(m.Entity).filter_by(code="LOST").count() == 1


def test_compact_event_preserves_effects_and_preview_refs_are_not_trial_ids(db_session):
    from app.portfolio import runtime

    _, _, ids = synthetic_observations(db_session, 1)
    event_id = db_session.get(m.BankObservation, ids[0]).event_id
    full = reports.event_detail(db_session, event_id)
    compact = runtime.project_event(full, "compact")
    for field in (
        "lines",
        "cash_classifications",
        "line_allocations",
        "management_allocations",
        "item_applications",
        "basis_adjustments",
    ):
        assert compact[field] == full[field]
    assert "source" in full["event"] and "source" not in compact["event"]
    proposal = m.Proposal(
        id=999,
        digest="digest",
        preview={
            "result": {
                "key": "prepared-only",
                "committed": False,
                "event_id": event_id,
            },
            "events": [full],
        },
    )
    response = runtime.proposal_result(proposal, "compact")
    assert (
        response["preview"]["events"][0]["full_result_ref"]
        == "/api/portfolio/proposals/999"
    )
    assert response["preview_ids_are_provisional"] is True


def test_mutation_server_error_is_unknown_outcome(monkeypatch):
    class Response:
        status = 500

        def read(self):
            return b'{"detail":"Internal error after execution"}'

    class Connection:
        def __init__(self, *args, **kwargs):
            pass

        def request(self, *args, **kwargs):
            pass

        def getresponse(self):
            return Response()

        def close(self):
            pass

    monkeypatch.setattr(
        mcp_server,
        "configuration",
        lambda: ("http://127.0.0.1:3001", "synthetic-token"),
    )
    monkeypatch.setattr(http.client, "HTTPConnection", Connection)
    with pytest.raises(ToolError) as caught:
        asyncio.run(mcp_server.request("POST", "/commands", body={"key": "same-key"}))
    error = json.loads(str(caught.value))
    assert error["code"] == "unknown_outcome"
    assert error["status"] == 500
    assert error["key"] == "same-key"


def test_already_committed_key_cannot_be_relabelled_prepared(client, db_session):
    command = {
        "operation": "create_entity",
        "key": "already-committed",
        "data": {"code": "ALREADY", "name": "Already committed"},
    }
    assert client.post("/api/portfolio/commands", json=command).status_code == 200
    before = (
        db_session.query(m.Proposal).count(),
        db_session.query(m.Event).count(),
        db_session.query(m.RequestRecord).count(),
    )
    response = client.post("/api/portfolio/proposals", json=command)
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "already_committed"
    assert (
        response.json()["detail"]["diagnostics"]["required_action"] == "request_result"
    )
    assert (
        db_session.query(m.Proposal).count(),
        db_session.query(m.Event).count(),
        db_session.query(m.RequestRecord).count(),
    ) == before
    command["data"]["name"] = "Changed payload"
    response = client.post("/api/portfolio/proposals", json=command)
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "idempotency_conflict"


@pytest.mark.parametrize(
    "key",
    [
        "folder/key",
        ".",
        "..",
        "a/../b",
        "?key=value&projection=full",
        "Unicode-é retry",
    ],
)
def test_request_recovery_preserves_every_allowed_key_character(client, key):
    command = {
        "operation": "create_entity",
        "key": key,
        "data": {"code": "KEY", "name": "Literal key"},
    }
    committed = client.post("/api/portfolio/commands?projection=compact", json=command)
    assert committed.status_code == 200
    recovered = client.get("/api/portfolio/requests", params={"key": key}).json()
    assert recovered["committed"] is True
    assert recovered["key"] == key
    assert recovered["result"]["effect_digest"] == committed.json()["effect_digest"]
    assert client.get(committed.json()["full_result_ref"]).json()["key"] == key


def test_mcp_request_recovery_uses_query_not_path(monkeypatch):
    captured = []

    async def request(method, path, params=None, body=None):
        captured.append((method, path, params))
        return {"committed": False}

    monkeypatch.setattr(mcp_server, "request", request)
    asyncio.run(mcp_server.request_result("a/../b"))
    assert captured == [("GET", "/requests", {"key": "a/../b", "projection": "full"})]
