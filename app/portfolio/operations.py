"""Recoverable accepted work; accounting still commits once through execute().

Admission is a separate short transaction. A background worker claims the saved
identity, then executes the entire command under the usual writer transaction.
After a process interruption clients explicitly resume the same key. Competing
attempts recheck the claim under the writer lock and cannot duplicate accounting.
"""

import json
import uuid
from functools import lru_cache
import logging
from concurrent.futures import ThreadPoolExecutor
from sqlalchemy.orm import sessionmaker
from pydantic import Field
from typing import Literal
from app.portfolio import models as m, schemas as s, runtime
from app.portfolio.operation_models import (
    AgentOperation,
    OperationTransition,
    OperationVerification,
)


def transition(db, row, status, detail=None, *, actor=None):
    row.status = status
    db.add(
        OperationTransition(
            key=row.key,
            status=status,
            attempt_id=row.attempt_id,
            actor=row.actor if actor is None else actor,
            detail=detail or {},
        )
    )


def authorize(db, authority, operation):
    from app.portfolio.service import fail
    from app.models.api_tokens import ApiToken
    from app.models.users import User

    kind = authority.get("kind")
    if kind == "token":
        principal = (
            db.query(ApiToken).filter_by(id=authority["id"], is_active=True).first()
        )
    elif kind == "session":
        principal = db.query(User).filter_by(id=authority["id"], is_active=True).first()
    else:
        principal = None
    if principal is None or principal.role not in ("admin", "bookkeeper"):
        fail(
            "operation_authorization",
            "The accepting principal is inactive or no longer permitted to write",
        )
    if operation in ("close_period", "reopen_period") and principal.role != "admin":
        fail("operation_authorization", "Period controls require an administrator")


def admission(db, payload, authority, actor):
    from app.portfolio import service as svc
    from app.portfolio.routes import commands

    if db.get_bind().dialect.name != "sqlite":
        svc.fail(
            "operation_database",
            "Durable background execution currently supports local SQLite companies",
        )
    data = payload.model_dump(mode="json", exclude={"expected_connection"})
    if len(json.dumps(data).encode()) > 4 * 1024 * 1024:
        svc.fail("operation_size", "Asynchronous requests are limited to 4 MiB")
    if data["operation"] not in s.CONTRACTS:
        svc.fail("unsupported_operation", "Unknown portfolio operation")
    # Validate before admitting, while retaining full business revalidation in execute.
    s.CONTRACTS[data["operation"]].model_validate(data["data"])
    authorize(db, authority, data["operation"])
    request_digest = svc.digest({k: v for k, v in data.items() if k != "key"})
    row = db.get(AgentOperation, payload.key)
    old = db.get(m.RequestRecord, payload.key)
    if (row and row.digest != request_digest) or (old and old.digest != request_digest):
        svc.fail(
            "idempotency_conflict", "This retry key was used for different contents"
        )
    runtime.check_connection(
        db, payload.expected_connection, lambda: svc.digest(commands())
    )
    if old or row:
        return operation_status(db, payload.key), False
    binding = {
        "company_session_id": runtime.company_session_id(db),
        "command_schema_digest": svc.digest(commands()),
        **{
            key: runtime.BUILD_IDENTITY[key]
            for key in ("source_tree_sha256", "executable_sha256")
            if runtime.BUILD_IDENTITY[key]
        },
    }
    if not binding["company_session_id"]:
        svc.fail(
            "company_identity_unavailable",
            "Open the authenticated company before submitting durable work",
        )
    if (
        db.query(AgentOperation)
        .filter(AgentOperation.status.in_(("accepted", "running")))
        .count()
        >= 200
    ):
        svc.fail(
            "operation_capacity",
            "Recover or finish outstanding operations before submitting more",
        )
    row = AgentOperation(
        key=payload.key,
        digest=request_digest,
        request=data,
        binding=binding,
        authorization=authority,
        actor=actor,
        status="accepted",
    )
    db.add(row)
    db.flush()
    transition(db, row, "accepted")
    db.flush()
    return operation_status(db, payload.key), True


def guard_reserved_request(db, command, request_digest):
    """Shared with direct commands and every batch child; one key namespace."""
    from app.portfolio.service import fail

    row = db.get(AgentOperation, command.key)
    if row is None:
        return
    if row.digest != request_digest:
        fail(
            "idempotency_conflict",
            "This key is reserved for different accepted contents",
        )
    if row.status in ("cancelled", "rejected"):
        fail(
            "operation_terminal",
            "This accepted request was cancelled or rejected; inspect its durable status",
        )
    # Direct same-key recovery must retain the accepted identity and permissions.
    from app.portfolio.routes import commands
    from app.portfolio.service import digest

    runtime.check_connection(
        db,
        runtime.ConnectionExpectation.model_validate(row.binding),
        lambda: digest(commands()),
    )
    authorize(db, row.authorization, command.operation)


def mark_committed(db, key):
    row = db.get(AgentOperation, key)
    if row is not None and row.status != "committed":
        row.error = None
        transition(db, row, "committed")


@lru_cache(maxsize=1)
def worker_factory():
    from app.services.audit import register_audit_hooks

    factory = sessionmaker(autoflush=False)
    register_audit_hooks(factory)
    return factory


@lru_cache(maxsize=1)
def worker_executor():
    # One local writer keeps waiting jobs out of the HTTP thread pool.
    return ThreadPoolExecutor(max_workers=1, thread_name_prefix="portfolio-writer")


def dispatch_operation(bind, key):
    future = worker_executor().submit(run_operation, bind, key)

    def completed(job):
        if not job.cancelled() and job.exception() is not None:
            logging.getLogger(__name__).error(
                "Durable worker stopped; inspect operation_status and resume the same key (%s)",
                type(job.exception()).__name__,
            )

    future.add_done_callback(completed)


def run_operation(bind, key):
    """No token secret, HTTP client or arbitrary user function crosses this boundary."""
    from app.portfolio import service as svc
    from app.portfolio.routes import serialize_write

    factory = worker_factory()
    attempt = uuid.uuid4().hex
    with factory(bind=bind) as db:
        serialize_write(db)
        row = db.get(AgentOperation, key)
        if row is None or row.status in ("committed", "rejected", "cancelled"):
            db.rollback()
            return
        # Running can be reclaimed explicitly. A former claimant must check this
        # value under the same writer lock before doing any accounting.
        row.attempt_id = attempt
        row.attempt_count += 1
        transition(db, row, "running")
        db.commit()
    try:
        with factory(bind=bind) as db:
            serialize_write(db)
            row = db.get(AgentOperation, key)
            if row.attempt_id != attempt or row.status != "running":
                db.rollback()
                return
            db.info["acting_username"] = row.actor
            svc.execute(db, row.request, row.actor)
            db.commit()
    except Exception as exc:
        # A lost commit acknowledgement can still have committed. Read the
        # request record first; if recovery itself fails, keep running/unknown.
        logging.getLogger(__name__).error(
            "Durable operation execution failed (%s)", type(exc).__name__
        )
        with factory(bind=bind) as db:
            serialize_write(db)
            row = db.get(AgentOperation, key)
            if db.get(m.RequestRecord, key) is not None:
                mark_committed(db, key)
            elif row and row.attempt_id == attempt and row.status == "running":
                detail = (
                    {"code": exc.code, "message": str(exc)}
                    if isinstance(exc, svc.Problem)
                    else {
                        "code": "execution_failed",
                        "message": "The transaction rolled back. Inspect the local application log before preparing revised work.",
                    }
                )
                row.error = detail
                transition(db, row, "rejected", detail)
            db.commit()


def cancel_operation(db, key, actor="operator"):
    from app.portfolio.service import fail

    row = db.get(AgentOperation, key)
    if row is None:
        fail("not_found", "No accepted operation has this key")
    if row.status == "cancelled":
        return operation_status(db, key)
    if row.status != "accepted" or db.get(m.RequestRecord, key):
        fail(
            "cancellation_unsafe",
            "Cancellation is supported only before a worker claims the operation",
        )
    transition(db, row, "cancelled", actor=actor)
    db.flush()
    return operation_status(db, key)


def verification_state(db, key):
    from app.portfolio.service import values

    rows = (
        db.query(OperationVerification)
        .filter_by(key=key)
        .order_by(OperationVerification.id)
        .all()
    )
    superseded = {row.supersedes_id for row in rows}
    return {
        **runtime.empty_verification(),
        "external_evidence": "recorded" if rows else "not_recorded",
        "latest_attestations": [
            values(row) for row in rows if row.id not in superseded
        ],
        "history": [values(row) for row in rows],
    }


def operation_status(db, key, projection="full"):
    from app.portfolio.service import values
    from app.portfolio.reports import read_snapshot

    read_snapshot(db)
    row = db.get(AgentOperation, key)
    record = db.get(m.RequestRecord, key)
    state = "committed" if record else (row.status if row else "unknown")
    total = (
        (
            len(row.request["data"]["operations"])
            if row.request["operation"] == "batch"
            else 1
        )
        if row
        else (record.result.get("operation_count", 1) if record else None)
    )
    result = {
        "key": key,
        "execution_state": state,
        "committed": record is not None,
        "operation": (
            record.operation if record else (row.request["operation"] if row else None)
        ),
        "accepted": row is not None,
        "attempt_count": row.attempt_count if row else 0,
        "completed_children": total if record else 0,
        "total_children": total if row or record else None,
        "atomic": True,
        "cancellable": state == "accepted",
        "resumable": state in ("accepted", "running"),
        "recovery": (
            "Running is the last durable stage, not proof that a worker is still alive. Resume this same key to safely reclaim interrupted work. No financial child is committed before the atomic command commits."
            if state in ("accepted", "running")
            else (
                "No accepted or committed result is visible; this does not prove an untracked synchronous request stopped."
                if state == "unknown"
                else None
            )
        ),
        "error": row.error if row else None,
        "verification": verification_state(db, key),
        "history": [
            values(item)
            for item in db.query(OperationTransition)
            .filter_by(key=key)
            .order_by(OperationTransition.id)
        ],
    }
    if record:
        result["result"] = runtime.operation_result(
            record.result, projection=projection
        )
        result["result"]["verification"] = result["verification"]
    if projection == "compact":
        from urllib.parse import urlencode

        result["history_count"] = len(result["history"])
        result["history"] = result["history"][-20:]
        result["history_truncated"] = result["history_count"] > 20
        result["verification"]["history_count"] = len(result["verification"]["history"])
        result["verification"]["history"] = result["verification"]["history"][-20:]
        result["verification"]["history_truncated"] = (
            result["verification"]["history_count"] > 20
        )
        result["projection"] = "compact"
        result["full_result_ref"] = "/api/portfolio/operations?" + urlencode(
            {"key": key}
        )
    return result


class VerificationSource(s.Input):
    document_id: s.Id
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class OperationVerificationInput(s.Input):
    request_key: str = Field(min_length=1, max_length=120)
    effect_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    scope: Literal["source_evidence", "accounting_effects", "source_and_effects"]
    status: Literal["pending", "passed", "failed"]
    source_documents: list[VerificationSource] = Field(min_length=1, max_length=100)
    proof_reference: str = Field(min_length=1, max_length=2000)
    summary: str = Field(min_length=1, max_length=4000)
    supersedes_id: s.Id | None = None


def record_verification(db, data, actor):
    from app.portfolio import service as svc

    record = svc.get(db, m.RequestRecord, data.request_key)
    effect = runtime.operation_result(record.result)["effect_digest"]
    if effect != data.effect_digest:
        svc.fail(
            "verification_effect",
            "The attestation must bind the exact committed effects",
        )
    previous = (
        db.query(OperationVerification)
        .filter_by(key=data.request_key, scope=data.scope)
        .order_by(OperationVerification.id.desc())
        .first()
    )
    if (previous.id if previous else None) != data.supersedes_id:
        svc.fail(
            "verification_stale",
            "Read the latest attestation before superseding this scope",
        )
    identities = [
        (source.document_id, source.sha256) for source in data.source_documents
    ]
    if len(set(identities)) != len(identities):
        svc.fail("verification_source", "Duplicate evidence identities are not allowed")
    for source in data.source_documents:
        svc.get(db, m.Document, source.document_id)
        if (
            not db.query(m.DocumentLocation)
            .filter_by(document_id=source.document_id, sha256=source.sha256)
            .first()
        ):
            svc.fail(
                "verification_source",
                "A registered immutable document location must match each source hash",
            )
    row = OperationVerification(
        key=data.request_key,
        effect_digest=effect,
        scope=data.scope,
        status=data.status,
        source_documents=[source.model_dump() for source in data.source_documents],
        proof_reference=data.proof_reference,
        summary=data.summary,
        supersedes_id=data.supersedes_id,
        actor=actor,
    )
    db.add(row)
    db.flush()
    return {
        "verification_id": row.id,
        "verified_request_key": row.key,
        "declared_status": row.status,
        "financial_events_created": 0,
        "external_evidence_independently_checked": False,
    }
