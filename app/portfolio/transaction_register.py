"""Review counts, evidence packets and one-tab exports from one book snapshot."""

from collections import Counter, defaultdict
from datetime import date, datetime, timezone
import json
from sqlalchemy import or_
from app.models.accounts import Account
from app.models.transactions import Transaction, TransactionLine
from app.portfolio import models as m
from app.portfolio.agent_exports import book_digest
from app.portfolio.ledger_snapshot import LedgerSnapshot
from app.portfolio.observation_state import observation_states
from app.portfolio.reports import read_snapshot
from app.portfolio.service import fail, get, values
from app.portfolio.review_workspace import Record, current_records


def inventory(db, codes=None, status="open", expected=None):
    read_snapshot(db)
    stamp = book_digest(db)
    if expected and expected != stamp:
        fail("stale_export", "Restart the register at the current book revision")
    q = db.query(m.ReviewItem.id, m.ReviewItem.observation_id, m.ReviewItem.code)
    if codes:
        q = q.filter(m.ReviewItem.code.in_(codes))
    if status:
        q = q.filter(m.ReviewItem.status == status)
    groups = defaultdict(list)
    counts = Counter()
    for rid, oid, code in q.order_by(m.ReviewItem.id):
        counts[code] += 1
        groups[oid].append((rid, code))
    unlinked = groups.pop(None, [])
    return groups, dict(
        book_digest=stamp,
        snapshot_utc=datetime.now(timezone.utc).isoformat(),
        flags=sum(counts.values()),
        distinct_transactions=len(groups),
        flags_without_observation=len(unlinked),
        by_type=dict(counts),
        multiple_selected_types=sum(
            len({code for _, code in rows}) > 1 for rows in groups.values()
        ),
        multiple_selected_flags=sum(len(rows) > 1 for rows in groups.values()),
    )


def register_page(db, codes, status, after_id, limit, expected):
    if after_id and not expected:
        fail("stale_export", "Supply the first page's book digest")
    groups, summary = inventory(db, codes, status, expected)
    ids = sorted(i for i in groups if i > after_id)[: limit + 1]
    page = []
    for obs in (
        db.query(m.BankObservation)
        .filter(m.BankObservation.id.in_(ids[:limit]))
        .order_by(m.BankObservation.id)
    ):
        page.append(
            {
                "observation": values(obs),
                "selected_flags": [
                    dict(review_id=r, code=c) for r, c in groups[obs.id]
                ],
                "all_open_flags": [
                    {"review_id": r, "code": c}
                    for r, c in db.query(m.ReviewItem.id, m.ReviewItem.code)
                    .filter_by(observation_id=obs.id, status="open")
                    .order_by(m.ReviewItem.id)
                ],
            }
        )
    return {
        **summary,
        "transactions": page,
        "next_after_id": ids[limit - 1] if len(ids) > limit else None,
    }


def mappings(db, account_id=None, property_id=None, alias=None, as_of=None):
    q = current_records(db).filter(Record.kind == "identity")
    if account_id:
        q = q.filter(
            or_(
                Record.account_id == account_id,
                Record.payload["replacement_account_id"].as_integer() == account_id,
            )
        )
    if property_id:
        q = q.filter(
            or_(
                Record.property_id == property_id,
                Record.payload["site_property_id"].as_integer() == property_id,
            )
        )
    if alias:
        q = q.filter(Record.payload["alias"].as_string() == alias)
    return [
        values(r)
        for r in q.order_by(Record.id)
        if not as_of
        or r.payload["start"]
        <= as_of.isoformat()
        <= (r.payload["end"] or date.max.isoformat())
    ]


def family_rows(db, state):
    obs = state["observation"]
    oid = obs["id"]
    ids = state["event_ids"]
    account = get(db, Account, obs["account_id"])
    scope = get(db, m.AccountScope, account.id)
    yield "Account", None, {
        "account_id": account.id,
        "account_name": account.name,
        "entity_id": scope.entity_id,
        "entity_name": get(db, m.Entity, scope.entity_id).name,
    }
    for review in state["reviews"]:
        yield "Review", review["event_id"], review
    from app.models.audit import AuditLog
    from app.services.audit_redaction import redact_row

    review_ids = [r["id"] for r in state["reviews"]]
    for audit in (
        db.query(AuditLog)
        .filter(
            AuditLog.table_name == "re_review_items", AuditLog.record_id.in_(review_ids)
        )
        .order_by(AuditLog.id)
    ):
        yield "Review history", None, redact_row(audit)
    for e in db.query(m.Event).filter(m.Event.id.in_(ids)).order_by(m.Event.id):
        yield "Event", e.id, values(e)
    jids = db.query(m.JournalScope.transaction_id).filter(
        m.JournalScope.event_id.in_(ids)
    )
    for js, txn in (
        db.query(m.JournalScope, Transaction)
        .join(Transaction, Transaction.id == m.JournalScope.transaction_id)
        .filter(m.JournalScope.event_id.in_(ids))
        .order_by(Transaction.id)
    ):
        yield "Journal", js.event_id, values(txn)
    for line, dim, js in (
        db.query(TransactionLine, m.LineScope, m.JournalScope)
        .join(m.LineScope, m.LineScope.line_id == TransactionLine.id)
        .join(
            m.JournalScope,
            m.JournalScope.transaction_id == TransactionLine.transaction_id,
        )
        .filter(m.JournalScope.event_id.in_(ids))
        .order_by(TransactionLine.id)
    ):
        yield "Original journal line", js.event_id, {**values(line), **values(dim)}
    for alloc in (
        db.query(m.LineAllocation)
        .filter(
            m.LineAllocation.line_id.in_(
                db.query(TransactionLine.id).filter(
                    TransactionLine.transaction_id.in_(jids)
                )
            )
        )
        .order_by(m.LineAllocation.id)
    ):
        yield "Allocation revision", alloc.event_id, values(alloc)
    for adj in (
        db.query(m.BasisAdjustment)
        .filter(m.BasisAdjustment.event_id.in_(ids))
        .order_by(m.BasisAdjustment.id)
    ):
        yield "Basis adjustment", adj.event_id, values(adj)
    projections = LedgerSnapshot(db, date.max, event_ids=ids).project_bases(
        date.max, event_ids=ids
    )
    for basis, lines in projections.items():
        for line in lines:
            yield "Effective " + basis, line["event_id"], line
    for link, doc in (
        db.query(m.DocumentLink, m.Document)
        .join(m.Document, m.Document.id == m.DocumentLink.document_id)
        .filter(m.DocumentLink.event_id.in_(ids))
        .order_by(m.DocumentLink.id)
    ):
        yield "Linked document", link.event_id, {
            "link": values(link),
            "document": values(doc),
            "locations": [
                values(r)
                for r in db.query(m.DocumentLocation)
                .filter_by(document_id=doc.id)
                .order_by(m.DocumentLocation.id)
            ],
        }
    for record in (
        db.query(Record)
        .filter(or_(Record.observation_id == oid, Record.related_observation_id == oid))
        .order_by(Record.id)
    ):
        successor = db.query(Record.id).filter_by(supersedes_id=record.id).scalar()
        yield "Workpaper revision", None, {
            **values(record),
            "superseded_by": successor,
            "current": successor is None,
        }
    for mapping in mappings(
        db, account_id=obs["account_id"], as_of=date.fromisoformat(obs["date"])
    ):
        yield "Account mapping", None, mapping
    for assoc in state.get("transfer_associations", []):
        yield "Transfer component", None, assoc
    residuals = state["suspense_residuals"]
    for basis, items in residuals.items():
        amount = abs(sum(item["amount_cents"] for item in items))
        if obs["status"] == "posted":
            yield "Classification coverage", None, {
                "basis": basis,
                "net_suspense_cents": amount,
                "scoped_residuals": items,
                "classified_cents": (
                    abs(obs["amount_cents"]) - amount
                    if amount <= abs(obs["amount_cents"])
                    and len(state["source_observations"]) == 1
                    and all(x["amount_cents"] * obs["amount_cents"] < 0 for x in items)
                    else None
                ),
            }
    yield "Effective state", None, {
        k: v for k, v in state.items() if k not in ("observation", "reviews")
    }


def family_page(db, oid, offset, limit, expected):
    from itertools import islice

    read_snapshot(db)
    stamp = book_digest(db)
    if (offset or expected) and expected != stamp:
        fail("stale_export", "Refresh the transaction family")
    state = observation_states(db, [oid])[0]
    rows = list(islice(family_rows(db, state), offset, offset + limit + 1))
    return {
        "observation": state["observation"],
        "book_digest": stamp,
        "rows": [{"kind": k, "event_id": e, "data": d} for k, e, d in rows[:limit]],
        "next_offset": offset + limit if len(rows) > limit else None,
    }


def write_register(db, target, codes=None, status="open", expected=None):
    from openpyxl import Workbook
    from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
    from openpyxl.cell import WriteOnlyCell
    from openpyxl.styles import Font, PatternFill
    from openpyxl.utils import get_column_letter

    groups, summary = inventory(db, codes, status, expected)
    wb = Workbook(write_only=True)
    ws = wb.create_sheet("Transaction register")
    ws.freeze_panes = "D2"
    columns = [
        "Row type",
        "Observation ID",
        "Event ID",
        "Date",
        "Account",
        "Legal entity",
        "Signed cents",
        "Memo",
        "Check / reference",
        "Selected reviews",
        "All open flags",
        "Proposed treatment (research)",
        "Confidence (research)",
        "Part",
        "Full source or proof (JSON)",
    ]
    for i, width in enumerate(
        (29, 17, 14, 14, 32, 30, 18, 55, 24, 34, 36, 45, 25, 8, 90), 1
    ):
        ws.column_dimensions[get_column_letter(i)].width = width

    row_count = 0

    def append(row, header=False):
        nonlocal row_count
        row_count += 1
        if row_count > 1048576:
            fail(
                "worksheet_capacity",
                "This selection exceeds the Excel worksheet row limit. Export smaller review selections.",
            )
        cells = []
        for val in row:
            if isinstance(val, str):
                val = ILLEGAL_CHARACTERS_RE.sub(lambda m: ascii(m[0])[1:-1], val)
            cell = WriteOnlyCell(ws, value=val)
            if isinstance(val, str):
                cell.data_type = "s"
            if header:
                cell.font = Font(bold=True, color="FFFFFF")
                cell.fill = PatternFill("solid", fgColor="254962")
            cells.append(cell)
        ws.append(cells)

    append(columns, True)
    append(["Snapshot"] + [None] * 13 + [json.dumps(summary)])
    ids = sorted(groups)
    for i in range(0, len(ids), 50):
        for state in observation_states(db, ids[i : i + 50]):
            obs = state["observation"]
            oid = obs["id"]
            acct = get(db, Account, obs["account_id"])
            entity = get(db, m.Entity, get(db, m.AccountScope, acct.id).entity_id)
            flags = ", ".join(f"{c} #{r}" for r, c in groups[oid])
            opened = ", ".join(
                f"{r['code']} #{r['id']}"
                for r in state["reviews"]
                if r["status"] == "open"
            )
            append(
                [
                    "Transaction",
                    oid,
                    obs["event_id"],
                    obs["date"],
                    acct.name,
                    entity.name,
                    obs["amount_cents"],
                    obs["description"][:12000],
                    obs["reference"],
                    flags[:12000],
                    opened[:12000],
                ]
            )

            def proof(kind, event_id, record):
                raw = json.dumps(record, ensure_ascii=True, default=str, sort_keys=True)
                for j in range(0, len(raw), 30000):
                    append(
                        [kind if j == 0 else kind + " (continued)", oid, event_id]
                        + [None] * 10
                        + [j // 30000 + 1, raw[j : j + 30000]]
                    )

            proof("Observation", obs["event_id"], obs)
            for kind, event_id, record in family_rows(db, state):
                proof(kind, event_id, record)
    standalone = db.query(m.ReviewItem).filter(m.ReviewItem.observation_id.is_(None))
    if status:
        standalone = standalone.filter(m.ReviewItem.status == status)
    if codes:
        standalone = standalone.filter(m.ReviewItem.code.in_(codes))
    for review in standalone.order_by(m.ReviewItem.id):
        raw = json.dumps(values(review), ensure_ascii=True, default=str, sort_keys=True)
        for i in range(0, len(raw), 30000):
            append(
                [
                    "Standalone review" if not i else "Standalone review (continued)",
                    None,
                    review.event_id,
                ]
                + [None] * 10
                + [i // 30000 + 1, raw[i : i + 30000]]
            )
    wb.save(target)
    return summary


def save_register(db, folder, codes=None, status="open", expected=None):
    from pathlib import Path
    import os
    import tempfile
    import uuid

    folder = Path(folder)
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".review-", dir=folder)
    path = folder / ("Review-register-" + uuid.uuid4().hex + ".xlsx")
    try:
        with os.fdopen(fd, "wb") as stream:
            summary = write_register(db, stream, codes, status, expected)
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)
    return {**summary, "path": str(path), "bytes": path.stat().st_size}


def receipt_candidates(
    db,
    account_id,
    start,
    end,
    amount_cents,
    provider_id=None,
    order_id=None,
    alias=None,
    limit=50,
):
    from app.portfolio.review_workspace import current_records

    if end < start or (end - start).days > 366:
        fail("candidate_dates", "Choose an ordered search interval of up to one year")
    read_snapshot(db)
    rows = (
        db.query(m.BankObservation)
        .filter(
            m.BankObservation.account_id == account_id,
            m.BankObservation.date.between(start, end),
            m.BankObservation.amount_cents == amount_cents,
            m.BankObservation.status != "duplicate",
        )
        .order_by(m.BankObservation.date, m.BankObservation.id)
        .limit(limit + 1)
        .all()
    )
    results = []
    for obs in rows[:limit]:
        notes = [
            r.payload
            for r in current_records(db)
            .filter_by(kind="research", observation_id=obs.id)
            .order_by(Record.id)
        ]
        src = obs.source or {}
        issuer_alias = (
            db.get(m.CardAlias, src["card_alias_id"])
            if src.get("card_alias_id")
            else None
        )
        facts = {
            "provider_id": (
                {src["provider_transaction_id"]}
                if src.get("provider_transaction_id")
                else set()
            ),
            "order_id": {
                n["order_id"]
                for n in notes
                if n.get("order_id") and n["kind"] == "confirmed_fact"
            },
            "alias": {issuer_alias.alias} if issuer_alias else set(),
        }
        supplied = {"provider_id": provider_id, "order_id": order_id, "alias": alias}
        matches = []
        conflicts = []
        unknown = []
        for key, v in supplied.items():
            if not v:
                continue
            options = {str(i).casefold() if key == "alias" else i for i in facts[key]}
            needle = v.casefold() if key == "alias" else v
            if not options:
                unknown.append(key)
            elif needle in options:
                matches.append(key)
            else:
                conflicts.append(key)
        results.append(
            {
                "observation": values(obs),
                "matching_fields": matches,
                "contradictory_fields": conflicts,
                "unverified_fields": unknown,
                "assessment": (
                    "conflicting"
                    if conflicts
                    else (
                        "identifier_supported"
                        if set(matches) & {"order_id", "provider_id"}
                        else "needs_more_evidence"
                    )
                ),
                "research": notes,
            }
        )
    return {
        "candidates": results,
        "truncated": len(rows) > limit,
        "basis": "Account, signed amount and date interval select candidates. Confirm the source before creating a relationship.",
        "book_digest": book_digest(db),
    }


def movement_check(db, account_id, start, end, after_id, limit, expected):
    """Tie posted bank or issuer observations to identified physical movements."""
    from app.portfolio.service import cents

    read_snapshot(db)
    if start > end:
        fail("date_range", "The end must follow the start")
    stamp = book_digest(db)
    if (after_id or expected) and stamp != expected:
        fail("stale_export", "Refresh the dated movement check")
    account = get(db, m.AccountScope, account_id)
    if account.role not in {"bank", "credit_card"}:
        fail("movement_account", "Choose a bank or issuer account")
    observations = (
        db.query(m.BankObservation)
        .filter(
            m.BankObservation.account_id == account_id,
            m.BankObservation.date.between(start, end),
            m.BankObservation.status != "duplicate",
        )
        .order_by(m.BankObservation.id)
        .all()
    )
    by_id = {o.id: o for o in observations}
    by_event = defaultdict(list)
    for obs in observations:
        if obs.event_id:
            by_event[obs.event_id].append(obs.id)
    actual = defaultdict(int)
    unmatched = []
    totals = defaultdict(int)
    physical = (
        db.query(
            m.Event, Transaction.date, TransactionLine.debit, TransactionLine.credit
        )
        .select_from(TransactionLine)
        .join(Transaction, Transaction.id == TransactionLine.transaction_id)
        .join(m.JournalScope, m.JournalScope.transaction_id == Transaction.id)
        .join(m.Event, m.Event.id == m.JournalScope.event_id)
        .filter(
            TransactionLine.account_id == account_id,
            Transaction.date.between(start, end),
        )
        .order_by(m.Event.id, TransactionLine.id)
    )
    events = {}
    for event, day, debit, credit in physical:
        totals[(event.id, day)] += cents(debit) - cents(credit)
        events[event.id] = event
    opening = 0
    for (eid, day), amount in totals.items():
        if not amount:
            continue
        e = events[eid]
        if e.kind == "opening":
            opening += amount
            continue
        src = e.source or {}
        pointers = {
            v
            for v in [
                src.get("observation_id"),
                *src.get("observation_ids", []),
                *by_event[eid],
            ]
            if type(v) is int and v in by_id and by_id[v].date == day
        }
        if len(pointers) == 1:
            actual[pointers.pop()] += amount
        else:
            unmatched.append(
                {"event_id": eid, "date": day.isoformat(), "amount_cents": amount}
            )
    open_ids = [o.id for o in observations if o.status != "posted" or not o.event_id]
    differences = [
        {
            "observation_id": o.id,
            "date": o.date.isoformat(),
            "status": o.status,
            "observed_cents": o.amount_cents,
            "identified_ledger_cents": actual[o.id],
        }
        for o in observations
        if o.status != "posted" or not o.event_id or actual[o.id] != o.amount_cents
    ]
    findings = [{"type": "observation", **v} for v in differences] + [
        {"type": "unassigned_journal", **v} for v in unmatched
    ]
    return {
        "account_id": account_id,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "book_digest": stamp,
        "observations": len(observations),
        "observed_cents": sum(o.amount_cents for o in observations),
        "ledger_movement_cents": sum(totals.values()) - opening,
        "opening_journal_cents": opening,
        "unposted_or_unlinked": len(open_ids),
        "discrepancies": len(findings),
        "rows": findings[after_id : after_id + limit],
        "next_offset": after_id + limit if len(findings) > after_id + limit else None,
        "scope": "Original signed movements in the selected interval. Statement completeness and economic classification require separate evidence.",
    }
