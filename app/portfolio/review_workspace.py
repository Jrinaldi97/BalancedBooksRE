"""Nonposting, source-backed portfolio operations with immutable receipts."""

from datetime import date
from sqlalchemy import or_, select
from app.models.transactions import Transaction, TransactionLine
from app.portfolio import models as m

Record = m.ResearchRecord
BLOCKED = {"software_limitation", "evidence_gap"}


def current_records(db):
    return db.query(Record).filter(
        ~Record.id.in_(
            select(Record.supersedes_id).where(Record.supersedes_id.isnot(None))
        )
    )


def append(db, kind, actor, payload, **fields):
    from app.portfolio.service import values

    row = Record(kind=kind, actor=actor, payload=payload, **fields)
    db.add(row)
    db.flush()
    return {"record": values(row), "financial_entries_added": False}


def previous(db, ident, kind, **fields):
    from app.portfolio.service import fail

    if ident is None:
        return None
    row = current_records(db).filter_by(id=ident, kind=kind, **fields).first()
    if not row:
        fail("stale_research", "Choose the current revision for the same subject")
    return row


def registered(db, doc_id, sha):
    from app.portfolio.service import fail

    if (
        not db.query(m.DocumentLocation.id)
        .filter_by(document_id=doc_id, sha256=sha)
        .first()
    ):
        fail("source_version", "Register the exact document version first")


def link_existing_card_journal(db, data, actor):
    from app.portfolio.service import fail, get, scope, cents
    from app.portfolio.observation_state import observation_state
    from app.portfolio.reports import ledger

    obs = get(db, m.BankObservation, data.observation_id)
    account = scope(db, obs.account_id)
    source = obs.source or {}
    if (
        account.role != "credit_card"
        or obs.status != "imported"
        or obs.event_id
        or obs.amount_cents >= 0
        or source.get("activity_status") != "posted"
    ):
        fail(
            "card_source",
            "Choose an unlinked posted issuer charge with negative signed amount",
        )
    ident = data.provider_transaction_id
    if (
        source.get("provider_transaction_id") != ident
        or obs.source_key != "id:" + ident
    ):
        fail(
            "provider_identity",
            "Issuer observation must carry the same provider identity",
        )
    if observation_state(db, obs.id)["state_digest"] != data.expected_state_digest:
        fail("stale_state", "Refresh the observation state")
    linked = (
        db.query(m.BankObservation.id)
        .filter(
            m.BankObservation.account_id == obs.account_id,
            m.BankObservation.source["provider_transaction_id"].as_string() == ident,
        )
        .all()
    )
    if linked != [(obs.id,)]:
        fail("ambiguous_identity", "Provider identity occurs on multiple observations")
    matches = (
        db.query(m.Event, m.JournalScope, Transaction, TransactionLine)
        .join(m.JournalScope, m.JournalScope.event_id == m.Event.id)
        .join(Transaction, Transaction.id == m.JournalScope.transaction_id)
        .join(TransactionLine, TransactionLine.transaction_id == Transaction.id)
        .filter(
            TransactionLine.account_id == obs.account_id,
            or_(
                m.Event.source["provider_transaction_id"].as_string() == ident,
                m.Event.source["id"].as_string() == ident,
            ),
        )
        .all()
    )
    if len(matches) != 1:
        fail(
            "ambiguous_journal",
            "Expected exactly one journal liability line for this issuer ID and account",
        )
    event, journal, tx, line = matches[0]
    if (
        event.id,
        journal.transaction_id,
        journal.entity_id,
        event.date,
        tx.date,
        cents(line.debit) - cents(line.credit),
    ) != (
        data.event_id,
        data.journal_id,
        account.entity_id,
        obs.date,
        obs.date,
        obs.amount_cents,
    ):
        fail(
            "journal_identity",
            "Event, journal, entity, posting date and signed liability must match",
        )
    if (
        any(
            event.source[k] != ident
            for k in ("provider_transaction_id", "id")
            if k in event.source
        )
        or event.source.get("entity_id", account.entity_id) != account.entity_id
        or str(event.source.get("status", "posted")).casefold()
        not in {"settled", "posted", "completed"}
    ):
        fail(
            "provider_identity",
            "Journal source identity, entity or settlement status conflicts",
        )
    if (
        db.query(m.BankObservation.id).filter_by(event_id=event.id).first()
        or db.query(m.CardActivity.id)
        .filter(
            or_(
                m.CardActivity.event_id == event.id,
                m.CardActivity.observation_id == obs.id,
            )
        )
        .first()
        or event.corrects_id
        or db.query(m.Event.id).filter_by(corrects_id=event.id).first()
    ):
        fail("journal_used", "Journal already has an association or correction chain")
    for basis in ("accrual", "modified_cash_bank_posting"):
        rows = ledger(db, date.max, basis=basis, event_ids=[event.id])
        physical = [r for r in rows if r["role"] in ("bank", "credit_card")]
        if (
            len(physical) != 1
            or physical[0]["account_id"] != obs.account_id
            or physical[0]["debit_cents"] - physical[0]["credit_cents"]
            != obs.amount_cents
        ):
            fail(
                "card_source_lineage",
                "Expected one exact issuer movement on each basis",
            )
    obs.event_id = event.id
    obs.status = "posted"
    return {
        **data.model_dump(),
        "account_id": obs.account_id,
        "amount_cents": obs.amount_cents,
        "coverage_effect": "posted issuer observation linked to existing liability movement",
        "financial_entries_added": False,
    }


def original_review_code(db, review):
    latest = (
        current_records(db).filter_by(kind="review_type", review_id=review.id).first()
    )
    if latest:
        return latest.payload["original_code"]
    if (
        review.code == "software_limitation"
        and " Original flag type: suspense. Original detail: " in review.detail
    ):
        return "suspense"
    return review.code


def suspense_review(db, review):
    return review.code == "suspense" or (
        review.code in BLOCKED and original_review_code(db, review) == "suspense"
    )


def retype_review(db, data, actor):
    from app.portfolio.service import fail, get

    review = get(db, m.ReviewItem, data.review_id)
    origin = original_review_code(db, review)
    if (
        review.status != "open"
        or review.code != data.expected_code
        or data.new_code == review.code
    ):
        fail("review_state", "Inspect the current open review and its type")
    if data.new_code not in BLOCKED | {origin}:
        fail(
            "review_type", "Choose a blockage type or restore the original review type"
        )
    latest = (
        current_records(db).filter_by(kind="review_type", review_id=review.id).first()
    )
    review.code = data.new_code
    return append(
        db,
        "review_type",
        actor,
        {**data.model_dump(), "original_code": origin},
        review_id=review.id,
        observation_id=review.observation_id,
        supersedes_id=latest.id if latest else None,
    )


def record_transaction_research(db, data, actor):
    from app.portfolio.service import fail, get, scope

    obs = get(db, m.BankObservation, data.observation_id)
    previous(db, data.supersedes_id, "research", observation_id=obs.id)
    if data.related_observation_id:
        get(db, m.BankObservation, data.related_observation_id)
    if data.entity_id:
        get(db, m.Entity, data.entity_id)
    if data.natural_account_id:
        scope(db, data.natural_account_id, data.entity_id)
    if data.property_id:
        get(db, m.Property, data.property_id)
    if data.unit_id and get(db, m.Unit, data.unit_id).property_id != data.property_id:
        fail("unit_property", "Choose the unit's property")
    for ref in data.sources:
        if ref.document_id:
            get(db, m.Document, ref.document_id)
        if ref.sha256 and ref.document_id:
            registered(db, ref.document_id, ref.sha256)
        if ref.event_id:
            get(db, m.Event, ref.event_id)
            if (
                ref.document_id
                and not db.query(m.DocumentLink.id)
                .filter_by(event_id=ref.event_id, document_id=ref.document_id)
                .first()
            ):
                fail("source_event", "Document is not linked to the stated event")
    if data.kind == "proposed_component":
        if not data.amount_cents:
            fail("component_amount", "Specify a positive component amount")
        parts = (
            current_records(db).filter_by(kind="research", observation_id=obs.id).all()
        )
        total = sum(
            r.amount_cents
            for r in parts
            if r.payload["kind"] == "proposed_component" and r.id != data.supersedes_id
        )
        if total + data.amount_cents > abs(obs.amount_cents):
            fail("component_amount", "Proposed components exceed the observed movement")
    return append(
        db,
        "research",
        actor,
        data.model_dump(mode="json"),
        observation_id=obs.id,
        related_observation_id=data.related_observation_id,
        amount_cents=data.amount_cents,
        property_id=data.property_id,
        supersedes_id=data.supersedes_id,
    )


def link_observation_application(db, data, actor):
    from app.portfolio.service import fail, get

    src = get(db, m.BankObservation, data.source_observation_id)
    dst = get(db, m.BankObservation, data.settlement_observation_id)
    if (
        src.id == dst.id
        or src.status != "posted"
        or dst.status != "posted"
        or not src.event_id
        or not dst.event_id
    ):
        fail(
            "application_source",
            "Both distinct observations must already have posted events",
        )
    if data.kind == "refund" and (src.amount_cents >= 0 or dst.amount_cents <= 0):
        fail("application_direction", "Choose the purchase and subsequent credit")
    if data.kind == "reimbursement" and src.amount_cents >= 0:
        fail("application_direction", "Choose the underlying outlay as the source")
    if dst.date < src.date:
        fail("application_date", "Settlement precedes the source movement")
    if data.kind == "reimbursement" and not data.reverse:
        from app.portfolio.observation_state import observation_states
        from app.portfolio.reports import ledger

        expenses = []
        for state in observation_states(db, [src.id, dst.id]):
            costs = {}
            for row in ledger(db, date.max, event_ids=state["event_ids"]):
                if row["role"] in {"expense", "cogs"}:
                    key = (row["entity_id"], row["account_id"])
                    costs[key] = (
                        costs.get(key, 0) + row["debit_cents"] - row["credit_cents"]
                    )
            expenses.append(any(v > 0 for v in costs.values()))
        if all(expenses):
            fail(
                "duplicate_expense",
                "Both families recognize expense. Inspect and correct the settlement posting before recording reimbursement.",
            )
    if data.kind == "refund" and not data.reverse:
        native = (
            db.query(m.CardActivity)
            .filter_by(observation_id=src.id, kind="purchase")
            .first()
        )
        if (
            native
            or db.query(m.CardActivity.id)
            .filter_by(observation_id=dst.id, kind="refund")
            .first()
        ):
            fail(
                "card_refund_parent",
                "Use the typed card refund workflow for an issuer purchase with tracked cost components",
            )
    registered(db, data.evidence.document_id, data.evidence.sha256)
    prior = previous(
        db,
        data.supersedes_id,
        "application",
        observation_id=src.id,
        related_observation_id=dst.id,
    )
    pair = (
        current_records(db)
        .filter_by(
            kind="application", observation_id=src.id, related_observation_id=dst.id
        )
        .all()
    )
    if any(not r.payload["reverse"] and r.id != data.supersedes_id for r in pair):
        fail("application_duplicate", "Revise the existing relationship for this pair")
    if prior and prior.payload["kind"] != data.kind:
        fail("application_identity", "A revision must keep the relationship type")
    if data.kind == "processor_batch" and (
        src.amount_cents <= 0 or dst.amount_cents <= 0
    ):
        fail(
            "application_direction", "Choose an incoming receipt and an incoming payout"
        )
    if data.reverse and (
        not prior
        or prior.payload["reverse"]
        or prior.payload["kind"] != data.kind
        or prior.amount_cents != data.amount_cents
    ):
        fail("application_reversal", "Reverse the precise current relationship")
    remaining = {}
    for obs in (src, dst):
        active = (
            current_records(db)
            .filter(
                Record.kind == "application",
                or_(
                    Record.observation_id == obs.id,
                    Record.related_observation_id == obs.id,
                ),
            )
            .all()
        )
        used = sum(
            r.amount_cents
            for r in active
            if not r.payload["reverse"] and r.id != data.supersedes_id
        )
        remaining[str(obs.id)] = (
            abs(obs.amount_cents) - used - (0 if data.reverse else data.amount_cents)
        )
    if min(remaining.values()) < 0:
        fail(
            "application_capacity",
            "A linked movement has insufficient unapplied capacity",
        )
    return {
        **append(
            db,
            "application",
            actor,
            data.model_dump(mode="json"),
            observation_id=src.id,
            related_observation_id=dst.id,
            amount_cents=data.amount_cents,
            supersedes_id=data.supersedes_id,
        ),
        "remaining_cents": remaining,
    }


def record_identity_mapping(db, data, actor):
    from app.portfolio.service import fail, get

    if bool(data.account_id) == bool(data.property_id) or (
        data.end and data.end < data.start
    ):
        fail(
            "identity_scope",
            "Identify one account or one property and an ordered interval",
        )
    if data.account_id:
        owner = get(db, m.AccountScope, data.account_id).entity_id
        if data.legal_entity_id != owner or data.site_property_id:
            fail("identity_owner", "Match the account's legal owner")
        if data.operating_entity_id:
            get(db, m.Entity, data.operating_entity_id)
        if data.replacement_account_id:
            if data.replacement_account_id == data.account_id:
                fail("account_lineage", "An account cannot replace itself")
            get(db, m.AccountScope, data.replacement_account_id)
    else:
        get(db, m.Property, data.property_id)
        if (
            not data.site_property_id
            or data.site_property_id == data.property_id
            or any(
                (
                    data.legal_entity_id,
                    data.operating_entity_id,
                    data.replacement_account_id,
                )
            )
        ):
            fail("site_property", "Choose a distinct parent site for the building")
        get(db, m.Property, data.site_property_id)
    registered(db, data.evidence.document_id, data.evidence.sha256)
    previous(
        db,
        data.supersedes_id,
        "identity",
        account_id=data.account_id,
        property_id=data.property_id,
    )
    for r in current_records(db).filter_by(
        kind="identity", account_id=data.account_id, property_id=data.property_id
    ):
        if (
            r.id != data.supersedes_id
            and r.payload["start"] <= (data.end or date.max).isoformat()
            and (r.payload["end"] or date.max.isoformat()) >= data.start.isoformat()
        ):
            fail(
                "identity_interval",
                "The effective interval overlaps another current mapping",
            )
    # Follow dated edges before installation so a site or account cannot
    # become its own ancestor during any overlapping interval.
    edge = "replacement_account_id" if data.account_id else "site_property_id"
    origin = data.account_id or data.property_id
    target = getattr(data, edge)
    if target:
        pending = [
            (
                target,
                data.start.isoformat(),
                (data.end or date.max).isoformat(),
                {origin},
            )
        ]
        while pending:
            node, start, end, seen = pending.pop()
            if node in seen:
                fail(
                    "identity_cycle",
                    "The effective-dated identity chain would contain a cycle",
                )
            for link in current_records(db).filter_by(
                kind="identity",
                **{("account_id" if data.account_id else "property_id"): node}
            ):
                left = max(start, link.payload["start"])
                right = min(end, link.payload["end"] or date.max.isoformat())
                if left <= right and link.payload.get(edge):
                    pending.append((link.payload[edge], left, right, seen | {node}))
    affected = None
    if data.account_id:
        query = db.query(m.BankObservation.id).filter(
            m.BankObservation.account_id == data.account_id,
            m.BankObservation.date >= data.start,
        )
        if data.end:
            query = query.filter(m.BankObservation.date <= data.end)
        affected = {
            "observations_in_interval": query.count(),
            "sample_observation_ids": [
                i for i, in query.order_by(m.BankObservation.id).limit(20)
            ],
        }
    result = append(
        db,
        "identity",
        actor,
        data.model_dump(mode="json"),
        account_id=data.account_id,
        property_id=data.property_id,
        supersedes_id=data.supersedes_id,
    )
    return {**result, "affected": affected}


RESEARCH_OPERATIONS = {
    f.__name__: f
    for f in (
        link_existing_card_journal,
        retype_review,
        record_transaction_research,
        link_observation_application,
        record_identity_mapping,
    )
}


def correct_posted_components(db, data, actor):
    from app.portfolio import schemas as s
    from app.portfolio.service import get, fail, scope, line, post_existing_pool
    from app.portfolio.observation_state import observation_state

    obs = get(db, m.BankObservation, data.observation_id)
    review = get(db, m.ReviewItem, data.review_id)
    bank = scope(db, obs.account_id)
    if obs.status != "posted" or not obs.event_id or bank.role != "bank" or bank.pooled:
        fail(
            "component_source",
            "Choose a posted ordinary bank movement. Pooled activity uses correct_posted_pool_split.",
        )
    if (
        review.observation_id != obs.id
        or review.code != "suspense"
        or review.status != "open"
    ):
        fail("review_state", "Choose this observation's open suspense review")
    state = observation_state(db, obs.id)
    if state["state_digest"] != data.expected_state_digest:
        fail("stale_state", "Refresh the observation before applying components")
    if len(set(data.component_ids)) != len(data.component_ids):
        fail("component_identity", "Choose each component once")
    entries = []
    management = []
    total = 0
    for ident in data.component_ids:
        row = previous(db, ident, "research", observation_id=obs.id)
        part = row.payload
        if (
            part["kind"] != "proposed_component"
            or not part.get("natural_account_id")
            or part.get("entity_id") != bank.entity_id
        ):
            fail(
                "component_scope",
                "Each component needs its own amount, natural account and source bank entity",
            )
        target = scope(db, part["natural_account_id"], bank.entity_id)
        if target.role not in {
            "income",
            "expense",
            "cogs",
            "project",
            "asset",
            "fixed_asset",
            "prepaid",
            "contribution",
            "distribution",
            "equity",
        }:
            fail(
                "component_account",
                "Use a dedicated settlement, custody or financing operation for this control",
            )
        amount = row.amount_cents * (1 if obs.amount_cents > 0 else -1)
        total += amount
        dims = dict(property_id=part.get("property_id"), unit_id=part.get("unit_id"))
        entries.extend(
            [
                line(obs.account_id, amount, cash_flow=target.cash_flow, **dims),
                line(target.account_id, -amount, **dims),
            ]
        )
        if target.role in {"income", "expense", "cogs"}:
            management.append(
                s.ManagementInput(
                    entity_id=bank.entity_id,
                    property_id=dims["property_id"],
                    account_id=target.account_id,
                    date=obs.date,
                    amount_cents=amount,
                )
            )
    posting = s.Posting(
        kind="component_classification",
        date=obs.date,
        description="Classify evidenced components of a bank movement",
        rationale=data.evidence,
        source={"observation_id": obs.id, "research_record_ids": data.component_ids},
        journals=[s.Journal(entity_id=bank.entity_id, lines=entries)],
        management=management,
    )
    result = post_existing_pool(db, posting, obs, review, state, actor)
    for ident in data.component_ids:
        append(
            db,
            "component_applied",
            actor,
            {"event_id": result["event_id"], "research_record_id": ident},
            observation_id=obs.id,
            supersedes_id=ident,
        )
    return {
        **result,
        "component_ids": data.component_ids,
        "classified_this_command_cents": abs(total),
        "amount_requiring_evidence_cents": abs(result["remaining_cents"]),
    }


RESEARCH_OPERATIONS["correct_posted_components"] = correct_posted_components


def record_processor_batch(db, data, actor):
    from app.portfolio.service import get, fail, scope

    obs = get(db, m.BankObservation, data.observation_id)
    owner = scope(db, obs.account_id)
    if (
        owner.role != "bank"
        or obs.amount_cents <= 0
        or obs.status not in {"posted", "imported"}
    ):
        fail("batch_source", "Choose the receiving bank deposit")
    registered(db, data.evidence.document_id, data.evidence.sha256)
    previous(db, data.supersedes_id, "processor_batch", observation_id=obs.id)
    ids = {r.provider_transaction_id for r in data.receipts}
    if len(ids) != len(data.receipts) or any(
        r.fee_cents > r.gross_cents for r in data.receipts
    ):
        fail(
            "batch_receipts",
            "Each receipt requires a distinct provider ID and a fee within its gross amount",
        )
    for r in data.receipts:
        get(db, m.Entity, r.beneficiary_entity_id)
        if r.property_id:
            get(db, m.Property, r.property_id)
    for prior in current_records(db).filter_by(kind="processor_batch"):
        if prior.id == data.supersedes_id:
            continue
        if prior.observation_id == obs.id or (
            prior.payload["processor"].casefold() == data.processor.casefold()
            and (
                prior.payload["batch_id"] == data.batch_id
                or ids.intersection(
                    r["provider_transaction_id"] for r in prior.payload["receipts"]
                )
            )
        ):
            fail(
                "batch_duplicate",
                "An active deposit, batch ID or processor receipt is already assigned",
            )
    gross = sum(r.gross_cents for r in data.receipts)
    fees = sum(r.fee_cents for r in data.receipts)
    if gross - fees != obs.amount_cents:
        fail("batch_net", "Gross less fees must equal the observed net deposit")
    return {
        **append(
            db,
            "processor_batch",
            actor,
            data.model_dump(mode="json"),
            observation_id=obs.id,
        ),
        "gross_cents": gross,
        "fee_cents": fees,
        "net_cents": obs.amount_cents,
        "bank_entity_id": owner.entity_id,
    }


RESEARCH_OPERATIONS["record_processor_batch"] = record_processor_batch
