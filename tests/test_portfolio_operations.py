"""Durable work is synthetic, atomic, recoverable, and never self-verifying."""

import json
import threading
import asyncio
import time
from concurrent.futures import ThreadPoolExecutor
from fastapi import BackgroundTasks
from starlette.requests import Request
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from app.database import Base, enable_sqlite_tuning
from app.models.api_tokens import ApiToken
from app.models.settings import Settings
from app.portfolio import (
    models as m,
    operations as ops,
    runtime,
    service as svc,
    guards,
    routes,
)
from app.portfolio.operation_models import AgentOperation, OperationVerification


@pytest.fixture
def company(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'operations.db'}",
        connect_args={"check_same_thread": False},
    )
    enable_sqlite_tuning(engine)
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        guards.install(conn)
    factory = sessionmaker(bind=engine, autoflush=False)
    with factory() as db:
        db.add(Settings(key="company_session_id", value="synthetic-company"))
        token = ApiToken(
            label="worker",
            token_hash="f" * 64,
            token_hint="synthetic",
            role="bookkeeper",
            is_active=True,
        )
        db.add(token)
        db.commit()
        authority = {"kind": "token", "id": token.id}
    yield engine, factory, authority
    engine.dispose()


def admit(company, key="queued/entity", operation="create_entity", data=None):
    engine, factory, authority = company
    payload = runtime.CommandInput(
        operation=operation,
        key=key,
        data=data or {"code": "ONE", "name": "Synthetic One"},
    )
    with factory() as db:
        routes.serialize_write(db)
        result, schedule = ops.admission(db, payload, authority, "token:worker")
        db.commit()
    return payload, result, schedule


def read_status(company, key, projection="full"):
    with company[1]() as db:
        return ops.operation_status(db, key, projection)


def test_admission_is_durable_without_accounting_and_commits_once(company):
    payload, accepted, schedule = admit(company)
    assert schedule and accepted["execution_state"] == "accepted"
    assert not accepted["committed"] and accepted["completed_children"] == 0
    with company[1]() as db:
        assert db.query(m.Entity).count() == 0
        assert db.get(m.RequestRecord, payload.key) is None
    ops.run_operation(company[0], payload.key)
    done = read_status(company, payload.key)
    assert done["committed"] and done["execution_state"] == "committed"
    assert done["completed_children"] == done["total_children"] == 1
    assert [row["status"] for row in done["history"]] == [
        "accepted",
        "running",
        "committed",
    ]
    assert done["verification"]["external_evidence"] == "not_recorded"
    _, replay, schedule = admit(company)
    assert replay == done and not schedule
    ops.run_operation(company[0], payload.key)
    assert read_status(company, payload.key) == done
    with company[1]() as db:
        assert db.query(m.Entity).count() == 1


def test_reserved_key_conflicts_across_async_and_direct_commands(company):
    payload, _, _ = admit(company)
    for path in ("submit", "direct"):
        with company[1]() as db:
            changed = payload.model_copy(
                update={"data": {"code": "OTHER", "name": "Changed"}}
            )
            with pytest.raises(svc.Problem, match="different"):
                if path == "submit":
                    ops.admission(db, changed, company[2], "token:worker")
                else:
                    svc.execute(db, changed.model_dump(exclude={"expected_connection"}))
            db.rollback()
    with company[1]() as db:
        result = svc.execute(db, payload.model_dump(exclude={"expected_connection"}))
        db.commit()
    assert result["entity_id"] == 1
    ops.run_operation(company[0], payload.key)
    assert read_status(company, payload.key)["committed"]


def test_cancel_only_before_claim_blocks_every_execution_path(company):
    payload, _, _ = admit(company)
    with company[1]() as db:
        result = ops.cancel_operation(db, payload.key)
        db.commit()
        assert result["execution_state"] == "cancelled"
        assert ops.cancel_operation(db, payload.key)["execution_state"] == "cancelled"
    ops.run_operation(company[0], payload.key)
    with company[1]() as db:
        with pytest.raises(svc.Problem, match="cancelled"):
            svc.execute(db, payload.model_dump(exclude={"expected_connection"}))
        assert db.query(m.Entity).count() == 0


def test_process_interruption_retains_running_and_same_key_resumes(
    company, monkeypatch
):
    class ProcessStopped(BaseException):
        pass

    payload, _, _ = admit(company)
    original = svc.execute
    monkeypatch.setattr(
        svc, "execute", lambda *args, **kwargs: (_ for _ in ()).throw(ProcessStopped())
    )
    with pytest.raises(ProcessStopped):
        ops.run_operation(company[0], payload.key)
    status = read_status(company, payload.key)
    assert status["execution_state"] == "running" and status["resumable"]
    assert status["completed_children"] == 0
    with company[1]() as db:
        with pytest.raises(svc.Problem, match="Cancellation"):
            ops.cancel_operation(db, payload.key)
    monkeypatch.setattr(svc, "execute", original)
    ops.run_operation(company[0], payload.key)
    done = read_status(company, payload.key)
    assert done["committed"] and done["attempt_count"] == 2


def test_lost_commit_acknowledgement_recovers_actual_result(company, monkeypatch):
    class LostAcknowledgement(Session):
        def commit(self):
            lose = self.info.get("acting_username") == "token:worker"
            super().commit()
            if lose:
                raise OSError("synthetic lost acknowledgement")

    monkeypatch.setattr(
        ops,
        "worker_factory",
        lambda: sessionmaker(class_=LostAcknowledgement, autoflush=False),
    )
    payload, _, _ = admit(company)
    ops.run_operation(company[0], payload.key)
    done = read_status(company, payload.key)
    assert done["committed"] and done["error"] is None
    with company[1]() as db:
        assert db.query(m.Entity).count() == 1


def test_overlapping_workers_do_not_double_execute(company, monkeypatch):
    payload, _, _ = admit(company)
    started, finish = threading.Event(), threading.Event()
    original = svc.execute
    calls = []

    def held(*args, **kwargs):
        calls.append(1)
        started.set()
        assert finish.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(svc, "execute", held)
    with ThreadPoolExecutor(max_workers=2) as pool:
        one = pool.submit(ops.run_operation, company[0], payload.key)
        assert started.wait(5)
        two = pool.submit(ops.run_operation, company[0], payload.key)
        finish.set()
        one.result(timeout=10)
        two.result(timeout=10)
    assert len(calls) == 1
    assert read_status(company, payload.key)["committed"]


def test_atomic_batch_rejection_has_no_partial_children(company):
    data = {
        "operations": [
            {"operation": "create_entity", "data": {"code": "DUP", "name": "First"}},
            {
                "operation": "create_entity",
                "data": {"code": "DUP", "name": "Duplicate"},
            },
        ]
    }
    payload, _, _ = admit(company, operation="batch", data=data)
    ops.run_operation(company[0], payload.key)
    status = read_status(company, payload.key)
    assert status["execution_state"] == "rejected"
    assert status["completed_children"] == 0 and status["total_children"] == 2
    assert "Duplicate" not in json.dumps(status["error"])
    with company[1]() as db:
        assert db.query(m.Entity).count() == db.query(m.RequestRecord).count() == 0


@pytest.mark.parametrize(
    "change", ["revoked", "readonly", "company", "schema", "build"]
)
def test_queue_rechecks_authorization_and_accepted_connection(
    company, change, monkeypatch
):
    payload, _, _ = admit(company)
    with company[1]() as db:
        if change in ("revoked", "readonly"):
            token = db.get(ApiToken, company[2]["id"])
            if change == "revoked":
                token.is_active = False
            else:
                token.role = "readonly"
        elif change == "company":
            db.query(Settings).filter_by(key="company_session_id").one().value = (
                "another-company"
            )
        db.commit()
    if change == "schema":
        monkeypatch.setattr(routes, "commands", lambda: {"different": {}})
    if change == "build":
        monkeypatch.setitem(runtime.BUILD_IDENTITY, "source_tree_sha256", "e" * 64)
    ops.run_operation(company[0], payload.key)
    result = read_status(company, payload.key)
    assert result["execution_state"] == "rejected"
    with company[1]() as db:
        assert db.query(m.Entity).count() == 0


def test_admission_requires_current_company_and_real_principal(company):
    payload = runtime.CommandInput(
        operation="create_entity",
        key="x",
        data={"code": "A", "name": "A"},
        expected_connection={"company_session_id": "wrong"},
    )
    with company[1]() as db:
        with pytest.raises(svc.Problem, match="Company"):
            ops.admission(db, payload, company[2], "token:worker")
        with pytest.raises(svc.Problem, match="principal"):
            ops.admission(db, payload, {"kind": "token", "id": 999}, "invented")
        assert db.query(AgentOperation).count() == 0


def verification_fixture(company):
    payload, _, _ = admit(company)
    ops.run_operation(company[0], payload.key)
    with company[1]() as db:
        document = svc.execute(
            db,
            {
                "operation": "attach_document",
                "key": "proof",
                "data": {
                    "code": "proof",
                    "title": "Synthetic verification",
                    "locator": "/synthetic/verification-proof.json",
                    "sha256": "a" * 64,
                },
            },
        )
        db.commit()
        effect = runtime.operation_result(db.get(m.RequestRecord, payload.key).result)[
            "effect_digest"
        ]
    return {
        "request_key": payload.key,
        "effect_digest": effect,
        "scope": "source_and_effects",
        "status": "passed",
        "source_documents": [
            {"document_id": document["document_id"], "sha256": "a" * 64}
        ],
        "proof_reference": "synthetic:proof#result",
        "summary": "Synthetic reviewer checked source and accounting",
    }


def test_verification_is_separate_scoped_append_only_and_metadata_only(company):
    data = verification_fixture(company)
    with company[1]() as db:
        before_events = db.query(m.Event).count()
        before_revision = db.get(m.Entity, 1).revision
        one = svc.execute(
            db,
            {
                "operation": "record_operation_verification",
                "key": "verify/one",
                "data": data,
            },
            "synthetic-reviewer",
        )
        db.commit()
        assert db.query(m.Event).count() == before_events
        assert db.get(m.Entity, 1).revision == before_revision
        assert one["external_evidence_independently_checked"] is False
        replay = svc.execute(
            db,
            {
                "operation": "record_operation_verification",
                "key": "verify/one",
                "data": data,
            },
            "synthetic-reviewer",
        )
        assert replay["replayed"]
        with pytest.raises(svc.Problem, match="latest"):
            svc.execute(
                db,
                {
                    "operation": "record_operation_verification",
                    "key": "verify/stale",
                    "data": data,
                },
            )
        db.rollback()
        data.update(
            supersedes_id=one["verification_id"],
            status="failed",
            summary="Later contradiction discovered",
        )
        two = svc.execute(
            db,
            {
                "operation": "record_operation_verification",
                "key": "verify/two",
                "data": data,
            },
        )
        db.commit()
        status = ops.operation_status(db, data["request_key"])
        assert status["committed"]
        assert len(status["verification"]["history"]) == 2
        assert [row["id"] for row in status["verification"]["latest_attestations"]] == [
            two["verification_id"]
        ]
        assert status["verification"]["latest_attestations"][0]["status"] == "failed"
        assert (
            routes.request_result(data["request_key"], db=db)["result"]["verification"]
            == status["verification"]
        )
    for sql in (
        "UPDATE re_operation_verifications SET summary='altered'",
        "DELETE FROM re_operation_verifications",
        "INSERT OR REPLACE INTO re_operation_verifications SELECT * FROM re_operation_verifications WHERE id=1",
    ):
        with company[1]() as db:
            with pytest.raises(IntegrityError, match="immutable"):
                db.execute(text(sql))


@pytest.mark.parametrize(
    "field,value",
    [
        ("effect_digest", "b" * 64),
        ("source_documents", [{"document_id": 1, "sha256": "c" * 64}]),
        ("supersedes_id", 999),
    ],
)
def test_verification_rejects_wrong_effect_source_or_supersession(
    company, field, value
):
    data = verification_fixture(company)
    data[field] = value
    with company[1]() as db:
        with pytest.raises(svc.Problem):
            svc.execute(
                db,
                {
                    "operation": "record_operation_verification",
                    "key": "wrong-proof",
                    "data": data,
                },
            )
        db.rollback()
        assert db.query(OperationVerification).count() == 0


def test_missing_operation_is_unknown_not_proof_of_rollback(company):
    status = read_status(company, "missing")
    assert status["execution_state"] == "unknown"
    assert "does not prove" in status["recovery"]
    assert not status["resumable"] and not status["committed"]


def test_api_admission_commits_before_dispatch_and_uses_registered_identity(
    company, monkeypatch
):
    request = Request({"type": "http", "session": {}})
    request.state.token_principal = {"username": "token:worker", "role": "bookkeeper"}
    background = BackgroundTasks()
    payload = runtime.CommandInput(
        operation="create_entity",
        key="api/queued",
        data={"code": "API", "name": "API synthetic"},
    )
    dispatches = []
    monkeypatch.setattr(
        ops, "dispatch_operation", lambda bind, key: dispatches.append((bind, key))
    )
    with company[1]() as db:
        db.info["acting_username"] = "token:worker"
        result = routes.submit_operation(payload, request, background, db)
        assert result["execution_state"] == "accepted"
    with company[1]() as observer:
        saved = observer.get(AgentOperation, payload.key)
        assert saved.actor == "token:worker" and saved.authorization == company[2]
        assert observer.query(m.Entity).count() == 0
    assert not dispatches
    asyncio.run(background())
    assert dispatches == [(company[0], payload.key)]


def test_bounded_status_wait_releases_snapshot_for_worker(company):
    payload, _, _ = admit(company)

    def delayed():
        time.sleep(0.05)
        ops.run_operation(company[0], payload.key)

    with ThreadPoolExecutor(max_workers=1) as pool:
        worker = pool.submit(delayed)
        with company[1]() as db:
            result = routes.operation_status(payload.key, "compact", 2, db)
        worker.result(timeout=5)
    assert result["execution_state"] == "committed"
    assert result["projection"] == "compact"


def test_compact_status_declares_bounded_history_and_full_reference(company):
    payload, _, _ = admit(company, key="queued/special?key#part")
    with company[1]() as db:
        row = db.get(AgentOperation, payload.key)
        for _ in range(25):
            ops.transition(db, row, "running")
        db.commit()
    compact = read_status(company, payload.key, "compact")
    assert len(compact["history"]) == 20 and compact["history_count"] == 26
    assert compact["history_truncated"] is True
    assert "%3F" in compact["full_result_ref"] and "%23" in compact["full_result_ref"]


def test_admitted_identity_is_immutable_at_database_boundary(company):
    admit(company)
    for sql in (
        "UPDATE re_agent_operations SET request='{}'",
        "DELETE FROM re_agent_operations",
        "INSERT OR REPLACE INTO re_agent_operations SELECT * FROM re_agent_operations",
    ):
        with company[1]() as db:
            with pytest.raises(IntegrityError, match="immutable"):
                db.execute(text(sql))


def test_administrative_batch_child_never_executes(company):
    payload, _, _ = admit(
        company,
        operation="batch",
        data={
            "operations": [
                {"operation": "create_entity", "data": {"code": "NO", "name": "No"}},
                {
                    "operation": "close_period",
                    "data": {
                        "entity_id": 1,
                        "through": "2026-01-31",
                        "reason": "Must reject",
                    },
                },
            ]
        },
    )
    ops.run_operation(company[0], payload.key)
    result = read_status(company, payload.key)
    assert (
        result["execution_state"] == "rejected"
        and result["error"]["code"] == "batch_operation"
    )
    with company[1]() as db:
        assert db.query(m.Entity).count() == 0


def test_cancellation_records_actual_actor_without_changing_accepted_identity(company):
    payload, _, _ = admit(company)
    request = Request({"type": "http", "session": {}})
    request.state.token_principal = {
        "username": "token:reviewer",
        "role": "bookkeeper",
    }
    with company[1]() as db:
        db.add(
            ApiToken(
                label="reviewer",
                token_hash="e" * 64,
                token_hint="synthetic reviewer",
                role="bookkeeper",
                is_active=True,
            )
        )
        db.commit()
        db.info["acting_username"] = "token:reviewer"
        result = routes.cancel_operation(
            routes.OperationKeyInput(key=payload.key), request, db
        )
        assert [(row["status"], row["actor"]) for row in result["history"]] == [
            ("accepted", "token:worker"),
            ("cancelled", "token:reviewer"),
        ]
        accepted = db.get(AgentOperation, payload.key)
        assert accepted.actor == "token:worker"
        assert accepted.authorization == company[2]
        assert accepted.request["data"] == payload.data
        # A repeated cancellation is a read of the same terminal outcome.
        assert ops.cancel_operation(db, payload.key, "other-reviewer") == result
    ops.run_operation(company[0], payload.key)
    with company[1]() as db:
        assert db.query(m.RequestRecord).count() == db.query(m.Entity).count() == 0


def test_agent_workflow_migration_is_repeatable_and_preserves_nonempty_history(
    company, monkeypatch
):
    import importlib
    from sqlalchemy import inspect, select, func

    payload, _, _ = admit(company)
    migration = importlib.import_module(
        "migrations.versions.bb0004_agent_and_card_workflows"
    )
    with company[1]() as db:
        connection = db.connection()
        monkeypatch.setattr(migration.op, "get_bind", lambda: connection)
        tables = migration.tables()
        before = {
            table.name: connection.execute(
                select(func.count()).select_from(table)
            ).scalar()
            for table in tables
        }
        migration.upgrade()
        migration.upgrade()
        with pytest.raises(RuntimeError, match="cannot be discarded"):
            migration.downgrade()
        assert {table.name for table in tables} <= set(
            inspect(connection).get_table_names()
        )
        assert {
            table.name: connection.execute(
                select(func.count()).select_from(table)
            ).scalar()
            for table in tables
        } == before
        assert ops.operation_status(db, payload.key)["execution_state"] == "accepted"
        with pytest.raises(IntegrityError, match="immutable"):
            connection.execute(text("UPDATE re_agent_operations SET actor='changed'"))


def test_agent_workflow_migration_can_roundtrip_empty_tables(company, monkeypatch):
    import importlib
    from sqlalchemy import inspect

    migration = importlib.import_module(
        "migrations.versions.bb0004_agent_and_card_workflows"
    )
    with company[1]() as db:
        connection = db.connection()
        monkeypatch.setattr(migration.op, "get_bind", lambda: connection)
        names = {table.name for table in migration.tables()}
        migration.downgrade()
        assert not names & set(inspect(connection).get_table_names())
        migration.upgrade()
        migration.upgrade()
        assert names <= set(inspect(connection).get_table_names())


def long_operation_history(company, count=250):
    data = verification_fixture(company)
    with company[1]() as db:
        original = ops.record_verification(
            db,
            ops.OperationVerificationInput(**{**data, "scope": "source_evidence"}),
            "reviewer",
        )
        previous = None
        operation = db.get(AgentOperation, data["request_key"])
        for _ in range(count):
            ops.transition(db, operation, "committed", {"proof": "x" * 4000})
            latest = ops.record_verification(
                db,
                ops.OperationVerificationInput(
                    **{
                        **data,
                        "scope": "accounting_effects",
                        "supersedes_id": previous,
                        "summary": "x" * 4000,
                    }
                ),
                "reviewer",
            )
            previous = latest["verification_id"]
        db.commit()
    return data, original["verification_id"], previous


def test_compact_poll_materializes_bounded_history_and_keeps_old_current_scope(company):
    from collections import Counter
    from sqlalchemy import event
    from app.portfolio.operation_models import OperationTransition

    data, original_id, latest_id = long_operation_history(company)
    full = read_status(company, data["request_key"])
    loaded = Counter()
    with company[1]() as db:
        event.listen(
            db,
            "loaded_as_persistent",
            lambda session, instance: loaded.update([type(instance)]),
        )
        compact = ops.operation_status(db, data["request_key"], "compact")
    assert loaded[OperationTransition] <= 21
    assert loaded[OperationVerification] <= 23
    assert compact["history"] == full["history"][-20:]
    assert compact["history_count"] == len(full["history"]) == 253
    assert compact["history_truncated"]
    verification = compact["verification"]
    assert verification["history"] == full["verification"]["history"][-20:]
    assert verification["history_count"] == 251 and verification["history_truncated"]
    assert (
        verification["latest_attestations"]
        == full["verification"]["latest_attestations"]
    )
    assert [row["id"] for row in verification["latest_attestations"]] == [
        original_id,
        latest_id,
    ]
    assert original_id not in {row["id"] for row in verification["history"]}
    assert compact["result"]["effect_digest"] == full["result"]["effect_digest"]
    assert compact["result"]["verification"] == verification


def test_compact_counts_windows_and_latest_attestations_share_one_snapshot(
    company, monkeypatch
):
    from app.portfolio.operation_models import OperationTransition

    data, original_id, latest_id = long_operation_history(company, count=25)
    original_reader = ops._history_rows
    inserted = []

    def concurrent_write(db, model, key, compact):
        result = original_reader(db, model, key, compact)
        if model is OperationTransition and not inserted:
            with company[1]() as writer:
                ops.transition(writer, writer.get(AgentOperation, key), "committed")
                amended = ops.record_verification(
                    writer,
                    ops.OperationVerificationInput(
                        **{
                            **data,
                            "scope": "source_evidence",
                            "status": "failed",
                            "supersedes_id": original_id,
                        }
                    ),
                    "later-reviewer",
                )
                writer.commit()
                inserted.append(amended["verification_id"])
        return result

    monkeypatch.setattr(ops, "_history_rows", concurrent_write)
    with company[1]() as db:
        prior = ops.operation_status(db, data["request_key"], "compact")
        assert prior["history_count"] == 28
        assert prior["verification"]["history_count"] == 26
        assert [row["id"] for row in prior["verification"]["latest_attestations"]] == [
            original_id,
            latest_id,
        ]
        db.rollback()
        current = ops.operation_status(db, data["request_key"], "compact")
        assert current["history_count"] == 29
        assert current["verification"]["history_count"] == 27
        assert [
            row["id"] for row in current["verification"]["latest_attestations"]
        ] == [latest_id, inserted[0]]
        assert current["verification"]["latest_attestations"][-1]["status"] == "failed"
