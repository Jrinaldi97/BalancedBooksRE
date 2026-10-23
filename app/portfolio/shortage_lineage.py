"""Identify physical restoration receipts across immutable classification history."""

import json
from datetime import date
from sqlalchemy import or_, exists, select, func
from app.portfolio import models as m
from app.portfolio.reports import ledger
from app.models.transactions import TransactionLine


def restoration_receipt(db, assessment, selected):
    from app.portfolio.service import get, fail, cents, digest, scope

    account_id, fund_id = assessment["account_id"], assessment["fund_id"]
    source = selected.source or {}
    obs_id = source.get("observation_id")
    obs = db.get(m.BankObservation, obs_id) if type(obs_id) is int else None
    if (
        assessment["effective_historical_balance_cents"] >= 0
        or not obs
        or obs.status != "posted"
        or obs.amount_cents <= 0
        or obs.account_id != account_id
        or obs.date != selected.date
        or selected.date <= date.fromisoformat(assessment["alert_date"])
        or not obs.event_id
    ):
        fail(
            "restoration_evidence",
            "Identify a later original receipt in the exact shortage scope",
        )
    linked_ids = func.json_each(m.Event.source, "$.observation_ids").table_valued(
        "value"
    )
    linked = exists(
        select(1).select_from(linked_ids).where(linked_ids.c.value == obs.id)
    )
    roots = (
        db.query(m.Event)
        .filter(
            or_(
                m.Event.source["observation_id"].as_integer() == obs.id,
                m.Event.id == obs.event_id,
                linked,
            )
        )
        .all()
    )
    events = {e.id: e for e in roots}
    frontier = set(events)
    while frontier:
        children = db.query(m.Event).filter(m.Event.corrects_id.in_(frontier)).all()
        frontier = {e.id for e in children} - events.keys()
        events.update({e.id: e for e in children})
    for event in events.values():
        metadata = event.source or {}
        if (
            event.date != obs.date
            or metadata.get("observation_id", obs.id) != obs.id
            or any(i != obs.id for i in metadata.get("observation_ids", []))
            or any(i not in events for i in metadata.get("source_event_ids", []))
            or (event.corrects_id is not None and event.corrects_id not in events)
        ):
            fail(
                "restoration_lineage",
                "Receipt ancestry includes another source, date or observation",
            )
    # Query all immutable physical bank rows, before any effective attribution.
    physical = (
        db.query(TransactionLine, m.JournalScope)
        .join(
            m.JournalScope,
            m.JournalScope.transaction_id == TransactionLine.transaction_id,
        )
        .join(m.AccountScope, m.AccountScope.account_id == TransactionLine.account_id)
        .filter(m.JournalScope.event_id.in_(events), m.AccountScope.role == "bank")
        .all()
    )
    originals = [
        (line, journal) for line, journal in physical if journal.event_id == selected.id
    ]
    if (
        len(originals) != 1
        or originals[0][0].account_id != account_id
        or cents(originals[0][0].debit) - cents(originals[0][0].credit)
        != obs.amount_cents
    ):
        fail(
            "restoration_evidence",
            "Select the unique original physical receipt, not a later attribution or classification",
        )
    # A new positive source under the same observation would be duplicate funding.
    # Cashless classification descendants are supported; gross attribution needs
    # the allocation workflow instead of being treated as another receipt.
    if any(j.event_id != selected.id for _, j in physical):
        fail("restoration_lineage", "Receipt ancestry has additional bank movements")
    classification = get(db, m.Event, obs.event_id)
    if classification.id != selected.id:
        descendants = {selected.id}
        while True:
            expanded = descendants | {
                e.id for e in events.values() if e.corrects_id in descendants
            }
            if expanded == descendants:
                break
            descendants = expanded
        explicit_links = (
            db.query(m.CashClassification)
            .filter_by(
                line_id=originals[0][0].id, event_id=classification.id, date=obs.date
            )
            .all()
        )
        if classification.id not in descendants:
            # match_transfer originally changed the pointer without corrects_id.
            # Its immutable per-line classification is the explicit linking edge.
            pair_id = (classification.source or {}).get("paired_observation_id")
            pair = db.get(m.BankObservation, pair_id) if type(pair_id) is int else None
            peer = db.get(m.Event, pair.event_id) if pair and pair.event_id else None
            holder = scope(db, obs.account_id)
            pair_holder = scope(db, pair.account_id) if pair else None
            if (
                classification.kind != "transfer"
                or len(explicit_links) != 1
                or (classification.source or {}).get("purpose")
                not in {"internal", "advance", "repayment"}
                or not pair
                or pair.id == obs.id
                or pair.account_id == obs.account_id
                or pair.status != "posted"
                or pair.amount_cents != -obs.amount_cents
                or not peer
                or peer.kind != "transfer"
                or (peer.source or {}).get("observation_id") != pair.id
                or (peer.source or {}).get("paired_observation_id") != obs.id
                or (peer.source or {}).get("purpose")
                != (classification.source or {}).get("purpose")
                or holder.role != "bank"
                or pair_holder.role != "bank"
                or (classification.source or {}).get("counterparty_entity_id")
                != pair_holder.entity_id
                or (peer.source or {}).get("counterparty_entity_id") != holder.entity_id
                or (
                    (holder.entity_id == pair_holder.entity_id)
                    != ((classification.source or {}).get("purpose") == "internal")
                )
            ):
                fail(
                    "restoration_lineage",
                    "The current classification has no verified reciprocal receipt ancestry",
                )
        permitted = descendants | {classification.id}
        if set(events) - permitted:
            fail(
                "restoration_lineage",
                "Unrelated receipt classifications make the source ambiguous",
            )
    elif any(e.id != selected.id and e.corrects_id is None for e in events.values()):
        fail(
            "restoration_lineage", "Multiple unrelated originals claim the same receipt"
        )
    rows = ledger(db, obs.date, account_id=account_id)
    scoped = [r for r in rows if fund_id is None or r["fund_id"] == fund_id]
    incoming = sum(
        r["debit_cents"] - r["credit_cents"]
        for r in scoped
        if r["line_id"] == originals[0][0].id
    )
    if incoming != obs.amount_cents:
        fail(
            "restoration_evidence",
            "The whole physical receipt must belong to the original shortage scope",
        )
    closing = sum(r["debit_cents"] - r["credit_cents"] for r in scoped)
    before = closing - incoming
    if before >= 0 or closing < 0:
        fail(
            "unresolved_shortage",
            "The identified receipt does not itself restore this shortfall",
        )
    # An alert cannot borrow a receipt from a later, separate deficit episode.
    daily = {}
    for row in scoped:
        daily[row["date"]] = (
            daily.get(row["date"], 0) + row["debit_cents"] - row["credit_cents"]
        )
    running = 0
    episode_start = None
    alert_date = assessment["alert_date"]
    for day in sorted(daily):
        if day == obs.date.isoformat():
            break
        prior = running
        running += daily[day]
        if running < 0 and prior >= 0:
            episode_start = day
        if running >= 0:
            episode_start = None
            if day >= alert_date:
                fail(
                    "restoration_episode",
                    "The original deficit ended before this receipt",
                )
    if episode_start is None or episode_start > alert_date:
        fail(
            "restoration_episode",
            "The receipt does not close the original dated deficit episode",
        )
    proof = {
        "observation_id": obs.id,
        "physical_event_id": selected.id,
        "physical_line_id": originals[0][0].id,
        "classification_event_id": classification.id,
        "amount_cents": incoming,
        "account_id": account_id,
        "fund_id": fund_id,
        "deficit_start": episode_start,
        "restoration_date": obs.date.isoformat(),
        "balance_before_receipt_cents": before,
        "balance_after_receipt_cents": closing,
    }
    proof["episode_id"] = digest(
        {
            k: proof[k]
            for k in (
                "observation_id",
                "account_id",
                "fund_id",
                "deficit_start",
                "restoration_date",
            )
        }
    )
    shared = []
    for review in db.query(m.ReviewItem).filter_by(
        code="actual_shortage", status="resolved"
    ):
        try:
            prior = json.loads(review.resolution or "{}")
        except (ValueError, TypeError):
            continue
        previous = prior.get("restoration_receipt", {})
        if previous.get("observation_id") == obs.id or (
            prior.get("reason") == "supported_restoration"
            and review.resolved_event_id == selected.id
        ):
            if previous.get("episode_id") != proof["episode_id"]:
                fail(
                    "restoration_reuse",
                    "This receipt was already used outside the verified deficit episode",
                )
            shared.append(review.id)
    proof["same_episode_review_ids"] = sorted(shared)
    return proof
