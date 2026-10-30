"""Compact registers and canonical continuation state with recoverable full proofs."""

import hashlib
import json
from urllib.parse import urlencode

from app.portfolio import models as m
from app.portfolio.reports import read_snapshot
from app.portfolio.service import digest, fail, get


def book_digest(db):
    return digest(list(db.query(m.Entity.id, m.Entity.revision).order_by(m.Entity.id)))


def review_register(
    db, after_id=0, limit=100, expected_book_digest=None, status=None, code=None
):
    if (
        type(after_id) is not int
        or after_id < 0
        or type(limit) is not int
        or not 1 <= limit <= 200
    ):
        fail("export_bounds", "Choose a nonnegative cursor and 1 to 200 reviews")
    read_snapshot(db)
    revision = book_digest(db)
    if (after_id and not expected_book_digest) or (
        expected_book_digest and expected_book_digest != revision
    ):
        fail("stale_export", "Restart the register at the current book revision")
    query = db.query(m.ReviewItem).filter(m.ReviewItem.id > after_id)
    if status:
        query = query.filter(m.ReviewItem.status == status)
    if code:
        query = query.filter(m.ReviewItem.code == code)
    records = query.order_by(m.ReviewItem.id).limit(limit + 1).all()
    more = len(records) > limit
    rows = []
    for row in records[:limit]:
        raw = row.resolution or ""
        reason = None
        try:
            decoded = json.loads(raw)
            if isinstance(decoded, dict):
                reason = decoded.get("reason") or decoded.get("method")
        except (ValueError, TypeError):
            pass
        rows.append(
            {
                "review_id": row.id,
                "code": row.code,
                "status": row.status,
                "entity_id": row.entity_id,
                "observation_id": row.observation_id,
                "event_id": row.event_id,
                "resolved_event_id": row.resolved_event_id,
                "reason": str(reason)[:200] if reason else None,
                "detail_excerpt": row.detail[:240],
                "excerpt_only": True,
                "resolution_bytes": len(raw.encode()),
                "resolution_sha256": hashlib.sha256(raw.encode()).hexdigest(),
                "full_proof_ref": "/api/portfolio/proof?"
                + urlencode({"kind": "review", "identity": str(row.id)}),
                "source_verification": "Not inferred from database closure",
            }
        )
    return {
        "book_digest": revision,
        "rows": rows,
        "complete": not more,
        "next_after_id": rows[-1]["review_id"] if more else None,
    }


def proof_page(db, kind, identity, offset=0, limit=8192, expected_digest=None):
    if (
        type(offset) is not int
        or offset < 0
        or type(limit) is not int
        or not 1 <= limit <= 32768
    ):
        fail(
            "proof_bounds",
            "Use nonnegative character offsets and at most 32768 characters",
        )
    read_snapshot(db)
    if kind == "request":
        raw = json.dumps(
            get(db, m.RequestRecord, identity).result,
            sort_keys=True,
            separators=(",", ":"),
        )
    elif kind in {"proposal", "review"}:
        try:
            ident = int(identity)
        except (TypeError, ValueError):
            fail("proof_identity", "Use a numeric proposal or review ID")
        if kind == "proposal":
            raw = json.dumps(
                get(db, m.Proposal, ident).preview,
                sort_keys=True,
                separators=(",", ":"),
            )
        else:
            raw = get(db, m.ReviewItem, ident).resolution or ""
    else:
        fail("proof_kind", "Choose request, proposal or review proof")
    proof_digest = hashlib.sha256(raw.encode()).hexdigest()
    if (offset and not expected_digest) or (
        expected_digest and expected_digest != proof_digest
    ):
        fail(
            "stale_proof",
            "The proof changed or the cursor is unbound; start from offset zero",
        )
    if offset > len(raw):
        fail("proof_bounds", "Offset exceeds the proof length")
    end = min(len(raw), offset + limit)
    return {
        "kind": kind,
        "identity": identity,
        "sha256": proof_digest,
        "total_chars": len(raw),
        "total_bytes": len(raw.encode()),
        "offset": offset,
        "text": raw[offset:end],
        "next_offset": end if end < len(raw) else None,
        "complete": end == len(raw),
        "encoding": "UTF-8; offsets count Unicode characters",
    }


def run_manifest(db, keys):
    from app.portfolio.operations import operation_status, verification_state

    if (
        not isinstance(keys, list)
        or not 1 <= len(keys) <= 200
        or any(not isinstance(key, str) or not 1 <= len(key) <= 120 for key in keys)
        or len(set(keys)) != len(keys)
    ):
        fail("manifest_keys", "Choose 1 to 200 unique exact operation keys")
    read_snapshot(db)
    revision = book_digest(db)
    current_revisions = {str(e.id): e.revision for e in db.query(m.Entity)}
    receipts = {
        row.key: row
        for row in db.query(m.RequestRecord).filter(m.RequestRecord.key.in_(keys))
    }
    proposals = {key: [] for key in keys}
    for row in (
        db.query(m.Proposal)
        .filter(m.Proposal.request["key"].as_string().in_(keys))
        .order_by(m.Proposal.id)
    ):
        proposals[row.request["key"]].append(
            {
                "proposal_id": row.id,
                "digest": row.digest,
                "state": (
                    "committed"
                    if row.committed_result
                    else "stale" if row.revisions != current_revisions else "prepared"
                ),
            }
        )
    rows = []
    for key in keys:
        receipt = receipts.get(key)
        if receipt:
            verification = verification_state(db, key, "compact")
            review_ids = set(
                receipt.result.get("review_lifecycle", {}).get(
                    "preexisting_closed_ids", []
                )
            ) | set(receipt.result.get("resolved_review_ids", []))
            rows.append(
                {
                    "key": key,
                    "state": "committed",
                    "body_digest": receipt.digest,
                    "effect_digest": digest(
                        {k: v for k, v in receipt.result.items() if k != "replayed"}
                    ),
                    "operation": receipt.operation,
                    "proposals": proposals[key],
                    "review_states": [
                        {
                            "review_id": row.id,
                            "status": row.status,
                            "resolved_event_id": row.resolved_event_id,
                        }
                        for row in db.query(m.ReviewItem)
                        .filter(m.ReviewItem.id.in_(review_ids))
                        .order_by(m.ReviewItem.id)
                    ],
                    "attestations": [
                        {
                            field: entry.get(field)
                            for field in (
                                "id",
                                "scope",
                                "status",
                                "effect_digest",
                                "proof_reference",
                                "source_documents",
                            )
                        }
                        for entry in verification["latest_attestations"]
                    ],
                    "full_result_ref": "/api/portfolio/requests?"
                    + urlencode({"key": key}),
                }
            )
        else:
            durable = db.get(m.AgentOperation, key)
            status = operation_status(db, key, "compact") if durable else None
            rows.append(
                {
                    "key": key,
                    "state": (
                        status["execution_state"]
                        if status
                        else (
                            proposals[key][-1]["state"]
                            if proposals[key]
                            else "not_found"
                        )
                    ),
                    "body_digest": durable.digest if durable else None,
                    "proposals": proposals[key],
                    "durable_status": status,
                }
            )
    return {
        "manifest_version": 1,
        "book_digest": revision,
        "operations": rows,
        "authority": "Current native receipts and review states supersede intermediate prepared notes. Missing receipts do not prove an in-flight operation stopped.",
        "verification_scope": "Reviewer attestations are separate from execution and are not automatic external evidence certification.",
    }
