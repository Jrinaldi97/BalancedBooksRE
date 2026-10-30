"""Bounded, revision-bound audit export. A changed book requires a new export."""

from sqlalchemy import or_, exists, select, func
from app.portfolio import models as m
from app.portfolio.observation_state import observation_states
from app.portfolio.reports import read_snapshot
from app.portfolio.service import digest, fail


def verification_export(db, after_id=0, limit=100, expected_book_digest=None):
    if (
        type(after_id) is not int
        or after_id < 0
        or type(limit) is not int
        or not 1 <= limit <= 200
    ):
        fail("export_bounds", "Use a nonnegative cursor and a page size from 1 to 200")
    read_snapshot(db)
    revision = digest(
        [(e.id, e.revision) for e in db.query(m.Entity).order_by(m.Entity.id)]
    )
    if (after_id and not expected_book_digest) or (
        expected_book_digest is not None and expected_book_digest != revision
    ):
        fail(
            "stale_export",
            "Book changed or cursor lacks its revision; restart the export",
        )
    ids = [
        i
        for (i,) in db.query(m.BankObservation.id)
        .filter(m.BankObservation.id > after_id)
        .order_by(m.BankObservation.id)
        .limit(limit + 1)
    ]
    more = len(ids) > limit
    ids = ids[:limit]
    states = observation_states(db, ids) if ids else []
    event_ids = {ident for state in states for ident in state["event_ids"]}
    review_ids = {r["id"] for state in states for r in state["reviews"]}
    references = []
    if event_ids or review_ids:
        linked = func.json_each(m.RequestRecord.result, "$.event_ids").table_valued(
            "value"
        )
        for receipt in (
            db.query(m.RequestRecord)
            .filter(
                or_(
                    m.RequestRecord.result["event_id"].as_integer().in_(event_ids),
                    m.RequestRecord.result["validated_event_id"]
                    .as_integer()
                    .in_(event_ids),
                    m.RequestRecord.result["review_id"].as_integer().in_(review_ids),
                    exists(
                        select(1)
                        .select_from(linked)
                        .where(linked.c.value.in_(event_ids))
                    ),
                )
            )
            .order_by(m.RequestRecord.key)
        ):
            references.append(
                {
                    "key": receipt.key,
                    "effect_digest": digest(
                        {k: v for k, v in receipt.result.items() if k != "replayed"}
                    ),
                    "operation": receipt.operation,
                    "event_ids": receipt.result.get(
                        "event_ids",
                        [
                            receipt.result.get(
                                "event_id", receipt.result.get("validated_event_id")
                            )
                        ],
                    ),
                    "review_id": receipt.result.get("review_id"),
                }
            )
    rows = [
        {
            "observation_id": row["observation"]["id"],
            "state_digest": row["state_digest"],
            "event_ids": row["event_ids"],
            "suspense_residuals": row["suspense_residuals"],
            "reviews": row["reviews"],
            "receipt_references": [
                ref
                for ref in references
                if set(ref["event_ids"]) & set(row["event_ids"])
                or ref["review_id"] in {r["id"] for r in row["reviews"]}
            ],
        }
        for row in states
    ]
    return {
        "book_digest": revision,
        "rows": rows,
        "next_after_id": ids[-1] if more else None,
        "complete": not more,
        "count": len(rows),
        "verification_scope": "Application state only; not independent source verification",
    }
