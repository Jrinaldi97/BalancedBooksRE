"""Validated portfolio commands; every posted entry uses SlowBooks' journal service.

Call execute inside a transaction. HTTP/CLI boundaries commit once, never helpers.
Untrusted descriptions/evidence are stored as data, never executed as instructions.
"""

import hashlib
import json
import uuid
from collections import defaultdict
from datetime import date
from decimal import Decimal
from sqlalchemy import func
from app.models.accounts import Account, AccountType
from app.models.transactions import Transaction, TransactionLine
from app.services.accounting import create_journal_entry
from app.portfolio import models as m, schemas as s


class Problem(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def fail(code, message):
    raise Problem(code, message)


def get(db, model, ident):
    obj = db.get(model, ident)
    if obj is None:
        fail("not_found", f"{model.__name__} {ident} does not exist")
    return obj


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def cents(value):
    return int(Decimal(str(value or 0)) * 100)


def values(obj):
    return {
        c.name: (
            getattr(obj, c.name).isoformat()
            if isinstance(getattr(obj, c.name), date)
            else getattr(obj, c.name)
        )
        for c in obj.__table__.columns
    }


def check_date(db, entity_id, day):
    entity = get(db, m.Entity, entity_id)
    if entity.closed_through and day <= entity.closed_through:
        fail(
            "closed_period", f"{entity.name} is closed through {entity.closed_through}"
        )


def scope(db, account_id, entity_id=None):
    item = get(db, m.AccountScope, account_id)
    if entity_id is not None and item.entity_id != entity_id:
        fail(
            "wrong_entity",
            f"Account {account_id} does not belong to entity {entity_id}",
        )
    if not get(db, Account, account_id).is_active:
        fail("inactive_account", f"Account {account_id} is inactive")
    return item


def line(account_id, amount, **dimensions):
    return s.Line(
        account_id=account_id,
        debit_cents=max(amount, 0),
        credit_cents=max(-amount, 0),
        **dimensions,
    )


def balance(db, account_id, through=None, fund_id=None, item=None, property_id=None):
    query = db.query(TransactionLine).join(
        Transaction, Transaction.id == TransactionLine.transaction_id
    )
    query = query.join(
        m.JournalScope, m.JournalScope.transaction_id == Transaction.id
    ).filter(TransactionLine.account_id == account_id)
    if through:
        query = query.filter(Transaction.date <= through)
    if fund_id is not None or item is not None or property_id is not None:
        query = query.join(m.LineScope, m.LineScope.line_id == TransactionLine.id)
        if fund_id is not None:
            query = query.filter(m.LineScope.fund_id == fund_id)
        if item is not None:
            query = query.filter(m.LineScope.item == item)
        if property_id is not None:
            query = query.filter(m.LineScope.property_id == property_id)
    return sum(cents(r.debit) - cents(r.credit) for r in query.all())


TEMPLATES = [
    ("suspense_debit", "Unclassified withdrawals", "asset"),
    ("suspense_credit", "Unclassified receipts", "liability"),
    ("due_from", "Due from other entities", "asset"),
    ("due_to", "Due to other entities", "liability"),
    ("bridge", "Transfers in transit", "asset"),
    ("custody_asset", "Funds held by another entity", "asset"),
    ("custody_liability", "Funds held for others", "liability"),
    ("deposit", "Security deposits payable", "liability"),
    ("last_month_rent", "Last month rent held", "liability"),
    ("equity", "Opening equity", "equity"),
    ("payable", "Accounts payable", "liability"),
    ("receivable", "Accounts receivable", "asset"),
]


def control(db, entity_id, role):
    rows = (
        db.query(m.AccountScope)
        .filter_by(entity_id=entity_id, role=role, code=role)
        .all()
    )
    if len(rows) != 1:
        fail(
            "control_mapping",
            f"Entity {entity_id} needs one mapped {role} control account",
        )
    return rows[0].account_id


def create_account(db, data, actor):
    get(db, m.Entity, data.entity_id)
    expected = {
        "bank": "asset",
        "cash_equivalent": "asset",
        "fixed_asset": "asset",
        "project": "asset",
        "escrow": "asset",
        "receivable": "asset",
        "prepaid": "asset",
        "due_from": "asset",
        "custody_asset": "asset",
        "suspense_debit": "asset",
        "bridge": "asset",
        "contra_asset": "asset",
        "payable": "liability",
        "deferred": "liability",
        "debt": "liability",
        "deposit": "liability",
        "last_month_rent": "liability",
        "due_to": "liability",
        "custody_liability": "liability",
        "suspense_credit": "liability",
        "credit_card": "liability",
        "income": "income",
        "expense": "expense",
        "cogs": "cogs",
        "equity": "equity",
        "contribution": "equity",
        "distribution": "equity",
    }
    if data.role in expected and expected[data.role] != data.account_type:
        fail("account_type", f"{data.role} requires {expected[data.role]}")
    if data.pooled and data.role != "bank":
        fail("fund_mapping", "Only physical bank accounts can be pooled")
    account = Account(
        name=data.name,
        account_number=None,
        account_type=AccountType(data.account_type),
        balance=Decimal("0"),
        bank_kind=(
            "bank"
            if data.role == "bank"
            else ("credit_card" if data.role == "credit_card" else None)
        ),
    )
    db.add(account)
    db.flush()
    account_data = data.model_dump(exclude={"name", "account_type"})
    if "cash_flow" not in data.model_fields_set:
        if data.role in ("debt", "due_to", "contribution", "distribution"):
            account_data["cash_flow"] = "financing"
        elif data.role in ("fixed_asset", "due_from"):
            account_data["cash_flow"] = "investing"
    db.add(m.AccountScope(account_id=account.id, **account_data))
    db.flush()
    return {"account_id": account.id}


def create_entity(db, data, actor):
    entity = m.Entity(**data.model_dump())
    db.add(entity)
    db.flush()
    db.add(m.Party(code=data.code, name=data.name, kind=data.kind, entity_id=entity.id))
    db.flush()
    for role, name, kind in TEMPLATES:
        create_account(
            db,
            s.AccountInput(
                entity_id=entity.id,
                code=role,
                name=name,
                account_type=kind,
                role=role,
                cash_flow=(
                    "financing"
                    if role in ("due_to", "due_from", "equity")
                    else "operating"
                ),
            ),
            actor,
        )
    return {"entity_id": entity.id}


def create_property(db, data, actor):
    row = m.Property(**data.model_dump())
    db.add(row)
    db.flush()
    return {"property_id": row.id}


def create_unit(db, data, actor):
    get(db, m.Property, data.property_id)
    row = m.Unit(**data.model_dump())
    db.add(row)
    db.flush()
    return {"unit_id": row.id}


def create_party(db, data, actor):
    if data.entity_id:
        get(db, m.Entity, data.entity_id)
    row = m.Party(**data.model_dump())
    db.add(row)
    db.flush()
    return {"party_id": row.id}


def record_ownership(db, data, actor):
    get(db, m.Party, data.owner_id)
    targets = [
        (key, model)
        for key, model in [
            ("entity_id", m.Entity),
            ("property_id", m.Property),
            ("unit_id", m.Unit),
        ]
        if getattr(data, key)
    ]
    if len(targets) != 1:
        fail("ownership_target", "Choose exactly one owned entity, property or unit")
    key, model = targets[0]
    get(db, model, getattr(data, key))
    if data.end and data.end <= data.start:
        fail("date_range", "Ownership end must follow start")
    others = (
        db.query(m.Ownership)
        .filter(
            getattr(m.Ownership, key) == getattr(data, key),
            m.Ownership.owner_id == data.owner_id,
            m.Ownership.interest == data.interest,
        )
        .all()
    )
    for other in others:
        if data.start < (other.end or date.max) and other.start < (
            data.end or date.max
        ):
            fail(
                "ownership_overlap", "This ownership period overlaps an existing record"
            )
    # Sum the complete active perimeter at every boundary in this interval.
    peers = (
        db.query(m.Ownership)
        .filter(
            getattr(m.Ownership, key) == getattr(data, key),
            m.Ownership.interest == data.interest,
        )
        .all()
    )
    boundaries = {data.start} | {
        x.start for x in peers if data.start <= x.start < (data.end or date.max)
    }
    if data.share_ppm is not None:
        for day in boundaries:
            total = data.share_ppm + sum(
                x.share_ppm or 0 for x in peers if x.start <= day < (x.end or date.max)
            )
            if total > 1000000:
                fail(
                    "ownership_total",
                    "Known ownership interests exceed 100 percent in this interval",
                )
    row = m.Ownership(**data.model_dump())
    db.add(row)
    db.flush()
    return {"ownership_id": row.id}


def create_fund(db, data, actor):
    get(db, m.Entity, data.holder_entity_id)
    get(db, m.Party, data.beneficiary_id)
    if data.property_id:
        get(db, m.Property, data.property_id)
    if data.unit_id:
        unit = get(db, m.Unit, data.unit_id)
        if unit.property_id != data.property_id:
            fail("wrong_property", "Unit does not belong to property")
    if (
        data.purpose in ("security_deposit", "escrow")
        and data.restriction == "unrestricted"
    ):
        fail("restriction", "Security deposits and escrow must carry a restriction")
    row = m.Fund(**data.model_dump())
    db.add(row)
    db.flush()
    return {"fund_id": row.id}


def validate_lines(db, entity_id, lines, balanced=True):
    if balanced and sum(x.debit_cents - x.credit_cents for x in lines):
        fail(
            "unbalanced", "Debits and credits must balance separately for every entity"
        )
    if balanced and not any(x.debit_cents for x in lines):
        fail("empty_journal", "A journal must contain positive amounts")
    for ln in lines:
        sc = scope(db, ln.account_id, entity_id)
        if (ln.debit_cents > 0) == (ln.credit_cents > 0):
            fail("invalid_line", "Each line must have one positive debit or credit")
        if ln.property_id:
            get(db, m.Property, ln.property_id)
        if ln.unit_id and get(db, m.Unit, ln.unit_id).property_id != ln.property_id:
            fail("wrong_property", "Unit does not belong to property")
        if ln.counterparty_id:
            get(db, m.Entity, ln.counterparty_id)
            if ln.counterparty_id == entity_id:
                fail("counterparty", "Intercompany counterparty must be another entity")
        if sc.role in ("due_from", "due_to") and (
            not ln.counterparty_id or not ln.item
        ):
            fail(
                "intercompany_item",
                "Due From/To needs a counterparty and individual item",
            )
        if ln.fund_id:
            fund = get(db, m.Fund, ln.fund_id)
            beneficiary = get(db, m.Party, fund.beneficiary_id)
            if (
                sc.role in ("bank", "bridge", "custody_liability")
                and fund.holder_entity_id != entity_id
            ):
                fail("wrong_fund", "Fund belongs to another cash holder")
            if sc.role == "custody_asset" and beneficiary.entity_id != entity_id:
                fail("wrong_fund", "Custody asset belongs to a different beneficiary")
        if sc.pooled and not ln.fund_id:
            fail("missing_fund", "Pooled bank lines require fund attribution")
        if sc.role in ("custody_asset", "custody_liability") and not ln.fund_id:
            fail("missing_fund", "Custody controls require a fund")


def post(db, data, actor):
    reciprocal = defaultdict(int)
    cash_changes = defaultdict(int)
    custodial = defaultdict(int)
    fund_movement = defaultdict(int)
    for journal in data.journals:
        check_date(db, journal.entity_id, data.date)
        validate_lines(db, journal.entity_id, journal.lines)
        for ln in journal.lines:
            sc = scope(db, ln.account_id)
            amount = ln.debit_cents - ln.credit_cents
            if sc.role in ("due_from", "due_to"):
                pair = (
                    (journal.entity_id, ln.counterparty_id)
                    if sc.role == "due_from"
                    else (ln.counterparty_id, journal.entity_id)
                )
                reciprocal[(*pair, ln.item)] += amount
            if sc.role in ("custody_asset", "custody_liability"):
                fund = get(db, m.Fund, ln.fund_id)
                party = get(db, m.Party, fund.beneficiary_id)
                if party.entity_id:
                    custodial[ln.fund_id] += amount
            if ln.fund_id and sc.role in ("bank", "bridge", "custody_liability"):
                fund = get(db, m.Fund, ln.fund_id)
                party = get(db, m.Party, fund.beneficiary_id)
                if (
                    party.entity_id != journal.entity_id
                    and fund.purpose != "unresolved"
                ):
                    fund_movement[ln.fund_id] += amount
            if sc.role in ("bank", "bridge"):
                cash_changes[(ln.account_id, ln.fund_id)] += amount
    if any(reciprocal.values()):
        fail(
            "reciprocal_mismatch",
            "Intercompany entries must update both entities for the same item",
        )
    if any(fund_movement.values()):
        fail(
            "custody_coverage",
            "Beneficiary bank attribution must match the holder custody liability",
        )
    if any(custodial.values()):
        fail(
            "custody_mismatch",
            "Maintained beneficiary and holder must have equal custody changes",
        )
    for (account_id, fund_id), amount in cash_changes.items():
        sc = scope(db, account_id)
        if fund_id and amount < 0:
            fund = get(db, m.Fund, fund_id)
            if (
                fund.restriction != "unrestricted"
                and not data.release_reason
                and not data.observed
            ):
                fail(
                    "restricted_funds",
                    "Restricted funds require an explicit supported release",
                )
            if (
                balance(db, account_id, data.date, fund_id) + amount < 0
                and not data.observed
            ):
                fail(
                    "insufficient_funds",
                    "Fund has insufficient supported holdings; record explicit financing first",
                )
        if (
            sc.role == "bank"
            and amount < 0
            and balance(db, account_id, data.date) + amount < 0
            and not data.observed
        ):
            fail("insufficient_cash", "Planned payment exceeds supported bank cash")
    for adjustment in data.adjustments:
        if adjustment.entity_id not in {j.entity_id for j in data.journals}:
            fail("wrong_entity", "Basis adjustment is outside the event entity scope")
        check_date(db, adjustment.entity_id, adjustment.date)
        validate_lines(db, adjustment.entity_id, adjustment.lines)
    ordinal = db.info.get("portfolio_event_ordinal", 0)
    db.info["portfolio_event_ordinal"] = ordinal + 1
    event_code = uuid.uuid5(
        uuid.NAMESPACE_URL,
        str(db.info.get("portfolio_command_key", uuid.uuid4().hex))
        + ":"
        + str(ordinal),
    ).hex[:16]
    event = m.Event(
        code="RE-" + event_code,
        kind=data.kind,
        date=data.date,
        description=data.description,
        rationale=data.rationale,
        actor=actor,
        source=data.source,
        corrects_id=data.corrects_id,
    )
    if data.corrects_id:
        get(db, m.Event, data.corrects_id)
    db.add(event)
    db.flush()
    transactions = []
    db.info["portfolio_posting"] = True
    try:
        for journal in data.journals:
            txn = create_journal_entry(
                db,
                data.date,
                data.description,
                [
                    {
                        "account_id": ln.account_id,
                        "debit": Decimal(ln.debit_cents) / 100,
                        "credit": Decimal(ln.credit_cents) / 100,
                        "description": ln.description,
                    }
                    for ln in journal.lines
                ],
                source_type="re_" + data.kind[:40],
                source_id=event.id,
                reference=event.code,
            )
            db.flush()
            posted = (
                db.query(TransactionLine)
                .filter_by(transaction_id=txn.id)
                .order_by(TransactionLine.id)
                .all()
            )
            for record, ln in zip(posted, journal.lines, strict=True):
                db.add(
                    m.LineScope(
                        line_id=record.id,
                        **ln.model_dump(
                            include={
                                "property_id",
                                "unit_id",
                                "fund_id",
                                "counterparty_id",
                                "item",
                                "cash_flow",
                            }
                        ),
                    )
                )
            db.flush()
            db.add(
                m.JournalScope(
                    transaction_id=txn.id,
                    event_id=event.id,
                    entity_id=journal.entity_id,
                )
            )
            transactions.append(txn.id)
        for adjustment in data.adjustments:
            db.add(
                m.BasisAdjustment(
                    event_id=event.id,
                    entity_id=adjustment.entity_id,
                    date=adjustment.date,
                    reason=adjustment.reason,
                    lines=[ln.model_dump() for ln in adjustment.lines],
                )
            )
        for entry in data.management:
            scope(db, entry.account_id, entry.entity_id)
            if entry.entity_id not in {j.entity_id for j in data.journals}:
                fail("wrong_entity", "Management allocation is outside event scope")
            db.add(m.ManagementAllocation(event_id=event.id, **entry.model_dump()))
        db.flush()
    finally:
        db.info.pop("portfolio_posting", None)
    for (account_id, fund_id), amount in cash_changes.items():
        if (
            balance(db, account_id, data.date, fund_id) < 0
            and scope(db, account_id).role == "bank"
        ):
            db.add(
                m.ReviewItem(
                    code="actual_shortage",
                    entity_id=scope(db, account_id).entity_id,
                    event_id=event.id,
                    detail=f"Actual bank activity leaves account {account_id}"
                    + (f" fund {fund_id}" if fund_id else "")
                    + " with a shortage; verify coverage, opening balances and financing. Obligations remain owed.",
                )
            )
    return {"event_id": event.id, "code": event.code, "transaction_ids": transactions}


def import_bank(db, data, actor):
    sc = scope(db, data.account_id)
    if sc.role not in ("bank", "credit_card"):
        fail("not_bank", "Imports require a bank or credit card account")
    added = []
    duplicates = []
    occurrences = defaultdict(int)
    for number, row in enumerate(data.rows, 2):
        if row.amount_cents == 0:
            fail("zero_amount", f"Row {number} has no movement")
        fingerprint = digest(
            [
                row.date.isoformat(),
                row.amount_cents,
                row.description.strip(),
                row.reference,
            ]
        )
        occurrences[fingerprint] += 1
        key = (
            "id:" + row.external_id
            if row.external_id
            else f"{data.source_sha256}:{number}"
        )
        old = (
            db.query(m.BankObservation)
            .filter_by(account_id=data.account_id, source_key=key)
            .first()
        )
        if old:
            if old.fingerprint != fingerprint:
                fail(
                    "conflicting_source",
                    f"Bank identity {key} has conflicting contents",
                )
            duplicates.append(old.id)
            continue
        possible = (
            db.query(m.BankObservation)
            .filter_by(account_id=data.account_id, fingerprint=fingerprint)
            .all()
        )
        cross_file = [
            r for r in possible if r.source.get("sha256") != data.source_sha256
        ]
        obs = m.BankObservation(
            account_id=data.account_id,
            date=row.date,
            amount_cents=row.amount_cents,
            description=row.description,
            reference=row.reference,
            source_key=key,
            fingerprint=fingerprint,
            source={
                "file": data.source_name,
                "sha256": data.source_sha256,
                "row": number,
                "occurrence": occurrences[fingerprint],
            },
            status="possible_duplicate" if cross_file else "imported",
        )
        db.add(obs)
        db.flush()
        added.append(obs.id)
        if cross_file:
            db.add(
                m.ReviewItem(
                    code="possible_duplicate",
                    entity_id=sc.entity_id,
                    observation_id=obs.id,
                    detail=f"Overlapping export may repeat bank observations {[r.id for r in cross_file]}; confirm identity before posting.",
                )
            )
        if sc.coverage_start and row.date < sc.coverage_start:
            fail(
                "before_opening",
                f"Row {number} predates the account opening cutoff; rebuild the opening explicitly",
            )
    return {"observation_ids": added, "duplicate_ids": duplicates, "count": len(added)}


def unposted(db, ident, distinct=False):
    obs = get(db, m.BankObservation, ident)
    if obs.event_id:
        fail("already_posted", f"Bank observation {ident} is already posted")
    if obs.status == "possible_duplicate" and not distinct:
        fail(
            "possible_duplicate",
            "Confirm whether this is distinct activity before posting",
        )
    return obs


def post_bank(db, data, actor):
    obs = unposted(db, data.observation_id, data.duplicate_resolution == "distinct")
    sc = scope(db, obs.account_id)
    offset = data.offset_account_id or control(
        db,
        sc.entity_id,
        "suspense_credit" if obs.amount_cents > 0 else "suspense_debit",
    )
    target = scope(db, offset, sc.entity_id)
    if sc.pooled and data.fund_id:
        fund = get(db, m.Fund, data.fund_id)
        if (
            get(db, m.Party, fund.beneficiary_id).entity_id != sc.entity_id
            and fund.purpose != "unresolved"
        ):
            fail(
                "custody_workflow",
                "Use pooled activity to update beneficiary books and holder liabilities together",
            )
    if target.role in (
        "bank",
        "bridge",
        "due_from",
        "due_to",
        "custody_asset",
        "custody_liability",
        "payable",
        "receivable",
    ):
        fail(
            "special_workflow",
            "Use a linked settlement, transfer or custody journal for this account",
        )
    bankline = line(
        obs.account_id,
        obs.amount_cents,
        property_id=data.property_id,
        fund_id=data.fund_id,
        cash_flow=(
            None
            if target.role in ("suspense_debit", "suspense_credit")
            else target.cash_flow
        ),
    )
    offsetline = line(offset, -obs.amount_cents, property_id=data.property_id)
    management = []
    if get(db, Account, offset).account_type.value in ("income", "expense", "cogs"):
        management = [
            s.ManagementInput(
                entity_id=sc.entity_id,
                property_id=data.property_id,
                account_id=offset,
                date=obs.date,
                amount_cents=obs.amount_cents,
            )
        ]
    result = post(
        db,
        s.Posting(
            date=obs.date,
            description=obs.description or "Bank activity",
            rationale=data.rationale,
            kind="bank",
            source={"observation_id": obs.id},
            journals=[s.Journal(entity_id=sc.entity_id, lines=[bankline, offsetline])],
            management=management,
            observed=True,
        ),
        actor,
    )
    obs.event_id = result["event_id"]
    obs.status = "posted"
    if target.role.startswith("suspense"):
        db.add(
            m.ReviewItem(
                code="suspense",
                entity_id=sc.entity_id,
                event_id=obs.event_id,
                observation_id=obs.id,
                detail="Bank activity recorded; accounting classification needs clarification.",
            )
        )
    if data.property_id is None:
        db.add(
            m.ReviewItem(
                code="unallocated_property",
                entity_id=sc.entity_id,
                event_id=obs.event_id,
                observation_id=obs.id,
                detail="Property attribution is unallocated.",
            )
        )
    return result


def transfer_observation(db, ident):
    obs = get(db, m.BankObservation, ident)
    if not obs.event_id:
        return unposted(db, ident), obs.account_id, None
    review = (
        db.query(m.ReviewItem)
        .filter_by(observation_id=ident, code="suspense", status="open")
        .first()
    )
    if not review:
        fail(
            "already_posted",
            "Only unposted or unresolved suspense bank activity can be matched",
        )
    bank = scope(db, obs.account_id)
    if bank.pooled:
        fail(
            "pooled_transfer",
            "Resolve pooled custody through coordinated fund entries before matching",
        )
    offset = control(
        db,
        bank.entity_id,
        "suspense_credit" if obs.amount_cents > 0 else "suspense_debit",
    )
    return obs, offset, review


def finish_transfer_observation(db, obs, review, event_id, flow, evidence):
    if not review:
        return
    lines = (
        db.query(TransactionLine)
        .join(
            m.JournalScope,
            m.JournalScope.transaction_id == TransactionLine.transaction_id,
        )
        .filter(
            m.JournalScope.event_id == obs.event_id,
            TransactionLine.account_id == obs.account_id,
        )
        .all()
    )
    for ln in lines:
        db.add(
            m.CashClassification(
                line_id=ln.id,
                event_id=event_id,
                date=obs.date,
                cash_flow=flow,
                reason=evidence,
            )
        )
    review.status = "resolved"
    review.resolution = evidence
    review.resolved_event_id = event_id


def match_transfer(db, data, actor):
    out, out_offset, out_review = transfer_observation(db, data.withdrawal_id)
    inc, inc_offset, inc_review = transfer_observation(db, data.deposit_id)
    if (
        out.id == inc.id
        or out.amount_cents >= 0
        or inc.amount_cents <= 0
        or out.amount_cents + inc.amount_cents
    ):
        fail(
            "transfer_amount",
            "Transfer needs equal opposite bank movements; record fees separately",
        )
    a = scope(db, out.account_id)
    b = scope(db, inc.account_id)
    if a.role != "bank" or b.role != "bank" or out.account_id == inc.account_id:
        fail("transfer_account", "Choose two different bank accounts")
    if (a.entity_id == b.entity_id) != (data.purpose == "internal"):
        fail(
            "transfer_purpose",
            "Same-entity transfers are internal; cross-entity transfers require advance or repayment classification",
        )
    amount = inc.amount_cents
    result_ids = []
    # The sending entity's bridge carries cash in transit, even when the
    # receiving bank posts first (negative transit then signals a timing liability).
    bridge = control(db, a.entity_id, "bridge")
    dim_a = {
        "property_id": data.source_property_id,
        "fund_id": data.source_fund_id,
        "item": data.item,
    }
    dim_b = {
        "property_id": data.destination_property_id,
        "fund_id": data.destination_fund_id,
        "item": data.item,
    }
    if a.entity_id == b.entity_id and data.source_fund_id != data.destination_fund_id:
        fail(
            "fund_transfer", "An internal bank transfer must preserve the fund identity"
        )
    for obs, review, dims in [(out, out_review, dim_a), (inc, inc_review, dim_b)]:
        if review:
            originals = (
                db.query(m.LineScope)
                .join(TransactionLine, m.LineScope.line_id == TransactionLine.id)
                .join(
                    m.JournalScope,
                    m.JournalScope.transaction_id == TransactionLine.transaction_id,
                )
                .filter(
                    m.JournalScope.event_id == obs.event_id,
                    TransactionLine.account_id == obs.account_id,
                )
                .all()
            )
            if any(
                x.property_id != dims["property_id"] or x.fund_id != dims["fund_id"]
                for x in originals
            ):
                fail(
                    "transfer_dimensions",
                    "For previously posted suspense, preserve its source property and fund attribution when matching",
                )
    first = post(
        db,
        s.Posting(
            date=out.date,
            description="Transfer withdrawal: " + data.item,
            rationale=data.evidence,
            kind="transfer",
            observed=True,
            source={
                "observation_id": out.id,
                "paired_observation_id": inc.id,
                "counterparty_entity_id": b.entity_id,
                "purpose": data.purpose,
            },
            journals=[
                s.Journal(
                    entity_id=a.entity_id,
                    lines=[
                        line(
                            out_offset,
                            -amount,
                            cash_flow=(
                                "internal"
                                if a.entity_id == b.entity_id
                                else (
                                    "investing"
                                    if data.purpose == "advance"
                                    else "financing"
                                )
                            ),
                            **dim_a,
                        ),
                        line(bridge, amount, **dim_a),
                    ],
                )
            ],
        ),
        actor,
    )
    result_ids.append(first["event_id"])
    finish_transfer_observation(
        db,
        out,
        out_review,
        first["event_id"],
        (
            "internal"
            if a.entity_id == b.entity_id
            else "investing" if data.purpose == "advance" else "financing"
        ),
        data.evidence,
    )
    out.event_id = first["event_id"]
    out.status = "posted"
    if a.entity_id == b.entity_id:
        journals = [
            s.Journal(
                entity_id=a.entity_id,
                lines=[
                    line(inc_offset, amount, cash_flow="internal", **dim_b),
                    line(bridge, -amount, **dim_a),
                ],
            )
        ]
    else:
        ar_role, br_role = (
            ("due_from", "due_to")
            if data.purpose == "advance"
            else ("due_to", "due_from")
        )
        ac = control(db, a.entity_id, ar_role)
        bc = control(db, b.entity_id, br_role)
        if data.purpose == "repayment" and (
            -balance(db, ac, inc.date, item=data.item) < amount
            or balance(db, bc, inc.date, item=data.item) < amount
        ):
            fail(
                "overpayment",
                "Repayment exceeds the supported intercompany item balance",
            )
        journals = [
            s.Journal(
                entity_id=a.entity_id,
                lines=[
                    line(
                        ac,
                        amount,
                        counterparty_id=b.entity_id,
                        item=data.item,
                        property_id=data.source_property_id,
                    ),
                    line(bridge, -amount, **dim_a),
                ],
            ),
            s.Journal(
                entity_id=b.entity_id,
                lines=[
                    line(
                        inc_offset,
                        amount,
                        cash_flow=(
                            "financing" if data.purpose == "advance" else "investing"
                        ),
                        **dim_b,
                    ),
                    line(
                        bc,
                        -amount,
                        counterparty_id=a.entity_id,
                        item=data.item,
                        property_id=data.destination_property_id,
                    ),
                ],
            ),
        ]
    second = post(
        db,
        s.Posting(
            date=inc.date,
            description="Transfer deposit: " + data.item,
            rationale=data.evidence,
            kind="transfer",
            observed=True,
            source={
                "observation_id": inc.id,
                "paired_observation_id": out.id,
                "purpose": data.purpose,
                "counterparty_entity_id": a.entity_id,
            },
            journals=journals,
        ),
        actor,
    )
    result_ids.append(second["event_id"])
    finish_transfer_observation(
        db,
        inc,
        inc_review,
        second["event_id"],
        (
            "internal"
            if a.entity_id == b.entity_id
            else "financing" if data.purpose == "advance" else "investing"
        ),
        data.evidence,
    )
    inc.event_id = second["event_id"]
    inc.status = "posted"
    return {
        "event_ids": result_ids,
        "item": data.item,
        "posting_dates_differ": out.date != inc.date,
    }


def recognize(db, data, actor):
    scope(db, data.account_id, data.entity_id)
    ctrl = scope(db, data.control_id, data.entity_id)
    if ctrl.role != ("receivable" if data.kind == "income" else "payable"):
        fail(
            "control_mapping",
            "Recognition needs the matching receivable/payable control",
        )
    if (
        get(db, Account, data.account_id).account_type.value
        != {"income": "income", "expense": "expense", "asset": "asset"}[data.kind]
    ):
        fail("account_type", "Recognition category and account type disagree")
    signed = -data.amount_cents if data.kind == "income" else data.amount_cents
    lines = [
        line(data.account_id, signed, property_id=data.property_id, item=data.code),
        line(data.control_id, -signed, property_id=data.property_id, item=data.code),
    ]
    adjustments = []
    if data.kind != "asset":
        adjustments = [
            s.Adjustment(
                entity_id=data.entity_id,
                date=data.date,
                reason="Remove unpaid operating recognition from modified cash",
                lines=[
                    line(
                        ln.account_id,
                        ln.credit_cents - ln.debit_cents,
                        property_id=ln.property_id,
                        item=data.code,
                    )
                    for ln in lines
                ],
            )
        ]
    result = post(
        db,
        s.Posting(
            date=data.date,
            description=data.description,
            rationale=data.evidence,
            kind="recognition",
            journals=[s.Journal(entity_id=data.entity_id, lines=lines)],
            adjustments=adjustments,
        ),
        actor,
    )
    item = m.OpenItem(
        event_id=result["event_id"],
        **data.model_dump(exclude={"description", "evidence"}),
    )
    db.add(item)
    db.flush()
    return {**result, "item_id": item.id}


def settle(db, data, actor, returned=False):
    item = get(db, m.OpenItem, data.item_id)
    obs = unposted(db, data.observation_id)
    scope(db, obs.account_id, item.entity_id)
    expected = 1 if item.kind == "income" else -1
    if returned:
        expected = -expected
    if obs.amount_cents * expected <= 0:
        fail(
            "settlement_direction",
            "Bank movement has the wrong direction for this item",
        )
    amount = abs(obs.amount_cents)
    settled = (
        db.query(func.coalesce(func.sum(m.ItemApplication.amount_cents), 0))
        .join(m.Event, m.Event.id == m.ItemApplication.event_id)
        .filter(m.ItemApplication.item_id == item.id, m.Event.date <= obs.date)
        .scalar()
    )
    if amount > (settled if returned else item.amount_cents - settled):
        fail(
            "overpayment",
            (
                "Return exceeds prior settlements"
                if returned
                else "Payment exceeds remaining recognized item"
            ),
        )
    if obs.date < item.date:
        fail(
            "settlement_date",
            "Use a supported advance/prepayment workflow for settlement before recognition",
        )
    bank = line(
        obs.account_id,
        obs.amount_cents,
        property_id=item.property_id,
        fund_id=data.fund_id,
        cash_flow=scope(db, item.account_id).cash_flow,
    )
    ctrl = line(
        item.control_id, -obs.amount_cents, property_id=item.property_id, item=item.code
    )
    adj = []
    management = []
    if item.kind != "asset":
        adj = [
            s.Adjustment(
                entity_id=item.entity_id,
                date=obs.date,
                reason="Recognize supported settled portion on modified cash basis",
                lines=[
                    line(
                        item.account_id,
                        -obs.amount_cents,
                        property_id=item.property_id,
                        item=item.code,
                    ),
                    line(
                        item.control_id,
                        obs.amount_cents,
                        property_id=item.property_id,
                        item=item.code,
                    ),
                ],
            )
        ]
        management = [
            s.ManagementInput(
                entity_id=item.entity_id,
                property_id=item.property_id,
                account_id=item.account_id,
                date=obs.date,
                amount_cents=obs.amount_cents,
            )
        ]
    result = post(
        db,
        s.Posting(
            date=obs.date,
            description=("Return settlement " if returned else "Settle ") + item.code,
            rationale=data.evidence,
            kind="settlement_return" if returned else "settlement",
            source={"observation_id": obs.id, "item_id": item.id},
            journals=[s.Journal(entity_id=item.entity_id, lines=[bank, ctrl])],
            adjustments=adj,
            management=management,
            observed=True,
        ),
        actor,
    )
    db.add(
        m.ItemApplication(
            item_id=item.id,
            event_id=result["event_id"],
            amount_cents=-amount if returned else amount,
        )
    )
    obs.event_id = result["event_id"]
    obs.status = "posted"
    return result


def correct_suspense(db, data, actor):
    review = get(db, m.ReviewItem, data.review_id)
    if review.code != "suspense" or review.status != "open":
        fail("review_state", "Choose an open suspense item")
    obs = get(db, m.BankObservation, review.observation_id)
    sc = scope(db, obs.account_id)
    target = scope(db, data.account_id, sc.entity_id)
    if target.role in (
        "bank",
        "bridge",
        "due_to",
        "due_from",
        "custody_asset",
        "custody_liability",
        "receivable",
        "payable",
    ) or target.role.startswith("suspense"):
        fail(
            "special_workflow",
            "Use a source-linked settlement or transfer for this classification",
        )
    if data.date < obs.date:
        fail("correction_date", "Correction cannot precede its original bank event")
    suspense = control(
        db,
        sc.entity_id,
        "suspense_credit" if obs.amount_cents > 0 else "suspense_debit",
    )
    original = (
        db.query(TransactionLine, m.LineScope)
        .join(
            m.JournalScope,
            m.JournalScope.transaction_id == TransactionLine.transaction_id,
        )
        .join(m.LineScope, m.LineScope.line_id == TransactionLine.id)
        .filter(m.JournalScope.event_id == obs.event_id)
        .all()
    )
    suspense_dim = next(dim for ln, dim in original if ln.account_id == suspense)
    entries = [
        line(suspense, obs.amount_cents, property_id=suspense_dim.property_id),
        line(data.account_id, -obs.amount_cents, property_id=data.property_id),
    ]
    management = []
    adjustments = []
    if get(db, Account, data.account_id).account_type.value in (
        "income",
        "expense",
        "cogs",
    ):
        check_date(db, sc.entity_id, obs.date)
        management = [
            s.ManagementInput(
                entity_id=sc.entity_id,
                property_id=data.property_id,
                account_id=data.account_id,
                date=obs.date,
                amount_cents=obs.amount_cents,
            )
        ]
        if data.date != obs.date:
            adjustments = [
                s.Adjustment(
                    entity_id=sc.entity_id,
                    date=obs.date,
                    reason="Classify settled activity at its supported bank posting date",
                    lines=entries,
                ),
                s.Adjustment(
                    entity_id=sc.entity_id,
                    date=data.date,
                    reason="Prevent duplicate cash-profile classification on correction date",
                    lines=[
                        line(
                            x.account_id,
                            x.credit_cents - x.debit_cents,
                            property_id=x.property_id,
                        )
                        for x in entries
                    ],
                ),
            ]
    result = post(
        db,
        s.Posting(
            date=data.date,
            description="Resolve bank classification",
            rationale=data.evidence,
            kind="correction",
            corrects_id=obs.event_id,
            journals=[s.Journal(entity_id=sc.entity_id, lines=entries)],
            adjustments=adjustments,
            management=management,
            source={"observation_id": obs.id},
        ),
        actor,
    )
    for ln, dim in original:
        if ln.account_id == obs.account_id:
            db.add(
                m.CashClassification(
                    line_id=ln.id,
                    event_id=result["event_id"],
                    date=data.date,
                    cash_flow=target.cash_flow,
                    reason=data.evidence,
                )
            )
    review.status = "resolved"
    review.resolution = data.evidence
    review.resolved_event_id = result["event_id"]
    return result


def attach_document(db, data, actor):
    from urllib.parse import urlsplit

    parsed = urlsplit(data.locator)
    if parsed.scheme and parsed.scheme not in ("https", "file"):
        fail("document_locator", "Use a Dropbox HTTPS link or local file path")
    if parsed.username or parsed.password:
        fail("document_locator", "Do not store credentials in document links")
    doc = db.query(m.Document).filter_by(code=data.code).first()
    if not doc:
        doc = m.Document(code=data.code, title=data.title, provider_id=data.provider_id)
        db.add(doc)
        db.flush()
    elif data.provider_id and doc.provider_id != data.provider_id:
        fail(
            "document_identity",
            "A document code cannot be reassigned to another provider file",
        )
    db.add(
        m.DocumentLocation(document_id=doc.id, locator=data.locator, sha256=data.sha256)
    )
    if data.event_id:
        get(db, m.Event, data.event_id)
        db.add(
            m.DocumentLink(document_id=doc.id, event_id=data.event_id, page=data.page)
        )
    return {"document_id": doc.id, "content_verified": False}


def reconcile(db, data, actor):
    scope(db, data.account_id)
    if data.end < data.start:
        fail("date_range", "Statement end precedes start")
    if data.document_id:
        get(db, m.Document, data.document_id)
    rows = (
        db.query(m.BankObservation)
        .filter(
            m.BankObservation.account_id == data.account_id,
            m.BankObservation.date >= data.start,
            m.BankObservation.date <= data.end,
        )
        .all()
    )
    active = [r for r in rows if r.status != "duplicate"]
    unposted_ids = [r.id for r in active if not r.event_id]
    difference = (
        data.ending_cents - data.opening_cents - sum(r.amount_cents for r in active)
    )
    book = balance(db, data.account_id, data.end)
    if difference or unposted_ids or book != data.ending_cents:
        fail(
            "reconciliation_difference",
            f"Statement difference {difference} cents; unposted observations {unposted_ids}; book versus statement {book-data.ending_cents} cents",
        )
    row = m.Reconciliation(
        **data.model_dump(),
        actor=actor,
        result={
            "difference_cents": 0,
            "book_cents": book,
            "observation_ids": [r.id for r in active],
        },
    )
    db.add(row)
    db.flush()
    return {"reconciliation_id": row.id, **row.result}


def close_period(db, data, actor):
    entity = get(db, m.Entity, data.entity_id)
    if entity.closed_through and data.through <= entity.closed_through:
        fail("period_order", "New close must follow the existing close")
    observations = (
        db.query(m.BankObservation)
        .join(m.AccountScope, m.AccountScope.account_id == m.BankObservation.account_id)
        .filter(
            m.AccountScope.entity_id == entity.id,
            m.BankObservation.date <= data.through,
            m.BankObservation.event_id.is_(None),
            m.BankObservation.status != "duplicate",
        )
        .count()
    )
    if observations:
        fail(
            "unposted_activity", "Post or resolve imported bank activity before closing"
        )
    critical = (
        db.query(m.ReviewItem)
        .filter(
            m.ReviewItem.entity_id == entity.id,
            m.ReviewItem.status == "open",
            m.ReviewItem.code.in_(["suspense", "actual_shortage"]),
        )
        .count()
    )
    if critical:
        fail("unresolved_accounting", "Resolve suspense and shortages before closing")
    for bank in db.query(m.AccountScope).filter_by(entity_id=entity.id, role="bank"):
        if bank.coverage_start and bank.coverage_start > data.through:
            continue
        reconciled = (
            db.query(m.Reconciliation)
            .filter(
                m.Reconciliation.account_id == bank.account_id,
                m.Reconciliation.start <= data.through,
                m.Reconciliation.end >= data.through,
            )
            .order_by(m.Reconciliation.id.desc())
            .first()
        )
        if (
            not reconciled
            or balance(db, bank.account_id, reconciled.end) != reconciled.ending_cents
        ):
            fail(
                "unreconciled_bank",
                f"Reconcile bank account {bank.account_id} through the close date first",
            )
    entity.closed_through = data.through
    return {
        "entity_id": entity.id,
        "closed_through": data.through.isoformat(),
        "reason": data.reason,
    }


def reopen_period(db, data, actor):
    entity = get(db, m.Entity, data.entity_id)
    if entity.closed_through is None or data.through >= entity.closed_through:
        fail("period_order", "Reopening must move the close date earlier")
    old = entity.closed_through
    entity.closed_through = data.through
    return {
        "entity_id": entity.id,
        "previous_close": old.isoformat(),
        "closed_through": data.through.isoformat(),
        "reason": data.reason,
        "issued_reports_require_review": True,
    }


def create_schedule(db, data, actor):
    get(db, m.Entity, data.entity_id)
    if data.property_id:
        get(db, m.Property, data.property_id)
    if data.supersedes_id:
        prior = get(db, m.Schedule, data.supersedes_id)
        if (
            prior.entity_id != data.entity_id
            or prior.kind != data.kind
            or prior.property_id != data.property_id
        ):
            fail(
                "schedule_scope",
                "Schedule revision must retain entity, property and kind",
            )
        if db.query(m.Schedule).filter_by(supersedes_id=prior.id).first():
            fail("superseded_schedule", "Revise the current schedule version")
    row = m.Schedule(**data.model_dump())
    db.add(row)
    db.flush()
    return {"schedule_id": row.id, "posted": False}


def post_schedule(db, data, actor):
    schedule = get(db, m.Schedule, data.schedule_id)
    if db.query(m.Schedule).filter_by(supersedes_id=schedule.id).first():
        fail("superseded_schedule", "Post against the current schedule revision")
    if any(j.entity_id != schedule.entity_id for j in data.posting.journals):
        fail("schedule_scope", "Posting is outside schedule entity")
    if (
        db.query(m.SchedulePosting)
        .filter_by(schedule_id=schedule.id, line_key=data.line_key)
        .first()
    ):
        fail("schedule_duplicate", "This schedule line has already posted")
    posting = data.posting.model_copy(
        update={
            "source": {
                **data.posting.source,
                "schedule_id": schedule.id,
                "schedule_line": data.line_key,
            }
        }
    )
    result = post(db, posting, actor)
    db.add(
        m.SchedulePosting(
            schedule_id=schedule.id, line_key=data.line_key, event_id=result["event_id"]
        )
    )
    return result


OPERATIONS = {name: globals()[name] for name in s.CONTRACTS if name in globals()}


def execute(db, command, actor="codex"):
    command = s.Command.model_validate(command)
    if command.operation not in s.CONTRACTS:
        fail("unsupported_operation", "Unknown portfolio operation")
    request_hash = digest(command.model_dump(mode="json", exclude={"key"}))
    old = db.get(m.RequestRecord, command.key)
    if old:
        if old.digest != request_hash:
            fail(
                "idempotency_conflict", "This retry key was used for different contents"
            )
        return {**old.result, "replayed": True}
    data = s.CONTRACTS[command.operation].model_validate(command.data)
    with db.begin_nested():
        db.info["portfolio_command_key"] = command.key
        db.info["portfolio_event_ordinal"] = 0
        try:
            result = OPERATIONS[command.operation](db, data, actor)
        finally:
            db.info.pop("portfolio_command_key", None)
            db.info.pop("portfolio_event_ordinal", None)
        db.flush()
        for entity in db.query(m.Entity).all():
            entity.revision += 1
        result = {
            "committed": True,
            "operation": command.operation,
            "key": command.key,
            **result,
        }
        db.add(
            m.RequestRecord(
                key=command.key,
                digest=request_hash,
                operation=command.operation,
                result=result,
                actor=actor,
            )
        )
        db.flush()
    return result


def resolve_duplicate(db, data, actor):
    obs = unposted(db, data.observation_id, True)
    original = get(db, m.BankObservation, data.original_id)
    if (
        obs.id == original.id
        or obs.account_id != original.account_id
        or obs.fingerprint != original.fingerprint
    ):
        fail(
            "duplicate_identity",
            "Duplicate resolution requires the same account and bank facts",
        )
    obs.status = "duplicate"
    for review in db.query(m.ReviewItem).filter_by(
        observation_id=obs.id, status="open"
    ):
        review.status = "resolved"
        review.resolution = f"Duplicate of observation {original.id}: {data.evidence}"
    return {"observation_id": obs.id, "duplicate_of": original.id, "posted": False}


OPERATIONS["resolve_duplicate"] = resolve_duplicate


def pool_activity(db, data, actor):
    obs = unposted(db, data.observation_id)
    bank = scope(db, obs.account_id)
    fund = get(db, m.Fund, data.fund_id)
    party = get(db, m.Party, fund.beneficiary_id)
    if not bank.pooled or bank.entity_id != fund.holder_entity_id:
        fail("pool_account", "Fund must belong to this pooled bank holder")
    if party.entity_id == bank.entity_id:
        fail("own_funds", "Use ordinary bank posting for the holder own funds")
    amount = obs.amount_cents
    prop = data.property_id or fund.property_id
    held = balance(db, obs.account_id, obs.date, fund.id)
    if amount < 0 and fund.restriction != "unrestricted" and not data.release_reason:
        fail(
            "restricted_funds",
            "Identify the supported release before classifying restricted money",
        )
    advance = max(0, -amount - held) if amount < 0 else 0
    if advance and (
        not data.advance_from_fund_id or not data.advance_item or not party.entity_id
    ):
        fail(
            "fund_shortfall",
            "This classification exceeds beneficiary funds; identify actual financing or record the observed withdrawal to unresolved suspense",
        )
    if not party.entity_id and data.offset_account_id:
        fail("external_books", "Do not invent books for an external beneficiary")
    if party.entity_id and not data.offset_account_id:
        fail(
            "beneficiary_account",
            "Choose the beneficiary income, expense, liability or other supported offset",
        )
    if data.offset_account_id:
        target = scope(db, data.offset_account_id, party.entity_id)
        if target.role in (
            "bank",
            "bridge",
            "due_from",
            "due_to",
            "custody_asset",
            "custody_liability",
            "receivable",
            "payable",
        ):
            fail(
                "special_workflow",
                "This offset needs its own linked settlement or financing workflow",
            )
    custody_change = amount + advance
    holder_lines = []
    if custody_change:
        holder_lines = [
            line(
                obs.account_id,
                custody_change,
                fund_id=fund.id,
                property_id=prop,
                cash_flow="operating",
            ),
            line(
                control(db, bank.entity_id, "custody_liability"),
                -custody_change,
                fund_id=fund.id,
                property_id=prop,
            ),
        ]
    recipient_lines = []
    if party.entity_id:
        recipient_lines = [
            line(
                data.offset_account_id, -amount, property_id=prop, unit_id=fund.unit_id
            )
        ]
        if custody_change:
            recipient_lines.append(
                line(
                    control(db, party.entity_id, "custody_asset"),
                    custody_change,
                    fund_id=fund.id,
                    property_id=prop,
                )
            )
    if advance:
        source = get(db, m.Fund, data.advance_from_fund_id)
        owner = get(db, m.Party, source.beneficiary_id)
        if (
            source.holder_entity_id != bank.entity_id
            or owner.entity_id != bank.entity_id
            or source.restriction != "unrestricted"
        ):
            fail(
                "advance_source",
                "Advance must use identified unrestricted holder funds",
            )
        if balance(db, obs.account_id, obs.date, source.id) < advance:
            fail(
                "advance_source",
                "Identified source does not have enough cash for the advance",
            )
        holder_lines += [
            line(
                obs.account_id,
                -advance,
                fund_id=source.id,
                property_id=prop,
                cash_flow="investing",
            ),
            line(
                control(db, bank.entity_id, "due_from"),
                advance,
                counterparty_id=party.entity_id,
                item=data.advance_item,
                property_id=prop,
            ),
        ]
        recipient_lines.append(
            line(
                control(db, party.entity_id, "due_to"),
                -advance,
                counterparty_id=bank.entity_id,
                item=data.advance_item,
                property_id=prop,
            )
        )
    journals = [s.Journal(entity_id=bank.entity_id, lines=holder_lines)]
    management = []
    if recipient_lines:
        journals.append(s.Journal(entity_id=party.entity_id, lines=recipient_lines))
        if get(db, Account, data.offset_account_id).account_type.value in (
            "income",
            "expense",
            "cogs",
        ):
            management = [
                s.ManagementInput(
                    entity_id=party.entity_id,
                    property_id=prop,
                    account_id=data.offset_account_id,
                    date=obs.date,
                    amount_cents=amount,
                )
            ]
    result = post(
        db,
        s.Posting(
            date=obs.date,
            description=obs.description or "Pooled bank activity",
            rationale=data.evidence,
            kind="custody",
            source={
                "observation_id": obs.id,
                "fund_id": fund.id,
                "advance_item": data.advance_item,
            },
            observed=True,
            release_reason=data.release_reason,
            journals=journals,
            management=management,
        ),
        actor,
    )
    obs.event_id = result["event_id"]
    obs.status = "posted"
    return {**result, "advance_cents": advance}


OPERATIONS["pool_activity"] = pool_activity


def mortgage_payment(db, data, actor):
    obs = unposted(db, data.observation_id)
    sc = scope(db, obs.account_id)
    if obs.amount_cents >= 0:
        fail("payment_direction", "A mortgage payment must be a bank withdrawal")
    components = [
        ("principal", data.principal_cents, data.debt_account_id, {"debt"}),
        (
            "interest",
            data.interest_cents,
            data.interest_account_id,
            {"expense"},
        ),
        ("escrow", data.escrow_cents, data.escrow_account_id, {"escrow"}),
        ("fee", data.fee_cents, data.fee_account_id, {"expense", "asset", "project"}),
    ]
    if sum(x[1] for x in components) != -obs.amount_cents:
        fail(
            "payment_components",
            "Principal, interest, escrow and fees must equal the bank withdrawal",
        )
    lines = []
    management = []
    for kind, amount, ident, roles in components:
        if not amount:
            continue
        if not ident:
            fail("payment_mapping", f"Choose a {kind} account")
        target = scope(db, ident, sc.entity_id)
        if target.role not in roles:
            fail("payment_mapping", f"Incorrect {kind} account role")
        if (
            kind == "principal"
            and -balance(
                db, ident, obs.date, item=data.instrument, property_id=data.property_id
            )
            < amount
        ):
            fail(
                "principal_balance",
                "Principal payment exceeds recorded debt; establish supported opening debt first",
            )
        flow = "financing" if kind == "principal" else target.cash_flow
        lines.extend(
            [
                line(ident, amount, property_id=data.property_id, item=data.instrument),
                line(
                    obs.account_id,
                    -amount,
                    property_id=data.property_id,
                    item=data.instrument,
                    cash_flow=flow,
                ),
            ]
        )
        if target.role == "expense":
            management.append(
                s.ManagementInput(
                    entity_id=sc.entity_id,
                    property_id=data.property_id,
                    account_id=ident,
                    date=obs.date,
                    amount_cents=-amount,
                )
            )
    result = post(
        db,
        s.Posting(
            date=obs.date,
            description="Mortgage payment " + data.instrument,
            rationale=data.evidence,
            kind="debt_payment",
            source={
                "observation_id": obs.id,
                "instrument": data.instrument,
                "components": {x[0]: x[1] for x in components},
            },
            observed=True,
            journals=[s.Journal(entity_id=sc.entity_id, lines=lines)],
            management=management,
        ),
        actor,
    )
    obs.event_id = result["event_id"]
    obs.status = "posted"
    return result


def transfer_property_allocation(db, data, actor):
    sc = scope(db, data.account_id, data.entity_id)
    if sc.pooled:
        fail(
            "fund_allocation",
            "Pooled allocations require explicit fund-level journal lines",
        )
    if data.source_property_id == data.destination_property_id:
        fail("same_property", "Choose different property segments")
    for ident in (data.source_property_id, data.destination_property_id):
        get(db, m.Property, ident)
    query = (
        db.query(TransactionLine)
        .join(m.LineScope, m.LineScope.line_id == TransactionLine.id)
        .join(Transaction, Transaction.id == TransactionLine.transaction_id)
        .filter(
            TransactionLine.account_id == data.account_id,
            m.LineScope.property_id == data.source_property_id,
            Transaction.date <= data.date,
        )
    )
    available = sum(cents(r.debit) - cents(r.credit) for r in query)
    if data.amount_cents > available:
        fail(
            "allocation_balance",
            "Transfer exceeds the identified source property asset balance",
        )
    existing = (
        db.query(m.AccountScope)
        .filter_by(entity_id=data.entity_id, code="segment_investment")
        .first()
    )
    net = (
        existing.account_id
        if existing
        else create_account(
            db,
            s.AccountInput(
                entity_id=data.entity_id,
                code="segment_investment",
                name="Allocated property net investment",
                account_type="equity",
                role="equity",
            ),
            actor,
        )["account_id"]
    )
    n = data.amount_cents
    return post(
        db,
        s.Posting(
            date=data.date,
            description="Property resource allocation",
            rationale=data.evidence,
            kind="segment_transfer",
            journals=[
                s.Journal(
                    entity_id=data.entity_id,
                    lines=[
                        line(
                            data.account_id,
                            -n,
                            property_id=data.source_property_id,
                            cash_flow="financing",
                        ),
                        line(net, n, property_id=data.source_property_id),
                        line(
                            data.account_id,
                            n,
                            property_id=data.destination_property_id,
                            cash_flow="financing",
                        ),
                        line(net, -n, property_id=data.destination_property_id),
                    ],
                )
            ],
        ),
        actor,
    )


def depreciate(db, data, actor):
    schedule = get(db, m.Schedule, data.schedule_id)
    if schedule.kind != "asset":
        fail("asset_schedule", "Choose an approved asset schedule")
    inputs = schedule.inputs
    if inputs.get("asset_kind") == "land":
        fail("nondepreciable", "Land is not depreciable")
    cost = inputs.get("cost_cents")
    salvage = inputs.get("salvage_cents", 0)
    if type(cost) is not int or type(salvage) is not int or cost < salvage:
        fail(
            "asset_inputs",
            "Asset schedule needs supported integer-cent cost and salvage values",
        )
    if not inputs.get("available_for_use") or data.date < date.fromisoformat(
        inputs["available_for_use"]
    ):
        fail(
            "asset_date",
            "Depreciation cannot precede the supported available-for-use date",
        )
    exp = scope(db, data.expense_account_id, schedule.entity_id)
    acc = scope(db, data.accumulated_account_id, schedule.entity_id)
    if exp.role != "expense" or acc.role != "contra_asset":
        fail(
            "asset_mapping",
            "Choose depreciation expense and accumulated depreciation accounts",
        )
    lineage = [schedule.id]
    prior = schedule
    while prior.supersedes_id:
        prior = get(db, m.Schedule, prior.supersedes_id)
        lineage.append(prior.id)
    previous = (
        db.query(m.SchedulePosting)
        .filter(m.SchedulePosting.schedule_id.in_(lineage))
        .all()
    )
    used = 0
    for posting in previous:
        event = get(db, m.Event, posting.event_id)
        if event.kind == "depreciation" and event.date == data.date:
            fail(
                "schedule_duplicate",
                "Depreciation for this date already posted in the schedule history",
            )
        used += event.source.get("depreciation_cents", 0)
    if used + data.amount_cents > cost - salvage:
        fail(
            "depreciation_limit", "Depreciation exceeds the remaining depreciable basis"
        )
    posting = s.Posting(
        date=data.date,
        description="Depreciation " + schedule.code,
        rationale=data.evidence,
        kind="depreciation",
        source={"depreciation_cents": data.amount_cents},
        journals=[
            s.Journal(
                entity_id=schedule.entity_id,
                lines=[
                    line(
                        data.expense_account_id,
                        data.amount_cents,
                        property_id=schedule.property_id,
                        item=schedule.code,
                    ),
                    line(
                        data.accumulated_account_id,
                        -data.amount_cents,
                        property_id=schedule.property_id,
                        item=schedule.code,
                    ),
                ],
            )
        ],
    )
    return post_schedule(
        db,
        s.SchedulePostInput(
            schedule_id=schedule.id,
            line_key="depreciation:" + data.date.isoformat(),
            posting=posting,
        ),
        actor,
    )


def release_project_cost(db, data, actor):
    project = scope(db, data.project_account_id, data.entity_id)
    cost = scope(db, data.cost_of_sales_account_id, data.entity_id)
    if project.role != "project" or cost.role != "cogs":
        fail("project_mapping", "Choose project asset and cost-of-sales accounts")
    available = balance(
        db,
        data.project_account_id,
        data.date,
        item=data.project_item,
        property_id=data.property_id,
    )
    if available < data.amount_cents:
        fail("project_basis", "Release exceeds the supported project item basis")
    return post(
        db,
        s.Posting(
            date=data.date,
            description="Release sold project cost " + data.project_item,
            rationale=data.evidence,
            kind="project_release",
            journals=[
                s.Journal(
                    entity_id=data.entity_id,
                    lines=[
                        line(
                            data.cost_of_sales_account_id,
                            data.amount_cents,
                            property_id=data.property_id,
                            item=data.project_item,
                        ),
                        line(
                            data.project_account_id,
                            -data.amount_cents,
                            property_id=data.property_id,
                            item=data.project_item,
                        ),
                    ],
                )
            ],
        ),
        actor,
    )


OPERATIONS.update(
    {
        name: globals()[name]
        for name in (
            "mortgage_payment",
            "transfer_property_allocation",
            "depreciate",
            "release_project_cost",
        )
    }
)


def service_accounts(db, entity, asset, result):
    sc = scope(db, asset, entity)
    target = scope(db, result, entity)
    if (sc.role, target.role) not in [("prepaid", "expense"), ("deferred", "income")]:
        fail(
            "service_mapping",
            "Choose prepaid operating service/expense or advance service consideration/income; refundable deposits use deposit liabilities",
        )
    return sc.role


def service_payment(db, data, actor):
    obs = unposted(db, data.observation_id)
    bank = scope(db, obs.account_id)
    role = service_accounts(
        db, bank.entity_id, data.balance_account_id, data.result_account_id
    )
    if (role == "prepaid") != (obs.amount_cents < 0):
        fail(
            "service_direction",
            "Prepayments require withdrawals; advance service receipts require deposits",
        )
    dims = {"property_id": data.property_id, "item": data.item}
    change = -obs.amount_cents
    journal = s.Journal(
        entity_id=bank.entity_id,
        lines=[
            line(obs.account_id, obs.amount_cents, cash_flow="operating", **dims),
            line(data.balance_account_id, change, **dims),
        ],
    )
    adj = s.Adjustment(
        entity_id=bank.entity_id,
        date=obs.date,
        reason="Recognize operating service on bank settlement in modified cash profile",
        lines=[
            line(data.balance_account_id, -change, **dims),
            line(data.result_account_id, change, **dims),
        ],
    )
    result = post(
        db,
        s.Posting(
            date=obs.date,
            kind="service_payment",
            description="Operating service settlement " + data.item,
            rationale=data.evidence,
            observed=True,
            source={"observation_id": obs.id},
            journals=[journal],
            adjustments=[adj],
            management=[
                s.ManagementInput(
                    entity_id=bank.entity_id,
                    date=obs.date,
                    property_id=data.property_id,
                    account_id=data.result_account_id,
                    amount_cents=obs.amount_cents,
                )
            ],
        ),
        actor,
    )
    obs.event_id = result["event_id"]
    obs.status = "posted"
    return result


def service_recognition(db, data, actor):
    role = service_accounts(
        db, data.entity_id, data.balance_account_id, data.result_account_id
    )
    held = balance(db, data.balance_account_id, data.date, item=data.item)
    if data.amount_cents > abs(held) or (held > 0) != (role == "prepaid"):
        fail(
            "service_balance",
            "Recognition exceeds the supported remaining service balance",
        )
    value = data.amount_cents if role == "prepaid" else -data.amount_cents
    dims = {"property_id": data.property_id, "item": data.item}
    entries = [
        line(data.balance_account_id, -value, **dims),
        line(data.result_account_id, value, **dims),
    ]
    reverse = [
        line(data.balance_account_id, value, **dims),
        line(data.result_account_id, -value, **dims),
    ]
    return post(
        db,
        s.Posting(
            date=data.date,
            kind="service_recognition",
            description="Operating service recognition " + data.item,
            rationale=data.evidence,
            journals=[s.Journal(entity_id=data.entity_id, lines=entries)],
            adjustments=[
                s.Adjustment(
                    entity_id=data.entity_id,
                    date=data.date,
                    reason="Prevent duplicate service result already recognized on settlement",
                    lines=reverse,
                )
            ],
        ),
        actor,
    )


OPERATIONS.update(
    service_payment=service_payment, service_recognition=service_recognition
)


def end_ownership(db, data, actor):
    row = get(db, m.Ownership, data.ownership_id)
    if row.end is not None:
        fail(
            "ownership_closed",
            "This ownership interval already has an end date; preserve its history",
        )
    if data.end <= row.start:
        fail(
            "date_range", "End date must follow the beginning of the ownership interval"
        )
    row.end = data.end
    return {
        "ownership_id": row.id,
        "ended": data.end.isoformat(),
        "evidence": data.evidence,
    }


def gross_closing(db, data, actor):
    obs = unposted(db, data.observation_id)
    bank = scope(db, obs.account_id)
    schedule = get(db, m.Schedule, data.schedule_id)
    if schedule.kind != "closing" or schedule.entity_id != bank.entity_id:
        fail("closing_scope", "Choose a closing schedule for the bank-owning entity")
    cash = sum(
        x.debit_cents - x.credit_cents
        for x in data.lines
        if x.account_id == obs.account_id
    )
    if cash != obs.amount_cents:
        fail(
            "closing_net",
            "Gross sources and uses must reconcile exactly to the observed net bank settlement",
        )
    for ln in data.lines:
        sc = scope(db, ln.account_id, bank.entity_id)
        if sc.role == "bank" and ln.account_id != obs.account_id:
            fail(
                "closing_cash",
                "Record each other bank movement from its own supported settlement",
            )
        if ln.account_id == obs.account_id and ln.cash_flow is None:
            fail(
                "closing_classification",
                "Classify each bank portion by supported operating, investing or financing purpose",
            )
    posting = s.Posting(
        date=obs.date,
        description="Gross closing " + schedule.code,
        rationale=data.evidence,
        kind="closing",
        observed=True,
        source={"observation_id": obs.id},
        journals=[s.Journal(entity_id=bank.entity_id, lines=data.lines)],
    )
    result = post_schedule(
        db,
        s.SchedulePostInput(
            schedule_id=schedule.id, line_key="closing:" + str(obs.id), posting=posting
        ),
        actor,
    )
    obs.event_id = result["event_id"]
    obs.status = "posted"
    return result


def resolve_review(db, data, actor):
    row = get(db, m.ReviewItem, data.review_id)
    if row.status != "open":
        fail("review_state", "This review item is already resolved")
    if row.code in ("suspense", "possible_duplicate"):
        fail(
            "specific_resolution",
            "Use the accounting correction or duplicate-resolution workflow; shortages must be restored and reconciled before resolution",
        )
    if row.code == "actual_shortage":
        if not data.corrective_event_id:
            fail(
                "shortage_evidence", "Identify the supported entry that restored funds"
            )
        correction = get(db, m.Event, data.corrective_event_id)
        if (
            not db.query(m.JournalScope)
            .filter_by(event_id=correction.id, entity_id=row.entity_id)
            .first()
        ):
            fail(
                "wrong_entity",
                "The correcting entry must belong to the reviewed entity",
            )
        holdings = (
            db.query(TransactionLine.account_id, m.LineScope.fund_id)
            .join(m.LineScope, m.LineScope.line_id == TransactionLine.id)
            .join(
                m.JournalScope,
                m.JournalScope.transaction_id == TransactionLine.transaction_id,
            )
            .join(
                m.AccountScope, m.AccountScope.account_id == TransactionLine.account_id
            )
            .filter(
                m.JournalScope.event_id == row.event_id, m.AccountScope.role == "bank"
            )
            .distinct()
            .all()
        )
        for bank_id, fund_id in holdings:
            recon = (
                db.query(m.Reconciliation)
                .filter(
                    m.Reconciliation.account_id == bank_id,
                    m.Reconciliation.end >= correction.date,
                )
                .order_by(m.Reconciliation.end.desc())
                .first()
            )
            if (
                not recon
                or balance(db, bank_id, recon.end, fund_id) < 0
                or balance(db, bank_id, recon.end) != recon.ending_cents
            ):
                fail(
                    "unresolved_shortage",
                    "Restore supported holdings and reconcile the bank before resolving this shortage",
                )
        row.resolved_event_id = correction.id
    row.status = "resolved"
    row.resolution = data.evidence
    return {"review_id": row.id, "resolved": True}


OPERATIONS.update(
    end_ownership=end_ownership,
    gross_closing=gross_closing,
    resolve_review=resolve_review,
)


def return_settlement(db, data, actor):
    return settle(db, data, actor, returned=True)


OPERATIONS["return_settlement"] = return_settlement


def create_reporting_group(db, data, actor):
    if data.end and data.end < data.start:
        fail("group_dates", "Reporting perimeter end precedes its start")
    if len(set(data.entity_ids)) != len(data.entity_ids):
        fail("duplicate_member", "Each entity appears once in a reporting group")
    for ident in data.entity_ids:
        get(db, m.Entity, ident)
    group = m.ReportingGroup(**data.model_dump(exclude={"entity_ids"}))
    db.add(group)
    db.flush()
    for ident in data.entity_ids:
        db.add(m.GroupMember(group_id=group.id, entity_id=ident))
    return {"group_id": group.id, "entity_ids": data.entity_ids}


def group_adjustment(db, data, actor):
    group = get(db, m.ReportingGroup, data.group_id)
    if data.date < group.start or (group.end and data.date > group.end):
        fail(
            "group_dates", "Adjustment date is outside the approved reporting perimeter"
        )
    members = {
        x.entity_id for x in db.query(m.GroupMember).filter_by(group_id=group.id)
    }
    if sum(x.debit_cents - x.credit_cents for x in data.lines):
        fail(
            "unbalanced",
            "Consolidation adjustment must balance across the reporting group",
        )
    for ln in data.lines:
        sc = scope(db, ln.account_id)
        if sc.entity_id not in members:
            fail("group_scope", "Adjustment account is outside the reporting group")
        check_date(db, sc.entity_id, data.date)
        # Bank cash and reciprocal controls are never created or counted twice by group workpapers.
        if sc.role in (
            "bank",
            "cash_equivalent",
            "due_from",
            "due_to",
            "custody_asset",
            "custody_liability",
        ):
            fail(
                "group_control",
                "Cash and reciprocal controls must be corrected in source books; reciprocal balances eliminate automatically",
            )
        validate_lines(db, sc.entity_id, [ln], balanced=False)
    cash_total = 0
    for ident in set(data.cash_flow_event_ids):
        event = get(db, m.Event, ident)
        if event.date > data.date:
            fail("group_cash_date", "Cash elimination cannot precede its source event")
        entries = (
            db.query(TransactionLine, m.AccountScope)
            .join(
                m.AccountScope, m.AccountScope.account_id == TransactionLine.account_id
            )
            .join(
                m.JournalScope,
                m.JournalScope.transaction_id == TransactionLine.transaction_id,
            )
            .filter(
                m.JournalScope.event_id == ident,
                m.AccountScope.role.in_(["bank", "cash_equivalent"]),
            )
            .all()
        )
        if not entries or any(sc.entity_id not in members for _, sc in entries):
            fail(
                "group_cash_scope",
                "Every selected cash event must belong to this reporting group",
            )
        cash_total += sum(cents(ln.debit) - cents(ln.credit) for ln, _ in entries)
    if cash_total:
        fail(
            "group_cash_balance",
            "Identified internal cash events must net to zero across the group",
        )
    row = m.GroupAdjustment(
        **data.model_dump(mode="python", exclude={"lines"}),
        lines=[x.model_dump() for x in data.lines],
    )
    db.add(row)
    db.flush()
    return {"group_adjustment_id": row.id, "legal_entity_books_changed": False}


def record_policy(db, data, actor):
    get(db, m.Entity, data.entity_id)
    if data.end and data.end < data.start:
        fail("policy_dates", "Policy end precedes its start")
    if data.supersedes_id:
        old = get(db, m.ReportingPolicy, data.supersedes_id)
        if old.entity_id != data.entity_id or old.topic != data.topic:
            fail("policy_scope", "Policy revision must retain entity and topic")
        if db.query(m.ReportingPolicy).filter_by(supersedes_id=old.id).first():
            fail("policy_version", "Revise the latest policy version")
    row = m.ReportingPolicy(**data.model_dump())
    db.add(row)
    db.flush()
    return {"policy_id": row.id}


OPERATIONS.update(
    create_reporting_group=create_reporting_group,
    group_adjustment=group_adjustment,
    record_policy=record_policy,
)
