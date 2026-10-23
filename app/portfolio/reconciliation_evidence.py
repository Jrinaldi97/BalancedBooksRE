"""Audited reconciliation coverage and append-only reviewed supersession."""

from datetime import timedelta
from app.portfolio import models as m
from app.portfolio.reports import ledger, read_snapshot


def active_reconciliations(db, account_id, through):
    from app.portfolio.service import fail

    rows = (
        db.query(m.Reconciliation)
        .filter(
            m.Reconciliation.account_id == account_id, m.Reconciliation.end <= through
        )
        .order_by(m.Reconciliation.start, m.Reconciliation.end, m.Reconciliation.id)
        .all()
    )
    ids = {r.id for r in rows}
    links = (
        db.query(m.ReconciliationRevision)
        .filter(m.ReconciliationRevision.supersedes_id.in_(ids))
        .all()
    )
    for link in links:
        old = db.get(m.Reconciliation, link.supersedes_id)
        new = db.get(m.Reconciliation, link.replacement_id)
        if not new or (new.account_id, new.start, new.end) != (
            old.account_id,
            old.start,
            old.end,
        ):
            fail(
                "reconciliation_revision_scope",
                "A revision must retain its original account and interval",
            )
    retired = {r.supersedes_id for r in links}
    return [r for r in rows if r.id not in retired]


def observation_inventory(db, account_id, start, end):
    from app.portfolio.service import values

    return [
        values(o)
        for o in db.query(m.BankObservation)
        .filter(
            m.BankObservation.account_id == account_id,
            m.BankObservation.date >= start,
            m.BankObservation.date <= end,
            m.BankObservation.status != "duplicate",
        )
        .order_by(m.BankObservation.id)
    ]


def supported_opening(db, bank, start):
    """Opening entries at the supported first cutoff precede its statement activity."""
    from app.portfolio.service import balance

    opening = balance(db, bank.account_id, start - timedelta(days=1))
    if start == bank.coverage_start:
        opening_events = {
            e.id for e in db.query(m.Event).filter_by(kind="opening", date=start)
        }
        opening += sum(
            r["debit_cents"] - r["credit_cents"]
            for r in ledger(
                db, start, account_id=bank.account_id, event_ids=list(opening_events)
            )
        )
    return opening


def reconciliation_state(db, reconciliation_id):
    from app.portfolio.service import get, scope, values, balance, digest

    read_snapshot(db)
    row = get(db, m.Reconciliation, reconciliation_id)
    bank = scope(db, row.account_id)
    links = (
        db.query(m.ReconciliationRevision)
        .filter(
            (m.ReconciliationRevision.supersedes_id == row.id)
            | (m.ReconciliationRevision.replacement_id == row.id)
        )
        .order_by(m.ReconciliationRevision.id)
        .all()
    )
    state = {
        "reconciliation": values(row),
        "revisions": [values(r) for r in links],
        "current_observations": observation_inventory(
            db, row.account_id, row.start, row.end
        ),
        "supported_opening_cents": supported_opening(db, bank, row.start),
        "current_ending_cents": balance(db, row.account_id, row.end),
        "coverage_start": (
            bank.coverage_start.isoformat() if bank.coverage_start else None
        ),
        "prior_coverage": (
            coverage_snapshot(db, bank, row.start - timedelta(days=1))
            if bank.coverage_start and row.start > bank.coverage_start
            else None
        ),
        "read_only": True,
    }
    state["reconciliation_digest"] = digest(state)
    return state


def coverage_snapshot(db, bank, through):
    """Bind reviewed version choice and current source inventory to an assessment."""
    from app.portfolio.service import values, digest

    rows = active_reconciliations(db, bank.account_id, through)
    ids = {
        r.id
        for r in db.query(m.Reconciliation).filter(
            m.Reconciliation.account_id == bank.account_id,
            m.Reconciliation.end <= through,
        )
    }
    snapshot = {
        "applicable_reconciliation_ids": [r.id for r in rows],
        "reconciliations": [values(r) for r in rows],
        "revisions": [
            values(r)
            for r in db.query(m.ReconciliationRevision)
            .filter(m.ReconciliationRevision.supersedes_id.in_(ids))
            .order_by(m.ReconciliationRevision.id)
        ],
        "observations": observation_inventory(
            db, bank.account_id, bank.coverage_start or through, through
        ),
    }

    return {
        "applicable_reconciliation_ids": snapshot["applicable_reconciliation_ids"],
        "revision_ids": [r["id"] for r in snapshot["revisions"]],
        "coverage_digest": digest(snapshot),
    }


def validate_coverage(db, bank, through):
    from app.portfolio.service import balance, fail

    if not bank.coverage_start or bank.coverage_start > through:
        fail("incomplete_coverage", "A supported opening cutoff is required")
    covered = bank.coverage_start - timedelta(days=1)
    used = []
    equivalent = {}
    for row in active_reconciliations(db, bank.account_id, through):
        signature = (
            row.opening_cents,
            row.ending_cents,
            tuple(sorted((row.result or {}).get("observation_ids", []))),
        )
        interval = (row.start, row.end)
        if interval in equivalent and equivalent[interval] != signature:
            fail(
                "ambiguous_reconciliation",
                "Competing active statement versions need explicit reviewed supersession",
            )
        equivalent[interval] = signature
        if row.start > covered + timedelta(days=1) or row.end <= covered:
            continue
        if (
            balance(db, bank.account_id, row.end) != row.ending_cents
            or (row.result or {}).get("difference_cents") != 0
        ):
            fail(
                "bank_reconciliation_difference",
                f"Reconciliation {row.id} no longer agrees with bank books",
            )
        inventory = observation_inventory(db, bank.account_id, row.start, row.end)
        if (
            any(not o["event_id"] for o in inventory)
            or {o["id"] for o in inventory}
            != set(row.result.get("observation_ids", []))
            or len(row.result.get("observation_ids", [])) != len(inventory)
            or row.ending_cents - row.opening_cents
            != sum(o["amount_cents"] for o in inventory)
        ):
            fail(
                "stale_reconciliation",
                f"Reconciliation {row.id} does not cover the current observations",
            )
        if supported_opening(db, bank, row.start) != row.opening_cents:
            fail(
                "unsupported_opening",
                f"Reconciliation {row.id} opening is not supported by bank books",
            )
        covered = row.end
        used.append(row.id)
    if covered < through:
        fail(
            "missing_reconciliation",
            "Continuous reconciled statements through the selected cutoff are required",
        )
    return used


def revise_reconciliation(db, data, actor):
    from app.portfolio.service import get, scope, fail

    original = get(db, m.Reconciliation, data.supersedes_id)
    state = reconciliation_state(db, original.id)
    if state["reconciliation_digest"] != data.expected_reconciliation_digest:
        fail(
            "stale_reconciliation_revision", "Refresh the reconciliation revision state"
        )
    if db.query(m.ReconciliationRevision).filter_by(supersedes_id=original.id).first():
        fail(
            "superseded_reconciliation",
            "Revise the current reviewed reconciliation version",
        )
    bank = scope(db, original.account_id)
    if not bank.coverage_start or original.start < bank.coverage_start:
        fail("incomplete_coverage", "A supported opening cutoff is required")
    if original.start > bank.coverage_start:
        validate_coverage(db, bank, original.start - timedelta(days=1))
    inventory = state["current_observations"]
    if len(data.observation_ids) != len(set(data.observation_ids)) or set(
        data.observation_ids
    ) != {o["id"] for o in inventory}:
        fail(
            "reconciliation_inventory",
            "Provide each and only the current nonduplicate observations in this interval",
        )
    if any(not o["event_id"] for o in inventory):
        fail("reconciliation_inventory", "Every statement observation must be posted")
    if data.opening_cents != state["supported_opening_cents"]:
        fail(
            "unsupported_opening",
            "Opening cash must agree with the supported prior bank balance",
        )
    if data.ending_cents != state[
        "current_ending_cents"
    ] or data.ending_cents - data.opening_cents != sum(
        o["amount_cents"] for o in inventory
    ):
        fail(
            "reconciliation_difference",
            "The complete inventory, opening and ending must agree exactly with bank books",
        )
    documents = [source.model_dump() for source in data.source_documents]
    if len({(d["document_id"], d["sha256"]) for d in documents}) != len(documents):
        fail("reconciliation_source", "Source document evidence must be unique")
    for source in documents:
        get(db, m.Document, source["document_id"])
        if not db.query(m.DocumentLocation).filter_by(**source).first():
            fail(
                "reconciliation_source",
                "Each source hash must match a registered immutable document location",
            )
    result = {
        "difference_cents": 0,
        "book_cents": data.ending_cents,
        "observation_ids": sorted(data.observation_ids),
        "supersedes_id": original.id,
        "reviewed_revision": True,
    }
    replacement = m.Reconciliation(
        account_id=original.account_id,
        start=original.start,
        end=original.end,
        opening_cents=data.opening_cents,
        ending_cents=data.ending_cents,
        document_id=documents[0]["document_id"],
        result=result,
        actor=actor,
    )
    db.add(replacement)
    db.flush()
    revision = m.ReconciliationRevision(
        supersedes_id=original.id,
        replacement_id=replacement.id,
        reason=data.reason,
        source_documents=documents,
        previous_digest=data.expected_reconciliation_digest,
        actor=actor,
    )
    db.add(revision)
    db.flush()
    return {
        "reconciliation_id": replacement.id,
        "revision_id": revision.id,
        "supersedes_id": original.id,
        "observation_ids": sorted(data.observation_ids),
        "bank_cash_delta_cents": 0,
        "financial_entries_added": False,
    }
